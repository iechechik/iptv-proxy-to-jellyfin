import os
import json
import time
import threading
import re
import copy
import secrets
from core.config import (
    IPTV_CACHE_FILE, IPTV_DEFAULT_UA, logger, IPTV_BACKUP_FILE, save_full_config,
    IPTV_JELLYFIN_MEDIAINFO_DIR, IPTV_MANAGE_URL,
)
from services.mux_service import invalidate_mux

# CONFIG_DATA читаем динамически через _cfg.CONFIG_DATA: reload_config
# переприсваивает core.config.CONFIG_DATA, а локальный bind,
# сделанный через `from core.config import CONFIG_DATA`, этого
# не видит — остаётся указатель на старый объект.
import core.config as _cfg

# ---------- Глобальные блокировки ----------
cache_lock = threading.RLock()          # защищает _epg_cache
save_lock = threading.Lock()            # защищает запись cache.json
channels_lock = threading.RLock()       # защищает _channels_memory

# ---------- Единый кэш рантайм-состояния ----------
# _epg_cache[name] = {
#     "active_stream_index": int,
#     "streams_cache": [
#         {
#             "cached_stream": str,        # payload (URL или синтетический манифест)
#             "cache_is_direct": bool,
#             "cache_expire": float,       # unixtime
#             "last_check_time": float,
#             "last_check_success": bool,
#             "last_check_detail": str,
#             "probe_elapsed": float,      # опционально
#             "needs_mux": bool,           # опционально, сбрасывается при смене payload
#             "last_checked_url": str,     # опционально
#             "last_checked_resolver": str,
#         },
#         ...
#     ]
# }
# Инвариант: streams_cache[idx] <-> ch["streams"][idx].
# Пустой слот — {}.
_epg_cache = {}
# Активный стрим — свойство конфига, а не кэша. Здесь хранится
# денормализованная карта {name: idx} только для быстрого чтения:
# get_channel_stream дёргается на каждый сегмент, а load_channels()
# делает deepcopy всех каналов — слишком дорого туда ходить.
# Источник истины — config.json. Карта синхронизируется при старте
# (_realign), при reload_config и при любом изменении active_idx.
_active_index_map = {}
_epg_channels = {}
_channels_memory = []
_epg_lock = threading.Lock()           # для build_filtered_epg
_epg_building = False

_failed_resolve_cache = {}
_last_active = {}   # {channel_name: timestamp последнего запроса от клиента}

_sse_clients = []
_sse_lock = threading.Lock()
_loop = None

_healthcheck_tasks = {}
_healthcheck_lock = threading.Lock()

# channel-events-state-v1
# Предыдущее состояние здоровья канала (для логов UP -> DOWN / DOWN -> UP).
# {"up", "down"} — из healthcheck. Пишется в channel_events.log при переходах.
_channel_health_state = {}


def invalidate_channels_cache():
    global _channels_memory
    with channels_lock:
        _channels_memory = []


def cleanup_expired_caches():
    now = time.time()
    with cache_lock:
        expired_failures = [k for k, v in _failed_resolve_cache.items() if v[1] < now]
        for k in expired_failures:
            _failed_resolve_cache.pop(k, None)


def pop_failed_resolve_for_channel(name: str) -> int:
    """Удаляет все записи _failed_resolve_cache для канала.

    Ключ в _failed_resolve_cache — tuple (name, stream_idx). Прямой
    `_failed_resolve_cache.pop(name)` не сработает (тихо, без ошибки):
    имя канала там лишь первый элемент ключа. Раньше все места делали
    это руками — итерацией с фильтром по tuple[0]. Если кто-то в
    будущем напишет `.pop(name)` по аналогии с _epg_cache — получит
    молчаливый no-op. Helper фиксирует единственный правильный способ.

    Возвращает число удалённых записей (для логов/тестов).
    """
    with cache_lock:
        removed = 0
        for key in [k for k in _failed_resolve_cache
                    if isinstance(k, tuple) and k and k[0] == name]:
            _failed_resolve_cache.pop(key, None)
            removed += 1
        return removed


# channel-events-state-marks-v1
def _log_health_transition(name: str, new_state: str, source: str) -> None:
    """Пишет UP -> DOWN / DOWN -> UP в channel_events.log при переходах.
    Вызывается из mark_channel_healthy/unhealthy. Не пишет, если
    состояние не изменилось."""
    prev = _channel_health_state.get(name)
    if prev == new_state:
        return
    try:
        from services.channel_events import log_state
        if new_state == "down" and prev != "down":
            log_state(name, "UP -> DOWN", source)
        elif new_state == "up" and prev == "down":
            log_state(name, "DOWN -> UP", source)
    except Exception as e:
        logger.warning(f"[EVENTS] '{name}': health transition log failed: {e}")
    _channel_health_state[name] = new_state


# mediainfo-invalidate-v1
def invalidate_jellyfin_mediainfo(name: str, reason: str = "") -> int:
    """Удаляет mediainfo-кэш Jellyfin для канала {name}.

    Jellyfin при первом probe канала сохраняет в cache/mediainfo/*.json
    поле Container (hls|ts). При последующих открытиях не перепроверяет,
    использует закэшированный -f. Если канал сменил режим доставки
    (HLS ↔ raw TS через мукс), старый файл ломает воспроизведение
    (ffmpeg exit 183 - Invalid data found).

    Удаляем файлы, у которых Path начинается с
    "{IPTV_MANAGE_URL}/redirect/{name}". Это покроет все варианты
    расширений (.m3u8, .ts, без расширения) и старые артефакты.
    Медиатека (фильмы, музыка, фото) имеет другие Path — не задеваем.

    Папка монтируется в контейнер как /jellyfin-mediainfo-cache.
    Если папки нет — тихо выходим.

    Возвращает число удалённых файлов.
    """
    if not name:
        return 0
    import os as _os
    import glob as _glob
    import json as _json

    folder = IPTV_JELLYFIN_MEDIAINFO_DIR
    if not folder or not _os.path.isdir(folder):
        logger.debug(f"[MEDIAINFO] '{name}': folder not available ({folder}), skip")
        return 0

    prefix = f"{IPTV_MANAGE_URL}/redirect/{name}"
    removed = 0
    errors = 0
    for path in _glob.glob(_os.path.join(folder, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            continue
        try:
            p = data.get("Path") or ""
        except Exception:
            continue
        if not isinstance(p, str) or not p.startswith(prefix):
            continue
        try:
            _os.remove(path)
            removed += 1
            logger.info(f"[MEDIAINFO] '{name}': removed {_os.path.basename(path)} (Path={p})")
        except Exception as e:
            errors += 1
            logger.warning(f"[MEDIAINFO] '{name}': failed to remove {_os.path.basename(path)}: {e}")

    if removed or errors:
        logger.info(
            f"[MEDIAINFO] '{name}': invalidated {removed} file(s), errors {errors} "
            f"(reason: {reason or 'unknown'})"
        )
    return removed


def get_max_chno(channels):
    chnos = []
    for ch in channels:
        try:
            val = int(ch.get("chno") or 0)
            chnos.append(val)
        except (ValueError, TypeError):
            pass
    return max(chnos) if chnos else 0


def sort_channels_by_chno(channels):
    def parse_chno(ch):
        try:
            return int(ch.get("chno") or 0)
        except (ValueError, TypeError):
            return 0
    return sorted(channels, key=parse_chno)


def get_active_index(name: str) -> int:
    """Индекс активного стрима канала. Источник истины — config.json;
    в _active_index_map лежит денормализованная копия для скорости."""
    with cache_lock:
        v = _active_index_map.get(name, 0)
        if isinstance(v, int) and v >= 0:
            return v
        return 0


def set_active_index(name: str, idx: int) -> None:
    """Обновляет карту активного стрима. В config.json пишет вызывающий
    (set_active_stream_index или update_stream_settings)."""
    if not isinstance(idx, int) or idx < 0:
        idx = 0
    with cache_lock:
        _active_index_map[name] = idx


def assign_stream_ids(streams: list) -> None:
    """Назначает каждому стриму уникальный stream_id на месте.

    stream_id — identity стрима. Используется как ключ при матчинге
    streams_cache: слот стрима следует за стримом при переупорядочивании,
    удалении соседей, смене resolver и т.п. Если у стрима stream_id
    отсутствует или дублируется — генерируем свежий.

    Значение — 52-битное случайное (secrets.randbits(52)), влезает в
    Number.MAX_SAFE_INTEGER, поэтому JS-фронт парсит без потерь.
    Мутирует список in-place.
    """
    if not isinstance(streams, list):
        return

    seen = set()
    needs_new = [False] * len(streams)
    for i, s in enumerate(streams):
        if not isinstance(s, dict):
            continue
        sid = s.get("stream_id")
        if isinstance(sid, int) and sid > 0 and sid not in seen:
            seen.add(sid)
        else:
            needs_new[i] = True

    for i, s in enumerate(streams):
        if not isinstance(s, dict):
            continue
        if not needs_new[i]:
            continue
        sid = secrets.randbits(52)
        while sid == 0 or sid in seen:
            sid = secrets.randbits(52)
        s["stream_id"] = sid
        seen.add(sid)


def load_channels():
    global _channels_memory
    with channels_lock:
        if _channels_memory:
            return copy.deepcopy(_channels_memory)

        if _cfg.CONFIG_DATA and isinstance(_cfg.CONFIG_DATA.get("channels"), list) and len(_cfg.CONFIG_DATA["channels"]) > 0:
            channels_raw = _cfg.CONFIG_DATA["channels"]
            channels = []
            for ch_cfg in channels_raw:
                name = ch_cfg.get("name", "").strip()
                if not name:
                    continue
                chno = str(ch_cfg.get("chno", "")).strip()
                group = ch_cfg.get("group", "")
                tvgid = re.sub(r'\s*\([^)]*\)\s*$', '', ch_cfg.get("tvgid", "")).strip()
                logo = ch_cfg.get("logo", "")
                disable = ch_cfg.get("disable", False)

                streams = ch_cfg.get("streams", [])
                if not isinstance(streams, list) or len(streams) == 0:
                    continue

                # stream_id — identity стрима. Нормализуем на входе:
                # старые конфиги без него получат свежие id, дубликаты
                # (например после ручного копирования в config.json)
                # будут разведены. Сохранение — через save_full_config.
                assign_stream_ids(streams)

                active_index = ch_cfg.get("active_stream_index", 0)
                if not isinstance(active_index, int) or active_index < 0 or active_index >= len(streams):
                    active_index = 0
                    for i, s in enumerate(streams):
                        if not s.get("disable", False):
                            active_index = i
                            break

                if streams[active_index].get("disable", False):
                    for i, s in enumerate(streams):
                        if not s.get("disable", False):
                            active_index = i
                            break

                first = streams[active_index]

                # mux-state-v1: mux_state на активном stream (auto|on|off).
                # Берём из first, но при переключении active_index в
                # load_channels пересчитывается first — значит всегда
                # актуально для выбранного stream.
                _mux_state = first.get("mux_state", "auto")
                if _mux_state not in ("auto", "on", "off"):
                    _mux_state = "auto"

                channels.append({
                    "name": name,
                    "chno": chno,
                    "group": group,
                    "logo": logo,
                    "tvgid": tvgid,
                    "real_name": ch_cfg.get("real_name", name),
                    "disable": disable,
                    "url": first.get("url", ""),
                    "resolver": first.get("resolver", "auto"),
                    "ua": first.get("ua", IPTV_DEFAULT_UA),
                    "fs_regex": first.get("fs_regex", ""),
                    "streams": streams,
                    "active_stream_index": active_index,
                    "fallback": ch_cfg.get("fallback", False),
                    "prefetch": first.get("prefetch", False),
                    "comment": ch_cfg.get("comment", ""),
                    "mux_state": _mux_state,
                })
            _channels_memory = channels
            logger.info(f"[STATE] loaded {len(channels)} channels from config.json")
            return copy.deepcopy(channels)

        _channels_memory = []
        return []

def get_channel(name: str):
    """Возвращает deepcopy одного канала или None.

    В отличие от load_channels() не копирует весь список — только
    запрошенный элемент. Для горячих путей (поиск одного канала
    на каждый GET /redirect/{name}.m3u8)."""
    with channels_lock:
        if not _channels_memory:
            load_channels()
        for ch in _channels_memory:
            if ch["name"] == name:
                return copy.deepcopy(ch)
    return None


def save_channels_to_file(channels):
    with channels_lock:
        save_full_config(channels)

        try:
            lines = ["# --- IPTV-PROXY CHANNELS CONFIG (legacy backup) ---"]
            for ch in channels:
                active_index = ch.get("active_stream_index", 0)
                streams = ch.get("streams", [])
                if streams:
                    if active_index >= len(streams):
                        active_index = 0
                    first = streams[active_index]
                    url = first.get("url", ch.get("url", ""))
                    resolver = first.get("resolver", ch.get("resolver", "auto"))
                    ua = first.get("ua", ch.get("ua", IPTV_DEFAULT_UA))
                    fs_regex = first.get("fs_regex", ch.get("fs_regex", ""))
                else:
                    url = ch.get("url", "")
                    resolver = ch.get("resolver", "auto")
                    ua = ch.get("ua", IPTV_DEFAULT_UA)
                    fs_regex = ch.get("fs_regex", "")
                lines.append(
                    f"{ch.get('name','')}|{url}|{ch.get('chno','')}|{ch.get('group','')}|"
                    f"{ch.get('logo','')}|{ch.get('tvgid','')}|{ua}|{fs_regex}|{resolver}"
                )
            with open(IPTV_BACKUP_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            invalidate_channels_cache()
            logger.info(f"[STATE] channels backup saved to {IPTV_BACKUP_FILE}")
        except Exception as e:
            logger.error(f"[STATE] backup save failed: {e}")


def _realign_streams_cache_with_config():
    """Выравнивает streams_cache по порядку config-стримов, матча по stream_id.

    После загрузки из cache.json (или после ручной правки config.json) массив
    слотов может не совпадать с порядком стримов в config. Строим новый массив:
    для каждого config-стрима берём слот с тем же stream_id. Не нашли — пустой.
    Слоты, чей stream_id отсутствует в config, отбрасываются.

    active_stream_index переносим в _active_index_map: config — единственный
    источник истины, в _epg_cache этого поля больше нет.
    """
    try:
        channels = load_channels()
    except Exception as e:
        logger.warning(f"[CACHE] realign: load_channels failed: {e}")
        return
    with cache_lock:
        for ch in channels:
            name = ch.get("name")
            cfg_idx = ch.get("active_stream_index", 0)
            if not isinstance(cfg_idx, int) or cfg_idx < 0:
                cfg_idx = 0
            _active_index_map[name] = cfg_idx

            entry = _epg_cache.get(name)
            if not isinstance(entry, dict):
                continue
            old_slots = entry.get("streams_cache", [])
            if not isinstance(old_slots, list):
                old_slots = []
            by_id = {}
            for s in old_slots:
                if not isinstance(s, dict):
                    continue
                sid = s.get("stream_id")
                if isinstance(sid, int) and sid > 0 and sid not in by_id:
                    by_id[sid] = s
            new_slots = []
            for cfg_s in ch.get("streams", []):
                sid = cfg_s.get("stream_id") if isinstance(cfg_s, dict) else None
                if isinstance(sid, int) and sid > 0 and sid in by_id:
                    new_slots.append(by_id[sid])
                else:
                    new_slots.append({})
            entry["streams_cache"] = new_slots


def load_cache():
    """Читает cache.json в _epg_cache. Flat-поля старого формата игнорируются.

    После чтения вызывается _realign_streams_cache_with_config: массив слотов
    выстраивается по порядку config-стримов, соответствие — по stream_id.
    """
    global _epg_cache
    with cache_lock:
        if not os.path.exists(IPTV_CACHE_FILE):
            _epg_cache = {}
        else:
            try:
                with open(IPTV_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("channels", {})
                _epg_cache = {}
                for name, entry in raw.items():
                    if not isinstance(entry, dict):
                        continue
                    streams_cache = entry.get("streams_cache")
                    if not isinstance(streams_cache, list):
                        streams_cache = []
                    streams_cache = [s if isinstance(s, dict) else {} for s in streams_cache]
                    # active_stream_index в файле — артефакт старой схемы,
                    # игнорируем. Реальный idx придёт из config через _realign.
                    _epg_cache[name] = {
                        "streams_cache": streams_cache,
                    }
            except Exception as e:
                logger.error(f"[STATE] {IPTV_CACHE_FILE}: read failed: {e}")
                _epg_cache = {}

    # Выравнивание всегда — и когда файла нет, и когда прочитали.
    _realign_streams_cache_with_config()


def save_cache():
    """Пишет cache.json. В каждый слот подставляется stream_id из config
    (по позиции), чтобы при следующей загрузке можно было выровнять слоты
    даже если порядок стримов в config.json изменится.

    ВАЖНО: снимок config берётся ДО захвата cache_lock. Если бы load_channels
    вызывался внутри cache_lock, то save_cache (cache → channels) и
    update_stream_settings (channels → cache) могли бы встать насмерть.
    """
    # Фаза 1: снимок config-стримов, без cache_lock.
    #
    # TOCTOU (допустим): между фазой 1 и фазой 2 UI может переставить
    # стримы в config.json. Тогда cfg_ids не совпадут с реальным порядком,
    # и в файл уедут неверные stream_id. При следующем load_cache _realign
    # выровняет слоты по этим (ошибочным) id, что приведёт к съезду.
    #
    # Риск принят: UI-правки структуры стримов (перестановка, добавление,
    # удаление) редки, а защита через channels_lock на всё время save_cache
    # заблокирует UI-операции на время дискового I/O (50-200 мс). При
    # следующем открытии модалки _realign всё равно всё выровняет.
    try:
        cfg_map = {}
        for ch in load_channels():
            cfg_map[ch["name"]] = [
                (s.get("stream_id") if isinstance(s, dict) else None)
                for s in ch.get("streams", [])
            ]
    except Exception as e:
        logger.warning(f"[CACHE] save: load_channels failed: {e}")
        cfg_map = {}

    # Фаза 2: под cache_lock — только работа с _epg_cache.
    with cache_lock:
        data_to_save = {}
        for name, entry in _epg_cache.items():
            if not isinstance(entry, dict):
                continue
            streams = entry.get("streams_cache", [])
            if not isinstance(streams, list):
                streams = []

            cfg_ids = cfg_map.get(name, [])
            out_slots = []
            for i, slot in enumerate(streams):
                slot_copy = dict(slot) if isinstance(slot, dict) else {}
                sid = cfg_ids[i] if i < len(cfg_ids) else None
                if isinstance(sid, int) and sid > 0:
                    slot_copy["stream_id"] = sid
                else:
                    slot_copy.pop("stream_id", None)
                out_slots.append(slot_copy)

            # active_stream_index в файл не пишем: он теперь живёт
            # только в config.json, а в _active_index_map — его runtime-копия.
            data_to_save[name] = {
                "streams_cache": out_slots,
            }

    with save_lock:
        tmp = IPTV_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"channels": data_to_save}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, IPTV_CACHE_FILE)


def peek_channel_stream(name: str):
    """Read-only peek at the active slot without triggering a resolve.

    Возвращает (is_direct, payload, expire_time, is_stale) или None.
    None — payload'а нет вообще (настоящий cache miss, нужен блокирующий
    resolve). is_stale=True — payload есть, но cache_expire <= now.
    """
    active_idx = get_active_index(name)
    with cache_lock:
        entry = _epg_cache.get(name, {})
        if not isinstance(entry, dict):
            return None
        streams = entry.get("streams_cache", [])
        if not isinstance(streams, list):
            return None
        if not (isinstance(active_idx, int) and 0 <= active_idx < len(streams)):
            return None
        s = streams[active_idx]
        if not isinstance(s, dict):
            return None
        payload = s.get("cached_stream")
        if not payload:
            return None
        expire = s.get("cache_expire", 0)
        return (s.get("cache_is_direct", True), payload, expire, expire <= time.time())


def get_stream_cache(name: str, index: int):
    """Возвращает (is_direct, payload, expire_time) для слота или None."""
    with cache_lock:
        entry = _epg_cache.get(name, {})
        streams = entry.get("streams_cache", [])
        if 0 <= index < len(streams):
            s = streams[index]
            if not isinstance(s, dict):
                return None
            payload = s.get("cached_stream")
            if payload and s.get("cache_expire", 0) > time.time():
                return (s.get("cache_is_direct", True), payload, s.get("cache_expire", 0))
        return None


def get_active_stream_state(name: str) -> dict:
    """Возвращает копию streams_cache[active_idx] или {} — единая точка чтения
    per-channel рантайм-статуса (для UI/SSE/healthcheck)."""
    idx = get_active_index(name)
    with cache_lock:
        entry = _epg_cache.get(name, {})
        if not isinstance(entry, dict):
            return {}
        streams = entry.get("streams_cache", [])
        if isinstance(idx, int) and 0 <= idx < len(streams):
            s = streams[idx]
            if isinstance(s, dict):
                return dict(s)
    return {}


def set_stream_cache(name: str, index: int, payload: str, is_direct: bool, expire_time: float,
                     method: str, probe_elapsed: float = None):
    """Пишет payload в streams_cache[index]. При смене payload сбрасывает needs_mux."""
    with cache_lock:
        if name not in _epg_cache or not isinstance(_epg_cache[name], dict):
            _epg_cache[name] = {"streams_cache": []}
        entry = _epg_cache[name]
        if "streams_cache" not in entry or not isinstance(entry["streams_cache"], list):
            entry["streams_cache"] = []
        streams = entry["streams_cache"]
        while len(streams) <= index:
            streams.append({})
        s = streams[index]
        if not isinstance(s, dict):
            s = {}
            streams[index] = s

        old_payload = s.get("cached_stream")
        if old_payload is not None and old_payload != payload:
            # payload сменился — needs_mux, вычисленный для старого, невалиден
            s.pop("needs_mux", None)

        now = time.time()
        s["cached_stream"] = payload
        s["cache_is_direct"] = is_direct
        s["cache_expire"] = expire_time
        s["last_check_time"] = now
        s["last_check_success"] = True
        s["last_check_detail"] = f"Resolved via {method}"
        if probe_elapsed is not None:
            s["probe_elapsed"] = probe_elapsed


def clear_stream_cache(name: str, index: int):
    """Обнуляет слот streams_cache[index]. Индекс сохраняется, соседи не сдвигаются."""
    with cache_lock:
        entry = _epg_cache.get(name, {})
        if not isinstance(entry, dict):
            return
        streams = entry.get("streams_cache", [])
        if 0 <= index < len(streams):
            streams[index] = {}


def clear_channel_stream_cache(name: str):
    """Полная очистка рантайм-состояния канала: все слоты в {}, чистка _failed_resolve."""
    with cache_lock:
        pop_failed_resolve_for_channel(name)

        entry = _epg_cache.get(name)
        if isinstance(entry, dict):
            streams = entry.get("streams_cache", [])
            if isinstance(streams, list):
                for i in range(len(streams)):
                    streams[i] = {}


def get_channel_stream(name: str):
    """Возвращает (is_direct, payload, expire_time, method).

    При cache-hit — из streams_cache[active_idx], method='cache'.
    При miss — резолв через resolver, запись в активный слот через set_stream_cache.
    """
    active_idx = get_active_index(name)
    with cache_lock:
        now = time.time()
        entry = _epg_cache.get(name, {})
        if not isinstance(entry, dict):
            entry = {}
        streams = entry.get("streams_cache", [])
        if isinstance(active_idx, int) and 0 <= active_idx < len(streams):
            s = streams[active_idx]
            if isinstance(s, dict):
                payload = s.get("cached_stream")
                expire_time = s.get("cache_expire", 0)
                if payload and expire_time > now:
                    return s.get("cache_is_direct", True), payload, expire_time, "cache"

    from services.resolver import resolve_channel_payload
    ch = next((c for c in load_channels() if c["name"] == name), None)
    if not ch:
        raise ValueError(f"Канал {name} не найден")

    # На случай, если карта ещё не подтянулась — сверяемся с config.
    active_idx = get_active_index(name)
    is_direct, payload, expire_time, method = resolve_channel_payload(ch)

    # Перечитываем индекс: за время resolve (sniffer = Chromium,
    # flaresolverr = до 70с) пользователь через UI или fallback-switch
    # мог сменить активный стрим. active_idx теперь указывает на
    # стрим, для которого payload НЕ резолвился — писать в его слот
    # нельзя, /redirect прочитает чужой URL.
    #
    # Payload всё равно возвращаем: вызывающий (/redirect) отдаст его
    # клиенту для текущего запроса. Следующий get_channel_stream пойдёт
    # резолвить заново — уже под новый активный стрим.
    current_idx = get_active_index(name)
    if current_idx != active_idx:
        logger.info(
            f"[CACHE] '{name}': active_idx сменился {active_idx}→{current_idx} "
            f"за время resolve ({method}), слот не трогаем"
        )
        return is_direct, payload, expire_time, method

    with cache_lock:
        set_stream_cache(name, active_idx, payload, is_direct, expire_time, method)

    return is_direct, payload, expire_time, method


def set_active_stream_index(name: str, new_index: int, source: str = "unknown"):
    # Смена активного стрима = старый мукс читает мёртвый источник.
    # Убиваем мукс ПЕРВЫМ, до обновления active_index и до снятия
    # channels_lock.
    #
    # Почему такой порядок:
    # 1. invalidate_mux() убивает ffmpeg-процесс (proc.kill + wait до 5 сек).
    #    Это не мгновенно. Если сделать это ПОСЛЕ снятия channels_lock,
    #    окно гонки: active_index уже новый, а старый мукс ещё жив и
    #    читает мёртвый источник. Следующий GET /mux/{name}.ts может
    #    подключиться к нему и получить мусор.
    # 2. Если сделать это ПОД channels_lock — блокируем все правки конфига
    #    на время kill. 5 сек — терпимо, и происходит это только при
    #    смене стрима (редко). Лучше подержать лок, чем ловить гонку.
    #
    # Порядок: kill mux -> обновить config под channels_lock ->
    # обновить cache_lock карту -> save_cache.
    try:
        invalidate_mux(name)
    except Exception as e:
        logger.warning(f"[MUX] '{name}': mux invalidation failed: {e}")

    with channels_lock:
        channels = load_channels()
        for ch in channels:
            if ch["name"] == name:
                ch["active_stream_index"] = new_index
                streams = ch.get("streams", [])
                if new_index < len(streams):
                    first = streams[new_index]
                    ch["url"] = first.get("url", "")
                    ch["resolver"] = first.get("resolver", "auto")
                    ch["ua"] = first.get("ua", IPTV_DEFAULT_UA)
                    ch["fs_regex"] = first.get("fs_regex", "")
                    # mux-state-v1: обновить mux_state активного stream.
                    _ms = first.get("mux_state", "auto")
                    ch["mux_state"] = _ms if _ms in ("auto", "on", "off") else "auto"
                invalidate_channels_cache()
                save_channels_to_file(channels)
                break

    with cache_lock:
        pop_failed_resolve_for_channel(name)
        _active_index_map[name] = new_index

    save_cache()

    # channel-events-state-v1: при автоматическом переключении (healthcheck,
    # fallback) пишем событие. При source="ui" — не пишем, роутер сам
    # залогирует cfg: active_stream: old -> new.
    if source not in ("ui", "unknown"):
        try:
            from services.channel_events import log_state
            log_state(name, f"active_stream switched: {new_index}", source)
        except Exception as _e:
            logger.warning(f"[EVENTS] '{name}': switch event log failed: {_e}")

    # mediainfo-invalidate-v1: смена активного стрима всегда потенциально
    # меняет режим доставки (HLS ↔ raw TS). Удаляем mediainfo-кэш Jellyfin,
    # чтобы при следующем открытии канала он сделал свежий probe.
    try:
        invalidate_jellyfin_mediainfo(name, reason="set_active_stream_index")
    except Exception as _e:
        logger.warning(f"[MEDIAINFO] '{name}': invalidate from set_active_stream_index failed: {_e}")


def update_probe_elapsed_in_cache(name: str, index: int, probe_elapsed: float):
    with cache_lock:
        entry = _epg_cache.get(name, {})
        if not isinstance(entry, dict):
            return
        streams = entry.setdefault("streams_cache", [])
        while len(streams) <= index:
            streams.append({})
        if not isinstance(streams[index], dict):
            streams[index] = {}
        streams[index]["probe_elapsed"] = probe_elapsed
    save_cache()


def mark_channel_healthy(name: str, method: str = "probe", source: str = "ui"):
    """Отмечает успешную проверку активного слота канала + чистит _failed_resolve_cache."""
    _log_health_transition(name, "up", source)
    idx = get_active_index(name)
    with cache_lock:
        _last_active[name] = time.time()
        if name not in _epg_cache or not isinstance(_epg_cache[name], dict):
            _epg_cache[name] = {"streams_cache": []}
        entry = _epg_cache[name]
        streams = entry.setdefault("streams_cache", [])
        while len(streams) <= idx:
            streams.append({})
        if not isinstance(streams[idx], dict):
            streams[idx] = {}
        s = streams[idx]
        now = time.time()
        s["last_check_time"] = now
        s["last_check_success"] = True
        s["last_check_detail"] = f"Resolved via {method}"

        pop_failed_resolve_for_channel(name)

def mark_channel_unhealthy(name: str, detail: str = "Probe failed", source: str = "ui"):
    """Симметрично mark_channel_healthy: пишет failure в активный слот.
    Не чистит _failed_resolve_cache (мы не знаем, был ли резолв неудачным)
    и не трогает _last_active. Задача — отразить результат probe в UI."""
    _log_health_transition(name, "down", source)
    idx = get_active_index(name)
    with cache_lock:
        if name not in _epg_cache or not isinstance(_epg_cache[name], dict):
            _epg_cache[name] = {"streams_cache": []}
        entry = _epg_cache[name]
        streams = entry.setdefault("streams_cache", [])
        while len(streams) <= idx:
            streams.append({})
        if not isinstance(streams[idx], dict):
            streams[idx] = {}
        s = streams[idx]
        s["last_check_time"] = time.time()
        s["last_check_success"] = False
        s["last_check_detail"] = detail

def mark_channel_active(name: str):
    """Отмечает, что клиент недавно запросил канал.
    Планировщик healthcheck пропускает такие каналы: если Jellyfin
    тянет сегменты, поток заведомо жив, а смерть посреди просмотра
    ловится через Stop-webhook, который форсирует проверку."""
    with cache_lock:
        _last_active[name] = time.time()
