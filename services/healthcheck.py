"""
Healthcheck: очередь + воркеры + планировщик.

Архитектура:

  Задача: check_channel(name, reason, force)
    reason: scheduled | stale | webhook
    force:  обойти recently_active (webhook)

  Источники задач (все через enqueue_check):
    Scheduler       — каналы с now >= next_check_at[name], не recently_active
    /redirect stale — см. routers/stream.py, reason=stale
    /redirect cold  — блокирующий resolve в хендлере, мимо очереди
    Webhook Stop    — routers/jellyfin.py, reason=webhook, force=True

  Воркер (на один канал):
    mux жив                              -> skip
    playing (recently_active, !force,
             reason != stale)            -> проверить всех КРОМЕ активного,
                                            switch НЕ делать, ждать Stop
    иначе (не играет)                    -> проверить ВСЕ стримы,
                                            при fallback=true выбрать лучший
                                            и переключиться

  Метод проверки одного стрима:
    fallback=true  -> ffprobe (нужен probe_elapsed для сравнения)
    fallback=false -> HEAD

  Резолв стрима при проверке:
    если слот имеет свежий payload (cache_expire > now) — используем его
    без резолва. Иначе — resolve_channel_payload.

  Критерий switch (services/healthcheck.py:_select_best_stream):
    1. лучший имеет больший score, ИЛИ
    2. score равны, активный медленнее >= fallback.switch_min_sec_active,
       и кандидат быстрее активного на >= fallback.switch_speedup_sec

  Расписание следующей проверки:
    base = cache_ttl (success) или fast_cache_ttl (fail)
    base = min(base, cache_expire активного - now) если известен
    base = max(base, healthcheck.scheduler_min_interval)
    next_check_at[name] = now + base

  Ограничители (services/limits.py):
    resolver_sem      — все резолвы (direct/yt-dlp/streamlink/sniffer/flare)
    sniffer_sem       — внутри resolver_sem, только Chromium
    flaresolverr_sem  — внутри resolver_sem, только FlareSolverr
    probe_sem         — ffprobe

  Используется также из:
    services/fallback.py   — switch на живой стрим при провале /redirect
    routers/channels.py    — ручные кнопки UI
    routers/stream.py      — FlareSolverr-фолбэк на сегментах

  run_healthcheck_async (старая обёртка) удалена. Точка входа — enqueue_check
  или trigger_check_all (для UI-кнопки).
"""

import time
import threading
import concurrent.futures
import queue

from core.config import (
    IPTV_HEALTHCHECK_MIN_INTERVAL, IPTV_HEALTHCHECK_WORKERS,
    IPTV_HEALTHCHECK_INTERVAL, IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC,
    logger,
)
import core.state as state
from services.events import send_broadcast_async
from services.segment_prefetch import cleanup_expired_segments
from services.mux_service import is_mux_alive_and_fresh


# ---------------------------------------------------------------------------
# Глобальные структуры
# ---------------------------------------------------------------------------

_executor = None
_task_queue = queue.Queue()
_scheduler_stop = threading.Event()

# {channel_name: timestamp, когда проверять}
_next_check_at = {}
# каналы, которые сейчас в работе (один воркер держит канал)
_active_checks = set()
# каналы, ожидающие в очереди (дедуп)
_queued_names = set()
_queue_lock = threading.Lock()

# Reason'ы
REASON_SCHEDULED = "scheduled"
REASON_STALE = "stale"
REASON_WEBHOOK = "webhook"


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def revalidate_channel_in_background(ch: dict):
    """SWR: положить stale-задачу в очередь. Вызывается из /redirect.

    НЕ резолвит здесь и сейчас — кладёт задачу. Иначе на каждый stale-запрос
    поднимается Chromium прямо в хендлере, что ровно та проблема, которую
    мы лечим.
    """
    enqueue_check(ch, reason=REASON_STALE, force=False)


def enqueue_check(ch: dict, reason: str = REASON_SCHEDULED, force: bool = False,
                  task_id: str = None) -> bool:
    """Кладёт канал в очередь. False — уже в очереди / в работе.

    Единая точка входа для scheduler, /redirect-stale, webhook, кнопки
    «Проверить все».
    """
    name = ch.get("name")
    if not name:
        return False
    with _queue_lock:
        if name in _queued_names or name in _active_checks:
            return False
        _queued_names.add(name)
    _task_queue.put(("check_channel", (name, ch, task_id, reason, force)))
    return True


def trigger_check_all(channels: list) -> str:
    """Кнопка «Проверить все». Создаёт task для UI, кладёт все каналы в очередь."""
    import uuid as _uuid
    task_id = f"check_{int(time.time())}_{_uuid.uuid4().hex[:8]}_{len(channels)}"
    with state._healthcheck_lock:
        state._healthcheck_tasks[task_id] = {
            "progress": 0,
            "total": len(channels),
            "results": {},
            "done": False,
            "complete_sent": False,
        }
    send_broadcast_async(event_type="healthcheck-start",
                         extra_data={"task_id": task_id, "total": len(channels)})

    queued = 0
    for ch in channels:
        if enqueue_check(ch, reason=REASON_SCHEDULED, force=False, task_id=task_id):
            queued += 1
        else:
            # Канал уже в очереди/работе — сразу закрываем его в task,
            # чтобы прогресс не завис.
            _finish_task_channel(ch["name"], task_id, {
                "success": False,
                "detail": "Already queued or in progress",
                "method": None,
                "streams_results": [],
            })

    # Если очередь оказалась пуста (все каналы уже в работе) — task надо
    # закрыть вручную, иначе UI-спиннер зависнет.
    with state._healthcheck_lock:
        task = state._healthcheck_tasks.get(task_id)
        if task and task["progress"] >= task["total"]:
            task["done"] = True
            task["finished_at"] = time.time()
            if not task.get("complete_sent"):
                send_broadcast_async(event_type="healthcheck-complete",
                                     extra_data={"task_id": task_id, "total": task["total"]})
                task["complete_sent"] = True
    return task_id


# ---------------------------------------------------------------------------
# Внутреннее: слоты
# ---------------------------------------------------------------------------

def _ensure_slot(name: str, index: int) -> dict:
    """Возвращает slot-словарь, создавая его при необходимости.
    Вызывать под state.cache_lock."""
    if name not in state._epg_cache or not isinstance(state._epg_cache[name], dict):
        state._epg_cache[name] = {"streams_cache": []}
    entry = state._epg_cache[name]
    streams = entry.setdefault("streams_cache", [])
    if not isinstance(streams, list):
        streams = []
        entry["streams_cache"] = streams
    while len(streams) <= index:
        streams.append({})
    if not isinstance(streams[index], dict):
        streams[index] = {}
    return streams[index]


def _set_slot_failure(name: str, index: int, detail: str):
    with state.cache_lock:
        s = _ensure_slot(name, index)
        s["last_check_time"] = time.time()
        s["last_check_success"] = False
        s["last_check_detail"] = detail


def _set_slot_success(name: str, index: int, detail: str):
    with state.cache_lock:
        s = _ensure_slot(name, index)
        s["last_check_time"] = time.time()
        s["last_check_success"] = True
        s["last_check_detail"] = detail


# ---------------------------------------------------------------------------
# Внутреннее: прогресс UI-задач
# ---------------------------------------------------------------------------

def _finish_task_channel(name: str, task_id: str, result: dict):
    """Закрывает канал в UI-задаче. Безопасно с task_id=None."""
    if not task_id:
        return
    with state._healthcheck_lock:
        task = state._healthcheck_tasks.get(task_id)
        if not task:
            return
        task["progress"] += 1
        task["results"][name] = result
        if task["progress"] >= task["total"]:
            task["done"] = True
            task["finished_at"] = time.time()
            if not task.get("complete_sent"):
                send_broadcast_async(event_type="healthcheck-complete",
                                     extra_data={"task_id": task_id, "total": task["total"]})
                task["complete_sent"] = True
        else:
            send_broadcast_async(event_type="healthcheck-progress",
                                 extra_data={
                                     "task_id": task_id,
                                     "progress": task["progress"],
                                     "total": task["total"],
                                     "name": name,
                                 })


# ---------------------------------------------------------------------------
# Внутреннее: одна проверка канала
# ---------------------------------------------------------------------------

def _worker():
    while True:
        try:
            task = _task_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if task is None:
            break
        task_type, payload = task
        if task_type != "check_channel":
            continue
        name, ch, task_id, reason, force = payload
        with _queue_lock:
            _queued_names.discard(name)
            _active_checks.add(name)
        try:
            _process_channel_check(name, ch, task_id, reason=reason, force=force)
        except Exception as e:
            logger.exception(f"[HEALTHCHECK] worker: '{name}' crashed: {e}")
            try:
                _finish_task_channel(name, task_id, {
                    "success": False, "detail": f"Worker crash: {e}",
                    "method": None, "streams_results": [],
                })
            except Exception:
                pass
        finally:
            with _queue_lock:
                _active_checks.discard(name)


# ---------------------------------------------------------------------------
# Планировщик
# ---------------------------------------------------------------------------

def _scheduler():
    """Раз в IPTV_HEALTHCHECK_INTERVAL секунд проходит по каналам и кладёт
    в очередь те, кому пора (now >= next_check_at), кроме recently_active."""
    while not _scheduler_stop.is_set():
        try:
            channels = state.load_channels()
            now = time.time()
            for ch in channels:
                name = ch.get("name")
                if not name:
                    continue
                if ch.get("disable", False):
                    continue

                # Мукс жив — не трогаем.
                try:
                    if is_mux_alive_and_fresh(name, max_stall=30):
                        continue
                except Exception:
                    pass

                # recently_active — не трогаем (stale-путь и webhook пробьют).
                with state.cache_lock:
                    last_active = state._last_active.get(name, 0)
                if (now - last_active) < IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC:
                    continue

                with state._healthcheck_lock:
                    next_at = _next_check_at.get(name)
                if next_at is not None and now < next_at:
                    continue

                enqueue_check(ch, reason=REASON_SCHEDULED, force=False)

            time.sleep(IPTV_HEALTHCHECK_INTERVAL)
        except Exception as e:
            logger.error(f"[HEALTHCHECK] scheduler error: {e}")
            time.sleep(10)


# ---------------------------------------------------------------------------
# Старт / стоп
# ---------------------------------------------------------------------------

def start_healthcheck_scheduler():
    global _executor
    if _executor is not None:
        return
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=IPTV_HEALTHCHECK_WORKERS)
    for _ in range(IPTV_HEALTHCHECK_WORKERS):
        _executor.submit(_worker)
    threading.Thread(target=_scheduler, daemon=True).start()

    # Стартовый разброс: first_check_at = now + i*5с.
    # Без разброса все каналы уйдут в очередь одним залпом на первом тике.
    try:
        now = time.time()
        for i, ch in enumerate(state.load_channels()):
            name = ch.get("name")
            if not name:
                continue
            with state._healthcheck_lock:
                _next_check_at[name] = now + i * 5
    except Exception as e:
        logger.warning(f"[HEALTHCHECK] startup stagger failed: {e}")

    logger.info(
        f"[HEALTHCHECK] started: workers={IPTV_HEALTHCHECK_WORKERS}, "
        f"interval={IPTV_HEALTHCHECK_INTERVAL}s, "
        f"recently_active={IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC}s, "
        f"min_interval={IPTV_HEALTHCHECK_MIN_INTERVAL}s"
    )


def stop_healthcheck_scheduler():
    global _executor
    _scheduler_stop.set()
    if _executor:
        for _ in range(IPTV_HEALTHCHECK_WORKERS):
            _task_queue.put(None)
        _executor.shutdown(wait=False)
        _executor = None


# ---------------------------------------------------------------------------
# Совместимость со старым API
# ---------------------------------------------------------------------------

def _cleanup_tmp_artifacts():
    """Чистка /tmp от артефактов Chromium/Playwright/Pulse.

    Chromium и Playwright оставляют временные каталоги в /tmp при каждом
    запуске sniffer. Они не удаляются сами и копятся сотнями за день
    (каждый — несколько МБ).

    Пороги:
      - org.chromium.*, playwright_*, playwright-* — старше 1 часа;
      - pulse-* — старше 24 часов (PulseAudio может держать его открытым
        длительное время, пока жив процесс).

    Логируем только если что-то реально удалено.
    """
    import os as _os
    import time as _time
    import glob as _glob
    import shutil as _shutil

    now = _time.time()
    # (паттерн, порог в секундах)
    rules = [
        ("/tmp/org.chromium.*",  3600),   # 1 час
        ("/tmp/playwright_*",    3600),   # 1 час
        ("/tmp/playwright-*",    3600),   # 1 час (артефакты)
        ("/tmp/pulse-*",        86400),   # 24 часа
    ]
    removed = 0
    for pattern, max_age in rules:
        for path in _glob.glob(pattern):
            try:
                if now - _os.path.getmtime(path) < max_age:
                    continue
                if _os.path.isdir(path):
                    _shutil.rmtree(path, ignore_errors=True)
                else:
                    _os.remove(path)
                removed += 1
            except Exception:
                pass
    if removed:
        logger.info(f"[CLEANUP] /tmp: removed {removed} stale artifacts")


_TMP_CLEANUP_INTERVAL_SEC = 600  # 10 минут
_last_tmp_cleanup = 0.0


def _kill_stale_chromium(max_age_sec: int = 90):
    """Убивает зависшие процессы Chromium/headless_shell старше max_age_sec.

    Playwright запускает Chromium в headless-режиме (headless_shell)
    или системный chromium. При зависании sniffer'а (баг Playwright,
    зацикливание JS-челленджа) процесс остаётся навсегда — плодятся
    зомби, съедают память, следующий sniffer не находит свободный порт.

    Правило простое: sniffer НЕ должен работать дольше
    navigation_timeout (20) + wait (15) + закрытие (5) = 40 сек.
    Порог 90 сек с запасом: живой sniffer никогда не дотянет до него.

    Убиваем по возрасту (etime), не по имени — чтобы не задеть
    уже закрывающиеся процессы.
    """
    import subprocess as _sp
    killed = 0
    patterns = ("headless_shell", "chrome", "chromium")
    try:
        # ps -eo pid,etimes,comm,args — etimes = elapsed seconds
        out = _sp.run(
            ["ps", "-eo", "pid=,etimes=,comm=,args="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception as e:
        logger.warning(f"[CLEANUP] ps failed: {e}")
        return
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            etimes = int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        args = parts[3]
        if not any(p in comm or p in args for p in patterns):
            continue
        # Исключаем сам этот python-процесс (в args будет 'chromium'
        # как подстрока, если мы его ищем в коде — но тут args реальные).
        if etimes < max_age_sec:
            continue
        try:
            import os as _os
            import signal as _sig
            _os.kill(pid, _sig.SIGKILL)
            killed += 1
        except Exception:
            pass
    if killed:
        logger.warning(f"[CLEANUP] killed {killed} stale chromium process(es) (> {max_age_sec}s)")


def background_cleanup():
    global _last_tmp_cleanup
    while True:
        time.sleep(60)
        state.cleanup_expired_caches()
        cleanup_expired_segments()
        _kill_stale_chromium(max_age_sec=90)
        # Чистка /tmp — реже, чем раз в 60 сек. glob по сотням файлов
        # на каждой итерации даёт лишний CPU. 10 минут более чем
        # достаточно: артефакты sniffer и так удаляются с порогом 1 час,
        # актуальны только отложенные удаления.
        now = time.time()
        if now - _last_tmp_cleanup >= _TMP_CLEANUP_INTERVAL_SEC:
            _cleanup_tmp_artifacts()
            _last_tmp_cleanup = now

        # Чистим _last_active от давно неактивных имён.
        with state.cache_lock:
            stale = [n for n, ts in state._last_active.items()
                     if time.time() - ts > 3600]
            for n in stale:
                state._last_active.pop(n, None)

        # Чистим UI-задачи старше часа.
        now = time.time()
        with state._healthcheck_lock:
            to_remove = [
                tid for tid, task in state._healthcheck_tasks.items()
                if task.get("done") and task.get("finished_at", 0) < now - 3600
            ]
            for tid in to_remove:
                state._healthcheck_tasks.pop(tid, None)


# healthcheck-refactor-v1
# Импорт _process_channel_check в конце файла: worker.py импортирует
# _ensure_slot/_set_slot_*/_finish_task_channel из этого модуля, которые
# к моменту этого импорта уже определены.
from services.healthcheck_worker import _process_channel_check  # noqa: E402
