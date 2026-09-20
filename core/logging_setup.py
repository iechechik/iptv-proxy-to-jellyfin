# core/logging_setup.py
import atexit
import logging
import logging.config
import logging.handlers as lh
import queue

from core.config import (
    IPTV_LOG_LEVEL,
    IPTV_LOG_MAX_BYTES,
    IPTV_LOG_BACKUP_COUNT,
)


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s - [%(name)s] - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "uvicorn": {
            "format": "%(asctime)s - [uvicorn] - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "level": logging.getLevelName(IPTV_LOG_LEVEL),
        },
        "console_uvicorn": {
            "class": "logging.StreamHandler",
            "formatter": "uvicorn",
            "level": logging.getLevelName(IPTV_LOG_LEVEL),
        },
        "file_iptv": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "/app/logs/iptv-proxy.log",
            "maxBytes": IPTV_LOG_MAX_BYTES,
            "backupCount": IPTV_LOG_BACKUP_COUNT,
            "encoding": "utf-8",
            "formatter": "default",
            "level": IPTV_LOG_LEVEL,
        },
    },
    "root": {
        "level": logging.getLevelName(IPTV_LOG_LEVEL),
        "handlers": ["console", "file_iptv"],
    },
    "loggers": {
        "uvicorn": {
            "level": logging.getLevelName(IPTV_LOG_LEVEL),
            "handlers": ["console_uvicorn"],
            "propagate": False,
        },
        "uvicorn.error": {
            "level": logging.getLevelName(IPTV_LOG_LEVEL),
            "handlers": ["console_uvicorn"],
            "propagate": False,
        },
        "uvicorn.access": {
            "level": logging.getLevelName(IPTV_LOG_LEVEL),
            "handlers": ["console_uvicorn"],
            "propagate": False,
        },
        "iptv-proxy": {
            "level": logging.getLevelName(IPTV_LOG_LEVEL),
            "handlers": ["console", "file_iptv"],
            "propagate": False,
        },
    },
}


class _DropQueueHandler(lh.QueueHandler):
    """QueueHandler, который дропает запись при переполнении, а не блокирует."""
    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass

def install_nonblocking_logging():
    """
    Переносит хендлеры логгеров в изолированные очереди по группам.

    Почему группы, а не один общий пул:
    QueueListener не знает, от какого логгера пришла запись, и играет
    её через ВСЕ свои хендлеры. Если смешать `console` (default-форматтер)
    и `console_uvicorn` (uvicorn-форматтер) в одном пуле, каждая
    [STREAM]-запись напечатается дважды — с обоими префиксами.

    Плюс исправлена давняя утечка: при повторной встрече хендлера
    использовался `continue`, из-за чего хендлер не снимался со второго
    логгера и продолжал писать напрямую (третья копия строки).
    Теперь removeHandler вызывается безусловно, а `seen` защищает
    только от дублирования в списке real.
    """
    from core.config import TruncateFilter, log_handler as _mem_handler

    _trunc = TruncateFilter()

    app_loggers = [logging.getLogger(n) for n in ("", "iptv-proxy")]
    uvicorn_loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access")]

    for lg in app_loggers + uvicorn_loggers:
        lg.addFilter(_trunc)

    def _drain(loggers):
        """Снимает все хендлеры с логгеров, возвращает список уникальных."""
        out, seen = [], set()
        for lg in loggers:
            for h in list(lg.handlers):
                lg.removeHandler(h)
                if id(h) in seen:
                    continue
                seen.add(id(h))
                out.append(h)
        return out

    app_real = _drain(app_loggers)
    uvicorn_real = _drain(uvicorn_loggers)

    # MemoryLogHandler — общий буфер для UI. Должен получать и app-записи,
    # и uvicorn-ошибки. uvicorn.access внутри emit() отфильтрован по имени.
    if not any(h is _mem_handler for h in app_real):
        app_real.append(_mem_handler)
    if not any(h is _mem_handler for h in uvicorn_real):
        uvicorn_real.append(_mem_handler)

    listeners = []

    def _start_pool(loggers, handlers):
        if not handlers:
            return
        q = queue.Queue(maxsize=50000)
        lst = lh.QueueListener(q, *handlers, respect_handler_level=True)
        lst.start()
        atexit.register(lst.stop)
        qh = _DropQueueHandler(q)
        qh.setLevel(logging.DEBUG)
        for lg in loggers:
            lg.addHandler(qh)
        listeners.append(lst)

    _start_pool(app_loggers, app_real)
    _start_pool(uvicorn_loggers, uvicorn_real)

    return listeners


def setup_logging():
    """Единая точка: применяет конфиг и оборачивает всё в QueueHandler.

    MemoryLogHandler навешивается внутри install_nonblocking_logging —
    после того, как dictConfig расставит свои хендлеры и _drain их снимет.
    Двойное добавление (здесь + там) давало дубли в UI-буфере.
    """
    logging.config.dictConfig(LOGGING_CONFIG)
    install_nonblocking_logging()

