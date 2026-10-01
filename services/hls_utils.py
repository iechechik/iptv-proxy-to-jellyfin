"""
services/hls_utils.py — общие утилиты для HLS-потоков:
  - parse_url_headers — разбор payload с |Referer=|Cookie=
  - TTL-логика по Cache-Control / expire в URL
  - детект рекламных URL и тегов в HLS-манифестах
"""
import re
import time
import urllib
import urllib.request
import urllib.error

from core.config import (
    IPTV_DEFAULT_UA,
    IPTV_CACHE_TTL, IPTV_FAST_CACHE_TTL,
)


def parse_url_headers(raw_url: str):
    """Разбирает строку вида:
    'http://stream.m3u8|Referer=http://site.ru|Cookie=bm=1; pu=2|User-Agent=Mozilla/5.0'
    Возвращает (clean_url, dict_headers).
    """
    clean_url = raw_url.split("|")[0]
    headers = {}
    if "|" in raw_url:
        for part in raw_url.split("|")[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                headers[k] = v
    return clean_url, headers


# ---------- TTL ----------
_MIN_CDN_TTL = 30
_TTL_NO_STORE = 30
_TTL_NO_CACHE = 60
_TTL_MUST_REVALIDATE = 60
_FAST_CAP = min(IPTV_FAST_CACHE_TTL, 600)
_SESSION_TTL_CAP = 300
_SNIFFER_HEAD_CAP = _FAST_CAP
_SNIFFER_NO_INFO_TTL = min(_FAST_CAP, 180)

_SESSION_URL_MARKERS = (
    ".php", ".asp", ".aspx", ".jsp",
    "wmsauthsign=", "nimblesessionid=",
    "phpsessid=", "session=", "token=", "auth=",
)

_URL_EXPIRY_PATTERNS = (
    re.compile(r"[?&]expire=(\d{10})", re.IGNORECASE),
    re.compile(r"[?&]expires=(\d{10})", re.IGNORECASE),
    re.compile(r"[?&]expire_at=(\d{10})", re.IGNORECASE),
    re.compile(r"/expire/(\d{10})/", re.IGNORECASE),
    re.compile(r"[?&]exp=(\d{10})", re.IGNORECASE),
)

_EXPIRY_SAFETY_MARGIN = 30

DEFAULT_TTL_BY_METHOD = {
    "direct": IPTV_CACHE_TTL,
    "yt-dlp": _FAST_CAP,
    "streamlink": _FAST_CAP,
    "sniffer": _FAST_CAP,
    "flaresolverr_simple": _FAST_CAP,
    "flaresolverr_session": _FAST_CAP,
}


def _is_session_url(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in _SESSION_URL_MARKERS)


def _extract_url_expiry(url: str) -> int | None:
    for pat in _URL_EXPIRY_PATTERNS:
        m = pat.search(url)
        if m:
            try:
                ts = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 946684800 <= ts <= 4102444800:
                return ts
    return None


def _parse_cache_control_ttl(cache_control: str, default_ttl: int) -> int:
    if not cache_control:
        return default_ttl
    directives = [d.strip() for d in cache_control.lower().split(",")]
    if "no-store" in directives:
        return _TTL_NO_STORE
    if "no-cache" in directives:
        return _TTL_NO_CACHE
    smaxage = None
    maxage = None
    for d in directives:
        if d.startswith("s-maxage="):
            try:
                smaxage = int(d.split("=", 1)[1])
            except ValueError:
                pass
        elif d.startswith("max-age="):
            try:
                maxage = int(d.split("=", 1)[1])
            except ValueError:
                pass
    if smaxage is not None:
        return min(max(smaxage, _MIN_CDN_TTL), default_ttl)
    if maxage is not None:
        return min(max(maxage, _MIN_CDN_TTL), default_ttl)
    if "must-revalidate" in directives:
        return _TTL_MUST_REVALIDATE
    return default_ttl


def _fetch_cache_control(clean_url: str, headers: dict, timeout: int = 2) -> str:
    req_headers = {"User-Agent": headers.get("User-Agent", IPTV_DEFAULT_UA)}
    if headers.get("Referer"):
        req_headers["Referer"] = headers["Referer"]
    if headers.get("Cookie"):
        req_headers["Cookie"] = headers["Cookie"]
    try:
        req = urllib.request.Request(clean_url, method="HEAD", headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.headers.get("Cache-Control", "") or ""
    except urllib.error.HTTPError as e:
        if e.code not in (405, 501):
            return ""
    except Exception:
        return ""
    try:
        get_headers = dict(req_headers)
        get_headers["Range"] = "bytes=0-0"
        req = urllib.request.Request(clean_url, headers=get_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.headers.get("Cache-Control", "") or ""
    except Exception:
        return ""


def _compute_cache_expire(payload: str, method: str, cache_control: str | None = None) -> float:
    default_ttl = DEFAULT_TTL_BY_METHOD.get(method, 600)
    if not payload:
        return time.time() + default_ttl
    if payload.startswith("#EXTM3U"):
        for line in payload.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                payload = line
                break
    clean_url, headers = parse_url_headers(payload)
    if not clean_url.startswith("http"):
        return time.time() + default_ttl
    url_exp = _extract_url_expiry(clean_url)
    if url_exp is not None:
        left = url_exp - time.time() - _EXPIRY_SAFETY_MARGIN
        if left <= 0:
            return time.time() + 5
        return time.time() + int(left)
    if cache_control is not None:
        cap = _SNIFFER_HEAD_CAP if method == "sniffer" else default_ttl
        ttl = _parse_cache_control_ttl(cache_control, cap)
        if method == "sniffer" and ttl < _SNIFFER_NO_INFO_TTL:
            clean_url, _ = parse_url_headers(payload)
            if _extract_url_expiry(clean_url) is None and not _is_session_url(clean_url):
                ttl = _SNIFFER_NO_INFO_TTL
        return time.time() + ttl
    if _is_session_url(clean_url):
        cc = _fetch_cache_control(clean_url, headers, timeout=2)
        if cc:
            return time.time() + _parse_cache_control_ttl(cc, _SESSION_TTL_CAP)
        return time.time() + _SESSION_TTL_CAP
    if method != "direct":
        cc = _fetch_cache_control(clean_url, headers, timeout=2)
        if cc:
            return time.time() + _parse_cache_control_ttl(cc, default_ttl)
        if method == "sniffer":
            return time.time() + _SNIFFER_NO_INFO_TTL
        return time.time() + default_ttl
    return time.time() + default_ttl


# ---------- Ad-детект ----------
_AD_URL_MARKERS = (
    "vmap", "vast", "doubleclick", "googlesyndication", "imasdk",
    "adservice", "adserver", "preroll", "midroll", "postroll",
    "/ad/", "/ads/",
)


def _is_ad_url(url: str) -> bool:
    if not url:
        return False
    low = url.lower()
    return any(m in low for m in _AD_URL_MARKERS)
