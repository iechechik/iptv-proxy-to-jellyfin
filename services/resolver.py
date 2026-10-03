"""
services/resolver.py — диспетчер резолверов.

Содержит:
  - методы резолвинга: direct, yt-dlp, streamlink, flaresolverr_simple/session.
  - YouTube-резолв.
  - probe_stream (ffprobe), verify_stream_alive (HEAD).
  - resolve_channel_payload — точка входа.

Playwright-сниффер — в services/chromium.py.
TTL и ad-детект URL — в services/hls_utils.py.
Детект рекламных тегов в манифесте — в services/ad_detect.py.

Реэкспорт для обратной совместимости:
  parse_url_headers, _compute_cache_expire, _is_ad_url, _is_ad_manifest,
  _run_browser_sniffer_sync, resolve_via_browser_sniffer,
  _extract_video_id, _root_host, _classify_hls_by_url.
"""
import re
import time
import json
import subprocess
import os
import uuid
import urllib
import urllib.request
import urllib.error
from urllib.parse import urljoin

from core.config import (
    IPTV_RESOLVER_ORDER, IPTV_DEFAULT_UA,
    IPTV_YOUTUBE_CACHE_TTL, IPTV_STREAMLINK_TIMEOUT,
    IPTV_FLARESOLVERR_TIMEOUT, IPTV_FLARESOLVERR_URL,
    IPTV_FETCH_TIMEOUT,
    logger,
)
import core.config as cfg

# Реэкспорт утилит и sniffer'а — внешние импорты не сломаются.
# Реэкспорт для обратной совместимости (url_analytics, fallback, healthcheck).
# F401 подавлен через noqa — это публичное API модуля.
from services.hls_utils import (        # noqa: F401
    parse_url_headers,
    _compute_cache_expire,
    _is_ad_url,
    _extract_url_expiry,
    _is_session_url,
    _SESSION_URL_MARKERS,
)
from services.ad_detect import _is_ad_manifest              # noqa: F401
from services.chromium import (         # noqa: F401
    resolve_via_browser_sniffer,
    _run_browser_sniffer_sync,
    _pick_best_candidate,
    _extract_video_id,
    _root_host,
    _classify_hls_by_url,
    _HLS_MASTER_NAMES,
    _HLS_MEDIA_NAMES,
)


def is_youtube_url(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u or "youtube-nocookie.com" in u


def is_direct_stream(url: str) -> bool:
    direct_patterns = ['.m3u8', 'manifest', 'playlist', 'master.m3u8', 'index.m3u8', 'chunklist']
    return any(p in url.lower() for p in direct_patterns)


def extract_m3u8_from_text(text: str) -> str | None:
    """Первый не-рекламный m3u8-URL из текста."""
    matches = re.findall(r'https?://[^\s"\'<>]+?\.m3u8[^\s"\'<>]*', text)
    for m in matches:
        if not _is_ad_url(m):
            return m
    return None


# ---------- Streamlink ----------
def resolve_streamlink(url: str) -> str:
    cmd = [
        "streamlink", "--stream-url",
        "--retry-streams", "3",
        "--stream-segment-attempts", "3",
        "--stream-timeout", "30",
        url, "best"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=IPTV_STREAMLINK_TIMEOUT)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError(result.stderr.strip() or "Streamlink вернул пустой результат")


# ---------- FlareSolverr ----------
def _flare_flag_path() -> str:
    return getattr(cfg, "IPTV_HEALTHCHECK_FLARESOLVERR_FLAG_FILE", "") or ""


def _flare_flag_exists() -> bool:
    path = _flare_flag_path()
    if not path:
        return False
    return os.path.exists(path)


def _flare_raise_flag(reason: str) -> None:
    path = _flare_flag_path()
    if not path:
        return
    if os.path.exists(path):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(reason)
        logger.warning(f"[FLARE-FLAG] flag raised for FlareSolverr restart: {reason}")
    except Exception as e:
        logger.warning(f"[FLARE-FLAG] failed to raise flag: {e}")


def resolve_via_flaresolverr_simple(page_url: str, extract_regex: str) -> str:
    if _flare_flag_exists():
        raise RuntimeError("FlareSolverr restart pending (flag file)")
    try:
        payload = json.dumps({
            "cmd": "request.get", "url": page_url, "maxTimeout": 60000
        }).encode()
        req = urllib.request.Request(
            IPTV_FLARESOLVERR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=IPTV_FLARESOLVERR_TIMEOUT) as resp:
            data = json.loads(resp.read())
        if data.get("status") != "ok":
            raise RuntimeError(f"flaresolverr status: {data.get('message')}")
        html_content = data["solution"]["response"]
        match = re.search(extract_regex, html_content)
        if not match:
            raise RuntimeError("regex did not match flaresolverr response")
        return match.group(0)
    except Exception as e:
        _flare_raise_flag(str(e))
        raise


def resolve_via_flaresolverr_session(target_url: str, timeout: int = 15) -> str | None:
    if _flare_flag_exists():
        raise RuntimeError("FlareSolverr restart pending (flag file)")
    session_id = f"proxy_session_{uuid.uuid4().hex[:12]}"
    try:
        req_create = urllib.request.Request(
            IPTV_FLARESOLVERR_URL,
            data=json.dumps({"cmd": "sessions.create", "session": session_id}).encode(),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req_create, timeout=5) as resp:
                create_res = json.loads(resp.read())
            if create_res.get("status") != "ok":
                logger.warning(f"[FLARESOLVERR] sessions.create status={create_res.get('status')}: {create_res.get('message')}")
                return None
        except Exception as e:
            logger.error(f"[FLARESOLVERR] sessions.create failed: {e}")
            return None

        get_payload = json.dumps({
            "cmd": "request.get", "session": session_id,
            "url": target_url, "maxTimeout": timeout * 1000
        }).encode()
        req_get = urllib.request.Request(
            IPTV_FLARESOLVERR_URL, data=get_payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req_get, timeout=timeout + 5) as warm_resp:
            warm_resp.read()
        time.sleep(6)
        with urllib.request.urlopen(req_get, timeout=timeout + 5) as resp:
            res = json.loads(resp.read())
        if res.get("status") != "ok":
            logger.warning(f"[FLARESOLVERR] request.get (retry) status={res.get('status')}: {res.get('message')}")
            return None
        solution = res.get("solution", {})
        cookies = solution.get("cookies", [])
        cookie_str = "; ".join([
            f"{c['name']}={c['value']}" for c in cookies
            if c.get("name") and c.get("value")
        ])

        def _attach_headers_if_needed(url_str: str) -> str:
            if not url_str:
                return url_str
            if "|Referer=" not in url_str:
                url_str += f"|Referer={target_url}"
            if cookie_str and "|Cookie=" not in url_str:
                url_str += f"|Cookie={cookie_str}"
            return url_str

        html = solution.get("response", "").replace("\\/", "/")
        m3u8_url = extract_m3u8_from_text(html)
        if m3u8_url:
            return _attach_headers_if_needed(m3u8_url)

        iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if iframe_match:
            iframe_url = iframe_match.group(1)
            if iframe_url.startswith("//"):
                iframe_url = "https:" + iframe_url
            elif iframe_url.startswith("/"):
                iframe_url = urljoin(target_url, iframe_url)
            m3u8_in_iframe = extract_m3u8_from_text(iframe_url)
            if m3u8_in_iframe:
                return _attach_headers_if_needed(m3u8_in_iframe)
            iframe_payload = json.dumps({
                "cmd": "request.get", "session": session_id,
                "url": iframe_url, "maxTimeout": timeout * 1000
            }).encode()
            req_iframe = urllib.request.Request(
                IPTV_FLARESOLVERR_URL, data=iframe_payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req_iframe, timeout=timeout + 10) as resp_iframe:
                iframe_res = json.loads(resp_iframe.read())
            if iframe_res.get("status") == "ok":
                iframe_html = iframe_res["solution"]["response"].replace("\\/", "/")
                found = extract_m3u8_from_text(iframe_html)
                return _attach_headers_if_needed(found)
    except Exception as e:
        logger.error(f"[FLARESOLVERR] session error: {e}")
        _flare_raise_flag(str(e))
    finally:
        try:
            req_destroy = urllib.request.Request(
                IPTV_FLARESOLVERR_URL,
                data=json.dumps({"cmd": "sessions.destroy", "session": session_id}).encode(),
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req_destroy, timeout=5)
        except Exception:
            pass
    return None


# ---------- yt-dlp ----------
def resolve_via_ytdlp(url: str, ua: str):
    cmd = ["yt-dlp", "-f", "bv*+ba/b", "-g", "--no-warnings", "--user-agent", ua, url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=IPTV_FETCH_TIMEOUT)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or "empty output")
    urls = [u for u in result.stdout.strip().splitlines() if u.strip()]
    if len(urls) == 1:
        return True, urls[0]
    elif len(urls) >= 2:
        video_url, audio_url = urls[0], urls[1]
        manifest = (
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="audio",AUTOSELECT=YES,DEFAULT=YES,URI="{audio_url}"\n'
            f'#EXT-X-STREAM-INF:BANDWIDTH=3000000,AUDIO="audio"\n{video_url}\n'
        )
        return False, manifest
    raise RuntimeError("yt-dlp вернул пустой список ссылок")


def resolve_youtube_stream(url: str, ua: str, name: str):
    try:
        stream_url = resolve_streamlink(url)
        logger.info(f"[RESOLVE] '{name}': streamlink (youtube) done")
        return True, stream_url, time.time() + IPTV_YOUTUBE_CACHE_TTL, "streamlink"
    except Exception as e:
        logger.warning(f"[RESOLVE] '{name}': streamlink (youtube) failed: {e}")
        try:
            is_direct, payload = resolve_via_ytdlp(url, ua)
            logger.info(f"[RESOLVE] '{name}': yt-dlp (youtube) done")
            return is_direct, payload, time.time() + IPTV_YOUTUBE_CACHE_TTL, "yt-dlp"
        except Exception as e2:
            logger.warning(f"[RESOLVE] '{name}': yt-dlp (youtube) failed: {e2}")
            raise RuntimeError(f"All YouTube methods failed for {name}")


# ---------- Обёртки методов ----------
def _resolve_direct(ch):
    url = ch["url"]
    return True, url, _compute_cache_expire(url, "direct"), "direct"


def _resolve_ytdlp(ch):
    is_direct, payload = resolve_via_ytdlp(ch["url"], ch.get("ua", IPTV_DEFAULT_UA))
    return is_direct, payload, _compute_cache_expire(payload, "yt-dlp"), "yt-dlp"


def _resolve_streamlink(ch):
    stream_url = resolve_streamlink(ch["url"])
    return True, stream_url, _compute_cache_expire(stream_url, "streamlink"), "streamlink"


def _resolve_flaresolverr_simple(ch):
    if not ch.get("fs_regex"):
        raise RuntimeError("flaresolverr_simple требует fs_regex")
    payload = resolve_via_flaresolverr_simple(ch["url"], ch["fs_regex"])
    return True, payload, _compute_cache_expire(payload, "flaresolverr_simple"), "flaresolverr_simple"


def _resolve_flaresolverr_session(ch):
    payload = resolve_via_flaresolverr_session(ch["url"])
    if not payload:
        raise RuntimeError("flaresolverr_session вернул None")
    return True, payload, _compute_cache_expire(payload, "flaresolverr_session"), "flaresolverr_session"


def _resolve_browser_sniffer(ch):
    name = ch["name"]
    sniff_res = resolve_via_browser_sniffer(ch["url"], ch.get("ua", IPTV_DEFAULT_UA))
    if not sniff_res:
        raise RuntimeError("sniffer не нашёл m3u8/embed")
    if sniff_res["type"] == "m3u8":
        payload = sniff_res["url"]
        cc = sniff_res.get("cache_control")
        return True, payload, _compute_cache_expire(payload, "sniffer", cache_control=cc), "sniffer"
    elif sniff_res["type"] == "embed":
        embed_url = sniff_res["url"]
        logger.info(f"[RESOLVE] '{name}': sniffer found embed: {embed_url}")
        if is_youtube_url(embed_url):
            is_direct, payload, expire, _ = resolve_youtube_stream(embed_url, ch.get("ua", IPTV_DEFAULT_UA), name)
            return is_direct, payload, expire, "sniffer"
        try:
            is_direct, payload, expire, _ = _resolve_ytdlp({"name": name, "url": embed_url, "ua": ch.get("ua", IPTV_DEFAULT_UA)})
            return is_direct, payload, expire, "sniffer"
        except Exception as e:
            logger.warning(f"[RESOLVE] '{name}': yt-dlp failed on embed {embed_url}: {e}")
            try:
                is_direct, payload, expire, _ = _resolve_streamlink({"name": name, "url": embed_url, "ua": ch.get("ua", IPTV_DEFAULT_UA)})
                return is_direct, payload, expire, "sniffer"
            except Exception as e2:
                raise RuntimeError(f"Не удалось обработать embed для '{name}': {e2}")
    else:
        raise RuntimeError(f"Неизвестный тип результата сниффера для '{name}'")


_RESOLVER_FUNCS = {
    "direct": _resolve_direct,
    "yt-dlp": _resolve_ytdlp,
    "streamlink": _resolve_streamlink,
    "flaresolverr_simple": _resolve_flaresolverr_simple,
    "sniffer": _resolve_browser_sniffer,
    "flaresolverr_session": _resolve_flaresolverr_session,
}
RESOLVERS = {name: _RESOLVER_FUNCS[name] for name in IPTV_RESOLVER_ORDER if name in _RESOLVER_FUNCS}


def is_valid_resolver(name: str) -> bool:
    return name in RESOLVERS or name == "auto"


# ---------- Probe / verify ----------
def verify_stream_alive(payload: str, ua: str = IPTV_DEFAULT_UA) -> bool:
    if not payload:
        return False
    if not isinstance(payload, (str, bytes)):
        logger.warning(f"[PROBE] invalid payload type {type(payload)}, treating as dead")
        return False
    if payload.startswith("#EXTM3U"):
        match = re.search(r'https?://[^\s"\']+', payload)
        if not match:
            return True
        target_url = match.group(0)
    else:
        target_url = payload
    clean_url, headers_dict = parse_url_headers(target_url)
    referer = headers_dict.get("Referer", "")
    headers = {
        "User-Agent": ua, "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive", "Range": "bytes=0-0"
    }
    if referer:
        headers["Referer"] = referer
    cookie = headers_dict.get("Cookie", "")
    if cookie:
        headers["Cookie"] = cookie
    if "googlevideo.com" in clean_url:
        headers.setdefault("Referer", "https://www.youtube.com/")
        headers.setdefault("Origin", "https://www.youtube.com")
    try:
        req = urllib.request.Request(clean_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if ".m3u8" in clean_url.lower() and "text/html" in content_type.lower():
                logger.warning(f"[PROBE] got HTML instead of HLS: {clean_url[:60]}...")
                return False
            return resp.status in (200, 206, 301, 302)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 410):
            logger.warning(f"[PROBE] unavailable (HTTP {e.code}): {clean_url[:60]}...")
            return False
        if e.code >= 500:
            logger.warning(f"[PROBE] CDN returned {e.code}: {clean_url[:60]}...")
            return False
        return True
    except Exception as e:
        logger.warning(f"[PROBE] could not reach stream CDN: {e}")
        return False


def resolve_channel_payload(ch):
    name = ch["name"]
    url = ch["url"]
    ua = ch.get("ua", IPTV_DEFAULT_UA)
    fs_regex = ch.get("fs_regex", "")
    if is_youtube_url(url):
        return resolve_youtube_stream(url, ua, name)
    resolver = ch.get("resolver", "auto").lower()
    if resolver != "auto":
        if resolver in RESOLVERS:
            logger.info(f"[RESOLVE] '{name}': {resolver} tried...")
            try:
                is_direct, payload, expire, method = RESOLVERS[resolver](ch)
                logger.info(f"[RESOLVE] '{name}': {resolver} done")
                return is_direct, payload, expire, method
            except Exception as e:
                logger.error(f"[RESOLVE] '{name}': {resolver} failed: {e}")
                raise
        else:
            logger.warning(f"[RESOLVE] '{name}': {resolver} unknown, use auto")
    logger.info(f"[RESOLVE] '{name}': auto started")
    for method_name, method_func in RESOLVERS.items():
        if method_name == "direct" and not is_direct_stream(url):
            continue
        if method_name == "flaresolverr_simple" and not fs_regex:
            continue
        logger.info(f"[RESOLVE] '{name}': {method_name} tried...")
        start = time.time()
        try:
            is_direct, payload, expire, method = method_func(ch)
            elapsed = (time.time() - start) * 1000
            logger.info(f"[RESOLVE] '{name}': {method_name} done {elapsed:.0f} ms")
            return is_direct, payload, expire, method
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            logger.warning(f"[RESOLVE] '{name}': {method_name} failed {elapsed:.0f} ms: {e}")
    logger.error(f"[RESOLVE] '{name}': all resolution methods failed")
    raise RuntimeError(f"All resolution methods failed for '{name}'")


def probe_stream(payload: str, timeout: int = 10, channel: str = None) -> dict:
    ch_pfx = f"'{channel}': " if channel else ""
    clean_url, headers_dict = parse_url_headers(payload)
    if payload.startswith("#EXTM3U"):
        lines = payload.splitlines()
        video_url = None
        for line in lines:
            if not line.startswith("#") and line.strip():
                video_url = line.strip()
                break
        if not video_url:
            return {"ok": False, "detail": "No video URL in synthetic manifest"}
        clean_url, _ = parse_url_headers(video_url)
    start_time = time.time()
    if "googlevideo.com" in clean_url or "youtube.com" in clean_url or "manifest.googlevideo.com" in clean_url:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json",
               "-timeout", str(timeout * 1000000)]
        headers_str = ""
        if headers_dict.get("Referer"):
            headers_str += f"Referer: {headers_dict['Referer']}\r\n"
        if headers_dict.get("Cookie"):
            headers_str += f"Cookie: {headers_dict['Cookie']}\r\n"
        if headers_dict.get("User-Agent"):
            headers_str += f"User-Agent: {headers_dict['User-Agent']}\r\n"
        if "Referer:" not in headers_str:
            headers_str += "Referer: https://www.youtube.com/\r\n"
        if "Origin:" not in headers_str:
            headers_str += "Origin: https://www.youtube.com\r\n"
        if headers_str:
            cmd += ["-headers", headers_str]
        cmd.append(clean_url)
        logger.info(f"[PROBE] {ch_pfx}direct ffprobe for YouTube: {clean_url[:120]}...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 35)  # probe-timeout-v2
            if result.returncode != 0:
                logger.warning(f"[PROBE] {ch_pfx}ffprobe failed: {result.stderr.strip()}")
                return {"ok": False, "detail": result.stderr.strip()[:150]}
            info = json.loads(result.stdout)
            streams = info.get("streams", [])
            has_video = any(s.get("codec_type") == "video" for s in streams)
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            ok = has_video or has_audio
            probe_elapsed = time.time() - start_time
            logger.info(f"[PROBE] {ch_pfx}ffprobe: streams={len(streams)}, video={has_video}, audio={has_audio}, ok={ok}")
            return {"ok": ok, "has_video": has_video, "has_audio": has_audio,
                    "detail": f"streams={len(streams)}, video={has_video}, audio={has_audio}",
                    "probe_elapsed": probe_elapsed}
        except Exception as e:
            logger.error(f"[PROBE] {ch_pfx}ffprobe launch error: {e}")
            return {"ok": False, "detail": f"ffprobe error: {e}"}
    url_path_lower = urllib.parse.urlsplit(clean_url).path.lower()
    is_media_direct = url_path_lower.endswith((".m3u8", ".mpd", ".ts", ".mp4"))
    if is_media_direct:
        cmd = ["ffprobe", "-v", "error",
               "-show_entries", "stream=codec_type,codec_name", "-of", "json",
               "-timeout", str((timeout + 25) * 1000000)]  # probe-timeout-v2
        headers_str = ""
        if headers_dict.get("Referer"):
            headers_str += f"Referer: {headers_dict['Referer']}\r\n"
        if headers_dict.get("Cookie"):
            headers_str += f"Cookie: {headers_dict['Cookie']}\r\n"
        if headers_dict.get("User-Agent"):
            headers_str += f"User-Agent: {headers_dict['User-Agent']}\r\n"
        if headers_str:
            cmd += ["-headers", headers_str]
        cmd.append(clean_url)
        logger.info(f"[PROBE] {ch_pfx}direct ffprobe: {clean_url[:120]}...")
    else:
        proxy_base = cfg.IPTV_MANAGE_URL
        params = {"url": clean_url}
        if headers_dict.get("Referer"):
            params["referer"] = headers_dict["Referer"]
        if headers_dict.get("Cookie"):
            params["cookie"] = headers_dict["Cookie"]
        if headers_dict.get("User-Agent"):
            params["ua"] = headers_dict["User-Agent"]
        if channel:
            params["channel"] = channel
        params["no_prefetch"] = "1"
        proxy_url = f"{proxy_base}/hls/manifest.m3u8?" + urllib.parse.urlencode(params)
        cmd = ["ffprobe", "-v", "error",
               "-show_entries", "stream=codec_type,codec_name", "-of", "json",
               "-timeout", str((timeout + 25) * 1000000),  # probe-timeout-v2
               proxy_url]
        logger.info(f"[PROBE] {ch_pfx}ffprobe via proxy: {proxy_url[:120]}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 35)  # probe-timeout-v2
        if result.returncode != 0:
            logger.warning(f"[PROBE] {ch_pfx}ffprobe failed: {result.stderr.strip()}")
            return {"ok": False, "detail": result.stderr.strip()[:150]}
        info = json.loads(result.stdout)
        streams = info.get("streams", [])
        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        ok = has_video or has_audio
        probe_elapsed = time.time() - start_time
        logger.info(f"[PROBE] {ch_pfx}ffprobe: streams={len(streams)}, video={has_video}, audio={has_audio}, ok={ok}")
        return {"ok": ok, "has_video": has_video, "has_audio": has_audio,
                "detail": f"streams={len(streams)}, video={has_video}, audio={has_audio}",
                "probe_elapsed": probe_elapsed}
    except Exception as e:
        logger.error(f"[PROBE] {ch_pfx}ffprobe launch error: {e}")
        return {"ok": False, "detail": f"ffprobe error: {e}"}
