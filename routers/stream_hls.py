"""
routers/stream_hls.py — HLS-прокси.

После stream-refactor-v1: /hls/manifest.m3u8, /hls/{name}.{ext},
/hls/segment.ts (+ HEAD).
"""
from fastapi import APIRouter, Request, Response
import gzip as _gz
import re
import time
import urllib.request
import urllib.error
from urllib.parse import unquote, urljoin

import core.state as state
from core.config import IPTV_DEFAULT_UA, IPTV_FETCH_TIMEOUT, logger
from services.proxy_service import fix_hls_manifest, sanitize_channel
from services.segment_prefetch import schedule_prefetch, get_cached_segment

router = APIRouter()


def _override_master_bandwidth(manifest_text: str, bandwidth: int = 3250000) -> str:
    """Принудительно ставит BANDWIDTH в master-плейлист.

    master-bandwidth-fix-v1: только если STREAM-INF РОВНО ОДИН.
    Иначе все варианты качества получают одинаковый BANDWIDTH, и
    Jellyfin не может выбрать между ними — спотыкается.
    Media-плейлисты (только #EXTINF + .ts) не трогает.
    """
    if "#EXT-X-STREAM-INF" not in manifest_text:
        return manifest_text
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


def _decode_body_gzip_aware(raw: bytes) -> str:
    """Декодирует тело HTTP-ответа. Если body начинается с gzip magic-bytes
    (0x1f 0x8b) — распаковывает. Некоторые CDN (ntv.ru) отдают gzip для
    вложенных m3u8-манифестов без явного Content-Encoding: gzip.
    TS fast-path вызывается ДО этой функции и не затрагивается.
    """
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = _gz.decompress(raw)
        except Exception as _e:
            logger.warning(f"[HLS-PROXY] gzip decompress failed: {_e}")
    return raw.decode("utf-8", errors="ignore")


# segment-head-key-v1
def _content_type_for_segment(target_url: str) -> str:
    """Content-Type по расширению target URL.
    Ключ AES-128 (.key) — 16 бинарных байт. ffmpeg ожидает
    application/octet-stream. video/mp2t для ключа ломает парсер.
    """
    u = target_url.lower().split("?")[0]
    if u.endswith(".key"):
        return "application/octet-stream"
    if u.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if u.endswith((".ts", ".m4s", ".mp4", ".m4a", ".aac", ".ac3", ".vtt")):
        return "video/mp2t"
    if u.endswith((".mpd", ".key", ".bin")):
        return "application/octet-stream"
    return "video/mp2t"


_MANIFEST_EXTS = ("m3u8", "mpd")
_BINARY_EXTS = ("ts", "m4s", "mp4", "m4a", "aac", "ac3", "vtt", "key", "bin")



@router.get("/hls/manifest.m3u8")
def proxy_hls_manifest(url: str, request: Request, referer: str = None, cookie: str = None, ua: str = None, channel: str = None):
    channel = sanitize_channel(channel, url)
    if channel:
        state.mark_channel_active(channel)
    try:
        # FastAPI уже декодировал query-параметр. Повторный unquote()
        # ломает percent-encoding внутри URL: %3A → :, %2B → +.
        # CDN ожидает исходные %3A/%2B в startdate= — с `+` он читает
        # как пробел и отдаёт 404.
        target_url = url
        # unquote-fix-v1: значения уже декодированы FastAPI.
        headers = {"User-Agent": ua if ua else IPTV_DEFAULT_UA}
        if referer:
            headers["Referer"] = referer
        if cookie:
            headers["Cookie"] = cookie

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


# hls-ext-paths-v1
# Универсальный роут: /hls/{name}.{ext}?url=...&referer=...&cookie=...&ua=...
# ext определяет Content-Type и поведение (manifest или binary).
# Старые /hls/manifest.m3u8 и /hls/segment.ts остаются ниже — для кэша.

_MANIFEST_EXTS = ("m3u8", "mpd")
_BINARY_EXTS = ("ts", "m4s", "mp4", "m4a", "aac", "ac3", "vtt", "key", "bin")

@router.head("/hls/{name}.{ext}")
def proxy_hls_by_ext_head(name: str, ext: str, url: str, request: Request,
                          referer: str = None, cookie: str = None,
                          ua: str = None, channel: str = None):
    ext_l = ext.lower()
    if ext_l == "key":
        ct = "application/octet-stream"
    elif ext_l == "vtt":
        ct = "text/vtt"
    elif ext_l in _MANIFEST_EXTS:
        ct = "application/vnd.apple.mpegurl"
    elif ext_l in _BINARY_EXTS:
        ct = "video/mp2t"
    else:
        ct = "application/octet-stream"
    return Response(status_code=200, media_type=ct,
                    headers={"Accept-Ranges": "bytes"})

@router.get("/hls/{name}.{ext}")
def proxy_hls_by_ext(name: str, ext: str, url: str, request: Request,
                     referer: str = None, cookie: str = None,
                     ua: str = None, channel: str = None):
    """Универсальный HLS-прокси. ext в пути — правильное расширение URL.

    Для .m3u8/.mpd — проксируем манифест (переписываем вложенные URL).
    Для .ts/.key/.vtt/.m4s/.mp4 — отдаём бинарь как есть.
    """
    if not channel:
        channel = name
    ext_l = ext.lower()
    if ext_l in _MANIFEST_EXTS:
        return proxy_hls_manifest(url=url, request=request, referer=referer,
                                  cookie=cookie, ua=ua, channel=channel)
    # бинарь: key/vtt/segment
    return proxy_hls_segment(url=url, request=request, referer=referer,
                             cookie=cookie, ua=ua, channel=channel)

@router.head("/hls/segment.ts")
def proxy_hls_segment_head(url: str, request: Request,
                          referer: str = None, cookie: str = None,
                          ua: str = None, channel: str = None):
    """HEAD для /hls/segment.ts.

    ffmpeg/ffprobe делают HEAD перед GET для определения размера и
    типа. Раньше возвращали 405, и ffmpeg отказывался открывать ключ.
    Отвечаем 200 без тела — с корректным Content-Type по расширению.
    """
    # url — сырой query-параметр (FastAPI декодировал). Проверим только
    # расширение, содержимое не качаем.
    target_url = url
    ct = _content_type_for_segment(target_url)
    return Response(
        status_code=200,
        media_type=ct,
        headers={"Accept-Ranges": "bytes"},
    )

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
        # unquote-fix-v1: FastAPI уже декодировал query-параметр.
        # Повторный unquote ломает %2B -> '+' -> ' ' (пробел), CDN 404.
        target_url = url
        # segment-head-key-v1: Content-Type по расширению. Для .key —
        # application/octet-stream, иначе ffmpeg не распарсит ключ AES-128.
        _ct = _content_type_for_segment(target_url)
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
            # remove-flare-from-segments-v1:
            # FlareSolverr убран из прокси сегментов/ключей. Он возвращает
            # solution.response как UTF-8 строку — бинарные TS и ключи
            # портятся. Плюс запускает Chromium на каждый сегмент.
            # Уместен только в резолвере (поиск m3u8 на HTML-странице).
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
        # segment-head-key-v1: если upstream отдал text/plain или пусто —
        # не доверяем ему, ставим свой Content-Type по расширению.
        # Для .key upstream часто отдаёт application/octet-stream — тогда
        # берём его. Иначе — наш _ct.
        # force-key-ct-v1: для .key ВСЕГДА наш application/octet-stream.
        # Pluto (и, возможно, другие) отдаёт для ключа Content-Type
        # application/vnd.apple.mpegurl — это неверно, ffmpeg ломается.
        _is_key = target_url.lower().split("?")[0].endswith(".key")
        if _is_key:
            _final_ct = "application/octet-stream"
        else:
            _upstream_ct = content_type or ""
            if _upstream_ct.startswith("video/") or _upstream_ct.startswith("application/"):
                _final_ct = _upstream_ct
            else:
                _final_ct = _ct
        return Response(
            content=data,
            media_type=_final_ct,
            status_code=status_code,
            headers=response_headers,
        )
    except Exception as e:
        logger.error(f"[TS-PROXY] [{channel or '?'}] {type(e).__name__}: {e} ({url[:100]})")
        return Response(f"Error: {e}", status_code=502)

# stream-refactor-v1
