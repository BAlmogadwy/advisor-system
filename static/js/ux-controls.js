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
   DIALOG `UXControls.dialog.open(dialog, {initialFocus, returnFocus})`,
          `.close(dialog, reason)` and `.isOpen(dialog)` for a template
          `<dialog>`: modal (or `[open]` plus `data-ux-dialog="fallback"`
          where the browser has no showModal), closed by its
          `[data-dialog-close]` buttons and Escape, with focus sent at close
          to wherever `returnFocus()` says THEN, and a `ux-dialog-close`
          event ({reason}). It acts only on dialogs it opened, so a page's
          own dialogs keep their own handlers.
   CHOICE LIST  `.ux-choice-list[data-ux-submit]` of `label.ux-choice` rows,
          each a native radio: Home, End, PageUp and PageDown (10 rows) on top
          of the native arrows; Enter submits the form once, only while its
          submit button is on.

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

  // ── Roving focus over grids ───────────────────────────────────
  // ROVING: one tab stop for a region of grids, the keyboard kept through
  // redraws (design record §3.3 and §7.3, 5a).
  /* `UXControls.roving(region, options)` gives a region of ARIA grids
     (`[role=grid] > [role=row] > [role=gridcell]`, rows usually
     `.ux-grid-row`) ONE tab stop and arrow-key movement, the WAI-ARIA grid
     pattern. A board (days × times, several tables) and a list (one cell per
     row) use the same controller.
     - stop: selector of the stops inside a cell (cards); a cell holding none
       is itself a stop. Stops are named by `data-focus-key`.
     - Keys: arrows (mirrored in RTL; ↑/↓ also walk the stops stacked in one
       cell), Home/End (row; the whole list when every row has one cell),
       Ctrl+Home/End, PageUp/PageDown (the previous/next table), Enter and
       Space (`activate`, default a click; never a repeat, never on a native
       control), and a `keys` map (a Latin capital is matched by `letterOf`,
       any other name by `event.key`; a handler returning false leaves the
       key alone).
     - The page redraws freely: the controller keeps the active stop's key and
       position, finds them again after every redraw (a MutationObserver, or
       `nav.refresh()`), and gives the keyboard back to the active stop when
       the keyboard was in the region and the redraw left it nowhere. A
       restore never scrolls unless the last input was a key.
     - hint: an element shown while the keyboard is in the region (its keys).
     nav: active() / active(key, {focus}), element(key), refresh(), restore(),
     reset(). */
  let lastInputWasKeyboard = false;
  document.addEventListener('keydown', () => { lastInputWasKeyboard = true; }, true);
  document.addEventListener('pointerdown', () => { lastInputWasKeyboard = false; }, true);
  document.addEventListener('wheel', () => { lastInputWasKeyboard = false; }, { capture: true, passive: true });

  /* The letter a key means, on any layout: the Latin letter it types, or,
     where it types another script's letter (Arabic ة on KeyM), its key's
     letter. Punctuation and digits mean none (AZERTY's KeyM types ","). */
  function letterOf(event) {
    const key = String(event.key || '');
    if (/^[a-z]$/i.test(key)) return key.toUpperCase();
    if (/^\p{L}$/u.test(key) && /^Key[A-Z]$/.test(String(event.code || ''))) return event.code.slice(3);
    return '';
  }

  const NATIVE_CONTROL = 'button, a[href], input, select, textarea';
  const rovers = new WeakMap();
  function roving(region, options = {}) {
    if (!region) return null;
    if (rovers.has(region)) {
      Object.assign(rovers.get(region).options, options);
      return rovers.get(region).nav;
    }
    const self = { options: { ...options }, key: '', pos: null, pending: '', misses: 0, owned: false, nav: null, boxed: false };
    rovers.set(region, self);
    const opt = self.options;
    const keyOf = element => (element && element.dataset ? element.dataset.focusKey || '' : '');
    // Hidden, or inside something hidden. An element that draws no box (a
    // display: contents row) has no visibility of its own to check: Chrome's
    // checkVisibility() says false for it.
    const skipped = element => {
      for (let node = element; node && node !== region; node = node.parentElement) {
        if (node.hidden || (node.classList && node.classList.contains('d-none'))) return true;
      }
      if (typeof element.checkVisibility !== 'function' || getComputedStyle(element).display === 'contents') return false;
      return element.checkVisibility() === false;
    };

    // The structure, read on demand: tables → rows → cells → stops.
    function layout() {
      return [...region.querySelectorAll('[role="grid"]')].filter(table => !skipped(table)).map(table =>
        [...table.querySelectorAll('[role="row"]')].filter(row => !skipped(row) && row.querySelector('[role="gridcell"]')).map(row =>
          [...row.querySelectorAll('[role="gridcell"]')].filter(cell => !skipped(cell)).map(cell => {
            const inner = opt.stop ? [...cell.querySelectorAll(opt.stop)].filter(stop => !skipped(stop)) : [];
            return { cell, stops: inner.length ? inner : [cell] };
          })).filter(row => row.length)).filter(table => table.length);
    }
    const allStops = grid => grid.flatMap(table => table.flatMap(row => row.flatMap(cell => cell.stops)));
    const listShaped = grid => grid.length > 0 && grid.every(table => table.every(row => row.length === 1));
    function locate(grid, element) {
      for (let g = 0; g < grid.length; g++) {
        for (let r = 0; r < grid[g].length; r++) {
          for (let c = 0; c < grid[g][r].length; c++) {
            const s = grid[g][r][c].stops.indexOf(element);
            if (s >= 0) return { g, r, c, s };
          }
        }
      }
      return null;
    }
    const at = (grid, pos) => grid[pos.g][pos.r][pos.c].stops[pos.s];
    // The stop a key names; a key naming a cell that now holds stops names its first.
    function byKey(grid, key) {
      if (!key) return null;
      for (const stop of allStops(grid)) if (keyOf(stop) === key) return stop;
      for (const table of grid) for (const row of table) for (const cell of row) {
        if (keyOf(cell.cell) === key) return cell.stops[0];
      }
      return null;
    }
    // In a row, the cell nearest a column (data-ux-col), else the same index.
    function aligned(row, fromCell, index) {
      const col = Number(fromCell.cell.dataset.uxCol);
      if (Number.isFinite(col) && row.every(cell => Number.isFinite(Number(cell.cell.dataset.uxCol)))) {
        let best = 0;
        row.forEach((cell, i) => {
          if (Math.abs(Number(cell.cell.dataset.uxCol) - col) < Math.abs(Number(row[best].cell.dataset.uxCol) - col)) best = i;
        });
        return best;
      }
      return Math.min(index, row.length - 1);
    }

    function setActive(grid, stop) {
      const stops = allStops(grid);
      region.querySelectorAll('[data-ux-roving]').forEach(element => {
        if (!stops.includes(element)) { element.removeAttribute('tabindex'); element.removeAttribute('data-ux-roving'); }
      });
      stops.forEach(element => {
        element.setAttribute('data-ux-roving', '');
        element.tabIndex = element === stop ? 0 : -1;
      });
      if (stop) {
        self.key = keyOf(stop);
        self.pos = locate(grid, stop);
      }
    }
    function moveTo(grid, stop) {
      if (!stop) return;
      setActive(grid, stop);
      stop.focus({ preventScroll: true });
      if (typeof stop.scrollIntoView === 'function') stop.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
    const nowhere = () => {
      const active = document.activeElement;
      return !active || active === document.body || !active.isConnected;
    };
    function giveBack(grid, stop, { always = false } = {}) {
      if (!always && !(self.owned && nowhere())) return;
      const target = stop || region;
      if (!stop && !region.hasAttribute('tabindex')) return;
      target.focus({ preventScroll: true });
      if (lastInputWasKeyboard && typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }

    // R2: which stop is active after a redraw.
    function refresh() {
      const grid = layout();
      const stops = allStops(grid);
      if (!stops.length) { setActive(grid, null); giveBack(grid, null); return; }
      let stop = null;
      if (self.pending) {
        stop = byKey(grid, self.pending);
        // A key named before it is drawn waits for the redraw that draws it,
        // not for ever (a stale one would take the keyboard much later). A
        // page redraw usually refreshes twice (its own call, then the
        // observer), so name the key just before the redraw that draws it.
        if (stop || ++self.misses > 1) { self.pending = ''; self.misses = 0; }
      }
      stop = stop || byKey(grid, self.key);
      if (!stop && typeof opt.initial === 'function') stop = byKey(grid, opt.initial() || '');
      if (!stop && self.pos) {
        const g = Math.min(self.pos.g, grid.length - 1);
        const r = Math.min(self.pos.r, grid[g].length - 1);
        const c = Math.min(self.pos.c, grid[g][r].length - 1);
        const cell = grid[g][r][c];
        stop = cell.stops[Math.min(self.pos.s, cell.stops.length - 1)];
      }
      stop = stop || stops.find(element => element.getAttribute('aria-disabled') !== 'true') || stops[0];
      setActive(grid, stop);
      giveBack(grid, stop);
    }

    function step(grid, pos, key, event, rtl) {
      const row = grid[pos.g][pos.r];
      const cell = row[pos.c];
      const forward = rtl ? 'ArrowLeft' : 'ArrowRight';
      const back = rtl ? 'ArrowRight' : 'ArrowLeft';
      const list = listShaped(grid);
      const stops = allStops(grid);
      const rowAt = (g, r, fromIndex, last) => {
        const target = grid[g][r];
        const c = aligned(target, cell, fromIndex);
        return last ? target[c].stops[target[c].stops.length - 1] : target[c].stops[0];
      };
      if (key === forward || key === back) {
        if (list) return undefined;
        const c = key === forward ? pos.c + 1 : pos.c - 1;
        if (c < 0 || c >= row.length) return at(grid, pos);
        return row[c].stops[Math.min(pos.s, row[c].stops.length - 1)];
      }
      if (key === 'ArrowDown') {
        if (pos.s + 1 < cell.stops.length) return cell.stops[pos.s + 1];
        if (pos.r + 1 < grid[pos.g].length) return rowAt(pos.g, pos.r + 1, pos.c, false);
        if (pos.g + 1 < grid.length) return rowAt(pos.g + 1, 0, pos.c, false);
        return at(grid, pos);
      }
      if (key === 'ArrowUp') {
        if (pos.s > 0) return cell.stops[pos.s - 1];
        if (pos.r > 0) return rowAt(pos.g, pos.r - 1, pos.c, true);
        if (pos.g > 0) return rowAt(pos.g - 1, grid[pos.g - 1].length - 1, pos.c, true);
        return at(grid, pos);
      }
      if (key === 'Home' || key === 'End') {
        if (event.ctrlKey || list) return key === 'Home' ? stops[0] : stops[stops.length - 1];
        return (key === 'Home' ? row[0] : row[row.length - 1]).stops[0];
      }
      if (key === 'PageDown' || key === 'PageUp') {
        const g = key === 'PageDown' ? pos.g + 1 : pos.g - 1;
        if (g < 0 || g >= grid.length) return at(grid, pos);
        // A list's tables are its groups (terms): the next one from its start.
        return rowAt(g, list ? 0 : Math.min(pos.r, grid[g].length - 1), pos.c, false);
      }
      return undefined;
    }

    const NAVIGATION = new Set(['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End', 'PageDown', 'PageUp']);
    region.addEventListener('keydown', event => {
      if (event.defaultPrevented || event.isComposing || event.altKey || event.metaKey || event.shiftKey) return;
      const grid = layout();
      const stops = allStops(grid);
      const target = event.target;
      const stop = stops.find(element => element === target || element.contains(target)) || null;
      if (!stop && target !== region) return;
      const key = event.key;
      if (event.ctrlKey && key !== 'Home' && key !== 'End') return;
      if (NAVIGATION.has(key)) {
        if (!stops.length) return;
        if (!stop) {
          event.preventDefault();
          moveTo(grid, byKey(grid, self.key) || stops[0]);
          return;
        }
        const rtl = getComputedStyle(region).direction === 'rtl';
        const next = step(grid, locate(grid, stop), key, event, rtl);
        if (next === undefined) return;
        event.preventDefault();
        if (next !== document.activeElement) moveTo(grid, next);
        return;
      }
      if (!stop || event.repeat) return;
      if (key === 'Enter' || key === ' ') {
        if (target.matches && target.matches(NATIVE_CONTROL)) return;
        event.preventDefault();
        if (typeof opt.activate === 'function') opt.activate(stop, event);
        else stop.click();
        return;
      }
      const keys = opt.keys || {};
      const letter = letterOf(event);
      const name = Object.keys(keys).find(entry => (/^[A-Z]$/.test(entry) ? entry === letter : entry === key));
      if (!name || event.ctrlKey) return;
      if (keys[name](stop, event) === false) return;
      event.preventDefault();
    });

    // R3/R4: who has the keyboard.
    region.addEventListener('focusin', event => {
      self.owned = true;
      const grid = layout();
      const stop = allStops(grid).find(element => element === event.target || element.contains(event.target));
      if (stop) setActive(grid, stop);
      if (opt.hint && lastInputWasKeyboard && opt.hint.hidden) {
        opt.hint.hidden = false;
        // The line takes room below its region: keep in view whatever has the
        // keyboard by then (it may have moved on within the frame).
        (window.requestAnimationFrame || setTimeout)(() => {
          const now = document.activeElement;
          if (now && region.contains(now) && typeof now.scrollIntoView === 'function') now.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        });
      }
    });
    region.addEventListener('focusout', event => {
      if (event.relatedTarget && !region.contains(event.relatedTarget)) {
        self.owned = false;
        if (opt.hint) opt.hint.hidden = true;
      }
    });
    document.addEventListener('pointerdown', event => {
      if (!region.contains(event.target)) {
        self.owned = false;
        if (opt.hint) opt.hint.hidden = true;
      }
    }, true);
    // R6: any redraw of the region (attributes are not watched: tones and the
    // selection never move the keyboard).
    new MutationObserver(() => { if (region.isConnected) refresh(); }).observe(region, { childList: true, subtree: true });
    if (window.ResizeObserver) {
      new ResizeObserver(entries => {
        const box = entries[entries.length - 1].contentRect;
        const boxed = box.width > 0 || box.height > 0;
        if (boxed && !self.boxed && region.isConnected) refresh();
        self.boxed = boxed;
      }).observe(region);
    }

    const nav = {
      active(key, { focus = false } = {}) {
        if (key === undefined) return self.key;
        const grid = layout();
        const stop = byKey(grid, key);
        if (!stop) { self.pending = key; self.misses = 0; return key; }
        self.pending = '';
        setActive(grid, stop);
        if (focus) moveTo(grid, stop);
        return self.key;
      },
      element(key = self.key) {
        const stop = byKey(layout(), key);
        return stop && stop.isConnected ? stop : null;
      },
      refresh,
      restore() {
        refresh();
        giveBack(layout(), nav.element(), { always: true });
      },
      reset() {
        self.key = '';
        self.pos = null;
        self.pending = '';
      },
    };
    self.nav = nav;
    refresh();
    return nav;
  }

  // ── Dialogs (UXControls.dialog) ───────────────────────────────
  /* One layer for the template dialogs that choose something (design record
     §7.3, 5c). A page opens one with
     `UXControls.dialog.open(dialog, {initialFocus, returnFocus})`:
     - false when a dialog of this helper is already open (they are modal:
       one at a time), or the dialog is open by other code;
     - menus close, then the dialog opens modal; a browser without
       showModal (jsdom) gets `[open]` plus `data-ux-dialog="fallback"`;
     - focus goes to `initialFocus()`, else the first control that takes it;
     - `returnFocus` is a function, kept and called only at close: the page
       may have redrawn the control that opened the dialog while it was open,
       and the new node is the one that should get the keyboard back.
     `close(dialog, reason = 'close')` closes it, focuses what `returnFocus()`
     gives if it can take focus (on the page, enabled, not a hidden input, not
     inside `[hidden]` or `.d-none`), then fires `ux-dialog-close` (bubbling,
     `detail.reason`). The user's ways out are 'cancel': a
     `[data-dialog-close]` button (never a submit, whatever its type: one
     with no type is given `type="button"` at open, so it is never the
     form's default button that Enter in a text field clicks) and
     Escape (the browser's `cancel`, or a keydown in fallback mode). A close
     the helper did not start and no cancel began (a `<form method="dialog">`
     submitted, page code calling `dialog.close()`) is 'close'. Every listener
     acts only on dialogs in `dialogs`, so the Generate dialog and the English
     chooser, which wire their own closing, are never touched. */
  const dialogs = new WeakMap();
  let shownDialog = null;
  const FOCUSABLE = 'button, [href], input, select, textarea, summary, [tabindex]:not([tabindex="-1"])';
  const canTakeFocus = element => Boolean(element) && typeof element.focus === 'function'
    && element.isConnected && !element.disabled && !(element.tagName === 'INPUT' && element.type === 'hidden')
    && !element.closest('[hidden], .d-none');
  const dialogIsOpen = dialog => Boolean(dialog) && dialogs.has(dialog)
    && dialog.isConnected && dialog.hasAttribute('open');

  function openDialog(dialog, { initialFocus, returnFocus } = {}) {
    if (!dialog) return false;
    if (shownDialog && dialogIsOpen(shownDialog)) return false;
    // A dialog taken off the page (or closed by other code without an event)
    // while open is forgotten, so it can never block the next one.
    if (shownDialog) dialogs.delete(shownDialog);
    shownDialog = null;
    if (dialog.hasAttribute('open')) return false;
    entries.forEach(entry => close(entry));
    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
    } else {
      dialog.setAttribute('open', '');
      dialog.dataset.uxDialog = 'fallback';
    }
    dialogs.set(dialog, { returnFocus });
    shownDialog = dialog;
    // A button with no type is a submit button, and the form's first submit
    // button is its default: Enter in a text field (a filter) would click a
    // way out placed before the commit and close the dialog. Each way out is
    // given the type it means; one added later is still stopped at its click.
    dialog.querySelectorAll('button[data-dialog-close]:not([type])').forEach(button => { button.type = 'button'; });
    const wanted = typeof initialFocus === 'function' ? initialFocus() : null;
    const first = canTakeFocus(wanted) ? wanted
      : [...dialog.querySelectorAll(FOCUSABLE)].find(canTakeFocus);
    if (first) first.focus();
    return true;
  }

  function closeDialog(dialog, reason = 'close') {
    const entry = dialog ? dialogs.get(dialog) : null;
    if (!entry) return;
    // Forget it first: closing a modal dialog fires the browser's own
    // `close`, which must find nothing left to do.
    dialogs.delete(dialog);
    if (shownDialog === dialog) shownDialog = null;
    if (dialog.dataset.uxDialog === 'fallback') {
      dialog.removeAttribute('open');
      delete dialog.dataset.uxDialog;
    } else if (dialog.open && typeof dialog.close === 'function') {
      dialog.close();
    }
    const target = typeof entry.returnFocus === 'function' ? entry.returnFocus() : null;
    if (canTakeFocus(target)) {
      // As the roving restores: scroll to it only for a keyboard user.
      target.focus({ preventScroll: true });
      if (lastInputWasKeyboard && typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
    dialog.dispatchEvent(new CustomEvent('ux-dialog-close', { bubbles: true, detail: { reason } }));
  }

  document.addEventListener('click', event => {
    const button = event.target.closest && event.target.closest('[data-dialog-close]');
    const dialog = button && button.closest('dialog');
    if (!dialog || !dialogs.has(dialog)) return;
    // A button with no type in the form is a submit button: it closes, and
    // never submits the choice it is closing on.
    event.preventDefault();
    closeDialog(dialog, 'cancel');
  });
  // Escape in a modal dialog: the browser fires `cancel` (it does not
  // bubble, so only a capturing listener on the document hears it). The
  // helper closes the dialog itself, so focus and the event are its own, and
  // lets it go before the browser's own `close` arrives.
  document.addEventListener('cancel', event => {
    if (!dialogs.has(event.target)) return;
    event.preventDefault();
    closeDialog(event.target, 'cancel');
  }, true);
  // So a `close` that still finds the dialog the helper's is one no cancel
  // began: a `<form method="dialog">` submitted with nothing stopping it, or
  // page code calling dialog.close(). It is closed; only the bookkeeping is
  // left, as 'close'. Chromium (145) fires `cancel` on every Escape, a second
  // one with no click in between included (a cancel it no longer lets the
  // page stop), so no Escape lands here there; the listener is a guard for a
  // browser that would close without one. The event comes a moment after
  // the closing; a dialog open again by then is not the one it means.
  document.addEventListener('close', event => {
    const dialog = event.target;
    if (dialogs.has(dialog) && !dialog.hasAttribute('open')) closeDialog(dialog, 'close');
  }, true);
  // No showModal, so no `cancel` either: Escape itself, unless a control
  // inside used it (a filter clearing its text stops it).
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return;
    const dialog = shownDialog;
    if (!dialog || dialog.dataset.uxDialog !== 'fallback' || !dialogs.has(dialog)) return;
    const owner = event.target.closest ? event.target.closest('dialog') : null;
    if (owner && owner !== dialog) return;
    event.preventDefault();
    closeDialog(dialog, 'cancel');
  });

  // ── Choice lists: radios as a list of rows ────────────────────
  /* A `.ux-choice-list[data-ux-submit]` holds `label.ux-choice` rows, each a
     native radio (a row that cannot be chosen is `.ux-choice-static`, with
     no radio). The arrows stay the browser's. Home, End, PageUp and PageDown
     (10 rows, held at the ends) check and focus a radio of the same group,
     skipping hidden rows (a filter hides them in place), and fire `input`
     and `change` as an arrow does. Enter submits the form once, and only
     while its submit button is on: the page holds that button while a choice
     cannot be committed. A list without `data-ux-submit` is left alone. */
  const choiceHidden = input => Boolean(input.closest('[hidden], .d-none'))
    || (typeof input.checkVisibility === 'function' && input.checkVisibility() === false);
  // The radio's group as the browser sees it: same form, same name (matched
  // in JS, so no name needs escaping into a selector).
  function choiceGroup(radio) {
    if (!radio.name) return [radio];
    const scope = radio.form ? [...radio.form.elements] : [...radio.ownerDocument.querySelectorAll('input')];
    return scope.filter(input => input.tagName === 'INPUT' && input.type === 'radio' && input.name === radio.name
      && input.form === radio.form && !input.disabled && !choiceHidden(input));
  }
  // The commit button: the first submit button that is not a way out (a
  // `[data-dialog-close]` button with no type is a submit button too).
  const submitButtonOf = form => [...form.elements].find(element => !element.matches('[data-dialog-close]')
    && ((element.tagName === 'BUTTON' && element.type === 'submit')
      || (element.tagName === 'INPUT' && (element.type === 'submit' || element.type === 'image'))));
  const CHOICE_KEYS = new Set(['Home', 'End', 'PageUp', 'PageDown', 'Enter']);
  document.addEventListener('keydown', event => {
    if (!CHOICE_KEYS.has(event.key) || event.defaultPrevented || event.isComposing
      || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const radio = event.target;
    if (!radio || radio.tagName !== 'INPUT' || radio.type !== 'radio'
      || !radio.closest('.ux-choice-list[data-ux-submit]')) return;
    if (event.key === 'Enter') {
      // The browser's own Enter (implicit submission) would be a second
      // submit, and a held key repeats: one press, one submit.
      event.preventDefault();
      const form = radio.form;
      const submit = form ? submitButtonOf(form) : null;
      // Off by itself or by a disabled fieldset around it.
      if (event.repeat || !submit || submit.matches(':disabled')) return;
      form.requestSubmit(submit);
      return;
    }
    const group = choiceGroup(radio);
    const index = group.indexOf(radio);
    if (index < 0) return;
    event.preventDefault();
    const last = group.length - 1;
    const next = group[event.key === 'Home' ? 0 : event.key === 'End' ? last
      : Math.max(0, Math.min(last, index + (event.key === 'PageDown' ? 10 : -10)))];
    const changed = !next.checked;
    next.checked = true;
    next.focus({ preventScroll: true });
    // The radio is a clipped 1px box at its row's top: bring the whole row
    // into view, not only that pixel.
    const row = next.closest('.ux-choice') || next;
    if (typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'nearest' });
    if (changed) {
      next.dispatchEvent(new Event('input', { bubbles: true }));
      next.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });

  // The floating job card: content kept clear of it (scroll padding) reads its
  // height from --ux-job-block.
  if (window.ResizeObserver) {
    const jobBlock = new ResizeObserver(entries => {
      entries.forEach(entry => {
        const card = entry.target;
        const size = card.hidden ? 0 : card.getBoundingClientRect().height;
        document.documentElement.style.setProperty('--ux-job-block', size ? `${Math.ceil(size) + 16}px` : '0px');
      });
    });
    document.querySelectorAll('.ux-job').forEach(card => jobBlock.observe(card));
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

  window.UXControls = {
    wire, close: () => entries.forEach(entry => close(entry)), disclosure, redraw, roving, letterOf,
    dialog: { open: openDialog, close: closeDialog, isOpen: dialogIsOpen },
  };
  // base.html loads this at the end of <body>, so the page's markup is already
  // there: wire it now, and again once parsing ends for anything later (wiring
  // is idempotent per control).
  wire();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => wire());
})();
