document.addEventListener('DOMContentLoaded', () => {
    checkStatus();
    connectSSE();

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
