"""
routers/channels_resolvers.py — массовое применение/сброс резолверов.

Вынесено из routers/channels.py (рефакторинг channels-refactor-v1).
  /channels/apply-resolvers
  /channels/reset-resolvers
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import core.state as state
from core.config import logger

router = APIRouter()


@router.post("/channels/apply-resolvers")
async def apply_resolvers(request: Request):
    try:
        last_task = None
        with state._healthcheck_lock:
            for task_id, task in state._healthcheck_tasks.items():
                if task.get("done"):
                    if last_task is None or task.get("finished_at", 0) > last_task.get("finished_at", 0):
                        last_task = task

        if not last_task:
            return JSONResponse({"success": False, "error": "Нет завершённой проверки"})

        results = last_task.get("results", {})
        updates = {}
        for name, res in results.items():
            if res.get("success") and res.get("method"):
                updates[name] = res["method"]

        if not updates:
            return JSONResponse({"success": False, "error": "Нет успешных методов для применения"})

        with state.channels_lock:
            channels = state.load_channels()
            updated_count = 0
            for ch in channels:
                if ch["name"] in updates and ch.get("resolver", "auto") != updates[ch["name"]]:
                    ch["resolver"] = updates[ch["name"]]
                    active_idx = ch.get("active_stream_index", 0)
                    if ch.get("streams") and active_idx < len(ch["streams"]):
                        ch["streams"][active_idx]["resolver"] = updates[ch["name"]]
                    updated_count += 1
                    # Смена resolver'а касается только активного стрима —
                    # не трогаем кэши остальных.
                    state.clear_stream_cache(ch["name"], active_idx)

            if updated_count > 0:
                sorted_channels = state.sort_channels_by_chno(channels)
                state.save_channels_to_file(sorted_channels)
                logger.info(f"[APPLY-RESOLVERS] resolvers updated for {updated_count} channels")

        return JSONResponse({
            "success": True,
            "updated": updated_count,
            "message": f"Обновлено резолверов: {updated_count}"
        })
    except Exception as e:
        logger.error(f"[APPLY-RESOLVERS] error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

@router.post("/channels/reset-resolvers")
async def reset_resolvers(request: Request):
    try:
        with state.channels_lock:
            channels = state.load_channels()
            updated_count = 0
            for ch in channels:
                if ch.get("resolver", "auto") != "auto":
                    ch["resolver"] = "auto"
                    active_idx = ch.get("active_stream_index", 0)
                    if ch.get("streams") and active_idx < len(ch["streams"]):
                        ch["streams"][active_idx]["resolver"] = "auto"
                    updated_count += 1
                    state.clear_stream_cache(ch["name"], active_idx)
            if updated_count > 0:
                sorted_channels = state.sort_channels_by_chno(channels)
                state.save_channels_to_file(sorted_channels)
                logger.info(f"[RESET-RESOLVERS] resolvers reset for {updated_count} channels")
        return JSONResponse({
            "success": True,
            "updated": updated_count,
            "message": f"Сброшено резолверов: {updated_count}"
        })
    except Exception as e:
        logger.error(f"[RESET-RESOLVERS] error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

# channels-refactor-v1
