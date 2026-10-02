"""
routers/stream.py — redirect, m3u, synthetic, black.ts.

После stream-refactor-v1 здесь остаётся то, что видит Jellyfin
как точку входа канала. HLS-прокси вынесен в stream_hls.py,
мукс — в stream_mux.py.
"""
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
import asyncio
import os
import re
import threading
import time
from urllib.parse import quote

import core.state as state
from core.config import IPTV_FAILED_RESOLVE_TTL, logger
from services.resolver import parse_url_headers
from services.proxy_service import (
    _proxy_googlevideo_manifest, _build_redirect_response,
    needs_mux, BLACK_MANIFEST,
)
from services.fallback import try_switch_to_healthy_stream
from services.healthcheck import revalidate_channel_in_background
from services.events import save_cache_and_broadcast

router = APIRouter()

def _decode_body_gzip_aware(raw: bytes) -> str:
    """Декодирует тело HTTP-ответа. Если body начинается с gzip magic-bytes
    (0x1f 0x8b) — распаковывает. Некоторые CDN (ntv.ru) отдают gzip для
    вложенных m3u8-манифестов без явного Content-Encoding: gzip.
    TS fast-path вызывается ДО этой функции и не затрагивается.
    """
    if raw[:2] == b"\x1f\x8b":
        import gzip as _gz
        try:
            raw = _gz.decompress(raw)
        except Exception as _e:
            logger.warning(f"[HLS-PROXY] gzip decompress failed: {_e}")
    return raw.decode("utf-8", errors="ignore")

# «Чёрный» манифест: 5-секундный VOD с одним сегментом.
# Jellyfin его проигрывает и корректно останавливает сессию,
# вместо бесконечных переподключений.
def _get_active_index(name: str) -> int:
    """Индекс активного слота канала. Тонкая обёртка над state.get_active_index."""
    return state.get_active_index(name)


def _write_slot_key(name: str, index: int, key: str, value):
    """Точечная запись одного ключа в слот streams_cache[index]."""
    with state.cache_lock:
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
        streams[index][key] = value


def _get_or_compute_needs_mux(name: str, payload: str, active_idx: int) -> bool:
    """Возвращает needs_mux для активного слота. Если cached_stream слота
    совпадает с payload и значение уже посчитано — берём из слота. Иначе
    считаем заново и кэшируем в слот."""
    with state.cache_lock:
        entry = state._epg_cache.get(name, {})
        if isinstance(entry, dict):
            streams = entry.get("streams_cache", [])
            if 0 <= active_idx < len(streams) and isinstance(streams[active_idx], dict):
                s = streams[active_idx]
                if s.get("cached_stream") == payload and s.get("needs_mux") is not None:
                    return bool(s["needs_mux"])
    flag = needs_mux(payload, channel_name=name)
    _write_slot_key(name, active_idx, "needs_mux", flag)
    state.save_cache()
    return flag


def _build_stream_response(name: str, request: Request, is_direct: bool, payload: str, expire_time: float, method: str, active_idx: int = None):
    """Формирует ответ для уже полученного потока.

    active_idx — если известен из peek/resolve, передаём явно, чтобы
    не перечитывать _active_index_map. Иначе читаем (гонка возможна:
    healthcheck мог переключить активный стрим между peek и вызовом).
    """
    if active_idx is None:
        active_idx = _get_active_index(name)

    if is_direct:
        clean_url, _ = parse_url_headers(payload)
        logger.info(f"[STREAM] '{name}': direct stream, URL={clean_url[:100]}...")
        if ".m3u8" in clean_url.lower():
            # mux-state-v1: mux_state активного stream имеет приоритет
            # над needs_mux.
            _mux_state = "auto"
            _ch_ms = state.get_channel(name)
            if _ch_ms:
                _mux_state = _ch_ms.get("mux_state", "auto")
                if _mux_state not in ("auto", "on", "off"):
                    _mux_state = "auto"
            if _mux_state == "on":
                logger.info(f"[STREAM] '{name}': mux_state=on, mux forced")
                return RedirectResponse(f"/mux/{quote(name)}.ts", status_code=302)
            if _mux_state == "off":
                logger.info(f"[STREAM] '{name}': mux_state=off, skipping mux")
                # mux-off-kills-v1: убить существующий мукс-процесс,
                # иначе UI показывает MUX и healthcheck думает, что
                # канал играет через мукс.
                try:
                    from services.mux_service import invalidate_mux
                    invalidate_mux(name)
                except Exception as _e:
                    logger.warning(f"[MUX] '{name}': invalidate on off failed: {_e}")
                # падаем вниз, отдадим через _build_redirect_response
            else:
                # auto — текущее поведение
                needs_mux_flag = _get_or_compute_needs_mux(name, payload, active_idx)
                if needs_mux_flag:
                    logger.info(f"[STREAM] '{name}': mux selected (auto/needs_mux), redirecting to /mux/{quote(name)}.ts")
                    return RedirectResponse(f"/mux/{quote(name)}.ts", status_code=302)
        if "googlevideo.com" in payload:
            client_ua = request.headers.get("user-agent")
            proxy_response = _proxy_googlevideo_manifest(payload, client_ua)
            if proxy_response:
                return proxy_response
            return _build_redirect_response(payload, channel=name)

        return _build_redirect_response(payload, channel=name)

    else:
        logger.info(f"[STREAM] '{name}': synthetic manifest, checking mux...")
        if needs_mux(payload, channel_name=name):
            _write_slot_key(name, active_idx, "needs_mux", True)
            state.save_cache()
            return RedirectResponse(f"/mux/{quote(name)}.ts", status_code=302)
        # payload уже лежит в streams_cache[active_idx] (записан set_stream_cache
        # в get_channel_stream). Отдельного _synthetic_manifests больше нет.
        return RedirectResponse(f"/synthetic/{quote(name)}.m3u8")


@router.get("/redirect/{name}.m3u8")

@router.get("/redirect/{name}.ts")
async def redirect_channel_with_ext(name: str, request: Request):
    return await redirect_channel(name, request)


def _handle_stream_failure(name: str, active_idx: int, error: str, now: float):
    with state.cache_lock:
        if name not in state._epg_cache or not isinstance(state._epg_cache[name], dict):
            state._epg_cache[name] = {"streams_cache": []}
        entry = state._epg_cache[name]
        streams = entry.setdefault("streams_cache", [])
        if not isinstance(streams, list):
            streams = []
            entry["streams_cache"] = streams
        while len(streams) <= active_idx:
            streams.append({})
        if not isinstance(streams[active_idx], dict):
            streams[active_idx] = {}
        s = streams[active_idx]
        s["last_check_time"] = now
        s["last_check_success"] = False
        s["last_check_detail"] = str(error)
        state._failed_resolve_cache[(name, active_idx)] = (str(error), time.time() + IPTV_FAILED_RESOLVE_TTL)
    save_cache_and_broadcast(name, {
        "last_check_time": now,
        "last_check_success": False,
        "last_check_detail": str(error)
    })
    logger.info(f"[BLACK-TS] '{name}': serving black manifest")
    return Response(content=BLACK_MANIFEST, media_type="application/vnd.apple.mpegurl")

@router.get("/redirect/{name}")
async def redirect_channel(name: str, request: Request):
    state.cleanup_expired_caches()
    state.mark_channel_active(name)

    active_idx = state.get_active_index(name)
    with state.cache_lock:
        failed = state._failed_resolve_cache.get((name, active_idx))
        if failed and failed[1] > time.time():
            logger.info(f"[BLACK-TS] '{name}': quarantined, serving black manifest")
            return Response(content=BLACK_MANIFEST, media_type="application/vnd.apple.mpegurl")

    ch = state.get_channel(name)
    if not ch:
        logger.warning(f"[STREAM] '{name}': channel not found (404)")
        return Response("Channel not found", status_code=404)

    if ch.get("disable", False):
        logger.warning(f"[STREAM] '{name}': request rejected, channel disabled")
        return Response("Channel is disabled", status_code=403)

    # Stale-while-revalidate: если payload есть, но TTL протух — отдаём
    # старый НЕМЕДЛЕННО, resolve запускаем в фоне. Jellyfin никогда не
    # ждёт sniffer (Chromium/flaresolverr = 5-7 сек), значит плеер не
    # уходит в спиннер/треугльник. Для URL типа cdn.ntv.ru/*.m3u8?filter=
    # сам URL не протухает — старый payload продолжает работать.
    peek = state.peek_channel_stream(name)
    if peek is not None and peek[3]:
        is_direct, payload, expire_time, _ = peek
        logger.info(f"[STREAM] '{name}': serving stale payload, revalidate in background")
        threading.Thread(
            target=revalidate_channel_in_background,
            args=(ch,),
            daemon=True,
        ).start()
        # active_idx берём на момент peek — тот же, что вернул payload.
        # Если healthcheck переключит активный в фоне, /redirect всё равно
        # уже отдаёт payload по старому индексу.
        return _build_stream_response(name, request, is_direct, payload, expire_time, "stale", active_idx=active_idx)

    now = time.time()
    try:
        is_direct, payload, expire_time, method = await asyncio.wait_for(
            asyncio.to_thread(state.get_channel_stream, name),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        logger.error(f"[STREAM] '{name}': resolve timeout (>30s)")
        return _handle_stream_failure(name, active_idx, "Резолв превысил таймаут", now)
    except ValueError as ve:
        logger.error(f"[STREAM] {ve}")
        return Response(str(ve), status_code=404)
    except Exception as e:
        logger.error(f"[STREAM] '{name}': resolve error: {e}")
        if ch.get("fallback"):
            try:
                switched = await asyncio.to_thread(try_switch_to_healthy_stream, name)
            except Exception as e_fb:
                logger.error(f"[STREAM] '{name}': fallback error: {e_fb}")
                switched = False

            if switched:
                try:
                    is_direct, payload, expire_time, method = await asyncio.wait_for(
                        asyncio.to_thread(state.get_channel_stream, name),
                        timeout=30.0,
                    )
                    return _build_stream_response(name, request, is_direct, payload, expire_time, method, active_idx=active_idx)
                except Exception as e2:
                    logger.error(f"[STREAM] '{name}': re-resolve after fallback failed: {e2}")

        return _handle_stream_failure(name, active_idx, e, now)

    return _build_stream_response(name, request, is_direct, payload, expire_time, method, active_idx=active_idx)

@router.get("/m3u")
def get_m3u(request: Request):
    base_url = str(request.base_url).rstrip('/')
    lines = ["#EXTM3U"]
    for ch in state.load_channels():
        name = ch["name"]
        if ch.get("disable", False):
            continue
        display_name = ch.get("real_name", name) or name
        tvg_id = ch.get("tvgid")
        final_chno = ch.get("chno") or ""
        logo = ch.get("logo")
        group = ch.get("group")

        attrs = []
        if tvg_id:
            attrs.append(f'tvg-id="{tvg_id}"')
        if final_chno:
            attrs.append(f'tvg-chno="{final_chno}"')
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
        if group:
            attrs.append(f'group-title="{group}"')
        extinf = f'#EXTINF:-1 {" ".join(attrs)},{display_name}'
        lines.append(extinf)

        url_line = f'{base_url}/redirect/{quote(name)}.m3u8'
        logger.debug(f"[M3U] '{name}' -> {url_line}")
        lines.append(url_line)

    return Response(
        "\n".join(lines),
        media_type="audio/x-mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


def _override_master_bandwidth(manifest_text: str, bandwidth: int = 3250000) -> str:
    """Принудительно ставит BANDWIDTH в master-плейлист.

    master-bandwidth-fix-v1: только если STREAM-INF РОВНО ОДИН.
    Иначе все варианты качества получают одинаковый BANDWIDTH, и
    Jellyfin не может выбрать между ними — спотыкается.
    Media-плейлисты (только #EXTINF + .ts) не трогает.
    """
    if "#EXT-X-STREAM-INF" not in manifest_text:
        return manifest_text
    # Несколько STREAM-INF — не перезаписываем BANDWIDTH, даём Jellyfin выбор.
    if manifest_text.count("#EXT-X-STREAM-INF") > 1:
        return manifest_text
    out = []
    for line in manifest_text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF"):
            if "BANDWIDTH=" in line:
                line = re.sub(r"BANDWIDTH=\d+", f"BANDWIDTH={bandwidth}", line)
            else:
                line = line.replace("#EXT-X-STREAM-INF:",
                                    f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},", 1)
            if "AVERAGE-BANDWIDTH=" in line:
                line = re.sub(r"AVERAGE-BANDWIDTH=\d+",
                              f"AVERAGE-BANDWIDTH={bandwidth}", line)
        out.append(line)
    return "\n".join(out)

@router.get("/synthetic/{name}.m3u8")
def synthetic_manifest(name: str):
    state.cleanup_expired_caches()
    now = time.time()
    idx = state.get_active_index(name)
    with state.cache_lock:
        entry = state._epg_cache.get(name, {})
        if isinstance(entry, dict):
            streams = entry.get("streams_cache", [])
            if isinstance(idx, int) and 0 <= idx < len(streams) and isinstance(streams[idx], dict):
                s = streams[idx]
                payload = s.get("cached_stream")
                expire = s.get("cache_expire", 0)
                if payload and expire > now:
                    return Response(payload, media_type="application/vnd.apple.mpegurl")
    return Response("Not available, re-resolve channel first", status_code=404)

@router.head("/redirect/{name}")
def redirect_channel_head(name: str):
    return Response(status_code=200)

@router.get("/black.ts")
def serve_black_ts():
    path = "/app/db/black.ts"
    if not os.path.exists(path):
        return Response("Black TS not available", status_code=500)
    with open(path, "rb") as f:
        data = f.read()
    return Response(
        content=data,
        media_type="video/mp2t",
        headers={"Cache-Control": "public, max-age=86400"},
    )

@router.head("/black.ts")
def serve_black_ts_head():
    return Response(
        media_type="video/mp2t",
        headers={"Cache-Control": "public, max-age=86400"},
    )

# stream-refactor-v1
