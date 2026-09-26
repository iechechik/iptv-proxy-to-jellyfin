import subprocess
import threading
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
            "-thread_queue_size", "1024",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-timeout", "10000000",
        ]

        if headers_str:
            cmd += ["-headers", headers_str]
        cmd += reconnect_opts + ["-user_agent", ua, "-i", video_url]

        if headers_str:
            cmd += ["-headers", headers_str]
        # -isync 0: input-опция, применить ко второму входу. Синхронизирует
        # аудио относительно первого входа (video) по разнице стартовых PTS.
        # Требует -copyts, чтобы PTS не перенормировались в ноль.
     ###cmd += ["-isync", "0"]
        cmd += reconnect_opts + ["-user_agent", ua, "-i", audio_url,
                # -copyts + -start_at_zero: сохранить исходные PTS
                # (не пересчитывать от нуля) и одновременно сдвинуть вывод
                # так, чтобы поток начинался с нуля. Без пары друг без
                # друга не работают; нужны для -isync.
            ####"-copyts", "-start_at_zero",
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
        while not self._stopped:
            time.sleep(10)
            with self.subscribers_lock:
                if not self.subscribers:
                    continue
                sizes = [q.qsize() for q in self.subscribers]
            age = time.time() - self.last_data_time
            logger.info(
                f"[MUX] '{self.name}': subs={len(sizes)} queues={sizes} last_data_age={age:.2f}s"
            )

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
        try:
            while True:
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                self.last_data_time = time.time()
                with self.subscribers_lock:
                    for q in self.subscribers:
                        try:
                            q.put_nowait(chunk)
                        except queue.Full:
                            # Медленный клиент. Дропаем чанк для него,
                            # но НЕ отключаем: stream_generator в
                            # routers/stream.py вечно ждал бы данные из
                            # очереди, в которую больше никто не пишет.
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
        finally:
            with self.subscribers_lock:
                for q in self.subscribers:
                    try:
                        q.put_nowait(None)
                    except queue.Full:
                        pass
                self.subscribers.clear()
            self.stop()

    def subscribe(self):
        # 200 чанков × 64 КБ ≈ 12.8 МБ ≈ 12 сек буфера при 8 Mbps.
        q = queue.Queue(maxsize=500)
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
