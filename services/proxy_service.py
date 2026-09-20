import re
import urllib.request
from urllib.parse import quote, urljoin
from typing import Optional
from fastapi import Response
from fastapi.responses import RedirectResponse
import gzip

from core.config import logger, IPTV_MANAGE_URL, IPTV_DEFAULT_UA, IPTV_FETCH_TIMEOUT, IPTV_FLARESOLVERR_URL
from services.resolver import parse_url_headers

def read_response_text(resp):
    """Читает тело HTTP-ответа, при необходимости распаковывая gzip."""
    if resp.headers.get('Content-Encoding') == 'gzip':
        return gzip.GzipFile(fileobj=resp).read().decode('utf-8', errors='ignore')
    else:
        return resp.read().decode('utf-8', errors='ignore')

def needs_mux(payload: str, channel_name: str = None) -> bool:
    tag = channel_name or "?"
    if payload.startswith("#EXTM3U"):
        result = "#EXT-X-MEDIA:TYPE=AUDIO" in payload
        logger.info(f"[MUX] '{tag}': synthetic payload, result={result}")
        return result

    clean_url, headers_dict = parse_url_headers(payload)
    if ".m3u8" not in clean_url.lower():
        logger.info(f"[MUX] '{tag}': not .m3u8, result=False")
        return False

    try:
        headers = {"User-Agent": headers_dict.get("User-Agent", IPTV_DEFAULT_UA)}
        if "Referer" in headers_dict:
            headers["Referer"] = headers_dict["Referer"]
        if "Cookie" in headers_dict:
            headers["Cookie"] = headers_dict["Cookie"]

        logger.info(
            f"[MUX] '{tag}': GET {clean_url[:120]} "
            f"(has_ref={'Referer' in headers_dict}, has_cookie={'Cookie' in headers_dict})"
        )
        req = urllib.request.Request(clean_url, headers=headers)
        with urllib.request.urlopen(req, timeout=IPTV_FETCH_TIMEOUT) as resp:
            content = read_response_text(resp)
        has_audio = "#EXT-X-MEDIA:TYPE=AUDIO" in content
        head = content[:80].replace("\n", " ").replace("\r", " ")
        logger.info(
            f"[MUX] '{tag}': http_ok, len={len(content)}, "
            f"starts_with_EXTM3U={content.lstrip().startswith('#EXTM3U')}, "
            f"has_AUDIO={has_audio}, head={head!r}"
        )
        return has_audio
    except Exception as e:
        if channel_name:
            logger.warning(f"[MUX] '{channel_name}': master playlist check failed: {e}")
        else:
            logger.warning(f"[MUX] master playlist check failed: {e}")
        return True

def _proxy_googlevideo_manifest(payload: str, client_ua: str) -> Optional[Response]:
    clean_url, _ = parse_url_headers(payload)
    headers = {
        "User-Agent": client_ua or IPTV_DEFAULT_UA,
        "Referer": "https://www.youtube.com/",
        "Origin": "https://www.youtube.com"
    }
    try:
        req = urllib.request.Request(clean_url, headers=headers)
        with urllib.request.urlopen(req, timeout=IPTV_FETCH_TIMEOUT) as resp:
            content = read_response_text(resp)
        return Response(
            content=content,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    except Exception as e:
        logger.error(f"[PROXY-GOOGLE] manifest proxy error: {e}")
        return None

def _build_redirect_response(payload: str, channel: str = None):
    clean_url, headers_dict = parse_url_headers(payload)

    referer = headers_dict.get("Referer", "") if isinstance(headers_dict, dict) else ""
    cookie = headers_dict.get("Cookie", "") if isinstance(headers_dict, dict) else ""
    ua = headers_dict.get("User-Agent", "") if isinstance(headers_dict, dict) else ""

    # Единственный критерий direct vs proxy — наличие заголовков, которые
    # резолвер захватил вместе с URL.
    #
    # Резолвер прикрепляет Referer/Cookie тогда и только тогда, когда
    # источник без них не отдаёт контент (Cloudflare-сессия, hotlink
    # protection, auth-token). В этом случае мы ОБЯЗАНЫ проксировать:
    # Jellyfin, следуя по 302, теряет заголовки на вложенных запросах
    # (chunklist, сегменты), и поток зависает.
    #
    # Если заголовков нет — это публичный CDN, отдаём чистый 302.
    # Direct предпочтительнее: прокси добавляет узкое место и лишний хоп,
    # но при наличии заголовков выбора нет.
    #
    # НЕ добавлять сюда списки домен-маркеров и эвристики "похоже на
    # player-wrapper". Резолвер уже знает ответ по факту — прикрепил
    # заголовки или нет.
    if not (referer or cookie):
        logger.info(f"[PROXY] direct redirect (no headers): {clean_url[:120]}")
        return RedirectResponse(url=clean_url, status_code=302)

    logger.info(f"[PROXY] HLS proxy (resolver headers): {clean_url[:120]}")
    proxy_url = f"/hls/manifest.m3u8?url={quote(clean_url, safe='')}"
    if referer:
        proxy_url += f"&referer={quote(referer, safe='')}"
    if cookie:
        proxy_url += f"&cookie={quote(cookie, safe='')}"
    if ua:
        proxy_url += f"&ua={quote(ua, safe='')}"
    if channel:
        proxy_url += f"&channel={quote(channel, safe='')}"
    return RedirectResponse(url=proxy_url, status_code=302)

def fix_hls_manifest(manifest_text: str, base_url: str, referer: str = None, cookie: str = None, ua: str = None, proxy_base_url: str = None, channel: str = None) -> str:
    lines = manifest_text.splitlines()
    new_lines = []
    params = ""
    # sanitize_channel — защита от мусорного имени канала, попавшего
    # из старого fallback'а. Без неё channel приклеится ко всем
    # ссылкам манифеста и раздует URL на десятки кБ.
    # В stream.py sanitize уже сделан, но fix_hls_manifest может
    # вызываться и в обход (probe, будущие места).
    channel = sanitize_channel(channel, base_url)
    if referer:
        params += f"&referer={quote(referer, safe='')}"
    if cookie:
        params += f"&cookie={quote(cookie, safe='')}"
    if ua:
        params += f"&ua={quote(ua, safe='')}"
    if channel:
        params += f"&channel={quote(channel, safe='')}"

    proxy_host = proxy_base_url.rstrip('/') if proxy_base_url else IPTV_MANAGE_URL

    # Теги, URI которых — вложенный HLS-манифест (проксируем через /hls/manifest.m3u8).
    _MANIFEST_URI_TAGS = ("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:", "#EXT-X-SESSION-DATA:")
    # Теги, URI которых — бинарный ресурс (проксируем через /hls/segment.ts).
    # #EXT-X-KEY/MAP/PART/PRELOAD-HINT — критично для fMP4 и AES-128.
    _BINARY_URI_TAGS = ("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:", "#EXT-X-MAP:",
                        "#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:", "#EXT-X-RENDITION-REPORT:")

    def _proxy_uri(uri: str, kind: str) -> str:
        abs_uri = urljoin(base_url, uri)
        if "googlevideo.com" in abs_uri:
            return abs_uri
        if kind == "manifest":
            return f"{proxy_host}/hls/manifest.m3u8?url={quote(abs_uri, safe='')}{params}"
        return f"{proxy_host}/hls/segment.ts?url={quote(abs_uri, safe='')}{params}"

    for line in lines:
        line_str = line.strip()
        if not line_str:
            new_lines.append(line)
            continue

        if line_str.startswith("#"):
            # Нормализация live-манифеста.
            #
            # CDN часто отдают live HLS с тегами, характерными для VOD
            # (PLAYLIST-TYPE:VOD, PLAYLIST-TYPE:EVENT, ENDLIST). Их
            # собственные плееры игнорируют эти теги, а Jellyfin — нет:
            # он переключается в VOD-режим, скачивает 1-2 сегмента,
            # доигрывает и перезапрашивает манифест раз в 40-60 секунд.
            # За это время окно сегментов на CDN уезжает — Jellyfin
            # приходит за сегментами, которых уже нет → «битые» сегменты,
            # «бар у правого края», бесконечные залипания.
            #
            # Мы знаем, что это live (потому что проксируем живой эфир),
            # поэтому убираем VOD-маркеры принудительно. Если поток —
            # реально VOD-запись, её никто не будет смотреть через
            # IPTV-канал.
            if line_str.startswith("#EXT-X-PLAYLIST-TYPE:VOD"):
                continue
            if line_str.startswith("#EXT-X-PLAYLIST-TYPE:EVENT"):
                continue
            if line_str.startswith("#EXT-X-ENDLIST"):
                continue

            if 'URI="' in line_str:
                kind = None
                if any(line_str.startswith(t) for t in _MANIFEST_URI_TAGS):
                    kind = "manifest"
                elif any(line_str.startswith(t) for t in _BINARY_URI_TAGS):
                    kind = "segment"
                if kind:
                    def _repl(match, _kind=kind):
                        return f'URI="{_proxy_uri(match.group(1), _kind)}"'
                    line = re.sub(r'URI="([^"]+)"', _repl, line)

            new_lines.append(line)
            continue

        abs_url = urljoin(base_url, line_str)
        path_lower = abs_url.split("?")[0].lower()

        if ".m3u8" in path_lower and "googlevideo.com" not in abs_url:
            proxy_url = f"{proxy_host}/hls/manifest.m3u8?url={quote(abs_url, safe='')}{params}"
            new_lines.append(proxy_url)
        elif (referer or cookie) and "googlevideo.com" not in abs_url:
            proxy_ts_url = f"{proxy_host}/hls/segment.ts?url={quote(abs_url, safe='')}{params}"
            new_lines.append(proxy_ts_url)
        else:
            if "googlevideo.com" not in abs_url:
                known_exts = [".ts", ".mp4", ".m4s", ".aac", ".ac3", ".vtt", ".m4a"]
                if not any(path_lower.endswith(ext) for ext in known_exts):
                    sep = "&" if "?" in abs_url else "?"
                    abs_url += f"{sep}ffmpeg_ext=.ts"
            new_lines.append(abs_url)

    return "\n".join(new_lines)

def fetch_via_flaresolverr(url: str, headers: dict = None, timeout: int = 60) -> bytes:
    import json
    payload = json.dumps({
        "cmd": "request.get",
        "url": url,
        "maxTimeout": timeout * 1000
    }).encode()
    req = urllib.request.Request(
        IPTV_FLARESOLVERR_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
        data = json.loads(resp.read())
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr error: {data.get('message')}")
    solution = data.get("solution", {})
    if solution.get("status") != 200:
        raise RuntimeError(f"FlareSolverr status {solution.get('status')}")
    return solution.get("response", "").encode()

def sanitize_channel(channel: str | None, base_url: str | None = None) -> str | None:
    """Отбрасывает мусорный channel, пришедший из старых fallback'ов.

    При отбросе пишет debug-строку с входным значением, hostname из
    base_url и причиной. Диагностика `[?]` в логах: видно, что именно
    режется и по какому критерию — можно отличить реальный мусор от
    ложного срабатывания. Оставляем debug, чтобы не шуметь на каждом
    вызове в stdout (лог-уровень INFO).
    """
    if not channel:
        return None

    host = None
    if base_url:
        try:
            from urllib.parse import urlparse
            host = urlparse(base_url).hostname
        except Exception:
            host = None

    # Укорачиваем значение для лога: полный URL-хост может быть длинным.
    shown = channel if len(channel) <= 80 else channel[:77] + "..."

    # Явно битые варианты: host'ы длиннее 60 символов — точно не имя канала
    if len(channel) > 60:
        logger.debug(
            f"[SANITIZE] dropped: value={shown!r} ({len(channel)} chars), "
            f"host={host!r}, reason=len>60"
        )
        return None

    if host:
        # channel == hostname из base_url — это старый fallback, а не имя канала
        if channel == host:
            logger.debug(
                f"[SANITIZE] dropped: value={shown!r}, host={host!r}, reason=equals-host"
            )
            return None
        # Обрезки host'а (linear901-...skycdp, linear901-...delivery и т.п.)
        # всегда являются префиксом настоящего host'а.
        if len(channel) >= 15 and host.startswith(channel):
            logger.debug(
                f"[SANITIZE] dropped: value={shown!r}, host={host!r}, reason=hostname-prefix"
            )
            return None

    return channel
