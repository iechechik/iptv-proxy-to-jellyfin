import time
import threading
import concurrent.futures
import queue

from core.config import (
    IPTV_DEFAULT_UA, IPTV_CACHE_TTL, IPTV_FAST_CACHE_TTL,
    IPTV_HEALTHCHECK_MIN_INTERVAL, IPTV_HEALTHCHECK_WORKERS, IPTV_HEALTHCHECK_INTERVAL, IPTV_FALLBACK_THRESHOLD,
    IPTV_SNIFFER_LIMIT, IPTV_FLARESOLVERR_LIMIT, IPTV_PROBE_LIMIT, IPTV_RESOLVER_LIMIT,
    logger
)
import core.state as state
from services.resolver import resolve_channel_payload, probe_stream
from services.events import send_broadcast_async, save_cache_and_broadcast
from services.segment_prefetch import cleanup_expired_segments
from services.mux_service import is_mux_alive_and_fresh

# Глобальные структуры
_executor = None
_task_queue = queue.Queue()
_scheduler_stop = threading.Event()

_next_check_at = {}          # {channel_name: timestamp}
_active_checks = set()       # каналы, которые сейчас проверяются
_queued_names = set()        # каналы, ожидающие в очереди
_queue_lock = threading.Lock()

# Семафоры
_sniffer_semaphore = threading.Semaphore(IPTV_SNIFFER_LIMIT)
_flaresolverr_semaphore = threading.Semaphore(IPTV_FLARESOLVERR_LIMIT)
_probe_semaphore = threading.Semaphore(IPTV_PROBE_LIMIT)
_resolver_semaphore = threading.Semaphore(IPTV_RESOLVER_LIMIT)


def revalidate_channel_in_background(ch: dict):
    """Фоновое обновление payload для активного стрима канала.
    Пишет через state.set_stream_cache — единая точка записи в слот."""
    name = ch["name"]
    with state._revalidating_lock:
        if name in state._revalidating_set:
            return
        state._revalidating_set.add(name)

    def _worker():
        try:
            with state._revalidate_semaphore:
                logger.info(f"[REVALIDATE] '{name}': background refresh started")
                is_direct, payload, expire_time, method = resolve_channel_payload(ch)

                active_idx = ch.get("active_stream_index", 0)
                state.set_stream_cache(name, active_idx, payload, is_direct, expire_time, method)

                with state.cache_lock:
                    entry = state._epg_cache.setdefault(
                        name, {"streams_cache": []}
                    )
                    streams = entry.setdefault("streams_cache", [])
                    while len(streams) <= active_idx:
                        streams.append({})
                    if not isinstance(streams[active_idx], dict):
                        streams[active_idx] = {}
                    streams[active_idx]["last_check_detail"] = f"Background refresh via {method}"
                    streams[active_idx]["last_checked_url"] = ch.get("url", "")
                    streams[active_idx]["last_checked_resolver"] = ch.get("resolver", "auto")

                save_cache_and_broadcast(name, {
                    "last_check_time": time.time(),
                    "last_check_success": True,
                    "last_check_detail": f"Background refresh via {method}"
                })
                logger.info(f"[REVALIDATE] '{name}': background refresh done")
        except Exception as e:
            logger.warning(f"[REVALIDATE] '{name}': background refresh failed: {e}")
        finally:
            with state._revalidating_lock:
                state._revalidating_set.discard(name)

    threading.Thread(target=_worker, daemon=True).start()


def _get_check_interval(ch) -> int:
    """Верхний предел интервала между проверками канала.

    Реальный интервал ограничивается оставшимся TTL кэша активного слота
    в _process_channel_check (через min(interval, ttl)).
    """
    name = ch["name"]
    s = state.get_active_stream_state(name)
    last_success = s.get("last_check_success")
    if last_success is False:
        return IPTV_FAST_CACHE_TTL
    return IPTV_CACHE_TTL


def _update_next_check(name: str, base_interval: int):
    with state._healthcheck_lock:
        _next_check_at[name] = time.time() + base_interval


def _channel_needs_check(name: str) -> bool:
    with state._healthcheck_lock:
        next_at = _next_check_at.get(name)
        if next_at is None:
            return True
        return time.time() >= next_at


def _worker():
    while True:
        try:
            task = _task_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if task is None:
            break

        task_type, payload = task
        if task_type == "check_channel":
            name, ch, task_id = payload
            with _queue_lock:
                _queued_names.discard(name)
                _active_checks.add(name)
            _process_channel_check(name, ch, task_id)
            with _queue_lock:
                _active_checks.discard(name)


def _ensure_slot(name: str, index: int) -> dict:
    """Возвращает slot-словарь, создавая его при необходимости. Вызывается под cache_lock."""
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
    """Пишет last_check_* = False в конкретный слот."""
    with state.cache_lock:
        s = _ensure_slot(name, index)
        s["last_check_time"] = time.time()
        s["last_check_success"] = False
        s["last_check_detail"] = detail


def _set_slot_success(name: str, index: int, detail: str):
    """Пишет last_check_* = True в конкретный слот."""
    with state.cache_lock:
        s = _ensure_slot(name, index)
        s["last_check_time"] = time.time()
        s["last_check_success"] = True
        s["last_check_detail"] = detail


def _finish_task_channel(name: str, task_id: str, result: dict):
    """Обновляет прогресс задачи и шлёт SSE-событие для одного канала.
    Единая точка — вызывается и в нормальном потоке, и в ветках раннего
    выхода (пропуск проверки), иначе task['done'] не выставляется и
    UI-спиннер крутится вечно."""
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
                                     "name": name
                                 })


def _process_channel_check(name: str, ch: dict, task_id: str = None):
    try:
        streams = ch.get("streams", [])
        if not streams:
            streams = [{
                "url": ch.get("url"),
                "resolver": ch.get("resolver", "auto"),
                "ua": ch.get("ua", IPTV_DEFAULT_UA),
                "fs_regex": ch.get("fs_regex", "")
            }]

        active_index = ch.get("active_stream_index", 0)
        stream_results = []

        with state.cache_lock:
            last_active = state._last_active.get(name, 0)
        recently_active = (time.time() - last_active) < 120

        # Канал сейчас смотрят через мукс? Тогда probe (ffprobe 15 сек)
        # бессмыслен: мукс сам тянет сегменты, а probe будет отбирать CPU
        # у ffmpeg. Пропускаем проверку целиком.
        try:
            if is_mux_alive_and_fresh(name, max_stall=30):
                logger.info(f"[HEALTHCHECK] '{name}': playing via mux, check skipped")
                _finish_task_channel(name, task_id, {
                    "success": True,
                    "detail": "Skipped (mux alive)",
                    "method": None,
                    "streams_results": []
                })
                return
        except Exception:
            pass

        for s_idx, stream in enumerate(streams):
            if stream.get("disable", False):
                stream_results.append({"index": s_idx, "success": False, "detail": "disabled"})
                continue

            temp_ch = {
                "name": name,
                "url": stream.get("url"),
                "resolver": stream.get("resolver", "auto"),
                "ua": stream.get("ua", IPTV_DEFAULT_UA),
                "fs_regex": stream.get("fs_regex", "")
            }

            try:
                with _resolver_semaphore:
                    resolver = stream.get("resolver", "auto").lower()
                    if resolver == "sniffer":
                        with _sniffer_semaphore:
                            is_direct, payload, expire_time, method = resolve_channel_payload(temp_ch)
                    elif resolver == "flaresolverr_session":
                        with _flaresolverr_semaphore:
                            is_direct, payload, expire_time, method = resolve_channel_payload(temp_ch)
                    else:
                        is_direct, payload, expire_time, method = resolve_channel_payload(temp_ch)

                result = {
                    "index": s_idx,
                    "success": True,
                    "detail": f"Resolved via {method}",
                    "method": method,
                    "probe_elapsed": None
                }

                # Probe только для fallback-каналов, которые сейчас НЕ смотрят.
                if ch.get("fallback") and not recently_active:
                    try:
                        with _probe_semaphore:
                            probe_result = probe_stream(payload, timeout=15, channel=name)
                        result["probe"] = probe_result
                        if probe_result.get("ok") and "probe_elapsed" in probe_result:
                            result["probe_elapsed"] = probe_result["probe_elapsed"]
                            state.set_stream_cache(name, s_idx, payload, is_direct, expire_time, method,
                                                   probe_elapsed=probe_result["probe_elapsed"])
                        else:
                            result["success"] = False
                            result["detail"] = f"Probe failed: {probe_result.get('detail', 'no detail')}"
                            state.set_stream_cache(name, s_idx, payload, is_direct, expire_time, method)

                    except Exception as e:
                        result["probe"] = {"ok": False, "detail": str(e)}
                        state.set_stream_cache(name, s_idx, payload, is_direct, expire_time, method)
                else:
                    state.set_stream_cache(name, s_idx, payload, is_direct, expire_time, method)

                stream_results.append(result)

            except Exception as e:
                stream_results.append({
                    "index": s_idx,
                    "success": False,
                    "detail": str(e),
                    "method": None,
                    "probe_elapsed": None
                })
                if s_idx == active_index:
                    # Не сносим слот целиком: payload мог ещё быть валиден
                    # (например, таймаут резолва при живом кэше). Просто
                    # помечаем статус как failed. set_stream_cache
                    # перезапишет слот при следующем успехе.
                    logger.warning(
                        f"[HEALTHCHECK] Активный стрим {s_idx} канала '{name}' "
                        f"не зарезолвился, помечаю failed"
                    )
                    _set_slot_failure(name, s_idx, str(e))

        # Fallback: выбрать лучший стрим на основе probe
        if ch.get("fallback") and not recently_active:
            switch_to = _select_best_stream(stream_results, active_index)
            if switch_to is not None and switch_to != active_index:
                logger.info(f"[FALLBACK] '{name}': switching stream {active_index} -> {switch_to}")
                state.set_active_stream_index(name, switch_to)
                active_index = switch_to

        # Перечитываем активный индекс — мог поменяться после fallback.
        # Именно по нему определяем итоговый статус канала.
        # Источник — _active_index_map в state (обновляется set_active_stream_index).
        idx_now = state.get_active_index(name)
        if isinstance(idx_now, int) and idx_now >= 0:
            active_index = idx_now

        active_result = next((r for r in stream_results if r.get("index") == active_index), None)
        active_ok = bool(active_result and active_result.get("success"))
        active_method = active_result.get("method") if active_result else None

        if active_ok:
            detail = f"Resolved via {active_method}" if active_method else "OK"
            _set_slot_success(name, active_index, detail)
        else:
            detail = f"Active stream {active_index} failed"
            _set_slot_failure(name, active_index, detail)

        # Фиксируем реальный resolver для активного стрима, если он был auto
        if ch.get("fallback") and active_ok and active_method and active_method != "auto":
            streams_ch = ch.get("streams", [])
            if active_index < len(streams_ch):
                if streams_ch[active_index].get("resolver", "auto") == "auto":
                    logger.info(f"[FALLBACK] '{name}': pinning resolver '{active_method}' for active stream {active_index}")
                    _fix_resolver(name, active_index, active_method)

        save_cache_and_broadcast(name, {
            "last_check_time": time.time(),
            "last_check_success": active_ok,
            "last_check_detail": detail
        })

        result = {
            "success": active_ok,
            "detail": detail,
            "method": active_method,
            "streams_results": stream_results
        }

        interval = _get_check_interval(ch)
        s_state = state.get_active_stream_state(name)
        cached_expire = s_state.get("cache_expire", 0)
        if cached_expire > time.time():
            ttl = cached_expire - time.time()
            interval = min(interval, int(ttl))
        # TTL — про то, когда payload невалиден; healthcheck — про то,
        # когда проактивно идти проверять канал. Не даём короткому TTL
        # (30 сек у sniffer) превращать планировщик в циклотрон:
        # реактивность сохраняет get_channel_stream, который резолвит
        # по требованию клиента. Здесь — пол, чтобы проверять не чаще,
        # чем раз в _MIN_INTERVAL.
        interval = max(interval, IPTV_HEALTHCHECK_MIN_INTERVAL)
        _update_next_check(name, interval)

        _finish_task_channel(name, task_id, result)

    except Exception as e:
        logger.error(f"[HEALTHCHECK] '{name}': check error: {e}")
        try:
            _finish_task_channel(name, task_id, {
                "success": False,
                "detail": f"Internal error: {e}",
                "method": None,
                "streams_results": []
            })
        except Exception as e2:
            logger.error(f"[HEALTHCHECK] task {task_id}: failed to close: {e2}")


def _select_best_stream(stream_results, active_index):
    """Выбирает лучший стрим на основе probe. Возвращает индекс для переключения или None."""
    candidates = []
    for res in stream_results:
        if not res.get("success", False):
            continue
        probe = res.get("probe", {})
        if not probe.get("ok"):
            continue
        has_video = probe.get("has_video", False)
        has_audio = probe.get("has_audio", False)
        score = (1 if has_video else 0) + (1 if has_audio else 0)
        probe_elapsed = res.get("probe_elapsed")
        candidates.append({
            "index": res["index"],
            "score": score,
            "probe_elapsed": probe_elapsed,
            "method": res.get("method")
        })

    if not candidates:
        return None

    candidates.sort(key=lambda x: (-x["score"], x["probe_elapsed"] if x["probe_elapsed"] is not None else float('inf')))

    best = candidates[0]
    current = next((c for c in candidates if c["index"] == active_index), None)

    if current is None:
        return best["index"]

    if best["score"] > current["score"]:
        return best["index"]
    elif best["score"] == current["score"] and best["probe_elapsed"] and current["probe_elapsed"]:
        if current["probe_elapsed"] > 0:
            diff_percent = (current["probe_elapsed"] - best["probe_elapsed"]) / current["probe_elapsed"] * 100
            if diff_percent >= IPTV_FALLBACK_THRESHOLD:
                return best["index"]

    return None


def _fix_resolver(name: str, stream_index: int, method: str):
    """Обновляет resolver для конкретного стрима канала в конфиге.

    Держим state.channels_lock на всё время операции (read-modify-write),
    чтобы параллельная UI-операция не перетёрла нашу правку.
    """
    with state.channels_lock:
        channels = state.load_channels()
        for ch in channels:
            if ch["name"] == name:
                streams = ch.get("streams", [])
                if stream_index < len(streams):
                    streams[stream_index]["resolver"] = method
                    if ch.get("active_stream_index") == stream_index:
                        ch["resolver"] = method
                state.save_channels_to_file(channels)
                logger.info(f"[FALLBACK] '{name}': stream {stream_index} resolver set to '{method}'")
                break


def _scheduler():
    while not _scheduler_stop.is_set():
        try:
            channels = state.load_channels()
            now = time.time()
            for ch in channels:
                name = ch["name"]

                # Если для канала сейчас работает живой мукс, который
                # недавно отдавал данные — не трогаем его. Клиент играет
                # через /mux/, ffmpeg сам тянет сегменты с CDN, а любой
                # sniffer (Chromium) или probe (ffprobe) в этот момент =
                # CPU-пик, из-за которого ffmpeg не успевает за CDN и
                # картинка сыпется. Смерть мукса отследит mux-watchdog —
                # когда last_data_time станет старым, is_mux_alive_and_fresh
                # вернёт False, и канал попадёт в очередь на проверку.
                try:
                    if is_mux_alive_and_fresh(name, max_stall=30):
                        continue
                except Exception:
                    pass

                s_state = state.get_active_stream_state(name)
                with state.cache_lock:
                    last_active = state._last_active.get(name, 0)

                recently_active = (now - last_active) < 60

                # Пока канал смотрят — не трогаем его. Поток заведомо жив
                # (Jellyfin тянет сегменты), а смерть во время просмотра
                # ловится Stop-webhook'ом. Принудительно проверяем только
                # если с последней проверки прошёл длинный TTL — на случай
                # тихой смерти, которую webhook почему-то не поймал.
                # Это защищает мукс-каналы от постоянных перерезолвов через
                # sniffer: раньше короткий TTL (60 сек) заставлял планировщик
                # запускать Chromium каждую минуту прямо во время просмотра.
                if recently_active:
                    last_check = s_state.get("last_check_time") or 0
                    if (now - last_check) < IPTV_CACHE_TTL:
                        continue

                if not _channel_needs_check(name):
                    continue
                with _queue_lock:
                    if name in _queued_names or name in _active_checks:
                        continue
                    _queued_names.add(name)
                with state._healthcheck_lock:
                    _next_check_at[name] = now + 10

                is_sniffer = any(
                    isinstance(s, dict) and s.get("resolver") == "sniffer"
                    for s in ch.get("streams", [])
                )
                _task_queue.put(("check_channel", (name, ch, None)))
                if is_sniffer:
                    time.sleep(1)
                
            time.sleep(IPTV_HEALTHCHECK_INTERVAL)
        except Exception as e:
            logger.error(f"[HEALTHCHECK] scheduler error: {e}")
            time.sleep(10)


def start_healthcheck_scheduler():
    global _executor
    if _executor is not None:
        return
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=IPTV_HEALTHCHECK_WORKERS)
    for _ in range(IPTV_HEALTHCHECK_WORKERS):
        _executor.submit(_worker)
    threading.Thread(target=_scheduler, daemon=True).start()

    # Начальный разброс для sniffer-каналов, чтобы они не стартовали
    # одной пачкой. Каждому — сдвиг на 10 секунд от предыдущего.
    try:
        sniffer_idx = 0
        now = time.time()
        for ch in state.load_channels():
            has_sniffer = any(
                isinstance(s, dict) and s.get("resolver") == "sniffer"
                for s in ch.get("streams", [])
            )
            if has_sniffer:
                with state._healthcheck_lock:
                    _next_check_at[ch["name"]] = now + sniffer_idx * 10
                sniffer_idx += 1
        logger.info(f"[HEALTHCHECK] sniffer stagger: {sniffer_idx} channels × 10s")
    except Exception as e:
        logger.warning(f"[HEALTHCHECK] sniffer stagger failed: {e}")
    
    logger.info(f"[HEALTHCHECK] scheduler started, workers={IPTV_HEALTHCHECK_WORKERS}, interval={IPTV_HEALTHCHECK_INTERVAL}s")


def stop_healthcheck_scheduler():
    global _executor
    _scheduler_stop.set()
    if _executor:
        for _ in range(IPTV_HEALTHCHECK_WORKERS):
            _task_queue.put(None)
        _executor.shutdown(wait=False)
        _executor = None


def run_healthcheck_async(task_id: str, channels: list, send_events: bool = False):
    total = len(channels)
    with state._healthcheck_lock:
        state._healthcheck_tasks[task_id] = {
            "progress": 0,
            "total": total,
            "results": {},
            "done": False,
            "complete_sent": False
        }

    if send_events:
        send_broadcast_async(event_type="healthcheck-start",
                             extra_data={"task_id": task_id, "total": total})

    for ch in channels:
        name = ch["name"]

        s_state = state.get_active_stream_state(name)
        last_check = s_state.get("last_check_time")
        if last_check and time.time() - last_check < 60:
            with state._healthcheck_lock:
                task = state._healthcheck_tasks[task_id]
                task["progress"] += 1
                task["results"][name] = {
                    "success": s_state.get("last_check_success", False),
                    "detail": "Skipped (recently checked)",
                    "method": None,
                    "streams_results": []
                }
                if task["progress"] >= task["total"]:
                    task["done"] = True
                    task["finished_at"] = time.time()
                send_broadcast_async(event_type="healthcheck-progress",
                                     extra_data={
                                         "task_id": task_id,
                                         "progress": task["progress"],
                                         "total": task["total"],
                                         "name": name
                                     })
            continue

        with _queue_lock:
            if name in _queued_names or name in _active_checks:
                with state._healthcheck_lock:
                    task = state._healthcheck_tasks[task_id]
                    task["progress"] += 1
                    task["results"][name] = {
                        "success": False,
                        "detail": "Already queued or in progress",
                        "method": None,
                        "streams_results": []
                    }
                    if task["progress"] >= task["total"]:
                        task["done"] = True
                        task["finished_at"] = time.time()
                    send_broadcast_async(event_type="healthcheck-progress",
                                         extra_data={
                                             "task_id": task_id,
                                             "progress": task["progress"],
                                             "total": task["total"],
                                             "name": name
                                         })
                continue
            _queued_names.add(name)

        _task_queue.put(("check_channel", (name, ch, task_id)))

    with state._healthcheck_lock:
        task = state._healthcheck_tasks.get(task_id)
        if task and task.get("done") and not task.get("complete_sent"):
            send_broadcast_async(event_type="healthcheck-complete",
                                 extra_data={"task_id": task_id, "total": task["total"]})
            task["complete_sent"] = True


def background_cleanup():
    while True:
        time.sleep(60)
        state.cleanup_expired_caches()
        cleanup_expired_segments()

        with state.cache_lock:
            stale = [n for n, ts in state._last_active.items() if time.time() - ts > 3600]
            for n in stale:
                state._last_active.pop(n, None)

        now = time.time()
        with state._healthcheck_lock:
            to_remove = [
                task_id for task_id, task in state._healthcheck_tasks.items()
                if task.get("done") and task.get("finished_at", 0) < now - 3600
            ]
            for task_id in to_remove:
                state._healthcheck_tasks.pop(task_id, None)
