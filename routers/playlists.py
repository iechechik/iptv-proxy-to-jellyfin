"""
Роутер внешних M3U-плейлистов.

Управление источниками (список, save, refresh, clear) + поиск по кэшу
+ quick_check по тапу в UI.

Никакого импорта каналов в config.json — только API.
Каналы добавляет пользователь через обычный /channels/add или /channels/update-stream.
"""

import asyncio
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import core.config as cfg
from core.config import logger
from services.playlist_service import (
    playlist_manager,
    refresh_source,
    quick_check,
)

router = APIRouter()


def _normalize_source(src: dict):
    """Нормализация источника плейлиста: name, url, interval, disable.

    Interval принимаем как int (сек) или строку с суффиксом (h/d/m/s).
    """
    if not isinstance(src, dict):
        return None
    url = str(src.get("url", "")).strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    name = str(src.get("name", url)).strip() or url

    raw_interval = src.get("interval", 86400)
    try:
        if isinstance(raw_interval, str):
            interval = cfg.parse_interval(raw_interval)
        else:
            interval = int(raw_interval)
    except Exception:
        interval = 86400
    if interval < 60:
        interval = 60

    out = {"name": name, "url": url, "interval": interval}
    if src.get("disable", False):
        out["disable"] = True
    return out


@router.get("/playlists/sources")
def get_playlist_sources():
    """Список источников: config + SQLite-метаданные, смердженные по имени."""
    config_sources = cfg.get_playlist_sources() or []
    stats_list = playlist_manager.get_sources_stats()
    stats_by_name = {s["name"]: s for s in stats_list}

    seen = set()
    result = []

    for src in config_sources:
        name = src.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        st = stats_by_name.get(name, {})
        result.append({
            "name": name,
            "url": src.get("url", ""),
            "interval": src.get("interval", 86400),
            "disable": src.get("disable", False),
            "in_config": True,
            "updated_at": st.get("updated_at"),
            "channel_count": st.get("channel_count", 0),
            "last_error": st.get("last_error"),
        })

    # Источники, которые есть в SQLite, но нет в config (удалённые)
    for st in stats_list:
        name = st["name"]
        if name in seen:
            continue
        result.append({
            "name": name,
            "url": st.get("url", ""),
            "interval": 86400,
            "disable": True,
            "in_config": False,
            "updated_at": st.get("updated_at"),
            "channel_count": st.get("channel_count", 0),
            "last_error": st.get("last_error"),
        })

    result.sort(key=lambda r: r["name"].lower())
    return JSONResponse({"success": True, "sources": result})


@router.post("/playlists/sources")
async def save_playlist_sources(request: Request):
    """Сохранить секцию playlist_sources в config.json.

    Нормализует каждый источник. Пустые/битые — отбрасываются.
    """
    try:
        data = await request.json()
        sources = data.get("sources", [])
        if not isinstance(sources, list):
            return JSONResponse({"success": False, "error": "sources должен быть списком"})

        normalized = []
        for src in sources:
            n = _normalize_source(src)
            if n:
                normalized.append(n)

        if cfg.CONFIG_DATA is None:
            return JSONResponse({"success": False, "error": "config.json не загружен"})

        cfg.CONFIG_DATA["playlist_sources"] = normalized
        if not cfg.save_full_config():
            return JSONResponse({"success": False, "error": "ошибка сохранения config.json"})

        logger.info(f"[PLAYLIST] sources saved: {len(normalized)}")
        return JSONResponse({"success": True, "sources": normalized})
    except Exception as e:
        logger.error(f"[PLAYLIST] sources save failed: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/playlists/refresh/{name}")
def refresh_playlist_source(name: str):
    """Обновить один источник в фоне."""
    sources = cfg.get_playlist_sources() or []
    src = next((s for s in sources if s.get("name") == name), None)
    if not src:
        return JSONResponse({"success": False, "error": "Источник не найден в config"})

    def _worker():
        try:
            refresh_source(src)
        except Exception as e:
            logger.error(f"[PLAYLIST] '{name}': refresh failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    return JSONResponse({"success": True})


@router.post("/playlists/refresh-all")
def refresh_all_playlist_sources():
    """Обновить все активные источники в фоне (последовательно)."""
    sources = cfg.get_playlist_sources() or []
    active = [s for s in sources if not s.get("disable", False)]
    if not active:
        return JSONResponse({"success": False, "error": "Нет активных источников"})

    def _worker():
        for src in active:
            try:
                refresh_source(src)
            except Exception as e:
                logger.error(f"[PLAYLIST] '{src.get('name')}': refresh failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    return JSONResponse({"success": True, "count": len(active)})


@router.post("/playlists/clear/{name}")
def clear_playlist_source(name: str):
    """Очистить данные источника в SQLite. config.json не трогает."""
    try:
        playlist_manager.clear_source(name)
        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"[PLAYLIST] '{name}': clear failed: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.get("/playlists/search")
def search_playlist_channels(q: str = "", source: str = None, limit: int = 150):
    """Поиск по кэшу плейлистов.

    Сортировка: точное → префикс → подстрока.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"success": True, "results": [], "note": "min 2 chars"})

    try:
        limit = int(limit)
    except Exception:
        limit = 150
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    src = source.strip() if source else None

    try:
        results = playlist_manager.search(q, source=src, limit=limit)
        return JSONResponse({"success": True, "results": results})
    except Exception as e:
        logger.error(f"[PLAYLIST] search failed: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@router.post("/playlists/check")
async def check_playlist_url(request: Request):
    """Проверка URL по тапу в UI. HEAD 1 сек.

    Возвращает {ok: bool, detail: str}.
    """
    try:
        data = await request.json()
        url_payload = data.get("url_payload", "")
        if not url_payload:
            return JSONResponse({"success": False, "error": "url_payload не указан"})

        ok, detail = await asyncio.to_thread(quick_check, url_payload)
        return JSONResponse({"success": True, "ok": ok, "detail": detail})
    except Exception as e:
        logger.error(f"[PLAYLIST] check failed: {e}")
        return JSONResponse({"success": False, "error": str(e)})
