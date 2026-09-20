let streamsData = [];
let epgChannels = [];
let currentSelectedEpgId = '';
let currentSelectedEpgName = '';
let selectedOriginalName = '';
let isAddMode = false;
let currentSelectedSourceName = '';

// === Вспомогательные функции ===
function getStreamsFromData() {
    const streams = streamsData.map(s => {
        const out = {
            url: s.url || '',
            resolver: s.resolver || 'auto',
            ua: s.ua || '',
            fs_regex: s.fs_regex || '',
            disable: !!s.disable
        };
        // stream_id — identity стрима, должен доехать до бэка,
        // иначе слот в streams_cache потеряет привязку.
        if (s.stream_id) out.stream_id = s.stream_id;
        if (s.prefetch) out.prefetch = true;
        return out;
    });
    const activeIdx = streamsData.findIndex(s => s.active);
    return { streams, active_stream_index: activeIdx >= 0 ? activeIdx : 0 };
}

// === Открытие/закрытие модалок канала ===
function openSettings(originalName) {
    isAddMode = false;
    selectedOriginalName = originalName;

    fetch('/channels/get?name=' + encodeURIComponent(originalName))
        .then(r => r.json())
        .then(ch => {
            if (ch.success) {
                document.getElementById('editOriginalName').value = originalName;
                document.getElementById('settingsChannelName').textContent = originalName;
                document.getElementById('editName').value = ch.data.name || '';
                document.getElementById('editChno').value = ch.data.chno || '';
                const activeIdx = ch.data.active_stream_index || 0;
                const activeStream = ch.data.streams ? ch.data.streams[activeIdx] : null;
                document.getElementById('editActiveUrl').value = activeStream ? activeStream.url : (ch.data.url || '');
                document.getElementById('editActiveResolver').value = activeStream ? (activeStream.resolver || 'auto') : '—';
                const probeEl = ch.data.streams_cache && ch.data.streams_cache[activeIdx];
                const probeSec = probeEl && probeEl.probe_elapsed != null ? parseFloat(probeEl.probe_elapsed).toFixed(1) : '';
                document.getElementById('editProbeElapsed').value = probeSec ? probeSec + ' с' : '—';
                document.getElementById('editGroup').value = ch.data.group || '';
                document.getElementById('editLogo').value = ch.data.logo || '';
                document.getElementById('editComment').value = ch.data.comment || '';

                currentSelectedEpgName = ch.data.real_name || originalName;
                currentSelectedEpgId = ch.data.tvg_id || '';
                currentSelectedSourceName = ch.data.source_name || '';
                document.getElementById('currentEpgName').textContent = currentSelectedEpgName || 'Не выбран';
                document.getElementById('currentEpgId').textContent = currentSelectedEpgId
                    ? `${currentSelectedEpgId}${currentSelectedSourceName ? ` (${currentSelectedSourceName})` : ''}`
                    : '---';

                const streams = ch.data.streams || [];
                const streamsCache = ch.data.streams_cache || [];
                streamsData = streams.map((s, i) => {
                    const cache = streamsCache[i] || {};
                    return {
                        url: s.url || '',
                        resolver: s.resolver || 'auto',
                        ua: s.ua || '',
                        fs_regex: s.fs_regex || '',
                        disable: !!s.disable,
                        stream_id: s.stream_id || null,
                        prefetch: !!s.prefetch,
                        needs_mux: cache.needs_mux !== undefined ? cache.needs_mux : null,
                        active: i === (ch.data.active_stream_index || 0),
                        cached_stream: cache.cached_stream || '',
                        cache_expire: cache.cache_expire || 0,
                        last_check_time: cache.last_check_time,
                        last_check_success: cache.last_check_success,
                        last_check_detail: cache.last_check_detail || '',
                        elapsed: cache.probe_elapsed || null
                    };
                });

                const statusEl = document.getElementById('modalCheckStatus');
                if (ch.data.last_check_time) {
                    const dt = new Date(ch.data.last_check_time * 1000);
                    const dateStr = dt.toLocaleString();
                    if (ch.data.last_check_success) {
                        statusEl.textContent = `✅ Поток доступен (${dateStr})`;
                        statusEl.style.color = '#10b981';
                    } else {
                        statusEl.textContent = `❌ Ошибка: ${ch.data.last_check_detail || 'Недоступен'} (${dateStr})`;
                        statusEl.style.color = '#ef4444';
                    }
                } else {
                    statusEl.textContent = 'Статус не проверялся';
                    statusEl.style.color = 'var(--text-muted)';
                }

                document.getElementById('editCachedStream').value = ch.data.cached_stream || '';

                document.getElementById('search').value = '';
                epgChannels = [];
                loadChannelList();
                document.getElementById('settingsModal').style.display = 'block';
            } else {
                showToast('Не удалось загрузить данные канала');
            }
        })
        .catch(() => showToast('Ошибка соединения с сервером'));
}

function openAddModal() {
    isAddMode = true;
    document.getElementById('editOriginalName').value = '';
    document.getElementById('settingsChannelName').textContent = 'Новый канал';
    document.getElementById('editName').value = '';
    document.getElementById('editChno').value = '';
    document.getElementById('editActiveUrl').value = '';
    document.getElementById('editGroup').value = '';
    document.getElementById('editLogo').value = '';
    document.getElementById('editComment').value = '';
    streamsData = [{ url: '', resolver: 'auto', ua: '', fs_regex: '', disable: false, stream_id: null, prefetch: false, needs_mux: null, active: true, cached_stream: '' }];
    currentSelectedEpgId = '';
    currentSelectedEpgName = '';
    currentSelectedSourceName = '';
    document.getElementById('currentEpgName').textContent = 'Не выбран';
    document.getElementById('currentEpgId').textContent = '';
    document.getElementById('editCachedStream').value = '';
    document.getElementById('search').value = '';
    epgChannels = [];
    loadChannelList();
    document.getElementById('settingsModal').style.display = 'block';
}

function closeSettingsModal() {
    isAddMode = false;
    document.getElementById('settingsModal').style.display = 'none';
}

// === EPG список ===
function loadChannelList() {
    const list = document.getElementById('channelList');
    list.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-muted);">Загрузка каналов...</div>';
    fetch('/epg/channels', { cache: 'no-store' })
        .then(r => r.json())
        .then(data => {
            epgChannels = data.channels || [];
            renderChannelList(epgChannels);
        })
        .catch(() => {
            list.innerHTML = '<div style="padding: 20px; text-align: center; color: #ef4444;">Ошибка загрузки базы EPG</div>';
        });
}

function renderChannelList(channels) {
    const list = document.getElementById('channelList');
    list.innerHTML = '';
    const limit = 150;
    const toRender = channels.slice(0, limit);
    toRender.forEach(ch => {
        const div = document.createElement('div');
        div.className = 'channel-item';
        const sourceInfo = ch.source_name ? ` (${ch.source_name})` : '';
        div.innerHTML = `<strong>${ch.name || ch.id}</strong><br><span style="font-size: 12px; color: #888;">${ch.id}${sourceInfo}</span>`;
        div.onclick = () => selectChannel(ch.id, ch.name || ch.id, ch.source_name || '');
        list.appendChild(div);
    });
    if (channels.length > limit) {
        const more = document.createElement('div');
        more.style.padding = '10px';
        more.style.textAlign = 'center';
        more.style.color = 'var(--text-muted)';
        more.style.fontSize = '12px';
        more.innerText = `Показано ${limit} из ${channels.length}. Используйте поиск.`;
        list.appendChild(more);
    }
    if (channels.length === 0) {
        list.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-muted);">Ничего не найдено</div>';
    }
}

function filterChannels() {
    const q = document.getElementById('search').value.toLowerCase().trim();
    if (!q) {
        renderChannelList(epgChannels);
        return;
    }
    const filtered = epgChannels.filter(ch =>
        ch.name.toLowerCase().includes(q) ||
        ch.id.toLowerCase().includes(q)
    );
    renderChannelList(filtered);
}

function selectChannel(tvgId, primaryName, sourceName) {
    currentSelectedEpgId = tvgId;
    currentSelectedEpgName = primaryName;
    currentSelectedSourceName = sourceName || '';
    document.getElementById('currentEpgName').textContent = primaryName;
    const sourceInfo = sourceName ? ` (${sourceName})` : '';
    document.getElementById('currentEpgId').textContent = tvgId ? tvgId + sourceInfo : '---';
    showToast('EPG выбран. Нажмите "Применить изменения" для сохранения.');
}

// === Потоки: модальное окно ===
function openStreamsModal() {
    document.getElementById('streamsChannelName').textContent = document.getElementById('editName').value || document.getElementById('settingsChannelName').textContent;
    const container = document.getElementById('streamsContainer');
    container.innerHTML = '';
    streamsData.forEach((stream, index) => {
        addStreamFieldToDOM(stream, index);
    });
    document.getElementById('streamsModal').style.display = 'block';
}

function closeStreamsModal() {
    document.getElementById('streamsModal').style.display = 'none';
}

function saveStreamsFromModal() {
    const container = document.getElementById('streamsContainer');
    const items = container.querySelectorAll('.stream-item');
    const newStreams = [];
    items.forEach((item, idx) => {
        const sidRaw = item.querySelector('.stream-id')?.value || '';
        const sid = parseInt(sidRaw, 10);
        const disableCb = item.querySelector('input[onchange^="setStreamDisable"]');
        newStreams.push({
            url: item.querySelector('.stream-url').value,
            resolver: item.querySelector('.stream-resolver').value,
            ua: item.querySelector('.stream-ua').value,
            fs_regex: item.querySelector('.stream-fs-regex').value,
            disable: disableCb ? disableCb.checked : false,
            stream_id: Number.isFinite(sid) && sid > 0 ? sid : null,
            prefetch: item.querySelector('.stream-prefetch')?.checked || false,
            active: false
        });
    });
    const checkedRadio = container.querySelector('input[name="activeStream"]:checked');
    const activeIdx = checkedRadio ? parseInt(checkedRadio.value) : 0;
    newStreams.forEach((s, i) => s.active = (i === activeIdx));
    streamsData = newStreams;

    // Обновляем активный URL и резолвер в модалке канала
    const activeStream = streamsData.find(s => s.active);
    if (activeStream) {
        const activeUrlInput = document.getElementById('editActiveUrl');
        if (activeUrlInput) activeUrlInput.value = activeStream.url;
        const resolverInput = document.getElementById('editActiveResolver');
        if (resolverInput) resolverInput.value = activeStream.resolver || 'auto';
    }

    closeStreamsModal();
    showToast('Потоки сохранены в памяти. Нажмите "Применить изменения" для записи в конфиг.');
}

function addStreamField() {
    const newStream = { url: '', resolver: 'auto', ua: '', fs_regex: '', disable: false, stream_id: null, prefetch: false, needs_mux: null, active: false, cached_stream: '' };
    streamsData.push(newStream);
    const index = streamsData.length - 1;
    addStreamFieldToDOM(newStream, index);
    if (streamsData.length === 1) {
        streamsData[0].active = true;
        const radio = document.querySelector(`input[name="activeStream"][value="${index}"]`);
        if (radio) radio.checked = true;
    }
}

function addStreamFieldToDOM(stream, index) {
    const container = document.getElementById('streamsContainer');
    const div = document.createElement('div');
    div.className = 'stream-item';
    div.dataset.index = index;
    div.style.border = '1px solid var(--border-color)';
    div.style.padding = '12px';
    div.style.marginBottom = '12px';
    div.style.borderRadius = '8px';
    div.style.background = 'var(--bg-color)';

    div.innerHTML = `
        <input type="hidden" class="stream-id" value="${stream.stream_id || ''}">
        <div style="display: flex; gap: 12px; align-items: center; margin-bottom: 10px;">
            <label style="display: flex; align-items: center; gap: 4px; font-size: 13px; color: var(--text-muted);">
                <input type="radio" name="activeStream" value="${index}" ${stream.active ? 'checked' : ''} onchange="setActiveStream(${index})">
                Активный
            </label>
            <label style="display: flex; align-items: center; gap: 4px; font-size: 13px; color: var(--text-muted);">
                <input type="checkbox" ${stream.disable ? 'checked' : ''} onchange="setStreamDisable(${index}, this.checked)">
                Отключить
            </label>
            <label style="display: flex; align-items: center; gap: 4px; font-size: 13px; color: var(--text-muted);" title="Фоновая подкачка HLS-сегментов">
                <input type="checkbox" class="stream-prefetch" ${stream.prefetch ? 'checked' : ''}>
                Prefetch
            </label>
            <span class="stream-mux-status" style="display: flex; align-items: center;">
                ${renderMuxBadge(stream.needs_mux, index)}
            </span>
            <button class="btn-sm btn-danger" style="margin-left: auto;" onclick="removeStreamField(${index})">Удалить</button>
        </div>

        <div style="display: flex; flex-direction: column; gap: 6px;">
            <div style="display: flex; align-items: center;">
                <label style="width: 120px; flex-shrink: 0; text-align: right; margin-right: 8px; font-size: 13px; color: var(--text-muted);">URL:</label>
                <input type="text" class="stream-url" value="${stream.url || ''}" style="flex: 1; background: var(--bg-color); border: 1px solid var(--border-color); color: var(--text-main); padding: 6px 10px; border-radius: 6px;">
            </div>
            <div style="display: flex; align-items: center;">
                <label style="width: 120px; flex-shrink: 0; text-align: right; margin-right: 8px; font-size: 13px; color: var(--text-muted);">Резолвер:</label>
                <select class="stream-resolver" style="flex: 1; background: var(--bg-color); border: 1px solid var(--border-color); color: var(--text-main); padding: 6px 10px; border-radius: 6px;">
                    <option value="auto" ${stream.resolver === 'auto' ? 'selected' : ''}>auto</option>
                    <option value="direct" ${stream.resolver === 'direct' ? 'selected' : ''}>direct</option>
                    <option value="yt-dlp" ${stream.resolver === 'yt-dlp' ? 'selected' : ''}>yt-dlp</option>
                    <option value="streamlink" ${stream.resolver === 'streamlink' ? 'selected' : ''}>streamlink</option>
                    <option value="flaresolverr_simple" ${stream.resolver === 'flaresolverr_simple' ? 'selected' : ''}>flaresolverr_simple</option>
                    <option value="flaresolverr_session" ${stream.resolver === 'flaresolverr_session' ? 'selected' : ''}>flaresolverr_session</option>
                    <option value="sniffer" ${stream.resolver === 'sniffer' ? 'selected' : ''}>sniffer</option>
                </select>
            </div>
            <div style="display: flex; align-items: center;">
                <label style="width: 120px; flex-shrink: 0; text-align: right; margin-right: 8px; font-size: 13px; color: var(--text-muted);">User-Agent:</label>
                <input type="text" class="stream-ua" value="${stream.ua || ''}" style="flex: 1; background: var(--bg-color); border: 1px solid var(--border-color); color: var(--text-main); padding: 6px 10px; border-radius: 6px;">
            </div>
            <div style="display: flex; align-items: center;">
                <label style="width: 120px; flex-shrink: 0; text-align: right; margin-right: 8px; font-size: 13px; color: var(--text-muted);">FlareSolverr Regex:</label>
                <input type="text" class="stream-fs-regex" value="${stream.fs_regex || ''}" style="flex: 1; background: var(--bg-color); border: 1px solid var(--border-color); color: var(--text-main); padding: 6px 10px; border-radius: 6px;">
            </div>
        </div>

        <div style="display: flex; gap: 8px; margin-top: 10px; align-items: center;">
            <input type="text" class="stream-cached-url" readonly value="${stream.cached_stream || ''}" style="flex: 1; background: #111318; color: #60a5fa; cursor: pointer; padding: 6px 10px; border: 1px solid var(--border-color); border-radius: 6px; font-family: monospace;" onclick="copyStreamCachedUrl(${index})" title="Копировать">
            <button class="btn-sm" onclick="checkStreamByIndex(${index})">Проверить поток</button>
            <button class="btn-sm" onclick="probeStreamByIndex(${index})">Проверить ffprobe</button>
            <span class="stream-elapsed" style="font-size: 12px; color: var(--text-muted); min-width: 50px; text-align: center;">
                ${stream.elapsed ? stream.elapsed.toFixed(1) + 'с' : '—'}
            </span>
            <button class="btn-sm" onclick="clearStreamCache(${index})" title="Удалить кэш стрима">🗑️</button>
            <span class="stream-check-status" style="font-size: 12px; color: var(--text-muted); min-width: 120px; text-align: right;"></span>
        </div>
    `;
    container.appendChild(div);
}

function removeStreamField(index) {
    const container = document.getElementById('streamsContainer');
    const item = container.querySelector(`.stream-item[data-index="${index}"]`);
    if (item) item.remove();
    streamsData.splice(index, 1);
    reindexStreamFields();
}

function reindexStreamFields() {
    const container = document.getElementById('streamsContainer');
    const items = container.querySelectorAll('.stream-item');
    items.forEach((item, idx) => {
        item.dataset.index = idx;
        const radio = item.querySelector('input[name="activeStream"]');
        if (radio) radio.value = idx;
    });
    const checkedRadio = container.querySelector('input[name="activeStream"]:checked');
    const activeIdx = checkedRadio ? parseInt(checkedRadio.value) : 0;
    streamsData.forEach((s, i) => s.active = (i === activeIdx));
}

function setActiveStream(index) {
    streamsData.forEach((s, i) => s.active = (i === index));
    document.querySelectorAll('input[name="activeStream"]').forEach(radio => {
        radio.checked = (parseInt(radio.value) === index);
    });
}

function setStreamDisable(index, checked) {
    if (streamsData[index]) streamsData[index].disable = checked;
}

function renderMuxBadge(needsMux, index) {
    if (needsMux === true) {
        return '<span class="mux-badge mux-on" title="По master-плейлисту требуется мукс (раздельные A/V)">MUX</span>';
    } else if (needsMux === false) {
        return '<span class="mux-badge mux-off" title="Мукс не требуется, поток играется напрямую">—</span>';
    }
    return `<button class="btn-sm" onclick="checkStreamMux(${index})" title="Проверить, нужен ли мукс (один HTTP-запрос на master)">? MUX</button>`;
}

function checkStreamMux(index) {
    const stream = streamsData[index];
    if (!stream || !stream.url) { showToast('Нет URL у потока'); return; }
    const statusEl = document.querySelector(`.stream-item[data-index="${index}"] .stream-mux-status`);
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--text-muted);font-size:12px;">⏳ проверка...</span>';
    const name = document.getElementById('editOriginalName').value || document.getElementById('editName').value.trim();
    fetch('/channels/check-mux', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            name: name,
            url: stream.url,
            ua: stream.ua,
            fs_regex: stream.fs_regex,
            resolver: stream.resolver
        })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            streamsData[index].needs_mux = res.needs_mux;
            if (statusEl) statusEl.innerHTML = renderMuxBadge(res.needs_mux, index);
        } else {
            if (statusEl) statusEl.innerHTML = '<span style="color:#ef4444;font-size:12px;">ошибка</span>';
            showToast('Ошибка: ' + (res.error || ''));
        }
    })
    .catch(() => {
        if (statusEl) statusEl.innerHTML = '<span style="color:#ef4444;font-size:12px;">сеть</span>';
    });
}

// === Проверка потоков ===
function checkModalStream() {
    const { streams, active_stream_index } = getStreamsFromData();
    const activeStream = streams[active_stream_index];
    if (!activeStream || !activeStream.url) {
        showToast('Нет активного потока с URL');
        return;
    }
    const statusEl = document.getElementById('modalCheckStatus');
    statusEl.textContent = '⏳ Проверка потока...';
    statusEl.style.color = 'var(--text-muted)';

    fetch('/channels/check-single', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            name: document.getElementById('editOriginalName').value || document.getElementById('editName').value.trim(),
            url: activeStream.url,
            ua: activeStream.ua || 'Mozilla/5.0',
            fs_regex: activeStream.fs_regex || '',
            resolver: activeStream.resolver || 'auto'
        })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            statusEl.textContent = '✅ Поток доступен';
            statusEl.style.color = '#10b981';
            const cachedField = document.getElementById('editCachedStream');
            if (cachedField) cachedField.value = res.cached_stream || '';
            if (res.method && activeStream.resolver === 'auto') {
                // ВАЖНО: getStreamsFromData() вернул новый массив через .map,
                // мутировать activeStream бесполезно — изменения уйдут в мусор.
                // Пишем в streamsData напрямую, чтобы "Применить изменения"
                // сохранил предложенный резолвер.
                if (streamsData[active_stream_index]) {
                    streamsData[active_stream_index].resolver = res.method;
                }
                showToast(`Резолвер "${res.method}" предложен. Нажмите "Применить изменения", чтобы сохранить.`);
            }
        } else {
            statusEl.textContent = '❌ Ошибка: ' + (res.detail || 'Недоступен');
            statusEl.style.color = '#ef4444';
        }
    })
    .catch(() => { statusEl.textContent = '❌ Ошибка сети'; statusEl.style.color = '#ef4444'; });
}

function probeChannelStream() {
    const { streams, active_stream_index } = getStreamsFromData();
    const activeStream = streams[active_stream_index];
    if (!activeStream || !activeStream.url) { showToast('Нет активного потока'); return; }
    const statusEl = document.getElementById('modalCheckStatus');
    statusEl.textContent = '⏳ Запуск ffprobe...';
    statusEl.style.color = 'var(--text-muted)';

    const originalName = document.getElementById('editOriginalName').value.trim();
    const newName = document.getElementById('editName').value.trim();
    const effectiveName = originalName || newName;

    const requestBody = {
        name: effectiveName,
        url: isAddMode ? activeStream.url : null,
        ua: activeStream.ua,
        fs_regex: activeStream.fs_regex,
        resolver: activeStream.resolver
    };

    fetch('/channels/probe', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(requestBody)
    })
    .then(r => r.json())
    .then(res => {
        if (res.success && res.probe && res.probe.ok) {
            const p = res.probe;
            let msg = `✅ Видео: ${p.has_video ? 'есть' : 'нет'}, Аудио: ${p.has_audio ? 'есть' : 'нет'}`;
            if (!p.has_audio) msg += ' — возможно, нужен мукс!';
            statusEl.textContent = msg;
            statusEl.style.color = p.has_video && p.has_audio ? '#10b981' : '#ef4444';
            if (p.probe_elapsed != null) {
                const pe = document.getElementById('editProbeElapsed');
                if (pe) pe.value = parseFloat(p.probe_elapsed).toFixed(1) + ' с';
            }
            if (res.method) {
                const ar = document.getElementById('editActiveResolver');
                if (ar) ar.value = res.method;
            }
        } else {
            statusEl.textContent = '❌ Ошибка: ' + (res.error || res.probe?.detail || 'Не удалось проверить');
            statusEl.style.color = '#ef4444';
        }
    })
    .catch(err => { statusEl.textContent = '❌ Сетевая ошибка'; statusEl.style.color = '#ef4444'; });
}

// === Проверка отдельных стримов ===
function checkStreamByIndex(index) {
    const item = document.querySelector(`.stream-item[data-index="${index}"]`);
    if (!item) { showToast('Стрим не найден'); return; }
    const url = item.querySelector('.stream-url')?.value || '';
    const resolver = item.querySelector('.stream-resolver')?.value || 'auto';
    const ua = item.querySelector('.stream-ua')?.value || '';
    const fs_regex = item.querySelector('.stream-fs-regex')?.value || '';

    if (!url) { showToast('Нет URL у потока'); return; }

    if (streamsData[index]) {
        streamsData[index].url = url;
        streamsData[index].resolver = resolver;
        streamsData[index].ua = ua;
        streamsData[index].fs_regex = fs_regex;
    }

    const statusEl = item.querySelector('.stream-check-status');
    if (statusEl) {
        statusEl.textContent = '⏳ Проверка...';
        statusEl.style.color = 'var(--text-muted)';
    }

    const name = document.getElementById('editOriginalName').value || document.getElementById('editName').value.trim();
    fetch('/channels/check-single', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            name: name,
            url: url,
            ua: ua || 'Mozilla/5.0',
            fs_regex: fs_regex || '',
            resolver: resolver || 'auto'
        })
    })
    .then(r => r.json())
    .then(res => {
        if (statusEl) {
            if (res.success) {
                statusEl.textContent = '✅ Успех';
                statusEl.style.color = '#10b981';
                if (streamsData[index]) {
                    streamsData[index].cached_stream = res.cached_stream || '';
                }
                const cachedInput = item.querySelector('.stream-cached-url');
                if (cachedInput) cachedInput.value = res.cached_stream || '';
                if (res.method) {
                    if (streamsData[index]) streamsData[index].resolver = res.method;
                    const select = item.querySelector('.stream-resolver');
                    if (select) select.value = res.method;
                    showToast(`Резолвер "${res.method}" предложен. Нажмите "Сохранить потоки" и "Применить изменения".`);
                }
            } else {
                statusEl.textContent = '❌ ' + (res.detail || 'Ошибка');
                statusEl.style.color = '#ef4444';
            }
        }
    })
    .catch(err => {
        if (statusEl) {
            statusEl.textContent = '❌ Сеть';
            statusEl.style.color = '#ef4444';
        }
    });
}

function probeStreamByIndex(index) {
    const stream = streamsData[index];
    if (!stream || !stream.url) { showToast('Нет URL у потока'); return; }
    const statusEl = document.querySelector(`.stream-item[data-index="${index}"] .stream-check-status`);
    if (statusEl) {
        statusEl.textContent = '⏳ ffprobe...';
        statusEl.style.color = 'var(--text-muted)';
    }
    const name = document.getElementById('editOriginalName').value || document.getElementById('editName').value.trim();
    fetch('/channels/probe', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            name: name,
            url: stream.url,
            ua: stream.ua,
            fs_regex: stream.fs_regex,
            resolver: stream.resolver
        })
    })
    .then(r => r.json())
    .then(res => {
        if (statusEl) {
            if (res.success && res.probe && res.probe.ok) {
                const p = res.probe;
                let msg = `✅ Видео: ${p.has_video ? 'есть' : 'нет'}, Аудио: ${p.has_audio ? 'есть' : 'нет'}`;
                if (!p.has_audio) msg += ' — возможно, нужен мукс!';
                statusEl.textContent = msg;
                statusEl.style.color = p.has_video && p.has_audio ? '#10b981' : '#ef4444';
            } else {
                statusEl.textContent = '❌ ' + (res.error || res.probe?.detail || 'Ошибка');
                statusEl.style.color = '#ef4444';
            }
            if (streamsData[index]) {
                streamsData[index].elapsed = res.probe?.probe_elapsed || res.probe?.elapsed || null;
                const elapsedEl = document.querySelector(`.stream-item[data-index="${index}"] .stream-elapsed`);
                if (elapsedEl && streamsData[index].elapsed !== null) {
                    elapsedEl.textContent = streamsData[index].elapsed.toFixed(1) + 'с';
                } else if (elapsedEl) {
                    elapsedEl.textContent = '—';
                }
            }
        }
    })
    .catch(err => {
        if (statusEl) {
            statusEl.textContent = '❌ Сеть';
            statusEl.style.color = '#ef4444';
        }
    });
}

function clearStreamCache(index) {
    const stream = streamsData[index];
    if (!stream) return;
    if (!confirm('Удалить кэш этого стрима?')) return;
    const name = document.getElementById('editOriginalName').value || document.getElementById('editName').value.trim();
    fetch('/channels/clear-cache', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name: name, stream_index: index })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            streamsData[index].cached_stream = '';
            const cachedInput = document.querySelector(`.stream-item[data-index="${index}"] .stream-cached-url`);
            if (cachedInput) cachedInput.value = '';
            const statusEl = document.querySelector(`.stream-item[data-index="${index}"] .stream-check-status`);
            if (statusEl) {
                statusEl.textContent = 'Кэш очищен';
                statusEl.style.color = 'var(--text-muted)';
            }
            showToast('Кэш стрима удалён');
        } else {
            showToast('Ошибка: ' + (res.error || ''));
        }
    })
    .catch(err => {
        console.error(err);
        showToast('Сетевая ошибка');
    });
}

function copyStreamCachedUrl(index) {
    const input = document.querySelector(`.stream-item[data-index="${index}"] .stream-cached-url`);
    if (input && input.value) {
        input.select();
        input.setSelectionRange(0, 99999);
        try {
            document.execCommand('copy');
            showToast('Ссылка скопирована');
        } catch (err) {
            showToast('Не удалось скопировать');
        }
    } else {
        showToast('Нет ссылки');
    }
}

function copyCachedStream() {
    const input = document.getElementById('editCachedStream');
    if (input && input.value) {
        input.select();
        input.setSelectionRange(0, 99999);
        try {
            document.execCommand('copy');
            showToast('Ссылка скопирована');
        } catch (err) {
            showToast('Не удалось скопировать');
        }
    } else {
        showToast('Нет ссылки');
    }
}

// === Сохранение ===
function saveUnifiedSettings() {
    const originalName = document.getElementById('editOriginalName').value;
    const newName = document.getElementById('editName').value.trim();
    const chno = document.getElementById('editChno').value.trim();
    const group = document.getElementById('editGroup').value.trim();
    const logo = document.getElementById('editLogo').value.trim();
    const comment = document.getElementById('editComment').value.trim();
    if (!newName) { showToast('Название обязательно'); return; }

    const { streams, active_stream_index } = getStreamsFromData();

    const payload = {
        original_name: originalName,
        name: newName,
        chno: chno,
        group: group,
        logo: logo,
        comment: comment,
        streams: streams,
        active_stream_index: active_stream_index,
        tvgid: currentSelectedEpgId || ''
    };
    // Если EPG выбран в модалке — шлём его имя как real_name. Бэк приоритетно
    // берёт из EPG-каталога, но это fallback на случай, если каталог ещё
    // не загружен или tvgid нестандартный.
    if (currentSelectedEpgId && currentSelectedEpgName) {
        payload.real_name = currentSelectedEpgName;
    }

    const endpoint = isAddMode ? '/channels/add' : '/channels/update-stream';
    fetch(endpoint, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            closeSettingsModal();
            showToast('Сохранено');
            setTimeout(() => location.reload(), 800);
        } else {
            showToast('Ошибка: ' + (res.error || ''));
        }
    })
    .catch(() => showToast('Сетевая ошибка'));
}

function clearChannelCache() {
    const originalName = document.getElementById('editOriginalName').value.trim();
    if (!originalName) { showToast('Ошибка: неизвестный канал'); return; }
    showGlobalSpinner('Очистка кэша потока...');
    fetch('/channels/clear-cache', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name: originalName })
    })
    .then(r => r.json())
    .then(res => {
        hideGlobalSpinner();
        if (res.success) {
            showToast('Кэш потока очищен.');
            document.getElementById('editCachedStream').value = '';
            document.getElementById('modalCheckStatus').textContent = 'Статус не проверялся';
            document.getElementById('modalCheckStatus').style.color = 'var(--text-muted)';
        } else {
            showToast('Ошибка: ' + (res.error || 'не удалось очистить кэш'));
        }
    })
    .catch(err => { hideGlobalSpinner(); console.error(err); showToast('Сетевая ошибка при очистке кэша'); });
}

// === Таблица каналов ===
function toggleChannel(checkbox, name) {
    const enabled = checkbox.checked;
    fetch('/channels/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name: name, enabled: enabled })
    })
    .then(r => r.json())
    .then(res => {
        if (!res.success) {
            checkbox.checked = !enabled;
            showToast('Ошибка при изменении статуса: ' + (res.error || ''));
        }
    })
    .catch(() => { checkbox.checked = !enabled; showToast('Сетевая ошибка при переключении'); });
}

async function toggleFallback(btn) {
    const name = btn.dataset.name;
    const isActive = btn.classList.contains('active');
    const enabled = !isActive;
    try {
        const resp = await fetch('/channels/toggle-fallback', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, enabled})
        });
        const data = await resp.json();
        if (data.success) {
            if (enabled) {
                btn.classList.add('active');
                btn.style.color = '#ffaa00';
            } else {
                btn.classList.remove('active');
                btn.style.color = '';
            }
        } else {
            alert('Не удалось переключить fallback: ' + (data.error || 'ошибка'));
        }
    } catch (e) {
        console.error(e);
        alert('Ошибка сети');
    }
}

function deleteChannel(name) {
    if (!confirm(`Вы уверены, что хотите удалить канал "${name}"?`)) return;
    fetch('/channels/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name: name })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            const row = getRowByChannelName(name);
            if (row) row.remove();
            showToast('Канал удален');
            setTimeout(() => saveChanges(), 500);
        } else {
            showToast('Ошибка при удалении');
        }
    });
}

function getRowByChannelName(name) {
    const safeName = name.replace(/"/g, '\\"');
    const btn = document.querySelector(`button[data-name="${safeName}"]`);
    return btn ? btn.closest('tr') : null;
}

function saveChanges() {
    const rows = document.querySelectorAll('tbody tr');
    const data = {};
    rows.forEach((row, index) => {
        const editBtn = row.querySelector('button.btn-primary');
        if (!editBtn) return;
        const origName = row.getAttribute('data-original-name') || editBtn.getAttribute('data-name');
        if (!origName) return;
        const epgNameEl = row.querySelector('.real-name');
        const tvgIdEl = row.querySelector('.tvg-id');
        if (!epgNameEl || !tvgIdEl) {
            console.warn('saveChanges: пропущена строка без .real-name/.tvg-id', origName);
            return;
        }
        const chnoInput = row.querySelector('.channel-chno') || row.querySelector('td:nth-child(3) input');
        // enabled больше не отправляем здесь: клик по чекбоксу
        // моментально идёт через /channels/toggle (onchange в шаблоне).
        // Если оставить тут enabled — «Сохранить» перезапишет поверх
        // то же значение вторым запросом, что даёт лишний шум и
        // может перетереть параллельный toggle.

        let tvgIdText = tvgIdEl ? tvgIdEl.textContent.trim() : '';
        if (tvgIdText.includes(' (')) {
            tvgIdText = tvgIdText.split(' (')[0].trim();
        }

        data[origName] = {
            real_name: epgNameEl ? epgNameEl.textContent.trim() : '',
            tvg_id: tvgIdText,
            chno: chnoInput ? chnoInput.value.trim() : (index + 1).toString()
        };
    });
    fetch('/epg/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ cache: data })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            showToast('Настройки и порядок успешно сохранены!');
            setTimeout(() => location.reload(), 800);
        } else {
            showToast('Ошибка сохранения: ' + (res.error || 'Неизвестно'));
        }
    })
    .catch(() => showToast('Сетевая ошибка при сохранении'));
}

function runHealthcheckAll() {
    const btn = document.getElementById('checkAllBtn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Запуск проверки...'; }
    showToast('Запуск проверки всех каналов...');
    showGlobalSpinner('Запуск проверки каналов...');
    fetch('/channels/check-all/start', { method: 'POST' })
        .then(r => r.json())
        .then(res => {
            if (!res.success) {
                showToast('Ошибка запуска: ' + (res.error || 'неизвестная'));
                hideGlobalSpinner();
                if (btn) { btn.disabled = false; btn.textContent = '🔍 Проверить все каналы'; }
            }
        })
        .catch(err => {
            console.error('Ошибка запуска:', err);
            showToast('Сетевая ошибка при запуске проверки');
            hideGlobalSpinner();
            if (btn) { btn.disabled = false; btn.textContent = '🔍 Проверить все каналы'; }
        });
}
