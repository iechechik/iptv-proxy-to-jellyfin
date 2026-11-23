// Вход в YouTube: живое окно браузера сервиса + ввод с клавиатуры и мыши.
(function () {
  const $ = (id) => document.getElementById(id);
  let pollTimer = null;
  let busy = false;

  function toast(msg, ok) {
    const el = $('toast');
    if (!el) { console.log(msg); return; }
    el.textContent = msg;
    el.className = ok === false ? 'show error' : 'show';
    clearTimeout(window.__ytLoginToast);
    window.__ytLoginToast = setTimeout(() => { el.className = ''; }, 6000);
  }

  async function api(path, body) {
    const res = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 204) return { empty: true };
    try { return await res.json(); } catch (e) { return { success: false, detail: 'HTTP ' + res.status }; }
  }

  async function sendInput(payload) {
    if (busy) return;
    busy = true;
    try { await api('/youtube/login/input', payload); } finally { busy = false; }
  }

  function showScreen() {
    const img = $('ytLoginScreen');
    if (!img) return;
    const probe = new Image();
    probe.onload = () => { img.src = probe.src; };
    probe.src = '/youtube/login/screen?t=' + Date.now();
  }

  function stepOf(url) {
    const u = String(url || '');
    if (/accounts\.google\.com\/.*challenge\/pwd/.test(u)) return 'Шаг: пароль — введите пароль от аккаунта';
    if (/accounts\.google\.com\/.*challenge\/(ipp|totp|dp|sk|sms|idv)/.test(u)) return 'Шаг: подтверждение — введите код из SMS';
    if (/accounts\.google\.com\/.*challenge\/bfuid|speedbump/.test(u)) return 'Шаг: Google просит дополнительно подтвердить вход';
    if (/sorry|challenge\/captcha/.test(u)) return 'Шаг: капча — щёлкните по картинке в окне';
    if (/accounts\.google\.com\/.*signin\/identifier/.test(u)) return 'Шаг: логин — введите email или телефон';
    if (/youtube\.com/.test(u) && !/accounts\.google/.test(u)) return 'Вход выполнен, забираю cookies…';
    return 'Ожидание страницы Google…';
  }

  function setStatus(text) {
    const el = $('ytLoginStatus');
    if (el) el.textContent = text || '';
  }

  async function tick() {
    if (!$('ytLoginModal') || $('ytLoginModal').style.display === 'none') return;
    showScreen();
    const st = await api('/youtube/login/status');
    if (st.state === 'running') {
      const age = st.shot_at ? Math.max(0, Math.round(Date.now() / 1000 - st.shot_at)) : null;
      const fresh = age == null ? '' : (age > 6 ? '  ⚠ кадр не обновляется ' + age + ' с' : '  (кадр ' + age + ' с)');
      setStatus(stepOf(st.url) + fresh);
    } else if (st.state === 'done') {
      setStatus(st.detail || 'Готово');
      toast('Вход выполнен — cookies сохранены', true);
      closeModal();
      if (window.loadYtCookies) window.loadYtCookies();
      return;
    } else if (st.state === 'error') {
      setStatus('Сессия завершилась: ' + (st.detail || 'неизвестно') +
                '  — нажмите «Войти в YouTube», чтобы начать заново.');
      toast('Вход не удался: ' + (st.detail || ''), false);
      return;
    } else if (st.state === 'idle') {
      setStatus(st.detail || 'Сессия закрыта');
      return;
    }
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(tick, 700);
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  window.ytLoginOpen = async function () {
    const profile = ($('ytCookieName') && $('ytCookieName').value || 'основной').trim() || 'основной';
    const modal = $('ytLoginModal');
    modal.style.display = 'flex';
    setStatus('Запускаю браузер сервиса…');
    const d = await api('/youtube/login/start', { profile });
    if (!d.success) {
      setStatus('Не удалось начать: ' + (d.detail || ''));
      return;
    }
    const kb = $('ytLoginKeys');
    if (kb) kb.focus();
    startPolling();
  };

  function closeModal() {
    const modal = $('ytLoginModal');
    if (modal) modal.style.display = 'none';
    stopPolling();
    api('/youtube/login/stop');
  }

  window.ytLoginClose = closeModal;

  document.addEventListener('DOMContentLoaded', () => {
    const img = $('ytLoginScreen');
    if (img) {
      img.addEventListener('click', (e) => {
        const r = img.getBoundingClientRect();
        const x = (e.clientX - r.left) * (img.naturalWidth / r.width);
        const y = (e.clientY - r.top) * (img.naturalHeight / r.height);
        sendInput({ type: 'click', x: Math.round(x), y: Math.round(y) });
        const kb = $('ytLoginKeys');
        if (kb) kb.focus();
      });
    }
    const kb = $('ytLoginKeys');
    if (kb) {
      kb.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === 'Tab' || e.key === 'Backspace' || e.key === 'Escape') {
          e.preventDefault();
          sendInput({ type: 'key', key: e.key });
        } else if (e.key && e.key.length === 1) {
          e.preventDefault();
          sendInput({ type: 'type', text: e.key });
        }
      });
      kb.addEventListener('paste', (e) => {
        e.preventDefault();
        const text = (e.clipboardData || window.clipboardData).getData('text');
        if (text) sendInput({ type: 'type', text });
      });
    }
  });
})();
