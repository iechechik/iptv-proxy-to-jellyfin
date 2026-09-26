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
import concurrent.futures
from typing import Optional, Dict
from playwright.sync_api import sync_playwright

from core.config import (
    IPTV_RESOLVER_ORDER, IPTV_DEFAULT_UA,
    IPTV_CACHE_TTL, IPTV_FAST_CACHE_TTL, IPTV_FETCH_TIMEOUT,
    IPTV_YOUTUBE_CACHE_TTL, IPTV_STREAMLINK_TIMEOUT,
    IPTV_FLARESOLVERR_TIMEOUT, IPTV_FLARESOLVERR_URL,
    IPTV_PLAYWRIGHT_CHROMIUM, IPTV_PLAYWRIGHT_NAVIGATION_TIMEOUT,
    logger
)
import core.config as cfg

COOKIE_PATTERNS = ["Accept", "Accept all", "Agree", "Allow all", "Tout accepter", "Accepter", "Continue without accepting", "Continuer sans accepter"]

def is_youtube_url(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u or "youtube-nocookie.com" in u

def is_direct_stream(url: str) -> bool:
    direct_patterns = ['.m3u8', 'manifest', 'playlist', 'master.m3u8', 'index.m3u8', 'chunklist']
    return any(p in url.lower() for p in direct_patterns)

def extract_m3u8_from_text(text: str) -> str | None:
    match = re.search(r'https?://[^\s"\'<>]+?\.m3u8[^\s"\'<>]*', text)
    return match.group(0) if match else None

def parse_url_headers(raw_url: str):
    """
    Разбирает строку вида:
    'http://stream.m3u8|Referer=http://site.ru|Cookie=bm=1; pu=2|User-Agent=Mozilla/5.0...'
    Возвращает: (clean_url, dict_headers)
    """
    clean_url = raw_url.split("|")[0]
    headers = {}
    if "|" in raw_url:
        for part in raw_url.split("|")[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                headers[k] = v
    return clean_url, headers

# ---------- Умный TTL: спрашиваем CDN, а не гадаем ----------

# Пол для max-age=N. max-age=0/1/2 от CDN округляем вверх —
# иначе Jellyfin дёргал бы резолвер каждые пару секунд.
_MIN_CDN_TTL = 30

# Явные значения для директив, где max-age не участвует.
_TTL_NO_STORE = 30
_TTL_NO_CACHE = 60
_TTL_MUST_REVALIDATE = 60

# Верхний потолок для не-direct методов. config-ручка IPTV_FAST_CACHE_TTL
# сохраняет смысл: пользователь может опустить потолок ниже, но поднять
# выше 600 нельзя — подписанные URL живут короче, чем «стабильные»,
# и over-cache здесь не даёт ничего, кроме риска отдать мёртвую ссылку.
_FAST_CAP = min(IPTV_FAST_CACHE_TTL, 600)
# Cap на TTL для session-URL (подписанные ссылки живут короче
# стабильных, но перерезолвить каждую минуту — тоже перебор).
_SESSION_TTL_CAP = 300
# Cap на TTL из Cache-Control для sniffer'а: реальный max-age=3600
# уважаем, но не бесконечно.
_SNIFFER_HEAD_CAP = _FAST_CAP
# Fallback для sniffer, когда HEAD не дал Cache-Control.
# Меньше — Chromium запускается слишком часто.
# Подчиняется _FAST_CAP снизу: если юзер опустил fast_cache_ttl ниже 180,
# уважаем его настройку.
_SNIFFER_NO_INFO_TTL = min(_FAST_CAP, 180)

# Если URL содержит эти маркеры — считаем его сессионным и идём к CDN
# за Cache-Control. Для остальных URL берём дефолт по методу (без запроса).
_SESSION_URL_MARKERS = (
    ".php", ".asp", ".aspx", ".jsp",
    "wmsauthsign=", "nimblesessionid=",
    "phpsessid=", "session=", "token=", "auth=",
)

# Явные expiration-токены внутри URL. Приоритетнее Cache-Control:
# даже если CDN говорит max-age=3600, а токен истекает через 5 минут,
# мы обязаны резать TTL по токену.
#
# Форматы, которые встречались в проекте:
#   wmsAuthSign=<hex>-<ts>-<suffix>              (....)
#   ?expire=<ts>&...                             (my.mail.ru, vk)
#   expire_at=<ts>                               (my.mail.ru)
#   expire/<ts>/                                 (googlevideo.com)
#   ?exp=<ts>                                    (разные CDN)
_URL_EXPIRY_PATTERNS = (
    re.compile(r"[?&]expire=(\d{10})", re.IGNORECASE),
    re.compile(r"[?&]expires=(\d{10})", re.IGNORECASE),
    re.compile(r"[?&]expire_at=(\d{10})", re.IGNORECASE),
    re.compile(r"/expire/(\d{10})/", re.IGNORECASE),
    re.compile(r"[?&]exp=(\d{10})", re.IGNORECASE),
)

# Запас: не отдаём клиенту URL, который вот-вот протухнет.
_EXPIRY_SAFETY_MARGIN = 30


def _is_session_url(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in _SESSION_URL_MARKERS)


def _extract_url_expiry(url: str) -> int | None:
    """Возвращает UNIX-timestamp из URL, если там есть явный expiration.
    Иначе None."""
    for pat in _URL_EXPIRY_PATTERNS:
        m = pat.search(url)
        if m:
            try:
                ts = int(m.group(1))
            except (TypeError, ValueError):
                continue
            # Грубая защита от мусора: timestamp должен быть в разумных пределах
            # (2000-01-01 .. 2100-01-01).
            if 946684800 <= ts <= 4102444800:
                return ts
    return None

def _parse_cache_control_ttl(cache_control: str, default_ttl: int) -> int:
    """TTL в секундах на основе Cache-Control.

    Приоритет директив (первое сработавшее побеждает):
      no-store                → _TTL_NO_STORE (30)
      no-cache                → _TTL_NO_CACHE (60)
      s-maxage=N              → min(max(N, _MIN_CDN_TTL), default_ttl)
      max-age=N               → min(max(N, _MIN_CDN_TTL), default_ttl)
      must-revalidate         → _TTL_MUST_REVALIDATE (60)
      иначе                   → default_ttl
    """
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

DEFAULT_TTL_BY_METHOD = {
    "direct": IPTV_CACHE_TTL,
    "yt-dlp": _FAST_CAP,
    "streamlink": _FAST_CAP,
    "sniffer": _FAST_CAP,
    "flaresolverr_simple": _FAST_CAP,
    "flaresolverr_session": _FAST_CAP,
}

def _fetch_cache_control(clean_url: str, headers: dict, timeout: int = 2) -> str:
    """HEAD, при 405/501 — GET Range: bytes=0-0. Возвращает Cache-Control или ''."""
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
        # 405/501 — метод не поддержан, пробуем GET.
        # 401/403 — и GET вернёт то же, экономим 2 секунды.
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
    """
    Timestamp, до которого payload считается валидным.

    Порядок вычисления (первое сработавшее выигрывает):
    1. expire/ в URL           → ts - now - 30 (hard cap, без min с default)
    2. cache_control передан   → _parse(cc, cap): cap=600 для sniffer, иначе default
    3. is_session_url          → HEAD → _parse(cc, 300); HEAD fail → 300
    4. не direct, 1-3 пусто    → HEAD → _parse(cc, default); HEAD fail →
                                 180 для sniffer, иначе default
    5. direct                  → default_ttl, без HEAD
    """
    default_ttl = DEFAULT_TTL_BY_METHOD.get(method, 600)

    if not payload:
        return time.time() + default_ttl

    # Синтетический манифест — берём первую не-комментарийную строку
    if payload.startswith("#EXTM3U"):
        for line in payload.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                payload = line
                break

    clean_url, headers = parse_url_headers(payload)
    if not clean_url.startswith("http"):
        return time.time() + default_ttl

    # 1) Явный expiration в URL — hard cap, приоритетнее Cache-Control
    #    и НЕ режется default_ttl: подпись знает свой срок лучше нас.
    #    Работает для ?expire=<ts>, /expire/<ts>/, ?exp=, ?expire_at=.
    #    НЕ работает для wmsAuthSign (там timestamp выпуска, не expiration).
    url_exp = _extract_url_expiry(clean_url)
    if url_exp is not None:
        left = url_exp - time.time() - _EXPIRY_SAFETY_MARGIN
        if left <= 0:
            return time.time() + 5
        return time.time() + int(left)

    # 2) Cache-Control, перехваченный sniffer'ом из браузера.
    #    Доверяем, но не больше cap'а: для sniffer — 600, для остальных — default.
    if cache_control is not None:
        cap = _SNIFFER_HEAD_CAP if method == "sniffer" else default_ttl
        ttl = _parse_cache_control_ttl(cache_control, cap)
        # Sniffer и «max-age=0 / no-cache» от CDN: это директива
        # «перепроверяй при каждом запросе», а не «URL сдох через 0 сек».
        # Мы только что перепроверили через Chromium и получили свежие
        # URL+куки. Гонять Chromium каждые 30 секунд дорого: пока он
        # работает, /redirect висит 5-7 сек, Jellyfin ждёт манифест,
        # буфер пустеет — плеер залипает (спиннер/треугльник/спиннер).
        # Если в самом URL нет expire=/token=/session=, поднимаем пол
        # до _SNIFFER_NO_INFO_TTL (180 сек).
        if method == "sniffer" and ttl < _SNIFFER_NO_INFO_TTL:
            clean_url, _ = parse_url_headers(payload)
            if _extract_url_expiry(clean_url) is None and not _is_session_url(clean_url):
                ttl = _SNIFFER_NO_INFO_TTL
        return time.time() + ttl

    # 3) Session URL — спрашиваем CDN, но жёстко ограничиваем сверху.
    #    Подписанные ссылки (token=, .php, wmsauthsign=) живут короче.
    if _is_session_url(clean_url):
        cc = _fetch_cache_control(clean_url, headers, timeout=2)
        if cc:
            return time.time() + _parse_cache_control_ttl(cc, _SESSION_TTL_CAP)
        return time.time() + _SESSION_TTL_CAP

    # 4) Не-direct без явных правил — HEAD с потолком default_ttl.
    #    Для sniffer это шанс узнать реальный Cache-Control вместо слепых 180.
    if method != "direct":
        cc = _fetch_cache_control(clean_url, headers, timeout=2)
        if cc:
            return time.time() + _parse_cache_control_ttl(cc, default_ttl)
        if method == "sniffer":
            return time.time() + _SNIFFER_NO_INFO_TTL
        return time.time() + default_ttl

    # 5) direct — не мучаем CDN.
    return time.time() + default_ttl

def resolve_streamlink(url: str) -> str:
    cmd = [
        "streamlink",
        "--stream-url",
        "--retry-streams", "3",
        "--stream-segment-attempts", "3",
        "--stream-timeout", "30",
        url,
        "best"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=IPTV_STREAMLINK_TIMEOUT)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError(result.stderr.strip() or "Streamlink вернул пустой результат")

# Флаг-файл. Watchdog-логика сведена к минимуму:
#   - при вызове flare-метода проверяем: файл есть → падаем сразу (значит
#     host-скрипт уже собирается рестартнуть FlareSolverr);
#   - при любой ошибке метода ставим файл; host-скрипт его видит,
#     рестартует flare, удаляет файл.
# Никаких пингов, счётчиков, фоновых потоков. Всё состояние — файл на диске.
def _flare_flag_path() -> str:
    """Путь к flag-файлу из config. Пусто — механизм выключен."""
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
    # Если флаг уже стоит — не перезаписываем. Файл снимает host-скрипт
    # после рестарта FlareSolverr. Многократные ошибки от параллельных
    # воркеров не должны спамить записью на диск.
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
            "cmd": "request.get",
            "url": page_url,
            "maxTimeout": 60000
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
        # Хост-скрипт собирается рестартнуть FlareSolverr — падаем сразу.
        raise RuntimeError("FlareSolverr restart pending (flag file)")

    # uuid вместо int(time.time()) — иначе два параллельных резолва
    # в одну секунду ловят коллизию session_id и FlareSolverr отдаёт ошибку.
    session_id = f"proxy_session_{uuid.uuid4().hex[:12]}"
    try:
        # 1) Создаём сессию. Проверяем ответ: если FlareSolverr лежит
        #    или не может создать сессию — выходим сразу, не ждём зря.
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

        # 2) ПЕРВЫЙ request.get — прогрев Cloudflare-челленджа.
        #    Ответ НЕ используется, важен только побочный эффект:
        #    FlareSolverr решает JS-челлендж и выставляет cf_clearance
        #    в сессию. Без этого второй запрос вернёт страницу-заглушку
        #    («Just a moment...»), а не реальный HTML.
        get_payload = json.dumps({
            "cmd": "request.get",
            "session": session_id,
            "url": target_url,
            "maxTimeout": timeout * 1000
        }).encode()

        req_get = urllib.request.Request(
            IPTV_FLARESOLVERR_URL,
            data=get_payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
#       urllib.request.urlopen(req_get, timeout=timeout + 5)
        with urllib.request.urlopen(req_get, timeout=timeout + 5) as warm_resp:
            warm_resp.read()

        # 3) Пауза — даём Cloudflare доиграть редирект и кукам устояться.
        #    Эмпирически подобрано; короче — челлендж не успевает закрыться.
        time.sleep(6)

        # 4) ВТОРОЙ request.get — теперь с установленными cookies
        #    получаем реальный контент страницы.
        with urllib.request.urlopen(req_get, timeout=timeout + 5) as resp:
            res = json.loads(resp.read())

        if res.get("status") != "ok":
            logger.warning(f"[FLARESOLVERR] request.get (retry) status={res.get('status')}: {res.get('message')}")
            return None

        solution = res.get("solution", {})
        cookies = solution.get("cookies", [])
        cookie_str = "; ".join([
            f"{c['name']}={c['value']}"
            for c in cookies
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
                "cmd": "request.get",
                "session": session_id,
                "url": iframe_url,
                "maxTimeout": timeout * 1000
            }).encode()
            req_iframe = urllib.request.Request(
                IPTV_FLARESOLVERR_URL,
                data=iframe_payload,
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

def _run_browser_sniffer_sync(target_url: str, ua: str, max_timeout: int) -> Optional[Dict[str, str]]:
    found_result: Optional[Dict[str, str]] = None
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
                    # Базовые для работы в docker
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    # Отключаем всё, что не нужно в headless:
                    # GPU (рендер в CPU всё равно), расширения,
                    # фоновые таймеры, восстановление после краша,
                    # сжатие картинки в фоне, автоматизационные флаги.
                    # Даёт ~20-30% экономии CPU и памяти на каждом
                    # запуске Chromium без влияния на результат.
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints",
                    "--disable-crash-reporter",
                    "--disable-breakpad",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-sync",
                    "--disable-default-apps",
                    "--metrics-recording-only",
                    "--mute-audio",
                ]
            }
            if os.path.exists(exec_path):
                launch_args["executable_path"] = exec_path

            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1280, "height": 720},
                locale="en-US"
            )
            page = context.new_page()


            def handle_request(request):
                nonlocal found_result
                req_url = request.url
                low = req_url.lower()
                if found_result:
                    return
                # DASH (.mpd) игнорируем: наш прокси умеет только HLS.
                if ".mpd" in low and not any(ext in low for ext in [".ts", ".m4s", ".key", ".aac", ".mp4"]):
                    logger.info(f"[SNIFFER] DASH ignored: {req_url[:120]}")
                    return
                if ".m3u8" in low and not any(ext in low for ext in [".ts", ".m4s", ".key", ".aac", ".mp4"]):
                    found_result = {"type": "m3u8", "url": req_url}
                elif any(domain in req_url for domain in ["youtube.com/embed", "youtu.be", "dailymotion.com/embed", "vimeo.com/video"]):
                    found_result = {"type": "embed", "url": req_url}

            def handle_response(response):
                # Перехват HLS-манифестов по Content-Type.
                nonlocal found_result
                try:
                    ct = response.headers.get("content-type", "").lower()
                    cc = response.headers.get("cache-control", "") or ""
                    url = response.url
                    low = url.lower()
                except Exception:
                    return

                # Никогда не берём сегменты за манифест
                if any(ext in low for ext in [".ts", ".m4s", ".key", ".aac", ".mp4", ".m4a"]):
                    return

                if not ("mpegurl" in ct or "m3u8" in ct):
                    return

                # Случай 1: URL уже известен из handle_request — обновляем cc.
                # Это типичный сценарий для tvcdnpotok.com/594/index.m3u8:
                # handle_request сработал раньше, увидел .m3u8 в URL,
                # поставил found_result без cc. Теперь ловим настоящий ответ
                # и записываем Cache-Control от CDN.
                if found_result is not None:
                    if (found_result.get("type") == "m3u8"
                            and found_result.get("url") == url
                            and not found_result.get("cache_control")):
                        found_result["cache_control"] = cc
                        logger.info(f"[SNIFFER] cache-control intercepted: {url} (cc={cc!r})")
                    return

                # Случай 2: раньше не находили (URL без .m3u8) — ставим found_result.
                found_result = {"type": "m3u8", "url": url, "cache_control": cc}
                logger.info(f"[SNIFFER] HLS by content-type: {url} (ct={ct}, cc={cc!r})")

            page.on("request", handle_request)
            page.on("response", handle_response)

            try:
                logger.info(f"[SNIFFER] loading: {target_url}")
                page.goto(target_url, wait_until="domcontentloaded", timeout=IPTV_PLAYWRIGHT_NAVIGATION_TIMEOUT)

                # Пауза 6 секунд для прохождения HostiMan / JS-челленджей
                page.wait_for_timeout(6000)

                for btn_text in COOKIE_PATTERNS:
                    if found_result:
                        break
                    try:
                        button = page.get_by_role("button", name=re.compile(btn_text, re.IGNORECASE))
                        if button.count() > 0 and button.first.is_visible():
                            button.first.click(timeout=1500)
                            page.wait_for_timeout(1500)
                            break
                    except Exception:
                        pass

                if not found_result:
                    selectors = ["video", "iframe", ".vjs-big-play-button", "[class*='player']"]
                    for sel in selectors:
                        elem = page.locator(sel)
                        if elem.count() > 0 and elem.first.is_visible():
                            elem.first.click(force=True, timeout=1000)
                            page.wait_for_timeout(1500)
                            if found_result:
                                break

                start_time = time.time()
                while not found_result and (time.time() - start_time) < max_timeout:
                    page.wait_for_timeout(500)

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
        # Закрываем всё в правильном порядке, не боясь, что что-то уже закрыто
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass

        # Дополнительная защита от зомби-процессов
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run_browser_sniffer_sync, target_url, ua or IPTV_DEFAULT_UA, max_timeout)
        try:
            return future.result(timeout=max_timeout + 5)
        except concurrent.futures.TimeoutError:
            logger.warning(f"[SNIFFER] timeout: {target_url}")
            return None

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
            # Возвращаем sniffer, так как именно он нашёл embed
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

# Таблица доступных методов резолвинга
# Каждый метод принимает (ch: dict) и возвращает (is_direct, payload, expire_time, method_name)
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
    """Проверяет, является ли имя резолвера допустимым (включая 'auto')."""
    return name in RESOLVERS or name == "auto"

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
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Range": "bytes=0-0"
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
            # 5xx — CDN не отдаёт поток. Считать живым = Jellyfin будет
            # бесконечно переподключаться к мёртвому URL.
            logger.warning(f"[PROBE] CDN returned {e.code}: {clean_url[:60]}...")
            return False
        return True
    except Exception as e:
        logger.warning(f"[PROBE] could not reach stream CDN: {e}")
        return False

def resolve_channel_payload(ch):
    # .get() вместо индексации — защита от вызова без ua/fs_regex.
    # Сейчас все вызывающие места кладут эти ключи, но жёсткое
    # обращение делает функцию ловушкой на будущее.
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
    # Единый префикс для логов: [PROBE] 'channel': msg  или  [PROBE] msg,
    # если channel не передан (тогда префикс пустой, а не None).
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

    # Время начала выполнения ffprobe
    start_time = time.time()

    # Для YouTube/Google Video используем прямой ffprobe с заголовками
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
        # Для YouTube добавляем обязательные заголовки, только если их ещё нет
        if "Referer:" not in headers_str:
            headers_str += "Referer: https://www.youtube.com/\r\n"
        if "Origin:" not in headers_str:
            headers_str += "Origin: https://www.youtube.com\r\n"
        if headers_str:
            cmd += ["-headers", headers_str]
        cmd.append(clean_url)

        logger.info(f"[PROBE] {ch_pfx}direct ffprobe for YouTube: {clean_url[:120]}...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
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

    # Для остальных потоков: 
    # если URL сам является HLS/DASH-манифестом — напрямую
    # Если это php/asp-обёртка (target=..., wms.php, players/...) — через наш прокси: ffprobe не умеет разбирать
    url_path_lower = urllib.parse.urlsplit(clean_url).path.lower()
    is_media_direct = url_path_lower.endswith((".m3u8", ".mpd", ".ts", ".mp4"))

    if is_media_direct:
        cmd = ["ffprobe", "-v", "error",
               "-show_entries", "stream=codec_type,codec_name", "-of", "json",
               "-timeout", str((timeout + 5) * 1000000)]
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
        # Обёртка. Идём через наш прокси — он умеет разбирать php/asp.
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
               "-timeout", str((timeout + 5) * 1000000),
               proxy_url]
        logger.info(f"[PROBE] {ch_pfx}ffprobe via proxy: {proxy_url[:120]}...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
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
