"""services/hls_utils.py — общие утилиты для HLS-потоков:
  - parse_url_headers — разбор payload с |Referer=|Cookie=
  - TTL-логика по Cache-Control / expire в URL
  - детект рекламных URL и тегов в HLS-манифестах
  - select_playable / probe_stream_types / tcp_reachable — общие правила
    «что реально играет» и «жив ли хост» (используются и муксом, и пробой,
    и сниффером, чтобы не расползались разные версии одной логики)
"""
import re
import socket
import subprocess
import time
import urllib
import urllib.parse
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
# ttl-url-lifetime-v1: TTL считается по сроку жизни ссылки (см. _compute_cache_expire),
# поэтому константы Cache-Control из проекта убраны.
_FAST_CAP = min(IPTV_FAST_CACHE_TTL, 600)
_SESSION_TTL_CAP = 300

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
    # hdnea/Akamai-подобные токены: "...~exp=1234567890~acl=*" — разделитель "~".
    # Без этого варианта истёкший токен не распознавался, TTL считался заново,
    # и ссылка уходила в probe уже мёртвой (наблюдали на Euronews).
    re.compile(r"[?&~]exp=(\d{10})", re.IGNORECASE),
)

_EXPIRY_SAFETY_MARGIN = 75

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


def _compute_cache_expire(payload: str, method: str) -> float:
    """Сколько держать payload в кэше — по сроку жизни ССЫЛКИ (ttl-url-lifetime-v1).

    Cache-Control/CDN-заголовки для этого не годятся: живые HLS-плейлисты сплошь
    отдаются с `no-store`/`max-age=1` (плейлист меняется каждые несколько секунд),
    из-за чего рабочие ссылки выбрасывались каждые 30 с и канал уходил в вечный
    перерезолв (наблюдали на трёх каналах: ссылки без подписи, TTL 29–30 с).

    Правило:
      * есть `exp` в URL → срок известен, берём его минус запас;
      * сессионные URL (wmsauthsign, PHPSESSID, token=…) → короткий TTL;
      * иначе → дефолт метода.
    Реальные смерти ловят проба перед записью, измеренный срок жизни
    (healthcheck_worker._note_payload_death) и stale-gate на 403/404 в муксе.
    """
    default_ttl = DEFAULT_TTL_BY_METHOD.get(method, 600)
    if not payload:
        return time.time() + default_ttl
    if payload.startswith("#EXTM3U"):
        for line in payload.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                payload = line
                break
    clean_url, _ = parse_url_headers(payload)
    if not clean_url.startswith("http"):
        return time.time() + default_ttl
    url_exp = _extract_url_expiry(clean_url)
    if url_exp is not None:
        left = url_exp - time.time() - _EXPIRY_SAFETY_MARGIN
        if left <= 0:
            return time.time() + 5
        return time.time() + int(left)
    if _is_session_url(clean_url):
        return time.time() + _SESSION_TTL_CAP
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


# ---------- Общие правила: что реально играет и жив ли хост ----------
_VARIANT_BW_RE = re.compile(r"BANDWIDTH=(\d+)", re.IGNORECASE)
_MEDIA_URI_RE = re.compile(r'URI="([^"]+)"', re.IGNORECASE)


def select_playable(text: str, base_url: str) -> dict:
    """Из текста манифеста — то, что реально пойдёт в плеер.

    Медиа-плейлист:  {"kind": "media",  "video": base_url, "audio": None}
    Master:          {"kind": "master", "video": <вариант с max BANDWIDTH>,
                                         "audio": <URI аудио-группы|None>}

    Одно место для правила на весь проект: мукс, проба и сниффер спрашивают
    здесь вместо того, чтобы парсить master каждый по-своему.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    if not any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        return {"kind": "media", "video": base_url, "audio": None}
    audio = None
    best_bw, best_url = -1, None
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in ln.upper():
            if audio is None:
                m = _MEDIA_URI_RE.search(ln)
                if m:
                    audio = urllib.parse.urljoin(base_url, m.group(1))
        elif ln.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt and not nxt.startswith("#"):
                m = _VARIANT_BW_RE.search(ln)
                bw = int(m.group(1)) if m else 0
                if bw > best_bw:
                    best_bw, best_url = bw, urllib.parse.urljoin(base_url, nxt)
    return {"kind": "master", "video": best_url, "audio": audio}


def probe_stream_types(url: str, ua: str = None, referer: str = None,
                       cookie: str = None, timeout: int = 8) -> set:
    """Какие дорожки реально несёт плейлист: {"video"}, {"audio"}, оба или пусто.

    Нужен там, где по имени файла или по объявленным CODECS судить нельзя:
    у части CDN объявленный в master'е аудио-кодек не означает, что аудио есть
    в самом варианте, и наоборот.
    """
    clean, _ = parse_url_headers(url or "")
    if not clean.startswith("http"):
        return set()
    cmd = ["ffprobe", "-v", "error", "-analyzeduration", "3000000",
           "-probesize", "1500000", "-show_entries", "stream=codec_type",
           "-of", "csv=p=0"]
    hdr = ""
    if referer:
        hdr += f"Referer: {referer}\r\n"
    if cookie:
        hdr += f"Cookie: {cookie}\r\n"
    if hdr:
        cmd += ["-headers", hdr]
    cmd += ["-user_agent", ua or IPTV_DEFAULT_UA, clean]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return set()
    types = set()
    for line in ((res.stdout or "") + "\n" + (res.stderr or "")).splitlines():
        low = line.strip().lower()
        if not low:
            continue
        if low.startswith("video") or ",video" in low:
            types.add("video")
        if low.startswith("audio") or ",audio" in low:
            types.add("audio")
    return types


def fetch_manifest_text(url: str, ua: str = None, referer: str = None,
                        cookie: str = None, timeout: int = 3) -> str:
    """Тянет плейлист и возвращает его текст ("" если это не плейлист).

    Дешёвая проверка «этот плейлист вообще жив»: 403/404/пустой ответ → "".
    Двоичные ответы (сегменты видео/аудио) отбрасываем по content-type, чтобы
    не тянуть мегабайты впустую.
    """
    clean, _ = parse_url_headers(url or "")
    if not clean.startswith("http"):
        return ""
    headers = {"User-Agent": ua or IPTV_DEFAULT_UA}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    try:
        req = urllib.request.Request(clean, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype.startswith(("video/", "audio/", "image/", "font/")):
                return ""
            raw = resp.read(300000)
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def tcp_reachable(url: str, timeout: float = 3.0) -> bool:
    """Жив ли хост вообще (TCP-коннект) — вместо 45-секундного ожидания ffprobe
    на мёртвом адресе. True также когда проверка неприменима: тогда решение
    остаётся за ffprobe.
    """
    try:
        clean, _ = parse_url_headers(url or "")
        parts = urllib.parse.urlsplit(clean)
    except Exception:
        return True
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return True
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except Exception:
        return False
