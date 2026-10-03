import gzip
import logging
import sqlite3
import threading
import time

# epg-prune-v1
# prune-vacuum-v4
logger = logging.getLogger("iptv-proxy")


class EPGManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Отдельная блокировка только для операций записи (импорт/инициализация)
        # prune-vacuum-v4: RLock, чтобы prune_old_programmes внутри
        # import_source (тоже под _write_lock) не дедлочил.
        self._write_lock = threading.RLock()
        # Для чтения блокировка не нужна: WAL позволяет читать параллельно с записью
        self.source_priority = []  # список имён источников по убыванию приоритета
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        with self._write_lock:
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS meta (
                        source TEXT PRIMARY KEY,
                        version TEXT,
                        updated_at REAL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS channels (
                        source TEXT NOT NULL,
                        id TEXT NOT NULL,
                        xml_data TEXT NOT NULL,
                        PRIMARY KEY (source, id)
                    )
                """)
                # epg-prune-v1: start_time — unix timestamp начала программы,
                # нужен для prune старых. При миграции существующей БД —
                # ALTER TABLE ADD COLUMN (не заполняет старые записи, они
                # получат start_time при следующем переимпорте источника).
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS programmes (
                        source TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        xml_data TEXT NOT NULL,
                        start_time INTEGER
                    )
                """)
                try:
                    conn.execute("ALTER TABLE programmes ADD COLUMN start_time INTEGER")
                except sqlite3.OperationalError:
                    pass  # колонка уже есть
                conn.execute("CREATE INDEX IF NOT EXISTS idx_programmes_source_channel ON programmes(source, channel_id)")
                # epg-index-channel-v1: отдельный индекс на channel_id.
                # get_filtered_xml делает "WHERE channel_id IN (?, ?, ...)" —
                # существующий idx_programmes_source_channel не подходит
                # (первый столбец source). Без этого индекса SQLite сканирует
                # всю programmes (десятки миллионов строк), сборка XML повисает
                # на минуты при старте.
                _idx_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_programmes_channel'"
                ).fetchone()
                if not _idx_exists:
                    logger.info("[EPG] building index idx_programmes_channel on programmes(channel_id) — may take minutes on large DB...")
                    _t0 = time.time()
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_programmes_channel ON programmes(channel_id)")
                    logger.info(f"[EPG] index idx_programmes_channel ready in {time.time() - _t0:.1f}s")
                else:
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_programmes_channel ON programmes(channel_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_programmes_start_time ON programmes(start_time)")

    def maintenance(self):
        try:
            with self._write_lock:
                with self._connect() as conn:
                    conn.execute("DELETE FROM channels WHERE source IS NULL OR source=''")
                    conn.execute("DELETE FROM programmes WHERE source IS NULL OR source=''")
                    conn.commit()
            logger.info("[EPG] maintenance done")
        except Exception as e:
            logger.warning(f"[EPG] maintenance failed: {e}")

    def set_source_priority(self, sources: list):
        """Устанавливает приоритет источников. Первый в списке — самый приоритетный."""
        self.source_priority = sources

    def import_source(self, source_name: str, file_path: str, filters: dict = None):
        """
        Импортирует EPG из файла (gzip или обычный XML).
        filters: dict с ключами mode, match, ids, names
        """
        if filters is None:
            filters = {}

        def _matches_filter(channel_id: str, display_names: list) -> bool:
            """Проверяет, должен ли канал быть импортирован согласно фильтру."""
            mode = filters.get("mode", "all")
            if mode == "all":
                return True
            ids = [str(x).lower() for x in filters.get("ids", [])]
            names = [str(x).lower() for x in filters.get("names", [])]
            cid_lower = channel_id.lower()
            id_matched = any(fid in cid_lower for fid in ids) if ids else False
            name_matched = False
            if names and display_names:
                display_names_lower = [str(n).lower() for n in display_names if n]
                name_matched = any(any(fname in dn for fname in names) for dn in display_names_lower)

            if mode == "whitelist":
                if filters.get("match", "any") == "all":
                    if ids and not id_matched:
                        return False
                    if names and not name_matched:
                        return False
                    return True
                else:
                    return id_matched or name_matched
            elif mode == "blacklist":
                return not (id_matched or name_matched)
            else:
                return True

        # Определяем, gzip ли файл
        with open(file_path, 'rb') as raw:
            first_two = raw.read(2)
            is_gzip = (first_two == b'\x1f\x8b')

        if is_gzip:
            opener = gzip.open(file_path, 'rb')
        else:
            opener = open(file_path, 'rb')

        logger.info(f"[EPG] source '{source_name}': importing from {file_path} (gzip={is_gzip}, filter={filters.get('mode', 'all') if filters else 'none'})...")

        try:
            from lxml import etree as ET
        except ImportError:
            import xml.etree.ElementTree as ET

        # Счётчики для отладки
        total_channels = 0
        passed_filter_channels = 0
        programme_count = 0

        with opener as f:
            with self._write_lock:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        # Удаляем старые записи только этого источника
                        conn.execute("DELETE FROM programmes WHERE source=?", (source_name,))
                        conn.execute("DELETE FROM channels WHERE source=?", (source_name,))

                        context = ET.iterparse(f, events=("start", "end"))
                        _, root = next(context)
                        channel_xml = {}
                        programme_channel_ids = set()
                        filtered_channel_ids = set()  # id каналов, прошедших фильтр

                        for event, elem in context:
                            if event == "end" and elem.tag == "channel":
                                total_channels += 1
                                cid = elem.get("id")
                                if not cid:
                                    root.clear()
                                    continue
                                # Извлекаем display-name
                                names = [dn.text for dn in elem.findall("display-name") if dn.text]
                                # Применяем фильтр
                                if _matches_filter(cid, names):
                                    passed_filter_channels += 1
                                    channel_xml[cid] = ET.tostring(elem, encoding="unicode")
                                    filtered_channel_ids.add(cid)
                                root.clear()
                            elif event == "end" and elem.tag == "programme":
                                ch_id = elem.get("channel")
                                if ch_id and ch_id in filtered_channel_ids:
                                    programme_channel_ids.add(ch_id)
                                    programme_count += 1
                                    xml_str = ET.tostring(elem, encoding="unicode")
                                    # epg-prune-v1: start_time для prune.
                                    _start_ts = self._parse_xmltv_time(elem.get("start", ""))
                                    conn.execute(
                                        "INSERT INTO programmes (source, channel_id, xml_data, start_time) VALUES (?, ?, ?, ?)",
                                        (source_name, ch_id, xml_str, _start_ts)
                                    )
                                root.clear()

                        # Дополнительная фильтрация каналов (как раньше: с программами или с иконкой)
                        final_channels = {}
                        for cid, xml_str in channel_xml.items():
                            has_icon = "<icon " in xml_str
                            has_programmes = cid in programme_channel_ids
                            if has_programmes or has_icon:
                                final_channels[cid] = xml_str

                        dropped_by_filter = total_channels - passed_filter_channels
                        dropped_no_content = passed_filter_channels - len(final_channels)

                        logger.info(
                            f"[EPG] source '{source_name}': "
                            f"total channels={total_channels}, "
                            f"dropped by filter={dropped_by_filter}, "
                            f"dropped without programmes/icons={dropped_no_content}, "
                            f"final channels={len(final_channels)}, "
                            f"programmes={programme_count}"
                        )
                        # Вставляем отобранные каналы
                        for cid, xml_str in final_channels.items():
                            conn.execute(
                                "INSERT INTO channels (source, id, xml_data) VALUES (?, ?, ?)",
                                (source_name, cid, xml_str)
                            )
                        conn.execute(
                            "INSERT OR REPLACE INTO meta (source, updated_at) VALUES (?, ?)",
                            (source_name, time.time())
                        )
                        conn.commit()
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                    except Exception:
                        conn.rollback()
                        raise
            # prune-vacuum-v4: prune старых programmes (7+ дней) после
            # каждого успешного импорта. _write_lock — RLock, вызов
            # безопасен (мы внутри того же лока).
            try:
                self.prune_old_programmes(days=7)
            except Exception as _e:
                logger.warning(f"[EPG] prune after import failed: {_e}")

    # epg-prune-v1
    def _parse_xmltv_time(self, s: str) -> int:
        """XMLTV time: '20261001120000 +0300' → unix ts. 0 при ошибке."""
        if not s or len(s) < 14:
            return 0
        try:
            from datetime import datetime, timezone, timedelta
            dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
            rest = s[14:].strip()
            tz_off = 0
            if rest and rest[0] in "+-" and len(rest) >= 5:
                sign = 1 if rest[0] == "+" else -1
                hh = int(rest[1:3]); mm = int(rest[3:5])
                tz_off = sign * (hh * 3600 + mm * 60)
            return int(dt.replace(tzinfo=timezone(timedelta(seconds=tz_off))).timestamp())
        except Exception:
            return 0

    def prune_old_programmes(self, days: int = 7) -> int:
        """Удаляет programmes с start_time < now - days*86400.
        Записи без start_time (0) сохраняются до следующего переимпорта."""
        cutoff = int(time.time()) - days * 86400
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM programmes WHERE start_time IS NOT NULL AND start_time > 0 AND start_time < ?",
                    (cutoff,)
                )
                removed = cur.rowcount
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if removed:
            logger.info(f"[EPG] pruned {removed} programmes older than {days} days")
        return removed

    def vacuum(self) -> None:
        """Сжатие БД: VACUUM + wal_checkpoint."""
        import os
        try:
            size_before = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        except Exception:
            size_before = 0
        try:
            with self._write_lock:
                with self._connect() as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
            try:
                size_after = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
            except Exception:
                size_after = 0
            delta_mb = (size_before - size_after) / 1024 / 1024
            logger.info(f"[EPG] vacuum done: {size_before/1024/1024:.1f} MB → {size_after/1024/1024:.1f} MB (freed {delta_mb:.1f} MB)")
        except Exception as e:
            logger.warning(f"[EPG] vacuum failed: {e}")

    def get_channels(self) -> dict:
        result = {}
        with self._connect() as conn:
            rows = conn.execute("SELECT source, id, xml_data FROM channels").fetchall()

        # Порядок источников (приоритет)
        if self.source_priority:
            priority = {src: idx for idx, src in enumerate(self.source_priority)}
        else:
            unique_sources = list(dict.fromkeys(r[0] for r in rows))
            priority = {src: idx for idx, src in enumerate(unique_sources)}

        # Выбираем лучший источник для каждого id
        best_per_id = {}
        for source, cid, xml_data in rows:
            src_priority = priority.get(source, 999)
            if cid not in best_per_id or src_priority < best_per_id[cid][0]:
                best_per_id[cid] = (src_priority, xml_data)

        for cid, (_, xml_data) in best_per_id.items():
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(xml_data)
                names = [dn.text for dn in root.findall("display-name") if dn.text]
                result[cid] = names
            except Exception:
                result[cid] = []
        return result

    def get_filtered_xml(self, wanted_ids: set) -> str:
        """Формирует итоговый XML, выбирая канал и программы из приоритетных источников."""
        parts = ['<?xml version="1.0" encoding="utf-8"?>\n<tv>\n']
        if not wanted_ids:
            parts.append('</tv>\n')
            return ''.join(parts)

        with self._connect() as conn:
            placeholders = ','.join('?' for _ in wanted_ids)
            rows_channels = conn.execute(
                f"SELECT source, id, xml_data FROM channels WHERE id IN ({placeholders})",
                tuple(wanted_ids)
            ).fetchall()
            rows_programmes = conn.execute(
                f"SELECT source, channel_id, xml_data FROM programmes WHERE channel_id IN ({placeholders})",
                tuple(wanted_ids)
            ).fetchall()

        channels_by_source = {}
        for source, cid, xml_data in rows_channels:
            channels_by_source.setdefault(source, {})[cid] = xml_data

        programmes_by_source = {}
        for source, ch_id, xml_data in rows_programmes:
            programmes_by_source.setdefault(source, {}).setdefault(ch_id, []).append(xml_data)

        priority = self.source_priority if self.source_priority else list(channels_by_source.keys())

        # wanted_ids — это set, а порядок итерации по set в Python
        # недетерминирован между запусками (зависит от хэш-сида
        # интерпретатора). XML собирается в порядке обхода, значит
        # build_filtered_epg получит разный md5 на одном и том же
        # наборе каналов, и будет пересобирать EPG каждый час впустую.
        # Сортируем — выход стабилен.
        for cid in sorted(wanted_ids):
            chosen_channel = None
            chosen_programmes = []
            for src in priority:
                if chosen_channel is None and src in channels_by_source and cid in channels_by_source[src]:
                    chosen_channel = channels_by_source[src][cid]
                if not chosen_programmes and src in programmes_by_source and cid in programmes_by_source[src]:
                    chosen_programmes = programmes_by_source[src][cid]
                if chosen_channel is not None and chosen_programmes:
                    break

            if chosen_channel:
                parts.append(chosen_channel)
                parts.append('\n')
            for xml in chosen_programmes:
                parts.append(xml)
                parts.append('\n')
        parts.append('</tv>\n')
        return ''.join(parts)

    def get_channel_source_name_map(self) -> dict:
        result = {}
        with self._connect() as conn:
            rows = conn.execute("SELECT source, id FROM channels").fetchall()
        sources_by_id = {}
        for source, cid in rows:
            sources_by_id.setdefault(cid, []).append(source)
        priority = self.source_priority if self.source_priority else list(dict.fromkeys(r[0] for r in rows))
        for cid, sources in sources_by_id.items():
            for src in priority:
                if src in sources:
                    result[cid] = src
                    break
        return result

    def get_source_updated_at(self, source_name: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT updated_at FROM meta WHERE source=?",
                (source_name,)
            ).fetchone()
        return row[0] if row else None

