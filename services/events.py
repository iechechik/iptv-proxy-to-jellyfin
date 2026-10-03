import json
import asyncio
from core.config import logger
import core.state as state

async def broadcast_update(channel_name: str = None, status_data: dict = None,
                           event_type: str = "status-update", extra_data: dict = None):
    event = {
        "type": event_type,
        "channel": channel_name,
        "status": status_data,
        "extra": extra_data
    }
    data = f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
    # list(...) копирует список под локом — итерируемся по снимку,
    # чтобы параллельное удаление в sse_endpoint не поймало нас
    # на «list changed during iteration».
    #
    # put_nowait вместо await queue.put: очередь без maxsize, put не
    # блокируется. Но await в цикле — это точка yield, а yield внутри
    # threading.Lock (state._sse_lock) блокирует event loop и любой
    # параллельный вход в sse_endpoint, который тоже берёт этот lock.
    # put_nowait убирает yield и делает блокировку честной.
    #
    # remove() завернут в try/except: если очередь уже удалена
    # параллельным finally, list.remove бросает ValueError. Раньше
    # это убивало всю корутину — остальные клиенты не получали
    # broadcast. Теперь просто пропускаем.
    with state._sse_lock:
        for queue in list(state._sse_clients):
            try:
                queue.put_nowait(data)
            except Exception:
                try:
                    state._sse_clients.remove(queue)
                except ValueError:
                    pass

def send_broadcast_async(name=None, status_data=None, event_type="status-update", extra_data=None):
    if state._loop is None:
        logger.warning("[SSE] event loop not initialized")
        return
    # B5: при рестарте uvicorn loop закрывается раньше, чем успевают
    # догореть фоновые потоки (healthcheck, EPG updater, playlist updater).
    # run_coroutine_threadsafe в этот момент бросает RuntimeError. Раньше
    # это валило вызывающий поток (например, worker healthcheck) с трейсбеком
    # в логе. Теперь — тихо пропускаем, событие просто не уйдёт в SSE.
    try:
        asyncio.run_coroutine_threadsafe(broadcast_update(name, status_data, event_type, extra_data), state._loop)
    except RuntimeError as e:
        logger.debug(f"[SSE] broadcast dropped (loop closed?): {e}")
    except Exception as e:
        logger.warning(f"[SSE] broadcast failed: {e}")

def save_cache_and_broadcast(name: str, status_data: dict):
    state.save_cache()
    send_broadcast_async(name, status_data)

def send_epg_progress(percent: int, message: str = ""):
    send_broadcast_async(event_type="epg-progress",
                         extra_data={"percent": percent, "message": message})

def send_epg_complete(success: bool = True, message: str = ""):
    send_broadcast_async(event_type="epg-complete",
                         extra_data={"success": success, "message": message})

