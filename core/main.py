from routers import system, web, stream, channels, epg, jellyfin

import os
import asyncio
import threading
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import core.state as state
from core.config import logger
from services.healthcheck import (
    background_cleanup,
    start_healthcheck_scheduler
)
from services.mux_service import start_mux_watchdog
from services.url_analytics import start_analytics
from services.epg_service import epg_manager, build_filtered_epg, periodic_epg_update, update_all_sources
from services.jellyfin_service import trigger_jellyfin_guide_refresh
from core.logging_setup import setup_logging

app = FastAPI()
app.include_router(system.router)
app.include_router(web.router)
app.include_router(stream.router)
app.include_router(channels.router)
app.include_router(epg.router)
app.include_router(jellyfin.router)


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.mount("/static", NoCacheStaticFiles(directory="web/static"), name="static")


def _ensure_black_ts():
    import subprocess
    path = "/app/db/black.ts"
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        logger.info(f"[BLACK] {path} already exists, skipping generation")
        return
    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=5:r=25",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", "5",
            "-c:v", "mpeg2video", "-b:v", "200k",
            "-c:a", "mp2", "-b:a", "64k",
            "-f", "mpegts", path,
        ], capture_output=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"[BLACK] ffmpeg exited with code {result.returncode}: {result.stderr.decode('utf-8', errors='ignore')[:300]}")
        else:
            logger.info(f"[BLACK] generated {path} ({os.path.getsize(path)} bytes)")
    except Exception as e:
        logger.error(f"[BLACK] generation failed: {e}")


def _ensure_black_ts_async():
    threading.Thread(target=_ensure_black_ts, daemon=True, name="black-ts-gen").start()


@app.on_event("startup")
def startup():
    state._epg_cache.clear()
    state._active_index_map.clear()
    state._failed_resolve_cache.clear()

    _ensure_black_ts_async()

    state._loop = asyncio.get_running_loop()

    logger.info("[SSE] asyncio loop captured")

    state.load_channels()

    # Материализуем stream_id в config.json. assign_stream_ids() вызывается
    # внутри load_channels и генерирует id только в памяти — на диск они
    # попадут через save_full_config. Без этого первого save_cache не сможет
    # подставить id в слоты, и streams_cache потеряет identity после рестарта.
    try:
        state.save_channels_to_file(state.load_channels())
        logger.info("[STARTUP] channels saved, stream_id materialized in config.json")
    except Exception as e:
        logger.warning(f"[STARTUP] config.json save failed: {e}")

    state.load_cache()

    # Maintenance sqlite — фоновым потоком. Полный скан programmes на
    # холодном кэше занимает секунды, старту он не нужен.
    threading.Thread(
        target=epg_manager.maintenance,
        daemon=True,
        name="epg-maintenance",
    ).start()
    channels_from_db = epg_manager.get_channels()
    if channels_from_db:
        state._epg_channels = channels_from_db
        logger.info(f"[EPG] loaded {len(state._epg_channels)} channels from db")
        if state._epg_cache:
            changed = build_filtered_epg(force_refresh=False)
            if changed:
                trigger_jellyfin_guide_refresh()
        else:
            build_filtered_epg(force_refresh=True)
            trigger_jellyfin_guide_refresh()
    else:
        logger.info("[EPG] db empty, starting background full fetch")
        threading.Thread(target=update_all_sources, daemon=True).start()

    threading.Thread(target=periodic_epg_update, daemon=True).start()
    logger.info("[EPG] background updater started (interval 1h)")

    logs_dir = "/app/logs"
    if not os.path.exists(logs_dir):
        try:
            os.makedirs(logs_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"[STARTUP] failed to create logs dir: {e}")

    threading.Thread(target=background_cleanup, daemon=True).start()
    logger.info("[CACHE] background cleanup started")

    start_healthcheck_scheduler()

    start_mux_watchdog()
    logger.info("[MUX] watchdog started")

    # Сборщик статистики URL для анализа TTL-логики resolver.
    # Включается секцией "analytics" в config.json (enabled=true).
    start_analytics()

    logger.info("[STARTUP] IPTV-Proxy server started")


setup_logging()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
