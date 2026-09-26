from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
import urllib
import urllib.error
import os
import re
import time
import asyncio
import queue
import threading
from urllib.parse import quote, unquote, urljoin

import core.state as state
from core.config import IPTV_DEFAULT_UA, IPTV_FETCH_TIMEOUT, IPTV_FAILED_RESOLVE_TTL, logger
from services.resolver import parse_url_headers
from services.proxy_service import (
    _proxy_googlevideo_manifest, _build_redirect_response, fix_hls_manifest,
    needs_mux, fetch_via_flaresolverr, read_response_text, sanitize_channel,
)
from services.mux_service import get_or_create_mux
from services.fallback import try_switch_to_healthy_stream
from services.healthcheck import revalidate_channel_in_background
from services.events import save_cache_and_broadcast
from services.segment_prefetch import schedule_prefetch, get_cached_segment
from services.limits import flaresolverr_sem

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
BLACK_MANIFEST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-PLAYLIST-TYPE:VOD\n"
    "#EXT-X-TARGETDURATION:6\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n"
    "#EXTINF:5.0,\n"
    "/black.ts\n"
    "#EXT-X-ENDLIST\n"
)


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
            needs_mux_flag = _get_or_compute_needs_mux(name, payload, active_idx)
            if needs_mux_flag:
                logger.info(f"[STREAM] '{name}': mux selected, redirecting to /mux/{quote(name)}.ts")
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
    Jellyfin иначе использует дефолт ~20 Mbps и уходит в транскод.
    Media-плейлисты (только #EXTINF + .ts) не трогает.
    """
    if "#EXT-X-STREAM-INF" not in manifest_text:
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


@router.get("/hls/manifest.m3u8")
def proxy_hls_manifest(url: str, request: Request, referer: str = None, cookie: str = None, ua: str = None, channel: str = None):
    channel = sanitize_channel(channel, url)
    if channel:
        state.mark_channel_active(channel)
    try:
        target_url = unquote(url)
        headers = {"User-Agent": unquote(ua) if ua else IPTV_DEFAULT_UA}
        if referer:
            headers["Referer"] = unquote(referer)
        if cookie:
            headers["Cookie"] = unquote(cookie)

        req = urllib.request.Request(target_url, headers=headers)
        with urllib.request.urlopen(req, timeout=IPTV_FETCH_TIMEOUT) as resp:
            final_url = resp.geturl()
            content_type = resp.headers.get("Content-Type", "") or ""
            raw = resp.read()

        # Fast path: апстрим вернул бинарный TS-сегмент вместо манифеста.
        # Jellyfin иногда просит .ts через manifest-эндпоинт (своя логика
        # или устаревший кэш плеера). Отдаём байты как есть — декодировать
        # нельзя, errors='ignore' уничтожит не-ASCII содержимое видеопотока.
        if raw[:1] == b"G" and "html" not in content_type.lower():
            logger.info(
                f"[HLS-PROXY] [{channel or '?'}] non-HLS served as TS "
                f"(len={len(raw)}) target_url={target_url}"
            )
            return Response(content=raw, media_type="video/mp2t")

        content = _decode_body_gzip_aware(raw)

        if not content.strip().startswith("#EXTM3U"):
            match = re.search(r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', content)
            if match:
                real_m3u8_url = match.group(1)
                logger.info(f"[HLS-PROXY] [{channel or '?'}] extracted real m3u8: {real_m3u8_url}")
                current_url = urljoin(final_url, real_m3u8_url)
                req = urllib.request.Request(current_url, headers=headers)
                with urllib.request.urlopen(req, timeout=IPTV_FETCH_TIMEOUT) as resp:
                    final_url = resp.geturl()
                    content_type = resp.headers.get("Content-Type", "") or ""
                    raw2 = resp.read()

                if raw2[:1] == b"G" and "html" not in content_type.lower():
                    logger.info(
                        f"[HLS-PROXY] [{channel or '?'}] non-HLS after extract "
                        f"served as TS (len={len(raw2)}) target_url={target_url}"
                    )
                    return Response(content=raw2, media_type="video/mp2t")

                content = _decode_body_gzip_aware(raw2)

                if not content.strip().startswith("#EXTM3U"):
                    logger.warning(
                        f"[HLS-PROXY] [{channel or '?'}] non-HLS after extract "
                        f"(ct={content_type!r}, len={len(content)}, "
                        f"head={content[:100]!r}) target_url={target_url}"
                    )
                    return Response("Invalid HLS manifest", status_code=502)

            else:
                logger.warning(
                    f"[HLS-PROXY] [{channel or '?'}] upstream non-HLS "
                    f"(ct={content_type!r}, len={len(content)}, "
                    f"head={content[:100]!r}) target_url={target_url}"
                )
                return Response("Not an HLS manifest", status_code=502)

        _no_prefetch = request.query_params.get("no_prefetch") == "1" if request else False
        _prefetch_allowed = False
        if channel and not _no_prefetch:
            try:
                active_idx = state.get_active_index(channel)
                _ch = state.get_channel(channel)
                if _ch:
                    _streams = _ch.get("streams", [])
                    if 0 <= active_idx < len(_streams) and isinstance(_streams[active_idx], dict):
                        _prefetch_allowed = bool(_streams[active_idx].get("prefetch", False))
            except Exception:
                pass

        try:
            from core.config import IPTV_PREFETCH_MAX_SEGMENTS
            segment_urls = []
            for _line in content.splitlines():
                _s = _line.strip()
                if not _s or _s.startswith("#"):
                    continue
                if ".m3u8" in _s.lower():
                    continue
                segment_urls.append(urljoin(final_url, _s))
            if segment_urls:
                segment_urls = segment_urls[-IPTV_PREFETCH_MAX_SEGMENTS:]
            if segment_urls and _prefetch_allowed:
                _pf_headers = {"User-Agent": unquote(ua) if ua else IPTV_DEFAULT_UA}
                if referer:
                    _pf_headers["Referer"] = unquote(referer)
                if cookie:
                    _pf_headers["Cookie"] = unquote(cookie)
                schedule_prefetch(segment_urls, _pf_headers, channel=channel)
        except Exception as _e:
            logger.warning(f"[PREFETCH] schedule failed: {_e}")

        proxy_base = str(request.base_url).rstrip('/')
        modified_m3u8 = fix_hls_manifest(
            manifest_text=content,
            base_url=final_url,
            referer=referer,
            cookie=cookie,
            ua=ua,
            proxy_base_url=proxy_base,
            channel=channel
        )
        modified_m3u8 = _override_master_bandwidth(modified_m3u8)
        logger.debug(f"[HLS-PROXY] [{channel or '?'}] manifest proxied successfully: {target_url[:120]}")
        _body = modified_m3u8.encode("utf-8")
        return Response(
            content=_body,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Content-Length": str(len(_body)),
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )
    except urllib.error.HTTPError as e:
        logger.error(f"[HLS-PROXY] [{channel or '?'}] HTTP {e.code}: {e.reason} (url={url[:120]})")
        return Response(f"Error fetching manifest: {e.code} {e.reason}", status_code=502)
    except Exception as e:
        logger.error(f"[HLS-PROXY] [{channel or '?'}] proxying failed ({url[:120]}): {e}")
        return Response(f"Error proxying playlist: {e}", status_code=502)


@router.get("/hls/segment.ts")
def proxy_hls_segment(
    url: str,
    request: Request,
    referer: str = None,
    cookie: str = None,
    ua: str = None,
    channel: str = None,
):
    channel = sanitize_channel(channel, url)
    if channel:
        state.mark_channel_active(channel)
    try:
        if cookie in ("None", "null", ""): cookie = None
        if ua in ("None", "null", ""): ua = None
        if referer in ("None", "null", ""): referer = None
        target_url = unquote(url)
        try:
            cached = get_cached_segment(target_url)
            if cached is not None:
                logger.debug(
                    f"[TS-PROXY] [{channel or '?'}] {len(cached)} bytes (prefetch): "
                    f"{target_url[:100]}"
                )
                return Response(content=cached, media_type="video/mp2t")
        except Exception as e:
            logger.warning(f"[PREFETCH] cache read error: {e}")

        headers = {"User-Agent": unquote(ua) if ua else IPTV_DEFAULT_UA}
        if referer:
            headers["Referer"] = unquote(referer)
        if cookie:
            headers["Cookie"] = unquote(cookie)

        client_range = request.headers.get("range") if request else None
        if client_range:
            headers["Range"] = client_range

        t0 = time.time()

        try:
            req = urllib.request.Request(target_url, headers=headers)
            with urllib.request.urlopen(req, timeout=IPTV_FETCH_TIMEOUT) as resp:
                status_code = resp.status
                resp_headers = resp.headers
                try:
                    data = resp.read()
                except Exception as _read_err:
                    partial = getattr(_read_err, "partial", None)
                    if partial:
                        logger.info(
                            f"[TS-PROXY] [{channel or '?'}] incomplete read: "
                            f"got {len(partial)} bytes, fetching remainder via Range"
                        )
                        data = partial
                        # Дозапрашиваем остаток через Range. CDN tvcdnpotok
                        # часто обрывает соединение на середине сегмента.
                        # Без докачки Jellyfin получает обрезанный TS и
                        # буферизует каждые 10 сек.
                        try:
                            h2 = dict(headers)
                            h2["Range"] = f"bytes={len(data)}-"
                            req2 = urllib.request.Request(target_url, headers=h2)
                            with urllib.request.urlopen(req2, timeout=IPTV_FETCH_TIMEOUT) as resp2:
                                try:
                                    data += resp2.read()
                                except Exception as _read_err2:
                                    partial2 = getattr(_read_err2, "partial", None)
                                    if partial2:
                                        data += partial2
                                    # второй обрыв — не критично, отдаём что есть
                            logger.info(
                                f"[TS-PROXY] [{channel or '?'}] remainder fetched, "
                                f"total {len(data)} bytes"
                            )
                        except Exception as _rng_err:
                            logger.warning(
                                f"[TS-PROXY] [{channel or '?'}] Range fetch failed: "
                                f"{_rng_err}, serving partial {len(data)} bytes"
                            )
                    else:
                        raise
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                logger.info(f"[TS-PROXY] [{channel or '?'}] direct access {e.code}, FlareSolverr: {target_url[:120]}")
                with flaresolverr_sem:
                    raw = fetch_via_flaresolverr(target_url, headers, IPTV_FETCH_TIMEOUT)
                elapsed = time.time() - t0
                logger.info(f"[TS-PROXY] [{channel or '?'}] FlareSolverr: {len(raw)} bytes in {elapsed:.2f}s")
                return Response(content=raw, media_type="video/mp2t")
            logger.error(f"[TS-PROXY] [{channel or '?'}] HTTP {e.code}: {target_url[:120]}")
            return Response(f"Upstream error: {e.code}", status_code=502)
        except Exception as e:
            logger.error(f"[TS-PROXY] [{channel or '?'}] {type(e).__name__}: {e} ({target_url[:120]})")
            return Response(f"Upstream error: {e}", status_code=502)

        elapsed = time.time() - t0
        content_type = resp_headers.get("Content-Type", "video/mp2t") or "video/mp2t"

        response_headers = {}
        if resp_headers.get("Content-Range"):
            response_headers["Content-Range"] = resp_headers["Content-Range"]
        if resp_headers.get("Accept-Ranges"):
            response_headers["Accept-Ranges"] = resp_headers["Accept-Ranges"]

        logger.debug(
            f"[TS-PROXY] [{channel or '?'}] {len(data)} bytes, status={status_code}, "
            f"in {elapsed:.2f}s: {target_url[:100]}"
        )
        response_headers["Content-Length"] = str(len(data))
        return Response(
            content=data,
            media_type=content_type,
            status_code=status_code,
            headers=response_headers,
        )
    except Exception as e:
        logger.error(f"[TS-PROXY] [{channel or '?'}] {type(e).__name__}: {e} ({url[:100]})")
        return Response(f"Error: {e}", status_code=502)


@router.get("/mux/{name}.ts")
async def mux_stream(name: str, request: Request):
    logger.info(f"[MUX] '{name}': mux requested")
    state.mark_channel_active(name)

    cached_stream = None
    idx = state.get_active_index(name)
    with state.cache_lock:
        entry = state._epg_cache.get(name, {})
        if isinstance(entry, dict):
            streams = entry.get("streams_cache", [])
            if isinstance(idx, int) and 0 <= idx < len(streams) and isinstance(streams[idx], dict):
                cached_stream = streams[idx].get("cached_stream")

    if not cached_stream:
        logger.warning(f"[MUX] '{name}': cache empty, re-resolving")
        try:
            is_direct, payload, expire_time, method = await asyncio.wait_for(
                asyncio.to_thread(state.get_channel_stream, name),
                timeout=30.0,
            )
            cached_stream = payload
            logger.info(f"[MUX] '{name}': re-resolved, {cached_stream[:80]}...")
        except asyncio.TimeoutError:
            logger.error(f"[MUX] '{name}': resolve timeout (>30s)")
            return Response("Resolve timeout", status_code=504)
        except Exception as e:
            logger.error(f"[MUX] '{name}': re-resolve failed: {e}")
            return Response("No cached stream", status_code=404)

    if cached_stream.startswith("#EXTM3U"):
        lines = cached_stream.splitlines()
        video_url = None
        audio_url = None
        for line in lines:
            if line.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line:
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    audio_url = m.group(1)
            elif not line.startswith("#") and line.strip() and video_url is None:
                video_url = line.strip()
        if not video_url or not audio_url:
            return Response("Cannot parse synthetic manifest", status_code=404)

        logger.info(f"[MUX] '{name}': synthetic manifest, video={video_url[:80]}, audio={audio_url[:80]}")
        mux_proc = get_or_create_mux(name, video_url, audio_url, IPTV_DEFAULT_UA)
    else:
        clean_url, headers_dict = parse_url_headers(cached_stream)
        if ".m3u8" not in clean_url:
            return Response("Not an HLS stream", status_code=400)

        referer_str = headers_dict.get("Referer", "") if isinstance(headers_dict, dict) else ""
        cookie_str = headers_dict.get("Cookie", "") if isinstance(headers_dict, dict) else ""
        user_agent = headers_dict.get("User-Agent", IPTV_DEFAULT_UA) if isinstance(headers_dict, dict) else IPTV_DEFAULT_UA

        headers = {"User-Agent": user_agent}
        if referer_str:
            headers["Referer"] = referer_str
        if cookie_str:
            headers["Cookie"] = cookie_str

        try:
            req = urllib.request.Request(clean_url, headers=headers)
            with urllib.request.urlopen(req, timeout=IPTV_FETCH_TIMEOUT) as resp:
                manifest_text = read_response_text(resp)
        except Exception as e:
            logger.error(f"[MUX] '{name}': master.m3u8 load error: {e}")
            return Response("Failed to load master manifest", status_code=404)

        lines = manifest_text.splitlines()
        video_url = None
        audio_url = None
        max_bw = -1

        for line in lines:
            if line.strip().startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line:
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    audio_url = urljoin(clean_url, m.group(1))
                    break

        for i, line in enumerate(lines):
            if line.strip().startswith("#EXT-X-STREAM-INF"):
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                if bw_match:
                    bw = int(bw_match.group(1))
                    if bw > max_bw and i + 1 < len(lines):
                        candidate = lines[i+1].strip()
                        if not candidate.startswith("#"):
                            max_bw = bw
                            video_url = urljoin(clean_url, candidate)

        if not video_url or not audio_url:
            return Response("Cannot find video/audio in master", status_code=404)

        logger.info(f"[MUX] '{name}': video={video_url[:80]}, audio={audio_url[:80]}")
        mux_proc = get_or_create_mux(name, video_url, audio_url, user_agent, referer_str, cookie_str)

    q = mux_proc.subscribe()

    async def stream_generator():
        empty_count = 0
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(q.get, timeout=1.0)
                    empty_count = 0
                except queue.Empty:
                    empty_count += 1
                    if empty_count >= 30:
                        logger.warning(f"[MUX] '{name}': no data for 30s, closing stream")
                        break
                    continue
                if chunk is None:
                    break
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            mux_proc.unsubscribe(q)

    return StreamingResponse(
        stream_generator(),
        media_type="video/mp2t",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


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
