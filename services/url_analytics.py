"""Временный сборщик статистики URL для анализа TTL-логики.

Задача — понять, покрывают ли текущие эвристики в resolver.py
(_extract_url_expiry, _is_session_url, _parse_cache_control_ttl) все
реальные URL из streams_cache.

Раз в INTERVAL_SECONDS проходит по _epg_cache, для каждого payload'а:
  - извлекает чистый URL (без |Referer|Cookie);
  - читает, что у нас в слоте (cache_expire, last_check_time, resolver);
  - делает HEAD (Range: bytes=0-0) к CDN — живой ли, что в Cache-Control;
  - прогоняет URL через _extract_url_expiry и _is_session_url;
  - пишет одну строку JSON в LOG_FILE.

Запускается один поток, свою работу делает молча, кроме старта/стопа.
Выключено по умолчанию (analytics.enabled=false в config.json).
Включается секцией "analytics" в config.json или env IPTV_ANALYTICS_ENABLED.
"""
import json
import os
import threading
import time
import urllib.request
import urllib.error
import logging
import logging.handlers as lh

from core.config import logger, IPTV_ANALYTICS_ENABLED, IPTV_ANALYTICS_INTERVAL, IPTV_ANALYTICS_MAX_BYTES, IPTV_ANALYTICS_BACKUP_COUNT
import core.state as state

# --- Настройки ---
# enabled/interval читаются из core.config (config.json → секция "analytics",
# либо env IPTV_ANALYTICS_ENABLED / IPTV_ANALYTICS_INTERVAL).
LOG_FILE = "/app/logs/url_analytics.jsonl"
HEAD_TIMEOUT = 5
_started = False


# Отдельный логгер для JSONL-файла. Не идёт через основной iptv-proxy
# (у того свой форматтер), не попадает в UI-буфер. Просто ротирует файл.
_jsonl_logger = logging.getLogger("url-analytics-jsonl")
_jsonl_logger.setLevel(logging.INFO)
_jsonl_logger.propagate = False


def _ensure_jsonl_handler():
    # Открывает файл-хендлер при первом использовании.
    if _jsonl_logger.handlers:
        return
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    _h = lh.RotatingFileHandler(
        LOG_FILE,
        maxBytes=IPTV_ANALYTICS_MAX_BYTES,
        backupCount=IPTV_ANALYTICS_BACKUP_COUNT,
        encoding="utf-8",
    )
    _h.setFormatter(logging.Formatter("%(message)s"))
    _jsonl_logger.addHandler(_h)


def _extract_url_clean(payload: str) -> str:
    if not payload:
        return ""
    return payload.split("|", 1)[0]


def _load_resolver_helpers():
    """Импорт приватных хелперов из resolver.py. Если их нет —
    просто пропускаем эти поля, статистика по остальным сохранится."""
    try:
        from services.resolver import (
            _extract_url_expiry, _is_session_url, _SESSION_URL_MARKERS,
        )
        return _extract_url_expiry, _is_session_url, _SESSION_URL_MARKERS
    except Exception as e:
        logger.warning(f"[URL-ANALYTICS] helpers import failed: {e}")
        return None, None, None


def _try_request(url: str, method: str, extra_headers: dict = None):
    """Один HTTP-запрос. Возвращает (status, cache_control, error)."""
    try:
        req = urllib.request.Request(url, method=method)
        if extra_headers:
            for k, v in extra_headers.items():
                req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=HEAD_TIMEOUT) as resp:
            cc = resp.headers.get("Cache-Control", "") or ""
            return resp.status, cc, None
    except urllib.error.HTTPError as e:
        # 4xx/5xx — возвращаем как есть, не как исключение
        return e.code, "", f"HTTP {e.code}"
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"


def _head_probe(url: str):
    """HEAD, при статусе >= 400 или ошибке — Range GET.
    Возвращает (status, cache_control, error, method_used).

    Многие CDN отвечают 403/405 на HEAD, но отдают данные на GET.
    И наоборот: xxxx.tv может висеть на HEAD, но отдавать GET (или
    наоборот). Поэтому если HEAD не дал 2xx — обязательно пробуем
    Range GET, чтобы отличить «мертвый URL» от «HEAD не поддержан».
    """
    if not url:
        return None, "", "empty url", None

    status, cc, err = _try_request(url, "HEAD")
    if status is not None and 200 <= status < 300:
        return status, cc, err, "HEAD"

    # HEAD не 2xx или упал — пробуем Range GET
    get_status, get_cc, get_err = _try_request(
        url, "GET", {"Range": "bytes=0-0"}
    )
    if get_status is not None and 200 <= get_status < 300:
        # GET отработал — это фактический статус URL
        return get_status, get_cc, None, "GET-Range"

    # Оба не прошли. Возвращаем более информативный результат:
    # приоритет — тот, где есть HTTP-статус.
    if get_status is not None:
        return get_status, get_cc, get_err, "GET-Range"
    if status is not None:
        return status, cc, err, "HEAD"
    return None, "", f"HEAD: {err}; GET: {get_err}", None


def _snapshot() -> list:
    """Снимок _epg_cache под локом — плоский список записей."""
    snap = []
    with state.cache_lock:
        for name, entry in state._epg_cache.items():
            if not isinstance(entry, dict):
                continue
            streams = entry.get("streams_cache", [])
            if not isinstance(streams, list):
                continue
            for idx, slot in enumerate(streams):
                if not isinstance(slot, dict):
                    continue
                payload = slot.get("cached_stream") or ""
                if not payload:
                    continue
                snap.append({
                    "channel": name,
                    "slot_idx": idx,
                    "slot": dict(slot),
                    "payload": payload,
                })
    return snap


def _collect_config_resolvers() -> dict:
    """{name: [resolver_0, resolver_1, ...]} для сопоставления по каналу."""
    out = {}
    try:
        for ch in state.load_channels():
            out[ch["name"]] = [
                (s.get("resolver") if isinstance(s, dict) else None)
                for s in ch.get("streams", [])
            ]
    except Exception as e:
        logger.warning(f"[URL-ANALYTICS] load_channels failed: {e}")
    return out


def _one_pass(extract_expiry, is_session, markers):
    now = time.time()
    cfg_resolvers = _collect_config_resolvers()
    snap = _snapshot()
    records = []
    for item in snap:
        channel = item["channel"]
        idx = item["slot_idx"]
        slot = item["slot"]
        payload = item["payload"]
        url = _extract_url_clean(payload)

        # resolver из config, если есть; иначе из last_check_detail
        resolver = None
        rs = cfg_resolvers.get(channel, [])
        if 0 <= idx < len(rs):
            resolver = rs[idx]
        if not resolver:
            detail = slot.get("last_check_detail", "")
            if "Resolved via " in detail:
                resolver = detail.split("Resolved via ", 1)[1].strip()

        cache_expire = slot.get("cache_expire", 0)
        left = int(cache_expire - now) if cache_expire else None

        url_expire = None
        if extract_expiry:
            try:
                url_expire = extract_expiry(url)
            except Exception:
                pass

        is_sess = False
        found_markers = []
        if is_session:
            try:
                is_sess = is_session(url)
            except Exception:
                pass
        if markers:
            low = url.lower()
            found_markers = [m for m in markers if m in low]

        # Если payload требует Referer/Cookie — probe без них врёт (403).
        # Такие URL пропускаем, статус из анализа не вытащить.
        if "|Referer=" in payload or "|Cookie=" in payload:
            http_status, cache_control, error, method_used = (
                None, "", "requires headers (skipped)", None
            )
        else:
            # Пауза между probe'ами: короткая жизнь сокетов + десятки
            # URL подряд = ephemeral port exhaustion (Errno 99).
            time.sleep(0.15)
            http_status, cache_control, error, method_used = _head_probe(url)

        records.append({
            "ts": now,
            "channel": channel,
            "slot_idx": idx,
            "url": url[:300],
            "resolver": resolver,
            "cache_expire": cache_expire,
            "left_sec": left,
            "url_expire_ts": url_expire,
            "is_session_url": is_sess,
            "session_markers_found": found_markers,
            "http_status": http_status,
            "cache_control": cache_control,
            "error": error,
            "probe_method": method_used,
        })
    return records


def _loop():
    _ensure_jsonl_handler()
    extract_expiry, is_session, markers = _load_resolver_helpers()
    # Первый проход — через 30 секунд после старта, чтобы всё прогрелось.
    time.sleep(30)
    while True:
        try:
            records = _one_pass(extract_expiry, is_session, markers)

            try:
                for r in records:
                    _jsonl_logger.info(json.dumps(r, ensure_ascii=False))
                logger.info(f"[URL-ANALYTICS] wrote {len(records)} records")
            except Exception as e:
                logger.warning(f"[URL-ANALYTICS] write failed: {e}")
        except Exception as e:
            logger.warning(f"[URL-ANALYTICS] loop error: {e}")
        time.sleep(IPTV_ANALYTICS_INTERVAL)


def start_analytics():
    """Запускает фоновый поток. Если enabled=False — молча выходит."""
    global _started
    if not IPTV_ANALYTICS_ENABLED:
        return
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="url-analytics").start()
    logger.info(f"[URL-ANALYTICS] started (interval={IPTV_ANALYTICS_INTERVAL}s, log={LOG_FILE})")
