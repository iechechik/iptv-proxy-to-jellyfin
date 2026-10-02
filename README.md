**Язык / Language:** русский предпочтительнее. Английский — ниже, для справки.
**Preferred language:** Russian. English version below, for reference.

---

# Русский

Личный проект. Без планов дальнейшего развития — публикуется как есть.

FastAPI-сервис, который отдаёт Jellyfin'у IPTV-каналы в виде M3U-плейлиста
с EPG (расписанием, собранным из публичных баз), автоматическим разрешением
потоков и fallback'ом между стримами канала.

---

## Что умеет

- **M3U-плейлист** на `/m3u` — каждый канал ведёт на `/redirect/{name}.m3u8`.
- **EPG** на `/xmltv.xml.gz` — собирается из указанных вами источников и
  фильтруется по каналам из конфига.
- **Резолверы**: прямой URL, `yt-dlp`, `streamlink`, FlareSolverr,
  headless-Chromium («sniffer»). Порядок применения задаётся в конфиге,
  результат кэшируется по TTL. Sniffer собирает все HLS-кандидаты
  (master/media/ad/embed), классифицирует их по имени файла и телу
  манифеста, отбрасывает рекламные (`_is_ad_url`, `_is_ad_manifest`) и
  выбирает лучший.
- **Fallback**: если активный стрим канала умер, сервис переключается на
  следующий рабочий из списка потоков этого канала.
- **Микширование A/V** через ffmpeg, если источник отдаёт видео и аудио
  раздельно (`#EXT-X-MEDIA:TYPE=AUDIO`). Включается автоматически по
  `needs_mux` или вручную через `mux_state` (см. ниже).
- **Ручное управление муксом** — `mux_state: auto | on | off` на уровне
  потока. Удобно, когда канал воспроизводится только через мукс
  (например, Pluto/AES-128) или наоборот — мукс не нужен.
- **Внешние M3U-плейлисты** — поиск каналов по публичным плейлистам
  (GitHub и т.п.) прямо из модалки потоков. Найденный URL можно проверить
  одной кнопкой и подставить в поток канала.
- **Журнал событий каналов** — отдельный текстовый лог изменений
  (конфигурация + переходы UP/DOWN). См. раздел «Журнал событий каналов».
- **EPG prune + VACUUM** — старые программы (7+ дней) удаляются после
  каждого успешного импорта источника; БД сжимается (`VACUUM`) раз в
  сутки в `IPTV_EPG_UPDATE_TIME`. `meta.updated_at` переживает рестарт —
  расписание импорта не сбрасывается.
- **Watchdog FlareSolverr** — опциональный скрипт, рестартует зависший
  контейнер по flag-файлу от `resolver.py`.
- **Веб-UI** на `/manage`: список каналов, модалка с потоками, вкладки
  EPG-источников, плейлистов и логов.

---

## Требования

- Docker и docker compose
- Jellyfin (или совместимый клиент, умеющий M3U + XMLTV)
- FlareSolverr — только если планируете использовать резолверы через него

---

## Быстрый старт

Все нужные файлы лежат в корне проекта.

**1. Создайте рабочий каталог `data/` и скопируйте в него примеры:**

```bash
mkdir -p data
cp config.example.json   data/config.json
cp override.example.json data/override.json      # опционально
cp docker-compose.example.yml docker-compose.yml
2. Впишите API-ключ Jellyfin в docker-compose.yaml:

text
IPTV_JELLYFIN_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Ключ создаётся в Jellyfin: Панель управления → API-ключи → Добавить.
Без ключа EPG обновляется по штатному расписанию Jellyfin, автотриггер
refresh guide работать не будет.

3. Отредактируйте data/config.json — свои каналы и EPG-источники.

4. Соберите и запустите:

bash
docker compose build iptv-proxy
docker compose up -d
5. Откройте веб-UI:

text
http://<хост>:9098/manage
6. Подключите к Jellyfin (Live TV):

Что	URL
TV-источники (M3U)	http://iptv-proxy:8000/m3u
Источники телепрограмм (XMLTV)	http://iptv-proxy:8000/xmltv.xml.gz
Если Jellyfin ходит к IPTV-Proxy не через docker-сеть, замените имя
iptv-proxy на адрес хоста, доступный из контейнера Jellyfin.

Переменные окружения
Переменная	Описание	По умолчанию
IPTV_JELLYFIN_API_KEY	API-ключ Jellyfin. Нужен для автотриггера refresh guide.	—
IPTV_JELLYFIN_URL	URL Jellyfin внутри docker-сети.	http://jellyfin:8096
IPTV_MANAGE_URL	URL самого сервиса. Используется во внутренних ссылках.	http://iptv-proxy:8000
IPTV_CONFIG_FILE	Путь к основному конфигу внутри контейнера.	/app/config.json
IPTV_LOG_LEVEL	debug / info / warning / error.	info
Остальные тюнинги (TTL кэша, лимиты резолверов, idle-таймаут мукса,
интервалы healthcheck'а и т.п.) описаны в config.example.json.
Их можно менять прямо там или через override.json — второй мёржится
поверх первого при старте.

Структура config.json
Секция	Назначение
epg	Источники EPG: URL, интервал обновления, фильтр по id/именам, update_time = время VACUUM
playlist_sources	Внешние M3U-плейлисты для поиска каналов
server	manage_url, имя файла override
jellyfin	URL Jellyfin, путь до его xmltv-кэша, таймаут API
resolver	Дефолтный User-Agent, порядок резолверов, таймауты
cache	TTL кэша для разных типов резолва
mux	Параметры ffmpeg-микшера
healthcheck	Планировщик проактивных проверок каналов
fallback	Переключение на резервный стрим
limits	Семафоры на параллельные резолвы
logging	Уровень, размер буфера, ротация
analytics	Сборщик статистики URL (по умолчанию выключен)
channels	Список каналов
EPG: prune старых программ и VACUUM
В programmes пишется start_time (unixtime) — парсится из XMLTV start.
Это позволяет удалять старые программы без парсинга XML.

Логика:

Prune — после каждого успешного import_source. Удаляет
программы старше 7 дней. Записи без start_time (старые, до миграции)
сохраняются до следующего переимпорта — после переимпорта у них появится
start_time.

VACUUM — раз в сутки, в IPTV_EPG_UPDATE_TIME (по умолчанию 02:35).
Сжимает БД (PRAGMA wal_checkpoint(TRUNCATE) + VACUUM).
У programmes после удаления остаётся freelist — VACUUM возвращает место ОС.

Расписание импорта источников — по interval каждого источника
(iptvx: 12ч, us_guide_nbc: 24ч). last_update читается из
meta.updated_at при старте — рестарт контейнера не сбрасывает таймер.
Пустая БД → last_update = 0 → импорт при первом же цикле.

IPTV_EPG_UPDATE_TIME больше не «ночное обновление всех источников»
(это было дважды: ночной апдейт всех + интервалы — источники могли
импортироваться два раза). Теперь это только время VACUUM.

Формат канала
json
{
    "name": "ChannelName",
    "chno": "1",
    "group": "News",
    "tvgid": "channel-id-in-epg",
    "real_name": "Channel Name as in EPG",
    "streams": [
        {
            "url": "https://site.example/watch",
            "resolver": "auto",
            "ua": "Mozilla/5.0 ...",
            "mux_state": "auto",
            "stream_id": 3918603096042605
        }
    ],
    "fallback": true
}
Поле resolver — одно из:

text
auto | direct | yt-dlp | streamlink
flaresolverr_simple | flaresolverr_session | sniffer
auto перебирает резолверы по порядку из секции resolver.order, пока
один не сработает. Конкретное имя заставляет использовать только его.

Поле stream_id — внутренний идентификатор потока (52-битное число).
Назначается автоматически при добавлении/сохранении. Нужен, чтобы слоты
streams_cache не теряли привязку при переупорядочивании потоков. Руками
трогать не надо.

Ручное управление муксом (mux_state)
Флаг mux_state живёт на уровне потока (streams[i].mux_state).
Возможные значения:

Значение	Что делает
auto (по умолчанию)	Как раньше: мукс включается, если в master-плейлисте есть #EXT-X-MEDIA:TYPE=AUDIO (раздельные A/V). Считается один раз, кэшируется в streams_cache[i].needs_mux.
on	Принудительно через мукс. Удобно для источников, где Jellyfin не читает HLS напрямую: Pluto (AES-128 + ffmpeg 7+ regression), SSAI-потоки и т.п.
off	Принудительно без мукс. Удобно, когда мукс не нужен и хочется сэкономить CPU. Для двухвходовых каналов (с #EXT-X-MEDIA:TYPE=AUDIO) это приведёт к воспроизведению без звука — Jellyfin сам не миксует.
Приоритет: mux_state из активного stream > needs_mux из кэша.

Ставится из UI: канал → модалка → 🎬 Управление потоками → селект mux:
рядом с Prefetch. Или напрямую в config.json.

Замечание про on: работает для любого потока, где Jellyfin-ffmpeg
успешно читает raw TS от мукса. При смене mux_state (auto ↔ on ↔ off),
active_stream_index, url или resolver активного потока iptv-proxy
автоматически удаляет mediainfo-кэш Jellyfin для этого канала, поэтому
перезагрузка Jellyfin не нужна и «залипания» старого режима не происходит
(см. раздел «mediainfo-кэш Jellyfin» ниже).

Замечание про off: если канал имеет #EXT-X-MEDIA:TYPE=AUDIO
(раздельные дорожки), Jellyfin не сможет их смикшировать сам —
будет без звука или упадёт. Для таких каналов — auto или on.

Журнал событий каналов
Отдельный текстовый лог с историей изменений: logs/channel_events.log.
Формат построчный, читается через tail -f:

text
2026-10-02 18:15:23 [НТВ] cfg: stream#1 (ivi.ru/...) mux_state: auto -> on (ui)
2026-10-02 18:16:01 [SkyNews] state: UP -> DOWN (healthcheck)
2026-10-02 18:17:44 [Euronews] state: DOWN -> UP (ui)
Префикс cfg: — конфигурационные изменения (пользователь через UI
или авто-подбор EPG). Префикс state: — переходы состояния канала
(UP/DOWN, автоматические переключения активного потока).
В скобках в конце — источник: ui, healthcheck, fallback,
startup, auto_match.

Ротация — те же параметры, что у основных логов
(IPTV_LOG_MAX_BYTES, IPTV_LOG_BACKUP_COUNT).

Путь к файлу: IPTV_CHANNEL_EVENTS_LOG (env) или
config.json → logging.channel_events_log. По умолчанию —
/app/logs/channel_events.log.

mediainfo-кэш Jellyfin
Jellyfin при первом probe канала сохраняет в cache/mediainfo/*.json
информацию о контейнере (Container: hls или Container: ts).
При последующих открытиях канала он не делает probe заново, а
использует закэшированный формат.

Если канал сменил режим доставки (HLS ↔ raw TS через мукс) — старый
кэш ломает воспроизведение (Jellyfin-ffmpeg падает с exit 183
«Invalid data found when processing input»).

Решение: iptv-proxy автоматически удаляет mediainfo-кэш Jellyfin
для канала при изменении:

mux_state активного потока (auto ↔ on ↔ off),

active_stream_index,

url активного потока,

resolver активного потока.

При следующем открытии канала Jellyfin делает свежий probe и
корректно определяет формат. Перезагрузка Jellyfin не нужна.

Требуется монтирование в docker-compose.yaml:

yaml
- /opt/docker-compose/configs/jellyfin/cache/mediainfo:/jellyfin-mediainfo-cache
Путь настраивается через IPTV_JELLYFIN_MEDIAINFO_DIR (env) или
config.json → jellyfin.mediainfo_cache_dir.

Внешние M3U-плейлисты
Раздел playlist_sources позволяет подключить публичные M3U-плейлисты
(GitHub, кураторские списки и т.п.) и искать в них URL для потоков
каналов. Это не импорт каналов в config.json, а справочник: ищешь,
проверяешь, подставляешь.

json
"playlist_sources": [
    {
        "name": "smolnp_main",
        "url": "https://raw.githubusercontent.com/smolnp/IPTVru/refs/heads/gh-pages/IPTVru.m3u",
        "interval": 86400,
        "disable": true
    }
]
Поля:

Поле	Назначение
name	Уникальное имя источника
url	URL M3U-плейлиста
interval	Интервал обновления в секундах (фон, раз в минуту проверяется)
disable	true — не обновлять автоматически (можно дёрнуть вручную из UI)
Как пользоваться:

Вкладка «Плейлисты» в веб-UI — управление источниками: добавить,
редактировать, обновить один, обновить все, очистить данные.

В модалке канала → «Управление потоками» → кнопка 🔍 рядом
с полем URL. Откроется поиск по плейлистам.

Набери имя канала (минимум 2 буквы). Результаты — по всем активным
источникам.

Тап на результат → HEAD-проверка URL за 1 секунду.

URL живой — подставляется в поле потока.

Мёртвый — сообщение с HTTP-кодом, поле не трогается.

Особенности:

Не матчит EPG. tvg-id из плейлиста игнорируется. Смысл — взять
URL. EPG выбирается отдельно, в правой колонке модалки канала.

Проверка по тапу — только HEAD, 1 секунда. Не ffprobe. Если нужно
точнее — после подстановки URL есть обычные кнопки «Проверить поток»
и «Проверить ffprobe».

Никаких автоимпортов. Каналы добавляются вручную, через обычные
модалки. Плейлисты только помогают найти URL.

#EXTVLCOPT парсится. Если в плейлисте есть http-referrer или
http-user-agent — они попадут в payload (url|Referer=...|User-Agent=...).

Логотипы, группы, tvg-id игнорируются. Из плейлиста берётся
только URL (+ опционально заголовки из #EXTVLCOPT).

disable: true в примере — намеренно. При старте ничего не
скачивается. Ты сам включаешь нужные источники и жмёшь «Обновить».

Эндпоинты
Путь	Назначение
/m3u	M3U-плейлист для Jellyfin
/xmltv.xml.gz	EPG в формате XMLTV (gzip)
/redirect/{name}.m3u8	Точка входа для каждого канала
/hls/{name}.{ext}	Универсальный прокси HLS: ext = m3u8 / ts / key / vtt / m4s / mp4. Расширение в пути обязательно, иначе Jellyfin-ffmpeg 8+ отказывается открывать.
/hls/manifest.m3u8	Совместимый старый роут (HLS-манифест)
/hls/segment.ts	Совместимый старый роут (сегмент)
/mux/{name}.ts	Микшированный A/V-поток (raw MPEG-TS)
/manage	Веб-UI
/logs	Просмотр логов
/status	Краткий статус сервиса
/events	SSE-поток обновлений для UI
/playlists/sources	GET/POST — список и сохранение источников M3U-плейлистов
/playlists/refresh/{name}	Обновить один источник (фоново)
/playlists/refresh-all	Обновить все активные источники
/playlists/clear/{name}	Очистить данные источника в SQLite
/playlists/search?q=...	Поиск по кэшу плейлистов
/playlists/check	HEAD-проверка URL (используется UI при тапе)
Watchdog FlareSolverr
restart_flaresolverr.sh — опциональный скрипт для случая, когда
FlareSolverr иногда зависает.

Как работает: resolver.py при ошибке FlareSolverr кладёт flag-файл
flags/flaresolverr_restart.request. Скрипт по таймеру (например,
systemd-юнитом раз в минуту) видит флаг, рестартует контейнер и снимает
флаг. Есть кулдаун, чтобы не уйти в цикл рестартов.

Внутри скрипта есть путь к каталогу с конфигом — поменяйте под свой сетап.
Пример systemd-юнита смотрите в шапке скрипта.

Обратная связь
Issues читаю. Отвечать не обещаю.

Лицензия
Unlicense — public domain. Делайте что хотите, без обязательств.
Полный текст: LICENSE.

English
A personal project. No plans for further development — published as-is.

FastAPI service that serves IPTV channels to Jellyfin as an M3U playlist
with EPG (schedule pulled from public sources), automatic stream
resolution, and per-channel fallback between streams.

Features
M3U playlist at /m3u — each channel points to /redirect/{name}.m3u8.

EPG at /xmltv.xml.gz — assembled from configured sources and
filtered by the channels in your config.

Resolvers: direct URL, yt-dlp, streamlink, FlareSolverr,
headless Chromium ("sniffer"). The order is configurable; results are
cached with per-method TTL. Sniffer collects all HLS candidates
(master/media/ad/embed), classifies them by file name and manifest
body, drops ad ones (_is_ad_url, _is_ad_manifest), and picks the
best.

Fallback: if the active stream of a channel dies, the service
switches to the next working stream in that channel's list.

A/V muxing via ffmpeg when a source provides video and audio
separately (#EXT-X-MEDIA:TYPE=AUDIO). Auto (needs_mux) or manual
via mux_state (see below).

Manual mux control — mux_state: auto | on | off per stream. Useful
when a channel only works through mux (Pluto/AES-128) or vice versa.

External M3U playlists — search channels across public playlists
(GitHub and similar) from the streams modal. A found URL can be checked
with one click and inserted into the channel stream.

Channel events log — separate text log of changes (configuration

UP/DOWN transitions). See "Channel events log" section.

EPG prune + VACUUM — programmes older than 7 days are removed after
each successful source import; the DB is compacted (VACUUM) once a
day at IPTV_EPG_UPDATE_TIME. meta.updated_at survives restarts,
so the import schedule is not reset.

FlareSolverr watchdog — optional script that restarts a stuck
container when resolver.py drops a flag file.

Web UI at /manage: channel list, streams modal, EPG sources tab,
playlists tab, log tab.

Requirements
Docker and docker compose

Jellyfin (or any compatible client with M3U + XMLTV support)

FlareSolverr — only if you plan to use resolvers that go through it

Quick start
All required files live in the project root.

1. Create the working data/ directory and copy the examples:

bash
mkdir -p data
cp config.example.json   data/config.json
cp override.example.json data/override.json      # optional
cp .env.example          .env
cp docker-compose.example.yml docker-compose.yml
2. Put your Jellyfin API key into .env:

text
IPTV_JELLYFIN_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Create the key in Jellyfin: Dashboard → API Keys → Add.
Without the key, EPG refreshes on Jellyfin's own schedule — the automatic
guide-refresh trigger won't work.

3. Edit data/config.json — your channels and EPG sources.

4. Build and start:

bash
docker compose build iptv-proxy
docker compose up -d
5. Open the web UI:

text
http://<host>:9098/manage
6. Add to Jellyfin (Live TV):

What	URL
TV sources (M3U)	http://iptv-proxy:8000/m3u
Program data sources (XMLTV)	http://iptv-proxy:8000/xmltv.xml.gz
If Jellyfin doesn't reach IPTV-Proxy through the docker network, replace
the iptv-proxy hostname with an address reachable from Jellyfin's
container.

Environment variables
Variable	Description	Default
IPTV_JELLYFIN_API_KEY	Jellyfin API key. Needed for the automatic guide-refresh trigger.	—
IPTV_JELLYFIN_URL	Jellyfin URL inside the docker network.	http://jellyfin:8096
IPTV_MANAGE_URL	This service's own URL. Used in internal links.	http://iptv-proxy:8000
IPTV_CONFIG_FILE	Path to the main config inside the container.	/app/config.json
IPTV_LOG_LEVEL	debug / info / warning / error.	info
Other tunables (cache TTL, resolver limits, mux idle timeout, healthcheck
intervals, etc.) are described in config.example.json. You can edit them
there directly or override them via override.json, which is merged on
top of the main config at startup.

config.json structure
Section	Purpose
epg	EPG sources: URL, refresh interval, filter by id/name, update_time = VACUUM time
playlist_sources	External M3U playlists used for URL search
server	manage_url, override file name
jellyfin	Jellyfin URL, its xmltv cache path, API timeout
resolver	Default User-Agent, resolver order, timeouts
cache	Cache TTL per resolver type
mux	ffmpeg muxer parameters
healthcheck	Proactive channel-check scheduler
fallback	Switch to an alternative stream
limits	Semaphores for parallel resolutions
logging	Level, buffer size, rotation
analytics	URL statistics collector (disabled by default)
channels	Channel list
EPG: prune old programmes and VACUUM
Each programmes row carries start_time (unixtime) parsed from the XMLTV
start attribute. This allows removing old rows without parsing XML.

Logic:

Prune — after each successful import_source. Removes
programmes older than 7 days. Rows without start_time (pre-migration)
survive until the next re-import.

VACUUM — once a day at IPTV_EPG_UPDATE_TIME (default 02:35).
Compacts the DB (PRAGMA wal_checkpoint(TRUNCATE) + VACUUM).

Per-source import schedule — by interval of each source
(iptvx: 12h, us_guide_nbc: 24h). last_update is read from
meta.updated_at at startup, so restarts do not reset the timer.
Empty DB → last_update = 0 → import happens on the first cycle.

IPTV_EPG_UPDATE_TIME is no longer "nightly update of all sources" —
it is only the VACUUM time.

Channel format
json
{
    "name": "ChannelName",
    "chno": "1",
    "group": "News",
    "tvgid": "channel-id-in-epg",
    "real_name": "Channel Name as in EPG",
    "streams": [
        {
            "url": "https://site.example/watch",
            "resolver": "auto",
            "ua": "Mozilla/5.0 ...",
            "mux_state": "auto",
            "stream_id": 3918603096042605
        }
    ],
    "fallback": true
}
resolver is one of:

text
auto | direct | yt-dlp | streamlink
flaresolverr_simple | flaresolverr_session | sniffer
auto iterates through the resolvers in resolver.order until one
succeeds. A specific name forces only that resolver to be used.

The stream_id field is an internal stream identifier (52-bit integer).
Assigned automatically on add/save. Needed so streams_cache slots do
not lose their binding when streams are reordered. Do not touch manually.

Manual mux control (mux_state)
mux_state lives on a stream (streams[i].mux_state). Values:

Value	Behaviour
auto (default)	As before: mux turns on if the master playlist has #EXT-X-MEDIA:TYPE=AUDIO (split A/V). Computed once, cached in streams_cache[i].needs_mux.
on	Force mux. Useful when Jellyfin can't read HLS directly: Pluto (AES-128 + ffmpeg 7+ regression), SSAI streams, etc.
off	Force no-mux. Saves CPU. For dual-input channels (with #EXT-X-MEDIA:TYPE=AUDIO) this leads to video-only playback — Jellyfin does not mux on its own.
Priority: mux_state of the active stream > needs_mux from cache.

Set from the UI: channel → modal → 🎬 Streams → mux: select next to
Prefetch. Or directly in config.json.

Note on on: works for any stream where Jellyfin-ffmpeg successfully
reads raw TS from the muxer. When mux_state (auto ↔ on ↔ off),
active_stream_index, url, or resolver of the active stream changes,
iptv-proxy automatically removes Jellyfin's mediainfo cache for that
channel, so no Jellyfin restart is needed and the old-mode cache does not
get stuck (see "Jellyfin mediainfo cache" below).

Note on off: if the channel has #EXT-X-MEDIA:TYPE=AUDIO (split
tracks), Jellyfin cannot mux them itself — it will be silent or fail.
Use auto or on for those.

Channel events log
A separate text log with the change history: logs/channel_events.log.
Line-based, readable via tail -f:

text
2026-10-02 18:15:23 [NTV] cfg: stream#1 (ivi.ru/...) mux_state: auto -> on (ui)
2026-10-02 18:16:01 [SkyNews] state: UP -> DOWN (healthcheck)
2026-10-02 18:17:44 [Euronews] state: DOWN -> UP (ui)
Prefix cfg: — configuration changes (via UI or EPG auto-match).
Prefix state: — channel health transitions (UP/DOWN, automatic
active-stream switches). In parentheses at the end — source: ui,
healthcheck, fallback, startup, auto_match.

Rotation uses the same settings as the main logs
(IPTV_LOG_MAX_BYTES, IPTV_LOG_BACKUP_COUNT).

File path: IPTV_CHANNEL_EVENTS_LOG (env) or
config.json → logging.channel_events_log. Default —
/app/logs/channel_events.log.

Jellyfin mediainfo cache
On the first probe of a channel, Jellyfin stores the container info
(Container: hls or Container: ts) into cache/mediainfo/*.json.
On subsequent opens it does not re-probe — it reuses the cached format.

If the channel switches delivery mode (HLS ↔ raw TS via mux), the old
cache breaks playback (Jellyfin-ffmpeg exits with code 183
"Invalid data found when processing input").

Solution: iptv-proxy automatically removes Jellyfin's mediainfo
cache for a channel when any of the following changes:

mux_state of the active stream (auto ↔ on ↔ off),

active_stream_index,

url of the active stream,

resolver of the active stream.

On the next open, Jellyfin does a fresh probe and detects the format
correctly. No Jellyfin restart needed.

Requires a volume mount in docker-compose.yaml:

yaml
- /opt/docker-compose/configs/jellyfin/cache/mediainfo:/jellyfin-mediainfo-cache
The path is configurable via IPTV_JELLYFIN_MEDIAINFO_DIR (env) or
config.json → jellyfin.mediainfo_cache_dir.

External M3U playlists
The playlist_sources section lets you attach public M3U playlists
(GitHub, curated lists, etc.) and search them for URLs to use in channel
streams. This is not a channel import — it is a lookup tool: search,
check, insert.

json
"playlist_sources": [
    {
        "name": "smolnp_main",
        "url": "https://raw.githubusercontent.com/smolnp/IPTVru/refs/heads/gh-pages/IPTVru.m3u",
        "interval": 86400,
        "disable": true
    }
]
Fields:

Field	Purpose
name	Unique source name
url	M3U playlist URL
interval	Refresh interval in seconds (background, checked once a minute)
disable	true — do not refresh automatically (still refreshable from the UI)
Usage:

Playlists tab in the web UI — manage sources: add, edit, refresh
one, refresh all, clear stored data.

In the channel modal → Streams → 🔍 button next to the URL
field. A playlist search dialog opens.

Type a channel name (at least 2 characters). Results come from all
active sources.

Tap a result → 1-second HEAD check.

URL alive — inserted into the stream URL field.

Dead — HTTP code shown, the field is left untouched.

Notes:

No EPG matching. tvg-id from the playlist is ignored. The point
is to grab the URL. EPG is selected separately in the right column of
the channel modal.

Tap-check is HEAD only, 1 second. Not ffprobe. For deeper checks
use the regular "Check stream" / "Check ffprobe" buttons after the
URL is inserted.

No auto-imports. Channels are added manually via the normal
modals. Playlists just help find URLs.

#EXTVLCOPT is parsed. If a playlist entry has http-referrer
or http-user-agent, they land in the payload
(url|Referer=...|User-Agent=...).

Logos, groups, tvg-id are ignored. Only the URL is taken from the
playlist (optionally plus headers from #EXTVLCOPT).

disable: true in the example is intentional. Nothing is fetched
on startup. You enable the sources you want and hit "Refresh".

Endpoints
Path	Purpose
/m3u	M3U playlist for Jellyfin
/xmltv.xml.gz	EPG in XMLTV format (gzip)
/redirect/{name}.m3u8	Entry point for each channel
/hls/{name}.{ext}	Universal HLS proxy: ext = m3u8 / ts / key / vtt / m4s / mp4. Extension in the path is required; otherwise Jellyfin-ffmpeg 8+ refuses to open.
/hls/manifest.m3u8	Legacy route (HLS manifest)
/hls/segment.ts	Legacy route (segment)
/mux/{name}.ts	Muxed A/V stream (raw MPEG-TS)
/manage	Web UI
/logs	Log viewer
/status	Short service status
/events	SSE stream for the UI
/playlists/sources	GET/POST — list and save M3U playlist sources
/playlists/refresh/{name}	Refresh one source (background)
/playlists/refresh-all	Refresh all active sources
/playlists/clear/{name}	Clear stored source data in SQLite
/playlists/search?q=...	Search the playlist cache
/playlists/check	HEAD check used by the UI on tap
FlareSolverr watchdog
restart_flaresolverr.sh is an optional script for the case where
FlareSolverr occasionally gets stuck.

How it works: on a FlareSolverr error, resolver.py drops a flag file at
flags/flaresolverr_restart.request. The script (run on a timer — for
example a systemd unit once a minute) sees the flag, restarts the
container, and removes the flag. A cooldown prevents restart loops.

The script hardcodes the path to the config directory — adjust it to your
setup. A sample systemd unit is in the script header.

Feedback
Issues are read. Replies are not guaranteed.

License
Unlicense — public domain. Do whatever you want, no obligations.
Full text: LICENSE.
