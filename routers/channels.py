"""
routers/channels.py — CRUD каналов и их потоков.

Рефакторинг channels-refactor-v1:
  - проверки потоков вынесены в routers/channels_checks.py
  - массовая работа с резолверами вынесена в routers/channels_resolvers.py
Здесь остаётся всё, что меняет config.json:
  /channels/add
  /channels/update-stream
  /channels/delete
  /channels/clear-cache
  /channels/toggle
  /channels/toggle-fallback
  /channels/get
  + внутренние хелперы _apply_pre_resolved_cache и _splice_streams_cache.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import re
import time

import core.state as state
from core.config import IPTV_DEFAULT_UA, logger
from services.resolver import is_valid_resolver
from services.epg_service import epg_manager
from services.channel_events import log_cfg, log_state

router = APIRouter()

def _apply_pre_resolved_cache(name: str, streams: list):
    """Применяет cached_stream/cache_expire/probe_elapsed, пришедшие из UI.

    UI мог проверить поток до сохранения канала (поток ещё не в config.json,
    или это вообще новый канал). Результат лежит в streams[i].cached_stream.
    Когда канал сохраняется — нужно записать этот payload в _epg_cache,
    чтобы не терять результат «Проверить поток»/«Проверить ffprobe».

    Вызывается ПОСЛЕ сохранения config.json и (для update-stream) после
    _splice_streams_cache. Индексы в streams соответствуют слотам в _epg_cache.

    Поля, которые могут прийти:
      cached_stream  — payload (URL или URL|Referer=...|User-Agent=...)
      cache_expire   — unixtime (не приходит от текущего UI, бэк пересчитает)
      probe_elapsed  — секунды ffprobe

    Если URL сменился относительно того, что лежит в слоте — не пишем.
    Если cached_stream пустой — пропускаем.
    """
    if not isinstance(streams, list):
        return
    for i, s in enumerate(streams):
        if not isinstance(s, dict):
            continue
        cached = s.get("cached_stream")
        if not cached:
            continue
        # Проверяем, что слот ещё пуст или указывает на тот же URL.
        with state.cache_lock:
            entry = state._epg_cache.get(name, {})
            if not isinstance(entry, dict):
                entry = {"streams_cache": []}
                state._epg_cache[name] = entry
            slots = entry.setdefault("streams_cache", [])
            while len(slots) <= i:
                slots.append({})
            if not isinstance(slots[i], dict):
                slots[i] = {}
            old_payload = slots[i].get("cached_stream")
        # Если в слоте уже что-то есть — не перезаписываем (свежий резолв
        # важнее, чем результат проверки, сделанный N минут назад в UI).
        if old_payload:
            continue

        # Пересчитываем expire на бэке. cached_stream может быть с |Referer=,
        # _compute_cache_expire это разбирает через parse_url_headers.
        from services.resolver import _compute_cache_expire
        # Метод для расчёта TTL не знаем точно — берём "auto", это даст
        # дефолтный TTL по URL. Если в payload есть явный expire — он
        # победит.
        try:
            expire_time = _compute_cache_expire(cached, "auto")
        except Exception:
            expire_time = time.time() + 600
        is_direct = not cached.startswith("#EXTM3U")
        probe_elapsed = s.get("probe_elapsed")
        try:
            state.set_stream_cache(
                name, i, cached, is_direct, expire_time, "ui",
                probe_elapsed=probe_elapsed,
            )
            logger.info(f"[CHANNELS] '{name}': applied pre-resolved cache for stream {i}")
        except Exception as e:
            logger.warning(f"[CHANNELS] '{name}': failed to apply cached_stream[{i}]: {e}")


# Замечание по блокировкам:
# Везде, где происходит «прочитать каналы → изменить в памяти → сохранить»,
# мы держим state.channels_lock через всю операцию. Без этого параллельный
# вызов (UI + healthcheck + scheduler) мог перетереть изменения
# по принципу last-write-wins. state.channels_lock — RLock, поэтому
# load_channels() и save_channels_to_file() внутри критической секции
# срабатывают рекурсивно, без самоблокировки.


def _splice_streams_cache(entry: dict, old_streams: list, new_streams: list):
    """Пересобирает streams_cache под новый набор стримов.

    Матчинг по stream_id — единственному стабильному identity стрима.
    Слот следует за стримом при переупорядочивании, удалении соседей,
    смене URL, смене resolver и при двух одинаковых URL с разными
    resolver'ами. Удалённый стрим уносит слот с собой, добавленный
    приходит с пустым.
    """
    old_cache = entry.get("streams_cache", [])
    if not isinstance(old_cache, list):
        old_cache = []

    old_by_id = {}
    for i, old_s in enumerate(old_streams):
        if not isinstance(old_s, dict):
            continue
        sid = old_s.get("stream_id")
        if isinstance(sid, int) and sid > 0 and sid not in old_by_id:
            old_by_id[sid] = i

    new_cache = []
    for new_s in new_streams:
        slot = {}
        if isinstance(new_s, dict):
            sid = new_s.get("stream_id")
            if isinstance(sid, int) and sid > 0:
                i = old_by_id.get(sid)
                if i is not None and i < len(old_cache) and isinstance(old_cache[i], dict):
                    slot = old_cache[i]
        new_cache.append(slot)
    entry["streams_cache"] = new_cache


@router.get("/channels/get")
def get_channel_info(name: str):
    ch = next((c for c in state.load_channels() if c["name"] == name), None)
    if not ch:
        return JSONResponse({"success": False, "error": "Канал не найден"}, status_code=404)
    data = dict(ch)
    active_idx = ch.get("active_stream_index", 0)
    if not isinstance(active_idx, int) or active_idx < 0:
        active_idx = 0

    with state.cache_lock:
        cache_entry = state._epg_cache.get(name, {})
        if not isinstance(cache_entry, dict):
            cache_entry = {}
        streams_cache = cache_entry.get("streams_cache", [])
        if not isinstance(streams_cache, list):
            streams_cache = []
        active_state = streams_cache[active_idx] if 0 <= active_idx < len(streams_cache) and isinstance(streams_cache[active_idx], dict) else {}

    data["resolver"] = ch.get("resolver", "auto")
    data["real_name"] = ch.get("real_name", name)
    data["tvg_id"] = ch.get("tvgid", "")
    source_name_map = epg_manager.get_channel_source_name_map()
    data["source_name"] = source_name_map.get(data["tvg_id"], "")
    data["disable"] = ch.get("disable", False)

    # Статус канала = состояние активного слота
    data["last_check_time"] = active_state.get("last_check_time")
    data["last_check_success"] = active_state.get("last_check_success")
    data["last_check_detail"] = active_state.get("last_check_detail", "")
    data["cached_stream"] = active_state.get("cached_stream")
    data["streams"] = ch.get("streams", [])
    data["active_stream_index"] = active_idx

    data["streams_cache"] = []
    for idx, _ in enumerate(ch.get("streams", [])):
        if idx < len(streams_cache) and isinstance(streams_cache[idx], dict):
            data["streams_cache"].append(streams_cache[idx])
        else:
            data["streams_cache"].append({})
    return JSONResponse({"success": True, "data": data})

@router.post("/channels/toggle")
async def toggle_channel(request: Request):
    try:
        data = await request.json()
        name = data.get("name")
        enabled = data.get("enabled")
        if name is None or enabled is None:
            return JSONResponse({"success": False, "error": "Не хватает параметров"})

        with state.channels_lock:
            channels = state.load_channels()
            found = False
            for ch in channels:
                if ch["name"] == name:
                    ch["disable"] = not bool(enabled)
                    found = True
                    break
            if not found:
                return JSONResponse({"success": False, "error": "Канал не найден"}, status_code=404)

            sorted_channels = state.sort_channels_by_chno(channels)
            state.save_channels_to_file(sorted_channels)

        logger.info(f"[TOGGLE] '{name}': {'enabled' if enabled else 'disabled'}")
        try:
            log_state(name, "enabled" if enabled else "disabled", "ui")
        except Exception:
            pass
        return JSONResponse({"success": True})

    except Exception as e:
        logger.error(f"[TOGGLE] error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

@router.post("/channels/toggle-fallback")
async def toggle_fallback(request: Request):
    try:
        data = await request.json()
        name = data.get("name")
        enabled = data.get("enabled")
        if name is None or enabled is None:
            return JSONResponse({"success": False, "error": "Не хватает параметров"})

        with state.channels_lock:
            channels = state.load_channels()
            found = False
            for ch in channels:
                if ch["name"] == name:
                    ch["fallback"] = bool(enabled)
                    found = True
                    break

            if not found:
                return JSONResponse({"success": False, "error": "Канал не найден"}, status_code=404)

            sorted_channels = state.sort_channels_by_chno(channels)
            state.save_channels_to_file(sorted_channels)
        logger.info(f"[FALLBACK] '{name}': fallback {'enabled' if enabled else 'disabled'}")
        try:
            log_cfg(name, f"fallback: {not enabled} -> {enabled}", "ui")
        except Exception:
            pass
        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"[FALLBACK] toggle error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

@router.post("/channels/delete")
async def delete_channel(request: Request):
    try:
        data = await request.json()
        name_to_delete = data.get("name")

        with state.channels_lock:
            channels = state.load_channels()
            new_channels = [ch for ch in channels if ch["name"] != name_to_delete]
            if len(new_channels) == len(channels):
                return JSONResponse({"success": False, "error": "Канал не найден"})

            sorted_channels = state.sort_channels_by_chno(new_channels)
            state.save_channels_to_file(sorted_channels)

        with state.cache_lock:
            state._epg_cache.pop(name_to_delete, None)
            state.pop_failed_resolve_for_channel(name_to_delete)

        state.save_cache()
        logger.info(f"[CHANNELS] '{name_to_delete}': deleted")
        try:
            log_cfg(name_to_delete, "deleted", "ui")
        except Exception:
            pass
        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"[CHANNELS] delete error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

@router.post("/channels/clear-cache")
async def clear_channel_cache(request: Request):
    try:
        data = await request.json()
        name = data.get("name")
        stream_index = data.get("stream_index", None)
        if not name:
            return JSONResponse({"success": False, "error": "Имя канала не указано"})
        if stream_index is not None:
            state.clear_stream_cache(name, int(stream_index))
            logger.info(f"[CACHE] '{name}': stream {stream_index} cache cleared manually")
        else:
            state.clear_channel_stream_cache(name)
            logger.info(f"[CACHE] '{name}': cache cleared manually")
        state.save_cache()
        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"[CACHE] clear error: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/channels/update-stream")
async def update_stream_settings(request: Request):
    try:
        data = await request.json()
        orig_name = data.get("original_name")
        new_name = data.get("name")
        streams = data.get("streams", [])
        active_index = data.get("active_stream_index", 0)

        if not new_name or not streams:
            return JSONResponse({"success": False, "error": "Название и потоки обязательны"})

        if not isinstance(active_index, int) or active_index < 0 or active_index >= len(streams):
            active_index = 0
            for i, s in enumerate(streams):
                if isinstance(s, dict) and not s.get("disable", False):
                    active_index = i
                    break
        if isinstance(streams[active_index], dict) and streams[active_index].get("disable", False):
            for i, s in enumerate(streams):
                if isinstance(s, dict) and not s.get("disable", False):
                    active_index = i
                    break

        for s in streams:
            if not isinstance(s, dict):
                continue
            if s.get("prefetch"):
                s["prefetch"] = True
            else:
                s.pop("prefetch", None)
            # mux-state-ui-v1: mux_state — только on/off, иначе убираем.
            _ms = s.get("mux_state", "auto")
            if _ms not in ("on", "off"):
                s.pop("mux_state", None)
            else:
                s["mux_state"] = _ms

        # stream_id — identity стрима. Приходит из UI как скрытое поле.
        # Для существующих стримов сохраняем, для новых (созданных в UI) —
        # назначаем свежий id, чтобы слот в streams_cache не потерял привязку.
        state.assign_stream_ids(streams)

        # mediainfo-invalidate-update-stream-v1: снимок старого состояния
        # активного потока ДО сохранения. После сохранения сравним — если
        # сменился active_stream_index / mux_state / url / resolver активного
        # потока, инвалидируем mediainfo-кэш Jellyfin для этого канала.
        _old_active_idx = None
        _old_mux_state = None
        _old_url = None
        _old_resolver = None
        _old_streams_snapshot = []
        try:
            _ch_old = state.get_channel(orig_name)
            if _ch_old:
                _old_active_idx = _ch_old.get("active_stream_index", 0)
                _old_streams_snapshot = list(_ch_old.get("streams", []))
                if isinstance(_old_active_idx, int) and 0 <= _old_active_idx < len(_old_streams_snapshot):
                    _old_s = _old_streams_snapshot[_old_active_idx]
                    if isinstance(_old_s, dict):
                        _old_mux_state = _old_s.get("mux_state", "auto")
                        _old_url = _old_s.get("url", "")
                        _old_resolver = _old_s.get("resolver", "auto")
        except Exception as _e:
            logger.warning(f"[MEDIAINFO] '{orig_name}': failed to snapshot old state: {_e}")

        with state.channels_lock:
            channels = state.load_channels()

            # Уникальность имени. 
            if new_name != orig_name and any(c["name"] == new_name for c in channels):
                return JSONResponse(
                    {"success": False, "error": f"Канал с именем '{new_name}' уже существует"},
                    status_code=409,
                )

            found = False
            for ch in channels:
                if ch["name"] == orig_name:
                    old_streams = list(ch.get("streams", []))

                    ch["name"] = new_name
                    ch["chno"] = data.get("chno", ch.get("chno", ""))
                    ch["group"] = data.get("group", ch.get("group", ""))
                    ch["logo"] = data.get("logo", ch.get("logo", ""))
                    ch["comment"] = data.get("comment", ch.get("comment", ""))
                    if "fallback" in data:
                        ch["fallback"] = bool(data.get("fallback", False))
                    if "disable" in data:
                        ch["disable"] = bool(data.get("disable", False))

                    new_tvgid = re.sub(
                        r'\s*\([^)]*\)\s*$', '',
                        str(data.get("tvgid", ch.get("tvgid", "")))
                    ).strip()
                    ch["tvgid"] = new_tvgid
                    if new_tvgid:
                        epg_names = state._epg_channels.get(new_tvgid)
                        if epg_names and epg_names[0]:
                            ch["real_name"] = epg_names[0]
                        elif "real_name" in data and data["real_name"]:
                            # Каталог ещё не подгружен — берём то, что прислал UI.
                            ch["real_name"] = data["real_name"]
                        # иначе — оставляем прежний real_name как есть
                    else:
                        ch["real_name"] = new_name
                    ch["streams"] = streams
                    ch["active_stream_index"] = active_index

                    if streams and active_index < len(streams):
                        active_stream = streams[active_index]
                        ch["url"] = active_stream.get("url", "")
                        ch["resolver"] = active_stream.get("resolver", "auto")
                        ch["ua"] = active_stream.get("ua", IPTV_DEFAULT_UA)
                        ch["fs_regex"] = active_stream.get("fs_regex", "")

                    # Синхронизируем _epg_cache: либо перенос при rename,
                    # либо splice streams_cache при изменении набора стримов.
                    # active_stream_index живёт в _active_index_map (в state),
                    # не в _epg_cache. Сначала переносим слоты при rename,
                    # потом splice, потом обновляем карту.
                    with state.cache_lock:
                        if new_name != orig_name:
                            if orig_name in state._epg_cache:
                                state._epg_cache[new_name] = state._epg_cache.pop(orig_name)
                            else:
                                state._epg_cache.setdefault(new_name, {"streams_cache": []})

                            # При rename подчищаем все per-channel карты от
                            # старого имени. Иначе:
                            #   - _active_index_map копит мусор и расходится
                            #     с config;
                            #   - _last_active даёт ложное «недавно активен»
                            #     если старое имя переиспользуют новым каналом;
                            #   - _failed_resolve_cache оставляет карантин
                            #     для несуществующего канала.
                            # Всё три защищены cache_lock — мы уже под ним.
                            state._active_index_map.pop(orig_name, None)
                            state._last_active.pop(orig_name, None)
                            state.pop_failed_resolve_for_channel(orig_name)
                        else:
                            state._epg_cache.setdefault(new_name, {"streams_cache": []})

                        entry = state._epg_cache[new_name]
                        _splice_streams_cache(entry, old_streams, streams)

                    state.set_active_index(new_name, active_index)
                    found = True
                    break

            if not found:
                return JSONResponse({"success": False, "error": "Исходный канал не найден"})

            sorted_channels = state.sort_channels_by_chno(channels)
            state.save_channels_to_file(sorted_channels)

        # Применяем pre-resolved кэш из UI (см. _apply_pre_resolved_cache).
        # Вызывается после _splice_streams_cache, чтобы индексы streams
        # уже соответствовали слотам. Каналы с rename — берём new_name.
        _apply_pre_resolved_cache(new_name, streams)

        state.save_cache()

        # channel-events-channels-v1: пишем cfg-события по каждому изменению.
        try:
            if orig_name != new_name:
                log_cfg(new_name, f"renamed: {orig_name} -> {new_name}", "ui")
            if _old_active_idx is not None and _old_active_idx != active_index:
                log_cfg(new_name, f"active_stream: {_old_active_idx} -> {active_index}", "ui")

            _old_by_id = {}
            for _os in _old_streams_snapshot:
                if isinstance(_os, dict):
                    _sid = _os.get("stream_id")
                    if isinstance(_sid, int) and _sid > 0:
                        _old_by_id[_sid] = _os
            _new_ids = set()
            for _i, _ns in enumerate(streams):
                if not isinstance(_ns, dict):
                    continue
                _sid = _ns.get("stream_id")
                if not isinstance(_sid, int) or _sid <= 0:
                    continue
                _new_ids.add(_sid)
                _os = _old_by_id.get(_sid)
                _url = _ns.get("url", "")
                if _os is None:
                    log_cfg(new_name, "added", "ui", stream_num=_i, stream_url=_url)
                    continue
                for _f in ("url", "resolver", "ua", "fs_regex", "mux_state"):
                    _o = _os.get(_f, "")
                    _n = _ns.get(_f, "")
                    if _f == "mux_state":
                        _o = _o or "auto"; _n = _n or "auto"
                    if _f == "resolver":
                        _o = _o or "auto"; _n = _n or "auto"
                    if _o != _n:
                        log_cfg(new_name, f"{_f}: {_o} -> {_n}", "ui",
                                stream_num=_i, stream_url=_url)
                # disable отдельно: событие disabled/enabled, не disable: False -> True.
                _o_dis = bool(_os.get("disable", False))
                _n_dis = bool(_ns.get("disable", False))
                if _o_dis != _n_dis:
                    log_cfg(new_name, "disabled" if _n_dis else "enabled", "ui",
                            stream_num=_i, stream_url=_url)
                _o_pf = bool(_os.get("prefetch", False))
                _n_pf = bool(_ns.get("prefetch", False))
                if _o_pf != _n_pf:
                    log_cfg(new_name, f"prefetch: {_o_pf} -> {_n_pf}", "ui",
                            stream_num=_i, stream_url=_url)
            for _sid, _os in _old_by_id.items():
                if _sid not in _new_ids:
                    log_cfg(new_name, "removed", "ui",
                            stream_url=_os.get("url", ""))
        except Exception as _e:
            logger.warning(f"[EVENTS] '{new_name}': cfg events failed: {_e}")

        # mediainfo-invalidate-update-stream-v1
        try:
            _new_mux_state = "auto"
            _new_url = ""
            _new_resolver = "auto"
            if isinstance(active_index, int) and 0 <= active_index < len(streams):
                _new_s = streams[active_index]
                if isinstance(_new_s, dict):
                    _new_mux_state = _new_s.get("mux_state", "auto")
                    _new_url = _new_s.get("url", "")
                    _new_resolver = _new_s.get("resolver", "auto")

            _changes = []
            if _old_active_idx is not None and _old_active_idx != active_index:
                _changes.append(f"active_index {_old_active_idx}->{active_index}")
            if _old_mux_state is not None and _old_mux_state != _new_mux_state:
                _changes.append(f"mux_state {_old_mux_state}->{_new_mux_state}")
            if _old_url is not None and _old_url != _new_url:
                _changes.append("url changed")
            if _old_resolver is not None and _old_resolver != _new_resolver:
                _changes.append(f"resolver {_old_resolver}->{_new_resolver}")

            if _changes:
                state.invalidate_jellyfin_mediainfo(
                    new_name, reason="; ".join(_changes)
                )
        except Exception as _e:
            logger.warning(f"[MEDIAINFO] '{new_name}': invalidate from update-stream failed: {_e}")

        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"[CHANNELS] update error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

@router.post("/channels/add")
async def add_new_channel(request: Request):
    try:
        data = await request.json()
        name = data.get("name")

        # URL приходит либо верхним уровнем (старый формат / внешние
        # клиенты), либо внутри streams — так шлёт текущий UI, где URL
        # вводится в модалке потоков, а не в основном окне.
        streams = data.get("streams", [])
        active_index = data.get("active_stream_index", 0)
        if not isinstance(active_index, int) or active_index < 0:
            active_index = 0

        # Совместимость со старым форматом: один поток в корне payload.
        if not streams and data.get("url"):
            streams = [{
                "url": data.get("url"),
                "resolver": data.get("resolver", "auto"),
                "ua": data.get("ua", IPTV_DEFAULT_UA),
                "fs_regex": data.get("fs_regex", ""),
                "disable": False,
            }]

        if not name or not streams:
            return JSONResponse({"success": False, "error": "Заполните название и хотя бы один поток с URL!"})

        # Хотя бы один поток должен иметь непустой url.
        if not any(isinstance(s, dict) and s.get("url") for s in streams):
            return JSONResponse({"success": False, "error": "У потока должен быть указан URL"})

        # Чистим стримы так же, как в /channels/update-stream:
        # не сохраняем False-флаги, назначаем stream_id.
        for s in streams:
            if not isinstance(s, dict):
                continue
            if s.get("prefetch"):
                s["prefetch"] = True
            else:
                s.pop("prefetch", None)
            r = s.get("resolver", "auto")
            if not is_valid_resolver(r):
                s["resolver"] = "auto"
        state.assign_stream_ids(streams)

        if active_index >= len(streams):
            active_index = 0

        with state.channels_lock:
            channels = state.load_channels()
            for ch in channels:
                if ch["name"] == name:
                    return JSONResponse({"success": False, "error": "Канал с таким именем уже существует!"})

            chno = str(data.get("chno", "")).strip()
            if not chno:
                chno = str(state.get_max_chno(channels) + 1)

            new_ch = {
                "name": name,
                "chno": chno,
                "group": data.get("group", ""),
                "logo": data.get("logo", ""),
                "tvgid": re.sub(r'\s*\([^)]*\)\s*$', '', str(data.get("tvgid", ""))).strip(),
                "disable": False,
                "streams": streams,
                "active_stream_index": active_index,
            }
            channels.append(new_ch)

            sorted_channels = state.sort_channels_by_chno(channels)
            state.save_channels_to_file(sorted_channels)

        # Применяем pre-resolved кэш из UI (см. _apply_pre_resolved_cache).
        _apply_pre_resolved_cache(name, streams)

        logger.info(f"[CHANNELS] '{name}': added (chno={chno}, streams={len(streams)})")
        try:
            log_cfg(name, "added", "ui")
        except Exception:
            pass
        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"[CHANNELS] add error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

# channels-refactor-v1
