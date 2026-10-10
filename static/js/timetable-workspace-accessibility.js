/* Keyboard and accessibility behaviour layered over the workspace's existing
 * click actions. The main workspace script remains the owner of application
 * state, requests, menu toggles and panel rendering. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const menus = [
    ['twGenerateMenu', 'twGenerateDropdown', 'twGenerateGroup'],
    ['twOptimiseMenu', 'twOptimiseDropdown', 'twOptimiseGroup'],
    ['twExportMenu', 'twExportDropdown', 'twExportGroup'],
  ].map(([trigger, menu, group]) => ({ trigger: byId(trigger), menu: byId(menu), group: byId(group) }))
    .filter(({ trigger, menu, group }) => trigger && menu && group);

  const isOpen = entry => entry.menu.style.display !== 'none';
  const itemsOf = entry => [...entry.menu.querySelectorAll('[role="menuitem"]')].filter(item => !item.disabled);
  const restoreMenuFocus = entry => {
    if (!entry.trigger.disabled) { entry.trigger.focus(); return; }
    // Starting a planner action can disable its trigger synchronously. Keep
    // focus in the visible status region instead of a now-hidden menu item.
    const status = byId('twGenStatus');
    if (status) { status.tabIndex = -1; status.focus(); }
  };
  const syncMenu = entry => {
    const open = isOpen(entry);
    entry.trigger.setAttribute('aria-expanded', String(open));
    entry.menu.setAttribute('aria-hidden', String(!open));
    if (!open && entry.menu.contains(document.activeElement)) restoreMenuFocus(entry);
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
