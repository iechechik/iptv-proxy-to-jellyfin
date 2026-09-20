from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
import asyncio
import os
import threading
import time as _time

from core.config import logger, log_handler
import core.state as state

router = APIRouter()
templates = Jinja2Templates(directory="web")

@router.post("/config/restart")
def restart_process():
    """Перезапускает uvicorn: PID 1 получает SIGTERM, docker поднимает
    контейнер заново (restart: unless-stopped). Отвечаем сразу,
    убиваем с задержкой — чтобы HTTP-ответ успел уехать клиенту."""
    logger.warning("[RESTART] container restart requested")

    def _delayed_exit():
        _time.sleep(1.5)
        os._exit(1)

    threading.Thread(target=_delayed_exit, daemon=True, name="restart-trigger").start()
    return JSONResponse({"success": True, "message": "Рестарт запущен"})


@router.get("/logs/data")
def get_logs_data():
    return JSONResponse({"logs": list(log_handler.buffer)})


@router.get("/logs/clear")
def clear_logs():
    log_handler.buffer.clear()
    logger.info("[LOGS] buffer cleared by user")
    return JSONResponse({"success": True})


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    return templates.TemplateResponse(request=request, name="logs.html", context={})


@router.get("/status")
def status():
    channels = state.load_channels()
    return JSONResponse({
        "epg_ready": True,
        "total_channels": len(channels),
        "matched_channels": len([c for c in channels if c.get("tvgid")])
    })


@router.get("/events")
async def sse_endpoint():
    queue = asyncio.Queue()
    with state._sse_lock:
        state._sse_clients.append(queue)

    async def event_generator():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield data
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with state._sse_lock:
                if queue in state._sse_clients:
                    state._sse_clients.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*"
        }
    )
