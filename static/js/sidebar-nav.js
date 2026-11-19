/* The sidebar's navigation behaviour: the command palette, the icon rail, the
   collapsible section groups and the scroll affordance.

   Thirty-three links in one flat scrolling list is past the point where a user
   finds a page by reading down it. Three things are added here:

   1. A command palette (Ctrl/Cmd+K, or the search row at the top of the nav)
      that fuzzy-matches every destination by name. Its list is read out of the
      RENDERED nav, so the labels are already in the page's language and a link
      this person's role never got is never offered.
   2. An icon rail: the sidebar collapses from 252px to 64px, which is the
      point on the wide data tables and timetable grids. Each item's name is
      drawn as a tooltip on hover and on focus, and stays in the DOM clipped so
      it is still the link's accessible name.
   3. Collapsible section groups, and a shadow at
      whichever edge of the list still has more to scroll.

   Where the state lives. The rail and the collapsed groups are applied as
   attributes on <html> by the anti-flash script in base.html, before any CSS
   paints, and the stylesheet reads those attributes. This file changes the same
   attributes. Staff desktop pages start full width with folded sections.
   Other layouts restore their navigation preferences from localStorage.

   Every user-visible string is written once, in both languages, in
   core/templates/core/partials/sidebar.html, and read from the search row's
   data attributes. This file composes none of them.

   New markup is built with safeHtml (static/js/shared-utils.js), which escapes
   every value it is given; the icons are cloned from the nav's own SVG nodes,
   never re-serialised. */
(function () {
  'use strict';

  const root = document.documentElement;
  const sidebar = document.getElementById('appSidebar');
  if (!sidebar) return;

  const RAIL_KEY = 'sidebar-rail';
  const GROUPS_KEY = 'sidebar-nav-collapsed';
  /* The same shape base.html's anti-flash script accepts, so a tampered or
     stale store can never put anything else into an attribute value. */
  const GROUP_ID = /^[a-z-]+$/;
  const WIDE = '(min-width: 769px)';

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } },
    drop(key) { try { localStorage.removeItem(key); } catch (e) { /* private mode */ } }
  };

  const navSec = document.getElementById('menu');
  const railToggle = document.getElementById('sbRailToggle');
  const findBtn = document.getElementById('navFind');
  const groups = Array.from(sidebar.querySelectorAll('.nav-grp[data-group]'));
  const foldOnLoad = root.hasAttribute('data-sidebar-fold-on-load');

  /* ── Where we are ──────────────────────────────────────────────────────
     `.active` is set server-side or by the sidebar partial's own script. The
     dashboard's hash panels get it from page-dashboard.js, which runs after
     this file, so `whereWeAre` is asked again whenever a nav link's class
     changes. */
  function whereWeAre() {
    return sidebar.querySelector('.nav-a.active, .nav-a.on, .nav-a[aria-current="page"]');
  }

  /* ── Section groups ───────────────────────────────────────────────────── */
  /* Everything the store holds that is shaped like a group name. The key is
     per browser, not per role, so this is a superset of the groups on THIS
     page: a student's sidebar has one group, and reading only that one would
     erase a super admin's on the next save. */
  function storedIds() {
    let stored = [];
    try { stored = JSON.parse(store.get(GROUPS_KEY) || '[]'); } catch (e) { stored = []; }
    if (!Array.isArray(stored)) return [];
    return stored.filter((id) => typeof id === 'string' && GROUP_ID.test(id));
  }

  /* The head script chooses the initial state before paint. */
  function collapsedIds() {
    const known = new Set(groups.map((group) => group.dataset.group));
    return (root.getAttribute('data-sidebar-collapsed') || '').split(' ').filter((id) => known.has(id));
  }

  /* `ids` replaces what this page knows about and leaves every other role's
     groups where they were. */
  function saveCollapsed(ids) {
    const known = new Set(groups.map((group) => group.dataset.group));
    const theirs = storedIds().filter((id) => !known.has(id));
    store.set(GROUPS_KEY, JSON.stringify(theirs.concat(ids)));
  }

  function applyGroups(ids, remember) {
    if (ids.length) root.setAttribute('data-sidebar-collapsed', ids.join(' '));
    else root.removeAttribute('data-sidebar-collapsed');
    groups.forEach((group) => {
      const header = group.querySelector('.nav-lbl');
      if (header) header.setAttribute('aria-expanded', ids.indexOf(group.dataset.group) === -1 ? 'true' : 'false');
    });
    if (remember && !foldOnLoad) saveCollapsed(ids);
  }

  function syncCurrentSection(reveal) {
    const here = whereWeAre();
    const group = here && here.closest('.nav-grp[data-group]');
    if (sidebar.hasAttribute('data-inline-sections')) {
      groups.forEach((section) => {
        const isCurrent = section === group;
        section.classList.toggle('is-current-section', isCurrent);
        const heading = section.querySelector('.nav-lbl');
        if (isCurrent) heading.setAttribute('aria-current', 'location');
        else heading.removeAttribute('aria-current');
      });
    }
    if (!reveal || !group) return;
    const ids = collapsedIds();
    if (ids.includes(group.dataset.group)) {
      applyGroups(ids.filter((id) => id !== group.dataset.group), true);
    }
  }

  groups.forEach((group) => {
    const header = group.querySelector('.nav-lbl');
    if (!header) return;
    header.addEventListener('click', () => {
      const id = group.dataset.group;
      const ids = collapsedIds();
      const at = ids.indexOf(id);
      if (at === -1) ids.push(id);
      else ids.splice(at, 1);
      applyGroups(ids, true);
    });
  });

  /* ── The icon rail ────────────────────────────────────────────────────── */
  function railIsOn() {
    return root.getAttribute('data-sidebar-rail') === '1';
  }

  /* On a phone the sidebar is already a drawer the hamburger opens, and the
     rail's stylesheet rules stop at that breakpoint. */
  function railIsDrawn() {
    return railIsOn() && window.matchMedia(WIDE).matches;
  }

  function syncRail() {
    if (!railToggle) return;
    const on = railIsOn();
    const label = on ? railToggle.dataset.labelExpand : railToggle.dataset.labelCollapse;
    railToggle.setAttribute('aria-expanded', on ? 'false' : 'true');
    if (label) {
      railToggle.setAttribute('aria-label', label);
      railToggle.setAttribute('title', label);
    }
  }

  if (railToggle) {
    railToggle.addEventListener('click', () => {
      if (railIsOn()) {
        root.removeAttribute('data-sidebar-rail');
        if (!foldOnLoad) store.drop(RAIL_KEY);
      } else {
        root.setAttribute('data-sidebar-rail', '1');
        if (!foldOnLoad) store.set(RAIL_KEY, '1');
      }
      syncRail();
      hideTip();
    });
  }

  /* ── The rail's tooltips ───────────────────────────────────────────────
     Drawn on <body>, not inside the item: `.nav-sec` scrolls, and a scroller
     clips both axes, so a tooltip positioned inside it is cut off at the
     rail's edge. It only ever repeats the item's own accessible name, which
     stays in the DOM clipped. */
  let tip = null;
  let tipFor = null;
  let tipTimer = 0;
  /* Escape dismissed it (WCAG 1.4.13); the next pointer move or focus change
     brings tooltips back. */
  let tipMuted = false;

  function tipItem(node) {
    return node && node.closest ? node.closest('.nav-a, .nav-find-btn') : null;
  }

  function tipWords(item) {
    const label = item.querySelector('.nav-t, .nav-find-t');
    return label ? label.textContent.trim() : '';
  }

  function tipNode() {
    if (tip) return tip;
    tip = document.createElement('div');
    tip.className = 'sb-tip';
    tip.setAttribute('role', 'tooltip');
    tip.hidden = true;
    /* The pointer may rest on the tooltip itself without it going away. */
    tip.addEventListener('pointerenter', () => { window.clearTimeout(tipTimer); });
    tip.addEventListener('pointerleave', () => { fadeTip(); });
    document.body.appendChild(tip);
    return tip;
  }

  /* Beside the item, just past the sidebar's inline end — the left of the rail
     when the page reads right to left — and never off the window. */
  function placeTip(item) {
    const node = tipNode();
    const where = item.getBoundingClientRect();
    const rail = sidebar.getBoundingClientRect();
    const box = node.getBoundingClientRect();
    const rtl = root.getAttribute('dir') === 'rtl';
    const inline = rtl ? rail.left - box.width - 8 : rail.right + 8;
    const block = where.top + (where.height - box.height) / 2;
    node.style.left = Math.max(8, Math.min(inline, window.innerWidth - box.width - 8)) + 'px';
    node.style.top = Math.max(8, Math.min(block, window.innerHeight - box.height - 8)) + 'px';
  }

  function showTip(item) {
    if (!railIsDrawn() || tipMuted) return;
    window.clearTimeout(tipTimer);
    const words = tipWords(item);
    if (!words) return;
    const node = tipNode();
    if (item !== tipFor || node.hidden) {
      node.textContent = words;
      node.hidden = false;
      tipFor = item;
    }
    placeTip(item);
  }

  /* The list scrolled or the window changed shape under a tooltip that is up:
     it follows its item rather than going away, and goes away only once the
     item it names has left the list's view. */
  function followTip() {
    if (!tip || tip.hidden || !tipFor) return;
    const where = tipFor.getBoundingClientRect();
    const box = (navSec || sidebar).getBoundingClientRect();
    if (where.bottom <= box.top + 1 || where.top >= box.bottom - 1) { hideTip(); return; }
    placeTip(tipFor);
  }

  function hideTip() {
    window.clearTimeout(tipTimer);
    if (tip) tip.hidden = true;
    tipFor = null;
  }

  function fadeTip() {
    window.clearTimeout(tipTimer);
    /* The pointer leaving an item the keyboard is still on is not a reason to
       take its name away. */
    if (tipFor && tipFor === document.activeElement) return;
    /* A moment's grace, so the pointer can cross the gap onto the tooltip. */
    tipTimer = window.setTimeout(hideTip, 140);
  }

  sidebar.addEventListener('pointerover', (event) => {
    const item = tipItem(event.target);
    if (!item) return;
    /* Escape dismissed the tooltip: moving between an item's own icon and its
       padding is not "asking again", so tooltips come back only once the
       pointer reaches a different item. */
    if (tipMuted && item === tipFor) return;
    tipMuted = false;
    showTip(item);
  });

  sidebar.addEventListener('pointerout', (event) => {
    const item = tipItem(event.target);
    if (!item || item !== tipFor) return;
    const to = event.relatedTarget;
    if (to && (item.contains(to) || (tip && tip.contains(to)))) return;
    fadeTip();
  });

  sidebar.addEventListener('focusin', (event) => {
    const item = tipItem(event.target);
    if (!item) { hideTip(); return; }
    tipMuted = false;
    showTip(item);
  });

  sidebar.addEventListener('focusout', (event) => {
    const item = tipItem(event.target);
    if (item && item === tipFor) hideTip();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !tip || tip.hidden) return;
    tipMuted = true;
    /* `tipFor` is kept: the pointer has not moved, so it is still what the
       dismissal applies to. */
    window.clearTimeout(tipTimer);
    tip.hidden = true;
  });

  /* The page you are on is brought into the list's view — on a short window the
     links below Insights start off-screen. Only the nav scrolls: scrollIntoView
     would move the page behind it too. */
  function revealHere() {
    if (!navSec) return;
    const here = whereWeAre();
    if (!here) return;
    const box = navSec.getBoundingClientRect();
    /* Called once at the bottom of the body and again after the first frame:
       before the window has settled on its height the scroller can measure a
       few pixels tall, and centring against that box lands anywhere. */
    if (box.height < 48) return;
    const item = here.getBoundingClientRect();
    if (item.top >= box.top && item.bottom <= box.bottom) return;
    const slack = navSec.scrollHeight - navSec.clientHeight;
    const want = navSec.scrollTop + item.top - box.top - (box.height - item.height) / 2;
    navSec.scrollTop = Math.max(0, Math.min(want, slack));
  }

  if (navSec) {
    navSec.addEventListener('scroll', followTip, { passive: true });
    window.addEventListener('resize', followTip);
  }

  /* ── The command palette ──────────────────────────────────────────────── */

  /* What a match should ignore: letter case, Latin accents, Arabic diacritics,
     the tatweel, and the alef / ya / ta-marbuta spellings a typist varies.
     NFKD splits a letter from its marks, so one rule removes them all.

     The Arabic code points are built from numbers rather than typed, as
     characters or as unicode escapes: the characters themselves are invisible
     or near-invisible in source, and the tool chain that writes these files
     decodes a typed escape into the character anyway. */
  const CH = String.fromCharCode;
  const ALEF = CH(0x0627);
  const YA = CH(0x064a);
  const HA = CH(0x0647);
  const COMBINING = /\p{M}/gu;
  const TATWEEL = new RegExp(CH(0x0640), 'g');
  const ALEF_FORMS = new RegExp('[' + CH(0x0622) + CH(0x0623) + CH(0x0625) + CH(0x0671) + ']', 'g');
  const ALEF_MAKSURA = new RegExp(CH(0x0649), 'g');
  const TA_MARBUTA = new RegExp(CH(0x0629), 'g');
  const WORD = /[\p{L}\p{N}]/u;
  const SPACES = /\s+/g;

  function fold(text) {
    return String(text)
      .normalize('NFKD')
      .replace(COMBINING, '')
      .replace(TATWEEL, '')
      .replace(ALEF_FORMS, ALEF)
      .replace(ALEF_MAKSURA, YA)
      .replace(TA_MARBUTA, HA)
      .toLowerCase();
  }

  /* Folds a label one character at a time and keeps, for each character of the
     result, which character of the original it came from. Folding the whole
     string can change its length, and a highlight has to land on the
     characters the reader actually sees. */
  function foldWithMap(text) {
    const chars = Array.from(String(text));
    const out = [];
    const from = [];
    for (let i = 0; i < chars.length; i++) {
      const folded = fold(chars[i]);
      for (let k = 0; k < folded.length; k++) { out.push(folded[k]); from.push(i); }
    }
    return { folded: out.join(''), from: from, chars: chars };
  }

  /* A subsequence match, scored so the earliest, tightest, word-starting run
     wins: "secpl" finds "Section Planning". null when a character of the query
     is not there at all. */
  function score(haystack, needle) {
    let want = 0;
    let points = 0;
    let run = 0;
    const hits = [];
    for (let at = 0; at < haystack.length && want < needle.length; at++) {
      if (haystack[at] !== needle[want]) { run = 0; continue; }
      const startsWord = at === 0 || !WORD.test(haystack[at - 1]);
      points += 1 + run * 3 + (startsWord ? 6 : 0) + (at < 12 ? 2 : 0);
      run += 1;
      hits.push(at);
      want += 1;
    }
    return want === needle.length ? { points: points, hits: hits } : null;
  }

  /* Every destination the server drew for this person, read out of the nav. */
  function destinations() {
    return Array.from(sidebar.querySelectorAll('.nav-sec .nav-a[href]')).map((link) => {
      const label = link.querySelector('.nav-t');
      const group = link.closest('.nav-grp[data-group]');
      const header = group ? group.querySelector('.nav-lbl-t') : null;
      return {
        el: link,
        href: link.getAttribute('href') || '',
        label: (label ? label.textContent : link.textContent).trim(),
        section: header ? header.textContent.trim() : '',
        icon: link.querySelector('.i')
      };
    }).filter((item) => item.href && item.label);
  }

  /* The destinations that match, best first. An empty query lists them all, in
     the order the sidebar draws them. */
  function ranked(query) {
    const all = destinations();
    const needle = fold(query).replace(SPACES, '');
    if (!needle) return all.map((item) => ({ item: item, marked: null }));
    const found = [];
    all.forEach((item, order) => {
      const name = foldWithMap(item.label);
      const direct = score(name.folded, needle);
      if (direct) {
        const marked = new Set(direct.hits.map((at) => name.from[at]));
        /* A hit on the name itself always outranks one that needed the group's
           name to match. */
        found.push({ item: item, marked: marked, points: direct.points + 1000, order: order });
        return;
      }
      const wider = score(fold(item.section + ' ' + item.label), needle);
      if (wider) found.push({ item: item, marked: null, points: wider.points, order: order });
    });
    found.sort((a, b) => (b.points - a.points) || (a.order - b.order));
    return found;
  }

  /* The label with its matched characters marked. Built as a list of safeHtml
     pieces, so every character of the label is escaped. */
  function labelPieces(label, marked) {
    const chars = Array.from(String(label));
    if (!marked || !marked.size) return [safeHtml`${label}`];
    const pieces = [];
    let run = '';
    let on = marked.has(0);
    for (let i = 0; i < chars.length; i++) {
      if (marked.has(i) !== on) {
        pieces.push(on ? safeHtml`<mark>${run}</mark>` : safeHtml`${run}`);
        run = '';
        on = marked.has(i);
      }
      run += chars[i];
    }
    if (run) pieces.push(on ? safeHtml`<mark>${run}</mark>` : safeHtml`${run}`);
    return pieces;
  }

  function optionMarkup(hit, index) {
    const label = labelPieces(hit.item.label, hit.marked);
    const section = hit.item.section
      ? safeHtml`<span class="sbp-opt-sec">${hit.item.section}</span>`
      : '';
    return safeHtml`<li class="sbp-opt" role="option" id="sbp-opt-${index}" aria-selected="false"><span class="sbp-opt-t">${label}</span>${section}</li>`;
  }

  const palette = (function () {
    if (!findBtn) return null;

    let shell = null;
    let input = null;
    let panel = null;
    let list = null;
    let empty = null;
    let count = null;
    let shown = [];
    let chosen = 0;
    let cameFrom = null;
    let inerted = [];

    function words(name) {
      return findBtn.dataset[name] || '';
    }

    function build() {
      if (shell) return;
      shell = document.createElement('div');
      shell.className = 'sbp';
      shell.id = 'sbp';
      shell.hidden = true;
      shell.innerHTML = safeHtml`<div class="sbp-backdrop" data-sbp-close="1"></div>
<div class="sbp-panel" role="dialog" aria-modal="true" aria-label="${words('palTitle')}">
  <div class="sbp-field">
    <span class="i" aria-hidden="true"></span>
    <input class="sbp-input" id="sbpInput" type="text" autocomplete="off" spellcheck="false" role="combobox" aria-expanded="true" aria-controls="sbpList" aria-autocomplete="list" aria-label="${words('palTitle')}" placeholder="${words('palPlaceholder')}">
    <kbd class="sbp-esc">esc</kbd>
  </div>
  <ul class="sbp-list" id="sbpList" role="listbox" aria-label="${words('palList')}"></ul>
  <p class="sbp-empty" id="sbpEmpty" hidden>${words('palEmpty')}</p>
  <div class="sbp-foot">${words('palHint')}</div>
  <p class="sr-only" id="sbpCount" role="status" aria-live="polite"></p>
</div>`;
      document.body.appendChild(shell);

      input = shell.querySelector('#sbpInput');
      panel = shell.querySelector('.sbp-panel');
      list = shell.querySelector('#sbpList');
      empty = shell.querySelector('#sbpEmpty');
      count = shell.querySelector('#sbpCount');

      /* The field's magnifier is the search row's own icon, cloned. */
      const glass = findBtn.querySelector('.nav-find-i svg');
      const slot = shell.querySelector('.sbp-field .i');
      if (glass && slot) slot.appendChild(glass.cloneNode(true));

      input.addEventListener('input', () => { render(input.value); });
      /* On the PANEL, not the field: keydown bubbles, so the field is covered
         and so is anything else inside the dialog. Bound on both, every key
         would be handled twice and one arrow press would move two rows. */
      panel.addEventListener('keydown', onKey);
      /* And Escape on the document, because a press only bubbles from where
         the focus IS: once focus has left the dialog altogether the panel
         never sees the key, and the rest of the page is inert. */
      document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape' || !isOpen() || panel.contains(event.target)) return;
        event.preventDefault();
        close();
      });

      /* Keeping the press off the panel keeps the caret in the field. Without
         this, pressing the footer, the empty-state line or the field's own
         padding moves focus to <body>, and since the rest of the page is inert
         the arrow keys and Escape would then do nothing at all. */
      panel.addEventListener('pointerdown', (event) => {
        if (event.target !== input) event.preventDefault();
      });
      list.addEventListener('click', (event) => {
        const option = event.target.closest('.sbp-opt');
        if (!option) return;
        const at = Array.prototype.indexOf.call(list.children, option);
        if (at !== -1) go(at);
      });
      /* pointermove, not pointerover: an arrow key scrolls the list under a
         resting pointer, and the pointerover that fires for that would snap the
         selection straight back to whatever slid under the cursor. */
      list.addEventListener('pointermove', (event) => {
        const option = event.target.closest('.sbp-opt');
        if (!option) return;
        const at = Array.prototype.indexOf.call(list.children, option);
        if (at !== -1 && at !== chosen) choose(at, false);
      });
      shell.addEventListener('pointerdown', (event) => {
        if (event.target.closest('[data-sbp-close]')) close();
      });
    }

    function render(query) {
      shown = ranked(query);
      list.innerHTML = safeHtml`${shown.map(optionMarkup)}`;
      /* The icons are the nav's own nodes, cloned — never markup. */
      Array.prototype.forEach.call(list.children, (option, at) => {
        const icon = shown[at].item.icon;
        if (icon) option.insertBefore(icon.cloneNode(true), option.firstChild);
      });
      empty.hidden = shown.length > 0;
      chosen = 0;
      choose(0, false);
      const many = words('palCountMany').replace('%s', String(shown.length));
      count.textContent = shown.length === 1 ? words('palCountOne') : many;
    }

    function choose(at, scroll) {
      if (!shown.length) {
        chosen = 0;
        input.removeAttribute('aria-activedescendant');
        return;
      }
      chosen = (at + shown.length) % shown.length;
      Array.prototype.forEach.call(list.children, (option, index) => {
        option.setAttribute('aria-selected', index === chosen ? 'true' : 'false');
      });
      const option = list.children[chosen];
      if (option) {
        input.setAttribute('aria-activedescendant', option.id);
        if (scroll) option.scrollIntoView({ block: 'nearest' });
      }
    }

    function go(at) {
      const hit = shown[at];
      if (!hit) return;
      close();
      /* The nav's own link is pressed, so a destination behaves exactly as it
         does from the sidebar — the dashboard's panel router included. */
      hit.item.el.click();
    }

    function onKey(event) {
      if (event.key === 'ArrowDown') { event.preventDefault(); choose(chosen + 1, true); return; }
      if (event.key === 'ArrowUp') { event.preventDefault(); choose(chosen - 1, true); return; }
      if (event.key === 'Home') { event.preventDefault(); choose(0, true); return; }
      if (event.key === 'End') { event.preventDefault(); choose(shown.length - 1, true); return; }
      if (event.key === 'Enter') { event.preventDefault(); go(chosen); return; }
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return; }
      /* Nothing else in the dialog takes focus, so Tab stays in the field. */
      if (event.key === 'Tab') { event.preventDefault(); input.focus(); }
    }

    function isOpen() {
      return Boolean(shell) && !shell.hidden;
    }

    function open() {
      build();
      if (isOpen()) return;
      cameFrom = document.activeElement;
      shell.hidden = false;
      /* The page behind the dialog is taken out of the tab order and out of
         the accessibility tree, as the phone drawer does. */
      inerted = Array.prototype.filter.call(
        document.body.children,
        (node) => node !== shell && !node.hasAttribute('inert')
      );
      inerted.forEach((node) => { node.inert = true; });
      input.value = '';
      render('');
      /* Synchronously, not on the next frame: the field has to take the very
         next keystroke, and a frame's delay drops whatever is typed into it. */
      input.focus();
    }

    function close() {
      if (!isOpen()) return;
      shell.hidden = true;
      inerted.forEach((node) => { node.inert = false; });
      inerted = [];
      if (cameFrom instanceof HTMLElement) cameFrom.focus();
      cameFrom = null;
    }

    return {
      open: open,
      close: close,
      isOpen: isOpen,
      toggle: function () { if (this.isOpen()) this.close(); else this.open(); }
    };
  })();

  if (findBtn && palette) {
    findBtn.addEventListener('click', () => { palette.open(); });

    /* The dashboard already owns Ctrl/Cmd+K: its quick search jumps to a
       student, course or advisor by id (core/templates/core/partials/dashboard/
       _modals.html, bound in _js_i18n.html), and it has no other way in. Two
       handlers on one chord would both fire, so the palette takes the chord
       only where it is free, and the search row stops advertising a shortcut
       that would do something else. Folding the two into one palette is the
       owner's call, not this change's. */
    const chordIsFree = !document.getElementById('cmdPalette');
    const hint = findBtn.querySelector('.nav-find-kbd');
    if (chordIsFree) {
      findBtn.setAttribute('aria-keyshortcuts', 'Control+K Meta+K');
      if (hint && /Mac|iPhone|iPad|iPod/.test(navigator.userAgent)) {
        hint.textContent = String.fromCharCode(0x2318) + ' K';
      }
      document.addEventListener('keydown', (event) => {
        /* `code` as well as `key`: on an Arabic keyboard layout the K key
           reports its Arabic letter, and this app is read in Arabic. `code` is
           the physical key, so Ctrl and the key the row calls "K" opens the
           palette whatever the layout is. */
        if (event.code !== 'KeyK' && event.key !== 'k' && event.key !== 'K') return;
        if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
        /* Not on a page that hides the sidebar outright. The MRI page does, and
           it is off limits: no overlay of ours opens over it. Asked when the key
           is pressed rather than when this binds, because that page marks itself
           from a script that loads after this one. The phone drawer is hidden
           with `visibility`, not `display`, so it still gets the palette. */
        if (window.getComputedStyle(sidebar).display === 'none') return;
        event.preventDefault();
        palette.toggle();
      });
    } else if (hint) {
      hint.remove();
    }
  }

  /* ── The search row's words before its badge ───────────────────────────────
     The row shares its width with the rail toggle, and at the narrower
     sidebar steps the label and the "Ctrl K" badge do not both fit: the
     label was cut to "Go t…". The label says what the button does; the
     badge is a convenience, and the shortcut stays announced through
     `aria-keyshortcuts`. So the badge shows only where the label fits
     beside it. Measured rather than set per breakpoint: the room depends on
     the language, the font that loaded and the rail. The button's own width
     does not depend on the badge (`flex: 1`), so this cannot feed back. */
  const findWords = findBtn && findBtn.querySelector('.nav-find-t');
  const findBadge = findBtn && findBtn.querySelector('.nav-find-kbd');
  if (findWords && findBadge && typeof ResizeObserver === 'function') {
    const fitBadge = () => {
      findBadge.hidden = false;
      /* Where the stylesheet already hides it (the rail, a phone) there is
         nothing to decide, and the label is clipped on purpose there. */
      if (!findBadge.getClientRects().length) return;
      findBadge.hidden = findWords.scrollWidth > findWords.clientWidth;
    };
    new ResizeObserver(fitBadge).observe(findBtn);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitBadge);
  }

  /* ── First run ────────────────────────────────────────────────────────── */
  syncRail();
  applyGroups(collapsedIds(), false);
  syncCurrentSection(!foldOnLoad);
  revealHere();
  /* Once more after the first frame: it reads the laid-out box, and it is a
     no-op when the first reading was already right. */
  requestAnimationFrame(revealHere);

  /* The dashboard marks its current panel after this file runs, and marks a new
     one every time the hash changes. Each of those is an arrival, so the group
     holding it is brought back into view. */
  if (navSec && typeof MutationObserver === 'function') {
    let lastUrl = location.href;
    const marks = new MutationObserver((changes) => {
      /* Only a LINK's own class is an arrival. The dashboard also rewrites its
         sidebar badges' className after a fetch (_js_i18n.html), and those are
         nodes inside this nav: treating one as an arrival would re-open a group
         the user had just deliberately collapsed, and write that to the store. */
      if (!changes.some((change) => change.target.classList && change.target.classList.contains('nav-a'))) return;
      // Initial dashboard highlighting happens after this script. Only a real
      // navigation within the document should open a freshly folded section.
      syncCurrentSection(!foldOnLoad || location.href !== lastUrl);
      lastUrl = location.href;
    });
    marks.observe(navSec, { subtree: true, attributes: true, attributeFilter: ['class', 'aria-current'] });
  }
})();
