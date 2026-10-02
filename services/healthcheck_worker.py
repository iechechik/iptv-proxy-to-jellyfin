"""
services/healthcheck_worker.py — проверка одного канала.

Вынесено из services/healthcheck.py (healthcheck-refactor-v1).

Содержит:
  _probe_with_semaphore      — ffprobe с семафором.
  _process_channel_check     — одна задача = один канал.
  _check_stream_with_cache   — резолв + verify одного стрима (probe или HEAD).
  _fix_resolvers             — пин резолверов после успешного резолва.
  _select_best_stream        — выбрать лучший стрим для switch.
  _schedule_next             — расписание следующей проверки.

Использует из healthcheck.py:
  _ensure_slot, _set_slot_success, _set_slot_failure,
  _finish_task_channel, _next_check_at,
  REASON_SCHEDULED, REASON_STALE.
"""

import time

from core.config import (
    IPTV_DEFAULT_UA, IPTV_CACHE_TTL, IPTV_FAST_CACHE_TTL,
    IPTV_HEALTHCHECK_MIN_INTERVAL, IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC,
    IPTV_FALLBACK_SWITCH_MIN_SEC_ACTIVE, IPTV_FALLBACK_SWITCH_SPEEDUP_SEC,
    logger,
)
import core.state as state
from services.resolver import (
    probe_stream, verify_stream_alive, parse_url_headers,
)
from services.mux_service import is_mux_alive_and_fresh
from services.limits import probe_sem, resolve_with_semaphores

# Импорт из healthcheck: модуль полностью загрузится к моменту первого
# вызова этих функций. Циклический импорт разрешён порядком загрузки
# (healthcheck.py импортирует worker.py в самом конце).
from services.healthcheck import (
    _ensure_slot, _set_slot_success, _set_slot_failure,
    _finish_task_channel, _next_check_at,
    REASON_SCHEDULED, REASON_STALE,
)


def _probe_with_semaphore(payload: str, name: str) -> dict:
    with probe_sem:
        return probe_stream(payload, timeout=15, channel=name)


def _process_channel_check(name: str, ch: dict, task_id: str = None,
                            reason: str = REASON_SCHEDULED, force: bool = False):
    """Одна задача = один канал.

    Режимы:
      playing=False — проверяем все стримы.
      playing=True  — проверяем всех, кроме активного (активный доказанно
                      жив — сегменты идут). Switch не делаем, ждём Stop.

    Метод проверки:
      fallback=true  -> ffprobe (нужен probe_elapsed для сравнения).
      fallback=false -> HEAD (один стрим, сравнивать не с чем).
    """
    try:
        streams = ch.get("streams", []) or []
        if not streams:
            _finish_task_channel(name, task_id, {
                "success": False, "detail": "No streams",
                "method": None, "streams_results": [],
            })
            return

        # --- Гард: мукс жив ---
        try:
            if is_mux_alive_and_fresh(name, max_stall=30):
                logger.info(f"[HEALTHCHECK] '{name}': mux alive, skip")
                _finish_task_channel(name, task_id, {
                    "success": True, "detail": "Skipped (mux alive)",
                    "method": None, "streams_results": [],
                })
                return
        except Exception:
            pass

        # --- Определяем режим ---
        with state.cache_lock:
            last_active = state._last_active.get(name, 0)
        recently_active = (time.time() - last_active) < IPTV_HEALTHCHECK_RECENTLY_ACTIVE_SEC
        stale_bypasses = (reason == REASON_STALE)
        playing = recently_active and not force and not stale_bypasses

        active_index = state.get_active_index(name)
        if not isinstance(active_index, int) or active_index < 0 or active_index >= len(streams):
            active_index = 0

        fallback_on = bool(ch.get("fallback"))

        # --- Какие стримы проверяем ---
        if playing:
            indices_to_check = [i for i in range(len(streams)) if i != active_index]
            logger.info(f"[HEALTHCHECK] '{name}': playing, checking {len(indices_to_check)} non-active stream(s)")
        else:
            indices_to_check = list(range(len(streams)))

        # --- Проверка ---
        # results_by_index[i] = {"success", "detail", "method", "probe_elapsed",
        #                        "payload", "is_direct", "expire_time"}
        results_by_index = {}

        for i in indices_to_check:
            s = streams[i]
            if s.get("disable", False):
                results_by_index[i] = {
                    "success": False, "detail": "disabled",
                    "method": None, "probe_elapsed": None,
                    "payload": None, "is_direct": None, "expire_time": None,
                }
                continue

            res = _check_stream_with_cache(
                name, s, i, use_probe=fallback_on,
            )
            results_by_index[i] = res

            if res["success"] and res["payload"]:
                state.set_stream_cache(
                    name, i, res["payload"], res["is_direct"],
                    res["expire_time"], res["method"],
                    probe_elapsed=res.get("probe_elapsed"),
                )
                with state.cache_lock:
                    slot = _ensure_slot(name, i)
                    slot["last_checked_url"] = s.get("url", "")
                    slot["last_checked_resolver"] = s.get("resolver", "auto")
            else:
                _set_slot_failure(name, i, res["detail"])

        # --- Fallback / switch ---
        if fallback_on and not playing:
            candidates = []
            for i, r in results_by_index.items():
                if r.get("success") and r.get("probe_elapsed") is not None:
                    candidates.append({
                        "index": i,
                        "score": 2,  # ffprobe уже отфильтровал мёртвых
                        "probe_elapsed": r["probe_elapsed"],
                        "method": r.get("method"),
                    })

            best = _select_best_stream(candidates, active_index)

            if best is not None and best != active_index:
                logger.info(f"[HEALTHCHECK] '{name}': switch {active_index} -> {best}")
                state.set_active_stream_index(name, best, source="healthcheck")
                active_index = best

        # --- Финал: статус активного слота ---
        if playing:
            # Активный не проверялся — отметим, что пропущен, чтобы UI
            # видел свежее время.
            _set_slot_success(name, active_index, "Skipped (playing)")
            active_ok = None  # статус не меняем, оставляем как было
            # mark_channel_healthy не вызываем — канал не проверялся,
            # состояние health не обновляем.
        else:
            active_r = results_by_index.get(active_index)
            active_ok = bool(active_r and active_r.get("success"))
            if active_ok:
                _set_slot_success(name, active_index,
                                  f"Resolved via {active_r.get('method') or 'unknown'}")
                # channel-events-v2: переходы UP/DOWN пишет сам mark_channel_healthy.
                try:
                    state.mark_channel_healthy(
                        name,
                        method=(active_r.get('method') or 'probe'),
                        source="healthcheck",
                    )
                except Exception as _e:
                    logger.warning(f"[EVENTS] '{name}': mark_channel_healthy failed: {_e}")
            else:
                detail = (active_r or {}).get("detail") or "Active stream failed"
                _set_slot_failure(name, active_index, detail)
                try:
                    state.mark_channel_unhealthy(
                        name, detail=detail, source="healthcheck"
                    )
                except Exception as _e:
                    logger.warning(f"[EVENTS] '{name}': mark_channel_unhealthy failed: {_e}")

        # --- Pin resolvers (только fallback-каналы) ---
        # Собираем все успешно резолвнутые стримы с method != auto/cache,
        # у которых в конфиге стоит resolver=auto. Один вызов _fix_resolvers
        # на канал: одна загрузка config.json, одна запись.
        #
        # Работает и для playing=True (кандидаты), и для playing=False
        # (все стримы, включая активный).
        if fallback_on:
            streams_ch = ch.get("streams", [])
            pins = []
            for i, r in results_by_index.items():
                if not r.get("success"):
                    continue
                m = r.get("method")
                if not m or m in ("auto", "cache"):
                    continue
                if i >= len(streams_ch):
                    continue
                if not isinstance(streams_ch[i], dict):
                    continue
                if streams_ch[i].get("resolver", "auto") != "auto":
                    continue
                pins.append((i, m))
            if pins:
                try:
                    _fix_resolvers(name, pins)
                except Exception as e:
                    logger.warning(f"[HEALTHCHECK] '{name}': _fix_resolvers failed: {e}")

        # --- Прогресс UI ---
        streams_results = []
        for i, r in results_by_index.items():
            streams_results.append({
                "index": i,
                "success": r.get("success"),
                "detail": r.get("detail"),
                "method": r.get("method"),
                "probe_elapsed": r.get("probe_elapsed"),
            })

        if playing:
            _finish_task_channel(name, task_id, {
                "success": True,
                "detail": "Skipped active (playing)",
                "method": None,
                "streams_results": streams_results,
            })
        else:
            detail = f"Active stream {active_index} " + ("OK" if active_ok else "failed")
            _finish_task_channel(name, task_id, {
                "success": bool(active_ok),
                "detail": detail,
                "method": None,
                "streams_results": streams_results,
            })

        # --- Расписание ---
        _schedule_next(name, success=bool(active_ok) if active_ok is not None else True)

    except Exception as e:
        logger.exception(f"[HEALTHCHECK] '{name}': check crashed")
        try:
            _finish_task_channel(name, task_id, {
                "success": False, "detail": f"Internal error: {e}",
                "method": None, "streams_results": [],
            })
        except Exception:
            pass


def _check_stream_with_cache(name: str, stream: dict, index: int, use_probe: bool) -> dict:
    """Резолвит и проверяет один стрим.

    Если в слоте есть свежий payload (cache_expire > now) — используем его,
    не резолвим. Иначе резолвим.

    use_probe=True  -> после получения payload делаем ffprobe.
    use_probe=False -> HEAD.

    Возвращает dict.
    """
    s_idx = index

    # --- Payload: кэш или резолв ---
    cached = state.get_stream_cache(name, s_idx)
    if cached:
        is_direct, payload, expire_time = cached
        method = stream.get("resolver", "auto")
        if method == "auto":
            # Метод теряется при чтении из кэша (get_stream_cache возвращает
            # только payload+expire). Читаем last_checked_resolver из слота —
            # если он там есть, используем как method. Иначе "cache".
            with state.cache_lock:
                _entry = state._epg_cache.get(name, {})
                _streams = _entry.get("streams_cache", []) if isinstance(_entry, dict) else []
                _slot = _streams[s_idx] if 0 <= s_idx < len(_streams) else {}
                method = (_slot.get("last_checked_resolver") if isinstance(_slot, dict) else None) or "cache"
    else:
        temp_ch = {
            "name": name,
            "url": stream.get("url"),
            "resolver": stream.get("resolver", "auto"),
            "ua": stream.get("ua", IPTV_DEFAULT_UA),
            "fs_regex": stream.get("fs_regex", ""),
        }
        try:
            is_direct, payload, expire_time, method = resolve_with_semaphores(temp_ch)
        except Exception as e:
            return {
                "success": False,
                "detail": f"Resolve failed: {e}",
                "method": None,
                "probe_elapsed": None,
                "payload": None, "is_direct": None, "expire_time": None,
            }

    # --- Verify ---
    probe_elapsed = None
    if use_probe:
        try:
            probe = _probe_with_semaphore(payload, name)
        except Exception as e:
            probe = {"ok": False, "detail": f"ffprobe raised: {e}"}
        if not probe.get("ok"):
            return {
                "success": False,
                "detail": f"Probe failed: {probe.get('detail', 'no detail')}",
                "method": method,
                "probe_elapsed": None,
                "payload": None, "is_direct": None, "expire_time": None,
            }
        probe_elapsed = probe.get("probe_elapsed")
    else:
        try:
            _clean, hdrs = parse_url_headers(payload)
            ua = hdrs.get("User-Agent", IPTV_DEFAULT_UA) if isinstance(hdrs, dict) else IPTV_DEFAULT_UA
        except Exception:
            ua = IPTV_DEFAULT_UA
        try:
            ok = verify_stream_alive(payload, ua=ua)
        except Exception:
            ok = False
        if not ok:
            return {
                "success": False,
                "detail": "HEAD failed",
                "method": method,
                "probe_elapsed": None,
                "payload": None, "is_direct": None, "expire_time": None,
            }

    return {
        "success": True,
        "detail": f"OK via {method}",
        "method": method,
        "probe_elapsed": probe_elapsed,
        "payload": payload,
        "is_direct": is_direct,
        "expire_time": expire_time,
    }


def _fix_resolvers(name: str, pins: list):
    """Пинит резолверы сразу для нескольких стримов канала.

    pins — список [(stream_index, method), ...]. Внутри отбрасываются
    method="auto"/"cache" и стримы, у которых resolver уже не auto.

    Одна загрузка config.json, одна запись. Для канала с 3 auto-стримами
    это 1 save вместо 3.

    Вызывается из _process_channel_check для fallback-каналов после
    проверки: пиним все успешно резолвнутые auto-стримы (и активный,
    и кандидатов при playing).
    """
    cleaned = [(i, m) for i, m in pins if m not in ("auto", "cache")]
    if not cleaned:
        return
    with state.channels_lock:
        channels = state.load_channels()
        for ch in channels:
            if ch["name"] != name:
                continue
            streams = ch.get("streams", [])
            changed = []
            for idx, method in cleaned:
                if idx >= len(streams):
                    continue
                if not isinstance(streams[idx], dict):
                    continue
                if streams[idx].get("resolver", "auto") != "auto":
                    continue
                streams[idx]["resolver"] = method
                if ch.get("active_stream_index") == idx:
                    ch["resolver"] = method
                changed.append((idx, method))
            if changed:
                state.save_channels_to_file(channels)
                logger.info(f"[HEALTHCHECK] '{name}': pinned {changed}")
            break


def _select_best_stream(candidates: list, active_index: int):
    """Выбирает лучший стрим из уже отфильтрованных кандидатов.

    candidates — список dict {"index", "score", "probe_elapsed", "method"}.
    Возвращает index лучшего или None, если switch не нужен.

    Критерии switch:
      1. Лучший кандидат имеет больший score, чем активный.
      2. score равен, и активный медленнее >= switch_min_sec_active,
         и кандидат быстрее активного на >= switch_speedup_sec.
    """
    if not candidates:
        return None

    best = min(candidates, key=lambda c: c["probe_elapsed"] if c["probe_elapsed"] is not None else float("inf"))
    current = next((c for c in candidates if c["index"] == active_index), None)

    if current is None:
        return best["index"]

    if best["index"] == active_index:
        return None

    if best["score"] > current["score"]:
        return best["index"]

    if best["score"] == current["score"]:
        cur_el = current["probe_elapsed"]
        best_el = best["probe_elapsed"]
        if cur_el is None or best_el is None:
            return None
        if cur_el < IPTV_FALLBACK_SWITCH_MIN_SEC_ACTIVE:
            return None
        if (cur_el - best_el) >= IPTV_FALLBACK_SWITCH_SPEEDUP_SEC:
            return best["index"]

    return None


def _schedule_next(name: str, success: bool):
    """Ставит next_check_at[name] с учётом TTL и min_interval."""
    base = IPTV_CACHE_TTL if success else IPTV_FAST_CACHE_TTL
    s_state = state.get_active_stream_state(name)
    cached_expire = s_state.get("cache_expire", 0)
    if cached_expire > time.time():
        ttl_left = cached_expire - time.time()
        base = min(base, ttl_left)
    base = max(int(base), IPTV_HEALTHCHECK_MIN_INTERVAL)
    with state._healthcheck_lock:
        _next_check_at[name] = time.time() + base


# ---------------------------------------------------------------------------
# Воркер
# ---------------------------------------------------------------------------


# healthcheck-refactor-v1
