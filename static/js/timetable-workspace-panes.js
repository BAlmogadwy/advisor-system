/* Timetable Workspace — its two side panes.

   Each pane is open or collapsed to a rail that still shows its count. The
   user's choice is remembered on this device; with no choice yet, the
   required-sections list starts collapsed below 1600px so the board gets the
   width (a lecture slot stays at least 96px at 1366 and 1440), and the
   inspector starts open. `[` toggles the list and `]` the inspector, unless a
   field has the keyboard. A click on a card opens a collapsed inspector for
   that click only (event `tw:pane-request`), without changing the saved choice.
   Arabic mirrors the panes (the grid is in logical order); `[` and `]` keep
   their meaning: list, inspector. */
(function () {
  'use strict';
  const workspace = document.getElementById('twWorkspace');
  if (!workspace) return;
  const STORE = 'tw.workspace.panes';
  const AR = document.documentElement.lang === 'ar';
  const panes = {
    left: { el: document.getElementById('twLeft'), toggle: document.getElementById('twToggleLeft'),
      show: AR ? 'إظهار الشعب المطلوبة' : 'Show required sections',
      hide: AR ? 'إخفاء الشعب المطلوبة' : 'Hide required sections' },
    right: { el: document.getElementById('twRight'), toggle: document.getElementById('twToggleRight'),
      show: AR ? 'إظهار لوحة الفحص' : 'Show the inspector',
      hide: AR ? 'إخفاء لوحة الفحص' : 'Hide the inspector' },
  };

  let saved = {};
  try { saved = JSON.parse(window.localStorage.getItem(STORE) || '{}') || {}; } catch (_) { saved = {}; }
  const wide = window.matchMedia ? window.matchMedia('(min-width: 1600px)').matches : true;
  const state = {
    left: saved.left === 'open' || saved.left === 'collapsed' ? saved.left : (wide ? 'open' : 'collapsed'),
    right: saved.right === 'open' || saved.right === 'collapsed' ? saved.right : 'open',
  };

  function apply() {
    Object.entries(panes).forEach(([name, pane]) => {
      const open = state[name] === 'open';
      workspace.dataset[name] = state[name];
      if (pane.toggle) {
        pane.toggle.setAttribute('aria-pressed', String(open));
        pane.toggle.setAttribute('aria-label', open ? pane.hide : pane.show);
      }
    });
  }

  function set(name, value, { persist = true, focus = false } = {}) {
    if (!panes[name] || state[name] === value) return;
    state[name] = value;
    apply();
    if (persist) {
      try { window.localStorage.setItem(STORE, JSON.stringify(state)); } catch (_) { /* per-device convenience only */ }
    }
    if (focus) {
      // Keep the keyboard where the pane now is: its collapse button when it
      // opens, its rail when it closes.
      const pane = panes[name].el;
      const target = value === 'open'
        ? pane?.querySelector('.tw-pane-collapse')
        : pane?.querySelector('.tw-rail');
      target?.focus();
    }
  }
  const toggle = (name, focus) => set(name, state[name] === 'open' ? 'collapsed' : 'open', { focus });

  document.querySelectorAll('[data-pane-toggle]').forEach(button => {
    button.addEventListener('click', () => toggle(button.dataset.paneToggle, true));
  });
  panes.left.toggle?.addEventListener('click', () => toggle('left', false));
  panes.right.toggle?.addEventListener('click', () => toggle('right', false));

  document.addEventListener('keydown', event => {
    if (event.key !== '[' && event.key !== ']') return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
    if (document.querySelector('dialog[open], .dlg-backdrop')) return;
    event.preventDefault();
    toggle(event.key === '[' ? 'left' : 'right', false);
  });

  window.addEventListener('tw:pane-request', event => {
    const { pane, open } = event.detail || {};
    if (open && state[pane] === 'collapsed') set(pane, 'open', { persist: false });
  });

  apply();
})();
