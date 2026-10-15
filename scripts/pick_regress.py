#!/usr/bin/env python3
# Регрессия отбора кандидатов: NBC (субтитры) + случаи, которые ломать нельзя.
import sys

sys.path.insert(0, "/app")
from services.chromium import _pick_best_candidate  # noqa: E402


def cand(url, is_master=False, is_media=False, status=200):
    return {"url": url, "type": "m3u8", "is_master": is_master, "is_media": is_media, "status": status}


CASES = [
    # 1) NBC: чужой мастер + реальный видео-плейлист + субтитры на родном CDN
    ("NBC Live (должен выбраться видео, не субтитры)", [
        cand("https://livehub-voidnet.onrender.com/cluster/streamcore/us/NBC_REDIS/master.m3u8", is_master=True),
        cand("https://xumo-drct-nbcnn-ir8ze.fast.nbcuni.com/live/master_1.m3u8", is_media=True),
        cand("https://xumo-drct-nbcnn-ir8ze.fast.nbcuni.com/live/master_caption.m3u8", is_media=True),
    ], "master_1.m3u8"),
    # 2) Dailymotion split A/V (Euronews): мастер обязан выигрывать у дорожек
    ("Euronews (split A/V)", [
        cand("https://cdndirector.dailymotion.com/cdn/live/video/xar7xr2.m3u8", is_master=True),
        cand("https://live2.eu-north-1a.cf.dmcdn.net/sec2(vQoE6xyDUCt_RVniLiB93)/live-h264-720.m3u8", is_media=True),
        cand("https://live2.eu-north-1a.cf.dmcdn.net/sec2(vQoE6xyDUCt_RVniLiB93)/live-aac-128.m3u8", is_media=True),
    ], "xar7xr2.m3u8"),
    # 3) обычный одиночный мастер
    ("Обычный мастер", [
        cand("https://example.invalid/live/master.m3u8", is_master=True),
    ], "master.m3u8"),
    # 4) media-плейлист без мастера
    ("Только media", [
        cand("https://example.invalid/live/index.m3u8", is_media=True),
    ], "index.m3u8"),
    # 5) битый/просроченный кандидат не должен побеждать живой
    ("Битый не побеждает живой", [
        cand("https://example.invalid/live/master.m3u8", is_master=True, status=403),
        cand("https://example.invalid/live/index.m3u8", is_media=True),
    ], "index.m3u8"),
]

ok = True
for name, cands, expect in CASES:
    got = _pick_best_candidate(cands, channel_name="test")
    got_url = (got or {}).get("url", "")
    good = expect in got_url
    ok = ok and good
    print(f"  {'OK ' if good else 'ФЕЙЛ'} {name}: выбрано …{got_url.split('/')[-1][:40]!r} (ждали {expect!r})")

# --- проверки самого помощника (он уже один раз молча ломался) ---
from services.chromium import _is_subtitle_playlist  # noqa: E402

print("\n=== помощник _is_subtitle_playlist ===")
hok = True
for u, exp in [
    ("https://xumo-drct-nbcnn-ir8ze.fast.nbcuni.com/live/master_caption.m3u8", True),
    ("https://xumo-drct-nbcnn-ir8ze.fast.nbcuni.com/live/master_1.m3u8", False),
    ("https://example.invalid/live/index.m3u8?subs=1", False),
    ("https://example.invalid/subs/index.m3u8", True),
    ("https://example.invalid/live/stream.m3u8", False),
]:
    got = _is_subtitle_playlist(u)
    good = got is exp
    hok = hok and good
    print(f"  {'OK ' if good else 'ФЕЙЛ'} {u.split('/')[-1][:34]:34s} -> {got} (ждали {exp})")
ok = ok and hok

print("\nИТОГ:", "все случаи на месте" if ok else "ЕСТЬ РЕГРЕССИЯ — не деплоить")
