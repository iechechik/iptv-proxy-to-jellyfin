import time
import threading
import core.state as state
from core.config import logger, IPTV_FALLBACK_COOLDOWN
from services.resolver import probe_stream
from services.limits import resolve_with_semaphores, probe_sem
from services.events import send_broadcast_async

_fallback_last_switch_ts = {}
_fallback_in_progress = set()
_fallback_lock = threading.Lock()


def try_switch_to_healthy_stream(name: str) -> bool:
    with _fallback_lock:
        # Кулдаун
        if time.time() - _fallback_last_switch_ts.get(name, 0) < IPTV_FALLBACK_COOLDOWN:
            return False
        if name in _fallback_in_progress:
            return False
        _fallback_in_progress.add(name)

    try:
        channels = state.load_channels()
        ch = next((c for c in channels if c["name"] == name), None)
        if not ch:
            return False

        active_idx = ch.get("active_stream_index", 0)
        streams = ch.get("streams", [])

        # Сканируем стримы, начиная с 0. Активный пропускаем — он уже мёртв.
        for idx, stream in enumerate(streams):
            if stream.get("disable", False):
                continue
            if idx == active_idx:
                continue

            payload = None
            is_direct = True
            method = "cache"

            # 1) Проверяем свежий кэш — но перед переключением всё равно делаем probe
            cached = state.get_stream_cache(name, idx)
            if cached:
                is_direct, payload, expire_time = cached
                # get_stream_cache уже отсеивает протухшие записи,
                # повторная проверка expire_time здесь излишняя.
                try:
                    with probe_sem:
                        probe_result = probe_stream(payload, timeout=15, channel=name)
                except Exception as e:
                    logger.warning(f"[FALLBACK] '{name}': probe from cache failed idx={idx}: {e}")
                    probe_result = {"ok": False}

                if probe_result.get("ok"):
                    logger.info(f"[FALLBACK] '{name}': switching {active_idx} -> {idx} (cache + probe OK)")
                    _do_switch(name, idx)
                    return True
                else:
                    logger.info(f"[FALLBACK] '{name}': cached idx={idx} failed probe, resolving")
                    payload = None  # идём через резолв

            # 2) Резолвим заново и проверяем probe
            if payload is None:
                try:
                    temp_ch = {
                        "name": name,
                        "url": stream.get("url"),
                        "resolver": stream.get("resolver", "auto"),
                        "ua": stream.get("ua", ""),
                        "fs_regex": stream.get("fs_regex", "")
                    }
                    is_direct, payload, expire_time, method = resolve_with_semaphores(temp_ch)
                except Exception as e:
                    logger.warning(f"[FALLBACK] '{name}': resolve failed idx={idx}: {e}")
                    continue

                try:
                    with probe_sem:
                        probe_result = probe_stream(payload, timeout=15, channel=name)
                except Exception as e:
                    logger.warning(f"[FALLBACK] '{name}': probe after resolve failed idx={idx}: {e}")
                    continue

                if not probe_result.get("ok"):
                    logger.info(f"[FALLBACK] '{name}': idx={idx} failed probe after resolve")
                    continue

                # Сохраняем в кэш с probe_elapsed
                probe_elapsed = probe_result.get("probe_elapsed")
                state.set_stream_cache(name, idx, payload, is_direct, expire_time, method, probe_elapsed)

            logger.info(f"[FALLBACK] '{name}': switching {active_idx} -> {idx} (probe OK, method={method})")
            _do_switch(name, idx)
            return True

        logger.warning(f"[FALLBACK] '{name}': no alternative stream passed probe")
        return False
    finally:
        with _fallback_lock:
            _fallback_in_progress.discard(name)

def _do_switch(name: str, new_index: int):
    state.set_active_stream_index(name, new_index, source="fallback")
    state.pop_failed_resolve_for_channel(name)

    # Кулдаун обновляем под тем же локом, под которым его читает
    # try_switch_to_healthy_stream. Реальной гонки тут нет (для одного
    # name в _fallback_in_progress сидит максимум один поток), но
    # единая дисциплина снимает вопросы при будущих правках.
    with _fallback_lock:
        _fallback_last_switch_ts[name] = time.time()

    send_broadcast_async(event_type="channel-switched",
                         extra_data={"channel": name, "new_index": new_index, "reason": "redirect"})
