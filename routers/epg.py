from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from datetime import datetime, timezone
import re
import threading
import os

import core.config as cfg
import core.state as state
from core.config import IPTV_EPG_CACHE_PATH, logger
from services.epg_service import (
    epg_manager, build_filtered_epg, auto_match_channels,
    download_and_import_source, on_epg_updated, update_all_sources
)
from services.jellyfin_service import trigger_jellyfin_guide_refresh

router = APIRouter()

@router.post("/epg/save")
async def save_all_changes(request: Request):
    try:
        data = await request.json()
        cache_data = data.get("cache", {})

        for orig_name, info in cache_data.items():
            if not orig_name.strip():
                return JSONResponse({"success": False, "error": "Обнаружено пустое имя канала!"}, status_code=400)

        # Read-modify-write конфига держим в channels_lock через всю операцию,
        # чтобы параллельный healthcheck или другая UI-правка
        # не перетёрли наши изменения (и наоборот — чтобы мы не потеряли их).
        # state.channels_lock — RLock, поэтому load_channels() и
        # save_channels_to_file() внутри критической секции срабатывают
        # рекурсивно, без самоблокировки.
        with state.channels_lock:
            channels = state.load_channels()
            for ch in channels:
                if ch["name"] in cache_data:
                    info = cache_data[ch["name"]]
                    saved_chno = str(info.get("chno", "")).strip()
                    if saved_chno:
                        ch["chno"] = saved_chno
                    saved_tvgid = re.sub(r'\s*\([^)]*\)\s*$', '', str(info.get("tvg_id", ""))).strip()
                    if saved_tvgid:
                        ch["tvgid"] = saved_tvgid
                    if "real_name" in info:
                        ch["real_name"] = info.get("real_name", ch["name"])
                    if "enabled" in info:
                        ch["disable"] = not bool(info.get("enabled", True))

            sorted_channels = state.sort_channels_by_chno(channels)
            state.save_channels_to_file(sorted_channels)

        # build_filtered_epg и trigger_jellyfin_guide_refresh — вне channels_lock:
        # внутри sqlite + сеть к Jellyfin (могут занять секунды), не блокируем
        # остальные операции с каналами на это время.
        build_filtered_epg(force_refresh=True)
        trigger_jellyfin_guide_refresh()
        logger.info("[EPG] Jellyfin guide refreshed")
        return JSONResponse({"success": True})

    except Exception as e:
        logger.error(f"[EPG] save error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)

@router.get("/epg/status")
def epg_status():
    # Читаем _epg_channels и _epg_building (не обязательно под блокировкой, но для безопасности)
    with state.cache_lock:
        channels_count = len(state._epg_channels)
        building = state._epg_building
    return JSONResponse({
        "loaded": bool(state._epg_channels),
        "count": channels_count,
        "building": building
    })

@router.get("/epg/channels")
def epg_channels():
    channels_list = []
    all_channels = epg_manager.get_channels()
    source_name_map = epg_manager.get_channel_source_name_map()
    for cid, names in all_channels.items():
        primary_name = names[0] if names else cid
        channels_list.append({
            "id": cid,
            "name": primary_name,
            "source_name": source_name_map.get(cid, "")
        })
    channels_list.sort(key=lambda x: x["name"].lower())
    return JSONResponse({"channels": channels_list})

@router.post("/epg/update-source/{source_name}")
async def update_single_source(source_name: str):
    source = next(
        (s for s in cfg.IPTV_EPG_SOURCES if s.get("name") == source_name or s.get("url") == source_name),
        None
    )
    if not source:
        return JSONResponse({"success": False, "error": "Источник не найден"})

    def background_task():
        try:
            download_and_import_source(source)
            on_epg_updated(success=True, message=f"Источник {source.get('name', source.get('url'))} обновлён")
        except Exception as e:
            logger.error(f"[EPG] source {source.get('name')}: update failed: {e}")

    threading.Thread(target=background_task, daemon=True).start()
    return JSONResponse({"success": True})

@router.get("/epg/auto-match")
def auto_match():
    def background_task():
        try:
            auto_match_channels()
        except Exception as e:
            logger.exception("[EPG] auto-match error")
    threading.Thread(target=background_task, daemon=True).start()
    return RedirectResponse("/manage", status_code=303)

@router.get("/epg/rebuild")
def rebuild_epg():
    def background_task():
        try:
            build_filtered_epg(force_refresh=True)
            trigger_jellyfin_guide_refresh()
        except Exception as e:
            logger.exception("[EPG] rebuild error")
    threading.Thread(target=background_task, daemon=True).start()
    return RedirectResponse("/manage", status_code=303)

# xmltv-etag-v1
# Jellyfin при RefreshGuide дёргает /xmltv.xml.gz отдельно для каждого
# канала (46 запросов × 0.7 МБ). Из-за Cache-Control: no-store Jellyfin
# не может использовать кэш, каждый запрос — полная выкачка.
#
# Теперь отдаём:
#   Cache-Control: no-cache        (можно кэшировать, но проверяй)
#   Last-Modified: <mtime>         (If-Modified-Since)
#   ETag: "<md5>"                  (If-None-Match)
# На совпадение ETag/If-Modified-Since возвращаем 304 без тела.
# ETag берём из IPTV_EPG_CACHE_PATH.md5, который пишет build_filtered_epg.
@router.get("/xmltv.xml.gz")
def get_xmltv(request: Request):
    if not os.path.exists(IPTV_EPG_CACHE_PATH):
        return Response("EPG not ready yet", status_code=503)

    mtime = os.path.getmtime(IPTV_EPG_CACHE_PATH)
    last_modified = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    # ETag — md5 от XML. Файл .md5 пишется build_filtered_epg рядом с .gz.
    etag = None
    md5_path = IPTV_EPG_CACHE_PATH + ".md5"
    if os.path.exists(md5_path):
        try:
            with open(md5_path, "r") as f:
                etag = f.read().strip() or None
        except Exception:
            etag = None

    # If-None-Match: клиент прислал ETag последней версии.
    client_etag = request.headers.get("if-none-match") if request else None
    if client_etag and etag:
        # Может содержать список "etag1, etag2" и слабые etag W/"..." —
        # для нас достаточно точного совпадения с нашим etag (в кавычках).
        candidates = [c.strip().strip('W/').strip('"') for c in client_etag.split(",")]
        if etag in candidates or etag.strip('"') in candidates:
            return Response(
                status_code=304,
                headers={
                    "ETag": f'"{etag}"',
                    "Last-Modified": last_modified,
                    "Cache-Control": "no-cache",
                },
            )

    # If-Modified-Since: клиент прислал дату последнего скачанного файла.
    ims = request.headers.get("if-modified-since") if request else None
    if ims and not client_etag:
        try:
            from email.utils import parsedate_to_datetime
            ims_dt = parsedate_to_datetime(ims)
            if ims_dt is not None:
                mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                if int(mtime_dt.timestamp()) <= int(ims_dt.timestamp()):
                    return Response(
                        status_code=304,
                        headers={
                            "Last-Modified": last_modified,
                            "Cache-Control": "no-cache",
                        },
                    )
        except Exception:
            pass

    with open(IPTV_EPG_CACHE_PATH, "rb") as f:
        data = f.read()

    headers = {
        "Content-Disposition": "attachment; filename=epg.xml.gz",
        "Cache-Control": "no-cache",
        "Last-Modified": last_modified,
    }
    if etag:
        headers["ETag"] = f'"{etag}"'

    return Response(
        data,
        media_type="application/x-gzip",
        headers=headers,
    )

@router.get("/epg/sources")
def get_epg_sources():
    sources = []
    raw_sources = cfg.CONFIG_DATA.get("epg", {}).get("sources", [])
    for src in raw_sources:
        norm = cfg.normalize_source_config(src)
        if norm:
            updated_at = epg_manager.get_source_updated_at(norm["name"])
            sources.append({
                "name": norm["name"],
                "url": norm["url"],
                "interval": norm["interval"],
                "filter": norm["filter"],
                "disable": norm["disable"],
                "comment": norm.get("comment", ""),
                "updated_at": updated_at
            })
    return JSONResponse({"success": True, "sources": sources})

@router.post("/epg/sources")
async def save_epg_sources(request: Request):
    try:
        data = await request.json()
        sources = data.get("sources", [])
        if cfg.CONFIG_DATA is None:
            return JSONResponse({"success": False, "error": "config.json не загружен"})
        cfg.CONFIG_DATA["epg"]["sources"] = sources
        cfg.IPTV_EPG_SOURCES = []
        for src in sources:
            norm = cfg.normalize_source_config(src)
            if norm and not norm.get("disable", False):
                cfg.IPTV_EPG_SOURCES.append(norm)
        if cfg.save_full_config():
            return JSONResponse({"success": True, "sources": sources})
        else:
            return JSONResponse({"success": False, "error": "Ошибка сохранения config.json"})
    except Exception as e:
        logger.error(f"[EPG] sources save failed: {e}")
        return JSONResponse({"success": False, "error": str(e)})

@router.post("/epg/update-all")
def update_all_epg_sources():
    def background_task():
        try:
            update_all_sources()
        except Exception as e:
            logger.exception("[EPG] background update-all failed")
    threading.Thread(target=background_task, daemon=True).start()
    return JSONResponse({"success": True})
