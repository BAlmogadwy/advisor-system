/* Keyboard, accessibility and placement behaviour layered over the
 * workspace's existing click actions. The main workspace script remains the
 * owner of application state, requests, menu toggles and panel rendering. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  // `align`: the edge of its button group a menu prefers on wide screens. The
  // Optimise and Export groups sit at the end of the command bar. `bar`: the
  // toolbar the group sits in, whose layout the placement follows.
  const menus = [
    ['twGenerateMenu', 'twGenerateDropdown', 'twGenerateGroup', 'start', 'twGenBar'],
    ['twOptimiseMenu', 'twOptimiseDropdown', 'twOptimiseGroup', 'end', 'twCmdBar'],
    ['twExportMenu', 'twExportDropdown', 'twExportGroup', 'end', 'twCmdBar'],
  ].map(([trigger, menu, group, align, bar]) => ({ trigger: byId(trigger), menu: byId(menu), group: byId(group), align, bar: byId(bar) }))
    .filter(({ trigger, menu, group, bar }) => trigger && menu && group && bar);

  const isOpen = entry => entry.menu.style.display !== 'none';
  const itemsOf = entry => [...entry.menu.querySelectorAll('[role="menuitem"]')].filter(item => !item.disabled);
  const restoreMenuFocus = entry => {
    if (!entry.trigger.disabled) { entry.trigger.focus(); return; }
    // Starting a planner action can disable its trigger synchronously. Keep
    // focus in the visible status region instead of a now-hidden menu item.
    const status = byId('twGenStatus');
    if (status) { status.tabIndex = -1; status.focus(); }
  };

  // Placement. Where a group lands depends on how its bar wraps, so a fixed
  // alignment cuts a menu off at some widths. On a phone the stylesheet makes
  // the bar the containing block and the menu a full-width sheet; it is moved
  // up to sit just under its own buttons. Elsewhere the menu takes its
  // preferred edge of its group, the other edge when that one would leave the
  // visible content, and is clamped inside it when neither fits. A menu is
  // placed when it opens and again while open whenever its bar or group
  // changes size (a resize, a rotation, the bar rewrapping).
  const phone = window.matchMedia?.('(max-width: 640px)');
  const GAP = 8;
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
  const placeMenu = entry => {
    const { menu, group } = entry;
    const set = (property, value) => menu.style.setProperty(property, value, 'important');
    const buttons = group.getBoundingClientRect();
    if (phone?.matches) {
      // Offsets from a wide-screen placement (before a rotation, or a window
      // narrowed with the menu open) would beat the sheet's insets.
      menu.style.removeProperty('left');
      menu.style.removeProperty('right');
      const bar = menu.offsetParent;
      if (bar && bar !== group) set('top', `${buttons.bottom - bar.getBoundingClientRect().top - bar.clientTop}px`);
    } else {
      set('top', '100%');
      const width = menu.getBoundingClientRect().width;
      const span = visibleSpan(group);
      const rtl = getComputedStyle(group).direction === 'rtl';
      const atStart = rtl ? buttons.right - width : buttons.left;
      const atEnd = rtl ? buttons.left : buttons.right - width;
      const order = entry.align === 'end' ? [atEnd, atStart] : [atStart, atEnd];
      const fits = left => left >= span.left + GAP - 0.5 && left + width <= span.right - GAP + 0.5;
      const left = order.find(fits) ?? Math.max(span.left + GAP, Math.min(order[0], span.right - GAP - width));
      set('left', `${left - buttons.left - group.clientLeft}px`);
      set('right', 'auto');
    }
  };

  const syncMenu = entry => {
    const open = isOpen(entry);
    entry.trigger.setAttribute('aria-expanded', String(open));
    entry.menu.setAttribute('aria-hidden', String(!open));
    if (!open && entry.menu.contains(document.activeElement)) restoreMenuFocus(entry);
    // Place once per opening; placing writes the style the observer watches.
    if (open && !entry.placed) {
      placeMenu(entry);
      // A short screen can open a menu below the fold. Only opening scrolls:
      // a phone's address bar resizes the page as it is scrolled, and a
      // re-placement must not pull the page back.
      entry.menu.scrollIntoView?.({ block: 'nearest' });
    }
    entry.placed = open;
  };
  const closeMenu = (entry, restoreFocus = false) => {
    entry.menu.style.display = 'none';
    syncMenu(entry);
    if (restoreFocus) restoreMenuFocus(entry);
  };
  const focusItem = (entry, last = false) => {
    const items = itemsOf(entry);
    (last ? items.at(-1) : items[0])?.focus();
  };

  menus.forEach(entry => {
    syncMenu(entry);
    new MutationObserver(() => syncMenu(entry)).observe(entry.menu, { attributes: true, attributeFilter: ['style'] });
    entry.trigger.addEventListener('click', event => {
      menus.filter(other => other !== entry).forEach(other => closeMenu(other));
      syncMenu(entry);
      // Native Enter/Space click activation reports detail=0. Pointer opening
      // leaves focus on the trigger until the user chooses an item.
      if (isOpen(entry) && event.detail === 0) focusItem(entry);
    });
    entry.trigger.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (entry.trigger.disabled) return;
        if (!isOpen(entry)) entry.trigger.click();
        focusItem(entry, event.key === 'ArrowUp');
      } else if (event.key === 'Escape' && isOpen(entry)) {
        event.preventDefault();
        closeMenu(entry, true);
      }
    });
    entry.menu.addEventListener('keydown', event => {
      const items = itemsOf(entry);
      const index = items.indexOf(document.activeElement);
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeMenu(entry, true);
      } else if (event.key === 'Tab') {
        closeMenu(entry);
      } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) && items.length) {
        event.preventDefault();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
          : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[next].focus();
      } else if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey && event.key !== ' ') {
        const ordered = [...items.slice(index + 1), ...items.slice(0, index + 1)];
        const match = ordered.find(item => item.textContent.trim().toLocaleLowerCase().startsWith(event.key.toLocaleLowerCase()));
        if (match) { event.preventDefault(); match.focus(); }
      }
    });
    entry.group.addEventListener('focusout', event => {
      if (event.relatedTarget && !entry.group.contains(event.relatedTarget)) closeMenu(entry);
    });
  });
  document.addEventListener('click', event => {
    menus.forEach(entry => { if (!entry.group.contains(event.target)) closeMenu(entry); });
  });
  // An open menu follows its bar and group. The window's resize event comes
  // too early: a bar can go on rewrapping and growing for several frames
  // after it. The observer reports after each layout; placing moves only the
  // menu, so it never resizes what is observed.
  if (window.ResizeObserver) {
    const relayout = new ResizeObserver(() => menus.filter(isOpen).forEach(placeMenu));
    menus.forEach(({ group, bar }) => { relayout.observe(bar); relayout.observe(group); });
  }

  document.querySelectorAll('.tw-panel-tabs[role="tablist"]').forEach(list => {
    const tabs = [...list.querySelectorAll('[role="tab"]')];
    const syncTabs = () => {
      tabs.forEach(tab => {
        const selected = tab.classList.contains('active');
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
        if (selected) byId(tab.getAttribute('aria-controls'))?.setAttribute('aria-labelledby', tab.id);
      });
    };
    syncTabs();
    tabs.forEach(tab => new MutationObserver(syncTabs).observe(tab, { attributes: true, attributeFilter: ['class'] }));
    list.addEventListener('click', syncTabs);
    list.addEventListener('keydown', event => {
      const current = tabs.indexOf(document.activeElement);
      if (current < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const rtl = getComputedStyle(list).direction === 'rtl';
      const step = (event.key === 'ArrowRight' ? 1 : -1) * (rtl ? -1 : 1);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
        : (current + step + tabs.length) % tabs.length;
      tabs[next].focus();
      tabs[next].click();
      syncTabs();
    });
  });

  const bottom = byId('twBottom');
  const handle = byId('twBottomHandle');
  const content = byId('twBottomContent');
  if (bottom && handle && content) {
    const syncBottom = () => {
      const open = bottom.classList.contains('open');
      handle.setAttribute('aria-expanded', String(open));
      content.hidden = !open;
      if (!open && content.contains(document.activeElement)) handle.focus();
    };
    syncBottom();
    handle.addEventListener('click', syncBottom);
    new MutationObserver(syncBottom).observe(bottom, { attributes: true, attributeFilter: ['class'] });
  }
})();
