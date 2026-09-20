import os
import json
import urllib.request

from core.config import (
    IPTV_JELLYFIN_URL, IPTV_JELLYFIN_API_KEY, IPTV_JELLYFIN_XMLTV_CACHE_DIR,
    IPTV_JELLYFIN_API_TIMEOUT, logger
)

def clear_jellyfin_xmltv_cache():
    if not os.path.isdir(IPTV_JELLYFIN_XMLTV_CACHE_DIR):
        logger.warning(f"[JELLYFIN] cache dir not mounted: {IPTV_JELLYFIN_XMLTV_CACHE_DIR}")
        return
    try:
        removed = 0
        for entry in os.listdir(IPTV_JELLYFIN_XMLTV_CACHE_DIR):
            full_path = os.path.join(IPTV_JELLYFIN_XMLTV_CACHE_DIR, entry)
            if os.path.isfile(full_path):
                os.remove(full_path)
                removed += 1
        logger.info(f"[JELLYFIN] xmltv cache cleared ({removed} files removed)")
    except Exception as e:
        logger.error(f"[JELLYFIN] failed to clear xmltv cache: {e}")

def trigger_jellyfin_guide_refresh():
    if not IPTV_JELLYFIN_API_KEY:
        logger.warning("[JELLYFIN] IPTV_JELLYFIN_API_KEY not set, skipping auto guide refresh")
        return
    clear_jellyfin_xmltv_cache()

    headers = {
        "Authorization": (
            'MediaBrowser Client="iptv-proxy", '
            'Device="iptv-proxy", '
            'DeviceId="iptv-proxy-server", '
            'Version="1.0.0", '
            f'Token="{IPTV_JELLYFIN_API_KEY}"'
        ),
        "Accept": "application/json",
    }

    try:
        req = urllib.request.Request(
            f"{IPTV_JELLYFIN_URL}/ScheduledTasks",
            headers=headers
        )
        with urllib.request.urlopen(req, timeout=IPTV_JELLYFIN_API_TIMEOUT) as resp:
            tasks = json.loads(resp.read().decode("utf-8"))

        task_id = next((t["Id"] for t in tasks if t.get("Key") == "RefreshGuide"), None)
        if not task_id:
            logger.warning("[JELLYFIN] RefreshGuide task not found")
            return

        req2 = urllib.request.Request(
            f"{IPTV_JELLYFIN_URL}/ScheduledTasks/Running/{task_id}",
            data=b"",
            headers=headers,
            method="POST"
        )
        urllib.request.urlopen(req2, timeout=IPTV_JELLYFIN_API_TIMEOUT)
        logger.info("[JELLYFIN] guide refresh triggered")
    except Exception as e:
        logger.error(f"[JELLYFIN] guide refresh failed: {e}")
