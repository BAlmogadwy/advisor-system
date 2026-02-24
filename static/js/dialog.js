/**
 * Dialog system — Advisor Portal
 * Provides dlg.confirm() and dlg.prompt()
 * Includes focus trapping and focus restoration.
 */
const dlg = (() => {
  const ICONS = {
    danger:  '<span class="i" style="color:var(--err-t,#C03030)" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>',
    info:    '<span class="i" style="color:var(--teal,#0A8E6E)" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg></span>',
    warning: '<span class="i" style="color:var(--warn-t,#9A6A08)" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>',
  };

  /* ── Focus trap helper ── */
  function trapFocus(container) {
    const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
    function handler(e) {
      if (e.key !== 'Tab') return;
      const els = Array.from(container.querySelectorAll(FOCUSABLE));
      if (!els.length) return;
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    container.addEventListener('keydown', handler);
    return () => container.removeEventListener('keydown', handler);
  }

  function build(opts) {
    const bd = document.createElement('div');
    bd.className = 'dlg-backdrop';
    bd.setAttribute('role', 'dialog');
    bd.setAttribute('aria-modal', 'true');
    const icon = opts.icon && ICONS[opts.icon] ? ICONS[opts.icon] : '';
    let inputHtml = '';
    if (opts.inputLabel) {
      const inputId = 'dlg-input-field';
      inputHtml =
        `<label for="${inputId}" class="dlg-input-label">${opts.inputLabel}</label>` +
        `<input id="${inputId}" class="dlg-input" type="${opts.inputType || 'text'}" placeholder="${opts.inputPlaceholder || ''}" autocomplete="off">` +
        (opts.inputHint ? `<div class="dlg-input-hint">${opts.inputHint}</div>` : '');
    }
    const titleId = 'dlg-title-text';
    bd.setAttribute('aria-labelledby', titleId);
    bd.innerHTML =
      `<div class="dlg-card">` +
        (icon ? `<div class="dlg-icon">${icon}</div>` : '') +
        `<div id="${titleId}" class="dlg-title">${opts.title || ''}</div>` +
        `<div class="dlg-body">${opts.body || ''}</div>` +
        inputHtml +
        `<div class="dlg-actions">` +
          `<button class="btn-cancel">${opts.cancelLabel || 'Cancel'}</button>` +
          `<button class="btn-confirm ${opts.confirmClass || ''}">${opts.confirmLabel || 'Confirm'}</button>` +
        `</div>` +
      `</div>`;
    return bd;
  }

  function confirm(opts = {}) {
    const previouslyFocused = document.activeElement;
    return new Promise(resolve => {
      const bd = build(opts);
      document.body.appendChild(bd);
      requestAnimationFrame(() => bd.classList.add('open'));
      const releaseTrap = trapFocus(bd);
      const input  = bd.querySelector('.dlg-input');
      const btnOk  = bd.querySelector('.btn-confirm');
      const btnNo  = bd.querySelector('.btn-cancel');

      if (opts.typedConfirm) { btnOk.disabled = true; }

      function close(val) {
        bd.classList.remove('open');
        releaseTrap();
        setTimeout(() => { bd.remove(); if (previouslyFocused) previouslyFocused.focus(); }, 200);
        resolve(val);
      }

      btnNo.addEventListener('click', () => close(false));
      btnOk.addEventListener('click', () => close(input ? input.value : true));
      bd.addEventListener('click', e => { if (e.target === bd) close(false); });
      bd.addEventListener('keydown', e => {
        if (e.key === 'Escape') close(false);
        if (e.key === 'Enter' && !btnOk.disabled) close(input ? input.value : true);
      });

      if (input && opts.typedConfirm) {
        input.addEventListener('input', () => {
          btnOk.disabled = input.value.trim().toLowerCase() !== opts.typedConfirm.toLowerCase();
        });
      }

      setTimeout(() => { if (input) input.focus(); else btnOk.focus(); }, 50);
    });
  }

  function prompt(opts = {}) {
    return confirm({
      inputLabel: opts.inputLabel || '',
      inputPlaceholder: opts.inputPlaceholder || '',
      inputHint: opts.inputHint || '',
      inputType: opts.inputType || 'text',
      ...opts,
    });
  }

  return { confirm, prompt };
})();
