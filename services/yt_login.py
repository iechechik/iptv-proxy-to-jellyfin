"""Интерактивный вход в YouTube из UI.

Браузер живёт в отдельном потоке: Playwright sync-API нельзя дёргать из
разных потоков. Поток сам обновляет JPEG-кадр (его забирает UI) и сам
обрабатывает команды (клик, ввод, клавиша). Как только в браузере появляются
cookie авторизации, они сохраняются в активный профиль — учётные данные
приложению не нужны, они вводятся прямо в живой странице Google.
"""
import queue
import threading
import time

from core.config import IPTV_PLAYWRIGHT_CHROMIUM, logger
import services.yt_cookies as store

IDLE_LIMIT = 600          # сколько секунд живёт сессия без действий
SHOT_INTERVAL = 0.25      # как часто обновляется кадр
LOGIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
VIEWPORT = {"width": 1280, "height": 900}


def _as_netscape(cookies) -> str:
    """cookies из Playwright -> cookies.txt в формате Netscape."""
    lines = ["# Netscape HTTP Cookie File", "# saved by iptv-proxy UI login", ""]
    n = 0
    for c in cookies or []:
        domain = c.get("domain") or ""
        if not domain:
            continue
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires <= 0:
            expires = int(time.time()) + 30 * 86400
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        name = c.get("name") or ""
        value = c.get("value") or ""
        if not name:
            continue
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([prefix + domain, flag, path, secure, str(expires), name, value]))
        n += 1
    return ("\n".join(lines) + "\n") if n else ""


class LoginSession:
    def __init__(self):
        self._q = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._state = "idle"        # idle | running | done | error
        self._detail = ""
        self._shot = b""
        self._shot_at = 0.0
        self._touched = 0.0
        self._profile = ""
        self._url = ""

    # ---------- управление из HTTP-потока ----------
    def start(self, profile: str = "основной"):
        with self._lock:
            if self._state == "running":
                return True, "сессия уже идёт"
            self._state = "running"
            self._detail = "запускаю браузер…"
            self._shot = b""
            self._touched = time.time()
            self._profile = profile or "основной"
            self._thread = threading.Thread(target=self._run, daemon=True, name="yt-login")
            self._thread.start()
        logger.info(f"[YT-LOGIN] сессия входа открыта, профиль '{self._profile}'")
        return True, "ok"

    def stop(self):
        self._q.put(("stop", None))
        with self._lock:
            if self._state == "running":
                self._state = "idle"
                self._detail = "сессия закрыта"
        return True, "остановлено"

    def touch(self):
        self._touched = time.time()

    def push(self, cmd: str, payload=None):
        self.touch()
        self._q.put((cmd, payload))
        return True

    def status(self):
        with self._lock:
            return {
                "state": self._state,
                "detail": self._detail,
                "profile": self._profile,
                "url": self._url,
                "shot_at": self._shot_at,
                "has_screen": bool(self._shot),
            }

    def screen(self):
        with self._lock:
            return self._shot

    # ---------- поток браузера ----------
    def _run(self):
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    executable_path=IPTV_PLAYWRIGHT_CHROMIUM,
                    args=[
                        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                        f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}",
                    ],
                    ignore_default_args=["--enable-automation"],
                )
                ctx = browser.new_context(
                    user_agent=UA, viewport=VIEWPORT, locale="en-US",
                    timezone_id="Europe/Moscow",
                )
                page = ctx.new_page()
                self._set("running", "страница входа открыта")
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)

                saved = False
                last_shot = 0.0
                while True:
                    # команды
                    stop = False
                    for _ in range(20):
                        try:
                            cmd, payload = self._q.get_nowait()
                        except queue.Empty:
                            break
                        stop = stop or self._handle(page, cmd, payload)
                    if stop:
                        break
                    # авто-стоп по простою
                    if time.time() - self._touched > IDLE_LIMIT:
                        self._set("idle", "сессия закрыта по простою")
                        break
                    # cookies → успех
                    try:
                        cookies = ctx.cookies()
                    except Exception:
                        cookies = []
                    text = _as_netscape(cookies)
                    ok_fmt, why = store.validate(text) if text else (False, "нет cookies")
                    # partial-session-v1: на странице подтверждения Google
                    # cookies ещё неполные — сохранять рано.
                    try:
                        cur_url = page.url or ""
                    except Exception:
                        cur_url = ""
                    on_challenge = "/signin/challenge" in cur_url
                    if ok_fmt and on_challenge:
                        self._set("running", "ждём подтверждения Google")
                        ok_fmt = False
                    if ok_fmt:
                        ok, detail = store.save(self._profile, text)
                        if ok:
                            saved = True
                            self._set("done", f"вход выполнен, cookies сохранены в '{self._profile}'")
                            logger.info(f"[YT-LOGIN] cookies сохранены в профиль '{self._profile}' ({len(text)} Б)")
                            break
                        self._set("error", f"cookies получены, но не сохранились: {detail}")
                        break
                    # кадр
                    if time.time() - last_shot >= SHOT_INTERVAL:
                        last_shot = time.time()
                        try:
                            buf = page.screenshot(type="jpeg", quality=85, animations="disabled")
                            with self._lock:
                                self._shot = buf
                                self._shot_at = time.time()
                                self._url = page.url
                        except Exception:
                            pass
                    time.sleep(0.05)

                if not saved and self._state == "running":
                    self._set("idle", "сессия закрыта")
                browser.close()
        except Exception as e:
            logger.error(f"[YT-LOGIN] ошибка сессии: {e}")
            if isinstance(e, ValueError):
                self._set("error", f"не удалось сохранить cookies: {e}")
            else:
                self._set("error", f"{type(e).__name__}: {str(e)[:160]}")

    def _handle(self, page, cmd, payload) -> bool:
        try:
            if cmd == "stop":
                return True
            if cmd == "click" and payload:
                page.mouse.click(float(payload.get("x", 0)), float(payload.get("y", 0)))
            elif cmd == "move" and payload:
                page.mouse.move(float(payload.get("x", 0)), float(payload.get("y", 0)))
            elif cmd == "type" and payload:
                page.keyboard.type(str(payload.get("text", "")))
            elif cmd == "key" and payload:
                page.keyboard.press(str(payload.get("key", "")))
            elif cmd == "scroll" and payload:
                page.mouse.wheel(0, float(payload.get("dy", 0)))
        except Exception as e:
            logger.warning(f"[YT-LOGIN] команда {cmd} не выполнена: {e}")
        return False

    def _set(self, state: str, detail: str):
        with self._lock:
            self._state = state
            self._detail = detail

        return "/usr/bin/chromium"


session = LoginSession()
