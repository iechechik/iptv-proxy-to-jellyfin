"""Вход в YouTube из UI: старт сессии, кадр экрана, ввод, остановка."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import services.yt_login as login

router = APIRouter()



@router.post("/youtube/login/start")
async def youtube_login_start(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    profile = (body.get("profile") or "основной").strip()
    ok, detail = login.session.start(profile)
    return JSONResponse({"success": ok, "detail": detail})


@router.get("/youtube/login/status")
def youtube_login_status():
    return JSONResponse({"success": True, **login.session.status()})


@router.get("/youtube/login/screen")
def youtube_login_screen():
    data = login.session.screen()
    if not data:
        return Response(status_code=204)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/youtube/login/input")
async def youtube_login_input(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "detail": "нужен JSON"}, status_code=400)
    kind = (body.get("type") or "").strip()
    if kind in ("click", "move"):
        login.session.push(kind, {"x": body.get("x", 0), "y": body.get("y", 0)})
    elif kind == "scroll":
        login.session.push("scroll", {"dy": body.get("dy", 0)})
    elif kind == "type":
        text = body.get("text") or ""
        if text:
            login.session.push("type", {"text": text})
    elif kind == "key":
        key = body.get("key") or ""
        if key:
            login.session.push("key", {"key": key})
    else:
        return JSONResponse({"success": False, "detail": "неизвестный тип ввода"}, status_code=400)
    return JSONResponse({"success": True})


@router.post("/youtube/login/stop")
def youtube_login_stop():
    ok, detail = login.session.stop()
    return JSONResponse({"success": ok, "detail": detail})
