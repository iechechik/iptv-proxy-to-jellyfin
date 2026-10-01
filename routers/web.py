from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import html
from urllib.parse import quote
from datetime import datetime

import core.state as state
from services.epg_service import epg_manager
from services.mux_service import is_mux_alive_and_fresh

router = APIRouter()
templates = Jinja2Templates(directory="web")


@router.get("/", response_class=HTMLResponse)
@router.get("/manage", response_class=HTMLResponse)
def manage_page(request: Request):
    channels = state.load_channels()
    seen_chnos = set()
    rows = []
    source_name_map = epg_manager.get_channel_source_name_map()

    with state.cache_lock:
        epg_cache_snapshot = {name: dict(entry) for name, entry in state._epg_cache.items()}

    for ch in channels:
        channel_name = ch["name"]
        entry = epg_cache_snapshot.get(channel_name, {})
        real_name = ch.get("real_name", channel_name)
        epg_id = ch.get("tvgid") or ""
        source_name = source_name_map.get(epg_id, "")
        epg_display = f"{epg_id} ({source_name})" if source_name else epg_id

        current_chno = ch.get("chno") or ""

        is_duplicate = False
        if current_chno:
            if current_chno in seen_chnos:
                is_duplicate = True
            else:
                seen_chnos.add(current_chno)

        enabled = not ch.get("disable", False)
        if is_duplicate:
            enabled = False

        checked_attr = "checked" if enabled else ""
        chno_border = "border-color: var(--danger);" if is_duplicate else ""
        chno_color = "color: var(--danger); font-weight: bold;" if is_duplicate else ""
        dup_badge = ' <span style="color: var(--danger); font-size: 11px;">[Дубликат]</span>' if is_duplicate else ""

        escaped_name = html.escape(channel_name, quote=True)

        # T1: состояние канала = АКТИВНЫЙ слот streams_cache.
        # Верхнеуровневых last_check_* в _epg_cache[name] больше нет.
        active_idx = ch.get("active_stream_index", 0)
        if not isinstance(active_idx, int) or active_idx < 0:
            active_idx = 0
        streams_cache = entry.get("streams_cache", [])
        if not isinstance(streams_cache, list):
            streams_cache = []
        active_slot = {}
        if 0 <= active_idx < len(streams_cache) and isinstance(streams_cache[active_idx], dict):
            active_slot = streams_cache[active_idx]

        # MUX-индикатор. Два состояния:
        #   mux-on  — прямо сейчас работает мукс-процесс (канал играет через мукс);
        #   mux-req — в активном слоте needs_mux=True (по master'у требуется мукс,
        #             но процесс ещё не создан — например, никто не смотрит).
        # Дополнительный источник — is_mux_alive_and_fresh. Покрывает случай,
        # когда слот ещё не заполнен, а ffmpeg уже запущен.
        mux_alive = False
        try:
            mux_alive = is_mux_alive_and_fresh(
                channel_name, max_stall=120, require_subscribers=False
            )
        except Exception:
            pass
        # mux-state-ui-v1: учитываем mux_state активного stream.
        mux_needed = bool(active_slot.get("needs_mux", False)) if isinstance(active_slot, dict) else False
        channel_mux_state = ch.get("mux_state", "auto")
        _active_streams = ch.get("streams", [])
        if 0 <= active_idx < len(_active_streams) and isinstance(_active_streams[active_idx], dict):
            channel_mux_state = _active_streams[active_idx].get("mux_state", "auto")

        # mux-off-kills-v1: mux_state=off приоритетнее mux_alive.
        if channel_mux_state == "off":
            mux_badge = ' <span class="mux-badge mux-off" title="mux_state=off (мукс отключён)">no-mux</span>'
        elif mux_alive:
            _title = "Мукс работает сейчас"
            if channel_mux_state == "on":
                _title = "Мукс работает сейчас (mux_state=on)"
            mux_badge = f' <span class="mux-badge mux-on" title="{_title}">MUX</span>'
        elif channel_mux_state == "on":
            mux_badge = ' <span class="mux-badge mux-req" title="mux_state=on (мукс принудительно, ещё не запущен)">MUX</span>'
        elif mux_needed:
            mux_badge = ' <span class="mux-badge mux-req" title="Требуется мукс (по master-плейлисту)">MUX</span>'
        elif channel_mux_state == "off":
            mux_badge = ' <span class="mux-badge mux-off" title="mux_state=off (мукс отключён)">no-mux</span>'
        else:
            mux_badge = ''


        last_time = active_slot.get("last_check_time")
        last_success = active_slot.get("last_check_success")
        check_status_html = ""
        if last_time:
            dt = datetime.fromtimestamp(last_time).strftime("%d.%m %H:%M")
            icon = "✅" if last_success else "❌"
            detail = active_slot.get("last_check_detail", "")
            check_status_html = f'<span title="{html.escape(detail)}">{icon} {dt}</span>'
        else:
            check_status_html = '<span style="color: #555;">—</span>'

        resolver = ch.get("resolver", "auto")
        fallback_class = 'active' if ch.get('fallback', False) else ''
        rows.append(f"""
        <tr id="row-{quote(channel_name)}" data-original-name="{escaped_name}">
            <td><input type="checkbox" class="channel-enabled" {checked_attr} data-name="{escaped_name}" onchange="toggleChannel(this, this.dataset.name)"></td>
            <td>{check_status_html}</td>
            <td><input type="text" class="channel-chno" value="{html.escape(str(current_chno))}" style="width: 60px; background: var(--bg-color); color: var(--text-main); border: 1px solid var(--border-color); {chno_border} {chno_color} padding: 6px 8px; border-radius: 6px; font-size: 14px;"></td>
            <td>
                <strong>{html.escape(channel_name)}</strong>{mux_badge} → <span class="real-name">{html.escape(real_name)}</span>{dup_badge}
            </td>
            <td>{html.escape(ch["group"])}</td>
            <td class="resolver">{html.escape(resolver)}</td>
            <td><code class="tvg-id">{html.escape(epg_display)}</code></td>
            <td>
                <button class="btn-sm btn-primary" data-name="{escaped_name}" onclick="openSettings(this.dataset.name)" title="Редактировать">✏️</button>
                <button class="btn-sm btn-fallback {fallback_class}" data-name="{escaped_name}" onclick="toggleFallback(this)" title="Fallback">⚡</button>
                <button class="btn-sm btn-danger" data-name="{escaped_name}" onclick="deleteChannel(this.dataset.name)" title="Удалить">🗑️</button>
            </td>
        </tr>""")

    rows_html = "\n".join(rows)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"rows": rows_html}
    )
