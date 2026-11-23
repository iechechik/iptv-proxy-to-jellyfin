// Cookies YouTube: профили, проверка, выбор активного.
(function () {
  const $ = (id) => document.getElementById(id);

  function toast(msg, ok) {
    const el = $('toast');
    if (!el) { console.log(msg); return; }
    el.textContent = msg;
    el.className = ok === false ? 'show error' : 'show';
    clearTimeout(window.__ytToastTimer);
    window.__ytToastTimer = setTimeout(() => { el.className = ''; }, 5000);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function when(ts) {
    if (!ts) return '—';
    try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return '—'; }
  }

  async function api(path, body) {
    const res = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    try { return await res.json(); } catch (e) { return { success: false, detail: 'HTTP ' + res.status }; }
  }

  async function load() {
    const box = $('ytCookiesContainer');
    if (!box) return;
    try {
      const d = await api('/youtube/cookies');
      const profiles = d.profiles || [];
      if (!profiles.length) {
        box.innerHTML = '<div style="padding:14px;color:var(--text-muted)">Профилей пока нет — вставьте cookies ниже.</div>';
        return;
      }
      const rows = profiles.map((p) => {
        const status = p.check_ok === true
          ? '✅ рабочие'
          : p.check_ok === false
            ? '❌ ' + esc(p.check_detail || 'не приняты')
            : '— не проверялся';
        const size = p.size ? Math.round(p.size / 1024) + ' КБ' : '—';
        const actions = `<button onclick="ytCheck('${esc(p.name)}')">Проверить</button> ` +
          (p.active ? '' : `<button class="btn-primary" onclick="ytSelect('${esc(p.name)}')">Сделать активным</button> `) +
          `<button class="btn-danger" onclick="ytDelete('${esc(p.name)}')">Удалить</button>`;
        return `<tr>
          <td>${esc(p.name)}${p.active ? ' <b>(активный)</b>' : ''}</td>
          <td>${status}</td>
          <td>${when(p.check_at)}</td>
          <td>${size}</td>
          <td style="white-space:nowrap">${actions}</td>
        </tr>`;
      }).join('');
      box.innerHTML = `<table class="table"><thead><tr>
          <th>Профиль</th><th>Статус</th><th>Проверен</th><th>Размер</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table>`;
    } catch (e) {
      box.innerHTML = '<div style="padding:14px">Ошибка загрузки: ' + esc(e.message) + '</div>';
    }
  }

  window.loadYtCookies = load;

  window.ytCheck = async (name) => {
    toast('Проверяю cookies (до минуты)…', true);
    const d = await api('/youtube/cookies/check', { name });
    toast(d.success ? 'Cookies рабочие' : (d.detail || 'Cookies не приняты'), d.success);
    load();
  };

  window.ytSelect = async (name) => {
    const d = await api('/youtube/cookies/select', { name });
    toast(d.success ? 'Активный профиль: ' + name : (d.detail || 'Ошибка'), d.success);
    load();
  };

  window.ytDelete = async (name) => {
    if (!confirm('Удалить профиль ' + name + '?')) return;
    const d = await api('/youtube/cookies/delete', { name });
    toast(d.success ? 'Удалено' : (d.detail || 'Ошибка'), d.success);
    load();
  };

  window.ytSave = async () => {
    const name = ($('ytCookieName').value || '').trim();
    const text = $('ytCookieText').value || '';
    if (!name || text.trim().length < 20) {
      toast('Нужны имя профиля и текст cookies', false);
      return;
    }
    const btn = $('ytSaveBtn');
    btn.disabled = true;
    btn.textContent = 'Проверяю… (до минуты)';
    try {
      const d = await api('/youtube/cookies/save', { name, text });
      toast(d.success ? 'Сохранено и работает' : (d.detail || 'Cookies не приняты'), d.success);
      if (d.success) $('ytCookieText').value = '';
    } catch (e) {
      toast('Ошибка: ' + e.message, false);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Сохранить и проверить';
      load();
    }
  };

  // youtube-account-ui-v1: вкладка появляется, только если роутер включён.
  document.addEventListener('DOMContentLoaded', async () => {
    try {
      const r = await fetch('/youtube/cookies');
      if (!r.ok) return;                      // 404 — аккаунтная часть выключена
      const btn = document.getElementById('tab-btn-yt-cookies');
      if (btn) btn.style.display = '';
      load();
    } catch (e) { /* тихо */ }
  });
  const tabBtn = document.querySelector('[onclick*="yt-cookies"]');
  if (tabBtn) tabBtn.addEventListener('click', load);
})();
