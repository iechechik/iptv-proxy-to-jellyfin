"""
routers/channels_checks.py — проверки потоков каналов.

Вынесено из routers/channels.py (рефакторинг channels-refactor-v1).
Эндпоинты, которые не меняют конфиг, а только проверяют потоки:
  /channels/check-all/start
  /channels/check-all/status/{task_id}
  /channels/check-single
  /channels/probe
  /channels/check-mux
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import asyncio
import time

import core.state as state
from core.config import IPTV_DEFAULT_UA, logger
from services.resolver import is_valid_resolver, probe_stream
from services.limits import resolve_with_semaphores, probe_sem
from services.healthcheck import trigger_check_all
from services.events import send_broadcast_async

router = APIRouter()


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

# channels-refactor-v1
