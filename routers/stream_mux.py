"""
routers/stream_mux.py — A/V-мукс.

После stream-refactor-v1: /mux/{name}.ts.
"""
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
import asyncio
import re
import time
import urllib.error
import urllib.request

import core.state as state
from core.config import IPTV_DEFAULT_UA, IPTV_FETCH_TIMEOUT, IPTV_RESOLVE_TIMEOUT, logger
from services.resolver import parse_url_headers
from services.hls_utils import select_playable, probe_stream_types
from services.proxy_service import (
    read_response_text, stale_gate_remaining, mark_payload_dead, mark_cdn_error,
)
from services.mux_service import get_live_mux, get_or_create_mux

# mux-variant-av-probe-v1: помним для канала, несёт ли выбранный вариант аудио.
# По объявленным CODECS в master'е судить нельзя (бывает и наоборот), поэтому
# проверяем сам вариант один раз на канал.
_variant_av_cache = {}

router = APIRouter()

# mux-black-fallback-v1: сколько ждать первый байт от мукса, прежде чем
# отдать чёрный поток. Источник по HLS отдаёт данные пачками (по границам
# сегментов, у некоторых вещателей сегмент ~10 секунд), поэтому пауза в
# несколько секунд — норма, а не поломка. 12 секунд покрывают холодный старт
# ffmpeg и один сегмент живого плейлиста; чёрный остаётся крайней мерой.
_MUX_FIRST_DATA_WAIT = 12.0

# mux-seed-wait-v1: сколько ждать готовую затравку (выровненную по ключевому
# кадру) перед началом отдачи. Пик — холодный старт ffmpeg (1-3 с), но
# ограничиваем: лучше отдать живой поток, чем держать клиент без ответа.
_MUX_SEED_WAIT = 10.0

@router.get("/mux/{name}.ts")
async def mux_stream(name: str, request: Request):
    logger.info(f"[MUX] '{name}': mux requested")
    state.mark_channel_active(name)

    # stale-gate-v1: пока идёт переразбор протухшей ссылки — отвечаем 503 сразу,
    # без обращения к CDN. Иначе ретраи клиента (раз в ~100 мс) превращаются в
    # шторм запросов по мёртвой ссылке, а плеер висит на логотипе.
    gate = stale_gate_remaining(name)
    if gate > 0:
        # mux-black-fallback-v1: раньше здесь был 503 «Re-resolving stream» —
        # Jellyfin на него отвечает мгновенной ошибкой воспроизведения, а
        # телевизор висит на логотипе. Отдаём валидный чёрный поток: клиент
        # играет 5 секунд чёрного и сам перезапрашивает канал, подхватывая
        # живой поток, как только он появится.
        logger.info(f"[MUX] '{name}': stale gate active ({gate:.1f}s) — отдаю black.ts")
        return RedirectResponse(url=f"{str(request.base_url).rstrip('/')}/black.ts", status_code=302)

    mux_proc = get_live_mux(name)
    if mux_proc is not None:
        # ffmpeg уже читает свои ранее разобранные URL — master не нужен, и его
        # протухание нам больше не мешает.
        logger.info(f"[MUX] '{name}': live mux reuse, master fetch skipped")
    else:
        mux_proc = None
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
                    timeout=float(IPTV_RESOLVE_TIMEOUT),
                )
                cached_stream = payload
                logger.info(f"[MUX] '{name}': re-resolved, {cached_stream[:80]}...")
            except asyncio.TimeoutError:
                logger.error(f"[MUX] '{name}': resolve timeout (>{IPTV_RESOLVE_TIMEOUT}s)")
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
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404):
                    # CDN отверг подписанную ссылку: payload протух раньше, чем
                    # истёк cache_expire. Слот чистим, канал заказываем на
                    # переразбор, клиенту — 503 с Retry-After (вместо 404, на
                    # который Jellyfin отвечает мгновенными ретраями).
                    logger.error(f"[MUX] '{name}': master rejected (HTTP {e.code}) -> payload stale")
                    mark_payload_dead(name)
                    return Response("Stale payload, re-resolving", status_code=503,
                                    headers={"Retry-After": "5"})
                logger.error(f"[MUX] '{name}': master.m3u8 load error: {e}")
                mark_cdn_error(name)
                return Response("Failed to load master manifest", status_code=503,
                                headers={"Retry-After": "3"})
            except Exception as e:
                logger.error(f"[MUX] '{name}': master.m3u8 load error: {e}")
                mark_cdn_error(name)
                return Response("Failed to load master manifest", status_code=503,
                                headers={"Retry-After": "3"})

            if "#EXTM3U" not in manifest_text:
                # payload не плейлист (HTML-заглушка, страница ошибки): в мукс
                # такое отдавать нельзя
                logger.error(f"[MUX] '{name}': payload не плейлист (нет #EXTM3U)")
                mark_cdn_error(name)
                return Response("Not a playlist", status_code=502,
                                headers={"Retry-After": "5"})

            # Общее правило выбора («что реально играет») живёт в hls_utils,
            # чтобы мукс, проба и сниффер не расходились версиями логики.
            pick = select_playable(manifest_text, clean_url)
            if pick.get("kind") == "media":
                # mux-media-payload-v2: payload сам является медиа-плейлистом —
                # A+V уже внутри, значит single-input.
                video_url = clean_url
                logger.info(f"[MUX] '{name}': media-as-payload, single-input (url={clean_url[:80]})")
                mux_proc = get_or_create_mux(name, video_url, None, user_agent, referer_str, cookie_str)
            else:
                video_url = pick.get("video")
                audio_url = pick.get("audio")
                if not video_url:
                    return Response("Cannot find video in master", status_code=404)

                # mux-single-input-av-v1: если вариант сам несёт аудио, берём его
                # ОДНИМ входом и не тянем второй источник. Причина: два входа с
                # -copyts дают TS, который начинается не с ключевого кадра, и
                # клиент при probe не определяет параметры видео и ремуксит
                # только аудио (картинки нет). Один вход стартует с границы
                # сегмента = с IDR, параметры видны сразу.
                # Решаем не по объявленным CODECS (они врут в обе стороны), а
                # проверкой самого варианта — один раз на канал.
                if audio_url:
                    if name not in _variant_av_cache:
                        _types = probe_stream_types(video_url, ua=user_agent,
                                                    referer=referer_str, cookie=cookie_str)
                        _variant_av_cache[name] = "audio" in _types
                        logger.info(
                            f"[MUX] '{name}': probe варианта: аудио в нём "
                            f"{'есть → single-input' if _variant_av_cache[name] else 'нет → два входа'}"
                        )
                    if _variant_av_cache[name]:
                        audio_url = None

                logger.info(f"[MUX] '{name}': master, video={video_url[:80]}, audio={(audio_url or '(none)')[:80]}")
                mux_proc = get_or_create_mux(name, video_url, audio_url, user_agent, referer_str, cookie_str)

    # q инициализируем None: если subscribe() упадёт, finally не сломается
    # на NameError, а корректно пропустит unsubscribe.
    q = None
    # mux-seed-wait-v1: сначала дожидаемся готовой затравки (заголовок потока
    # от последнего ключевого кадра). Без неё первый зритель входит в середину
    # GOP: за своё прощупывание (Jellyfin ~3 с / 1.5 МБ) он не находит ни
    # одного ключевого кадра и показывает «вечный логотип» / ошибку
    # воспроизведения. Так же ведёт себя обычный плеер: он ждёт начало GOP.
    try:
        deadline = time.monotonic() + _MUX_SEED_WAIT
        while not mux_proc.gop_seed_ready() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if not mux_proc.gop_seed_ready():
            logger.info(f"[MUX] '{name}': затравка не готова за {_MUX_SEED_WAIT:.0f} с — отдаю живой поток")
    except Exception as _e:
        logger.warning(f"[MUX] '{name}': ожидание затравки сорвалось: {_e}")

    q = mux_proc.subscribe()

    # mux-black-fallback-v1: не держим зрителя молча. Ждём первый байт до
    # _MUX_FIRST_DATA_WAIT секунд: HLS-источник отдаёт данные пачками (по
    # границам сегментов), поэтому «нет данных» 1-2 секунды — норма, а не
    # поломка. Если данных нет дольше (мукс мёртв/холодный вход) — отдаём
    # чёрный поток: Jellyfin проиграет 5 секунд чёрного и сам перезапросит
    # канал, подхватив живой поток.
    try:
        first_chunk = await asyncio.wait_for(q.get(), timeout=_MUX_FIRST_DATA_WAIT)
    except asyncio.TimeoutError:
        if q is not None:
            mux_proc.unsubscribe(q)
        logger.warning(f"[MUX] '{name}': нет данных за {_MUX_FIRST_DATA_WAIT:.0f} с — отдаю black.ts")
        return RedirectResponse(url=f"{str(request.base_url).rstrip('/')}/black.ts", status_code=302)

    async def stream_generator():
        # MUX-BATCH-READ: q — asyncio.Queue, читаем напрямую через await q.get(),
        # без thread-hop. После первого чанка забираем всё, что уже накопилось
        # (get_nowait), чтобы уменьшить число yield-переключений event loop.
        empty_count = 0
        pending = [first_chunk]
        try:
            while True:
                batch = pending
                pending = []
                if not batch:
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
