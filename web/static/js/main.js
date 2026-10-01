document.addEventListener('DOMContentLoaded', () => {
    checkStatus();
    connectSSE();

    // Восстанавливаем активную вкладку после F5.
    // Сначала показываем body — оно было скрыто скриптом в index.html,
    // чтобы не мигало первой вкладкой. Восстанавливаем вкладку и
    // показываем всё в одном кадре — мерцания нет.
    try {
        const saved = sessionStorage.getItem('activeTab');
        if (saved && document.getElementById('tab-' + saved)) {
            switchTab(saved);
        }
    } catch (e) {}
    document.body.style.visibility = 'visible';

    const logsContainer = document.getElementById('logsContainer') || document.getElementById('logs');
    if (logsContainer) {
        loadLogs();
    }
});

document.getElementById('autorefresh')?.addEventListener('change', (e) => {
    if (e.target.checked) {
        clearInterval(logIntervalId);
        logIntervalId = setInterval(loadLogs, 2000);
        loadLogs();
    } else {
        clearInterval(logIntervalId);
    }
});

// Инициализация логов
const logContainer = document.getElementById('log-container');
if (logContainer) {
    const autorefresh = document.getElementById('autorefresh');
    if (autorefresh) autorefresh.checked = true;
    loadLogs();
}
