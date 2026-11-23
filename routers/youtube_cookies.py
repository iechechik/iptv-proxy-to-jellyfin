"""Cookies YouTube: статус профилей, сохранение с проверкой, выбор активного."""
import re
import subprocess

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.config import logger
import core.state as state
import services.yt_cookies as store

router = APIRouter()


def _pick_test_url():
    """Берём YouTube-ссылку одного из каналов проекта — проверяем ровно то,
    что сервису и нужно от cookies."""
    try:
        for ch in state.load_channels():
            for s in (ch.get("streams") or []):
                url = (s.get("url") or "").strip()
                if "youtube.com/" in url and ("watch" in url or "/live/" in url):
                    return url
    except Exception as e:
        logger.warning(f"[YT-COOKIES] не удалось выбрать канал для проверки: {e}")
    return None


def _run_test(path: str):
    url = _pick_test_url()
    if not url:
        return None, "нет ни одного YouTube-канала в конфиге — проверка пропущена"
    cmd = [
        "yt-dlp", "-J", "--no-warnings", "--no-playlist",
        "--socket-timeout", "15", "--cookies", path, url,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=70)
    except subprocess.TimeoutExpired:
        return False, "проверка не уложилась в 70 секунд"
    if res.returncode == 0:
        return True, "cookies рабочие: yt-dlp взял поток"
    err = (res.stderr or "").strip().split("\n")[-1]
    if "Sign in to confirm" in err:
        return False, "Google требует вход: cookies не приняты (или истекли)"
    if "confirm your age" in err:
        return False, "YouTube требует подтвердить возраст — нужен возрастной аккаунт"
    return False, (err or "yt-dlp не смог получить поток")[:180]


@router.get("/youtube/cookies")
def youtube_cookies_status():
    try:
        data = store.status()
        return JSONResponse({"success": True, **data})
    except Exception as e:
        logger.error(f"[YT-COOKIES] status failed: {e}")
        return JSONResponse({"success": False, "detail": str(e)}, status_code=500)


@router.post("/youtube/cookies/save")
async def youtube_cookies_save(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "detail": "тело запроса должно быть JSON"}, status_code=400)
    name = (body.get("name") or "").strip()
    text = body.get("text") or ""
    if not name:
        return JSONResponse({"success": False, "detail": "укажите имя профиля"}, status_code=400)
    ok, detail = store.save(name, text)
    if not ok:
        return JSONResponse({"success": False, "detail": detail}, status_code=400)
    path = store.profile_path(name)
    test_ok, test_detail = _run_test(path)
    if test_ok is not None:
        store.set_check(name, test_ok, test_detail)
    logger.info(f"[YT-COOKIES] '{name}': сохранение + проверка -> {test_ok} / {test_detail[:100]}")
    return JSONResponse({
        "success": bool(test_ok) if test_ok is not None else True,
        "saved": True,
        "checked": test_ok is not None,
        "check_ok": test_ok,
        "detail": test_detail,
    })


@router.post("/youtube/cookies/check")
async def youtube_cookies_check(request: Request):
    """Проверить уже сохранённый профиль (без повторной вставки cookies)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "detail": "тело запроса должно быть JSON"}, status_code=400)
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"success": False, "detail": "укажите имя профиля"}, status_code=400)
    try:
        path = store.profile_path(name)
    except ValueError as e:
        return JSONResponse({"success": False, "detail": str(e)}, status_code=400)
    if not __import__("os").path.exists(path):
        return JSONResponse({"success": False, "detail": "профиль не найден"}, status_code=404)
    ok, detail = _run_test(path)
    if ok is not None:
        store.set_check(name, ok, detail)
    logger.info(f"[YT-COOKIES] '{name}': проверка -> {ok} / {(detail or '')[:100]}")
    return JSONResponse({"success": bool(ok), "check_ok": ok, "detail": detail})


@router.post("/youtube/cookies/select")
async def youtube_cookies_select(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "detail": "тело запроса должно быть JSON"}, status_code=400)
    ok, detail = store.select(body.get("name") or "")
    return JSONResponse({"success": ok, "detail": detail}, status_code=200 if ok else 400)


@router.post("/youtube/cookies/delete")
async def youtube_cookies_delete(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "detail": "тело запроса должно быть JSON"}, status_code=400)
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"success": False, "detail": "укажите имя профиля"}, status_code=400)
    ok, detail = store.delete(name)
    return JSONResponse({"success": ok, "detail": detail})
