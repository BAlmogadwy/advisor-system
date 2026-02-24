/**
 * Toast / Notify — Advisor Portal
 * Provides notify.success(), notify.error(), notify.info(), notify.warning()
 */
const notify = (() => {
  let c = document.getElementById('toast-container');
  if (!c) {
    c = document.createElement('div');
    c.id = 'toast-container';
    c.setAttribute('aria-live', 'polite');
    c.setAttribute('role', 'status');
    document.body.appendChild(c);
  }
  const I = {
    success: '<span class="i" style="width:16px;height:16px" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></span>',
    error:   '<span class="i" style="width:16px;height:16px" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg></span>',
    info:    '<span class="i" style="width:16px;height:16px" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg></span>',
    warning: '<span class="i" style="width:16px;height:16px" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>',
  };
  const D = { success: 3200, error: 5000, info: 4000, warning: 4500 };
  function show(title, kind = 'success', sub = '') {
    const dur = D[kind] || 3200, t = document.createElement('div');
    t.className = `ux-toast ${kind}`;
    t.setAttribute('role', 'alert');
    t.innerHTML =
      `<div class="ux-toast-icon">${I[kind] || I.info}</div>` +
      `<div class="ux-toast-body"><div class="ux-toast-title">${title}</div>${sub ? `<div class="ux-toast-sub">${sub}</div>` : ''}</div>` +
      `<button class="ux-toast-close" aria-label="Dismiss"><span class="i" style="width:12px;height:12px" aria-hidden="true"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></span></button>` +
      `<div class="ux-toast-progress" style="animation-duration:${dur}ms;"></div>`;
    c.appendChild(t);
    function dismiss() { t.classList.add('removing'); setTimeout(() => t.remove(), 250); }
    t.querySelector('.ux-toast-close').addEventListener('click', dismiss);
    setTimeout(dismiss, dur);
  }
  return {
    success: (m, s) => show(m, 'success', s),
    error:   (m, s) => show(m, 'error', s),
    info:    (m, s) => show(m, 'info', s),
    warning: (m, s) => show(m, 'warning', s),
  };
})();
