// Глобальные переменные
let eventSource = null;
let globalSpinner = null;
let logIntervalId = null;

function showGlobalSpinner(message) {
    if (globalSpinner) {
        globalSpinner.querySelector('.spinner-text').innerHTML = message || 'Выполняется...';
        globalSpinner.style.display = 'flex';
        return;
    }
    const spinner = document.createElement('div');
    spinner.id = 'global-spinner';
    spinner.style.position = 'fixed';
    spinner.style.top = '0';
    spinner.style.left = '0';
    spinner.style.width = '100%';
    spinner.style.height = '100%';
    spinner.style.background = 'rgba(0,0,0,0.4)';
    spinner.style.display = 'flex';
    spinner.style.justifyContent = 'center';
    spinner.style.alignItems = 'center';
    spinner.style.zIndex = '9999';
    spinner.style.backdropFilter = 'blur(2px)';
    spinner.innerHTML = `
        <div style="background: white; padding: 20px 30px; border-radius: 12px; text-align: center; box-shadow: 0 4px 20px rgba(0,0,0,0.3); min-width: 280px;">
            <div style="border: 4px solid #ccc; border-top-color: #10b981; width: 40px; height: 40px; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 10px;"></div>
            <div class="spinner-text" style="font-size: 14px; color: #333; line-height: 1.4;">${message || 'Выполняется...'}</div>
        </div>
        <style>@keyframes spin { to { transform: rotate(360deg); } }</style>
    `;
    document.body.appendChild(spinner);
    globalSpinner = spinner;
}

function hideGlobalSpinner() {
    if (globalSpinner) globalSpinner.style.display = 'none';
}

function showToast(msg) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = msg;
    toast.className = "show";
    setTimeout(() => { toast.className = toast.className.replace("show", ""); }, 3000);
}

function restartContainer() {
    if (!confirm('Перезапустить контейнер iptv-proxy?\n\nСтраница вернётся через ~10 секунд.')) return;
    showGlobalSpinner('Рестарт контейнера...<br>Страница вернётся через ~10 секунд.');

    fetch('/config/restart', { method: 'POST' })
        .then(r => r.json())
        .then(() => {
            // Ждём, пока контейнер поднимется: поллим /status.
            let attempts = 0;
            const maxAttempts = 30;
            const timer = setInterval(() => {
                attempts++;
                fetch('/status', { cache: 'no-store' })
                    .then(r => {
                        if (r.ok) {
                            clearInterval(timer);
                            hideGlobalSpinner();
                            location.reload();
                        }
                    })
                    .catch(() => { /* ещё не поднялся — продолжаем */ });
                if (attempts >= maxAttempts) {
                    clearInterval(timer);
                    hideGlobalSpinner();
                    showToast('Контейнер не поднялся за 60 сек. Проверьте логи.');
                }
            }, 2000);
        })
        .catch(err => {
            hideGlobalSpinner();
            console.error(err);
            showToast('Не удалось запустить рестарт');
        });
}

function startEpgUpdate() {
    showGlobalSpinner('Запуск обновления EPG...');
    fetch('/epg/update-source', { method: 'GET' })
        .then(r => r.json())
        .then(res => {
            if (!res.success) {
                hideGlobalSpinner();
                showToast('Ошибка запуска обновления: ' + (res.error || ''));
            }
        })
        .catch(err => {
            hideGlobalSpinner();
            showToast('Сетевая ошибка при запуске обновления');
        });
}

function checkStatus() {
    const el = document.getElementById('status');
    if (!el) return;
    el.textContent = 'Загрузка статуса...';
    fetch('/status')
        .then(r => {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(data => {
            el.innerHTML = `
                <span style="color: ${data.epg_ready ? '#10b981' : '#ef4444'}">EPG: ${data.epg_ready ? 'Готов' : 'Отсутствует'}</span> |
                Всего каналов: <b>${data.total_channels}</b> |
                Сопоставлено: <b>${data.matched_channels}</b>
            `;
        })
        .catch(err => {
            console.error('Ошибка статуса:', err);
            el.textContent = 'Ошибка загрузки статуса';
        });
}

function connectSSE() {
    console.log('Попытка подключения SSE...');
    if (eventSource) eventSource.close();
    try {
        eventSource = new EventSource('/events');
        console.log('SSE объект создан');

        eventSource.addEventListener('open', () => console.log('SSE соединение установлено!'));

        eventSource.addEventListener('status-update', (e) => {
            try {
                const data = JSON.parse(e.data);
                const channelName = data.channel;
                const status = data.status;
                const row = getRowByChannelName(channelName);
                if (!row) return;
                const statusCell = row.querySelector('td:nth-child(2)');
                if (!statusCell) return;
                if (status.last_check_time) {
                    const dt = new Date(status.last_check_time * 1000);
                    const timeStr = dt.toLocaleString('ru', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'});
                    const icon = status.last_check_success ? '✅' : '❌';
                    const detail = status.last_check_detail || '';
                    statusCell.innerHTML = `<span title="${detail}">${icon} ${timeStr}</span>`;
                } else {
                    statusCell.innerHTML = '<span style="color: #555;">—</span>';
                }
            } catch (err) { console.error('Ошибка обработки SSE события:', err); }
        });

        eventSource.addEventListener('epg-progress', (e) => {
            try {
                const data = JSON.parse(e.data);
                const extra = data.extra || {};
                showGlobalSpinner(extra.message || 'Обновление EPG...');
            } catch (err) { console.error('Ошибка обработки epg-progress:', err); }
        });

        eventSource.addEventListener('epg-complete', (e) => {
            hideGlobalSpinner();
            try {
                const data = JSON.parse(e.data);
                const extra = data.extra || {};
                showToast(extra.message || (extra.success !== false ? 'EPG успешно обновлён' : 'Ошибка обновления EPG'));
                if (extra.success !== false) {
                    setTimeout(() => { checkStatus(); if (typeof loadChannelList === 'function') loadChannelList(); }, 500);
                }
            } catch (err) { console.error('Ошибка обработки epg-complete:', err); }
        });

        eventSource.addEventListener('healthcheck-progress', (e) => {
            try {
                const data = JSON.parse(e.data);
                const extra = data.extra || {};
                const progress = extra.progress || 0;
                const total = extra.total || 0;
                const channelName = extra.name || '';
                const percent = total ? Math.round((progress / total) * 100) : 0;
                showGlobalSpinner(`Проверка каналов (${progress}/${total}) — ${percent}%\nТестируется: <b>${channelName}</b>`);
                const btn = document.getElementById('checkAllBtn');
                if (btn) btn.textContent = `⏳ Проверка... (${progress}/${total}) ${percent}%`;
            } catch (err) { console.error('Ошибка обработки healthcheck-progress:', err); }
        });

        eventSource.addEventListener('healthcheck-complete', (e) => {
            hideGlobalSpinner();
            try {
                const btn = document.getElementById('checkAllBtn');
                if (btn) { btn.disabled = false; btn.textContent = '🔍 Проверить все каналы'; }
                showToast('Проверка всех каналов завершена! Обновляем страницу...');
                setTimeout(() => location.reload(), 1500);
            } catch (err) { console.error('Ошибка обработки healthcheck-complete:', err); }
        });

        eventSource.onerror = (e) => {
            console.error('SSE ошибка, переподключение через 5 секунд...', e);
            eventSource.close();
            // Debounce: без него несколько onerror подряд (рестарт uvicorn)
            // плодят параллельные EventSource — лишние запросы и конфликты.
            if (connectSSE._reconnectTimer) {
                return;
            }
            connectSSE._reconnectTimer = setTimeout(() => {
                connectSSE._reconnectTimer = null;
                connectSSE();
            }, 5000);
        };
    } catch (err) { console.error('Ошибка создания SSE:', err); }
}

function loadLogs() {
    fetch('/logs/data')
        .then(response => response.json())
        .then(data => {
            const container = document.getElementById('log-container');
            if (!container) return;
            const logs = data.logs || [];
            container.innerHTML = logs.map(line => {
                let escaped = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
                if (escaped.includes("ERROR") || escaped.includes("Traceback") || escaped.includes("Exception")) {
                    return `<span style="color: var(--danger); font-weight: bold;">${escaped}</span>`;
                } else if (escaped.includes("WARNING")) {
                    return `<span style="color: #d29922;">${escaped}</span>`;
                } else if (escaped.includes("INFO")) {
                    return `<span style="color: #7ee787;">${escaped}</span>`;
                }
                return escaped;
            }).join('\n');
            if (document.getElementById('autoscroll')?.checked) {
                container.scrollTop = container.scrollHeight;
            }
        })
        .catch(err => console.error('Ошибка загрузки логов:', err));
}

function clearLogsHistory() {
    if (!confirm('Очистить локальный буфер логов?')) return;
    fetch('/logs/clear')
        .then(() => loadLogs())
        .catch(err => console.error('Ошибка очистки логов:', err));
}

function stopLogAutoRefresh() {
    clearInterval(logIntervalId);
    logIntervalId = null;
}

function startLogAutoRefresh() {
    // Всегда сразу показываем логи
    loadLogs();

    // Останавливаем предыдущий интервал, если был
    if (logIntervalId) {
        clearInterval(logIntervalId);
        logIntervalId = null;
    }

    // Запускаем интервал только если включено автообновление
    const autorefresh = document.getElementById('autorefresh');
    if (autorefresh && autorefresh.checked) {
        logIntervalId = setInterval(loadLogs, 2000);
    }
}
