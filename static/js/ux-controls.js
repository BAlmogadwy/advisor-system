/* ══════════════════════════════════════════════════════════════════
   UX controls — the shared behaviour of the design system's menus and tabs.
   Loaded on every page by base.html; it acts only on markup that opts in.

   MENU   A trigger `<button aria-haspopup="menu" aria-controls="ID">` and its
          `<div id="ID" class="ux-menu" role="menu" style="display:none">` of
          `[role=menuitem]` buttons or links, both inside one positioned anchor
          (`.btn-split` or `.ux-menu-anchor`). The menu's inline `display` IS its
          state: this script opens and closes it, keeps `aria-expanded` and
          `aria-hidden` true to it, places it, and gives it the keyboard
          (↑ ↓ Home End Esc Tab, type-ahead). Page scripts only listen for item
          clicks.
          `data-align="end"` on the menu prefers its anchor's end edge.
          `data-ux-menu-fallback="ID"` names where focus goes when a choice
          disables the trigger that opened the menu.
          On a phone the menu is a sheet across its `.ux-toolbar` (global.css).
   TABS   `[role=tablist].ux-tabs` of `[role=tab]` buttons whose `.active` class
          marks the selected one: roving tabindex, ← → Home End (mirrored in
          Arabic), `aria-selected`, and the owned panel's `aria-labelledby`.

   Generalised from the Timetable Workspace's own menu script, whose placement
   rules (preferred edge, flip, clamp, phone sheet, re-place on resize) were
   proven by its browser tests at eight widths in both languages.
   ══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  const GAP = 8;
  const phone = window.matchMedia ? window.matchMedia('(max-width: 640px)') : null;
  const entries = [];

  const isOpen = entry => entry.menu.style.display !== 'none';
  const itemsOf = entry => [...entry.menu.querySelectorAll('[role="menuitem"]')]
    .filter(item => !item.disabled && item.getAttribute('aria-disabled') !== 'true');

  // What the user can see of an element: the window cut by every ancestor that
  // clips sideways.
  const visibleSpan = element => {
    let left = 0;
    let right = window.innerWidth;
    for (let a = element.parentElement; a; a = a.parentElement) {
      if (getComputedStyle(a).overflowX === 'visible') continue;
      const box = a.getBoundingClientRect();
      left = Math.max(left, box.left);
      right = Math.min(right, box.right);
    }
    return { left, right };
  };

  function place(entry) {
    const { menu, anchor } = entry;
    const set = (property, value) => menu.style.setProperty(property, value, 'important');
    const buttons = anchor.getBoundingClientRect();
    if (phone && phone.matches) {
      // The stylesheet makes the toolbar the containing block and the menu a
      // sheet across it; lift the sheet to sit just under its own buttons.
      menu.style.removeProperty('left');
      menu.style.removeProperty('right');
      const bar = menu.offsetParent;
      if (bar && bar !== anchor) set('top', `${buttons.bottom - bar.getBoundingClientRect().top - bar.clientTop}px`);
      return;
    }
    set('top', '100%');
    const width = menu.getBoundingClientRect().width;
    const span = visibleSpan(anchor);
    const rtl = getComputedStyle(anchor).direction === 'rtl';
    const atStart = rtl ? buttons.right - width : buttons.left;
    const atEnd = rtl ? buttons.left : buttons.right - width;
    const order = entry.align === 'end' ? [atEnd, atStart] : [atStart, atEnd];
    const fits = left => left >= span.left + GAP - 0.5 && left + width <= span.right - GAP + 0.5;
    const left = order.find(fits) ?? Math.max(span.left + GAP, Math.min(order[0], span.right - GAP - width));
    set('left', `${left - buttons.left - anchor.clientLeft}px`);
    set('right', 'auto');
  }

  function restoreFocus(entry) {
    if (!entry.trigger.disabled) { entry.trigger.focus(); return; }
    // A choice can disable its own trigger synchronously (a long job starts).
    // Keep focus somewhere visible and announced rather than on a hidden item.
    const id = entry.menu.dataset.uxMenuFallback;
    const fallback = id && document.getElementById(id);
    if (fallback) { fallback.tabIndex = -1; fallback.focus(); }
  }

  function sync(entry) {
    const open = isOpen(entry);
    entry.trigger.setAttribute('aria-expanded', String(open));
    entry.menu.setAttribute('aria-hidden', String(!open));
    if (!open && entry.menu.contains(document.activeElement)) restoreFocus(entry);
    // Place once per opening; placing writes the style the observer watches.
    if (open && !entry.placed) {
      place(entry);
      // A short window can open a menu below the fold. Only opening scrolls.
      if (entry.menu.scrollIntoView) entry.menu.scrollIntoView({ block: 'nearest' });
    }
    entry.placed = open;
  }

  function close(entry, focusTrigger) {
    if (isOpen(entry)) entry.menu.style.display = 'none';
    sync(entry);
    if (focusTrigger) restoreFocus(entry);
  }

  function open(entry) {
    entries.filter(other => other !== entry).forEach(other => close(other));
    entry.menu.style.display = 'block';
    sync(entry);
  }

  function focusItem(entry, last) {
    const items = itemsOf(entry);
    const target = last ? items[items.length - 1] : items[0];
    if (target) target.focus();
  }

  function wireMenu(trigger) {
    if (trigger.dataset.uxMenuWired) return;
    const menu = document.getElementById(trigger.getAttribute('aria-controls') || '');
    if (!menu || !menu.classList.contains('ux-menu')) return;
    const anchor = trigger.closest('.btn-split, .ux-menu-anchor');
    if (!anchor || !anchor.contains(menu)) return;
    trigger.dataset.uxMenuWired = '1';
    if (!menu.style.display) menu.style.display = 'none';
    const entry = { trigger, menu, anchor, align: menu.dataset.align || 'start', placed: false };
    entries.push(entry);
    sync(entry);

    new MutationObserver(() => sync(entry)).observe(menu, { attributes: true, attributeFilter: ['style'] });
    trigger.addEventListener('click', event => {
      event.stopPropagation();
      if (trigger.disabled) return;
      if (isOpen(entry)) close(entry);
      else open(entry);
      // Keyboard activation (Enter/Space) reports detail 0: move into the menu.
      // A pointer leaves focus on the trigger until an item is chosen.
      if (isOpen(entry) && event.detail === 0) focusItem(entry);
    });
    trigger.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (trigger.disabled) return;
        if (!isOpen(entry)) open(entry);
        focusItem(entry, event.key === 'ArrowUp');
      } else if (event.key === 'Escape' && isOpen(entry)) {
        event.preventDefault();
        close(entry, true);
      }
    });
    menu.addEventListener('keydown', event => {
      const items = itemsOf(entry);
      const index = items.indexOf(document.activeElement);
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        close(entry, true);
      } else if (event.key === 'Tab') {
        close(entry);
      } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) && items.length) {
        event.preventDefault();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
          : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[next].focus();
      } else if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey && event.key !== ' ') {
        const ordered = [...items.slice(index + 1), ...items.slice(0, index + 1)];
        const match = ordered.find(item => item.textContent.trim().toLocaleLowerCase()
          .startsWith(event.key.toLocaleLowerCase()));
        if (match) { event.preventDefault(); match.focus(); }
      }
    });
    // Choosing an item closes its menu; the page's own click handler runs too.
    menu.addEventListener('click', event => {
      if (event.target.closest('[role="menuitem"]')) close(entry);
    });
    anchor.addEventListener('focusout', event => {
      if (event.relatedTarget && !anchor.contains(event.relatedTarget)) close(entry);
    });
    if (window.ResizeObserver) {
      // Border boxes: a bar whose padding grows moves its buttons without
      // changing its content box.
      const relayout = new ResizeObserver(() => { if (isOpen(entry)) place(entry); });
      relayout.observe(anchor, { box: 'border-box' });
      const bar = anchor.closest('.ux-toolbar');
      if (bar) relayout.observe(bar, { box: 'border-box' });
    }
  }

  function wireTabs(list) {
    if (list.dataset.uxTabsWired) return;
    list.dataset.uxTabsWired = '1';
    const tabs = () => [...list.querySelectorAll('[role="tab"]')];
    const syncTabs = () => {
      tabs().forEach(tab => {
        const selected = tab.classList.contains('active');
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
        if (selected) {
          const panel = document.getElementById(tab.getAttribute('aria-controls') || '');
          if (panel) panel.setAttribute('aria-labelledby', tab.id);
        }
      });
    };
    syncTabs();
    new MutationObserver(syncTabs).observe(list, { attributes: true, attributeFilter: ['class'], subtree: true });
    list.addEventListener('click', syncTabs);
    list.addEventListener('keydown', event => {
      const all = tabs();
      const current = all.indexOf(document.activeElement);
      if (current < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const rtl = getComputedStyle(list).direction === 'rtl';
      const step = (event.key === 'ArrowRight' ? 1 : -1) * (rtl ? -1 : 1);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? all.length - 1
        : (current + step + all.length) % all.length;
      all[next].focus();
      all[next].click();
      syncTabs();
    });
  }

  function wire(root) {
    const scope = root || document;
    scope.querySelectorAll('button[aria-haspopup="menu"][aria-controls]').forEach(wireMenu);
    scope.querySelectorAll('.ux-tabs[role="tablist"]').forEach(wireTabs);
  }

  // One outside-click closer for every menu.
  document.addEventListener('click', event => {
    entries.forEach(entry => { if (!entry.anchor.contains(event.target)) close(entry); });
  });

  window.UXControls = { wire, close: () => entries.forEach(entry => close(entry)) };
  // base.html loads this at the end of <body>, so the page's markup is already
  // there: wire it now, and again once parsing ends for anything later (wiring
  // is idempotent per control).
  wire();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => wire());
})();
