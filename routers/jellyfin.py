from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import threading
import time

import core.state as state
from core.config import logger
from services.healthcheck import run_healthcheck_async

router = APIRouter()


def _extract_channel_name(data: dict):
    """Извлекает название канала из Item.Name или Name."""
    item = data.get("Item") or {}
    name = item.get("Name") or data.get("Name")
    if isinstance(name, str):
        return name.strip()
    return None


@router.post("/webhooks/jellyfin")
async def jellyfin_webhook(request: Request):
    """Приёмник вебхуков от Jellyfin.

    Реагируем только на Stop. Логика:
      - событие не Stop → игнор;
      - канал не найден → 400;
      - у канала fallback выключен → игнор (переключаться некуда);
      - дедупликация: для этого канала уже есть активная задача → игнор;
      - иначе — фоновый healthcheck. Он сам решит, что делать:
        пропустит недавно проверенные (<60 сек) и не будет запускать
        probe, если канал активно смотрят.
    """
    try:
        data = await request.json()
        event_type = data.get("Event")

        if event_type != "Stop":
            return JSONResponse({"success": True, "message": "ignored"})

        channel_name = _extract_channel_name(data)
        if not channel_name:
            logger.warning("[WEBHOOK] could not determine channel name")
            return JSONResponse({"success": False, "error": "no channel name"})

        channels = state.load_channels()
        ch = next(
            (c for c in channels if c["name"] == channel_name or c.get("real_name") == channel_name),
            None
        )
        if not ch:
            logger.warning(f"[WEBHOOK] '{channel_name}': channel not found")
            return JSONResponse({"success": False, "error": "channel not found"})

        if not ch.get("fallback", False):
            logger.info(f"[WEBHOOK] '{channel_name}': Stop event, fallback disabled, skipping")
            return JSONResponse({"success": True, "message": "fallback disabled"})

        # Дедупликация: не запускаем healthcheck, если для этого канала
        # уже есть незавершённая webhook-задача.
        with state._healthcheck_lock:
            for tid, task in state._healthcheck_tasks.items():
                if task.get("done"):
                    continue
                if tid.startswith("webhook_") and ch["name"] in tid:
                    logger.info(f"[WEBHOOK] '{channel_name}': task already queued ({tid}), skipping")
                    return JSONResponse({"success": True, "message": "already queued"})

        logger.info(f"[WEBHOOK] '{channel_name}': Stop event, starting background healthcheck")

        task_id = f"webhook_{int(time.time())}_{ch['name']}"
        threading.Thread(
            target=run_healthcheck_async,
            args=(task_id, [ch], False),
            daemon=True
        ).start()

        return JSONResponse({"success": True})

    except Exception as e:
        logger.error(f"[WEBHOOK] handling error: {e}")
        return JSONResponse({"success": False, "error": str(e)})
