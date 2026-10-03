"""
services/chromium.py — Playwright-сниффер HLS-манифестов.

  - _run_browser_sniffer_sync — синхронный прогон Playwright.
  - resolve_via_browser_sniffer — обёртка с ad-check (2 попытки).
  - _pick_best_candidate — выбор лучшего m3u8 из собранных.
  - _extract_video_id, _root_host — identity кандидатов.
  - _classify_hls_by_url — master/media по имени файла.
"""
import os
import re
import time
import concurrent.futures
from typing import Optional, Dict
from playwright.sync_api import sync_playwright

from core.config import (
    IPTV_DEFAULT_UA,
    IPTV_PLAYWRIGHT_CHROMIUM,
    IPTV_PLAYWRIGHT_NAVIGATION_TIMEOUT,
    logger,
)
from services.hls_utils import _is_ad_url
from services.ad_detect import _is_ad_manifest


COOKIE_PATTERNS = [
    "Accept", "Accept all", "Agree", "Allow all",
    "Tout accepter", "Accepter",
    "Continue without accepting", "Continuer sans accepter",
]


_HLS_MASTER_NAMES = ("master.m3u8",)
_HLS_MEDIA_NAMES = ("playlist.m3u8", "chunklist", "index.m3u8", "media.m3u8", "prog_index.m3u8")


def _classify_hls_by_url(url: str):
    if not url:
        return (False, False)
    low = url.lower().split("?", 1)[0]
    fname = low.rsplit("/", 1)[-1]
    for n in _HLS_MASTER_NAMES:
        if fname == n or fname.endswith("/" + n) or low.endswith(n):
            return (True, False)
    for n in _HLS_MEDIA_NAMES:
        if fname == n or fname.endswith("/" + n):
            return (False, True)
        if n == "chunklist" and "chunklist" in fname:
            return (False, True)
    return (False, False)


_VIDEO_ID_QUERY_KEYS = ("video_id", "vid", "stream_id", "videoid")


def _extract_video_id(url: str) -> str:
    if not url:
        return ""
    clean = url.split("|", 1)[0]
    m = re.search(r"/video/([A-Za-z0-9_-]+)\.m3u8", clean)
    if m:
        return m.group(1)
    m = re.search(r"/([A-Za-z0-9_-]{6,})\.m3u8", clean)
    if m:
        return m.group(1)
    try:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(clean).query)
        for k in _VIDEO_ID_QUERY_KEYS:
            if k in qs and qs[k]:
                return qs[k][0]
    except Exception:
        pass
    return ""


def _root_host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url.split("|", 1)[0]).hostname or ""
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return ""


def _pick_best_candidate(candidates: list, channel_name: str = None):
    tag = f"[{channel_name}] " if channel_name else ""
    for c in candidates:
        if c.get("type") == "embed":
            return c
    ad_vids = set()
    for c in candidates:
        if c.get("is_ad"):
            v = _extract_video_id(c.get("url", ""))
            if v:
                ad_vids.add(v)
    if ad_vids:
        logger.info(f"[SNIFFER] {tag}ad video_ids: {sorted(ad_vids)}")
    masters = [c for c in candidates
               if c.get("type") == "m3u8" and c.get("is_master") and not c.get("is_ad")]
    medias = [c for c in candidates
              if c.get("type") == "m3u8" and c.get("is_media") and not c.get("is_ad")]
    filtered_masters = []
    for c in masters:
        vid = _extract_video_id(c.get("url", ""))
        if vid and vid in ad_vids:
            logger.info(f"[SNIFFER] {tag}master dropped (vid={vid!r} == ad): {c['url'][:120]}")
            continue
        filtered_masters.append(c)
    if filtered_masters and medias:
        m_host = _root_host(filtered_masters[-1].get("url", ""))
        d_host = _root_host(medias[-1].get("url", ""))
        if m_host and d_host and m_host != d_host:
            logger.info(f"[SNIFFER] {tag}master/media different CDN ({m_host} vs {d_host}) -> prefer media")
            chosen = medias[-1]
            logger.info(f"[SNIFFER] {tag}selected media (CDN mismatch): {chosen['url'][:120]}")
            return chosen
    if filtered_masters:
        chosen = filtered_masters[-1]
        logger.info(f"[SNIFFER] {tag}selected master: {chosen['url'][:120]}")
        return chosen
    if medias:
        chosen = medias[-1]
        logger.info(f"[SNIFFER] {tag}selected media (no live master): {chosen['url'][:120]}")
        return chosen
    for c in candidates:
        if c.get("type") == "m3u8" and not c.get("is_ad"):
            logger.info(f"[SNIFFER] {tag}selected unclassified m3u8: {c['url'][:120]}")
            return c
    logger.warning(f"[SNIFFER] {tag}no usable m3u8/embed found")
    return None


def _run_browser_sniffer_sync(target_url: str, ua: str, max_timeout: int, skip_urls: set = None) -> Optional[Dict[str, str]]:
    candidates: list = []
    found_result = None  # sniffer-unbound-fix-v1: без этого UnboundLocalError
                         # на return, если try упал до присваивания.
    found_result = None
    master_seen_ts: float = 0.0
    _skip_urls = skip_urls or set()
    user_agent = ua or IPTV_DEFAULT_UA
    exec_path = IPTV_PLAYWRIGHT_CHROMIUM
    browser = None
    context = None
    page = None
    try:
        with sync_playwright() as p:
            launch_args = {
                "headless": True,
                "args": [
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-gpu", "--disable-extensions",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints",
                    "--disable-crash-reporter", "--disable-breakpad",
                    "--no-first-run", "--no-default-browser-check",
                    "--disable-sync", "--disable-default-apps",
                    "--metrics-recording-only", "--mute-audio",
                ]
            }
            if os.path.exists(exec_path):
                launch_args["executable_path"] = exec_path
            try:
                browser = p.chromium.launch(**launch_args)
            except Exception as _le:
                logger.warning(
                    f"[SNIFFER] chromium launch failed: {type(_le).__name__}: {_le} "
                    f"(exec_path={exec_path}, exists={os.path.exists(exec_path)})"
                )
                raise
            context = browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1280, "height": 720},
                locale="en-US"
            )
            page = context.new_page()

            def handle_request(request):
                req_url = request.url
                low = req_url.lower()
                if ".mpd" in low and not any(ext in low for ext in [".ts", ".m4s", ".key", ".aac", ".mp4"]):
                    logger.info(f"[SNIFFER] DASH ignored: {req_url[:120]}")
                    return
                if ".m3u8" in low and not any(ext in low for ext in [".ts", ".m4s", ".key", ".aac", ".mp4", ".m4a"]):
                    if req_url in _skip_urls:
                        logger.info(f"[SNIFFER] skip_url (previous ad): {req_url[:120]}")
                        return
                    is_ad_flag = _is_ad_url(req_url)
                    if is_ad_flag:
                        logger.info(f"[SNIFFER] ad manifest (kept for vid-compare): {req_url[:120]}")
                    m, md = _classify_hls_by_url(req_url)
                    for c in candidates:
                        if c.get("url") == req_url:
                            if m: c["is_master"] = True
                            if md: c["is_media"] = True
                            return
                    candidates.append({
                        "url": req_url, "type": "m3u8",
                        "cache_control": "", "ct": "",
                        "is_master": m, "is_media": md,
                        "is_ad": is_ad_flag, "ts": time.time(),
                    })
                elif any(domain in req_url for domain in ["youtube.com/embed", "youtu.be", "dailymotion.com/embed", "vimeo.com/video"]):
                    for c in candidates:
                        if c.get("url") == req_url and c.get("type") == "embed":
                            return
                    candidates.append({
                        "url": req_url, "type": "embed",
                        "cache_control": "", "ct": "",
                        "is_master": False, "is_media": False, "ts": time.time(),
                    })

            def handle_response(response):
                nonlocal master_seen_ts
                try:
                    ct = response.headers.get("content-type", "").lower()
                    cc = response.headers.get("cache-control", "") or ""
                    url = response.url
                    low = url.lower()
                except Exception:
                    return
                if any(ext in low for ext in [".ts", ".m4s", ".key", ".aac", ".mp4", ".m4a"]):
                    return
                if not ("mpegurl" in ct or "m3u8" in ct):
                    return
                if url in _skip_urls:
                    logger.info(f"[SNIFFER] skip_url (previous ad): {url[:120]}")
                    return
                is_ad_flag = _is_ad_url(url)
                if is_ad_flag:
                    logger.info(f"[SNIFFER] ad response (kept for vid-compare): {url[:120]}")
                body_head = ""
                try:
                    raw = response.body()
                    body_head = raw[:500].decode("utf-8", errors="ignore")
                except Exception:
                    pass
                is_master = "#EXT-X-STREAM-INF" in body_head
                is_media = ("#EXTINF" in body_head) and not is_master
                target = None
                for c in candidates:
                    if c.get("url") == url:
                        target = c
                        break
                if target is None:
                    target = {
                        "url": url, "type": "m3u8",
                        "cache_control": "", "ct": "",
                        "is_master": False, "is_media": False, "ts": time.time(),
                    }
                    candidates.append(target)
                if cc and not target.get("cache_control"):
                    target["cache_control"] = cc
                target["ct"] = ct
                if is_master:
                    target["is_master"] = True
                    if master_seen_ts == 0.0:
                        master_seen_ts = time.time()
                        logger.info(f"[SNIFFER] master manifest: {url[:120]}")
                elif is_media:
                    target["is_media"] = True
                    logger.info(f"[SNIFFER] media manifest: {url[:120]}")
                elif not target.get("is_master") and not target.get("is_media"):
                    target["is_unclassified"] = True

            page.on("request", handle_request)
            page.on("response", handle_response)

            try:
                logger.info(f"[SNIFFER] loading: {target_url}")
                page.goto(target_url, wait_until="domcontentloaded", timeout=IPTV_PLAYWRIGHT_NAVIGATION_TIMEOUT)
                page.wait_for_timeout(6000)
                for btn_text in COOKIE_PATTERNS:
                    if candidates:
                        break
                    try:
                        button = page.get_by_role("button", name=re.compile(btn_text, re.IGNORECASE))
                        if button.count() > 0 and button.first.is_visible():
                            button.first.click(timeout=1500)
                            page.wait_for_timeout(1500)
                            break
                    except Exception:
                        pass
                if not candidates:
                    selectors = ["video", "iframe", ".vjs-big-play-button", "[class*='player']"]
                    for sel in selectors:
                        elem = page.locator(sel)
                        if elem.count() > 0 and elem.first.is_visible():
                            elem.first.click(force=True, timeout=1000)
                            page.wait_for_timeout(1500)
                            if candidates:
                                break
                start_time = time.time()
                while True:
                    elapsed = time.time() - start_time
                    has_embed = any(c.get("type") == "embed" for c in candidates)
                    if has_embed:
                        break
                    if elapsed >= max_timeout:
                        break
                    page.wait_for_timeout(500)

                # sniffer-waitbody-v1: под нагрузкой (несколько параллельных
                # Chromium) response.body() не успевает прочитать тела манифестов
                # до этого момента. Кандидаты есть, но is_master/is_media у них
                # ещё не проставлены — тело не дочитано. Даём дополнительное
                # окно: пока есть unclassified m3u8-кандидаты, ждём до
                # WAIT_BODY_SEC. Это лечит ложные "no usable m3u8/embed found"
                # на живых каналах (Al Jazeera, NBC) при healthcheck-all.
                WAIT_BODY_SEC = 5
                wait_body_start = time.time()
                while True:
                    unclassified = [
                        c for c in candidates
                        if c.get("type") == "m3u8"
                        and not c.get("is_master") and not c.get("is_media")
                    ]
                    if not unclassified:
                        break
                    if time.time() - wait_body_start >= WAIT_BODY_SEC:
                        break
                    page.wait_for_timeout(500)

                for c in candidates:
                    if c.get("type") == "m3u8":
                        v = _extract_video_id(c.get("url", ""))
                        flags = []
                        if c.get("is_master"): flags.append("master")
                        if c.get("is_media"): flags.append("media")
                        if c.get("is_ad"): flags.append("AD")
                        logger.info(f"[SNIFFER] candidate vid={v!r} flags={flags}: {c['url'][:120]}")
                found_result = _pick_best_candidate(candidates, channel_name=None)
                if found_result and found_result["type"] == "m3u8":
                    cookies = context.cookies()
                    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies if c.get('value')])
                    final_url = found_result["url"]
                    final_url += f"|Referer={target_url}"
                    if cookie_str:
                        final_url += f"|Cookie={cookie_str}"
                    final_url += f"|User-Agent={user_agent}"
                    found_result["url"] = final_url
            except Exception as e:
                logger.warning(f"[SNIFFER] parse failed: {target_url}: {e}")
    finally:
        try:
            if page: page.close()
        except Exception: pass
        try:
            if context: context.close()
        except Exception: pass
        try:
            if browser: browser.close()
        except Exception: pass
        try:
            while True:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            pass
        except Exception:
            pass
    return found_result


def resolve_via_browser_sniffer(target_url: str, ua: str = None, max_timeout: int = 15) -> Optional[Dict[str, str]]:
    skip_urls = set()
    ua_final = ua or IPTV_DEFAULT_UA
    for attempt in range(2):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _run_browser_sniffer_sync, target_url, ua_final, max_timeout, skip_urls
            )
            res = future.result()
        if not res:
            # sniffer-retry-v1: не выходим сразу — второй attempt часто
            # проходит (сайт успевает ответить, Chromium стартует чище).
            if attempt == 0:
                logger.info(f"[SNIFFER] attempt 1 returned nothing, retry: {target_url}")
                continue
            logger.warning(f"[SNIFFER] both attempts returned nothing: {target_url}")
            return None
        if res.get("type") != "m3u8":
            return res
        if res.get("is_master"):
            return res
        payload = res.get("url", "")
        if _is_ad_manifest(payload):
            clean = payload.split("|")[0]
            logger.info(f"[SNIFFER] ad detected (attempt {attempt + 1}), retry: {clean[:120]}")
            skip_urls.add(clean)
            continue
        return res
    logger.warning(f"[SNIFFER] all attempts returned ad: {target_url}")
    return None
