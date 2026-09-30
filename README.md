
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
  результат кэшируется по TTL.
- **Fallback**: если активный стрим канала умер, сервис переключается на
  следующий рабочий из списка потоков этого канала.
- **Микширование A/V** через ffmpeg, если источник отдаёт видео и аудио
  раздельно (`#EXT-X-MEDIA:TYPE=AUDIO`).
- **Внешние M3U-плейлисты** — поиск каналов по публичным плейлистам
  (GitHub и т.п.) прямо из модалки потоков. Найденный URL можно проверить
  одной кнопкой и подставить в поток канала.
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
2. Впишите API-ключ Jellyfin в docker-compose.yaml

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
epg	Источники EPG: URL, интервал обновления, фильтр по id/именам
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
            "ua": "Mozilla/5.0 ..."
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
/hls/manifest.m3u8	Прокси HLS-манифеста (когда нужны Referer/Cookie)
/hls/segment.ts	Прокси HLS-сегмента
/mux/{name}.ts	Микшированный A/V-поток
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
cached with per-method TTL.

Fallback: if the active stream of a channel dies, the service
switches to the next working stream in that channel's list.

A/V muxing via ffmpeg when a source provides video and audio
separately (#EXT-X-MEDIA:TYPE=AUDIO).

External M3U playlists — search channels across public playlists
(GitHub and similar) from the streams modal. A found URL can be checked
with one click and inserted into the channel stream.

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
epg	EPG sources: URL, refresh interval, filter by id/name
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
            "ua": "Mozilla/5.0 ..."
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
/hls/manifest.m3u8	HLS manifest proxy (when Referer/Cookie are needed)
/hls/segment.ts	HLS segment proxy
/mux/{name}.ts	Muxed A/V stream
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

