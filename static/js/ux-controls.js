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
   DISCLOSURE  `<button class="ux-disclosure-toggle" aria-expanded aria-controls="ID"
          data-ux-disclosure="KEY">` opening and closing the region `#ID`
          (its `hidden`). One delegated listener, so it survives a page that
          redraws its markup; the choice is remembered for the session per KEY,
          and a page drawing a group asks `UXControls.disclosure.isOpen(KEY,
          fallback)` for its state.

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
    // Choosing an item hands focus back to the trigger FIRST (capture phase,
    // before the page's own handler runs): a dialog the item opens then returns
    // focus to the trigger when it closes, not to an item that is now hidden.
    menu.addEventListener('click', event => {
      if (!event.target.closest('[role="menuitem"]')) return;
      if (menu.contains(document.activeElement) && !trigger.disabled) trigger.focus({ preventScroll: true });
    }, true);
    // ... then closes the menu. If the choice disabled the trigger (a job
    // started), focus goes to the page's announced status region instead.
    menu.addEventListener('click', event => {
      if (!event.target.closest('[role="menuitem"]')) return;
      close(entry);
      const lost = document.activeElement === trigger || document.activeElement === document.body
        || menu.contains(document.activeElement);
      if (trigger.disabled && lost) restoreFocus(entry);
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

  // ── Disclosure ────────────────────────────────────────────────
  // The choice is kept for the session; where storage is unavailable (a
  // private window, blocked site data) it is kept for the page's life.
  const DISCLOSURE_STORE = 'ux-disclosure:';
  const disclosureChoices = new Map();
  const disclosure = {
    isOpen(key, fallback = true) {
      try {
        const saved = window.sessionStorage.getItem(DISCLOSURE_STORE + key);
        if (saved === 'open') return true;
        if (saved === 'closed') return false;
      } catch (_) { /* a per-session convenience only */ }
      return disclosureChoices.has(key) ? disclosureChoices.get(key) : fallback;
    },
    remember(key, open) {
      disclosureChoices.set(key, open);
      try { window.sessionStorage.setItem(DISCLOSURE_STORE + key, open ? 'open' : 'closed'); } catch (_) { /* as above */ }
    },
  };
  document.addEventListener('click', event => {
    const toggle = event.target.closest && event.target.closest('.ux-disclosure-toggle[aria-controls]');
    if (!toggle) return;
    const region = document.getElementById(toggle.getAttribute('aria-controls'));
    if (!region) return;
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(open));
    region.hidden = !open;
    if (toggle.dataset.uxDisclosure) disclosure.remember(toggle.dataset.uxDisclosure, open);
  });

  // ── Redraw: new content for a region, the keyboard kept in place ──
  /* A page that draws a region from its data again and again (an inspector
     panel after every answer from the server) calls
     `UXControls.redraw(region, html)` rather than setting innerHTML:
     - the same content leaves the nodes alone, so a screen reader keeps its
       place and a click in progress still lands;
     - new content gives focus back to the same control, matched by its
       `data-focus-key`, its `data-ux-disclosure` or its id; when that
       control is gone, or disabled for now (a save in progress), focus goes
       to the region if it can take it, and the control gets it back on a
       later redraw that shows it enabled, unless the user has moved on.
     A control disabled in place while it has the keyboard (a page switching
     its controls off while a write is in flight) drops focus to the page
     before any redraw; the region takes it, and hands it back the moment the
     control is enabled again — in place or by a redraw.
     The focused field keeps an edit not yet committed when the page draws it
     from the same value. Returns whether it drew, so a page wires listeners
     only on new nodes. Limits: not a caret or text selection; the region
     needs a tabindex to hold the keyboard. */
  const drawn = new WeakMap();
  const focusKeyOf = element => (element.dataset.focusKey ? `focus:${element.dataset.focusKey}`
    : element.dataset.uxDisclosure ? `disclosure:${element.dataset.uxDisclosure}`
      : element.id ? `id:${element.id}` : '');
  const findControl = (region, key) => [...region.querySelectorAll('[data-focus-key],[data-ux-disclosure],[id]')]
    .find(element => focusKeyOf(element) === key);
  function record(region) {
    if (!drawn.has(region)) drawn.set(region, { html: null, first: null, count: -1, awaiting: '' });
    return drawn.get(region);
  }
  const watched = new WeakSet();
  function watch(region) {
    if (watched.has(region)) return;
    watched.add(region);
    // Focus landing anywhere but the region (another control, another part
    // of the page) means the user moved on: nothing waits for the keyboard any
    // more. A window losing focus moves it nowhere, so the wait survives that.
    // The listener lets go of a region taken off the page (a global helper
    // may watch short-lived regions), which is then watched again if reused.
    const onFocusin = event => {
      if (!region.isConnected) {
        document.removeEventListener('focusin', onFocusin);
        observer.disconnect();
        watched.delete(region);
        return;
      }
      if (event.target !== region && drawn.has(region)) drawn.get(region).awaiting = '';
    };
    document.addEventListener('focusin', onFocusin);
    region.addEventListener('focusout', event => {
      const control = event.target;
      // Only a control that lost the keyboard by being switched off is held
      // (and only while nothing else has it: leaving for somewhere else is the
      // user's move).
      if (!control.disabled || !region.hasAttribute('tabindex')) return;
      const key = focusKeyOf(control);
      if (!key) return;
      record(region).awaiting = key;
      // Now if the browser has already let go of it, else once it has.
      const hold = () => {
        const active = document.activeElement;
        if (!active || active === document.body) region.focus({ preventScroll: true });
      };
      hold();
      setTimeout(hold, 0);
    });
    // Enabled again in place: the awaited control takes the keyboard back
    // if the region still holds it.
    const observer = new MutationObserver(() => {
      const entry = drawn.get(region);
      if (!entry || !entry.awaiting || document.activeElement !== region) return;
      const control = findControl(region, entry.awaiting);
      if (!control || control.disabled) return;
      control.focus({ preventScroll: true });
      if (document.activeElement === control) entry.awaiting = '';
    });
    observer.observe(region, { attributes: true, attributeFilter: ['disabled'], subtree: true });
  }
  // A field's value and the value the page drew it with (a select's
  // selected option, an input's value attribute); null for other controls.
  function fieldOf(element) {
    if (element.tagName === 'SELECT') {
      if (element.multiple) return null;
      const drawnOption = [...element.options].find(option => option.defaultSelected) || element.options[0];
      return { value: element.value, initial: drawnOption ? drawnOption.value : '' };
    }
    if (element.tagName === 'TEXTAREA' || (element.tagName === 'INPUT'
      && !['checkbox', 'radio', 'file', 'button', 'submit', 'reset', 'image'].includes(element.type))) {
      return { value: element.value, initial: element.defaultValue };
    }
    return null;
  }
  const sameData = (a, b) => JSON.stringify(Object.entries(a.dataset).sort()) === JSON.stringify(Object.entries(b.dataset).sort());
  function redraw(region, html) {
    watch(region);
    const last = drawn.get(region);
    if (last && last.html === html && last.first === region.firstChild && last.count === region.childNodes.length) return false;
    const active = document.activeElement;
    const inside = Boolean(active) && active !== region && region.contains(active);
    const key = inside ? focusKeyOf(active) : active === region && last ? last.awaiting : '';
    // An edit the user has not committed yet (a keyboard choice waiting to
    // settle) survives a redraw that draws the field from the same value.
    const field = inside ? fieldOf(active) : null;
    region.innerHTML = html;
    let awaiting = '';
    if (inside || key) {
      const control = key ? findControl(region, key) : null;
      // The same field (its key and its data attributes, so another
      // section's select never inherits the edit), drawn from the same value,
      // and able to hold the edit (a select has that option).
      const fresh = control && field && field.value !== field.initial && sameData(active, control) ? fieldOf(control) : null;
      if (fresh && fresh.initial === field.initial
        && (control.tagName !== 'SELECT' || [...control.options].some(option => option.value === field.value))) {
        control.value = field.value;
      }
      if (control && !control.disabled) control.focus({ preventScroll: true });
      // Gone, or not focusable now (disabled, inside a closed group): the region.
      if (!control || document.activeElement !== control) {
        if (region.hasAttribute('tabindex') && document.activeElement !== region) region.focus({ preventScroll: true });
        if (control) awaiting = key;
      }
    }
    drawn.set(region, { html, first: region.firstChild, count: region.childNodes.length, awaiting });
    return true;
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

  window.UXControls = { wire, close: () => entries.forEach(entry => close(entry)), disclosure, redraw };
  // base.html loads this at the end of <body>, so the page's markup is already
  // there: wire it now, and again once parsing ends for anything later (wiring
  // is idempotent per control).
  wire();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => wire());
})();
