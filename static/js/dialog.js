/**
 * Dialog system — Advisor Portal
 * Provides dlg.confirm() and dlg.prompt()
 * Includes focus trapping and focus restoration.
 *
 * Accepted options:
 *   title          – dialog heading text
 *   body           – HTML body content
 *   icon / kind    – 'danger' | 'warning' | 'info' (sets icon + button style)
 *   confirmText    – label for the confirm button  (alias: confirmLabel)
 *   cancelText     – label for the cancel button   (alias: cancelLabel)
 *   typed          – require the user to type this string to enable confirm (alias: typedConfirm)
 *   inputLabel     – show an input field with this label
 *   inputPlaceholder / inputHint / inputType – input configuration
 */
const dlg = (() => {
  const ICONS = {
    danger:  '<svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    info:    '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    warning: '<svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    success: '<svg viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
  };

  /* ── Focus trap helper ── */
  function trapFocus(container) {
    const SEL = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
    function handler(e) {
      if (e.key !== 'Tab') return;
      const els = Array.from(container.querySelectorAll(SEL));
      if (!els.length) return;
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    container.addEventListener('keydown', handler);
    return () => container.removeEventListener('keydown', handler);
  }

  /* ── Normalize caller options ── */
  function norm(opts) {
    const kind = opts.kind || opts.icon || opts.confirmClass || '';
    return {
      title:       opts.title || '',
      body:        opts.body || '',
      kind,
      confirmLabel: opts.confirmText || opts.confirmLabel || 'Confirm',
      cancelLabel:  opts.cancelText  || opts.cancelLabel  || 'Cancel',
      typedConfirm: opts.typed       || opts.typedConfirm || '',
      inputLabel:       opts.inputLabel || opts.label || '',
      inputPlaceholder: opts.inputPlaceholder || opts.placeholder || '',
      inputHint:        opts.inputHint || opts.hint || '',
      inputType:        opts.inputType || opts.type || 'text',
    };
  }

  function build(raw) {
    const o = norm(raw);
    const bd = document.createElement('div');
    bd.className = 'dlg-backdrop';
    bd.setAttribute('role', 'dialog');
    bd.setAttribute('aria-modal', 'true');

    /* Icon */
    const iconSvg = ICONS[o.kind] || '';
    const iconHtml = iconSvg
      ? `<div class="dlg-icon dlg-icon-${o.kind}"><span class="i" aria-hidden="true">${iconSvg}</span></div>`
      : '';

    /* Typed-confirm input */
    let inputHtml = '';
    if (o.typedConfirm) {
      inputHtml =
        `<div class="dlg-input-row">` +
          `<label for="dlg-typed-field">Type <strong>${o.typedConfirm}</strong> to confirm</label>` +
          `<input id="dlg-typed-field" class="dlg-input" type="text" placeholder="${o.typedConfirm}" autocomplete="off">` +
        `</div>`;
    }

    /* Free-form input (prompt) */
    if (o.inputLabel && !o.typedConfirm) {
      inputHtml =
        `<div class="dlg-input-row">` +
          `<label for="dlg-input-field">${o.inputLabel}</label>` +
          `<input id="dlg-input-field" class="dlg-input" type="${o.inputType}" placeholder="${o.inputPlaceholder}" autocomplete="off">` +
          (o.inputHint ? `<div class="dlg-input-hint">${o.inputHint}</div>` : '') +
        `</div>`;
    }

    /* Button style class */
    const btnClass = o.kind || 'primary';

    const titleId = 'dlg-title-text';
    bd.setAttribute('aria-labelledby', titleId);

    bd.innerHTML =
      `<div class="dlg-box">` +
        `<div class="dlg-header">` +
          iconHtml +
          `<div>` +
            `<div id="${titleId}" class="dlg-title">${o.title}</div>` +
          `</div>` +
        `</div>` +
        (o.body ? `<div class="dlg-body">${o.body}</div>` : '') +
        inputHtml +
        `<div class="dlg-footer">` +
          `<button class="btn-cancel">${o.cancelLabel}</button>` +
          `<button class="btn-confirm ${btnClass}">${o.confirmLabel}</button>` +
        `</div>` +
      `</div>`;

    return { bd, o };
  }

  function confirm(opts = {}) {
    const previouslyFocused = document.activeElement;
    return new Promise(resolve => {
      const { bd, o } = build(opts);

      /* Hide background from screen readers while dialog is open */
      const mainEl = document.querySelector('main');
      if (mainEl) mainEl.setAttribute('aria-hidden', 'true');

      document.body.appendChild(bd);
      requestAnimationFrame(() => bd.classList.add('open'));
      const releaseTrap = trapFocus(bd);

      const input  = bd.querySelector('.dlg-input');
      const btnOk  = bd.querySelector('.btn-confirm');
      const btnNo  = bd.querySelector('.btn-cancel');

      if (o.typedConfirm) { btnOk.disabled = true; }

      function close(val) {
        bd.classList.remove('open');
        releaseTrap();
        if (mainEl) mainEl.removeAttribute('aria-hidden');
        setTimeout(() => { bd.remove(); if (previouslyFocused) previouslyFocused.focus(); }, 200);
        resolve(val);
      }

      btnNo.addEventListener('click', () => close(false));
      btnOk.addEventListener('click', () => close(input ? input.value || true : true));
      bd.addEventListener('click', e => { if (e.target === bd) close(false); });
      bd.addEventListener('keydown', e => {
        if (e.key === 'Escape') close(false);
        if (e.key === 'Enter' && !btnOk.disabled) close(input ? input.value || true : true);
      });

      if (input && o.typedConfirm) {
        input.addEventListener('input', () => {
          btnOk.disabled = input.value.trim().toLowerCase() !== o.typedConfirm.toLowerCase();
        });
      }

      setTimeout(() => { if (input) input.focus(); else btnOk.focus(); }, 50);
    });
  }

  function prompt(opts = {}) {
    return confirm({ ...opts });
  }

  return { confirm, prompt };
})();
