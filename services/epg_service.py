import os
import gzip
import hashlib
import time
import urllib.request
import threading
import datetime

import core.config as cfg
from core.config import logger
import core.state as state
from services.events import send_epg_progress, send_epg_complete
from services.jellyfin_service import trigger_jellyfin_guide_refresh
from services.epg_manager import EPGManager

epg_manager = EPGManager(db_path=cfg.IPTV_EPG_DB_FILE)


def on_epg_updated(success: bool = True, message: str = "EPG успешно обновлён"):
    if success:
        # Сначала выгружаем из sqlite (может занять секунды на больших
        # базах), потом — короткий swap под lock. Раньше lock держался
        # ровно на время запроса, что блокировало healthcheck, stream
        # и всё, что ходит в cache_lock, на десятки секунд.
        logger.info("[EPG] refreshing in-memory channels...")
        _t_ch = time.time()
        new_epg_channels = epg_manager.get_channels()
        logger.info(f"[EPG] in-memory channels refreshed in {time.time() - _t_ch:.1f}s (count={len(new_epg_channels)})")
        with state.cache_lock:
            state._epg_channels = new_epg_channels
        changed = build_filtered_epg(force_refresh=False)
        if changed:
            trigger_jellyfin_guide_refresh()
        logger.info("[EPG] db updated, filtered EPG rebuilt")
    send_epg_complete(success=success, message=message)


def download_and_import_source(source: dict):
    """Скачивает и импортирует один источник."""
    url = source["url"]
    name = source.get("name") or url
    safe_name = hashlib.md5(url.encode()).hexdigest()[:10]
    tmp_file = f"/tmp/epg_{safe_name}.tmp"
    part_file = tmp_file + ".part"
    try:
        for old_file in (tmp_file, part_file):
            if os.path.exists(old_file):
                os.remove(old_file)

        req = urllib.request.Request(url, headers={"User-Agent": cfg.IPTV_DEFAULT_UA})
        logger.info(f"[EPG] downloading: {url}")
        with urllib.request.urlopen(req, timeout=cfg.IPTV_EPG_DOWNLOAD_TIMEOUT) as resp:
            logger.debug(f"[EPG] response status: {resp.status}")
            with open(part_file, "wb") as f:
                total = 0
                while chunk := resp.read(1024 * 1024):
                    f.write(chunk)
                    total += len(chunk)
            logger.debug(f"[EPG] downloaded {total} bytes")

        if not os.path.exists(part_file) or os.path.getsize(part_file) == 0:
            raise RuntimeError("Временный файл пуст или отсутствует")

        os.replace(part_file, tmp_file)

        send_epg_progress(-1, f"Импорт источника {name}...")
        logger.info(f"[EPG] source '{name}': importing...")
        _t_imp = time.time()
        epg_manager.import_source(name, tmp_file, filters=source.get("filter"))
        logger.info(f"[EPG] source '{name}': imported in {time.time() - _t_imp:.1f}s")
    except Exception as e:
        logger.error(f"[EPG] source '{name}': update failed: {e}")
        raise
    finally:
        for f in (tmp_file, part_file):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass


_epg_update_lock = threading.Lock()


def update_all_sources():
    """Обновляет все источники последовательно, затем пересобирает итоговый EPG."""
    if not _epg_update_lock.acquire(blocking=False):
        logger.info("[EPG] update already in progress, skipping")
        return
    _t_all = time.time()
    _ok = 0
    _fail = 0
    logger.info(f"[EPG] update-all started: {len(cfg.IPTV_EPG_SOURCES)} source(s)")
    try:
        for src in cfg.IPTV_EPG_SOURCES:
            try:
                download_and_import_source(src)
                _ok += 1
            except Exception as e:
                _fail += 1
                logger.error(f"[EPG] source {src.get('url')} skipped due to error: {e}")
        epg_manager.set_source_priority([s.get("name", s["url"]) for s in cfg.IPTV_EPG_SOURCES])
        on_epg_updated(success=True)
        logger.info(f"[EPG] update-all finished: {_ok} ok, {_fail} failed, total {time.time() - _t_all:.1f}s")
    finally:
        _epg_update_lock.release()

def periodic_epg_update():
    """
    epg-schedule-v2:
      - Каждый источник обновляется по своему интервалу (src["interval"]).
      - last_update берётся из meta.updated_at (переживает рестарт).
      - Пустая БД → last_update=0 → импорт сразу.
      - IPTV_EPG_UPDATE_TIME — время VACUUM (раз в сутки), не импорта.
    """
    # Инициализируем время последнего обновления из БД, чтобы не качать всё заново
    last_update = {}
    for src in cfg.IPTV_EPG_SOURCES:
        name = src.get("name", src["url"])
        last_update[name] = epg_manager.get_source_updated_at(name) or 0

    # epg-schedule-v2: IPTV_EPG_UPDATE_TIME → время VACUUM (раз в сутки).
    # Больше не "ночное обновление всех" — обновления только по интервалам.
    next_vacuum_run = None
    if cfg.IPTV_EPG_UPDATE_TIME:
        try:
            hour, minute = map(int, cfg.IPTV_EPG_UPDATE_TIME.split(":"))
            now = datetime.datetime.now()
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += datetime.timedelta(days=1)
            next_vacuum_run = candidate
            logger.info(f"[EPG] vacuum scheduled at {next_vacuum_run.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception:
            logger.error(f"[EPG] invalid IPTV_EPG_UPDATE_TIME format: {cfg.IPTV_EPG_UPDATE_TIME}")

    time.sleep(10)

    while True:
        try:
            now = time.time()
            need_update = False

            # epg-schedule-v2: вместо "ночного обновления всех" — VACUUM.
            # Раз в сутки, в IPTV_EPG_UPDATE_TIME. Импорты — только по интервалам.
            if next_vacuum_run and datetime.datetime.now() >= next_vacuum_run:
                logger.info("[EPG] scheduled vacuum starting")
                try:
                    epg_manager.vacuum()
                except Exception as e:
                    logger.warning(f"[EPG] scheduled vacuum failed: {e}")
                next_vacuum_run += datetime.timedelta(days=1)

            # 2) Интервальные обновления
            for src in cfg.IPTV_EPG_SOURCES:
                name = src.get("name", src["url"])
                interval = src.get("interval", 86400)
                if now - last_update.get(name, 0) >= interval:
                    if not _epg_update_lock.acquire(blocking=False):
                        continue
                    try:
                        download_and_import_source(src)
                        last_update[name] = now
                        need_update = True
                    except Exception as e:
                        logger.error(f"[EPG] source '{name}': update failed: {e}")
                    finally:
                        _epg_update_lock.release()

            if need_update:
                epg_manager.set_source_priority([s.get("name", s["url"]) for s in cfg.IPTV_EPG_SOURCES])
                on_epg_updated(success=True)
        except Exception as e:
            logger.error(f"[EPG] periodic update error: {e}")
        time.sleep(60)

def get_wanted_tvg_ids_from_cache():
    ids = set()
    for ch in state.load_channels():
        if not ch.get("disable", False):
            tvg_id = ch.get("tvgid")
            if tvg_id:
                ids.add(tvg_id)
    return ids

def build_filtered_epg(force_refresh: bool = False) -> bool:
    # _epg_lock уже защищает от параллельной сборки
    if not state._epg_lock.acquire(blocking=False):
        logger.warning("[EPG] filtered EPG build already in progress, skipping")
        return False
    state._epg_building = True  # можно без блокировки, т.к. защищено _epg_lock
    try:
        _t_build = time.time()
        wanted_ids = get_wanted_tvg_ids_from_cache()  # внутри возьмёт cache_lock
        logger.info(f"[EPG] building filtered XML for {len(wanted_ids)} channel(s)...")
        xml_content = epg_manager.get_filtered_xml(wanted_ids)

        tmp_xml = cfg.IPTV_EPG_CACHE_PATH + ".xml.tmp"
        with open(tmp_xml, "w", encoding="utf-8") as f:
            f.write(xml_content)

        new_hash = hashlib.md5(xml_content.encode('utf-8')).hexdigest()
        old_hash_file = cfg.IPTV_EPG_CACHE_PATH + ".md5"
        old_hash = None
        if os.path.exists(old_hash_file):
            with open(old_hash_file, "r") as f:
                old_hash = f.read().strip()

        changed = False
        if force_refresh or old_hash is None or new_hash != old_hash:
            gz_tmp = cfg.IPTV_EPG_CACHE_PATH + ".gz.tmp"
            with open(tmp_xml, "rb") as f_in, gzip.open(gz_tmp, "wb") as f_out:
                f_out.write(f_in.read())
            os.replace(gz_tmp, cfg.IPTV_EPG_CACHE_PATH)
            with open(old_hash_file, "w") as f:
                f.write(new_hash)
            changed = True

        if os.path.exists(tmp_xml):
            os.remove(tmp_xml)
        _sz = os.path.getsize(cfg.IPTV_EPG_CACHE_PATH) if os.path.exists(cfg.IPTV_EPG_CACHE_PATH) else 0
        logger.info(
            f"[EPG] filtered XML built in {time.time() - _t_build:.1f}s "
            f"(size={_sz / 1024 / 1024:.1f} MB, changed={changed})"
        )
        return changed
    finally:
        state._epg_building = False
        state._epg_lock.release()


def auto_match_channels():
    logger.info("[AUTO-MATCH] starting channel auto-match")

    # Снимок EPG — read-only, лочить не нужно.
    if not state._epg_channels:
        state._epg_channels = epg_manager.get_channels()

    # Read-modify-write config.json держим в channels_lock через всю
    # операцию. Без этого параллельный healthcheck,
    # UI-правка или toggle могут записать свои изменения между нашим
    # load_channels() и save_channels_to_file() — и мы перетрём их
    # своим устаревшим снимком. channels_lock — RLock, поэтому
    # load_channels()/save_channels_to_file() внутри сработают
    # рекурсивно, без самоблокировки.
    with state.channels_lock:
        channels = state.load_channels()
        epg_index = {}
        for cid, names in state._epg_channels.items():
            for dn in names:
                if dn:
                    key = dn.strip().lower()
                    epg_index.setdefault(key, []).append((cid, dn))

        matched_count = 0
        for ch in channels:
            name = ch["name"]
            tvgid = ch.get("tvgid")
            disable = ch.get("disable", False)

            current_chno = ch.get("chno", "")
            real_name = ch.get("real_name", name)
            epg_id = ch.get("tvgid", tvgid)

            if not real_name or real_name == name:
                if tvgid and tvgid in state._epg_channels:
                    real_name = state._epg_channels[tvgid][0] if state._epg_channels[tvgid] else name
                    epg_id = tvgid
                    matched_count += 1
                else:
                    name_lower = name.lower()
                    if name_lower in epg_index:
                        cid, dn = epg_index[name_lower][0]
                        real_name = dn
                        epg_id = cid
                        matched_count += 1

            ch["real_name"] = real_name
            ch["tvgid"] = epg_id
            ch["chno"] = str(current_chno)
            ch["disable"] = disable

        state.save_channels_to_file(channels)

    logger.info(f"[AUTO-MATCH] done: {matched_count} new/updated matches")
