"""
routers/stream_mux.py — A/V-мукс.

После stream-refactor-v1: /mux/{name}.ts.
"""
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
import asyncio
import re
import urllib.request
from urllib.parse import urljoin

import core.state as state
from core.config import IPTV_DEFAULT_UA, IPTV_FETCH_TIMEOUT, logger
from services.resolver import parse_url_headers
from services.proxy_service import read_response_text
from services.mux_service import get_or_create_mux

router = APIRouter()

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

        # mux-media-payload-v2: payload может быть media (не master).
        # Если STREAM-INF нет, но есть EXTINF — это media, значит
        # video_url = clean_url (сам payload), audio_url = None
        # (single-input: A+V уже внутри media).
        has_stream_inf = any(
            line.strip().startswith("#EXT-X-STREAM-INF")
            for line in lines
        )
        has_extinf = any(
            line.strip().startswith("#EXTINF")
            for line in lines
        )

        if not has_stream_inf and has_extinf:
            video_url = clean_url
            logger.info(f"[MUX] '{name}': media-as-payload, single-input (url={clean_url[:80]})")
            mux_proc = get_or_create_mux(name, video_url, None, user_agent, referer_str, cookie_str)
        else:
            # master: ищем AUDIO-группу и STREAM-INF (как раньше).
            has_audio_group = any(
                line.strip().startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in line
                for line in lines
            )
            if has_audio_group:
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

            if not video_url:
                return Response("Cannot find video in master", status_code=404)

            logger.info(f"[MUX] '{name}': master, video={video_url[:80]}, audio={(audio_url or '(none)')[:80]}")
            mux_proc = get_or_create_mux(name, video_url, audio_url, user_agent, referer_str, cookie_str)

    # q инициализируем None: если subscribe() упадёт, finally не сломается
    # на NameError, а корректно пропустит unsubscribe.
    q = None
    q = mux_proc.subscribe()

    async def stream_generator():
        # MUX-BATCH-READ: q — asyncio.Queue, читаем напрямую через await q.get(),
        # без thread-hop. После первого чанка забираем всё, что уже накопилось
        # (get_nowait), чтобы уменьшить число yield-переключений event loop.
        empty_count = 0
        try:
            while True:
                batch = []
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=1.0)
                    batch.append(chunk)
                    # Добираем всё, что уже в очереди, без блокировки
                    while True:
                        try:
                            nxt = q.get_nowait()
                            batch.append(nxt)
                        except asyncio.QueueEmpty:
                            break
                    empty_count = 0
                except asyncio.TimeoutError:
                    empty_count += 1
                    if empty_count >= 30:
                        logger.warning(f"[MUX] '{name}': no data for 30s, closing stream")
                        break
                    continue

                stop = False
                for chunk in batch:
                    if chunk is None:
                        stop = True
                        break
                    yield chunk
                if stop:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if q is not None:
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

# stream-refactor-v1
