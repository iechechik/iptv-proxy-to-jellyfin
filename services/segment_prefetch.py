"""
Фоновая подкачка HLS-сегментов.

Проблема: медленные источники отдают сегмент 1 МБ
за 6-10 секунд. Jellyfin запрашивает сегмент и ждёт, пока он доедет —
буфер пустеет, воспроизведение рывками.

Решение: пока клиент играет текущий сегмент, мы в фоне качаем следующие.
Когда Jellyfin приходит за сегментом, он уже в кэше — отдаём мгновенно.

Все параметры через env-переменные, см. core/config.py:
  IPTV_PREFETCH_MAX_WORKERS      — воркеров на подкачку (default 2)
  IPTV_PREFETCH_SEGMENT_TTL      — сколько секунд держать сегмент (default 60)
  IPTV_PREFETCH_MAX_CACHE_MB     — лимит кэша в МБ (default 100)
  IPTV_PREFETCH_FETCH_SEMAPHORE  — одновременных fetch к источнику (default 4)

Модуль изолирован: свой кэш, свои локи, свой ThreadPool.
Ничего в core/state.py не трогает.
"""
import threading
import time
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from core.config import (
    logger, IPTV_FETCH_TIMEOUT,
    IPTV_PREFETCH_MAX_WORKERS, IPTV_PREFETCH_SEGMENT_TTL,
    IPTV_PREFETCH_MAX_CACHE_MB, IPTV_PREFETCH_FETCH_SEMAPHORE,
)


_SEGMENT_TTL = IPTV_PREFETCH_SEGMENT_TTL
_MAX_CACHE_BYTES = IPTV_PREFETCH_MAX_CACHE_MB * 1024 * 1024

# --- Кэш скачанных сегментов ---
# url -> (bytes, expire_time)
_SEGMENT_CACHE: OrderedDict = OrderedDict()
_SEGMENT_CACHE_LOCK = threading.Lock()

_current_bytes = 0
_current_bytes_lock = threading.Lock()

# Очередь задач на prefetch.
_prefetch_pool = ThreadPoolExecutor(
    max_workers=IPTV_PREFETCH_MAX_WORKERS,
    thread_name_prefix="segpf",
)

# Что уже в работе — чтобы не дублировать запросы.
_in_progress = set()
_in_progress_lock = threading.Lock()

# Потолок одновременных задач prefetch. ThreadPoolExecutor(max_workers=2)
# берёт из своей очереди по 2 задачи, но сама очередь не ограничена:
# если Jellyfin перезапросит манифест 10 раз подряд, туда уедет 30 задач.
# Каждая — fetch того же сегмента, если предыдущий не докачался.
# Ограничиваем по _in_progress: при достижении потолка новые задачи
# в пул не ставим. 30 = 10 циклов манифеста при PREFETCH_MAX_SEGMENTS=3.
MAX_IN_PROGRESS = 30

# Ограничитель одновременных fetch к источникам (prefetch + Jellyfin).
_fetch_semaphore = threading.Semaphore(IPTV_PREFETCH_FETCH_SEMAPHORE)


# prefetch-summary-v1
# fetched-лог на каждый сегмент — шумно (10+ строк/мин на активный канал).
# Вместо этого:
#   - первый успешный fetch на канал — INFO (подтверждение, что prefetch
#     вообще работает);
#   - дальше — DEBUG (видно только при IPTV_LOG_LEVEL=debug);
#   - раз в _SUMMARY_INTERVAL_SEC — INFO-строка с агрегатом.
_SUMMARY_INTERVAL_SEC = 300  # 5 минут

# {channel_name: True} — первый fetch уже залогирован в этом процессе.
_first_fetch_logged = set()
_first_fetch_logged_lock = threading.Lock()

# Счётчики за период summary.
_sum_segments = 0
_sum_bytes = 0
_sum_time_sec = 0.0
_sum_lock = threading.Lock()
_last_summary_ts = time.time()


def get_cached_segment(url: str):
    """Возвращает байты из кэша или None."""
    with _SEGMENT_CACHE_LOCK:
        entry = _SEGMENT_CACHE.get(url)
        if not entry:
            return None
        data, expire = entry
        if expire > time.time():
            _SEGMENT_CACHE.move_to_end(url)
            return data
        # Протух — удаляем
        del _SEGMENT_CACHE[url]
        global _current_bytes
        with _current_bytes_lock:
            _current_bytes -= len(data)
        return None


def put_cached_segment(url: str, data: bytes) -> None:
    global _current_bytes
    with _SEGMENT_CACHE_LOCK:
        old = _SEGMENT_CACHE.get(url)
        if old:
            with _current_bytes_lock:
                _current_bytes -= len(old[0])
        _SEGMENT_CACHE[url] = (data, time.time() + _SEGMENT_TTL)
        _SEGMENT_CACHE.move_to_end(url)
        with _current_bytes_lock:
            _current_bytes += len(data)
        # Trim: выкидываем самые старые, пока не влезем в лимит
        while _current_bytes > _MAX_CACHE_BYTES and _SEGMENT_CACHE:
            _, (evicted, _) = _SEGMENT_CACHE.popitem(last=False)
            with _current_bytes_lock:
                _current_bytes -= len(evicted)


def _fetch_segment_sync(url: str, headers: dict):
    with _fetch_semaphore:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=IPTV_FETCH_TIMEOUT) as resp:
                return resp.read()
        except Exception as e:
            logger.debug(f"[PREFETCH] {type(e).__name__}: {url[:100]}")
            return None


def _do_prefetch(url: str, headers: dict, channel: str = None) -> None:
    global _sum_segments, _sum_bytes, _sum_time_sec
    try:
        if get_cached_segment(url) is not None:
            return
        t0 = time.time()
        data = _fetch_segment_sync(url, headers)
        if data:
            put_cached_segment(url, data)
            elapsed = time.time() - t0
            tag = channel or '?'

            # prefetch-summary-v1: первый fetch на канал — INFO,
            # дальше — DEBUG. Плюс агрегат для summary.
            _is_first = False
            with _first_fetch_logged_lock:
                if tag not in _first_fetch_logged:
                    _first_fetch_logged.add(tag)
                    _is_first = True

            if _is_first:
                logger.info(f"[PREFETCH] [{tag}] first fetch: {len(data)} bytes in {elapsed:.2f}s")
            else:
                logger.debug(f"[PREFETCH] [{tag}] fetched {len(data)} bytes in {elapsed:.2f}s: {url[:100]}")

            with _sum_lock:
                _sum_segments += 1
                _sum_bytes += len(data)
                _sum_time_sec += elapsed
    finally:
        with _in_progress_lock:
            _in_progress.discard(url)


def schedule_prefetch(urls: list, headers: dict, channel: str = None) -> None:
    """Ставит URL сегментов на фоновую загрузку.

    Идемпотентна: уже закэшированные и уже качающиеся URL пропускаются.
    Вызывается из proxy_hls_manifest при каждом обновлении плейлиста.
    channel — только для читаемости логов.
    """
    scheduled = 0
    skipped_by_limit = 0
    for url in urls:
        if get_cached_segment(url) is not None:
            continue
        with _in_progress_lock:
            if url in _in_progress:
                continue
            # Потолок одновременных задач. При достижении — все
            # оставшиеся URL откладываем (break, не continue: лимит
            # общий для всего списка, дальше проверять смысла нет).
            if len(_in_progress) >= MAX_IN_PROGRESS:
                skipped_by_limit = len(urls) - scheduled
                break
            _in_progress.add(url)
        _prefetch_pool.submit(_do_prefetch, url, headers, channel)
        scheduled += 1
    if scheduled:
        tag = channel or '?'
        logger.debug(f"[PREFETCH] [{tag}] scheduled {scheduled} segments")
    if skipped_by_limit:
        tag = channel or '?'
        logger.debug(f"[PREFETCH] [{tag}] {skipped_by_limit} segments skipped, "
                     f"already {MAX_IN_PROGRESS} in progress")


def _maybe_log_summary() -> None:
    """prefetch-summary-v1: раз в _SUMMARY_INTERVAL_SEC — агрегат."""
    global _sum_segments, _sum_bytes, _sum_time_sec, _last_summary_ts
    now = time.time()
    if now - _last_summary_ts < _SUMMARY_INTERVAL_SEC:
        return
    with _sum_lock:
        segs = _sum_segments
        byts = _sum_bytes
        secs = _sum_time_sec
        _sum_segments = 0
        _sum_bytes = 0
        _sum_time_sec = 0.0
        _last_summary_ts = now
    if segs == 0:
        return
    avg_kbps = (byts / 1024.0 / secs) if secs > 0 else 0.0
    logger.info(
        f"[PREFETCH] summary (last {_SUMMARY_INTERVAL_SEC}s): "
        f"{segs} segments, {byts / 1024.0 / 1024.0:.1f} MB, avg {avg_kbps:.0f} KB/s"
    )


def cleanup_expired_segments() -> None:
    """Периодическая очистка. Вызывается из background_cleanup."""
    global _current_bytes
    now = time.time()
    expired_count = 0
    with _SEGMENT_CACHE_LOCK:
        expired = [u for u, (_, exp) in _SEGMENT_CACHE.items() if exp <= now]
        for u in expired:
            data, _ = _SEGMENT_CACHE.pop(u)
            with _current_bytes_lock:
                _current_bytes -= len(data)
            expired_count += 1
    if expired_count:
        logger.debug(f"[PREFETCH] cleaned {expired_count} expired segments")
    # prefetch-summary-v1: попутно проверяем, не пора ли вывести summary.
    # background_cleanup вызывает нас каждые 60 сек — этого достаточно.
    try:
        _maybe_log_summary()
    except Exception as e:
        logger.warning(f"[PREFETCH] summary failed: {e}")
