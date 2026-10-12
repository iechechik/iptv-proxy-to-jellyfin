#!/usr/bin/env python3
"""Разовая чистка mediainfo-кэша Jellyfin по каналам этого сервиса.

Зачем: Jellyfin запоминает тип контейнера канала (Container: hls|ts) и открывает
канал с закэшированным `-f`. Если запись разошлась с тем, что сервис реально
отдаёт, канал не играет: в логе Jellyfin «Invalid data found when processing
input» либо «Could not detect TS packet size». Сервис чистит запись сам при смене
mux_state, url, resolver или active_stream_index; этот скрипт нужен, когда режим
меняли в обход сервиса (при выключенном сервисе, правкой конфига руками), либо
когда нужно вычистить все каналы разом.

Удаляются только записи, у которых Path ведёт на этот сервис (`/redirect/`):
медиатека (фильмы, музыка) не затрагивается.

Запуск внутри контейнера сервиса:
    python3 scripts/clear_channel_mediainfo.py --dry-run
    python3 scripts/clear_channel_mediainfo.py
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, "/app")
from core.config import IPTV_JELLYFIN_MEDIAINFO_DIR as DEFAULT_DIR  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Чистка mediainfo-кэша Jellyfin по каналам сервиса")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="каталог mediainfo-кэша Jellyfin")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет удалено")
    args = ap.parse_args()

    if not args.dir or not os.path.isdir(args.dir):
        print(f"каталог недоступен: {args.dir}")
        print("проверьте монтирование каталога (README, раздел «Связка с Jellyfin»).")
        return 1

    removed = kept = 0
    for path in sorted(glob.glob(os.path.join(args.dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        p = str(data.get("Path") or "")
        if "/redirect/" not in p:
            kept += 1
            continue
        name = p.split("/redirect/", 1)[1].split("/")[0]
        cont = data.get("Container") or "?"
        if args.dry_run:
            print(f"  будет удалено: {name} (Container={cont})")
        else:
            try:
                os.remove(path)
                print(f"  удалено: {name} (Container={cont})")
            except Exception as e:
                print(f"  не удалось удалить {name}: {e}")
                continue
        removed += 1

    if args.dry_run:
        print(f"\nитого: будет удалено {removed}, оставлено записей не-каналов {kept}")
        print("это был dry-run; для удаления запустите без --dry-run")
    else:
        print(f"\nитого: удалено {removed}, оставлено записей не-каналов {kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
