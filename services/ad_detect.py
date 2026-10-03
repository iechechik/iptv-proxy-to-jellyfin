"""
services/ad_detect.py — детект рекламы в HLS-манифесте.

  - _is_ad_manifest — по тегам SCTE-35 и длительностям #EXTINF.
  - _AD_TAG_MARKERS — теги-маркеры рекламных вставок.
"""
import re
import time
import urllib.request
from urllib.parse import urljoin

from core.config import IPTV_DEFAULT_UA, logger
from services.hls_utils import parse_url_headers


_AD_TAG_MARKERS = (
    "#EXT-X-SPLICEPOINT-SCTE35",
    "SCTE35-OUT",
    "SCTE35-IN",
)


# ad-detect-v2
# Изменения относительно v1:
#   - timeout=2 (был 5). AD-check вызывается ПОСЛЕ sniffer, до возврата
#     в resolve_channel_payload. Долгий AD-check = задержка резолва.
#   - Маркеры SCTE-35 ищутся в ОБОИХ телах (master и media). Раньше
#     master затирался media-телом, и маркер в master терялся.
#   - Длительности #EXTINF для ad-by-durations анализируются только
#     в media-теле (в master #EXTINF обычно нет).
#   - Если AD-check занял >1 сек, пишем info-лог для диагностики.
def _is_ad_manifest(payload: str, timeout: int = 2) -> bool:
    if not payload:
        return False
    clean_url, headers_dict = parse_url_headers(payload)
    if not clean_url.startswith(("http://", "https://")):
        return False

    req_headers = {"User-Agent": headers_dict.get("User-Agent", IPTV_DEFAULT_UA)}
    if headers_dict.get("Referer"):
        req_headers["Referer"] = headers_dict["Referer"]
    if headers_dict.get("Cookie"):
        req_headers["Cookie"] = headers_dict["Cookie"]

    def _fetch(u):
        req = urllib.request.Request(u, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    _t0 = time.time()

    try:
        master_body = _fetch(clean_url)
    except Exception as e:
        logger.debug(f"[SNIFFER] ad-check fetch failed: {e}")
        return False

    media_body = None
    if "#EXT-X-STREAM-INF" in master_body:
        lines = master_body.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("#EXT-X-STREAM-INF"):
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if nxt and not nxt.startswith("#"):
                        try:
                            media_url = urljoin(clean_url, nxt)
                            media_body = _fetch(media_url)
                        except Exception as e:
                            # media недоступна — проверяем только master.
                            logger.debug(f"[SNIFFER] ad-check media fetch failed: {e}")
                break

    # Маркеры SCTE-35 ищем в обоих телах.
    bodies = [master_body]
    if media_body:
        bodies.append(media_body)
    for body in bodies:
        for marker in _AD_TAG_MARKERS:
            if marker in body:
                logger.info(f"[SNIFFER] ad tag found: {marker}")
                _maybe_log_slow_ad_check(_t0)
                return True

    # Длительности #EXTINF анализируем только по media (в master их нет).
    if media_body:
        extinfs = re.findall(r"#EXTINF:([0-9.]+)", media_body)
        if extinfs:
            durs = []
            for d in extinfs:
                try:
                    durs.append(float(d))
                except ValueError:
                    pass
            if len(durs) >= 4:
                srt = sorted(durs)
                median = srt[len(srt) // 2]
                if median <= 12.0:
                    outliers = [d for d in durs if d >= 15.0]
                    if outliers:
                        logger.info(
                            f"[SNIFFER] ad by durations: median={median:.1f}s, "
                            f"outliers={outliers[:5]}"
                        )
                        _maybe_log_slow_ad_check(_t0)
                        return True

    _maybe_log_slow_ad_check(_t0)
    return False


def _maybe_log_slow_ad_check(t0: float, threshold: float = 1.0) -> None:
    """Логирует длительность AD-check, если он занял больше threshold."""
    elapsed = time.time() - t0
    if elapsed > threshold:
        logger.info(f"[SNIFFER] ad-check slow: {elapsed:.2f}s")
