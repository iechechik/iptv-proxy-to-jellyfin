let epgSources = [];

function switchTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-link').forEach(el => el.classList.remove('active'));
    document.getElementById('tab-' + tabId).classList.add('active');
    document.querySelector(`.tab-link[onclick="switchTab('${tabId}')"]`).classList.add('active');

    // Запоминаем активную вкладку, чтобы F5 не сбрасывал на первую.
    try { sessionStorage.setItem('activeTab', tabId); } catch (e) {}

    if (tabId === 'epg-sources') {
        stopLogAutoRefresh();
        loadEpgSources();
    } else if (tabId === 'playlist-sources') {
        stopLogAutoRefresh();
        if (typeof loadPlaylistSources === 'function') loadPlaylistSources();
    } else if (tabId === 'logs') {
        startLogAutoRefresh();
    } else {
        stopLogAutoRefresh();
    }
}

function loadEpgSources() {
    const container = document.getElementById('epgSourcesTableContainer');
    container.innerHTML = 'Загрузка...';
    fetch('/epg/sources')
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                epgSources = data.sources || [];
                renderEpgSourcesTable(epgSources);
            } else {
                container.innerHTML = 'Ошибка: ' + (data.error || '');
            }
        })
        .catch(err => {
            console.error(err);
            container.innerHTML = 'Сетевая ошибка при загрузке источников';
        });
}

function renderEpgSourcesTable(sources) {
    const container = document.getElementById('epgSourcesTableContainer');
    if (sources.length === 0) {
        container.innerHTML = '<p style="color: var(--text-muted);">Источники не найдены. Нажмите «+ Добавить источник».</p>';
        return;
    }
    let html = `<div style="overflow-x: auto;"><table style="width: 100%; table-layout: fixed;"><thead><tr>
    <th style="width: 100px;">Имя</th>
    <th style="width: 200px;">URL</th>
    <th style="width: 60px;">Интервал</th>
    <th style="width: 140px;">Фильтр</th>
    <th style="width: 80px;">Статус</th>
    <th style="width: 80px;">Обновлено</th>
    <th style="width: 240px; white-space: nowrap;">Действия</th>
    </tr></thead><tbody>`;
    sources.forEach((src, index) => {
        const mode = src.filter ? src.filter.mode : 'all';
        const ids = src.filter ? (src.filter.ids || []).join(', ') : '';
        const names = src.filter ? (src.filter.names || []).join(', ') : '';
        const filterInfo = mode === 'all' ? 'все' : `${mode}: ids=[${ids}] names=[${names}]`;
        const disabled = src.disable ? '❌ отключен' : '✅ активен';
        let updatedText = '—';
        if (src.updated_at) {
            const dt = new Date(src.updated_at * 1000);
            const dd = String(dt.getDate()).padStart(2, '0');
            const mm = String(dt.getMonth() + 1).padStart(2, '0');
            const hh = String(dt.getHours()).padStart(2, '0');
            const min = String(dt.getMinutes()).padStart(2, '0');
            updatedText = `${dd}.${mm} ${hh}:${min}`;
        }
        html += `<tr>
            <td>${src.name || src.url}</td>
            <td style="max-width: 250px; overflow: hidden; text-overflow: ellipsis;" title="${src.url}">${src.url}</td>
            <td>${Math.round(src.interval / 3600)}ч</td>
            <td>${filterInfo}</td>
            <td>${disabled}</td>
            <td>${updatedText}</td>
            <td style="white-space: nowrap; overflow: visible;">
                <button class="btn-sm btn-primary" onclick="openEpgSourceModal(${index})">✏️</button>
                <button class="btn-sm ${src.disable ? 'btn-success' : 'btn-secondary'}" onclick="toggleEpgSource(${index})" title="${src.disable ? 'Включить' : 'Отключить'}">${src.disable ? '✓' : '✕'}</button>
                <button class="btn-sm" onclick="updateEpgSource(${index})" title="Обновить источник">🔄</button>
                <button class="btn-sm btn-danger" onclick="deleteEpgSource(${index})">🗑️</button>
            </td>
        </tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

function openEpgSourceModal(index = null) {
    const modal = document.getElementById('epgSourceModal');
    const title = document.getElementById('epgSourceModalTitle');
    const nameField = document.getElementById('epgSourceName');

    if (index !== null && epgSources[index]) {
        const src = epgSources[index];
        document.getElementById('editEpgSourceIndex').value = index;
        title.textContent = 'Редактировать источник';
        nameField.value = src.name || '';
        nameField.readOnly = true; // имя не редактируется после создания
        document.getElementById('epgSourceUrl').value = src.url || '';
        document.getElementById('epgSourceInterval').value = (src.interval / 3600).toString() + 'h';
        const mode = src.filter ? src.filter.mode : 'all';
        document.getElementById('epgSourceMode').value = mode;
        document.getElementById('epgSourceMatch').value = src.filter ? (src.filter.match || 'any') : 'any';
        document.getElementById('epgSourceIds').value = src.filter ? (src.filter.ids || []).join(', ') : '';
        document.getElementById('epgSourceNames').value = src.filter ? (src.filter.names || []).join(', ') : '';
        document.getElementById('epgSourceDisable').checked = !!src.disable;
    } else {
        document.getElementById('editEpgSourceIndex').value = '';
        title.textContent = 'Добавить EPG источник';
        nameField.value = '';
        nameField.readOnly = false; // при добавлении имя можно вводить
        document.getElementById('epgSourceUrl').value = '';
        document.getElementById('epgSourceInterval').value = '24h';
        document.getElementById('epgSourceMode').value = 'all';
        document.getElementById('epgSourceMatch').value = 'any';
        document.getElementById('epgSourceIds').value = '';
        document.getElementById('epgSourceNames').value = '';
        document.getElementById('epgSourceDisable').checked = false;
    }
    toggleFilterMode();
    modal.style.display = 'block';
}

function closeEpgSourceModal() { document.getElementById('epgSourceModal').style.display = 'none'; }

function toggleFilterMode() {
    const mode = document.getElementById('epgSourceMode').value;
    document.getElementById('epgSourceIds').disabled = (mode === 'all');
    document.getElementById('epgSourceNames').disabled = (mode === 'all');
}

function toggleEpgSource(index) {
    const src = epgSources[index];
    if (!src) return;
    src.disable = !src.disable;
    showGlobalSpinner('Сохранение...');
    fetch('/epg/sources', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ sources: epgSources })
    })
    .then(r => r.json())
    .then(res => {
        hideGlobalSpinner();
        if (res.success) {
            epgSources = res.sources || epgSources;
            renderEpgSourcesTable(epgSources);
            showToast('Статус источника изменён');
        } else {
            showToast('Ошибка: ' + (res.error || ''));
        }
    })
    .catch(err => { hideGlobalSpinner(); console.error(err); showToast('Сетевая ошибка'); });
}

function saveEpgSource() {
    const index = document.getElementById('editEpgSourceIndex').value;
    const name = document.getElementById('epgSourceName').value.trim();
    const url = document.getElementById('epgSourceUrl').value.trim();
    if (!name) { showToast('Имя источника обязательно!'); return; }
    if (!url) { showToast('URL обязателен'); return; }

    const source = {
        name: name,
        url: url,
        interval: document.getElementById('epgSourceInterval').value,
        filter: {
            mode: document.getElementById('epgSourceMode').value,
            match: document.getElementById('epgSourceMatch').value,
            ids: document.getElementById('epgSourceIds').value.split(',').map(s => s.trim()).filter(Boolean),
            names: document.getElementById('epgSourceNames').value.split(',').map(s => s.trim()).filter(Boolean)
        },
        disable: document.getElementById('epgSourceDisable').checked
    };

    let updatedSources;
    if (index === '') {
        // проверяем уникальность имени
        if (epgSources.some(s => s.name === name)) {
            showToast('Источник с таким именем уже существует');
            return;
        }
        updatedSources = [...epgSources, source];
    } else {
        updatedSources = [...epgSources];
        updatedSources[parseInt(index)] = source;
    }
    showGlobalSpinner('Сохранение источников...');
    fetch('/epg/sources', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ sources: updatedSources })
    })
    .then(r => r.json())
    .then(res => {
        hideGlobalSpinner();
        if (res.success) {
            epgSources = res.sources || updatedSources;
            renderEpgSourcesTable(epgSources);
            closeEpgSourceModal();
            showToast('Источники сохранены');
        } else {
            showToast('Ошибка: ' + (res.error || 'не удалось сохранить'));
        }
    })
    .catch(err => { hideGlobalSpinner(); console.error(err); showToast('Сетевая ошибка при сохранении'); });
}

function deleteEpgSource(index) {
    if (!confirm('Удалить источник?')) return;
    const updatedSources = [...epgSources];
    updatedSources.splice(index, 1);
    showGlobalSpinner('Удаление источника...');
    fetch('/epg/sources', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ sources: updatedSources })
    })
    .then(r => r.json())
    .then(res => {
        hideGlobalSpinner();
        if (res.success) {
            epgSources = res.sources || updatedSources;
            renderEpgSourcesTable(epgSources);
            showToast('Источник удалён');
        } else {
            showToast('Ошибка: ' + (res.error || ''));
        }
    })
    .catch(err => { hideGlobalSpinner(); console.error(err); showToast('Сетевая ошибка'); });
}

function updateEpgSource(index) {
    const src = epgSources[index];
    if (!src) return;
    if (!confirm(`Обновить источник "${src.name || src.url}"?`)) return;

    showGlobalSpinner(`Обновление источника ${src.name || src.url}...`);
    fetch('/epg/update-source/' + encodeURIComponent(src.name || src.url), { method: 'POST' })
        .then(r => r.json())
        .then(res => {
            hideGlobalSpinner();
            if (res.success) {
                showToast('Обновление запущено');
            } else {
                showToast('Ошибка: ' + (res.error || ''));
            }
        })
        .catch(err => {
            hideGlobalSpinner();
            console.error(err);
            showToast('Сетевая ошибка');
        });
}

function updateAllEpgSources() {
    if (!confirm('Запустить обновление всех EPG источников?')) return;
    showGlobalSpinner('Обновление всех EPG источников...');
    fetch('/epg/update-all', { method: 'POST' })
        .then(r => r.json())
        .then(res => {
            hideGlobalSpinner();
            if (res.success) {
                showToast('Обновление всех источников запущено');
            } else {
                showToast('Ошибка: ' + (res.error || 'не удалось запустить'));
            }
        })
        .catch(err => {
            hideGlobalSpinner();
            console.error(err);
            showToast('Сетевая ошибка');
        });
}
