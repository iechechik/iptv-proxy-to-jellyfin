**Язык / Language:** русский предпочтительнее, английский — краткая сводка в конце.
**Preferred language:** Russian; a short English summary is at the end.

---

# IPTV-Proxy

Личный проект. Без планов дальнейшего развития — публикуется как есть.

FastAPI-сервис, который отдаёт Jellyfin'у IPTV-каналы в виде M3U-плейлиста с EPG
(расписанием из публичных источников), сам разрешает ссылки на потоки, держит их
живыми и подменяет умершие на резервные.

```
Jellyfin ──/m3u──▶ канал ──/redirect/{name}.m3u8──▶ резолвер ──▶ payload ──┐
                                                                          ├─▶ HLS напрямую
                                                                          └─▶ /mux/{name}.ts (ffmpeg, если A/V раздельные)
```

## Что умеет

- **M3U-плейлист** на `/m3u` — каждый канал ведёт на `/redirect/{name}.m3u8`.
- **EPG** на `/xmltv.xml.gz` — собирается из ваших источников, фильтруется по
  каналам конфига, отдаётся с `ETag`/`Last-Modified` (Jellyfin получает 304 и не
  выкачивает один и тот же файл по 46 раз).
- **Резолверы**: `direct`, `yt-dlp`, `streamlink`, `flaresolverr_simple`,
  `flaresolverr_session`, `sniffer` (headless Chromium). Порядок перебора
  настраивается, результат кэшируется.
- **Сниффер**: собирает HLS-кандидатов со страницы, классифицирует их по имени и
  телу манифеста, выбрасывает рекламные, мёртвые (4xx) и уже истёкшие (по `exp`),
  выбирает лучший. Если у master'а аудио вынесено отдельной группой, а сам
  вариант аудио не несёт, сервис отдаёт не master, а собранный из **проверенных
  пробой** листьев манифест (у части CDN повторная загрузка master'а не проходит,
  а листья живут).
- **Проба живости** — проверяется не «payload вообще», а то, что реально пойдёт в
  плеер: вариант с максимальным битрейтом и (если есть) аудио-группа. Перед
  ffprobe делается быстрый TCP-коннект, а разбор ограничен 3 с / 1.5 МБ — иначе
  на живом «бесконечном» потоке (склейка рекламы, SSAI) ffprobe читает его до
  таймаута и канал получает ложный DOWN.
- **Кэш по сроку жизни ссылки** — TTL считается по самой ссылке (`exp` в URL,
  сессионные маркеры), а не по `Cache-Control` ответа. Проверка канала
  планируется заранее — на 0.75 от остатка жизни самой короткой ссылки канала.
  См. «Кэш и расписание проверок».
- **Микширование A/V** через ffmpeg, когда источник отдаёт видео и аудио
  раздельно (`#EXT-X-MEDIA:TYPE=AUDIO`). Входы прогреваются до старта, чтобы
  первый сеанс не остался без звука.
- **Fallback** между потоками канала: умер активный — сервис переключается на
  следующий рабочий и уводит канал в карантин (чёрный экран) на время
  переразбора.
- **Журнал событий каналов** — отдельный лог изменений конфигурации и переходов
  UP/DOWN.
- **mediainfo-кэш Jellyfin** чистится автоматически при смене режима доставки —
  Jellyfin не «залипает» на старом формате.
- **EPG prune + VACUUM** — старые программы удаляются после каждого импорта,
  база сжимается раз в сутки.
- **Ручное управление муксом** (`mux_state: auto | on | off`) на уровне потока.
- **Внешние M3U-плейлисты** — поиск URL по публичным плейлистам прямо из модалки
  канала.
- **Watchdog FlareSolverr** — опциональный скрипт, рестартует зависший контейнер
  по flag-файлу от сервиса.
- **Веб-UI** на `/manage`: каналы, потоки, EPG-источники, плейлисты, логи.

## Требования

- Docker и docker compose
- Jellyfin (или совместимый клиент с M3U + XMLTV)
- FlareSolverr — только если используете резолверы через него

## Быстрый старт

**1. Рабочий каталог и примеры:**

```bash
mkdir -p data
cp config.example.json   data/config.json
cp override.example.json data/override.json     # опционально
cp docker-compose.example.yml docker-compose.yml
```

**2. Впишите API-ключ Jellyfin** в `docker-compose.yml`:

```yaml
environment:
  IPTV_JELLYFIN_API_KEY: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

Ключ создаётся в Jellyfin: Панель управления → API-ключи → Добавить. Без ключа
EPG обновляется по штатному расписанию Jellyfin, автотриггер «обновить
расписание» работать не будет.

**3. Отредактируйте `data/config.json`** — свои каналы и источники EPG.

**4. Сборка и запуск:**

```bash
docker compose build iptv-proxy
docker compose up -d
```

**5. Веб-UI:** `http://<хост>:9098/manage`

**6. Подключение в Jellyfin (Live TV):**

| Что | URL |
| --- | --- |
| TV-источники (M3U) | `http://iptv-proxy:8000/m3u` |
| Источники телепрограмм (XMLTV) | `http://iptv-proxy:8000/xmltv.xml.gz` |

Если Jellyfin ходит к IPTV-Proxy не через docker-сеть, замените имя `iptv-proxy`
на адрес хоста, доступный из контейнера Jellyfin.

## Переменные окружения

| Переменная | Описание | По умолчанию |
| --- | --- | --- |
| `IPTV_JELLYFIN_API_KEY` | API-ключ Jellyfin. Нужен для автотриггера «обновить расписание». | — |
| `IPTV_JELLYFIN_URL` | URL Jellyfin внутри docker-сети. | `http://jellyfin:8096` |
| `IPTV_MANAGE_URL` | URL самого сервиса (используется во внутренних ссылках). | `http://iptv-proxy:8000` |
| `IPTV_CONFIG_FILE` | Путь к основному конфигу внутри контейнера. | `/app/config.json` |
| `IPTV_LOG_LEVEL` | `debug` / `info` / `warning` / `error`. | `info` |
| `IPTV_EPG_UPDATE_TIME` | Время суточного VACUUM базы EPG. | `02:35` |
| `IPTV_CHANNEL_EVENTS_LOG` | Путь к журналу событий каналов. | `/app/logs/channel_events.log` |
| `IPTV_JELLYFIN_MEDIAINFO_DIR` | Каталог mediainfo-кэша Jellyfin внутри контейнера. | `/jellyfin-mediainfo-cache` |

Остальные параметры (TTL, лимиты резолверов, idle-таймаут мукса, интервалы
healthcheck'а) живут в `config.json` → полная схема со всеми ключами и
комментариями лежит в `config.example.json`. Временные переопределения удобно
держать в `override.json` (см. `override.example.json`): он мёржится поверх
основного конфига при старте и не перезаписывается из UI.

## Структура config.json

| Секция | Назначение |
| --- | --- |
| `epg` | Источники расписания: URL, интервал, фильтр, `update_time` = время VACUUM |
| `playlist_sources` | Внешние M3U-плейлисты для поиска URL |
| `server` | `manage_url`, имя файла override |
| `jellyfin` | URL Jellyfin, каталог его xmltv-кэша, каталог mediainfo-кэша, таймаут API |
| `resolver` | User-Agent по умолчанию, порядок резолверов, таймауты |
| `cache` | Верхние пределы TTL, пауза после неудачного резолва |
| `mux` | Параметры ffmpeg-микшера |
| `healthcheck` | Планировщик проактивных проверок каналов |
| `fallback` | Переключение на резервный поток |
| `limits` | Семафоры параллельных операций |
| `logging` | Уровень, буфер, ротация, путь журнала событий |
| `analytics` | Сборщик статистики URL (по умолчанию выключен) |
| `channels` | Список каналов |

## Кэш и расписание проверок

Кэшируется не «ответ CDN», а **ссылка**, и TTL считается по её сроку жизни:

| Случай | TTL |
| --- | --- |
| В URL есть `exp`/`expires`/`expire_at` (в том числе Akamai `~exp=`) | `exp` минус 75 секунд |
| Сессионный URL (`wmsauthsign`, `PHPSESSID`, `token=`, `.php` и т.п.) | 300 секунд |
| Всё остальное | верхний предел метода: `cache_ttl` (3600) или `fast_cache_ttl` (900) |

`Cache-Control` для TTL **не используется**: живые HLS-плейлисты почти всегда
отдаются с `no-store`/`max-age=1` (плейлист меняется каждые несколько секунд),
хотя сама ссылка живёт часами. Если брать эти заголовки за срок жизни, рабочий
payload выбрасывается каждые 30 секунд, канал уходит в бесконечный перерезолв и
падает от любой заминки.

Дальше включаются страховки:

- **проба перед записью** — в кэш не попадает ссылка, которая уже не играет;
- **измеренный срок жизни** — если ссылка канала умирала раньше верхнего
  предела, интервал проверок сжимается до `0.7 ×` минимума последних наблюдений
  (пол — 60 секунд);
- **stale-gate** — мукс, получивший 403/404 от CDN, помечает payload мёртвым и
  заказывает переразбор, не дожидаясь плановой проверки.

Плановая проверка канала ставится на `0.75 ×` остатка жизни **самой короткой**
ссылки канала: обновляем заранее, а не в момент смерти. Нижняя граница —
`healthcheck.scheduler_min_interval`, но она никогда не ставится больше срока
жизни ссылки, иначе payload гарантированно протухает между проверками. Канал,
который только что смотрели (`healthcheck.recently_active_sec`), планировщик не
трогает — его проверяет путь по запросу клиента.

Свой вердикт планировщик пишет в лог:

```
[HEALTHCHECK] 'Канал': следующий разбор через 446 с (остаток ссылки 595 с, success=True)
```

## Микширование A/V (мукс)

Мукс нужен, когда источник отдаёт видео и аудио раздельными дорожками. Режим
задаётся полем потока `mux_state`:

| Значение | Что делает |
| --- | --- |
| `auto` (по умолчанию) | Мукс включается, если в master-плейлисте есть `#EXT-X-MEDIA:TYPE=AUDIO`. Вердикт кэшируется в `streams_cache[i].needs_mux`. |
| `on` | Принудительно через мукс. Нужно для источников, которые Jellyfin не читает напрямую (SSAI-склейки, AES-128 и т.п.). |
| `off` | Принудительно без мукса. Для каналов с раздельными A/V это даст воспроизведение без звука — Jellyfin сам дорожки не миксует. |

Приоритет: `mux_state` активного потока > `needs_mux` из кэша.

Что сервис делает при старте мукса:

1. **Прогревает входы** — тянет плейлисты, пока в них не появятся сегменты.
   Иначе холодный аудио-лист на старте даёт сеанс без звука, который «лечится»
   только перезапуском канала.
2. **Решает, сколько входов** — если выбранный вариант master'а сам несёт и
   видео, и аудио, он идёт **одним** входом: поток начинается с границы сегмента
   (с ключевого кадра), и Jellyfin корректно определяет параметры видео. При двух
   входах картинка может начаться не с ключевого кадра — тогда клиент видит
   «параметры видео неизвестны» и играет только звуком.
3. **Следит за смертью входа** — три подряд 4xx от CDN → payload помечается
   мёртвым, мукс гасится, канал уходит на переразбор.

Ставится из UI: канал → модалка → «Управление потоками» → селект `mux`.
При смене `mux_state`, `active_stream_index`, `url` или `resolver` активного
потока сервис сам удаляет mediainfo-кэш Jellyfin для канала — перезапускать
Jellyfin не нужно.

## Fallback и карантин

Если активный поток канала перестал играть, сервис переключается на следующий
рабочий поток того же канала (порядок — как в конфиге). Параметры поведения —
в секции `fallback` (`cooldown`, `switch_min_sec_active`, `switch_speedup_sec`).

Пока канал в карантине (не разрезолвился, пауза `cache.failed_resolve_ttl`),
вместо ошибки отдаётся чёрный манифест (`/black.ts`) — плеер не сыпет ошибками и
не крутит бесконечную загрузку.

## EPG

Источники расписания — секция `epg.sources`. Каждый источник обновляется по
своему `interval` (`12h`, `30m`, `2d` или секунды); `disable: true` выключает
автообновление, но источник можно дёрнуть руками из UI.

Фильтр на источник:

```json
"filter": { "mode": "whitelist", "match": "any", "ids": ["channel-id"], "names": ["Channel Name"] }
```

`mode`: `all` | `whitelist` | `blacklist`; `match`: `any` | `all`.

Обслуживание базы:

- **prune** — после каждого успешного импорта удаляются программы старше 7 дней
  (в таблице хранится `start_time` в unixtime, поэтому XML не парсится);
- **VACUUM** — раз в сутки в `epg.update_time` (`PRAGMA wal_checkpoint(TRUNCATE)`
  + `VACUUM`);
- расписание импорта переживает рестарт: `meta.updated_at` читается из базы при
  старте.

Готовый XMLTV отдаётся на `/xmltv.xml.gz` вместе с `ETag` (`<файл>.md5`) и
`Last-Modified`; при совпадении Jellyfin получает `304` без тела.

## Формат канала

```json
{
    "name": "ChannelName",
    "chno": "1",
    "group": "News",
    "tvgid": "channel-id-in-epg",
    "real_name": "Channel Name as in EPG",
    "fallback": true,
    "active_stream_index": 0,
    "streams": [
        {
            "url": "https://site.example/watch",
            "resolver": "auto",
            "ua": "Mozilla/5.0 ...",
            "mux_state": "auto",
            "stream_id": 3918603096042605,
            "prefetch": true
        }
    ]
}
```

- `resolver`: `auto` | `direct` | `yt-dlp` | `streamlink` |
  `flaresolverr_simple` | `flaresolverr_session` | `sniffer`. `auto` перебирает
  резолверы в порядке `resolver.order`, пока один не сработает; конкретное имя
  заставляет использовать только его.
- `stream_id` — внутренний идентификатор потока (52-битное число). Назначается
  автоматически, нужен, чтобы слоты `streams_cache` не теряли привязку при
  переупорядочивании потоков. Руками трогать не надо.
- `prefetch` — предзагрузить первый сегмент до запроса клиента.
- `disable: true` — поток (или канал) не используется, но остаётся в конфиге.
- `comment` — свободный текст, сохраняется как есть.

## Журнал событий каналов

Отдельный текстовый лог: `logs/channel_events.log`. Читается через `tail -f`:

```
2026-10-02 18:15:23 [НТВ] cfg: stream#1 (ivi.ru/...) mux_state: auto -> on (ui)
2026-10-02 18:16:01 [SkyNews] state: UP -> DOWN (healthcheck)
2026-10-02 18:17:44 [Euronews] state: DOWN -> UP (ui)
```

- префикс `cfg:` — изменения конфигурации (UI, авто-подбор EPG);
- префикс `state:` — переходы состояния канала (UP/DOWN, смена активного потока);
- в скобках — источник: `ui`, `healthcheck`, `fallback`, `startup`, `auto_match`.

Ротация — как у основных логов (`logging.max_bytes`, `logging.backup_count`).

## mediainfo-кэш Jellyfin

Jellyfin при первом probe канала запоминает формат контейнера
(`Container: hls` или `Container: ts`) в `cache/mediainfo/*.json` и потом
переиспользует его. Если канал сменил режим доставки (HLS ↔ raw TS через мукс),
старый кэш ломает воспроизведение (Jellyfin-ffmpeg падает с
`Invalid data found when processing input`).

Сервис сам удаляет файл канала, когда меняется `mux_state`, `active_stream_index`,
`url` или `resolver` активного потока — при следующем открытии Jellyfin делает
свежий probe. Нужно только смонтировать каталог:

```yaml
volumes:
  - ./jellyfin-config/cache/mediainfo:/jellyfin-mediainfo-cache
```

Путь настраивается через `IPTV_JELLYFIN_MEDIAINFO_DIR` или
`config.json → jellyfin.mediainfo_cache_dir`.

## Внешние M3U-плейлисты

`playlist_sources` подключает публичные M3U (GitHub, кураторские списки) как
**справочник**: найти URL, проверить, подставить в поток. Автоимпорта каналов нет.

```json
"playlist_sources": [
    { "name": "example", "url": "https://.../tv.m3u", "interval": 86400, "disable": true }
]
```

Как пользоваться: вкладка «Плейлисты» в UI → добавить/обновить источник; в модалке
канала → «Управление потоками» → 🔍 рядом с полем URL → поиск по имени канала →
тап по результату делает HEAD-проверку за секунду; живой URL подставляется в
поле, мёртвый показывает HTTP-код и поле не трогает.

Из плейлиста берётся только URL (и заголовки из `#EXTVLCOPT`, если есть —
`http-referrer`, `http-user-agent`), они попадают в payload как
`url|Referer=...|User-Agent=...`. Логотипы, группы и `tvg-id` игнорируются: EPG
выбирается отдельно, в модалке канала.

## Эндпоинты

| Путь | Назначение |
| --- | --- |
| `/m3u` | M3U-плейлист для Jellyfin |
| `/xmltv.xml.gz` | EPG (XMLTV, gzip) с `ETag`/304 |
| `/redirect/{name}.m3u8` | Точка входа для каждого канала (`.ts` — то же самое) |
| `/mux/{name}.ts` | Микшированный A/V-поток (raw MPEG-TS) |
| `/synthetic/{name}.m3u8` | Сохранённый синтетический манифест канала |
| `/hls/{name}.{ext}` | Универсальный прокси HLS: `ext` = `m3u8` / `ts` / `key` / `vtt` / `m4s` / `mp4` (расширение обязательно) |
| `/hls/manifest.m3u8`, `/hls/segment.ts` | Совместимые старые роуты |
| `/black.ts` | Чёрный TS (заглушка для карантина) |
| `/manage` | Веб-UI |
| `/logs`, `/logs/data`, `/logs/clear` | Логи: просмотр, данные, очистка |
| `/status` | Краткий статус сервиса |
| `/events` | SSE-поток обновлений для UI |
| `/config/restart` | Перезапуск контейнера (POST) |
| `/channels/get`, `/channels/add`, `/channels/delete` | Каналы: чтение, добавление, удаление |
| `/channels/toggle`, `/channels/toggle-fallback` | Вкл/выкл канала, fallback |
| `/channels/update-stream` | Сохранение потоков канала (чистит mediainfo-кэш) |
| `/channels/clear-cache` | Очистка кэша канала (одного или всех) |
| `/channels/check-single`, `/channels/check-all/start`, `/channels/check-all/status/{task_id}` | Проверки каналов |
| `/channels/probe`, `/channels/check-mux` | Проба потока, проверка мукса |
| `/channels/apply-resolvers`, `/channels/reset-resolvers` | Пин/сброс резолверов |
| `/epg/save`, `/epg/sources`, `/epg/status`, `/epg/channels` | EPG: сохранение, источники, статус, каналы |
| `/epg/rebuild`, `/epg/auto-match`, `/epg/update-source/{name}`, `/epg/update-all` | Пересборка, авто-подбор tvg-id, обновление источников |
| `/playlists/sources`, `/playlists/refresh/{name}`, `/playlists/refresh-all`, `/playlists/clear/{name}`, `/playlists/search`, `/playlists/check` | Внешние M3U-плейлисты |
| `/webhooks/jellyfin` | Вебхук Jellyfin (события воспроизведения) |

## Watchdog FlareSolverr

`restart_flaresolverr.sh` — опциональный скрипт на случай, когда FlareSolverr
иногда зависает. Сервис при ошибке FlareSolverr кладёт flag-файл
(`healthcheck.flaresolverr_flag_file`), скрипт по таймеру (например,
systemd-юнитом раз в минуту) видит флаг, рестартует контейнер и снимает флаг.
Есть кулдаун, чтобы не уйти в цикл рестартов. Внутри скрипта есть путь к каталогу
с конфигом — поменяйте под свой сетап; пример systemd-юнита — в шапке скрипта.

## Обратная связь

Issues читаю. Отвечать не обещаю.

## Лицензия

Unlicense — public domain. Делайте что хотите, без обязательств. Полный текст:
`LICENSE`.

---

# English (short summary)

A personal project, published as-is — no plans for further development.

FastAPI service that feeds IPTV channels to Jellyfin as an M3U playlist with EPG.
It resolves stream URLs itself (direct, yt-dlp, streamlink, FlareSolverr, headless
Chromium "sniffer"), keeps the links fresh, switches to a backup stream when the
active one dies, and can mux separately delivered video/audio via ffmpeg.

Key points worth knowing:

- **Cache TTL follows the LINK, not the response.** Live HLS playlists are served
  with `Cache-Control: no-store`/`max-age=1` even though the URL stays valid for
  hours; TTL comes from `exp` in the URL, session markers, or the per-method cap.
- **Checks are scheduled at 0.75 of the shortest remaining link lifetime**, never
  later than the link itself. Observed early deaths shrink the interval further.
- **Liveness probing verifies what actually plays** (max-bandwidth variant plus the
  audio group), with a 3 s / 1.5 MB analysis window and a fast TCP pre-check — a
  live "endless" SSAI feed must not produce a false DOWN.
- **The mux warms its inputs before starting** (a cold audio playlist would give a
  soundless session), uses a single input when the variant already carries both
  tracks, and marks the payload dead after three 4xx responses.
- **Endpoint summary:** `/m3u`, `/xmltv.xml.gz`, `/redirect/{name}.m3u8`,
  `/mux/{name}.ts`, `/manage`, `/status`, `/logs`, `/events`.
- **Config:** `config.example.json` documents every supported key;
  `override.example.json` shows temporary overrides (merged on top at startup).
  The API key goes into the environment (`IPTV_JELLYFIN_API_KEY`).

Full details are in the Russian part above — it is the authoritative version.
