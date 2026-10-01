/**
 * UI для вкладки «Плейлисты» и (в Части 5.2) модалки поиска.
 *
 * Работает с API:
 *   GET  /playlists/sources        — список источников
 *   POST /playlists/sources        — сохранить источники
 *   POST /playlists/refresh/{name} — обновить один (фоново)
 *   POST /playlists/refresh-all    — обновить все
 *   POST /playlists/clear/{name}   — очистить данные в SQLite
 *
 * (5.2) GET  /playlists/search     — поиск
 * (5.2) POST /playlists/check      — HEAD-проверка URL
 */

let playlistSources = [];

// ===========================================================================
// Вкладка «Плейлисты»
// ===========================================================================

function loadPlaylistSources() {
    const container = document.getElementById('playlistSourcesTableContainer');
    container.innerHTML = 'Загрузка...';
    fetch('/playlists/sources')
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                playlistSources = data.sources || [];
                renderPlaylistSourcesTable(playlistSources);
            } else {
                container.innerHTML = 'Ошибка: ' + (data.error || '');
            }
        })
        .catch(err => {
            console.error(err);
            container.innerHTML = 'Сетевая ошибка при загрузке источников';
        });
}

function renderPlaylistSourcesTable(sources) {
    const container = document.getElementById('playlistSourcesTableContainer');
    if (sources.length === 0) {
        container.innerHTML = '<p style="color: var(--text-muted);">Источники не найдены. Нажмите «+ Добавить источник».</p>';
        return;
    }
    let html = `<div style="overflow-x: auto;"><table style="width: 100%; table-layout: fixed;"><thead><tr>
    <th style="width: 120px;">Имя</th>
    <th style="width: 220px;">URL</th>
    <th style="width: 70px;">Интервал</th>
    <th style="width: 80px;">Каналов</th>
    <th style="width: 90px;">Обновлено</th>
    <th style="width: 90px;">Статус</th>
    <th style="width: 200px; white-space: nowrap;">Действия</th>
    </tr></thead><tbody>`;
    sources.forEach((src, index) => {
        const disabled = src.disable ? '❌ откл' : '✅ акт';
        const inConfig = src.in_config ? '' : ' <span style="color:#888;font-size:11px;">(нет в config)</span>';
        let updatedText = '—';
        if (src.updated_at) {
            const dt = new Date(src.updated_at * 1000);
            const dd = String(dt.getDate()).padStart(2, '0');
            const mm = String(dt.getMonth() + 1).padStart(2, '0');
            const hh = String(dt.getHours()).padStart(2, '0');
            const min = String(dt.getMinutes()).padStart(2, '0');
            updatedText = `${dd}.${mm} ${hh}:${min}`;
        }
        const count = src.channel_count || 0;
        const errTitle = src.last_error ? ` title="${src.last_error.replace(/"/g, '&quot;')}"` : '';
        const errIcon = src.last_error ? ' ⚠️' : '';
        html += `<tr>
            <td>${src.name || src.url}${inConfig}</td>
            <td style="max-width: 250px; overflow: hidden; text-overflow: ellipsis;" title="${src.url}">${src.url}</td>
            <td>${Math.round(src.interval / 3600)}ч</td>
            <td>${count}</td>
            <td>${updatedText}</td>
            <td${errTitle}>${disabled}${errIcon}</td>
            <td style="white-space: nowrap; overflow: visible;">
                <button class="btn-sm btn-primary" onclick="openPlaylistSourceModal(${index})" title="Редактировать">✏️</button>
                <button class="btn-sm" onclick="refreshPlaylistSource(${index})" title="Обновить источник">🔄</button>
                <button class="btn-sm btn-danger" onclick="clearPlaylistSource(${index})" title="Очистить данные источника">🗑️</button>
            </td>
        </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

function openPlaylistSourceModal(index = null) {
    const modal = document.getElementById('playlistSourceModal');
    const title = document.getElementById('playlistSourceModalTitle');

    if (index !== null && playlistSources[index]) {
        const src = playlistSources[index];
        document.getElementById('editPlaylistSourceIndex').value = index;
        document.getElementById('editPlaylistSourceOrigName').value = src.name || '';
        title.textContent = 'Редактировать источник';
        document.getElementById('playlistSourceName').value = src.name || '';
        document.getElementById('playlistSourceUrl').value = src.url || '';
        document.getElementById('playlistSourceInterval').value = (Math.round(src.interval / 3600)).toString() + 'h';
        document.getElementById('playlistSourceDisable').checked = !!src.disable;
    } else {
        document.getElementById('editPlaylistSourceIndex').value = '';
        document.getElementById('editPlaylistSourceOrigName').value = '';
        title.textContent = 'Добавить источник плейлиста';
        document.getElementById('playlistSourceName').value = '';
        document.getElementById('playlistSourceUrl').value = '';
        document.getElementById('playlistSourceInterval').value = '24h';
        document.getElementById('playlistSourceDisable').checked = true;
    }
    modal.style.display = 'block';
}

function closePlaylistSourceModal() {
    document.getElementById('playlistSourceModal').style.display = 'none';
}

function savePlaylistSource() {
    const indexRaw = document.getElementById('editPlaylistSourceIndex').value;
    const name = document.getElementById('playlistSourceName').value.trim();
    const url = document.getElementById('playlistSourceUrl').value.trim();
    if (!name) { showToast('Имя источника обязательно'); return; }
    if (!url) { showToast('URL обязателен'); return; }

    const intervalStr = document.getElementById('playlistSourceInterval').value; // 6h/12h/24h/48h
    const intervalHours = parseInt(intervalStr.replace('h', ''), 10) || 24;
    const intervalSec = intervalHours * 3600;

    const source = {
        name: name,
        url: url,
        interval: intervalSec,
        disable: document.getElementById('playlistSourceDisable').checked
    };

    // Собираем список для отправки. Источники, которых нет в config
    // (in_config=false) — из формы не редактируются, но в общий список
    // их не включаем (save их выкинет — их нет в config).
    const updated = playlistSources
        .filter(s => s.in_config !== false)
        .map(s => ({ name: s.name, url: s.url, interval: s.interval, disable: !!s.disable }));

    if (indexRaw === '') {
        if (updated.some(s => s.name === name)) {
            showToast('Источник с таким именем уже существует');
            return;
        }
        updated.push(source);
    } else {
        const idx = parseInt(indexRaw, 10);
        // Сопоставляем по origName (индекс в playlistSources совпадает с фильтрованным updated только
        // если нет выкинутых источников; безопаснее — по имени)
        const origName = document.getElementById('editPlaylistSourceOrigName').value;
        const pos = updated.findIndex(s => s.name === origName);
        if (pos === -1) {
            updated.push(source);
        } else {
            updated[pos] = source;
        }
    }

    showGlobalSpinner('Сохранение источников...');
    fetch('/playlists/sources', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ sources: updated })
    })
    .then(r => r.json())
    .then(res => {
        hideGlobalSpinner();
        if (res.success) {
            closePlaylistSourceModal();
            loadPlaylistSources();
            showToast('Источники сохранены');
        } else {
            showToast('Ошибка: ' + (res.error || ''));
        }
    })
    .catch(err => { hideGlobalSpinner(); console.error(err); showToast('Сетевая ошибка'); });
}

function refreshPlaylistSource(index) {
    const src = playlistSources[index];
    if (!src) return;
    if (src.in_config === false) {
        showToast('Источник не в config — сначала сохраните его');
        return;
    }
    if (!confirm(`Обновить источник "${src.name}"?`)) return;

    showGlobalSpinner(`Обновление "${src.name}"...`);
    fetch('/playlists/refresh/' + encodeURIComponent(src.name), { method: 'POST' })
        .then(r => r.json())
        .then(res => {
            hideGlobalSpinner();
            if (res.success) {
                showToast('Обновление запущено в фоне');
            } else {
                showToast('Ошибка: ' + (res.error || ''));
            }
        })
        .catch(err => { hideGlobalSpinner(); console.error(err); showToast('Сетевая ошибка'); });
}

function refreshAllPlaylists() {
    if (!confirm('Запустить обновление всех активных источников?')) return;
    showGlobalSpinner('Обновление всех источников...');
    fetch('/playlists/refresh-all', { method: 'POST' })
        .then(r => r.json())
        .then(res => {
            hideGlobalSpinner();
            if (res.success) {
                showToast(`Обновление запущено (${res.count} источников)`);
            } else {
                showToast('Ошибка: ' + (res.error || ''));
            }
        })
        .catch(err => { hideGlobalSpinner(); console.error(err); showToast('Сетевая ошибка'); });
}

function clearPlaylistSource(index) {
    const src = playlistSources[index];
    if (!src) return;
    if (!confirm(`Очистить данные источника "${src.name}"?\n\nКаналы, уже добавленные в config.json, останутся.`)) return;

    showGlobalSpinner('Очистка данных...');
    fetch('/playlists/clear/' + encodeURIComponent(src.name), { method: 'POST' })
        .then(r => r.json())
        .then(res => {
            hideGlobalSpinner();
            if (res.success) {
                loadPlaylistSources();
                showToast('Данные источника очищены');
            } else {
                showToast('Ошибка: ' + (res.error || ''));
            }
        })
        .catch(err => { hideGlobalSpinner(); console.error(err); showToast('Сетевая ошибка'); });
}


// ===========================================================================
// Модалка поиска (5.2)
// ===========================================================================

let currentPlaylistSearchStreamIndex = null;
let currentPlaylistSearchSourceFilter = '';
let currentPlaylistSearchResults = [];
let _playlistSearchTimer = null;

function openPlaylistSearch(streamIndex) {
    currentPlaylistSearchStreamIndex = streamIndex;
    currentPlaylistSearchResults = [];

    document.getElementById('playlistSearchQuery').value = '';
    document.getElementById('playlistSearchResults').innerHTML =
        '<div style="padding: 20px; text-align: center; color: var(--text-muted);">Введите запрос</div>';

    // Заполняем фильтр источников (только те, что in_config, чтобы не мусорить)
    const sel = document.getElementById('playlistSearchSourceFilter');
    sel.innerHTML = '<option value="">Все источники</option>';
    playlistSources.forEach(src => {
        if (src.in_config === false) return;
        const opt = document.createElement('option');
        opt.value = src.name;
        opt.textContent = src.name;
        sel.appendChild(opt);
    });
    sel.value = '';

    document.getElementById('playlistSearchModal').style.display = 'block';
    setTimeout(() => document.getElementById('playlistSearchQuery').focus(), 100);
}

function closePlaylistSearch() {
    document.getElementById('playlistSearchModal').style.display = 'none';
    currentPlaylistSearchStreamIndex = null;
    currentPlaylistSearchResults = [];
    if (_playlistSearchTimer) {
        clearTimeout(_playlistSearchTimer);
        _playlistSearchTimer = null;
    }
}

function onPlaylistSearchKeyup(event) {
    if (event.key === 'Escape') {
        closePlaylistSearch();
        return;
    }
    if (_playlistSearchTimer) clearTimeout(_playlistSearchTimer);
    _playlistSearchTimer = setTimeout(doPlaylistSearch, 300);
}

function doPlaylistSearch() {
    const q = document.getElementById('playlistSearchQuery').value.trim();
    const source = document.getElementById('playlistSearchSourceFilter').value;

    if (q.length < 2) {
        document.getElementById('playlistSearchResults').innerHTML =
            '<div style="padding: 20px; text-align: center; color: var(--text-muted);">Минимум 2 символа</div>';
        return;
    }

    const url = '/playlists/search?q=' + encodeURIComponent(q)
        + (source ? '&source=' + encodeURIComponent(source) : '')
        + '&limit=150';

    fetch(url)
        .then(r => r.json())
        .then(data => {
            if (!data.success) {
                document.getElementById('playlistSearchResults').innerHTML =
                    '<div style="padding: 20px; text-align: center; color: #ef4444;">Ошибка: ' + (data.error || '') + '</div>';
                return;
            }
            currentPlaylistSearchResults = data.results || [];
            renderPlaylistSearchResults();
        })
        .catch(err => {
            console.error(err);
            document.getElementById('playlistSearchResults').innerHTML =
                '<div style="padding: 20px; text-align: center; color: #ef4444;">Сетевая ошибка</div>';
        });
}

function renderPlaylistSearchResults() {
    const container = document.getElementById('playlistSearchResults');
    if (currentPlaylistSearchResults.length === 0) {
        container.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-muted);">Ничего не найдено</div>';
        return;
    }
    let html = '';
    currentPlaylistSearchResults.forEach((r, idx) => {
        html += `<div class="playlist-result-item" data-idx="${idx}" onclick="pickPlaylistResult(${idx})"
                      style="padding: 10px 14px; border-bottom: 1px solid var(--border-color); cursor: pointer;"
                      onmouseover="this.style.background='#27272a'" onmouseout="this.style.background=''">
            <div style="font-weight: 600; font-size: 14px;">${escapeHtml(r.name)}</div>
            <div style="font-size: 11px; color: #888; margin-top: 2px;">источник: ${escapeHtml(r.source || '?')}</div>
            <div class="playlist-result-status" style="font-size: 11px; margin-top: 4px;"></div>
        </div>`;
    });
    container.innerHTML = html;
}

function pickPlaylistResult(idx) {
    const r = currentPlaylistSearchResults[idx];
    if (!r) return;

    const item = document.querySelector(`.playlist-result-item[data-idx="${idx}"]`);
    const statusEl = item ? item.querySelector('.playlist-result-status') : null;
    if (statusEl) {
        statusEl.textContent = '⏳ проверка...';
        statusEl.style.color = 'var(--text-muted)';
    }

    fetch('/playlists/check', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ url_payload: r.url_payload })
    })
    .then(resp => resp.json())
    .then(data => {
        if (!data.success) {
            if (statusEl) {
                statusEl.textContent = '❌ ' + (data.error || 'ошибка');
                statusEl.style.color = '#ef4444';
            }
            return;
        }
        if (data.ok) {
            if (statusEl) {
                statusEl.textContent = '✅ ' + (data.detail || 'OK') + ' — вставлено';
                statusEl.style.color = '#10b981';
            }
            applyPlaylistResult(r.url_payload);
        } else {
            if (statusEl) {
                statusEl.textContent = '❌ не отвечает: ' + (data.detail || '?');
                statusEl.style.color = '#ef4444';
            }
        }
    })
    .catch(err => {
        console.error(err);
        if (statusEl) {
            statusEl.textContent = '❌ сетевая ошибка';
            statusEl.style.color = '#ef4444';
        }
    });
}

function applyPlaylistResult(urlPayload) {
    if (currentPlaylistSearchStreamIndex === null) return;
    const item = document.querySelector(`.stream-item[data-index="${currentPlaylistSearchStreamIndex}"]`);
    if (!item) {
        showToast('Поток не найден');
        return;
    }
    const urlInput = item.querySelector('.stream-url');
    if (!urlInput) {
        showToast('Поле URL не найдено');
        return;
    }
    urlInput.value = urlPayload;
    showToast('URL подставлен. Проверьте резолвер и сохраните поток.');
    closePlaylistSearch();
}

function escapeHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
