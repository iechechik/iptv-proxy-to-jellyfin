"""Профили cookies YouTube: хранение, активный профиль, статус проверки.

Файлы лежат в /app/db/yt-cookies/ (каталог смонтирован с хоста, переживает
пересборку образа). index.json держит активный профиль и результаты проверок;
сами cookies — отдельные .txt в формате Netscape, права 0600.
"""
import json
import os
import re
import time

from core.config import logger

_DB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "yt-cookies"
)
_INDEX = "index.json"

AUTH_COOKIES = (
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
)


def dir_path():
    os.makedirs(_DB_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(_DB_DIR, 0o700)
    except OSError:
        pass
    return _DB_DIR


def _index_path():
    return os.path.join(dir_path(), _INDEX)


def read_index():
    try:
        with open(_index_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("index is not a dict")
        data.setdefault("active", "")
        data.setdefault("profiles", {})
        return data
    except Exception:
        return {"active": "", "profiles": {}}


def _write_index(data):
    path = _index_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _safe_name(name: str) -> str:
    # \w в Python 3 — юникодный: кириллица в именах профилей допустима.
    safe = re.sub(r"[^\w.\-]+", "_", (name or "").strip(), flags=re.UNICODE)
    safe = safe[:40].strip("._-")
    if not safe:
        raise ValueError("пустое имя профиля: нужны буквы или цифры")
    return safe


def profile_path(name: str):
    return os.path.join(dir_path(), _safe_name(name) + ".txt")


def active_path():
    """Путь к cookies активного профиля или None, если его нет."""
    idx = read_index()
    name = idx.get("active") or ""
    if not name:
        return None
    path = profile_path(name)
    return path if os.path.exists(path) else None


def cookie_lines(text: str):
    """Строки cookies в формате Netscape (7 полей через таб)."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            out.append(parts)
    return out


def validate(text: str):
    lines = cookie_lines(text)
    if len(lines) < 3:
        return False, (
            "это не похоже на cookies.txt: строк в формате Netscape "
            f"найдено {len(lines)}, нужно ≥3 (7 полей через табуляцию)"
        )
    names = {p[5].strip() for p in lines if len(p) > 5}
    # partial-session-v1: полный web-сеанс YouTube включает не только SID с
    # *PAPISID, но и HSID/SSID и хотя бы один __Secure-*PSID. Если их нет —
    # это сессия, остановленная на подтверждении Google, и толку от неё нет.
    missing = [c for c in ("SID", "HSID", "SSID", "SAPISID") if c not in names]
    if not ({"__Secure-1PSID", "__Secure-3PSID"} & names):
        missing.append("__Secure-3PSID")
    if missing:
        return False, (
            "сессия неполная — нет cookies " + ", ".join(missing) +
            ". Скорее всего вход не доведён до конца (подтверждение Google не пройдено)"
        )
    return True, f"формат ок: {len(lines)} cookies"


def save(name: str, text: str):
    ok, detail = validate(text)
    if not ok:
        return False, detail
    path = profile_path(name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    idx = read_index()
    prof = idx["profiles"].get(_safe_name(name)) or {}
    prof.update({"saved_at": time.time(), "size": os.path.getsize(path)})
    idx["profiles"][_safe_name(name)] = prof
    if not idx.get("active"):
        idx["active"] = _safe_name(name)
    _write_index(idx)
    logger.info(f"[YT-COOKIES] профиль '{_safe_name(name)}' сохранён ({os.path.getsize(path)} Б)")
    return True, detail


def set_check(name: str, ok: bool, detail: str):
    idx = read_index()
    key = _safe_name(name)
    prof = idx["profiles"].get(key) or {}
    prof.update({"check_ok": bool(ok), "check_detail": (detail or "")[:200], "check_at": time.time()})
    idx["profiles"][key] = prof
    _write_index(idx)


def mark_expired(detail: str = ""):
    """Отметить активный профиль как непринятый (например, 'Sign in to confirm')."""
    idx = read_index()
    key = idx.get("active") or ""
    if not key:
        return
    prof = idx["profiles"].get(key) or {}
    prof.update({"check_ok": False, "check_detail": (detail or "cookies не приняты")[:200], "check_at": time.time()})
    idx["profiles"][key] = prof
    _write_index(idx)
    logger.warning(f"[YT-COOKIES] профиль '{key}' помечен как непринятый: {(detail or '')[:120]}")


def select(name: str):
    key = _safe_name(name)
    if not os.path.exists(profile_path(key)):
        return False, "профиль не найден"
    idx = read_index()
    idx["active"] = key
    _write_index(idx)
    logger.info(f"[YT-COOKIES] активный профиль: {key}")
    return True, "активный профиль переключён"


def delete(name: str):
    key = _safe_name(name)
    path = profile_path(key)
    if os.path.exists(path):
        os.remove(path)
    idx = read_index()
    idx["profiles"].pop(key, None)
    if idx.get("active") == key:
        idx["active"] = next(iter(idx["profiles"]), "")
    _write_index(idx)
    logger.info(f"[YT-COOKIES] профиль '{key}' удалён")
    return True, "удалено"


def status():
    idx = read_index()
    active = idx.get("active") or ""
    out = []
    seen = set()
    for key, prof in (idx.get("profiles") or {}).items():
        seen.add(key)
        path = profile_path(key)
        out.append({
            "name": key,
            "active": key == active,
            "exists": os.path.exists(path),
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
            "saved_at": (prof or {}).get("saved_at"),
            "check_ok": (prof or {}).get("check_ok"),
            "check_detail": (prof or {}).get("check_detail"),
            "check_at": (prof or {}).get("check_at"),
        })
    # файлы, о которых индекс не знает (например, положили руками)
    for fn in os.listdir(dir_path()):
        if fn.endswith(".txt"):
            key = fn[:-4]
            if key not in seen:
                path = os.path.join(dir_path(), fn)
                out.append({
                    "name": key, "active": key == active, "exists": True,
                    "size": os.path.getsize(path), "saved_at": None,
                    "check_ok": None, "check_detail": "не проверялся", "check_at": None,
                })
    out.sort(key=lambda p: (not p["active"], p["name"]))
    return {"active": active, "profiles": out}
