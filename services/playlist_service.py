"""
Сервис внешних M3U-плейлистов.

Роль: загрузка, парсинг, хранение и поиск каналов из внешних плейлистов.
Изолирован от core/state.py, healthcheck, EPG. Своя SQLite, своя блокировка.

Использование:
  - playlist_sources в config.json → periodic_playlist_update → refresh_source
  - refresh_source → download_playlist → PlaylistManager.import_source → SQLite
  - UI-поиск: PlaylistManager.search(q)
  - Проверка по тапу: quick_check(url_payload)

Что берём из плейлиста: только URL + #EXTVLCOPT (Referer/UA).
Что игнорируем: tvg-id, tvg-logo, group-title, tvg-name.
"""

import gzip
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import core.config as cfg
from core.config import logger


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

PLAYLIST_DB_FILE = "/app/db/playlists.db"

# Регексп атрибутов в #EXTINF: key="value"
_ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')

# Таймаут скачивания плейлиста
_DOWNLOAD_TIMEOUT = 60

# Таймаут quick_check (по тапу в UI)
_QUICK_CHECK_TIMEOUT = 1.0


# ---------------------------------------------------------------------------
# Парсинг M3U
# ---------------------------------------------------------------------------

def _find_name_in_extinf(line: str):
    """Возвращает имя канала — всё после первого 'вне кавычек' запятой.

    #EXTINF:-1 tvg-id="x",НТВ → 'НТВ'
    #EXTINF:-1,НТВ            → 'НТВ'
    #EXTINF:-1                → None (нет запятой)
    """
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ',' and not in_quotes:
            name = line[i + 1:].strip()
            return name if name else None
    return None


def parse_m3u(text: str):
    """Генератор записей из M3U-текста.

    Yields dict: {"name": str, "url_payload": str}
    """
    lines = text.splitlines()

    # Состояние текущей записи
    pending_name = None
    pending_referer = None
    pending_ua = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF:"):
            # Начинаем новую запись. Если предыдущая не была закрыта
            # URL — она сбрасывается (битый M3U).
            pending_name = _find_name_in_extinf(line)
            pending_referer = None
            pending_ua = None
            continue

        if line.startswith("#EXTVLCOPT:"):
            opt = line[len("#EXTVLCOPT:"):].strip()
            if opt.startswith("http-referrer="):
                val = opt[len("http-referrer="):].strip()
                if val:
                    pending_referer = val
            elif opt.startswith("http-user-agent="):
                val = opt[len("http-user-agent="):].strip()
                if val:
                    pending_ua = val
            continue

        if line.startswith("#"):
            # Прочие теги — игнорируем
            continue

        # Не комментарий, не тег — это URL. Закрывает запись.
        if pending_name is None:
            # URL без предшествующего #EXTINF — пропускаем
            continue

        url = line

        # Собираем payload
        payload = url
        if pending_referer:
            payload += f"|Referer={pending_referer}"
        if pending_ua:
            payload += f"|User-Agent={pending_ua}"

        yield {"name": pending_name, "url_payload": payload}

        # Сбрасываем состояние
        pending_name = None
        pending_referer = None
        pending_ua = None


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

class PlaylistManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._write_lock = threading.Lock()
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
                    CREATE TABLE IF NOT EXISTS sources (
                        name TEXT PRIMARY KEY,
                        url TEXT NOT NULL,
                        updated_at REAL,
                        channel_count INTEGER,
                        last_error TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS channels (
                        source TEXT NOT NULL,
                        name TEXT NOT NULL,
                        url_payload TEXT NOT NULL,
                        PRIMARY KEY (source, url_payload)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_channels_name ON channels(name)"
                )

    def import_source(self, source_name: str, url: str, text: str):
        """Распарсить M3U и перезаписать каналы источника.

        DELETE старых + INSERT новых + обновить meta — в одной транзакции.
        """
        entries = list(parse_m3u(text))

        with self._write_lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("DELETE FROM channels WHERE source=?", (source_name,))
                    for entry in entries:
                        conn.execute(
                            "INSERT OR IGNORE INTO channels (source, name, url_payload) "
                            "VALUES (?, ?, ?)",
                            (source_name, entry["name"], entry["url_payload"]),
                        )
                    conn.execute(
                        "INSERT OR REPLACE INTO sources (name, url, updated_at, channel_count, last_error) "
                        "VALUES (?, ?, ?, ?, NULL)",
                        (source_name, url, time.time(), len(entries)),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

        logger.info(f"[PLAYLIST] '{source_name}': imported {len(entries)} channels")
        return len(entries)

    def mark_source_error(self, source_name: str, url: str, error: str):
        """Записать ошибку обновления. Данные каналов не трогаем."""
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sources (name, url, updated_at, channel_count, last_error) "
                    "VALUES (?, ?, "
                    "  COALESCE((SELECT updated_at FROM sources WHERE name=?), 0), "
                    "  COALESCE((SELECT channel_count FROM sources WHERE name=?), 0), "
                    "  ?)",
                    (source_name, url, source_name, source_name, error[:500]),
                )
                conn.commit()

    def search(self, q: str, source: str = None, limit: int = 150):
        """Поиск по имени. Возвращает список dict.

        Сортировка: точное совпадение → префикс → подстрока.
        """
        if not q:
            return []
        q_lower = q.lower()

        # Фильтрация в Python, а не в SQL: SQLite LOWER() работает только
        # с ASCII, кириллицу не понижает. Раньше `LOWER(name) LIKE '%нтв%'`
        # не находил `НТВ HD`. Для 300-100K строк фильтрация в Python
        # мгновенная или ≤ 500 мс.
        with self._connect() as conn:
            if source:
                rows = conn.execute(
                    "SELECT source, name, url_payload FROM channels WHERE source = ?",
                    (source,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT source, name, url_payload FROM channels"
                ).fetchall()

        rows = [r for r in rows if q_lower in r[1].lower()]

        results = []
        for src, name, url_payload in rows:
            nl = name.lower()
            if nl == q_lower:
                rank = 0
            elif nl.startswith(q_lower):
                rank = 1
            else:
                rank = 2
            results.append({"rank": rank, "source": src, "name": name, "url_payload": url_payload})

        results.sort(key=lambda r: (r["rank"], r["name"].lower(), r["source"]))
        trimmed = results[:limit]
        for r in trimmed:
            r.pop("rank", None)
        return trimmed

    def get_sources_stats(self):
        """Список источников с метаданными."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, url, updated_at, channel_count, last_error FROM sources ORDER BY name"
            ).fetchall()
        return [
            {
                "name": r[0],
                "url": r[1],
                "updated_at": r[2],
                "channel_count": r[3] or 0,
                "last_error": r[4],
            }
            for r in rows
        ]

    def clear_source(self, source_name: str):
        """Удалить все каналы и метаданные источника. config.json не трогает."""
        with self._write_lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM channels WHERE source=?", (source_name,))
                conn.execute("DELETE FROM sources WHERE name=?", (source_name,))
                conn.commit()
        logger.info(f"[PLAYLIST] '{source_name}': cleared")


# Глобальный экземпляр
playlist_manager = PlaylistManager(db_path=PLAYLIST_DB_FILE)


# ---------------------------------------------------------------------------
# Скачивание
# ---------------------------------------------------------------------------

def download_playlist(url: str) -> str:
    """Скачать M3U. gzip-aware. Возвращает текст.

    gzip детектим по magic bytes (0x1f 0x8b), как epg_service.
    Content-Encoding: gzip тоже учитываем через urllib (он сам
    разжимает, если заголовок есть).
    """
    req = urllib.request.Request(url, headers={"User-Agent": cfg.IPTV_DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
        raw = resp.read()

    # gzip без Content-Encoding (некоторые CDN отдают так)
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception as e:
            logger.warning(f"[PLAYLIST] gzip decompress failed for {url}: {e}")

    return raw.decode("utf-8", errors="ignore")


def refresh_source(source: dict) -> bool:
    """Скачать и импортировать один источник. Обновляет meta."""
    name = source.get("name")
    url = source.get("url")
    if not name or not url:
        return False
    try:
        text = download_playlist(url)
        count = playlist_manager.import_source(name, url, text)
        logger.info(f"[PLAYLIST] '{name}': refreshed, {count} channels")
        return True
    except Exception as e:
        logger.error(f"[PLAYLIST] '{name}': refresh failed: {e}")
        try:
            playlist_manager.mark_source_error(name, url, str(e))
        except Exception as e2:
            logger.warning(f"[PLAYLIST] '{name}': failed to mark error: {e2}")
        return False


# ---------------------------------------------------------------------------
# Периодическое обновление
# ---------------------------------------------------------------------------

_playlist_update_lock = threading.Lock()


def periodic_playlist_update():
    """Фоновый поток. Раз в 60 сек проверяет interval каждого источника.

    Читает источники из cfg.get_playlist_sources() каждый цикл —
    через UI пользователь может добавлять/менять.
    """
    time.sleep(15)  # начальная пауза, чтобы старт не совпал с EPG

    while True:
        try:
            sources = cfg.get_playlist_sources()
            # last_update берём из БД по каждому источнику
            stats = {s["name"]: s["updated_at"] for s in playlist_manager.get_sources_stats()}

            now = time.time()
            for src in sources:
                if src.get("disable", False):
                    continue
                name = src.get("name")
                if not name:
                    continue
                interval = src.get("interval", 86400)
                last = stats.get(name) or 0
                if now - last < interval:
                    continue

                if not _playlist_update_lock.acquire(blocking=False):
                    logger.info("[PLAYLIST] update already in progress, skipping")
                    break
                try:
                    refresh_source(src)
                finally:
                    _playlist_update_lock.release()
        except Exception as e:
            logger.error(f"[PLAYLIST] periodic update error: {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Проверка по тапу (лёгкая, 1 сек)
# ---------------------------------------------------------------------------

def quick_check(url_payload: str) -> tuple:
    """HEAD (1 сек) на чистый URL. При 405/501 → GET Range: bytes=0-0.

    Возвращает (ok: bool, detail: str).
    Без семафоров, без блокировок — одиночный запрос.
    """
    if not url_payload:
        return False, "empty url"

    # Отделяем URL от |Referer=...|User-Agent=...
    clean_url = url_payload.split("|", 1)[0]
    if not clean_url:
        return False, "empty url"

    if not clean_url.startswith(("http://", "https://")):
        return False, "not http(s)"

    # Заголовки из payload
    headers = {"User-Agent": cfg.IPTV_DEFAULT_UA}
    if "|" in url_payload:
        for part in url_payload.split("|")[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                if k == "Referer":
                    headers["Referer"] = v
                elif k == "User-Agent":
                    headers["User-Agent"] = v

    def _do_request(method: str, extra: dict = None):
        h = dict(headers)
        if extra:
            h.update(extra)
        req = urllib.request.Request(clean_url, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=_QUICK_CHECK_TIMEOUT) as resp:
            return resp.status

    try:
        status = _do_request("HEAD")
        if 200 <= status < 400:
            return True, f"{status}"
        # 4xx/5xx — пробуем GET Range
        raise urllib.error.HTTPError(clean_url, status, "", {}, None)
    except urllib.error.HTTPError as e:
        if e.code in (405, 501):
            # HEAD не поддержан — GET Range
            try:
                status = _do_request("GET", {"Range": "bytes=0-0"})
                if 200 <= status < 400:
                    return True, f"{status} (GET)"
                return False, f"{status}"
            except urllib.error.HTTPError as e2:
                return False, f"{e2.code}"
            except Exception as e2:
                return False, type(e2).__name__
        if 300 <= e.code < 400:
            return True, f"{e.code}"
        return False, f"{e.code}"
    except Exception as e:
        # timeout, connection refused, dns
        return False, type(e).__name__
