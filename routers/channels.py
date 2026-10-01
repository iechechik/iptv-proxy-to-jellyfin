from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import asyncio
import re
import time

import core.state as state
from core.config import IPTV_DEFAULT_UA, logger
from services.resolver import is_valid_resolver, probe_stream
from services.limits import resolve_with_semaphores, probe_sem
from services.healthcheck import trigger_check_all
from services.events import send_broadcast_async
from services.epg_service import epg_manager

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


@router.post("/channels/check-all/start")
def start_healthcheck():
    channels = state.load_channels()
    if not channels:
        return JSONResponse({"success": False, "error": "Нет каналов для проверки"})

    # trigger_check_all сам создаёт task, шлёт SSE, кладёт каналы в очередь.
    task_id = trigger_check_all(channels)
    return JSONResponse({"success": True, "task_id": task_id})


@router.get("/channels/check-all/status/{task_id}")
def get_healthcheck_status(task_id: str):
    with state._healthcheck_lock:
        task = state._healthcheck_tasks.get(task_id)
        if not task:
            return JSONResponse({"success": False, "error": "Задача не найдена"})
        return JSONResponse({
            "success": True,
            "progress": task["progress"],
            "total": task["total"],
            "results": task["results"],
            "done": task["done"]
        })


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


@router.post("/channels/check-single")
async def check_single_stream(request: Request):
    try:
        data = await request.json()
        name = data.get("name", "")
        url = data.get("url")
        ua = data.get("ua", IPTV_DEFAULT_UA)
        fs_regex = data.get("fs_regex", "")
        resolver = data.get("resolver", "auto")
        if not url:
            return JSONResponse({"success": False, "detail": "URL не указан"})
        if not is_valid_resolver(resolver):
            resolver = "auto"

        logger.info(f"[HEALTHCHECK] checking: {url}")
        ch = {"name": name or "temp", "url": url, "ua": ua, "fs_regex": fs_regex, "resolver": resolver}
        try:
            is_direct, payload, expire_time, method = resolve_with_semaphores(ch)
            probe_elapsed = None

            if name:
                channels = state.load_channels()
                ch_found = next((c for c in channels if c["name"] == name), None)
                if ch_found:
                    # Ищем слот по url+resolver — именно в него пишем результат проверки,
                    # а не обязательно в активный: пользователь мог проверить другой стрим.
                    target_stream_index = None
                    for s_idx, s in enumerate(ch_found.get("streams", [])):
                        if s.get("url") == url and s.get("resolver", "auto") == resolver:
                            target_stream_index = s_idx
                            break
                    if target_stream_index is None:
                        for s_idx, s in enumerate(ch_found.get("streams", [])):
                            if s.get("url") == url:
                                target_stream_index = s_idx
                                break

                    if target_stream_index is not None:
                        state.set_stream_cache(name, target_stream_index, payload, is_direct,
                                               expire_time, method, probe_elapsed)
                        # Санитарные отметки на слоте
                        with state.cache_lock:
                            entry = state._epg_cache.setdefault(
                                name, {"streams_cache": []}
                            )
                            streams = entry.setdefault("streams_cache", [])
                            while len(streams) <= target_stream_index:
                                streams.append({})
                            if not isinstance(streams[target_stream_index], dict):
                                streams[target_stream_index] = {}
                            streams[target_stream_index]["last_checked_url"] = url
                            streams[target_stream_index]["last_checked_resolver"] = resolver
                        state.save_cache()

                        if resolver == "auto" and method != "auto":
                            with state.channels_lock:
                                channels = state.load_channels()
                                for ch_ in channels:
                                    if ch_["name"] == name:
                                        streams = ch_.get("streams", [])
                                        if target_stream_index < len(streams):
                                            streams[target_stream_index]["resolver"] = method
                                            if ch_.get("active_stream_index") == target_stream_index:
                                                ch_["resolver"] = method
                                            state.save_channels_to_file(channels)
                                            logger.info(f"[CHECK] '{name}': stream {target_stream_index} resolver set to '{method}'")
                                        break

                        # SSE про статус активного слота — обновляем только если
                        # проверяли активный (иначе UI покажет не то).
                        if ch_found.get("active_stream_index") == target_stream_index:
                            send_broadcast_async(name, {
                                "last_check_time": time.time(),
                                "last_check_success": True,
                                "last_check_detail": f"Resolved via {method}"
                            })
                    else:
                        logger.warning(f"[HEALTHCHECK] '{name}': URL did not match any stream")
                else:
                    logger.warning(f"[HEALTHCHECK] '{name}': channel not found in config")
            return JSONResponse({
                "success": True,
                "detail": f"Resolved via {method}",
                "method": method,
                "cached_stream": payload,
                "cache_expire": expire_time,
                "probe": None
            })
        except Exception as e:
            if name:
                # Провал проверки — пишем в слот того стрима, который проверяли
                # (если смогли его найти). Если не нашли — в активный.
                channels = state.load_channels()
                ch_found = next((c for c in channels if c["name"] == name), None)
                target_stream_index = None
                if ch_found:
                    for s_idx, s in enumerate(ch_found.get("streams", [])):
                        if s.get("url") == url and s.get("resolver", "auto") == resolver:
                            target_stream_index = s_idx
                            break
                    if target_stream_index is None:
                        for s_idx, s in enumerate(ch_found.get("streams", [])):
                            if s.get("url") == url:
                                target_stream_index = s_idx
                                break
                    if target_stream_index is None:
                        target_stream_index = ch_found.get("active_stream_index", 0)
                else:
                    target_stream_index = 0

                with state.cache_lock:
                    entry = state._epg_cache.setdefault(
                        name, {"streams_cache": []}
                    )
                    streams = entry.setdefault("streams_cache", [])
                    while len(streams) <= target_stream_index:
                        streams.append({})
                    if not isinstance(streams[target_stream_index], dict):
                        streams[target_stream_index] = {}
                    streams[target_stream_index]["last_check_time"] = time.time()
                    streams[target_stream_index]["last_check_success"] = False
                    streams[target_stream_index]["last_check_detail"] = str(e)
                state.save_cache()

                if ch_found and ch_found.get("active_stream_index") == target_stream_index:
                    send_broadcast_async(name, {
                        "last_check_time": time.time(),
                        "last_check_success": False,
                        "last_check_detail": str(e)
                    })
            return JSONResponse({"success": False, "detail": str(e)})
    except Exception as e:
        logger.error(f"[HEALTHCHECK] check error: {e}")
        return JSONResponse({"success": False, "detail": str(e)})


@router.post("/channels/probe")
async def probe_channel_stream(request: Request):
    try:
        data = await request.json()
        name = data.get("name")
        url = data.get("url")

        if not url and not name:
            return JSONResponse({"success": False, "error": "Нужен URL или имя канала"})

        # Приоритет — URL. Сценарий: пользователь создаёт новый канал,
        # резолвит поток («Проверить поток» работает), но ffprobe падал
        # с «канал не найден» — потому что старый код при наличии name
        # шёл в get_channel_stream(name), а канала ещё нет в конфиге.
        # Симметрично: при проверке неактивного стрима существующего
        # канала (url передан) раньше тестировался активный стрим.
        if url:
            ch = {
                "name": name or "temp",
                "url": url,
                "ua": data.get("ua", IPTV_DEFAULT_UA),
                "fs_regex": data.get("fs_regex", ""),
                "resolver": data.get("resolver", "auto"),
            }

            def _resolve_and_probe_url():
                is_direct, payload, expire_time, method = resolve_with_semaphores(ch)
                with probe_sem:
                    probe_result = probe_stream(payload, channel=name)
                return is_direct, payload, expire_time, method, probe_result

            is_direct, payload, expire_time, method, probe_result = await asyncio.to_thread(_resolve_and_probe_url)
            probe_elapsed = probe_result.get("probe_elapsed") if probe_result.get("ok") else None

            # Если name указывает на существующий канал, а url совпадает
            # с одним из его стримов — пишем payload и probe_elapsed именно
            # в слот этого стрима, а не активного. Иначе «Проверить ffprobe»
            # на неактивном стриме затирал бы метрики активного.
            if name:
                ch_found = state.get_channel(name)
                if ch_found:
                    target_idx = None
                    for idx, s in enumerate(ch_found.get("streams", [])):
                        if s.get("url") == url:
                            target_idx = idx
                            break
                    if target_idx is not None:
                        state.set_stream_cache(name, target_idx, payload, is_direct,
                                               expire_time, method, probe_elapsed)
                        active_idx = ch_found.get("active_stream_index", 0)
                        with state.cache_lock:
                            entry = state._epg_cache.setdefault(name, {"streams_cache": []})
                            streams = entry.setdefault("streams_cache", [])
                            while len(streams) <= target_idx:
                                streams.append({})
                            if not isinstance(streams[target_idx], dict):
                                streams[target_idx] = {}
                            s = streams[target_idx]
                            s["last_checked_url"] = url
                            s["last_checked_resolver"] = data.get("resolver", "auto")
                            # set_stream_cache выше уже поставил last_check_success=True
                            # (его контракт — «резолв успешен»). Если probe провалился,
                            # переопределяем именно в target-слот, а не в активный.
                            if probe_result.get("ok"):
                                s["last_check_success"] = True
                                s["last_check_detail"] = f"Resolved via {method}"
                            else:
                                s["last_check_success"] = False
                                s["last_check_detail"] = f"Probe failed: {probe_result.get('detail', 'no detail')}"
                        state.save_cache()

                        # Статус канала в UI = активный стрим. Если проверяли
                        # неактивный — badge менять не надо. Симметрично
                        # check_single_stream (там ровно та же проверка).
                        if active_idx == target_idx:
                            if probe_result.get("ok"):
                                send_broadcast_async(name, {
                                    "last_check_time": time.time(),
                                    "last_check_success": True,
                                    "last_check_detail": f"Resolved via {method}"
                                })
                            else:
                                send_broadcast_async(name, {
                                    "last_check_time": time.time(),
                                    "last_check_success": False,
                                    "last_check_detail": f"Probe failed: {probe_result.get('detail', 'no detail')}"
                                })

            return JSONResponse({
                "success": True,
                "method": method,
                "probe": probe_result
            })

        # Ветка без url — резолвим активный стрим канала через кэш.
        # Используется «Проверить ffprobe» в модалке канала, когда канал
        # уже сохранён и проверяем его текущий активный поток.
        def _get_stream():
            return state.get_channel_stream(name)
        is_direct, payload, expire_time, method = await asyncio.to_thread(_get_stream)
        if method == "cache":
            _ch = next((c for c in state.load_channels() if c["name"] == name), None)
            if _ch:
                _idx = _ch.get("active_stream_index", 0)
                _streams = _ch.get("streams", [])
                if 0 <= _idx < len(_streams):
                    method = _streams[_idx].get("resolver", "auto")

        def _probe():
            return probe_stream(payload, channel=name)
        probe_result = await asyncio.to_thread(_probe)

        if probe_result.get("ok") and "probe_elapsed" in probe_result:
            ch = next((c for c in state.load_channels() if c["name"] == name), None)
            if ch:
                active_idx = ch.get("active_stream_index", 0)
                state.update_probe_elapsed_in_cache(name, active_idx, probe_result["probe_elapsed"])

        if probe_result.get("ok"):
            state.mark_channel_healthy(name, method=method)
            send_broadcast_async(name, {
                "last_check_time": time.time(),
                "last_check_success": True,
                "last_check_detail": f"Resolved via {method}"
            })
        else:
            detail = f"Probe failed: {probe_result.get('detail', 'no detail')}"
            state.mark_channel_unhealthy(name, detail=detail)
            state.save_cache()
            send_broadcast_async(name, {
                "last_check_time": time.time(),
                "last_check_success": False,
                "last_check_detail": detail
            })

        return JSONResponse({
            "success": True,
            "method": method,
            "probe": probe_result
        })
    except Exception as e:
        logger.error(f"[PROBE] error: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/channels/check-mux")
async def check_stream_mux(request: Request):
    """Проверяет, требуется ли мукс для конкретного потока.

    Резолвит URL → берёт master-плейлист → ищет #EXT-X-MEDIA:TYPE=AUDIO.
    Ничего не проигрывает, Jellyfin не трогает."""
    try:
        data = await request.json()
        name = data.get("name")
        url = data.get("url")
        ua = data.get("ua", IPTV_DEFAULT_UA)
        fs_regex = data.get("fs_regex", "")
        resolver = data.get("resolver", "auto")
        if not url:
            return JSONResponse({"success": False, "error": "URL не указан"})

        ch = {"name": name or "temp", "url": url, "ua": ua,
              "fs_regex": fs_regex, "resolver": resolver}

        def _work():
            is_direct, payload, expire_time, method = resolve_with_semaphores(ch)
            from services.proxy_service import needs_mux as _nm
            needs_mux_flag = _nm(payload, channel_name=name)
            return is_direct, payload, expire_time, method, needs_mux_flag

        is_direct, payload, expire_time, method, needs_mux_flag = await asyncio.to_thread(_work)

        # Кэшируем в слот, чтобы при следующем открытии модалки
        # бейдж был сразу, без повторного HTTP-запроса.
        if name:
            ch_found = state.get_channel(name)
            if ch_found:
                # check-mux-slot-v1: ищем slot сначала по url из streams,
                # потом по last_checked_url в кэше (на случай, если sniffer
                # перерезолвил payload и cached_stream отличается от url,
                # который прислал UI).
                target_idx = None
                for idx, s in enumerate(ch_found.get("streams", [])):
                    if s.get("url") == url:
                        target_idx = idx
                        break
                if target_idx is None:
                    with state.cache_lock:
                        _entry = state._epg_cache.get(name, {})
                        _slots = _entry.get("streams_cache", []) if isinstance(_entry, dict) else []
                        for idx, slot in enumerate(_slots):
                            if not isinstance(slot, dict):
                                continue
                            if slot.get("last_checked_url") == url:
                                target_idx = idx
                                break
                # Если всё равно не нашли — пишем в активный (лучше, чем ничего).
                if target_idx is None:
                    target_idx = ch_found.get("active_stream_index", 0)
                    logger.info(f"[CHECK-MUX] '{name}': URL не совпал ни с одним stream, пишу в активный slot {target_idx}")
                if target_idx is not None:
                    state.set_stream_cache(name, target_idx, payload, is_direct,
                                           expire_time, method)
                    with state.cache_lock:
                        entry = state._epg_cache.setdefault(name, {"streams_cache": []})
                        streams = entry.setdefault("streams_cache", [])
                        while len(streams) <= target_idx:
                            streams.append({})
                        if not isinstance(streams[target_idx], dict):
                            streams[target_idx] = {}
                        streams[target_idx]["needs_mux"] = needs_mux_flag
                        streams[target_idx]["last_checked_url"] = url
                    state.save_cache()

        return JSONResponse({"success": True, "needs_mux": needs_mux_flag, "method": method})
    except Exception as e:
        logger.error(f"[CHECK-MUX] error: {e}")
        return JSONResponse({"success": False, "error": str(e)})


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
        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"[CHANNELS] add error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

@router.post("/channels/apply-resolvers")
async def apply_resolvers(request: Request):
    try:
        last_task = None
        with state._healthcheck_lock:
            for task_id, task in state._healthcheck_tasks.items():
                if task.get("done"):
                    if last_task is None or task.get("finished_at", 0) > last_task.get("finished_at", 0):
                        last_task = task

        if not last_task:
            return JSONResponse({"success": False, "error": "Нет завершённой проверки"})

        results = last_task.get("results", {})
        updates = {}
        for name, res in results.items():
            if res.get("success") and res.get("method"):
                updates[name] = res["method"]

        if not updates:
            return JSONResponse({"success": False, "error": "Нет успешных методов для применения"})

        with state.channels_lock:
            channels = state.load_channels()
            updated_count = 0
            for ch in channels:
                if ch["name"] in updates and ch.get("resolver", "auto") != updates[ch["name"]]:
                    ch["resolver"] = updates[ch["name"]]
                    active_idx = ch.get("active_stream_index", 0)
                    if ch.get("streams") and active_idx < len(ch["streams"]):
                        ch["streams"][active_idx]["resolver"] = updates[ch["name"]]
                    updated_count += 1
                    # Смена resolver'а касается только активного стрима —
                    # не трогаем кэши остальных.
                    state.clear_stream_cache(ch["name"], active_idx)

            if updated_count > 0:
                sorted_channels = state.sort_channels_by_chno(channels)
                state.save_channels_to_file(sorted_channels)
                logger.info(f"[APPLY-RESOLVERS] resolvers updated for {updated_count} channels")

        return JSONResponse({
            "success": True,
            "updated": updated_count,
            "message": f"Обновлено резолверов: {updated_count}"
        })
    except Exception as e:
        logger.error(f"[APPLY-RESOLVERS] error: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/channels/reset-resolvers")
async def reset_resolvers(request: Request):
    try:
        with state.channels_lock:
            channels = state.load_channels()
            updated_count = 0
            for ch in channels:
                if ch.get("resolver", "auto") != "auto":
                    ch["resolver"] = "auto"
                    active_idx = ch.get("active_stream_index", 0)
                    if ch.get("streams") and active_idx < len(ch["streams"]):
                        ch["streams"][active_idx]["resolver"] = "auto"
                    updated_count += 1
                    state.clear_stream_cache(ch["name"], active_idx)
            if updated_count > 0:
                sorted_channels = state.sort_channels_by_chno(channels)
                state.save_channels_to_file(sorted_channels)
                logger.info(f"[RESET-RESOLVERS] resolvers reset for {updated_count} channels")
        return JSONResponse({
            "success": True,
            "updated": updated_count,
            "message": f"Сброшено резолверов: {updated_count}"
        })
    except Exception as e:
        logger.error(f"[RESET-RESOLVERS] error: {e}")
        return JSONResponse({"success": False, "error": str(e)})
