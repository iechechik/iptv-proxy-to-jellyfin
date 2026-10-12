import subprocess
import os
import threading
import asyncio
import time
import re
import gzip
import urllib.request

from core.config import logger, IPTV_MUX_MAX_PROCESSES, IPTV_MUX_IDLE_TIMEOUT, IPTV_FFMPEG_LOG_LEVEL
# mux-debug-cleanup-v1

_mux_lock = threading.Lock()
_mux_processes = {}  # name -> MuxProcess

# Сколько 4xx от входа мукса считаем смертью ссылки (а не разовой заминкой),
# прежде чем пометить payload мёртвым и перезапустить разбор.
_MUX_INPUT_ERROR_LIMIT = 3


# mux-warm-v1: прогрев входов мукса перед запуском ffmpeg.
#
# У CDN-эджей первый запрос к дочернему плейлисту может вернуть пустой список
# (эдж ещё не сходил наверх за потоком). ffmpeg с двумя входами решает маппинг
# ОДИН раз при старте: если в этот момент в аудио-листе нет сегментов, звука не
# будет во всём сеансе, пока мукс не перезапустят (наблюдали на Р_Культуре:
# «первый запуск без звука, после переоткрытия канала звук есть»).
def _warm_input(name, url, label, ua, referer=None, cookie=None, attempts=3, wait=0.7) -> bool:
    """Тянет плейлист, пока в ответе не появятся сегменты (#EXTINF)."""
    if not url:
        return False
    headers = {"User-Agent": ua or "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                raw = resp.read(200000)
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = raw.decode("utf-8", errors="ignore")
            if "#EXTINF" in body:
                if i > 1:
                    logger.info(f"[MUX] '{name}': {label}-вход прогрет с попытки {i}")
                return True
            logger.info(f"[MUX] '{name}': {label}-вход пуст (попытка {i}/{attempts}), ждём")
        except Exception as e:
            logger.info(f"[MUX] '{name}': прогрев {label}-входа, попытка {i}/{attempts}: {e}")
        if i < attempts:
            time.sleep(wait)
    logger.warning(f"[MUX] '{name}': {label}-вход не прогрелся за {attempts} попыток — стартуем как есть")
    return False


def _is_fmp4_video(url, ua, headers_str=None, timeout=6.0) -> bool:
    """fMP4/AVCC-источник или обычный Annex-B TS — по СЕГМЕНТАМ плейлиста.

    Нужно, чтобы не ставить `-bsf:v dump_extra` на fMP4: фильтр ожидает Annex-B и
    падает на смене параметров на границе сегмента («Error applying bitstream
    filters to an output packet … Invalid data found»), убивая мукс. Замер: без
    фильтра параметры кадра в выходе всё равно повторяются (SPS/PPS по 3 раза на
    2,4 МБ) — их вставляет упаковщик MPEG-TS при конвертации AVCC→Annex-B.
    """
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua or "Mozilla/5.0"})
        if headers_str:
            for part in headers_str.split("\r\n"):
                if ":" in part:
                    k, v = part.split(":", 1)
                    req.add_header(k.strip(), v.strip())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read(200000).decode("utf-8", "ignore")
    except Exception:
        return False
    if "#EXT-X-MAP" in txt:
        return True
    exts = {l.strip().split("?")[0].rsplit(".", 1)[-1].lower()
            for l in txt.splitlines() if l.strip() and not l.strip().startswith("#")}
    return bool(exts & {"m4s", "mp4", "m4v", "m4a"})


def _audio_ready(name, url, ua, headers_str=None, attempts=3, timeout=4.0, wait=2.0) -> bool:
    """Есть ли в потоке аудиодорожка — до старта двухвходового мукса.

    Прогрев (_warm_input) доказывает только наличие сегментов в плейлисте, но не
    наличие аудиопотока. Если стартовать раньше, ffmpeg фиксирует `-map 1:a:0?`
    как «дорожки нет» и ВЕСЬ сеанс идёт без звука: у клиента картинка без звука,
    и только следующий запуск (когда вход уже прогрет) играет нормально.
    """
    if not url:
        return False
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a",
           "-show_entries", "stream=codec_type", "-of", "csv=p=0",
           "-user_agent", ua or "Mozilla/5.0"]
    if headers_str:
        cmd += ["-headers", headers_str]
    cmd.append(url)
    for i in range(1, attempts + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
            if "audio" in (r.stdout or ""):
                if i > 1:
                    logger.info(f"[MUX] '{name}': аудиодорожка появилась с попытки {i}")
                return True
            logger.info(f"[MUX] '{name}': аудиодорожки пока нет (попытка {i}/{attempts})")
        except Exception as e:
            logger.info(f"[MUX] '{name}': проверка аудио, попытка {i}/{attempts}: {e}")
        if i < attempts:
            time.sleep(wait)
    return False


# Если из stdout ffmpeg давно нет данных, а процесс жив —
# считаем его зависшим на мёртвом источнике и убиваем.
_MUX_STALL_TIMEOUT = 60
# Окно, в течение которого watchdog не убивает мукс по отсутствию данных —
# на случай медленного старта (двойной коннект к CDN + первый сегмент).
_MUX_START_GRACE = 120

# Рейтлимит для лога "subscriber queue FULL": в шторме в секунду
# прилетало по сотне строк, что само по себе создаёт нагрузку.
_QUEUE_FULL_LOG_INTERVAL = 1.0

# mux-gop-seed-v1: отдавать новому зрителю затравку (SPS/PPS + данные от IDR) — без неё клиент входит в середину GOP и не находит ключевой кадр. Отладка: IPTV_MUX_GOP_SEED=0.
_GOP_SEED_ENABLED = os.environ.get("IPTV_MUX_GOP_SEED", "1") not in ("0", "", "no", "false")


# mux-reuse-base-url-v1
def _url_base(url: str) -> str:
    """Базовая часть URL: host + путь до .m3u8/.ts, без query и без
    signed-хвостов. Сравниваем по ней, чтобы не убивать мукс при
    смене только sec2(...)/token=/expires=.

    Пример:
      https://live2...dmcdn.net/sec2(AbC123)/cloud/3/xyz/d/live-720.m3u8?startdate=...
      → https://live2...dmcdn.net/cloud/3/xyz/d/live-720.m3u8

    Параметры sec2(...) вырезаются как часть пути.
    """
    if not url:
        return ""
    import re as _re
    from urllib.parse import urlparse as _urlparse
    u = url.split("|", 1)[0]
    p = _urlparse(u)
    path = p.path
    # Вырезаем sec2(...) — signed-хвост пути.
    path = _re.sub(r"/sec2\([^)]*\)", "", path)
    # Также вырезаем /sign/..., /key/..., /iv/... — часть signed-обёрток Pluto.
    # Оставляем только смысловые компоненты.
    #
    # mux-base-strip-file-v1: отрезаем имя .m3u8-файла.
    # Pluto (и некоторые другие CDN) могут переключить варианты
    # master.m3u8 / master_5.m3u8 / master_caption.m3u8 при
    # перебалансировке. Если сравнивать полный path, base разойдётся,
    # мукс убьётся и создастся заново — лишний обрыв клиента.
    # Сравниваем только host + путь до файла.
    if path.endswith(".m3u8"):
        path = path.rsplit("/", 1)[0]
    return f"{p.scheme}://{p.netloc}{path}"


class MuxProcess:
    # single-input-mux-v1
    def __init__(self, name, video_url, audio_url, ua, referer=None, cookie=None):
        self.name = name
        # Параметры запуска. Нужны, чтобы get_or_create_mux мог сравнить
        # их с новым запросом: если URL/headers/cookies расходятся,
        # старый процесс читает протухший источник и его надо убить.
        self.video_url = video_url
        self.video_url_base = _url_base(video_url)
        self.audio_url = audio_url
        self.ua = ua
        self.referer = referer
        self.cookie = cookie
        # -nostdin: ffmpeg по умолчанию слушает stdin для интерактивных
        # команд (q, подтверждение перезаписи и т.п.). В сервисе stdin
        # наследуется от родителя и может не давать чистого EOF — это
        # известная причина зависаний ffmpeg, запущенного как демон/сервис.
        cmd = ["ffmpeg", "-nostdin", "-loglevel", IPTV_FFMPEG_LOG_LEVEL]

        headers_str = ""
        if referer:
            headers_str += f"Referer: {referer}\r\n"
        if cookie:
            headers_str += f"Cookie: {cookie}\r\n"

        # mux-timeouts-v1: длинный -analyzeduration/-probesize, чтобы первый запуск двухвходового мукса не шёл без звука.
        reconnect_opts = [
            "-thread_queue_size", "4096",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-timeout", "10000000",
            "-rw_timeout", "10000000",
        ]

        # single-input-mux-v1: если audio_url пусто — ffmpeg читает только
        # video (в Pluto media уже есть A+V).
        single_input = not audio_url

        # mux-avcc-no-bsf-v1: dump_extra ожидает Annex-B (TS). На fMP4/AVCC-источнике он
        # падает при смене параметров кадра на границе сегмента и роняет мукс («Error
        # applying bitstream filters to an output packet … Invalid data found», exit 183;
        # наблюдалось перемежающееся падение, при этом ручной прогон той же команды в
        # другой момент проходил). Параметры кадра не теряются: при упаковке в MPEG-TS
        # их вставляет сам ffmpeg, конвертируя AVCC→Annex-B (замер: SPS/PPS повторяются
        # у каждого ключевого кадра).
        _fmp4 = _is_fmp4_video(video_url, ua, headers_str)
        _bsf_args = [] if _fmp4 else ["-bsf:v", "dump_extra=freq=keyframe"]
        if _fmp4:
            logger.info(f"[MUX] '{name}': fMP4-источник — мукс без dump_extra")

        if headers_str:
            cmd += ["-headers", headers_str]
        # mux-timeouts-v1: -analyzeduration/-probesize — ffmpeg дольше
        # анализирует входы до старта. Без этого первый запуск
        # двухвходового мукса идёт без звука (audio-плейлист не прогрет).
        cmd += ["-analyzeduration", "10000000", "-probesize", "10000000"]
        # -copyts + -start_at_zero: сохранить исходные PTS и сдвинуть
        # первый в ноль.
        cmd += ["-copyts", "-start_at_zero"]
        cmd += reconnect_opts + ["-user_agent", ua, "-i", video_url]

        if single_input:
            # Один вход: -map только video+audio из него.
            cmd += [
                "-muxdelay", "0",
                "-map", "0:v:0", "-map", "0:a:0?",
                *_bsf_args,
                "-c:v", "copy", "-c:a", "copy",
                "-flush_packets", "1",
                "-f", "mpegts", "pipe:1",
            ]
        else:
            if headers_str:
                cmd += ["-headers", headers_str]
            # Два входа: video отдельно, audio отдельно, синхронизация PTS.
            # mux-optional-audio-v1: "1:a:0?" — если в аудио-входе аудио нет
            # (например, из sniffer'а пришёл не тот лист), ffmpeg не падает
            # целиком ("Invalid argument" → exit 234), а отдаёт видео.
            cmd += reconnect_opts + [
                    "-isync", "0",
                    "-user_agent", ua, "-i", audio_url,
                    "-muxdelay", "0",
                    "-map", "0:v:0", "-map", "1:a:0?",
                    *_bsf_args,
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                    "-flush_packets", "1",
                    "-f", "mpegts", "pipe:1"]
        # mux-warm-v1: прогреваем входы ДО запуска ffmpeg — иначе холодный
        # аудио-лист на старте даёт беззвучный сеанс (см. _warm_input).
        _warm_input(name, video_url, "video", ua, referer, cookie)
        if not single_input:
            _warm_input(name, audio_url, "audio", ua, referer, cookie)
            # mux-audio-ready-v1: дождаться аудиодорожки до старта, иначе первый зритель смотрит без звука (см. _audio_ready).
            if not _audio_ready(name, audio_url, ua, headers_str):
                logger.warning(
                    f"[MUX] '{name}': аудиодорожка не появилась за ~8 с — стартуем видео-only"
                )

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.subscribers = []
        self.subscribers_lock = threading.Lock()
        self.last_activity = time.time()
        # Обновляется в _read_loop на каждом чанке. По нему watchdog
        # отличает «ffmpeg работает, но данных нет» от «ffmpeg жив».
        self.grace_started_at = time.time()
        self.last_data_time = time.time()
        self._stopped = False
        self._stop_lock = threading.Lock()
        # Event loop, в котором создаются asyncio.Queue подписчиков.
        # Нужен для call_soon_threadsafe из _read_loop.
        self._loop = None
        self._last_stderr_line = None
        self._last_stderr_repeat_log = 0.0
        # mux-input-death-v1: сколько раз вход ответил 4xx и признан ли он мёртвым
        self._http_error_count = 0
        self._input_dead = False
        # mux-gop-cache-v1: кольцевой буфер последнего GOP — новый зритель стартует с ключевого кадра, а не из середины группы.
        self._gop_lock = threading.Lock()
        self._gop = bytearray()
        self._params = b""
        # Абсолютные смещения буфера в потоке: нужны, чтобы резать затравку по
        # границе TS-пакета (188 байт), а не по произвольному байту.
        self._abs_start = 0
        self._abs_end = 0
        self._gop_cap = 8 * 1024 * 1024
        # Выровнен ли буфер по ключевому кадру (иначе затравку не отдаём).
        self._gop_aligned = False
        # Больше этого затравку не отдаём: 4 МБ с запасом покрывают
        # прощупывание клиента (~1.5 МБ) и не заставляют плеер буферизовать
        # десятки секунд старого потока.
        self._seed_max = 4 * 1024 * 1024
        self._gop_served = 0
        # Рейтлимит queue-FULL warning.
        self._queue_full_last_log = 0.0
        self._queue_full_dropped = 0
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._stats_loop, daemon=True).start()
        logger.info(f"[MUX] '{name}': ffmpeg started (pid={self.proc.pid}, loglevel={IPTV_FFMPEG_LOG_LEVEL})")

    def _stats_loop(self):
        # Диагностика: раз в 10 секунд показывает глубину очереди каждого
        # подписчика и давность последнего чанка от ffmpeg. Логируем
        # только когда есть активные подписчики, чтобы не шуметь.
        #
        # asyncio.Queue не имеет qsize() (в отличие от queue.Queue).
        # Берём длину через приватный _queue (collections.deque) —
        # в CPython 3.11 структура стабильна, len() к deque атомарен.
        # Всё завёрнуто в try/except: диагностический поток НЕ должен
        # падать ни от чего — иначе пропадает весь смысл диагностики.
        while not self._stopped:
            time.sleep(10)
            try:
                with self.subscribers_lock:
                    if not self.subscribers:
                        continue
                    subs_snapshot = list(self.subscribers)
                sizes = []
                for q in subs_snapshot:
                    try:
                        sizes.append(len(q._queue))
                    except Exception:
                        sizes.append(-1)
                age = time.time() - self.last_data_time
                logger.info(
                    f"[MUX] '{self.name}': subs={len(sizes)} queues={sizes} last_data_age={age:.2f}s"
                )
            except Exception as e:
                logger.warning(f"[MUX] '{self.name}': _stats_loop error: {e}")

    def _read_stderr(self):
        # Уровень вывода ffmpeg задаётся флагом -loglevel IPTV_FFMPEG_LOG_LEVEL.
        # Дополнительно распределяем строки по уровням Python-логгера по
        # ключевым словам — чтобы error/warning ffmpeg были видны даже
        # если приложение поднято до DEBUG.
        #
        # Чтобы видеть подробный вывод (Opening / Stream mapping / frame=),
        # достаточно поднять ffmpeg_log_level в override.json до info/debug.
        #
        # Дедуп одинаковых строк подряд сохранён: «Non-monotonic DTS»
        # сыплется десятками в секунду, без дедупа логирование само
        # становится узким местом.
        try:
            for raw_line in self.proc.stderr:
                try:
                    line = raw_line.decode('utf-8', errors='ignore').strip()
                    if not line:
                        continue
                    now = time.time()
                    if line == self._last_stderr_line and (now - self._last_stderr_repeat_log) < 1.0:
                        continue
                    self._last_stderr_line = line
                    self._last_stderr_repeat_log = now
                    lowered = line.lower()
                    # mux-input-death-v1: вход умер (CDN отвечает 4xx на плейлист).
                    # ffmpeg будет ходить по кругу, отдавая клиенту пустоту —
                    # помечаем payload мёртвым и гасим процесс, чтобы следующая
                    # попытка переразобрала master, а не ждала healthcheck.
                    if re.search(r"http error 4\d\d", lowered):
                        self._http_error_count += 1
                        if self._http_error_count >= _MUX_INPUT_ERROR_LIMIT and not self._input_dead:
                            self._input_dead = True
                            logger.warning(
                                f"[MUX] '{self.name}': вход отвечает ошибкой "
                                f"{self._http_error_count}-й раз подряд — payload помечаю мёртвым"
                            )
                            threading.Thread(target=self._on_input_dead, daemon=True).start()
                    if "error" in lowered or "fatal" in lowered or "invalid" in lowered:
                        logger.error(f"[MUX] '{self.name}': ffmpeg: {line}")
                    elif "thread message queue blocking" in lowered or "warning" in lowered:
                        logger.warning(f"[MUX] '{self.name}': ffmpeg: {line}")
                    else:
                        logger.debug(f"[MUX] '{self.name}': ffmpeg: {line}")
                except Exception:
                    # Что бы ни случилось при обработке одной строки —
                    # цикл чтения обязан продолжаться, иначе пайп
                    # переполнится и ffmpeg зависнет.
                    continue
        except Exception as e:
            logger.error(f"[MUX] '{self.name}': _read_stderr crashed: {e}")
        return_code = self.proc.wait()
        logger.error(f"[MUX] '{self.name}': ffmpeg exited with code {return_code}")

    # Маркеры ключевого кадра H.264 (IDR) в Annex-B: 3- и 4-байтный старт-код.
    _KF_MARKERS = (b"\x00\x00\x00\x01\x65", b"\x00\x00\x01\x65")
    # mux-params-v1: резать буфер по IDR нельзя — SPS/PPS стоят ПЕРЕД ключевым
    # кадром, и подписчик получил бы кадр без параметров («non-existing PPS 0»).
    _SPS_MARKERS = (b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67")
    # Насколько далеко назад искать SPS от найденного ключевого кадра.
    _params_scan_back = 65536

    def _ingest_gop(self, chunk: bytes) -> None:
        """Держим текущий GOP строго от ключевого кадра — для новых зрителей.

        Затравка для нового подписчика — это данные ОТ КЛЮЧЕВОГО КАДРА вместе с
        параметрами кадра (SPS/PPS), которые идут перед ним. Два условия
        обязательны, оба проверены замерами на живом потоке:

        * резать ровно по IDR нельзя — параметры кадра стоят ПЕРЕД ним, клиент
          декодировал весь поток с «non-existing PPS 0» (230 ошибок против 1);
        * резать по байту нельзя вообще — начало попадало в СЕРЕДИНУ
          TS-пакета, и параметры кадра из этого пакета терялись при
          демультиплексировании (клиент снова видел поток без SPS/PPS).
          Поэтому границу выравниваем по 188 байтам, забирая целый пакет с SPS.
        """
        with self._gop_lock:
            self._gop += chunk
            self._abs_end += len(chunk)
            kf = -1
            for marker in self._KF_MARKERS:
                pos = self._gop.rfind(marker)
                if pos > kf:
                    kf = pos
            if kf > 0:
                sps = -1
                for marker in self._SPS_MARKERS:
                    pos = self._gop.rfind(marker, max(0, kf - self._params_scan_back), kf)
                    if pos > sps:
                        sps = pos
                if sps < 0:
                    # параметров кадра перед ключевым кадром нет — выравнивать
                    # нечего, затравку такому зрителю не отдаём (см. snapshot)
                    self._gop_aligned = False
                    return
                anchor_abs = self._abs_start + sps
                aligned_abs = anchor_abs - (anchor_abs % 188)
                cut = aligned_abs - self._abs_start
                if cut > 0:
                    del self._gop[:cut]
                    self._abs_start += cut
                self._gop_aligned = True
            elif len(self._gop) > self._gop_cap:
                # Ключевого кадра давно нет (некоторые вещатели отдают их редко).
                # Тогда буфер теряет выравнивание: режем его и помечаем, чтобы НЕ
                # отдавать затравку из середины GOP — иначе клиент получит поток
                # без параметров кадра и «вечный логотип». Такому зрителю отдаём
                # живой поток, а мукс ждёт следующего ключевого кадра источника.
                drop = len(self._gop) - self._gop_cap // 2
                del self._gop[:drop]
                self._abs_start += drop
                self._gop_aligned = False

    def gop_seed_ready(self) -> bool:
        """Готова ли затравка (выровнена по TS-пакету/ключевому кадру, в лимите).

        Дешёвая проверка без копирования буфера: по ней роутер ждёт, прежде
        чем отдать клиенту поток, иначе первый зритель входит в середину GOP.
        """
        with self._gop_lock:
            return bool(self._gop) and self._gop_aligned and \
                len(self._gop) <= self._seed_max

    def _gop_snapshot(self) -> bytes:
        """Затравка для нового подписчика: поток от ключевого кадра с SPS/PPS.

        Отдаём только выровненную по TS-пакету затравку: иначе начало попадает
        в середину пакета или в середину GOP, и клиент декодирует битый поток.
        """
        with self._gop_lock:
            if not self._gop or not self._gop_aligned:
                return b""
            if len(self._gop) > self._seed_max:
                return b""
            return bytes(self._gop)

    def _read_loop(self):
        # Читаем stdout ffmpeg в отдельном потоке. Очереди подписчиков —
        # asyncio.Queue, созданы в event loop, поэтому писать в них
        # из этого потока можно только через loop.call_soon_threadsafe.
        # Прямой put_nowait небезопасен: внутренний deque и wakeup
        # waiter'ов не защищены от гонки.
        def _enqueue_chunk(q, chunk):
            """Вызывается в event loop через call_soon_threadsafe."""
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                # Медленный клиент. Дропаем чанк для него,
                # но НЕ отключаем: stream_generator в routers/stream.py
                # вечно ждал бы данные из очереди, в которую больше
                # никто не пишет.
                self._queue_full_dropped += 1
                now = time.time()
                if (now - self._queue_full_last_log) >= _QUEUE_FULL_LOG_INTERVAL:
                    logger.warning(
                        f"[MUX] '{self.name}': subscriber queue FULL, "
                        f"dropped {self._queue_full_dropped} chunks "
                        f"({self._queue_full_dropped * 64 // 1024} MiB)"
                    )
                    self._queue_full_last_log = now
                    self._queue_full_dropped = 0

        def _enqueue_stop(q):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

        try:
            while True:
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    logger.info(f"[MUX] '{self.name}': ffmpeg stdout closed (EOF)")
                    break
                self.last_data_time = time.time()
                # mux-gop-atomic-v1: снимок GOP и регистрация подписчика под одним замком — иначе в потоке дубли/пропуски («corrupted macroblock»).
                with self.subscribers_lock:
                    self._ingest_gop(chunk)
                    subs_snapshot = list(self.subscribers)
                    loop = self._loop
                for q in subs_snapshot:
                    if loop is not None and loop.is_running():
                        try:
                            loop.call_soon_threadsafe(_enqueue_chunk, q, chunk)
                        except RuntimeError:
                            # loop уже закрыт — процесс останавливается.
                            # Молча пропускаем, finally всё уберёт.
                            pass
                    else:
                        # Fallback: loop не сохранён (subscribe вызван
                        # вне async) — прямой put_nowait. CPython GIL
                        # защищает от порчи памяти, но могут быть гонки
                        # с waiter'ами. Ожидаемо не срабатывает.
                        _enqueue_chunk(q, chunk)
        finally:
            with self.subscribers_lock:
                subs_snapshot = list(self.subscribers)
                self.subscribers.clear()
            loop = self._loop
            for q in subs_snapshot:
                if loop is not None and loop.is_running():
                    try:
                        loop.call_soon_threadsafe(_enqueue_stop, q)
                    except RuntimeError:
                        pass
                else:
                    _enqueue_stop(q)
            self.stop()

    def subscribe(self):
        # asyncio.Queue вместо queue.Queue: потребитель в event loop
        # читает напрямую через await q.get(), без thread-hop.
        # Это устраняет bottleneck в routers/stream.py:mux_stream.
        #
        # Запоминаем loop, в котором создали очередь. _read_loop крутится
        # в отдельном потоке, и кладёт в очередь через
        # loop.call_soon_threadsafe — это единственный корректный способ
        # писать в asyncio.Queue из чужого потока.
        q = asyncio.Queue(maxsize=2000)
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError as _e:
            self._loop = None
            logger.warning(f"[MUX] '{self.name}': subscribe without running loop: {_e}")
        # mux-gop-atomic-v1: снимок GOP и регистрация очереди — под ОДНИМ
        # замком с читающим потоком. Тогда «затравка» гарантированно идёт
        # перед всеми чанками, которые подписчик получит дальше: в потоке
        # не появляется ни пропусков, ни дублей (иначе плеер декодирует
        # битый поток — «corrupted macroblock»).
        with self.subscribers_lock:
            seed = self._gop_snapshot() if _GOP_SEED_ENABLED else b""
            sent = 0
            if seed:
                for i in range(0, len(seed), 65536):
                    try:
                        q.put_nowait(seed[i:i + 65536])
                        sent += 1
                    except asyncio.QueueFull:
                        break
            self.subscribers.append(q)
        if sent:
            self._gop_served += 1
            logger.info(f"[MUX] '{self.name}': новому зрителю отдан GOP "
                        f"{len(seed) // 1024} КиБ ({sent} чанков)")
        self.last_activity = time.time()
        return q

    def unsubscribe(self, q):
        with self.subscribers_lock:
            if q in self.subscribers:
                self.subscribers.remove(q)
        self.last_activity = time.time()

    def has_subscribers(self):
        with self.subscribers_lock:
            return len(self.subscribers) > 0

    def stop(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        logger.info(f"[MUX] '{self.name}': process stopped")

    def _on_input_dead(self):
        """Вход мукса мёртв: помечаем payload мёртвым и убираем процесс.

        Дальше канал переразбирается по обычному пути (healthcheck/следующий
        запрос), а не ждёт плановой проверки с мёртвой ссылкой в кэше.
        """
        try:
            from services.proxy_service import mark_payload_dead
            mark_payload_dead(self.name)
        except Exception as e:
            logger.warning(f"[MUX] '{self.name}': mark_payload_dead failed: {e}")
        try:
            invalidate_mux(self.name)
        except Exception as e:
            logger.warning(f"[MUX] '{self.name}': invalidate_mux failed: {e}")


def is_mux_alive_and_fresh(name, max_stall=15, require_subscribers=True):
    """True, если для канала есть живой мукс-процесс, недавно отдававший данные.

    require_subscribers=True (по умолчанию) — считаем «живым для healthcheck»
    только мукс, у которого есть хотя бы один подписчик. Иначе процесс,
    который никто не смотрит (Jellyfin остановил воспроизведение, но мукс
    ещё не убит по idle_timeout), блокирует проверку канала: в логе
    появляется «Канал X играет через мукс, пропуск проверки», хотя никто
    не играет. Для UI-бейджа передаём False — показать состояние
    мукс-процесса независимо от наличия клиентов."""
    with _mux_lock:
        mp = _mux_processes.get(name)
        if not mp or mp.proc.poll() is not None:
            return False
        if require_subscribers and not mp.has_subscribers():
            return False
        return (time.time() - mp.last_data_time) < max_stall


def get_live_mux(name):
    """Живой mux-процесс канала или None.

    Нужен, чтобы не ходить за master'ом к CDN, когда ffmpeg уже работает: он
    читает свои ранее разобранные video/audio URL, и master ему не нужен. Без
    этой проверки /mux на КАЖДЫЙ запрос заново загружал master — а если
    подписанная ссылка успела протухнуть, ответ был 404 и клиент уходил в
    шторм ретраев (наблюдали на Euronews)."""
    if not name:
        return None
    with _mux_lock:
        mp = _mux_processes.get(name)
        if mp and mp.proc.poll() is None:
            return mp
    return None


def get_or_create_mux(name, video_url, audio_url, ua, referer=None, cookie=None):
    with _mux_lock:
        existing = _mux_processes.get(name)
        alive = existing is not None and existing.proc.poll() is None
        logger.info(
            f"[MUX] '{name}': called, "
            f"existing={'yes' if existing else 'no'} alive={alive}, "
            f"v={video_url[:60]} a={(audio_url or '')[:60]}"
        )

        if existing and alive:
            # mux-reuse-base-url-v1: сравниваем базовый URL (без signed
            # sec2/token/query), а не полный. Иначе sniffer каждые 30 сек
            # возвращает новый signed URL → kill ffmpeg → обрыв стрима.
            #
            # headers/ua/cookie сравниваем по-прежнему полностью — смена
            # Cookie означает смену сессии, тут нужен новый ffmpeg.
            existing_base = getattr(existing, "video_url_base", "") or _url_base(existing.video_url)
            new_base = _url_base(video_url)
            same = (
                existing_base == new_base
                and (existing.audio_url or "") == (audio_url or "")
                and (existing.ua or "") == (ua or "")
                and (existing.referer or "") == (referer or "")
                and (existing.cookie or "") == (cookie or "")
            )
            if same:
                if existing.video_url != video_url:
                    logger.info(
                        f"[MUX] '{name}': reuse (base same, signed URL updated "
                        f"{existing.video_url[:40]}... -> {video_url[:40]}...)"
                    )
                else:
                    logger.info(f"[MUX] '{name}': reuse (params same)")
                return existing
            # Параметры разошлись — старый ffmpeg читает протухшие URL.
            # Именно это даёт «только видео» или «только звук»: один вход
            # 403-ит, второй кое-как проскакивает. Убиваем, стартуем свежий.
            logger.info(
                f"[MUX] '{name}': params changed, recreating "
                f"(old_v={existing.video_url[:200]} new_v={video_url[:200]})"
            )
            existing.stop()
            _mux_processes.pop(name, None)
        elif existing and not alive:
            logger.info(f"[MUX] '{name}': existing dead, recreating")
            _mux_processes.pop(name, None)

        if len(_mux_processes) >= IPTV_MUX_MAX_PROCESSES:
            idle = [p for p in _mux_processes.values() if not p.has_subscribers()]
            if idle:
                victim = min(idle, key=lambda p: p.last_activity)
                victim.stop()
                _mux_processes.pop(victim.name, None)
                logger.warning(f"[MUX] '{victim.name}': killed (process limit)")

        mp = MuxProcess(name, video_url, audio_url, ua, referer, cookie)
        _mux_processes[name] = mp
        logger.info(f"[MUX] '{name}': created new MuxProcess")
        return mp


def invalidate_mux(name):
    """Убивает мукс-процесс для канала. Вызывать при смене активного стрима —
    старый ffmpeg читает мёртвый источник и не должен отдаваться клиентам."""
    with _mux_lock:
        mp = _mux_processes.pop(name, None)
        if mp:
            mp.stop()


def mux_watchdog_loop():
    while True:
        time.sleep(10)
        now = time.time()
        with _mux_lock:
            # 1) Idle — нет подписчиков и давно нет обращений.
            idle = [n for n, mp in _mux_processes.items()
                    if not mp.has_subscribers() and (now - mp.last_activity) > IPTV_MUX_IDLE_TIMEOUT]
            # 2) Stalled — процесс жив, но из stdout давно нет данных.
            #    Так выглядит «ffmpeg висит на мёртвом источнике, не завершаясь».
            stalled = []
            for n, mp in _mux_processes.items():
                if mp.proc.poll() is not None:
                    continue
                # Процесс в grace-окне старта — не судим по last_data_time.
                # По выходу из grace правило возвращается к обычному.
                if (now - mp.grace_started_at) < _MUX_START_GRACE:
                    continue
                if (now - mp.last_data_time) > _MUX_STALL_TIMEOUT:
                    stalled.append(n)
            for n in set(idle) | set(stalled):
                reason = "idle" if n in idle else "no data"
                _mux_processes[n].stop()
                del _mux_processes[n]
                logger.info(f"[MUX] '{n}': stopped ({reason})")


def start_mux_watchdog():
    threading.Thread(target=mux_watchdog_loop, daemon=True).start()
