import re
import os
import json
import logging
import time
from collections import deque
from typing import Any, Optional

# ---------- Базовый логгер (создаётся сразу, чтобы его можно было использовать везде) ----------
logger = logging.getLogger("iptv-proxy")
if not logger.handlers:
    _default_handler = logging.StreamHandler()
    _default_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(_default_handler)
    logger.setLevel(logging.INFO)

# ---------- Вспомогательные функции ----------
def strip_json_comments(text: str) -> str:
    """Удаляет // и /* */ комментарии, не трогая строки."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    lines = []
    in_string = False
    for line in text.splitlines():
        new_line = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '"' and (i == 0 or line[i-1] != '\\'):
                in_string = not in_string
                new_line.append(ch)
                i += 1
            elif line[i:i+2] == '//' and not in_string:
                break
            else:
                new_line.append(ch)
                i += 1
        lines.append(''.join(new_line))
    return '\n'.join(lines)

def load_json_with_comments(file_path: str) -> tuple[Optional[dict], Optional[str]]:
    if not os.path.exists(file_path):
        return None, None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = f.read()
        clean = strip_json_comments(raw)
        # Удаляем висячие запятые перед закрывающими скобками (JSON5-style)
        clean = re.sub(r',(\s*[}\]])', r'\1', clean)
        try:
            data = json.loads(clean)
        except json.JSONDecodeError as e:
            # Extra data: после закрывающей скобки корневого объекта
            # остался мусор (например, от прерванной записи).
            # Обрезаем по последней } и пробуем ещё раз.
            if "Extra data" in str(e):
                last_brace = clean.rfind('}')
                if last_brace > 0:
                    clean2 = clean[:last_brace + 1]
                    data = json.loads(clean2)
                    logger.warning(
                        f"[CONFIG] {file_path}: обнаружен мусор после корневого JSON, "
                        f"обрезано по позиции {last_brace + 1}"
                    )
                else:
                    raise
            else:
                raise
        if not isinstance(data, dict):
            return None, f"Корневой элемент должен быть объектом, получен {type(data).__name__}"
        return data, None
    except Exception as e:
        logger.exception(f"[CONFIG] read/parse failed: {file_path}: {e}")
        return None, str(e)

def backup_file(file_path: str) -> None:
    if os.path.exists(file_path):
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = f"{file_path}.bak_{timestamp}"
        try:
            os.rename(file_path, backup_path)
            logger.warning(f"[CONFIG] corrupt file backed up as {backup_path}")
        except Exception as e:
            logger.error(f"[CONFIG] backup failed: {e}")

# ---------- Глобальные переменные ----------
CONFIG_FILE = os.getenv("IPTV_CONFIG_FILE", "config.json")
IPTV_OVERRIDE_FILE = os.getenv("IPTV_OVERRIDE_FILE", "config.override.json")
CONFIG_ERROR = None

IPTV_MANAGE_URL = os.getenv("IPTV_MANAGE_URL", "http://iptv-proxy:8000")
IPTV_BACKUP_FILE = os.getenv("IPTV_BACKUP_FILE", "channels.save")
IPTV_CACHE_FILE = os.getenv("IPTV_CACHE_FILE", "cache.json")

IPTV_EPG_DB_FILE = os.getenv("IPTV_EPG_DB_FILE", "epg.db")
IPTV_EPG_SOURCES_RAW = os.getenv("IPTV_EPG_SOURCES", "")
IPTV_EPG_CACHE_PATH = os.getenv("IPTV_EPG_CACHE_PATH", "epg_filtered.xml.gz")
IPTV_EPG_UPDATE_TIME = os.getenv("IPTV_EPG_UPDATE_TIME", "").strip()

IPTV_DEFAULT_UA = os.getenv("IPTV_DEFAULT_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36")
IPTV_PLAYWRIGHT_CHROMIUM = os.getenv("IPTV_PLAYWRIGHT_CHROMIUM", "/usr/bin/chromium")
IPTV_FLARESOLVERR_URL = os.getenv("IPTV_FLARESOLVERR_URL", "http://flaresolverr:8191/v1")

IPTV_JELLYFIN_URL = os.environ.get("IPTV_JELLYFIN_URL", "http://jellyfin:8096").rstrip("/")
IPTV_JELLYFIN_API_KEY = os.environ.get("IPTV_JELLYFIN_API_KEY", "").strip()
IPTV_JELLYFIN_XMLTV_CACHE_DIR = os.getenv("IPTV_JELLYFIN_XMLTV_CACHE_DIR", "/jellyfin-xmltv-cache")
IPTV_JELLYFIN_API_TIMEOUT = int(os.getenv("IPTV_JELLYFIN_API_TIMEOUT", "15"))

IPTV_LOG_LEVEL = os.getenv("IPTV_LOG_LEVEL", "info").upper()
IPTV_FFMPEG_LOG_LEVEL = os.getenv("IPTV_FFMPEG_LOG_LEVEL", "error")
IPTV_LOG_DIR = os.getenv("IPTV_LOG_DIR", "/app/logs")
IPTV_LOG_CAPACITY = int(os.getenv("IPTV_LOG_CAPACITY", "2000"))
IPTV_LOG_MAX_BYTES = int(os.getenv("IPTV_LOG_MAX_BYTES", "5242880"))   # 5 МБ
IPTV_LOG_BACKUP_COUNT = int(os.getenv("IPTV_LOG_BACKUP_COUNT", "3"))

IPTV_ANALYTICS_ENABLED = os.getenv("IPTV_ANALYTICS_ENABLED", "false").lower() in ("1", "true", "yes", "on")
IPTV_ANALYTICS_INTERVAL = int(os.getenv("IPTV_ANALYTICS_INTERVAL", "3600"))
IPTV_ANALYTICS_MAX_BYTES = int(os.getenv("IPTV_ANALYTICS_MAX_BYTES", "10485760"))
IPTV_ANALYTICS_BACKUP_COUNT = int(os.getenv("IPTV_ANALYTICS_BACKUP_COUNT", "3"))

IPTV_EPG_HEAD_TIMEOUT = int(os.getenv("IPTV_EPG_HEAD_TIMEOUT", "10"))
IPTV_EPG_DOWNLOAD_TIMEOUT = int(os.getenv("IPTV_EPG_DOWNLOAD_TIMEOUT", "60"))
IPTV_STREAMLINK_TIMEOUT = int(os.getenv("IPTV_STREAMLINK_TIMEOUT", "30"))
IPTV_FLARESOLVERR_TIMEOUT = int(os.getenv("IPTV_FLARESOLVERR_TIMEOUT", "70"))
IPTV_PLAYWRIGHT_NAVIGATION_TIMEOUT = int(os.getenv("IPTV_PLAYWRIGHT_NAVIGATION_TIMEOUT", "20")) * 1000
IPTV_FETCH_TIMEOUT = int(os.getenv("IPTV_FETCH_TIMEOUT", "15"))

IPTV_FAILED_RESOLVE_TTL = int(os.getenv("IPTV_FAILED_RESOLVE_TTL", "60"))
IPTV_YOUTUBE_CACHE_TTL = int(os.getenv("IPTV_YOUTUBE_CACHE_TTL", "1800"))
IPTV_CACHE_TTL = int(os.getenv("IPTV_CACHE_TTL", "3600"))
IPTV_FAST_CACHE_TTL = int(os.getenv("IPTV_FAST_CACHE_TTL", "600"))

IPTV_MUX_MAX_PROCESSES = int(os.getenv("IPTV_MUX_MAX_PROCESSES", "5"))
IPTV_MUX_IDLE_TIMEOUT = int(os.getenv("IPTV_MUX_IDLE_TIMEOUT", "90"))

IPTV_HEALTHCHECK_WORKERS = int(os.getenv("IPTV_HEALTHCHECK_WORKERS", "10"))
IPTV_HEALTHCHECK_INTERVAL = int(os.getenv("IPTV_HEALTHCHECK_INTERVAL", "30"))
IPTV_HEALTHCHECK_MIN_INTERVAL = int(os.getenv("IPTV_HEALTHCHECK_MIN_INTERVAL", "300"))
IPTV_HEALTHCHECK_FLARESOLVERR_FLAG_FILE = os.getenv("IPTV_HEALTHCHECK_FLARESOLVERR_FLAG_FILE", "")
IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC = int(os.getenv("IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC", "120"))
IPTV_FALLBACK_COOLDOWN = int(os.getenv("IPTV_FALLBACK_COOLDOWN", "120"))
IPTV_FALLBACK_SWITCH_MIN_SEC_ACTIVE = float(os.getenv("IPTV_FALLBACK_SWITCH_MIN_SEC_ACTIVE", "3.0"))
IPTV_FALLBACK_SWITCH_SPEEDUP_SEC = float(os.getenv("IPTV_FALLBACK_SWITCH_SPEEDUP_SEC", "1.0"))

IPTV_SNIFFER_LIMIT = int(os.getenv("IPTV_SNIFFER_LIMIT", "5"))
IPTV_FLARESOLVERR_LIMIT = int(os.getenv("IPTV_FLARESOLVERR_LIMIT", "5"))
IPTV_PROBE_LIMIT = int(os.getenv("IPTV_PROBE_LIMIT", "2"))
IPTV_RESOLVER_LIMIT = int(os.getenv("IPTV_RESOLVER_LIMIT", "5"))

# --- Prefetch ---
# Фоновая подкачка HLS-сегментов. Помогает медленным источникам:
# пока Jellyfin играет текущий сегмент, мы качаем следующие.
IPTV_PREFETCH_MAX_WORKERS = int(os.getenv("IPTV_PREFETCH_MAX_WORKERS", "2"))
IPTV_PREFETCH_SEGMENT_TTL = int(os.getenv("IPTV_PREFETCH_SEGMENT_TTL", "60"))
IPTV_PREFETCH_MAX_CACHE_MB = int(os.getenv("IPTV_PREFETCH_MAX_CACHE_MB", "100"))
IPTV_PREFETCH_FETCH_SEMAPHORE = int(os.getenv("IPTV_PREFETCH_FETCH_SEMAPHORE", "4"))
IPTV_PREFETCH_MAX_SEGMENTS = int(os.getenv("IPTV_PREFETCH_MAX_SEGMENTS", "3"))

IPTV_RESOLVER_ORDER = [
    "direct",
    "yt-dlp",
    "streamlink",
    "flaresolverr_simple",
    "flaresolverr_session",
    "sniffer",
]

# ---------- Загрузка config.json ----------
def deep_merge_config(base: dict, override: dict) -> dict:
    """Рекурсивно сливает override в base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge_config(result[key], value)
        elif key == "channels" and isinstance(result.get("channels"), list) and isinstance(value, list):
            # Слияние списков каналов по имени.
            # ВАЖНО: поля канала МЕРДЖИМ, а не заменяем объект целиком.
            # Иначе override {"name": "X", "prefetch": true} стёр бы у
            # канала X все streams/chno/group/tvgid, и канал выпал бы
            # из load_channels (там отсеиваются записи без streams).
            base_channels = {ch.get("name"): ch for ch in result.get("channels", []) if isinstance(ch, dict)}
            for ch in value:
                if not isinstance(ch, dict):
                    continue
                name = ch.get("name")
                if name in base_channels:
                    base = dict(base_channels[name])
                    if "streams" in ch and isinstance(ch.get("streams"), list):
                        base_streams = list(base.get("streams", []))
                        by_url = {s.get("url"): i for i, s in enumerate(base_streams) if isinstance(s, dict)}
                        for ns in ch["streams"]:
                            if not isinstance(ns, dict):
                                continue
                            ns_url = ns.get("url")
                            if ns_url and ns_url in by_url:
                                idx = by_url[ns_url]
                                merged_s = dict(base_streams[idx])
                                merged_s.update(ns)
                                base_streams[idx] = merged_s
                            else:
                                base_streams.append(ns)
                        base["streams"] = base_streams
                        for k, v in ch.items():
                            if k != "streams":
                                base[k] = v
                    else:
                        base.update(ch)
                    base_channels[name] = base
                else:
                    base_channels[name] = ch
            result["channels"] = list(base_channels.values())
        else:
            result[key] = value
    return result

CONFIG_DATA, CONFIG_ERROR = load_json_with_comments(CONFIG_FILE)
if CONFIG_ERROR:
    backup_file(CONFIG_FILE)
# Определяем имя файла оверлея из основного конфига (или env)
if CONFIG_DATA and isinstance(CONFIG_DATA.get("server"), dict):
    _override_from_config = CONFIG_DATA["server"].get("override_file")
    if _override_from_config:
        IPTV_OVERRIDE_FILE = _override_from_config

# Загружаем оверлей и сливаем с основным конфигом
if CONFIG_DATA is not None and IPTV_OVERRIDE_FILE:
    override_path = IPTV_OVERRIDE_FILE
    override_data, override_error = load_json_with_comments(override_path)
    if override_error:
        logger.error(f"[CONFIG] {override_path}: load failed: {override_error}")
    elif override_data is not None:
        logger.info(f"[CONFIG] {override_path}: loaded, applying overlay")
        CONFIG_DATA = deep_merge_config(CONFIG_DATA, override_data)
    else:
        logger.info(f"[CONFIG] {override_path}: not found, using main config only")

# ---------- Функция получения значения с приоритетом env > config > default ----------
def get_config_value(section: str, key: str, default: Any, config: Optional[dict] = None) -> Any:
    if config is None:
        config = CONFIG_DATA or {}

    # проверяем env (docker-compose)
    env_var = f"IPTV_{section.upper()}_{key.upper()}"
    env_val = os.getenv(env_var)
    if env_val is not None:
        if isinstance(default, bool):
            return env_val.lower() in ("1", "true", "yes", "on")
        if isinstance(default, int):
            try:
                return int(env_val)
            except ValueError:
                pass
        if isinstance(default, float):
            try:
                return float(env_val)
            except ValueError:
                pass
        if isinstance(default, list):
            return [x.strip() for x in env_val.split(",") if x.strip()]
        return env_val

    # проверяем config.json
    val = config.get(section, {}).get(key)
    if val is not None:
        return val

    # возвращаем default
    return default

# ---------- Применяем конфиг ----------
if CONFIG_DATA:
    IPTV_MANAGE_URL = get_config_value("server", "manage_url", IPTV_MANAGE_URL)
    IPTV_OVERRIDE_FILE = get_config_value("server", "override_file", IPTV_OVERRIDE_FILE)

    IPTV_EPG_DB_FILE = get_config_value("epg", "db_file", IPTV_EPG_DB_FILE)
    IPTV_EPG_CACHE_PATH = get_config_value("epg", "cache_path", IPTV_EPG_CACHE_PATH)
    IPTV_EPG_UPDATE_TIME = get_config_value("epg", "update_time", IPTV_EPG_UPDATE_TIME)
    IPTV_EPG_HEAD_TIMEOUT = get_config_value("epg", "head_timeout", IPTV_EPG_HEAD_TIMEOUT)
    IPTV_EPG_DOWNLOAD_TIMEOUT = get_config_value("epg", "download_timeout", IPTV_EPG_DOWNLOAD_TIMEOUT)

    IPTV_JELLYFIN_URL = get_config_value("jellyfin", "url", IPTV_JELLYFIN_URL)
    IPTV_JELLYFIN_API_KEY = get_config_value("jellyfin", "api_key", IPTV_JELLYFIN_API_KEY)
    IPTV_JELLYFIN_XMLTV_CACHE_DIR = get_config_value("jellyfin", "xmltv_cache_dir", IPTV_JELLYFIN_XMLTV_CACHE_DIR)
    IPTV_JELLYFIN_API_TIMEOUT = get_config_value("jellyfin", "api_timeout", IPTV_JELLYFIN_API_TIMEOUT)

    IPTV_LOG_LEVEL =  get_config_value("logging", "level", IPTV_LOG_LEVEL).upper()
    IPTV_LOG_DIR =  get_config_value("logging", "dir", IPTV_LOG_DIR)
    IPTV_LOG_CAPACITY = get_config_value("logging", "capacity", IPTV_LOG_CAPACITY)
    IPTV_LOG_MAX_BYTES = get_config_value("logging", "max_bytes", IPTV_LOG_MAX_BYTES)
    IPTV_LOG_BACKUP_COUNT = get_config_value("logging", "backup_count", IPTV_LOG_BACKUP_COUNT)

    IPTV_ANALYTICS_ENABLED = get_config_value("analytics", "enabled", False)
    IPTV_ANALYTICS_INTERVAL = get_config_value("analytics", "interval", 3600)
    IPTV_ANALYTICS_MAX_BYTES = get_config_value("analytics", "max_bytes", IPTV_ANALYTICS_MAX_BYTES)
    IPTV_ANALYTICS_BACKUP_COUNT = get_config_value("analytics", "backup_count", IPTV_ANALYTICS_BACKUP_COUNT)

    IPTV_DEFAULT_UA = get_config_value("resolver", "default_ua", IPTV_DEFAULT_UA)
    IPTV_PLAYWRIGHT_CHROMIUM = get_config_value("resolver", "playwright_chromium", IPTV_PLAYWRIGHT_CHROMIUM)
    IPTV_FLARESOLVERR_URL = get_config_value("resolver", "flaresolverr_url", IPTV_FLARESOLVERR_URL)
    IPTV_STREAMLINK_TIMEOUT = get_config_value("resolver", "streamlink_timeout", IPTV_STREAMLINK_TIMEOUT)
    IPTV_FLARESOLVERR_TIMEOUT = get_config_value("resolver", "flaresolverr_timeout", IPTV_FLARESOLVERR_TIMEOUT)
    IPTV_FETCH_TIMEOUT = get_config_value("resolver", "fetch_timeout", IPTV_FETCH_TIMEOUT)
    IPTV_RESOLVER_ORDER = get_config_value("resolver", "order", IPTV_RESOLVER_ORDER)

    nav_timeout_sec = CONFIG_DATA.get("resolver", {}).get("playwright_navigation_timeout")
    if nav_timeout_sec is not None:
        IPTV_PLAYWRIGHT_NAVIGATION_TIMEOUT = nav_timeout_sec * 1000

    IPTV_FAILED_RESOLVE_TTL = get_config_value("cache", "failed_resolve_ttl", IPTV_FAILED_RESOLVE_TTL)
    IPTV_YOUTUBE_CACHE_TTL = get_config_value("cache", "youtube_cache_ttl", IPTV_YOUTUBE_CACHE_TTL)
    IPTV_CACHE_TTL = get_config_value("cache", "cache_ttl", IPTV_CACHE_TTL)
    IPTV_FAST_CACHE_TTL = get_config_value("cache", "fast_cache_ttl", IPTV_FAST_CACHE_TTL)

    IPTV_MUX_MAX_PROCESSES = get_config_value("mux", "max_processes", IPTV_MUX_MAX_PROCESSES)
    IPTV_MUX_IDLE_TIMEOUT = get_config_value("mux", "idle_timeout", IPTV_MUX_IDLE_TIMEOUT)
    IPTV_FFMPEG_LOG_LEVEL = get_config_value("mux", "ffmpeg_log_level", IPTV_FFMPEG_LOG_LEVEL)

    IPTV_HEALTHCHECK_WORKERS = get_config_value("healthcheck", "workers", IPTV_HEALTHCHECK_WORKERS)
    IPTV_HEALTHCHECK_INTERVAL = get_config_value("healthcheck", "scheduler_interval", IPTV_HEALTHCHECK_INTERVAL)
    IPTV_HEALTHCHECK_MIN_INTERVAL = get_config_value("healthcheck", "scheduler_min_interval", IPTV_HEALTHCHECK_MIN_INTERVAL)
    IPTV_HEALTHCHECK_FLARESOLVERR_FLAG_FILE = get_config_value(
        "healthcheck", "flaresolverr_flag_file", IPTV_HEALTHCHECK_FLARESOLVERR_FLAG_FILE
    )
    IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC = get_config_value(
        "healthcheck", "recently_active_sec", IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC
    )
    IPTV_FALLBACK_COOLDOWN = get_config_value("fallback", "cooldown", IPTV_FALLBACK_COOLDOWN)
    IPTV_FALLBACK_SWITCH_MIN_SEC_ACTIVE = get_config_value("fallback", "switch_min_sec_active", IPTV_FALLBACK_SWITCH_MIN_SEC_ACTIVE)
    IPTV_FALLBACK_SWITCH_SPEEDUP_SEC = get_config_value("fallback", "switch_speedup_sec", IPTV_FALLBACK_SWITCH_SPEEDUP_SEC)

    IPTV_SNIFFER_LIMIT = get_config_value("limits", "sniffer", IPTV_SNIFFER_LIMIT)
    IPTV_FLARESOLVERR_LIMIT = get_config_value("limits", "flaresolverr", IPTV_FLARESOLVERR_LIMIT)
    IPTV_PROBE_LIMIT = get_config_value("limits", "probe", IPTV_PROBE_LIMIT)
    IPTV_RESOLVER_LIMIT = get_config_value("limits", "resolver", IPTV_RESOLVER_LIMIT)

    # Prefetch: приоритет env > дефолт. В config.json не выносим —
    # это тюнинг, а не пользовательские настройки.
    IPTV_PREFETCH_MAX_WORKERS = int(os.getenv("IPTV_PREFETCH_MAX_WORKERS", str(IPTV_PREFETCH_MAX_WORKERS)))
    IPTV_PREFETCH_SEGMENT_TTL = int(os.getenv("IPTV_PREFETCH_SEGMENT_TTL", str(IPTV_PREFETCH_SEGMENT_TTL)))
    IPTV_PREFETCH_MAX_CACHE_MB = int(os.getenv("IPTV_PREFETCH_MAX_CACHE_MB", str(IPTV_PREFETCH_MAX_CACHE_MB)))
    IPTV_PREFETCH_FETCH_SEMAPHORE = int(os.getenv("IPTV_PREFETCH_FETCH_SEMAPHORE", str(IPTV_PREFETCH_FETCH_SEMAPHORE)))
    IPTV_PREFETCH_MAX_SEGMENTS = int(os.getenv("IPTV_PREFETCH_MAX_SEGMENTS", str(IPTV_PREFETCH_MAX_SEGMENTS)))

# ---------- Перенастраиваем логгер с учётом конфига ----------
if not os.path.exists(IPTV_LOG_DIR):
    try:
        os.makedirs(IPTV_LOG_DIR, exist_ok=True)
    except Exception:
        pass

class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity=IPTV_LOG_CAPACITY):
        super().__init__()
        self.buffer = deque(maxlen=capacity)

    def emit(self, record):
        try:
            if record.name == "uvicorn.access":
                return
            msg = self.format(record)
            # Страховка, если фильтр не навешен на этот логгер:
            # обрезаем длинные сообщения, чтобы UI-логи оставались читаемыми.
            if len(msg) > 260:
                msg = msg[:180] + f"...[{len(msg) - 240} симв.]..." + msg[-60:]
            self.buffer.append(msg)
        except Exception:
            self.handleError(record)

class TruncateFilter(logging.Filter):
    """Обрезает слишком длинные сообщения, оставляя начало и конец.

    uvicorn.access пишет строку вида
      '172.18.0.12:46790 - "GET /hls/segment.ts?url=<2000 симв.>&cookie=... HTTP/1.1" 200'
    которую невозможно читать. Оставляем HEAD первых и TAIL последних
    символов (там обычно HTTP-статус ответа). Середину заменяем маркером
    с числом выброшенных символов.
    """
    HEAD = 90
    TAIL = 60
    MIN_LEN = 180

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if len(msg) <= self.MIN_LEN:
            return True
        cut = len(msg) - self.HEAD - self.TAIL
        record.msg = f"{msg[:self.HEAD]}...[{cut} симв.]...{msg[-self.TAIL:]}"
        record.args = ()
        return True

# Удаляем старые обработчики и добавляем новый
for h in logger.handlers[:]:
    logger.removeHandler(h)

log_handler = MemoryLogHandler(capacity=IPTV_LOG_CAPACITY)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log_handler.setFormatter(log_formatter)
logger.addHandler(log_handler)
logger.setLevel(IPTV_LOG_LEVEL)

root_logger = logging.getLogger()
cache_logger = logging.getLogger("cache-monitor")
root_logger.setLevel(IPTV_LOG_LEVEL)

# ---------- Сообщения о загрузке конфига ----------
if CONFIG_ERROR:
    logger.error(f"[CONFIG] config.json load failed: {CONFIG_ERROR}")
elif CONFIG_DATA is None:
    logger.info("[CONFIG] config.json not found, using env vars and defaults")
else:
    logger.info("[CONFIG] config.json loaded")

# ---------- Функции для EPG ----------
def parse_interval(interval_str) -> int:
    if isinstance(interval_str, int):
        return interval_str
    if isinstance(interval_str, float):
        return int(interval_str)
    if not isinstance(interval_str, str):
        return 86400
    interval_str = interval_str.strip().lower()
    if not interval_str:
        return 86400
    try:
        if interval_str.isdigit():
            return int(interval_str)
        if interval_str.endswith("h"):
            return int(float(interval_str[:-1]) * 3600)
        elif interval_str.endswith("d"):
            return int(float(interval_str[:-1]) * 86400)
        elif interval_str.endswith("m"):
            return int(float(interval_str[:-1]) * 60)
        elif interval_str.endswith("s"):
            return int(float(interval_str[:-1]))
        else:
            return int(interval_str)
    except ValueError:
        return 86400

def normalize_filter_config(filter_config: dict) -> dict:
    if not isinstance(filter_config, dict):
        return {"mode": "all", "match": "any", "ids": [], "names": []}
    mode = filter_config.get("mode", "all")
    if mode not in ("all", "whitelist", "blacklist"):
        mode = "all"
    match = filter_config.get("match", "any")
    if match not in ("any", "all"):
        match = "any"
    ids = filter_config.get("ids", [])
    if isinstance(ids, str):
        ids = [ids]
    names = filter_config.get("names", [])
    if isinstance(names, str):
        names = [names]
    return {
        "mode": mode,
        "match": match,
        "ids": [str(x).strip() for x in ids if str(x).strip()],
        "names": [str(x).strip() for x in names if str(x).strip()]
    }

def normalize_source_config(src: dict) -> Optional[dict]:
    url = src.get("url", "").strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    name = src.get("name", url)
    interval = parse_interval(src.get("interval", "24h"))
    filter_config = normalize_filter_config(src.get("filter", {}))
    disabled = src.get("disable", False)
    return {
        "name": name,
        "url": url,
        "interval": interval,
        "filter": filter_config,
        "disable": disabled,
        "comment": src.get("comment", "")
    }

def parse_epg_sources(raw: str) -> list:
    sources = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            url = part
            interval_str = "24h"
        else:
            url, interval_str = part.rsplit(":", 1)
            url = url.strip()
            interval_str = interval_str.strip().lower()
            valid_intervals = ("h", "d", "m", "s")
            if not (interval_str and interval_str[-1] in valid_intervals and interval_str[:-1].replace('.', '', 1).isdigit()):
                url = part
                interval_str = "24h"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        seconds = parse_interval(interval_str)
        sources.append({
            "name": url,
            "url": url,
            "interval": seconds,
            "filter": {"mode": "all", "match": "any", "ids": [], "names": []},
            "disable": False
        })
    return sources

def get_epg_sources():
    if CONFIG_DATA and isinstance(CONFIG_DATA.get("epg", {}).get("sources"), list):
        sources = []
        for src in CONFIG_DATA["epg"]["sources"]:
            norm = normalize_source_config(src)
            if norm and not norm.get("disable", False):
                sources.append(norm)
        if sources:
            return sources
    if IPTV_EPG_SOURCES_RAW:
        return parse_epg_sources(IPTV_EPG_SOURCES_RAW)
    return []

# ---------- Инициализация источников EPG ----------
IPTV_EPG_SOURCES = get_epg_sources()
logger.info(f"[CONFIG] loaded {len(IPTV_EPG_SOURCES)} EPG sources")

# ---------- Функция сохранения конфига ----------
def save_full_config(channels: list = None) -> bool:
    if CONFIG_DATA is None:
        logger.error("[CONFIG] no CONFIG_DATA, cannot save")
        return False

    # === Секции с настройками ===
    # Источники EPG (все, включая отключённые, из CONFIG_DATA)
    raw_sources = CONFIG_DATA.get("epg", {}).get("sources", [])
    epg_sources_clean = []
    for src_raw in raw_sources:
        s = normalize_source_config(src_raw)
        if not s:
            continue
        out = {
            "name": s["name"],
            "url": s["url"],
            "interval": s["interval"],
        }
        f = s.get("filter", {})
        if not (f.get("mode", "all") == "all" and
                f.get("match", "any") == "any" and
                not f.get("ids") and not f.get("names")):
            f_clean = {}
            if f.get("mode") != "all":
                f_clean["mode"] = f["mode"]
            if f.get("match") != "any":
                f_clean["match"] = f["match"]
            if f.get("ids"):
                f_clean["ids"] = f["ids"]
            if f.get("names"):
                f_clean["names"] = f["names"]
            out["filter"] = f_clean
        if s.get("disable", False):
            out["disable"] = True
        if s.get("comment"):
            out["comment"] = s["comment"]
        epg_sources_clean.append(out)

    CONFIG_DATA["epg"] = {
        "sources": epg_sources_clean,
        "update_time": IPTV_EPG_UPDATE_TIME,
        "cache_path": IPTV_EPG_CACHE_PATH,
        "db_file": IPTV_EPG_DB_FILE,
        "download_timeout": IPTV_EPG_DOWNLOAD_TIMEOUT,
        "head_timeout": IPTV_EPG_HEAD_TIMEOUT,
    }

    CONFIG_DATA["server"] = {
        "manage_url": IPTV_MANAGE_URL,
        "override_file": IPTV_OVERRIDE_FILE,
    }
    CONFIG_DATA["jellyfin"] = {
        "url": IPTV_JELLYFIN_URL,
        # API-KEY не сохраняем, значение read-only из ENV
        "xmltv_cache_dir": IPTV_JELLYFIN_XMLTV_CACHE_DIR,
        "api_timeout": IPTV_JELLYFIN_API_TIMEOUT,
    }
    CONFIG_DATA["resolver"] = {
        "default_ua": IPTV_DEFAULT_UA,
        "playwright_chromium": IPTV_PLAYWRIGHT_CHROMIUM,
        "flaresolverr_url": IPTV_FLARESOLVERR_URL,
        "order": IPTV_RESOLVER_ORDER,
        "streamlink_timeout": IPTV_STREAMLINK_TIMEOUT,
        "flaresolverr_timeout": IPTV_FLARESOLVERR_TIMEOUT,
        "playwright_navigation_timeout": IPTV_PLAYWRIGHT_NAVIGATION_TIMEOUT // 1000,
        "fetch_timeout": IPTV_FETCH_TIMEOUT,
    }
    CONFIG_DATA["cache"] = {
        "failed_resolve_ttl": IPTV_FAILED_RESOLVE_TTL,
        "youtube_cache_ttl": IPTV_YOUTUBE_CACHE_TTL,
        "cache_ttl": IPTV_CACHE_TTL,
        "fast_cache_ttl": IPTV_FAST_CACHE_TTL,
    }
    CONFIG_DATA["mux"] = {
        "max_processes": IPTV_MUX_MAX_PROCESSES,
        "idle_timeout": IPTV_MUX_IDLE_TIMEOUT,
        "ffmpeg_log_level": IPTV_FFMPEG_LOG_LEVEL,
    }
    CONFIG_DATA["healthcheck"] = {
        "workers": IPTV_HEALTHCHECK_WORKERS,
        "scheduler_interval": IPTV_HEALTHCHECK_INTERVAL,
        "scheduler_min_interval": IPTV_HEALTHCHECK_MIN_INTERVAL,
        "recently_active_sec": IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC,
        "flaresolverr_flag_file": IPTV_HEALTHCHECK_FLARESOLVERR_FLAG_FILE,
    }
    CONFIG_DATA["fallback"] = {
        "cooldown": IPTV_FALLBACK_COOLDOWN,
        "switch_min_sec_active": IPTV_FALLBACK_SWITCH_MIN_SEC_ACTIVE,
        "switch_speedup_sec": IPTV_FALLBACK_SWITCH_SPEEDUP_SEC,
    }
    CONFIG_DATA["limits"] = {
        "sniffer": IPTV_SNIFFER_LIMIT,
        "flaresolverr": IPTV_FLARESOLVERR_LIMIT,
        "probe": IPTV_PROBE_LIMIT,
        "resolver": IPTV_RESOLVER_LIMIT,
    }
    CONFIG_DATA["logging"] = {
        "level": IPTV_LOG_LEVEL,
        "dir": IPTV_LOG_DIR,
        "capacity": IPTV_LOG_CAPACITY,
        "max_bytes": IPTV_LOG_MAX_BYTES,
        "backup_count":  IPTV_LOG_BACKUP_COUNT,
    }
    CONFIG_DATA["analytics"] = {
        "enabled": IPTV_ANALYTICS_ENABLED,
        "interval": IPTV_ANALYTICS_INTERVAL,
        "max_bytes": IPTV_ANALYTICS_MAX_BYTES,
        "backup_count":  IPTV_ANALYTICS_BACKUP_COUNT,
    }

    # === Каналы ===
    if channels is None:
        channels = CONFIG_DATA.get("channels", [])

    cleaned_channels = []
    for ch in channels:
        new_ch = {
            "name": ch.get("name", ""),
            "chno": str(ch.get("chno", "")).strip(),
            "group": ch.get("group", ""),
            "tvgid": ch.get("tvgid", "")
        }
        if ch.get("real_name"):
            new_ch["real_name"] = ch["real_name"]
        logo = ch.get("logo", "")
        if logo:
            new_ch["logo"] = logo
        if ch.get("disable", False):
            new_ch["disable"] = True
        if ch.get("active_stream_index", 0) != 0:
            new_ch["active_stream_index"] = ch["active_stream_index"]

        streams = ch.get("streams", [])
        if not streams:
            streams = [{
                "url": ch.get("url", ""),
                "resolver": ch.get("resolver", "auto"),
                "ua": ch.get("ua", IPTV_DEFAULT_UA),
                "fs_regex": ch.get("fs_regex", ""),
                "disable": False
            }]

        normalized_streams = []
        for s in streams:
            stream_entry = {
                "url": s.get("url", ""),
                "resolver": s.get("resolver", "auto"),
                "ua": s.get("ua", IPTV_DEFAULT_UA),
            }
            # stream_id — identity стрима. Стабилен между сохранениями,
            # привязан к слоту в streams_cache. Без него при перезаписи
            # config.json из UI слоты съезжают.
            if isinstance(s.get("stream_id"), int) and s["stream_id"] > 0:
                stream_entry["stream_id"] = s["stream_id"]
            if s.get("fs_regex"):
                stream_entry["fs_regex"] = s["fs_regex"]
            if s.get("disable", False):
                stream_entry["disable"] = True
            if s.get("prefetch", False):
                stream_entry["prefetch"] = True
            if not stream_entry["ua"]:
                stream_entry.pop("ua", None)
            normalized_streams.append(stream_entry)

        new_ch["streams"] = normalized_streams
        if ch.get("fallback", False):
            new_ch["fallback"] = True
        comment = ch.get("comment", "")
        if comment:
            new_ch["comment"] = comment

        cleaned_channels.append(new_ch)

    CONFIG_DATA["channels"] = cleaned_channels

    # Формируем итоговый конфиг с явным порядком секций
    ordered_config = {
        "epg": CONFIG_DATA.get("epg"),
        "server": CONFIG_DATA.get("server"),
        "jellyfin": CONFIG_DATA.get("jellyfin"),
        "resolver": CONFIG_DATA.get("resolver"),
        "cache": CONFIG_DATA.get("cache"),
        "mux": CONFIG_DATA.get("mux"),
        "healthcheck": CONFIG_DATA.get("healthcheck"),
        "fallback": CONFIG_DATA.get("fallback"),
        "limits": CONFIG_DATA.get("limits"),
        "logging": CONFIG_DATA.get("logging"),
        "analytics": CONFIG_DATA.get("analytics"),
        "channels": CONFIG_DATA.get("channels"),
    }

    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ordered_config, f, ensure_ascii=False, indent=4)
        os.replace(tmp, CONFIG_FILE)
        logger.info(f"[CONFIG] full config saved to {CONFIG_FILE}")
        return True
    except Exception as e:
        logger.exception(f"[CONFIG] config.json save failed: {e}")
        return False
