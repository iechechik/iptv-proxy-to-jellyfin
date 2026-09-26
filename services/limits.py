"""
Общие семафоры для ограничения параллельных тяжёлых операций.

Используется в:
  services/healthcheck.py  — при проверке каналов
  services/fallback.py     — при переключении на живой стрим
  routers/channels.py      — при ручных проверках из UI
  routers/stream.py        — при FlareSolverr-фолбэке на сегментах

Семафоры:
  resolver_sem      — все резолвы (direct/yt-dlp/streamlink/sniffer/flare)
  sniffer_sem       — внутри resolver_sem, только Chromium
  flaresolverr_sem  — внутри resolver_sem, только FlareSolverr
  probe_sem         — ffprobe

Единственный экземпляр на процесс. Модуль импортируется один раз.
"""
import threading

from core.config import (
    IPTV_RESOLVER_LIMIT, IPTV_SNIFFER_LIMIT,
    IPTV_FLARESOLVERR_LIMIT, IPTV_PROBE_LIMIT,
)

resolver_sem = threading.Semaphore(IPTV_RESOLVER_LIMIT)
sniffer_sem = threading.Semaphore(IPTV_SNIFFER_LIMIT)
flaresolverr_sem = threading.Semaphore(IPTV_FLARESOLVERR_LIMIT)
probe_sem = threading.Semaphore(IPTV_PROBE_LIMIT)


def resolve_with_semaphores(temp_ch: dict):
    """Резолв с общим семафором + специфичным для sniffer/flare.

    temp_ch — словарь канала/стрима с ключами url, resolver, ua, fs_regex.
    Возвращает (is_direct, payload, expire_time, method).
    """
    # Ленивый импорт: limits.py импортируется из healthcheck, который
    # сам импортируется из core.main — а resolver тяжёлый (playwright).
    # Импорт на уровне модуля дал бы медленный старт.
    from services.resolver import resolve_channel_payload

    resolver = (temp_ch.get("resolver") or "auto").lower()
    with resolver_sem:
        if resolver == "sniffer":
            with sniffer_sem:
                return resolve_channel_payload(temp_ch)
        if resolver in ("flaresolverr_simple", "flaresolverr_session"):
            with flaresolverr_sem:
                return resolve_channel_payload(temp_ch)
        return resolve_channel_payload(temp_ch)
