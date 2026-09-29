import subprocess
import threading
import asyncio
import time
import queue

from core.config import logger, IPTV_MUX_MAX_PROCESSES, IPTV_MUX_IDLE_TIMEOUT, IPTV_FFMPEG_LOG_LEVEL

_mux_lock = threading.Lock()
_mux_processes = {}  # name -> MuxProcess

# Если из stdout ffmpeg давно нет данных, а процесс жив —
# считаем его зависшим на мёртвом источнике и убиваем.
_MUX_STALL_TIMEOUT = 60
# Окно, в течение которого watchdog не убивает мукс по отсутствию данных —
# на случай медленного старта (двойной коннект к CDN + первый сегмент).
_MUX_START_GRACE = 120

# Рейтлимит для лога "subscriber queue FULL": в шторме в секунду
# прилетало по сотне строк, что само по себе создаёт нагрузку.
_QUEUE_FULL_LOG_INTERVAL = 1.0


class MuxProcess:
    def __init__(self, name, video_url, audio_url, ua, referer=None, cookie=None):
        self.name = name
        # Параметры запуска. Нужны, чтобы get_or_create_mux мог сравнить
        # их с новым запросом: если URL/headers/cookies расходятся,
        # старый процесс читает протухший источник и его надо убить.
        self.video_url = video_url
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

        # -thread_queue_size: дефолт (обычно 8) слишком мал для двух
        # независимых живых сетевых входов, синхронизируемых на муксе —
        # при малейшей заминке любого из них ловим "Thread message queue
        # blocking" и визуально это выглядит как зависание/рассыпание.
        # -reconnect*: не даёт ffmpeg встать колом при кратковременном
        # обрыве HTTP-соединения на любом из входов.
        # -timeout: верхняя граница ожидания на коннект/TLS-рукопожатие/
        # чтение (микросекунды). Без него зависшее на рукопожатии
        # соединение не считается оборванным вообще — reconnect выше
        # просто не наступает, ffmpeg ждёт бесконечно.
        reconnect_opts = [
            "-thread_queue_size", "4096",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-timeout", "10000000",
        ]

        if headers_str:
            cmd += ["-headers", headers_str]
        # -copyts + -start_at_zero: сохранить исходные PTS и сдвинуть
        # первый в ноль. Без них -isync не работает.
        cmd += ["-copyts", "-start_at_zero"]
        cmd += reconnect_opts + ["-user_agent", ua, "-i", video_url]

        if headers_str:
            cmd += ["-headers", headers_str]
        # -isync 0: input-опция, применить ко второму входу. Синхронизирует
        # аудио относительно первого входа (video) по разнице стартовых PTS.
        # Требует -copyts, чтобы PTS не перенормировались в ноль.
     ###cmd += ["-isync", "0"]
        cmd += reconnect_opts + [
                # -isync 0: выровнять timestamps второго входа (audio)
                # по первому (video). Требует -copyts -start_at_zero выше.
                "-isync", "0",
                "-user_agent", ua, "-i", audio_url,
                "-muxdelay", "0",
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-flush_packets", "1",
                "-f", "mpegts", "pipe:1"]
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
                    break
                self.last_data_time = time.time()
                with self.subscribers_lock:
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
        except RuntimeError:
            # subscribe вызван из sync-контекста — крайне маловероятно
            # (mux_stream в routers/stream.py — async def). Оставляем
            # self._loop как None, put будет через прямой put_nowait.
            self._loop = None
        with self.subscribers_lock:
            self.subscribers.append(q)
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
            same = (
                existing.video_url == video_url
                and (existing.audio_url or "") == (audio_url or "")
                and (existing.ua or "") == (ua or "")
                and (existing.referer or "") == (referer or "")
                and (existing.cookie or "") == (cookie or "")
            )
            if same:
                logger.info(f"[MUX] '{name}': reuse (params same)")
                return existing
            # Параметры разошлись — старый ffmpeg читает протухшие URL.
            # Именно это даёт «только видео» или «только звук»: один вход
            # 403-ит, второй кое-как проскакивает. Убиваем, стартуем свежий.
            logger.info(
                f"[MUX] '{name}': params changed, recreating "
                f"(old_v={existing.video_url[:60]} new_v={video_url[:60]})"
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
