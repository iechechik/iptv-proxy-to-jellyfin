"""
services/channel_events.py — журнал событий каналов.

Формат строки (одна строка = одно событие):
  YYYY-MM-DD HH:MM:SS [Канал] cfg|state: <событие> (<source>)

cfg   — конфигурационные изменения (пользователь через UI, авто-подбор EPG).
state — состояние канала (enabled/disabled, UP/DOWN, авто-switch активного).

Пишем только то, что привело к последствиям. Пробу и метрики — не пишем.

Файл: logs/channel_events.log (путь из core.config.IPTV_CHANNEL_EVENTS_LOG).
Ротация — те же параметры, что у основного лога.
"""
import logging
import logging.handlers as lh
import os

from core.config import (
    logger,
    IPTV_CHANNEL_EVENTS_LOG,
    IPTV_LOG_LEVEL,
    IPTV_LOG_MAX_BYTES,
    IPTV_LOG_BACKUP_COUNT,
)

_events_logger = logging.getLogger("channel-events")
_events_logger.setLevel(IPTV_LOG_LEVEL)
_events_logger.propagate = False


def _ensure_handler():
    if _events_logger.handlers:
        return
    try:
        os.makedirs(os.path.dirname(IPTV_CHANNEL_EVENTS_LOG), exist_ok=True)
    except Exception:
        pass
    try:
        h = lh.RotatingFileHandler(
            IPTV_CHANNEL_EVENTS_LOG,
            maxBytes=IPTV_LOG_MAX_BYTES,
            backupCount=IPTV_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        h.setFormatter(logging.Formatter("%(message)s"))
        _events_logger.addHandler(h)
    except Exception as e:
        logger.warning(f"[EVENTS] failed to open {IPTV_CHANNEL_EVENTS_LOG}: {e}")


def _short_url(url: str, limit: int = 50) -> str:
    """Обрезает URL до limit символов: начало...конец."""
    if not url:
        return ""
    url = url.split("|", 1)[0]  # отбрасываем |Referer=|Cookie=
    if len(url) <= limit:
        return url
    head = limit - 18
    tail = 15
    return f"{url[:head]}...{url[-tail:]}"


def _fmt_stream(stream_num, stream_url):
    """Формирует ' stream#N (url_short)' или ''."""
    if stream_num is None:
        return ""
    s = f" stream#{stream_num}"
    if stream_url:
        s += f" ({_short_url(stream_url)})"
    return s


def log_event(channel: str, event: str, source: str = "unknown",
              stream_num=None, stream_url=None):
    """Пишет одно событие в channel_events.log.

    channel    — имя канала (НТВ).
    event      — 'cfg: url: old -> new', 'state: enabled', 'state: UP -> DOWN'.
    source     — 'ui' | 'healthcheck' | 'fallback' | 'startup' | 'auto_match'.
    stream_num — номер потока (0-based) или None (для событий канала).
    stream_url — URL потока для читаемости (обрежется).
    """
    if not channel or not event:
        return
    _ensure_handler()
    try:
        from datetime import datetime as _dt
        stream_part = _fmt_stream(stream_num, stream_url)
        ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [{channel}]{stream_part} {event} ({source})"
        _events_logger.info(line)
    except Exception as e:
        logger.warning(f"[EVENTS] '{channel}': log failed: {e}")


def log_cfg(channel: str, event: str, source: str = "unknown",
            stream_num=None, stream_url=None):
    """Конфигурационное событие. event без префикса: 'url: old -> new'."""
    log_event(channel, f"cfg: {event}", source, stream_num, stream_url)


def log_state(channel: str, event: str, source: str = "unknown",
              stream_num=None, stream_url=None):
    """Событие состояния. event без префикса: 'enabled', 'UP -> DOWN'."""
    log_event(channel, f"state: {event}", source, stream_num, stream_url)
