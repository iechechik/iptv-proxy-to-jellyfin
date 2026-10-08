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
from services.hls_utils import (
    _is_ad_url, _extract_url_expiry, _EXPIRY_SAFETY_MARGIN,
    probe_stream_types,
)
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


def _master_audio_uri(text: str, master_url: str) -> str:
    """URI аудио-группы master'а (абсолютный), или ""."""
    from urllib.parse import urljoin
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in s:
            m = re.search(r'URI="([^"]+)"', s)
            if m:
                return urljoin(master_url, m.group(1))
    return ""


def _master_variants(text: str, master_url: str):
    """[(bandwidth, абсолютный URI), ...] для #EXT-X-STREAM-INF."""
    from urllib.parse import urljoin
    out = []
    lines = [l.strip() for l in text.splitlines()]
    for i, s in enumerate(lines):
        if s.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt.startswith("#") or not nxt:
                continue
            bw = re.search(r"BANDWIDTH=(\d+)", s)
            out.append((int(bw.group(1)) if bw else 0, urljoin(master_url, nxt)))
    return out


def _leaf_key(url: str) -> str:
    """Имя листового плейлиста в URL (live-h264-720.m3u8, live-aac-128.m3u8…).

    Сопоставлять master и кандидатов по ПОЛНОМУ URL нельзя: master ссылается на
    варианты относительными путями и с якорем `#cell=...`, а браузер запрашивает
    их уже с signed-токеном. Имя файла (без query и якоря) — устойчивый признак
    одной и той же дорожки."""
    if not url:
        return ""
    u = url.split("|", 1)[0]
    u = u.split("#", 1)[0].split("?", 1)[0]
    return u.rsplit("/", 1)[-1].lower()


def _build_synthetic_from_master(master_candidate: dict, candidates: list,
                                 ua: str = None, referer: str = None, cookie: str = None):
    """Собирает синтетический манифест из листьев master'а: видео + аудио.

    Зачем: master нужен только чтобы достать листья, а его собственную ссылку
    часть CDN отдаёт через раз (403 на повторную загрузку). Листья — те же
    плейлисты, что играет браузер, и они отдаются стабильно.
    Ссылки берём ИЗ КАНДИДАТОВ (то, что браузер уже скачал) — со своими токенами.

    Решение принимается ПРОВЕРКОЙ листьев, а не по именам файлов:
      * видео-лист должен нести видео и НЕ нести аудио (если несёт — синтетика не
        нужна: master играет одним входом);
      * аудио-лист обязан реально нести аудио (в master'е аудио-группа иногда
        указывает на что угодно);
      * имена служат только подсказкой при выборе кандидатов.
    Возвращает строку-манифест или None.
    """
    audio = master_candidate.get("audio_uri") or ""
    variants = master_candidate.get("variants") or []
    seen = {}
    for c in candidates:
        if c.get("type") == "m3u8":
            k = _leaf_key(c.get("url", ""))
            if k:
                seen.setdefault(k, c.get("url", ""))
    if not variants or not seen:
        return None
    # Синтетику собираем ТОЛЬКО когда имена листьев явно говорят, кто есть кто
    # (`…-aac-128.m3u8` — аудио, `…-h264-720.m3u8` — видео). Для smotrim/uplift
    # имена вида `track_101_…/chunklist.m3u8` не различимы по имени — там master
    # разбирает сам mux, и подмена листьев ломала воспроизведение.
    _audio_hint = master_candidate.get("audio_uri") or ""
    if _track_marker(_audio_hint) != "audio" and not any(_track_marker(u) == "audio" for u in seen.values()):
        logger.info("[SNIFFER] no synthetic: имена листьев не различают аудио/видео")
        return None

    # --- аудио ---
    audio_url = seen.get(_leaf_key(audio)) if audio else None
    if audio_url and _track_marker(audio_url) == "video":
        audio_url = None
    if not audio_url:
        _cand_audio = [u for k, u in seen.items() if _track_marker(u) == "audio"]
        if not _cand_audio:
            logger.info("[SNIFFER] no synthetic: аудио-дорожки нет среди запросов браузера")
            return None
        audio_url = _cand_audio[-1]
        logger.info(f"[SNIFFER] synthetic: аудио-лист берём из запросов браузера: {_leaf_key(audio_url)}")
    audio_key = _leaf_key(audio_url)

    # --- видео ---
    video = None
    bw = 0
    for _bw, uri in sorted(variants, key=lambda x: -x[0]):
        k = _leaf_key(uri)
        if not k or k == audio_key:
            continue
        got = seen.get(k)
        if got and _track_marker(got) == "video":
            video, bw = got, _bw
            break
    if not video:
        logger.info("[SNIFFER] no synthetic: не нашли видео-вариант среди запросов браузера")
        return None

    # --- проверка листьев фактом, а не именем ---
    video_types = probe_stream_types(video, ua=ua, referer=referer, cookie=cookie)
    if "video" not in video_types:
        logger.info("[SNIFFER] no synthetic: видео-лист не подтвердился пробой")
        return None
    if "audio" in video_types:
        logger.info("[SNIFFER] no synthetic: видео-лист уже несёт аудио (master играет одним входом)")
        return None
    if "audio" not in probe_stream_types(audio_url, ua=ua, referer=referer, cookie=cookie):
        logger.info("[SNIFFER] no synthetic: аудио-лист не подтвердился пробой")
        return None

    return (
        "#EXTM3U\n"
        f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="audio",DEFAULT=YES,URI="{audio_url}"\n'
        f"#EXT-X-STREAM-INF:BANDWIDTH={bw}\n"
        f"{video}\n"
    )


_AUDIO_URL_MARKERS = ("aac", "audio", "opus", "mp3")
_VIDEO_URL_MARKERS = ("h264", "h265", "hevc", "avc", "video")


def _track_marker(url: str) -> str:
    """Что за дорожка в URL: "video", "audio" или "" (не понять по имени)."""
    if not url:
        return ""
    low = url.lower().split("|", 1)[0]
    fname = low.rsplit("/", 1)[-1]
    if any(m in fname for m in _VIDEO_URL_MARKERS):
        return "video"
    if any(m in fname for m in _AUDIO_URL_MARKERS):
        return "audio"
    return ""


def _is_track_url(url: str) -> bool:
    """True для дорожки-рендиции: только видео или только аудио.

    Dailymotion-подобные плееры запрашивают дорожки отдельными плейлистами
    (`live-h264-720.m3u8`, `live-aac-128.m3u8`) вместо muxed-плейлиста. Отдать
    такую дорожку = канал без картинки (или без звука), поэтому при наличии
    master'а, который несёт все дорожки, она не должна его обыгрывать.
    """
    return bool(_track_marker(url))


def _candidate_expired(url: str) -> bool:
    """True, если у URL есть собственный exp-параметр и он уже истёк.

    signed-ссылки (`?hdnea=st=..~exp=..`) продолжают жить в DOM/кэше страницы
    после истечения: отдать такую ссылку наружу = гарантированный 403 в probe
    и ложный DOWN (наблюдали на Euronews: токен истёк за 10 минут до
    использования).
    """
    exp = _extract_url_expiry(url)
    if exp is None:
        return False
    return exp <= time.time() + _EXPIRY_SAFETY_MARGIN


def _candidate_usable(c: dict) -> bool:
    """Годится ли m3u8-кандидат: не реклама, без 4xx/5xx и токен не истёк.

    status=None (ответа ещё не видели) считаем годным — это случай, когда тело
    манифеста не успело прочитаться под нагрузкой, а URL рабочий.
    """
    if c.get("is_ad"):
        return False
    st = c.get("status")
    if isinstance(st, int) and st >= 400:
        return False
    return not _candidate_expired(c.get("url", ""))


def _pick_best_candidate(candidates: list, channel_name: str = None):
    tag = f"[{channel_name}] " if channel_name else ""
    for c in candidates:
        if c.get("type") == "embed":
            return c
    # A2: убран двойной отсев AD. Единственный критерий — флаг is_ad,
    # который проставляется в handle_request через _is_ad_url. Раньше был
    # второй фильтр по vid (сравнение ad_vids с _extract_video_id мастера),
    # но _extract_video_id для не-YouTube URL часто возвращает "" — и
    # AD-мастер проходил. Один надёжный фильтр вместо двух ненадёжных.
    # sniffer-candidate-quality-v1: кандидаты, которые браузер получил с ошибкой
    # (4xx/5xx) или у которых уже истёк собственный токен, наружу не отдаём —
    # иначе probe получает 403, и живой канал ложно уходит в DOWN.
    usable = [c for c in candidates
              if c.get("type") == "m3u8" and _candidate_usable(c)]
    for c in candidates:
        if c.get("type") == "m3u8" and not _candidate_usable(c):
            logger.info(
                f"[SNIFFER] {tag}candidate dropped (status={c.get('status')}, "
                f"ad={bool(c.get('is_ad'))}, "
                f"expired={_candidate_expired(c.get('url', ''))}): {c['url'][:100]}"
            )
    masters = [c for c in usable if c.get("is_master")]
    medias = [c for c in usable if c.get("is_media")]
    # Dailymotion-подобные плееры запрашивают дорожки отдельными плейлистами
    # (`live-h264-720`, `live-aac-128`), и последней обычно идёт аудио — брать
    # её нельзя: master несёт все дорожки (sniffer-audio-fix-v1).
    tv_medias = [c for c in medias if _track_marker(c.get("url", "")) != "audio"]
    media_pool = tv_medias or medias
    filtered_masters = masters
    if filtered_masters and media_pool:
        m_host = _root_host(filtered_masters[-1].get("url", ""))
        # Среди media предпочитаем muxed/видео-плейлист, а не дорожку-рендицию.
        muxed = [c for c in media_pool
                 if not _is_track_url(c.get("url", ""))]
        chosen_media = muxed[-1] if muxed else None
        d_host = _root_host((chosen_media or media_pool[-1]).get("url", ""))
        if m_host and d_host and m_host != d_host:
            if chosen_media is not None:
                logger.info(f"[SNIFFER] {tag}master/media different CDN ({m_host} vs {d_host}) -> prefer media")
                logger.info(f"[SNIFFER] {tag}selected media (CDN mismatch): {chosen_media['url'][:120]}")
                return chosen_media
            logger.info(f"[SNIFFER] {tag}media candidates are single-track renditions -> keep master")
    if filtered_masters:
        chosen = filtered_masters[-1]
        logger.info(f"[SNIFFER] {tag}selected master: {chosen['url'][:120]}")
        return chosen
    if media_pool:
        chosen = media_pool[-1]
        logger.info(f"[SNIFFER] {tag}selected media (no live master): {chosen['url'][:120]}")
        return chosen
    for c in usable:
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
                # sniffer-embed-domains-v1: youtube-nocookie.com — тот же
                # YouTube-плеер, но подстрока "youtube.com/embed" в таком URL
                # не встречается (Al Jazeera отдаёт live именно так).
                elif any(domain in req_url for domain in [
                        "youtube.com/embed", "youtube-nocookie.com/embed",
                        "youtu.be", "dailymotion.com/embed", "vimeo.com/video"]):
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
                # sniffer-status-v1: фиксируем HTTP-статус ДО фильтра по
                # content-type — 403/404 приходят с text/html, и без этого
                # статус теряется, а битый URL остаётся «живым» кандидатом.
                if ".m3u8" in low:
                    for c in candidates:
                        if c.get("url") == url:
                            c["status"] = response.status
                            break
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
                body_text = ""
                try:
                    raw = response.body()
                    # sniffer-bodyfull-v1: классифицируем по всему манифесту, а не
                    # по первым 500 символам. У Dailymotion master начинается с
                    # длинного #EXT-X-MEDIA:TYPE=AUDIO..., и #EXT-X-STREAM-INF
                    # уезжал за 500-й символ — master попадал в «unclassified»,
                    # а payload'ом становилась отдельная дорожка (без картинки).
                    body_text = raw[:65536].decode("utf-8", errors="ignore")
                except Exception:
                    pass
                is_master = "#EXT-X-STREAM-INF" in body_text
                is_media = ("#EXTINF" in body_text) and not is_master
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
                    # sniffer-master-leaves-v1: запоминаем листья master'а
                    # (аудио-группа и варианты), чтобы отдать их вместо самого
                    # master'а — его ссылку CDN принимает через раз.
                    try:
                        _a = _master_audio_uri(body_text, url)
                        if _a:
                            target["audio_uri"] = _a
                        _v = _master_variants(body_text, url)
                        if _v:
                            target["variants"] = _v
                    except Exception:
                        pass
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
                if found_result and found_result["type"] == "m3u8" and found_result.get("is_master"):
                    # sniffer-master-leaves-v1: master нужен только чтобы достать
                    # листья (video/audio), а его собственную ссылку часть CDN
                    # отдаёт через раз (403 на повторную загрузку) — поэтому
                    # отдаём листья, а master не запрашиваем вообще.
                    # Решение принимает сборщик: он проверяет листья пробой и
                    # сам отказывается (None), если подмена не подтверждена —
                    # тогда master разбирает mux, как обычно.
                    _synth = _build_synthetic_from_master(
                        found_result, candidates,
                        ua=user_agent, referer=target_url,
                        cookie="; ".join([f"{c['name']}={c['value']}"
                                          for c in context.cookies() if c.get('value')]),
                    )
                    if _synth:
                        logger.info(
                            "[SNIFFER] master leaves -> synthetic payload "
                            f"(video+audio, master не нужен): {found_result['url'][:100]}"
                        )
                        found_result["url"] = _synth
                        found_result["is_synthetic"] = True
                if found_result and found_result["type"] == "m3u8" and not found_result.get("is_synthetic"):
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
