"""
services/ad_detect.py — детект рекламы в HLS-манифесте.

  - _is_ad_manifest — по тегам SCTE-35 и длительностям #EXTINF.
  - _AD_TAG_MARKERS — теги-маркеры рекламных вставок.
"""
import re
import urllib.request
from urllib.parse import urljoin

from core.config import IPTV_DEFAULT_UA, logger
from services.hls_utils import parse_url_headers


_AD_TAG_MARKERS = (
    "#EXT-X-SPLICEPOINT-SCTE35",
    "SCTE35-OUT",
    "SCTE35-IN",
)


def _is_ad_manifest(payload: str, timeout: int = 5) -> bool:
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

    try:
        body = _fetch(clean_url)
    except Exception as e:
        logger.debug(f"[SNIFFER] ad-check fetch failed: {e}")
        return False

    if "#EXT-X-STREAM-INF" in body:
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("#EXT-X-STREAM-INF"):
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if nxt and not nxt.startswith("#"):
                        try:
                            media_url = urljoin(clean_url, nxt)
                            body = _fetch(media_url)
                        except Exception as e:
                            logger.debug(f"[SNIFFER] ad-check media fetch failed: {e}")
                            return False
                break

    for marker in _AD_TAG_MARKERS:
        if marker in body:
            logger.info(f"[SNIFFER] ad tag found: {marker}")
            return True

    extinfs = re.findall(r"#EXTINF:([0-9.]+)", body)
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
                    return True

    return False
