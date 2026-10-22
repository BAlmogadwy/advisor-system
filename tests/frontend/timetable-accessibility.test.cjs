/* The design system's menu and tab behaviour (static/js/ux-controls.js) on the
 * real Timetable Workspace markup: every menu opens, navigates, closes and
 * restores focus from the keyboard; only one is open at a time; the inspector's
 * tabs rove. The page script only answers menu items; the shared script owns
 * their state. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/ux-controls.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'core/templates/core/timetable_workspace.html'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const MENUS = ['twNewMenu', 'twMoreMenu', 'twOptimiseMenu', 'twExportMenu', 'twOpenMenu'];

function harness() {
  // Exercise the real markup while removing server template tags and scripts;
  // keep the English copy only, as a rendered English page has it.
  const html = template.replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/{% if LANGUAGE_CODE == 'ar' %}[\s\S]*?{% else %}/g, '')
    .replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only' });
  const { window } = dom;
  const doc = window.document;
  const activations = [];
  for (const id of MENUS) {
    const trigger = doc.getElementById(id);
    trigger.disabled = false;
    // The page answers items; the shared script closes the menu.
    doc.getElementById(trigger.getAttribute('aria-controls')).querySelectorAll('[role="menuitem"]').forEach(item => {
      item.disabled = false;
      item.addEventListener('click', event => {
        event.preventDefault();
        activations.push(item.dataset.mode || item.id);
      });
    });
  }
  doc.querySelectorAll('[role="tablist"]').forEach(list => {
    list.querySelectorAll('[role="tab"]').forEach(tab => tab.addEventListener('click', () => {
      list.querySelectorAll('[role="tab"]').forEach(other => other.classList.remove('active'));
      tab.classList.add('active');
    }));
  });
  window.eval(source);
  const key = (element, value) => element.dispatchEvent(new window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true }));
  return { dom, doc, key, activations };
}

test('Publish is a native button; every menu item is a button or a real link', () => {
  const h = harness();
  assert.equal(h.doc.getElementById('twPublish').tagName, 'BUTTON');
  assert.match(h.doc.getElementById('twPublish').textContent, /Publish/);
  const items = [...h.doc.querySelectorAll('[role="menuitem"]')];
  // New (2), scenario actions (3), Optimise (2), Export (2), Open in (3).
  assert.equal(items.length, 12);
  for (const item of items) {
    if (item.tagName === 'A') assert.match(item.getAttribute('href'), /^\/timetable-workspace\/(mri|graph|split)\/$/);
    else assert.equal(item.tagName, 'BUTTON');
  }
  for (const id of MENUS) {
    const trigger = h.doc.getElementById(id);
    const menu = h.doc.getElementById(trigger.getAttribute('aria-controls'));
    assert.equal(trigger.getAttribute('aria-haspopup'), 'menu', id);
    assert.ok(menu.classList.contains('ux-menu'), id);
    assert.equal(menu.getAttribute('role'), 'menu', id);
    // The inline display is the state; the menu starts closed and hidden.
    assert.equal(menu.style.display, 'none', id);
    assert.equal(menu.getAttribute('aria-hidden'), 'true', id);
    assert.equal(trigger.getAttribute('aria-expanded'), 'false', id);
    assert.ok(trigger.closest('.btn-split, .ux-menu-anchor').contains(menu), id);
  }
  h.dom.window.close();
});

test('menus navigate using arrows and endpoints, Escape restores focus, and a choice fires once', async () => {
  const h = harness();
  const trigger = h.doc.getElementById('twExportMenu');
  const items = [...h.doc.querySelectorAll('#twExportDropdown [role="menuitem"]')];
  trigger.focus();
  h.key(trigger, 'ArrowDown');
  assert.equal(trigger.getAttribute('aria-expanded'), 'true');
  assert.equal(h.doc.getElementById('twExportDropdown').style.display, 'block');
  assert.equal(h.doc.activeElement, items[0]);
  h.key(items[0], 'End');
  assert.equal(h.doc.activeElement, items[1]);
  h.key(items[1], 'ArrowDown');
  assert.equal(h.doc.activeElement, items[0]);
  h.key(items[0], 'Home');
  assert.equal(h.doc.activeElement, items[0]);
  h.key(items[0], 'Escape');
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(h.doc.activeElement, trigger);
  h.key(trigger, 'ArrowUp');
  assert.equal(h.doc.activeElement, items[1]);
  items[1].click();
  await tick();
  assert.deepEqual(h.activations, ['combined']);
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(h.doc.activeElement, trigger);
  h.dom.window.close();
});

test('type-ahead moves to the next item starting with the typed letter', () => {
  const h = harness();
  const trigger = h.doc.getElementById('twMoreMenu');
  h.key(trigger, 'ArrowDown');
  const items = [...h.doc.querySelectorAll('#twMoreDropdown [role="menuitem"]')];
  assert.equal(h.doc.activeElement, items[0]);
  h.key(items[0], 'e');
  assert.equal(h.doc.activeElement, items[1], 'Edit time slots…');
  h.key(items[1], 'e');
  assert.equal(h.doc.activeElement, items[2], 'Elective mapping…');
  h.dom.window.close();
});

test('only one menu stays open, and an outside click or leaving focus closes it', async () => {
  const h = harness();
  const gen = h.doc.getElementById('twNewMenu');
  const opt = h.doc.getElementById('twOptimiseMenu');
  gen.click(); opt.click();
  assert.equal(gen.getAttribute('aria-expanded'), 'false');
  assert.equal(opt.getAttribute('aria-expanded'), 'true');
  h.doc.getElementById('twSearch').focus();
  assert.equal(opt.getAttribute('aria-expanded'), 'false');
  gen.click();
  assert.equal(gen.getAttribute('aria-expanded'), 'true');
  h.doc.getElementById('twSearch').click();
  await tick();
  assert.equal(gen.getAttribute('aria-expanded'), 'false');
  h.dom.window.close();
});

test('a choice that disables its trigger moves focus to the announced status region', async () => {
  const h = harness();
  const trigger = h.doc.getElementById('twOptimiseMenu');
  h.key(trigger, 'ArrowDown');
  const item = h.doc.activeElement;
  item.addEventListener('click', () => { trigger.disabled = true; });
  item.click();
  await tick();
  assert.equal(h.doc.activeElement, h.doc.getElementById('twGenStatus'));
  h.dom.window.close();
});

test('a disabled trigger does not open its menu', () => {
  const h = harness();
  const trigger = h.doc.getElementById('twOptimiseMenu');
  trigger.disabled = true;
  trigger.dispatchEvent(new h.dom.window.MouseEvent('click', { bubbles: true }));
  h.key(trigger, 'ArrowDown');
  assert.equal(h.doc.getElementById('twOptimiseDropdown').style.display, 'none');
  h.dom.window.close();
});

test('header menus drop over the board bar, and every menu stays under sidebars, dialogs and toasts', () => {
  // The header's glass makes it a stacking context: its menus can only paint
  // over the board bar if the header's layer is above the bar's.
  const globalCss = fs.readFileSync(path.join(root, 'static/css/global.css'), 'utf8');
  const pageCss = fs.readFileSync(path.join(root, 'static/css/timetable-workspace.css'), 'utf8');
  const token = name => Number(new RegExp(`${name}:\\s*(\\d+)`).exec(globalCss)[1]);
  const header = Number(/\n\.page-header\s*{[^}]*?z-index:\s*(\d+)/.exec(globalCss)[1]);
  const boardBar = Number(/\n\.tw-cmd-bar\s*{[^}]*?z-index:\s*(\d+)/.exec(pageCss)[1]);
  assert.ok(boardBar < header, `board bar ${boardBar} must sit under the header ${header}`);
  const [, base, plus] = /\n\.ux-menu\s*{[^}]*?z-index:\s*calc\(var\((--z-[\w-]+)\)\s*\+\s*(\d+)\)/.exec(globalCss);
  const menu = token(base) + Number(plus);
  for (const layer of ['--z-sidebar', '--z-drawer', '--z-modal', '--z-toast']) assert.ok(menu < token(layer), layer);
});

test('the inspector tabs use roving focus and update the panel label, including programmatic selection', async () => {
  const h = harness();
  const lists = [...h.doc.querySelectorAll('[role="tablist"]')];
  assert.equal(lists.length, 1, 'board buttons are not a tablist');
  for (const list of lists) {
    assert.ok(list.classList.contains('ux-tabs'));
    const tabs = [...list.querySelectorAll('[role="tab"]')];
    tabs[0].focus();
    h.key(tabs[0], 'End');
    const last = tabs.at(-1);
    assert.equal(h.doc.activeElement, last);
    assert.equal(last.getAttribute('aria-selected'), 'true');
    assert.equal(tabs[0].tabIndex, -1);
    assert.equal(h.doc.getElementById(last.getAttribute('aria-controls')).getAttribute('aria-labelledby'), last.id);
    h.key(last, 'ArrowRight');
    assert.equal(h.doc.activeElement, tabs[0]);
    last.classList.add('active'); tabs[0].classList.remove('active');
    await tick();
    assert.equal(last.getAttribute('aria-selected'), 'true');
  }
  h.dom.window.close();
});

/* ── Disclosure and redraw (global, ux-controls.js) ── */
function bare(storage = true, setup = null) {
  const dom = new JSDOM('<!doctype html><html><body><div id="region" tabindex="0"></div><button id="outside">x</button></body></html>', { runScripts: 'outside-only' });
  if (!storage) {
    Object.defineProperty(dom.window, 'sessionStorage', { get() { throw new dom.window.DOMException('blocked', 'SecurityError'); } });
  }
  if (setup) setup(dom.window);
  dom.window.eval(source);
  return { dom, doc: dom.window.document, UX: dom.window.UXControls, region: dom.window.document.getElementById('region') };
}
const group = (key, open, extra = '') => `<h3><button type="button" class="ux-disclosure-toggle" data-ux-disclosure="${key}" aria-expanded="${open}" aria-controls="r-${key}">`
  + `<span class="ux-disclosure-label">${key}</span><span class="ux-disclosure-chevron"></span></button></h3><div id="r-${key}"${open ? '' : ' hidden'}>${extra}</div>`;

test('a disclosure opens and closes from anywhere on its header and keeps the choice, with or without storage', () => {
  for (const storage of [true, false]) {
    const h = bare(storage);
    const draw = () => h.UX.redraw(h.region, group('a', h.UX.disclosure.isOpen('a', true)));
    draw();
    const toggle = () => h.doc.querySelector('[data-ux-disclosure="a"]');
    toggle().querySelector('.ux-disclosure-chevron').click();  // a click on a part of the header
    assert.deepEqual([toggle().getAttribute('aria-expanded'), h.doc.getElementById('r-a').hidden], ['false', true], `storage ${storage}`);
    assert.equal(h.UX.disclosure.isOpen('a', true), false, `the choice holds (storage ${storage})`);
    draw();
    assert.deepEqual([toggle().getAttribute('aria-expanded'), h.doc.getElementById('r-a').hidden], ['false', true], `redrawn closed (storage ${storage})`);
    h.dom.window.close();
  }
});

test('a redraw keeps the keyboard on the same control and leaves the same content alone', () => {
  const h = bare();
  const html = n => `${group('a', true, `<button data-focus-key="row-1">one</button><button id="named">${n}</button><button>keyless</button>`)}`;
  assert.equal(h.UX.redraw(h.region, html(1)), true);
  const first = h.doc.querySelector('[data-ux-disclosure="a"]');
  first.focus();
  assert.equal(h.UX.redraw(h.region, html(1)), false, 'the same content is not drawn again');
  assert.equal(h.doc.querySelector('[data-ux-disclosure="a"]'), first, 'its nodes are left alone');
  for (const [i, selector] of ['[data-ux-disclosure="a"]', '[data-focus-key="row-1"]', '#named'].entries()) {
    h.doc.querySelector(selector).focus();
    const before = h.doc.activeElement;
    assert.equal(h.UX.redraw(h.region, html(`v${i}`)), true);
    assert.notEqual(h.doc.activeElement, before, 'a new node');
    assert.equal(h.doc.activeElement, h.doc.querySelector(selector), `focus back on ${selector}`);
  }
  // A control with no key, or one that is gone: the region keeps the keyboard.
  [...h.region.querySelectorAll('button')].at(-1).focus();
  h.UX.redraw(h.region, html('keyless'));
  assert.equal(h.doc.activeElement, h.region);
  h.doc.querySelector('#named').focus();
  h.UX.redraw(h.region, '<p>empty</p>');
  assert.equal(h.doc.activeElement, h.region);
  // Focus elsewhere is never taken.
  h.UX.redraw(h.region, html(2));
  h.doc.getElementById('outside').focus();
  h.UX.redraw(h.region, html(3));
  assert.equal(h.doc.activeElement.id, 'outside');
  // Content other code replaced, or added to, is drawn again even when the
  // text to draw matches the last.
  h.region.innerHTML = '<p>other</p><p>two</p>';
  assert.equal(h.UX.redraw(h.region, html(3)), true, 'replaced, same node count');
  h.region.appendChild(h.doc.createElement('p'));
  assert.equal(h.UX.redraw(h.region, html(3)), true, 'added to');
  assert.equal(h.region.querySelectorAll(':scope > p').length, 0);
  h.dom.window.close();
});

test('a control disabled for a moment gets the keyboard back once it is enabled, unless the user moved on', () => {
  const h = bare();
  const html = disabled => `<select id="mark"${disabled ? ' disabled' : ''}><option>A</option></select><button id="other">o</button>`;
  h.UX.redraw(h.region, html(false));
  h.doc.getElementById('mark').focus();
  h.UX.redraw(h.region, html(true));  // saving
  assert.equal(h.doc.activeElement, h.region, 'held by the region meanwhile');
  h.UX.redraw(h.region, html(true) + ' ');  // another redraw while it saves
  assert.equal(h.doc.activeElement, h.region);
  h.UX.redraw(h.region, html(false));  // saved
  assert.equal(h.doc.activeElement, h.doc.getElementById('mark'));
  // Moved on while it saved, inside the region or out of it: the control does
  // not take the keyboard back.
  for (const elsewhere of ['other', 'outside']) {
    h.doc.getElementById('mark').focus();
    h.UX.redraw(h.region, html(true));
    assert.equal(h.doc.activeElement, h.region);
    h.doc.getElementById(elsewhere).focus();
    h.UX.redraw(h.region, html(false));
    assert.equal(h.doc.activeElement, h.doc.getElementById(elsewhere), elsewhere);
  }
  h.dom.window.close();
});

test('a control switched off in place while it has the keyboard gets it back when switched on, unless the user moved on', async () => {
  const h = bare();
  h.UX.redraw(h.region, '<button data-focus-key="lock">Lock</button><button data-focus-key="other">Other</button>');
  const lock = () => h.region.querySelector('[data-focus-key="lock"]');
  // What Chromium does when a page disables the focused button: it lets go of
  // it and reports focusout from the now disabled control, with nowhere to go
  // (jsdom does neither, so the test plays it; a browser test runs the real one).
  const switchOff = button => {
    button.blur();
    button.disabled = true;
    button.dispatchEvent(new h.dom.window.FocusEvent('focusout', { bubbles: true, relatedTarget: null }));
  };
  lock().focus();
  switchOff(lock());
  assert.equal(h.doc.activeElement, h.region, 'the region holds the keyboard');
  lock().disabled = false;  // the write is done: switched on in place
  await tick();
  assert.equal(h.doc.activeElement, lock(), 'back on the control');
  // Leaving for another control is the user's move, even if the control is
  // switched off as it goes.
  lock().focus();
  lock().disabled = true;
  lock().dispatchEvent(new h.dom.window.FocusEvent('focusout', { bubbles: true, relatedTarget: h.region.querySelector('[data-focus-key="other"]') }));
  h.region.querySelector('[data-focus-key="other"]').focus();
  lock().disabled = false;
  await tick();
  assert.equal(h.doc.activeElement.dataset.focusKey, 'other');
  lock().focus();
  h.region.querySelector('[data-focus-key="other"]').focus();
  lock().disabled = true; lock().disabled = false;
  await tick();
  assert.equal(h.doc.activeElement.dataset.focusKey, 'other');
  // Switched off, then the user went elsewhere before it came back.
  lock().focus();
  switchOff(lock());
  h.doc.getElementById('outside').focus();
  lock().disabled = false;
  await tick();
  assert.equal(h.doc.activeElement.id, 'outside');
  // ... and came back to the panel itself later: nothing is still waiting.
  h.region.focus();
  lock().disabled = true; lock().disabled = false;
  await tick();
  assert.equal(h.doc.activeElement, h.region, 'no stale hand-back on a later switch');
  h.UX.redraw(h.region, '<button data-focus-key="lock">Lock</button><button data-focus-key="other">Other!</button>');
  assert.equal(h.doc.activeElement, h.region, 'nor on a later redraw');
  h.dom.window.close();
});

test('a region taken off the page is let go, and watched again when it comes back', async () => {
  // Count the observers the helper keeps watching.
  let live = 0;
  const h = bare(true, w => {
    const Native = w.MutationObserver;
    w.MutationObserver = class extends Native {
      observe(...args) { live++; return super.observe(...args); }
      disconnect() { live--; return super.disconnect(); }
    };
  });
  const html = '<button data-focus-key="lock">Lock</button>';
  h.UX.redraw(h.region, html);
  const parent = h.region.parentNode;
  h.region.remove();
  h.doc.getElementById('outside').focus();  // the next focus anywhere lets the region go
  assert.equal(live, 0, 'nothing watches a region off the page');
  parent.prepend(h.region);
  h.UX.redraw(h.region, html + ' ');
  const lock = () => h.region.querySelector('[data-focus-key="lock"]');
  lock().focus();
  lock().blur();
  lock().disabled = true;
  lock().dispatchEvent(new h.dom.window.FocusEvent('focusout', { bubbles: true, relatedTarget: null }));
  assert.equal(h.doc.activeElement, h.region);
  lock().disabled = false;
  await tick();
  assert.equal(h.doc.activeElement, lock(), 'the hand-back still works after it came back');
  h.dom.window.close();
});

test('a redraw keeps an edit the user has not committed when the field is drawn from the same value', () => {
  const h = bare();
  const html = (mark, extra = '') => `<select id="mark"><option value="">Any</option><option value="AI"${mark === 'AI' ? ' selected' : ''}>AI</option><option value="DS"${mark === 'DS' ? ' selected' : ''}>DS</option></select>`
    + `<input id="name" value="${mark}">${extra}`;
  h.UX.redraw(h.region, html(''));
  const select = () => h.doc.getElementById('mark');
  select().focus();
  select().value = 'DS';  // chosen, not saved yet
  h.UX.redraw(h.region, html('', '<p>another answer</p>'));
  assert.equal(select().value, 'DS', 'the choice survives an unrelated redraw');
  h.UX.redraw(h.region, html('AI', '<p>the saved mark changed</p>'));
  assert.equal(select().value, 'AI', 'a new value from the page wins');
  const input = () => h.doc.getElementById('name');
  input().focus();
  input().value = 'typed';
  h.UX.redraw(h.region, html('AI'));
  assert.deepEqual([input().value, h.doc.activeElement.id], ['typed', 'name']);
  h.dom.window.close();
});

test('a kept edit belongs to the same field, fits it, and skips multiple selects', () => {
  const h = bare();
  const select = (owner, options, extra = '') => `<select id="mark" data-owner="${owner}"${extra}>${options.map(v => `<option value="${v}">${v || 'Any'}</option>`).join('')}</select>`;
  h.UX.redraw(h.region, select('10', ['', 'AI', 'DS']));
  h.doc.getElementById('mark').focus();
  h.doc.getElementById('mark').value = 'DS';
  h.UX.redraw(h.region, select('11', ['', 'AI', 'DS']));
  assert.equal(h.doc.getElementById('mark').value, '', 'another section’s select does not inherit the edit');
  h.doc.getElementById('mark').value = 'DS';
  h.UX.redraw(h.region, select('11', ['', 'AI']));
  assert.equal(h.doc.getElementById('mark').selectedIndex, 0, 'an edit the new select cannot hold is not forced on it (no blank select)');
  h.UX.redraw(h.region, select('11', ['', 'AI', 'DS'], ' multiple'));
  const multi = h.doc.getElementById('mark');
  multi.options[1].selected = true; multi.options[2].selected = true;
  h.UX.redraw(h.region, select('11', ['', 'AI', 'DS'], ' multiple') + ' ');
  assert.deepEqual([...h.doc.getElementById('mark').selectedOptions].map(o => o.value), [], 'a multiple select is drawn as the page says');
  h.dom.window.close();
});

/* ── Roving focus over grids (global, ux-controls.js; design record §7.3, 5a) ── */
function rovingFixture({ rtl = false, stopsAsButtons = false } = {}) {
  const cell = (key, col, inner = '') => `<div role="gridcell" data-focus-key="${key}" data-ux-col="${col}">${inner}</div>`;
  const card = key => stopsAsButtons ? `<button class="c" data-focus-key="${key}">${key}</button>` : `<div class="c" data-focus-key="${key}">${key}</div>`;
  const html = `<!doctype html><html><body>
    <div id="board" tabindex="-1"${rtl ? ' style="direction: rtl"' : ''}>
      <div role="grid" id="A">
        <div role="row"><div role="columnheader">Day</div></div>
        <div role="row"><div role="rowheader">SUN</div>${cell('A:SUN:480', 480, card('p:1') + card('p:2'))}${cell('A:SUN:570', 570)}${cell('A:SUN:660', 660)}</div>
        <div role="row"><div role="rowheader">MON</div>${cell('A:MON:480', 480)}${cell('A:MON:570', 570)}${cell('A:MON:660', 660)}</div>
      </div>
      <div role="grid" id="B">
        <div role="row"><div role="rowheader">SUN</div>${cell('B:SUN:420', 420)}${cell('B:SUN:480', 480)}${cell('B:SUN:900', 900)}</div>
        <div role="row"><div role="rowheader">MON</div>${cell('B:MON:420', 420)}${cell('B:MON:480', 480)}${cell('B:MON:900', 900)}</div>
      </div>
    </div>
    <div id="list" tabindex="-1">
      <div role="grid"><div role="row"><div role="gridcell" data-focus-key="l:1">one</div></div><div role="row"><div role="gridcell" data-focus-key="l:2" aria-disabled="true">two</div></div><div role="row" hidden><div role="gridcell" data-focus-key="l:h">hidden</div></div></div>
      <div role="grid"><div role="row" class="d-none"><div role="gridcell" data-focus-key="l:d">d-none</div></div><div role="row"><div role="gridcell" data-focus-key="l:3">three</div></div><div role="row"><div role="gridcell" data-focus-key="l:4">four</div></div></div>
    </div>
    <button id="outside">outside</button>
  </body></html>`;
  const dom = new JSDOM(html, { runScripts: 'outside-only' });
  const w = dom.window;
  const scrolled = [];
  w.Element.prototype.scrollIntoView = function () { scrolled.push(this.dataset.focusKey || this.id); };
  w.eval(source);
  const doc = w.document;
  const clicks = [];
  const nav = w.UXControls.roving(doc.getElementById('board'), { stop: '.c', activate: stop => clicks.push(stop.dataset.focusKey) });
  const press = (key, init = {}) => {
    const event = new w.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init });
    (doc.activeElement || doc.body).dispatchEvent(event);
    return event;
  };
  const focused = () => doc.activeElement && doc.activeElement.dataset.focusKey;
  const zeros = region => [...region.querySelectorAll('[tabindex="0"]')].map(e => e.dataset.focusKey);
  return { dom, w, doc, nav, press, focused, zeros, clicks, scrolled, board: doc.getElementById('board'), list: doc.getElementById('list') };
}

test('R1 roving: one tab stop; arrows walk cells and the stops stacked in one; Home goes to the row start', () => {
  const h = rovingFixture();
  assert.deepEqual(h.zeros(h.board), ['p:1'], 'one stop, the first');
  h.nav.element().focus();
  h.press('ArrowDown'); assert.equal(h.focused(), 'p:2', 'down walks the stack');
  h.press('ArrowDown'); assert.equal(h.focused(), 'A:MON:480', 'then the next row');
  h.press('ArrowUp'); assert.equal(h.focused(), 'p:2', 'up enters a stack at its last stop');
  h.press('ArrowRight'); assert.equal(h.focused(), 'A:SUN:570', 'right: the next column');
  assert.deepEqual(h.zeros(h.board), ['A:SUN:570'], 'the old stop gave its tabindex up');
  h.press('ArrowLeft'); assert.equal(h.focused(), 'p:1', 'left: the stack, at its clamped index');
  h.press('ArrowDown'); h.press('Home');
  assert.equal(h.focused(), 'p:1', 'Home: the row start, the first stop');
  h.dom.window.close();
});

test('R2 roving: down crosses tables by column time; PageDown keeps the row; Ctrl+Home/End reach the ends', () => {
  const h = rovingFixture();
  h.nav.active('A:MON:660', { focus: true });
  h.press('ArrowDown'); assert.equal(h.focused(), 'B:SUN:480', 'the nearest time, not the same index');
  h.nav.active('A:MON:570', { focus: true });
  h.press('PageDown'); assert.equal(h.focused(), 'B:MON:480', 'PageDown: the next table, same row, nearest time');
  h.press('PageUp'); assert.equal(h.focused(), 'A:MON:480');
  h.press('End', { ctrlKey: true }); assert.equal(h.focused(), 'B:MON:900');
  h.press('Home', { ctrlKey: true }); assert.equal(h.focused(), 'p:1');
  h.press('ArrowUp'); assert.equal(h.focused(), 'p:1', 'the first row stays put');
  h.dom.window.close();
});

test('R3 roving: right-to-left mirrors the arrows', () => {
  const h = rovingFixture({ rtl: true });
  h.nav.active('A:MON:480', { focus: true });
  h.press('ArrowLeft'); assert.equal(h.focused(), 'A:MON:570', 'in Arabic, left is the next time');
  h.press('ArrowRight'); assert.equal(h.focused(), 'A:MON:480');
  h.dom.window.close();
});

test('R4 roving: a redraw keeps the keyboard on the same stop, or where it was; never takes it from elsewhere; never scrolls a passive restore', async () => {
  const h = rovingFixture();
  const grid = h.doc.getElementById('A');
  h.nav.active('A:SUN:570', { focus: true });
  // A redraw moves the stop's content (a new node with the same key) to MON.
  const sun = grid.querySelector('[data-focus-key="A:SUN:570"]');
  sun.setAttribute('data-focus-key', 'gone');
  grid.querySelector('[data-focus-key="A:MON:570"]').innerHTML = '<div class="c" data-focus-key="A:SUN:570">moved</div>';
  sun.remove();
  await tick();
  assert.equal(h.focused(), 'A:SUN:570', 'the moved stop has the keyboard');
  assert.equal(h.doc.activeElement.parentElement.dataset.focusKey, 'A:MON:570');
  assert.deepEqual(h.zeros(h.board), ['A:SUN:570']);
  // The stop is deleted: the one now at its position takes the keyboard.
  h.doc.activeElement.remove();
  await tick();
  assert.equal(h.focused(), 'A:MON:570', 'the stop at the same position');
  // The keyboard elsewhere is never taken.
  h.doc.getElementById('outside').focus();
  grid.querySelector('[data-focus-key="A:MON:660"]').innerHTML = '<div class="c" data-focus-key="p:9">new</div>';
  await tick();
  assert.equal(h.doc.activeElement.id, 'outside');
  // Nor after the user clicked somewhere that takes no focus (the keyboard is
  // nowhere, but it was not left in the board).
  h.nav.element().focus();
  h.doc.body.dispatchEvent(new h.w.Event('pointerdown', { bubbles: true }));
  h.doc.activeElement.blur();
  grid.querySelector('[data-focus-key="A:MON:660"]').innerHTML = '<div class="c" data-focus-key="p:9">again</div>';
  await tick();
  assert.equal(h.doc.activeElement, h.doc.body, 'a redraw does not pull the keyboard back');
  // A key named before it is drawn takes effect once it is.
  h.nav.active('p:11');
  grid.querySelector('[data-focus-key="A:MON:480"]').innerHTML = '<div class="c" data-focus-key="p:11">later</div>';
  await tick();
  assert.deepEqual(h.zeros(h.board), ['p:11']);
  // A restore after a pointer input focuses without scrolling.
  h.nav.element().focus();
  h.scrolled.length = 0;
  h.doc.dispatchEvent(new h.w.Event('pointerdown', { bubbles: true }));
  h.doc.activeElement.blur();
  h.nav.restore();
  assert.equal(h.focused(), 'p:11');
  assert.deepEqual(h.scrolled, [], 'a passive restore never scrolls');
  h.dom.window.close();
});

test('R5 roving: letters by key or by key position, never punctuation; Enter activates once; a handler can decline', () => {
  const h = rovingFixture();
  const L = h.w.UXControls.letterOf;
  const k = (key, code) => ({ key, code });
  assert.deepEqual([L(k('ة', 'KeyM')), L(k('m', 'Semicolon')), L(k(',', 'KeyM')), L(k('p', 'KeyR')), L(k('1', 'KeyM'))], ['M', 'M', '', 'P', '']);
  const fired = [];
  h.nav.active('p:1', { focus: true });
  h.w.UXControls.roving(h.board, { keys: { M: stop => { fired.push(stop.dataset.focusKey); }, R: () => false } });
  h.press('m', { code: 'KeyM' });
  for (const mod of [{ ctrlKey: true }, { shiftKey: true }, { altKey: true }, { repeat: true }]) h.press('m', { code: 'KeyM', ...mod });
  assert.deepEqual(fired, ['p:1'], 'once, and never with a modifier or a repeat');
  assert.equal(h.press('r', { code: 'KeyR' }).defaultPrevented, false, 'a handler returning false leaves the key alone');
  h.press('Enter'); h.press('Enter', { repeat: true }); h.press(' ');
  assert.deepEqual(h.clicks, ['p:1', 'p:1'], 'Enter and Space, never a repeat');
  h.dom.window.close();
  // A native control as the stop: the browser activates it, not the controller.
  const b = rovingFixture({ stopsAsButtons: true });
  b.nav.active('p:1', { focus: true });
  b.press('Enter');
  assert.deepEqual(b.clicks, [], 'no synthetic activation of a native control');
  b.dom.window.close();
});

test('R6 roving: a list has Home and End over the whole list, leaves left and right alone, reaches disabled rows and skips hidden ones', () => {
  const h = rovingFixture();
  const nav = h.w.UXControls.roving(h.list, {});
  nav.element().focus();
  assert.equal(h.focused(), 'l:1');
  h.press('ArrowDown'); assert.equal(h.focused(), 'l:2', 'a disabled row is reachable');
  h.press('ArrowDown'); assert.equal(h.focused(), 'l:3', 'hidden and d-none rows are skipped');
  h.press('Home'); assert.equal(h.focused(), 'l:1');
  h.press('End'); assert.equal(h.focused(), 'l:4');
  assert.equal(h.press('ArrowLeft').defaultPrevented, false);
  assert.equal(h.press('ArrowRight').defaultPrevented, false);
  h.dom.window.close();
});

/* D1 (design record §7.2 item 6, 6c): showing or hiding rows changes no
   node, so a region's roving hears it from the disclosure's event alone —
   and only from that: an attribute changing (a tone, a dim) never makes it
   read its stops again. */
test('D1 a disclosure says it opened or shut with one bubbling event; a roving region reads its stops again on it, never on attributes', async () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div id="board" tabindex="-1">
      <div role="grid">
        <div role="row"><div role="gridcell"><button type="button" class="ux-disclosure-toggle s" data-focus-key="fold" tabindex="-1" aria-expanded="false" aria-controls="rows">Fold</button></div></div>
        <div role="rowgroup" id="rows" hidden>
          <div role="row"><div role="gridcell"><button type="button" class="s" data-focus-key="in:1">one</button></div></div>
        </div>
      </div>
    </div>
    <button type="button" class="ux-disclosure-toggle" data-ux-disclosure="k" aria-expanded="true" aria-controls="kept">Kept</button><div id="kept"></div>
  </body></html>`, { runScripts: 'outside-only' });
  const w = dom.window, doc = w.document;
  w.eval(source);
  const events = [];
  doc.addEventListener('ux-disclosure', event => events.push({ from: event.target.dataset.focusKey || event.target.dataset.uxDisclosure, bubbles: event.bubbles, ...event.detail }));
  w.UXControls.roving(doc.getElementById('board'), { stop: '.s' });
  const inner = doc.querySelector('[data-focus-key="in:1"]');
  const toggle = doc.querySelector('[data-focus-key="fold"]');
  assert.equal(inner.hasAttribute('data-ux-roving'), false, 'hidden: not a stop');
  toggle.click();
  assert.deepEqual(events, [{ from: 'fold', bubbles: true, open: true, key: null }], 'one event, from the toggle, bubbling');
  // Read again at once, with no node changed: the button just shown is one
  // of the region's stops, never a Tab stop of its own.
  assert.deepEqual([inner.getAttribute('data-ux-roving'), inner.tabIndex], ['', -1]);
  toggle.focus();
  toggle.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }));
  assert.equal(doc.activeElement, inner, '↓ goes into the rows just shown');
  // An attribute is not a reason to read the stops again.
  toggle.focus();
  inner.removeAttribute('data-ux-roving');
  inner.setAttribute('data-dim', 'filter');
  inner.classList.add('critical');
  inner.title = 'Blocks publishing';
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(inner.hasAttribute('data-ux-roving'), false, 'no refresh on attributes');
  toggle.click();
  assert.equal(inner.hasAttribute('data-ux-roving'), false, 'shut: not a stop');
  assert.equal(events[1].open, false);
  // A kept disclosure (its key remembered) says its key.
  doc.querySelector('[data-ux-disclosure="k"]').click();
  assert.deepEqual(events[2], { from: 'k', bubbles: true, open: false, key: 'k' });
  assert.equal(events.length, 3);
  dom.window.close();
});

test('R7 roving: restore gives the keyboard back to the active stop after the user was elsewhere', () => {
  const h = rovingFixture();
  h.nav.active('A:SUN:570', { focus: true });
  const outside = h.doc.getElementById('outside');
  outside.focus();
  outside.remove();
  h.doc.querySelector('[data-focus-key="A:SUN:570"]').remove();
  h.nav.restore();
  assert.ok(h.focused(), 'something in the board');
  assert.equal(h.board.contains(h.doc.activeElement), true);
  h.dom.window.close();
});

test('R8 roving: the keys line shows while the keyboard is in the region, not after a mouse focus', async () => {
  const h = rovingFixture();
  const hint = h.doc.createElement('p');
  hint.hidden = true;
  h.doc.body.appendChild(hint);
  h.w.UXControls.roving(h.board, { hint });
  h.doc.dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
  h.nav.element().focus();
  assert.equal(hint.hidden, false, 'shown on a keyboard focus');
  h.doc.getElementById('outside').focus();
  assert.equal(hint.hidden, true, 'hidden on leaving');
  h.doc.dispatchEvent(new h.w.Event('pointerdown', { bubbles: true }));
  h.nav.element().focus();
  assert.equal(hint.hidden, true, 'not shown on a mouse focus');
  // The line takes room: the stop the keyboard is on is scrolled back into view.
  h.doc.getElementById('outside').focus();
  await new Promise(resolve => setTimeout(resolve, 30));  // the first showing's scroll lands
  h.scrolled.length = 0;
  h.doc.dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
  h.nav.element().focus();
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.deepEqual(h.scrolled, [h.nav.active()], 'kept in view as the line appears');
  h.dom.window.close();
});

/* ── The panes' keys by position (timetable-workspace-panes.js) ── */
function panesHarness() {
  const h = harness();
  h.dom.window.eval(fs.readFileSync(path.join(root, 'static/js/timetable-workspace-panes.js'), 'utf8'));
  const workspace = h.doc.getElementById('twWorkspace');
  const press = (init, target = h.doc.body) => target.dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init }));
  return { ...h, workspace, press };
}

test('K1 the pane keys work by position on any layout, and never with Shift on another key, a repeat, Ctrl, in a field or under a dialog', () => {
  const h = panesHarness();
  const left = () => h.workspace.dataset.left, right = () => h.workspace.dataset.right;
  const startLeft = left(), startRight = right();
  h.press({ key: 'ج', code: 'BracketLeft' });
  assert.notEqual(left(), startLeft, 'Arabic layout: ج on BracketLeft');
  h.press({ key: 'د', code: 'BracketRight' });
  assert.notEqual(right(), startRight, 'Arabic layout: د on BracketRight');
  const now = left();
  h.press({ key: '[', code: 'KeyF', shiftKey: true });
  assert.notEqual(left(), now, 'a layout that types [ elsewhere');
  const before = [left(), right()];
  h.press({ key: '{', code: 'BracketLeft', shiftKey: true });
  h.press({ key: '[', code: 'BracketLeft', repeat: true });
  h.press({ key: '[', code: 'BracketLeft', ctrlKey: true });
  h.press({ key: '[', code: 'BracketLeft' }, h.doc.getElementById('twSectionSearch'));
  const dialog = h.doc.createElement('dialog'); dialog.setAttribute('open', ''); h.doc.body.appendChild(dialog);
  h.press({ key: '[', code: 'BracketLeft' });
  assert.deepEqual([left(), right()], before);
  h.dom.window.close();
});

test('K2 collapsing the pane that has the keyboard moves the keyboard to its rail', () => {
  const h = panesHarness();
  if (h.workspace.dataset.left !== 'open') h.press({ key: '[', code: 'BracketLeft' });
  const row = h.doc.createElement('div');
  row.tabIndex = 0;
  h.doc.getElementById('twSectionList').appendChild(row);
  row.focus();
  h.press({ key: 'ج', code: 'BracketLeft' }, row);
  assert.equal(h.workspace.dataset.left, 'collapsed');
  assert.equal(h.doc.activeElement, h.doc.querySelector('#twLeft .tw-rail'));
  h.dom.window.close();
});

test('R9 roving: a new region starts where initial() says; a cell that gains cards gives its stop up; a list pages by group start', () => {
  const h = rovingFixture();
  const nav = h.w.UXControls.roving(h.doc.getElementById('list'), {});
  // initial() is read for a region seen for the first time (a fresh one).
  const fresh = h.doc.createElement('div');
  fresh.innerHTML = '<div role="grid"><div role="row"><div role="gridcell" data-focus-key="f:1">1</div><div role="gridcell" data-focus-key="f:2">2</div></div></div>';
  h.doc.body.appendChild(fresh);
  h.w.UXControls.roving(fresh, { initial: () => 'f:2' });
  assert.deepEqual(h.zeros(fresh), ['f:2']);
  // A cell that gains stops is no longer one: its tabindex goes.
  h.nav.active('A:SUN:570', { focus: true });
  const cell = h.doc.querySelector('[data-focus-key="A:SUN:570"]');
  cell.insertAdjacentHTML('beforeend', '<div class="c" data-focus-key="p:5">five</div>');
  h.nav.refresh();
  assert.equal(cell.hasAttribute('tabindex'), false, 'the cell gave its stop up');
  assert.equal(h.doc.querySelector('[data-focus-key="p:5"]').getAttribute('tabindex'), '0', 'its card has it');
  // In a list, PageDown goes to the next group's first row, wherever it was.
  nav.active('l:2', { focus: true });
  h.press('PageDown');
  assert.equal(h.focused(), 'l:3');
  h.dom.window.close();
});

test('R10 roving: keys are left alone with Ctrl (but Home/End), while composing, or when something else took them; a pending key does not wait for ever', async () => {
  const h = rovingFixture();
  h.nav.active('p:1', { focus: true });
  assert.equal(h.press('ArrowDown', { ctrlKey: true }).defaultPrevented, false, 'Ctrl+arrow is not the board’s');
  assert.equal(h.focused(), 'p:1');
  assert.equal(h.press('ArrowDown', { isComposing: true }).defaultPrevented, false, 'an IME composition owns its keys');
  const taken = new h.w.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true });
  taken.preventDefault();
  h.doc.activeElement.dispatchEvent(taken);
  assert.equal(h.focused(), 'p:1', 'a key another handler took is not moved on');
  // A key named but never drawn is dropped after two redraws.
  h.nav.active('p:99');
  const grid = h.doc.getElementById('A');
  grid.querySelector('[data-focus-key="A:MON:660"]').innerHTML = '<div class="c" data-focus-key="p:7">7</div>';
  h.nav.refresh();
  h.nav.refresh();
  grid.querySelector('[data-focus-key="A:MON:570"]').innerHTML = '<div class="c" data-focus-key="p:99">late</div>';
  h.nav.refresh();
  assert.notEqual(h.zeros(h.board)[0], 'p:99', 'a stale key no longer takes the stop');
  h.dom.window.close();
});

test('K3 the pane keys read the key typed first; only another script’s letter falls back to the position; opening from the rail keeps the keyboard', () => {
  const h = panesHarness();
  const left = () => h.workspace.dataset.left, right = () => h.workspace.dataset.right;
  const before = [left(), right()];
  h.press({ key: ']', code: 'BracketLeft' });  // Hebrew: ] on the left bracket key
  assert.deepEqual([left(), right() !== before[1]], [before[0], true], 'the key typed decides');
  const now = [left(), right()];
  h.press({ key: 'ü', code: 'BracketLeft' });  // German: a Latin letter there is typing
  h.press({ key: 'ج', code: 'BracketLeft', isComposing: true });
  assert.deepEqual([left(), right()], now);
  // Collapsed list, keyboard on its rail: opening it puts the keyboard on its collapse button.
  if (left() !== 'collapsed') h.press({ key: '[', code: 'BracketLeft' });
  const rail = h.doc.querySelector('#twLeft .tw-rail');
  rail.focus();
  h.press({ key: '[', code: 'BracketLeft' }, rail);
  assert.equal(left(), 'open');
  assert.equal(h.doc.activeElement, h.doc.querySelector('#twLeft .tw-pane-collapse'));
  h.dom.window.close();
});

test('R11 roving: a pending key survives a redraw that misses it once; the keys line scrolls whatever has the keyboard, once', async () => {
  const h = rovingFixture();
  const grid = h.doc.getElementById('A');
  h.nav.active('p:1', { focus: true });
  h.nav.active('p:98');
  h.nav.refresh();  // one miss
  grid.querySelector('[data-focus-key="A:MON:570"]').innerHTML = '<div class="c" data-focus-key="p:98">late</div>';
  h.nav.refresh();
  assert.deepEqual(h.zeros(h.board), ['p:98'], 'still waiting after one miss');
  // Keys line: one scroll when it appears; none for a later keyboard focus.
  const hint = h.doc.createElement('p');
  hint.hidden = true;
  h.doc.body.appendChild(hint);
  h.w.UXControls.roving(h.board, { hint });
  h.doc.getElementById('outside').focus();
  await new Promise(resolve => setTimeout(resolve, 30));
  h.doc.dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
  h.nav.element().focus();
  h.press('ArrowRight');  // moved on within the frame
  h.scrolled.length = 0;
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.deepEqual(h.scrolled, [h.focused()], 'the stop that has the keyboard now, once');
  h.dom.window.close();
});

test('K4 the pane keys never take Shift with another script’s letter', () => {
  const h = panesHarness();
  const before = h.workspace.dataset.left;
  h.press({ key: 'ج', code: 'BracketLeft', shiftKey: true });
  assert.equal(h.workspace.dataset.left, before);
  h.dom.window.close();
});

/* ── Dialogs and choice lists (UXControls.dialog; design record §7.3, 5c) ── */
const DIALOG = `<button id="opener" type="button">Change…</button>
  <dialog id="dlg" class="ux-dialog ux-dialog-scroll" aria-labelledby="dlgTitle"><form method="dialog" novalidate>
    <div class="ux-dialog-head"><h2 id="dlgTitle">Change room</h2>
      <button type="button" id="dlgX" data-dialog-close aria-label="Close"><span>×</span></button></div>
    <div class="ux-dialog-body" id="dlgBody" tabindex="-1"><input id="dlgFilter" type="search"></div>
    <div class="ux-dialog-foot"><button type="button" id="dlgCancel" data-dialog-close>Cancel</button>
      <button type="submit" id="dlgOk">Assign room</button></div>
  </form></dialog>
  <div id="spares"><button id="off" disabled>off</button><div hidden><button id="inHidden">h</button></div>
    <div class="d-none"><button id="inDNone">d</button></div></div>`;
function dialogFixture({ modal = false } = {}) {
  const h = bare(true, w => {
    if (!modal) return;
    // A browser's dialog: showModal, and close() whose `close` event comes
    // a moment later.
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
    w.HTMLDialogElement.prototype.close = function () {
      if (!this.hasAttribute('open')) return;
      this.removeAttribute('open');
      setTimeout(() => this.dispatchEvent(new w.Event('close')), 0);
    };
  });
  h.doc.body.insertAdjacentHTML('beforeend', DIALOG);
  const closes = [];
  h.doc.addEventListener('ux-dialog-close', event => closes.push(event.detail.reason));
  const $ = id => h.doc.getElementById(id);
  const key = (target, init) => target.dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init }));
  // The page redraws the control that opened the dialog: a new node, same id.
  const redrawOpener = () => { const fresh = $('opener').cloneNode(true); $('opener').replaceWith(fresh); return fresh; };
  return { ...h, dlg: $('dlg'), closes, $, key, redrawOpener };
}

test('G1 a helper dialog without showModal opens on its initial focus; its buttons and Escape close it; focus returns to the opener as it is at close', () => {
  const h = dialogFixture();
  const D = h.UX.dialog;
  h.$('opener').focus();
  const options = { initialFocus: () => h.$('dlgBody'), returnFocus: () => h.$('opener') };
  assert.equal(D.open(h.dlg, options), true);
  assert.deepEqual([h.dlg.hasAttribute('open'), h.dlg.dataset.uxDialog, D.isOpen(h.dlg)], [true, 'fallback', true]);
  assert.equal(h.doc.activeElement, h.$('dlgBody'), 'initialFocus');
  assert.equal(D.open(h.dlg, options), false, 'one helper dialog at a time');
  // The × (a click on its icon), with the opener redrawn while it was open.
  let opener = h.redrawOpener();
  h.$('dlgX').querySelector('span').click();
  assert.deepEqual([h.dlg.hasAttribute('open'), 'uxDialog' in h.dlg.dataset, D.isOpen(h.dlg)], [false, false, false]);
  assert.equal(h.doc.activeElement, opener, 'the opener drawn now, not the one at open');
  assert.deepEqual(h.closes, ['cancel']);
  // No initialFocus: the first control that takes focus. Escape closes.
  assert.equal(D.open(h.dlg, { returnFocus: () => h.$('opener') }), true);
  assert.equal(h.doc.activeElement, h.$('dlgX'));
  h.$('dlgFilter').focus();
  opener = h.redrawOpener();
  assert.equal(h.key(h.$('dlgFilter'), { key: 'Escape' }), false, 'Escape is used');
  assert.equal(h.dlg.hasAttribute('open'), false);
  assert.equal(h.doc.activeElement, opener);
  // An Escape a control inside used (a filter clearing its text) leaves it open; Cancel closes.
  D.open(h.dlg, options);
  h.$('dlgFilter').addEventListener('keydown', event => event.preventDefault(), { once: true });
  h.key(h.$('dlgFilter'), { key: 'Escape' });
  assert.equal(D.isOpen(h.dlg), true);
  h.$('dlgCancel').click();
  assert.equal(D.isOpen(h.dlg), false);
  // The page's own close: reason 'close'; a second close does nothing.
  D.open(h.dlg, options);
  D.close(h.dlg);
  D.close(h.dlg);
  assert.deepEqual(h.closes, ['cancel', 'cancel', 'cancel', 'close']);
  assert.equal(h.doc.activeElement, h.$('opener'));
  h.dom.window.close();
});

test('G2 the browser’s cancel is stopped and closes the dialog once; a native close no cancel began does the same bookkeeping, as a close', async () => {
  const h = dialogFixture({ modal: true });
  const D = h.UX.dialog;
  const options = { initialFocus: () => h.$('dlgBody'), returnFocus: () => h.$('opener') };
  assert.equal(D.open(h.dlg, options), true);
  assert.equal('uxDialog' in h.dlg.dataset, false, 'modal: showModal, no fallback');
  // Modal: the browser turns Escape into `cancel`; the keydown alone closes nothing.
  h.key(h.$('dlgBody'), { key: 'Escape' });
  assert.equal(D.isOpen(h.dlg), true);
  const opener = h.redrawOpener();
  const cancel = new h.dom.window.Event('cancel', { cancelable: true });
  h.dlg.dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented, true);
  assert.deepEqual([h.dlg.hasAttribute('open'), D.isOpen(h.dlg)], [false, false]);
  assert.equal(h.doc.activeElement, opener);
  await new Promise(resolve => setTimeout(resolve, 5));  // the browser's own `close` event, after ours
  assert.deepEqual(h.closes, ['cancel'], 'one close');
  // Closed natively with no cancel first (page code calling dialog.close(), as
  // a `<form method="dialog">` submitted does): the bookkeeping, as a close,
  // not as the user's cancel.
  D.open(h.dlg, options);
  h.dlg.close();
  assert.equal(D.isOpen(h.dlg), false, 'closed, though not by the helper yet');
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(h.doc.activeElement, h.$('opener'));
  assert.deepEqual(h.closes, ['cancel', 'close']);
  // A late `close` from an earlier closing finds the dialog open again: not its own.
  D.open(h.dlg, options);
  h.dlg.dispatchEvent(new h.dom.window.Event('close'));
  assert.equal(D.isOpen(h.dlg), true);
  assert.deepEqual(h.closes, ['cancel', 'close']);
  h.dom.window.close();
});

test('G3 a focus target that cannot take focus is skipped quietly; dialogs the helper did not open keep their own behaviour', async () => {
  const h = dialogFixture();
  const D = h.UX.dialog;
  const detached = h.doc.createElement('button');
  for (const target of [h.$('off'), detached, h.$('inHidden'), h.$('inDNone'), null]) {
    D.open(h.dlg, { returnFocus: () => target });
    h.$('dlgFilter').focus();
    assert.doesNotThrow(() => D.close(h.dlg));
    assert.equal(h.doc.activeElement, h.$('dlgFilter'), `focus not given to ${target ? target.id || 'a detached button' : 'nothing'}`);
    assert.equal(D.isOpen(h.dlg), false);
  }
  assert.equal(h.closes.length, 5, 'each close completed');
  h.dom.window.close();

  // The real Generate dialog, wired as the page wires it (no showModal in jsdom).
  const page = harness();
  const w = page.dom.window;
  const generate = page.doc.getElementById('twGenerateDialog');
  const reasons = [];
  page.doc.addEventListener('ux-dialog-close', event => reasons.push(event.detail.reason));
  generate.querySelectorAll('[data-dialog-close]').forEach(button => button.addEventListener('click', () => generate.removeAttribute('open')));
  generate.setAttribute('open', '');
  const radio = generate.querySelector('input[name="twGenEngine"]');
  radio.focus();
  assert.equal(w.UXControls.dialog.open(generate), false, 'open by its page: not the helper’s');
  assert.equal(page.key(radio, 'Escape'), true, 'Escape is not taken');
  const cancel = new w.Event('cancel', { cancelable: true });
  generate.dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented, false, 'the browser may close it natively');
  w.UXControls.dialog.close(generate);
  assert.equal(generate.hasAttribute('open'), true, 'still the page’s to close');
  generate.querySelector('.ux-dialog-head [data-dialog-close]').click();
  assert.equal(generate.hasAttribute('open'), false, 'its × closes it through the page');
  // The English chooser: its own cancel handler answers Escape.
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  w.eval(fs.readFileSync(path.join(root, 'static/js/timetable-meeting-policy.js'), 'utf8'));
  const choice = w.TimetableMeetingPolicy.choose('ENG101');
  const chooser = page.doc.querySelector('dialog.tw-pattern-dialog');
  chooser.dispatchEvent(new w.Event('cancel', { cancelable: true }));
  assert.equal(await choice, null);
  assert.deepEqual(reasons, [], 'the helper never ran');
  page.dom.window.close();
});

test('C1 a choice list with data-ux-submit: Home, End and PageUp/PageDown (clamped) skip hidden rows and bring the row into view; Enter submits once, through the commit button, only while it is on', () => {
  const scrolled = [];
  const h = bare(true, w => { w.Element.prototype.scrollIntoView = function () { scrolled.push(this); }; });
  const row = (id, name, { dNone = false, hidden = false } = {}) => `<label class="ux-choice${dNone ? ' d-none' : ''}"${hidden ? ' hidden' : ''}>`
    + `<input type="radio" name="${name}" id="${id}" value="${id}"><span class="ux-choice-title">${id}</span></label>`;
  const rows = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => from + i)
    .map(n => row(`r${n}`, 'pick', { dNone: n === 4, hidden: n === 13 })).join('');
  // As a real choosing dialog's form: a leading type=button (its ×) and a
  // way out with no type (itself a submit button) before the commit button.
  h.doc.body.insertAdjacentHTML('beforeend', `<form id="f" novalidate><button type="button" id="x">×</button>
      <fieldset class="ux-choice-list" data-ux-submit>${rows(0, 6)}</fieldset>
      <fieldset class="ux-choice-list" data-ux-submit>${rows(7, 13)}</fieldset>
      ${row('o1', 'other')}<button id="bareClose" data-dialog-close>Cancel</button><button type="submit" id="go">Assign</button></form>
    <form id="g" novalidate><div class="ux-choice-list">${row('s0', 'plain')}${row('s1', 'plain')}</div><button type="submit">Go</button></form>`);
  const $ = id => h.doc.getElementById(id);
  const changes = [];
  h.doc.addEventListener('change', event => changes.push(event.target.id));
  const submits = { f: [], g: [] };
  for (const id of ['f', 'g']) $(id).addEventListener('submit', event => { event.preventDefault(); submits[id].push(event.submitter && event.submitter.id); });
  const press = (target, key, init = {}) => target.dispatchEvent(new h.dom.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init }));
  const step = (from, key, expected) => {
    $(from).focus();
    scrolled.length = 0;
    assert.equal(press($(from), key), false, `${key} is used`);
    const checked = h.doc.querySelector('#f input[name="pick"]:checked');
    assert.deepEqual([checked && checked.id, h.doc.activeElement.id], [expected, expected], `${key} from ${from}`);
    // The radio is a clipped pixel: the whole row is what comes into view.
    assert.deepEqual(scrolled, [$(expected).closest('label.ux-choice')], `${key} from ${from}: the row scrolls into view`);
  };
  // Visible rows, in order: r0–r3, r5–r12 (r4 is .d-none, r13 hidden), across both lists.
  step('r2', 'End', 'r12');
  step('r12', 'Home', 'r0');
  step('r0', 'PageDown', 'r11');
  step('r11', 'PageDown', 'r12');
  step('r12', 'PageUp', 'r1');
  step('r1', 'PageUp', 'r0');
  step('r0', 'Home', 'r0');
  assert.deepEqual(changes, ['r12', 'r0', 'r11', 'r12', 'r1', 'r0'], 'a change for each new choice, none for the same one');
  assert.equal(press($('r0'), 'ArrowDown'), true, 'the arrows stay the browser’s');
  // Enter: one submit per press, with the commit button as its submitter (not
  // the × before it, nor the way out with no type).
  assert.equal(press($('r0'), 'Enter'), false);
  press($('r0'), 'Enter', { repeat: true });
  assert.deepEqual(submits.f, ['go']);
  $('go').disabled = true;
  assert.equal(press($('r0'), 'Enter'), false, 'Enter is still used');
  assert.deepEqual(submits.f, ['go'], 'nothing submitted while the button is off');
  // Off through a disabled fieldset around it is off too.
  $('go').disabled = false;
  const wrap = h.doc.createElement('fieldset');
  wrap.disabled = true;
  $('go').before(wrap);
  wrap.append($('go'));
  assert.equal(press($('r0'), 'Enter'), false);
  assert.deepEqual(submits.f, ['go'], 'nothing submitted while a disabled fieldset holds the button');
  wrap.disabled = false;
  press($('r0'), 'Enter');
  assert.deepEqual(submits.f, ['go', 'go'], 'and it submits once the fieldset is on');
  // A list without data-ux-submit: neither Enter nor Home/End.
  $('s0').focus();
  assert.equal(press($('s0'), 'Enter'), true);
  assert.equal(press($('s0'), 'End'), true);
  assert.deepEqual([submits.g, $('s1').checked], [[], false]);
  h.dom.window.close();
});

test('G4 opening a helper dialog closes an open menu; isOpen answers only for a helper dialog that is still on the page', async () => {
  const h = dialogFixture();
  const D = h.UX.dialog;
  h.doc.body.insertAdjacentHTML('beforeend', `<div class="ux-menu-anchor"><button type="button" id="more" aria-haspopup="menu" aria-controls="moreMenu">More</button>
      <div id="moreMenu" class="ux-menu" role="menu" style="display:none"><button type="button" role="menuitem">One</button></div></div>
    <dialog id="pageOwn" open><button type="button">The page’s own</button></dialog>
    <dialog id="next"><button type="button" id="nextBtn">Next</button></dialog>`);
  h.UX.wire();
  // A pointer opens the menu: the keyboard stays where it was, outside it, so
  // no focus leaving the menu closes it on the way.
  h.$('more').dispatchEvent(new h.dom.window.MouseEvent('click', { bubbles: true, detail: 1 }));
  assert.deepEqual([h.$('moreMenu').style.display, h.$('more').getAttribute('aria-expanded')], ['block', 'true']);
  assert.equal(D.open(h.dlg, { returnFocus: () => h.$('opener') }), true);
  assert.deepEqual([h.$('moreMenu').style.display, h.$('more').getAttribute('aria-expanded')], ['none', 'false'], 'the menu closed as the dialog opened');
  // Open by its page: not the helper's.
  assert.equal(h.$('pageOwn').hasAttribute('open'), true);
  assert.equal(D.isOpen(h.$('pageOwn')), false, 'open, but not the helper’s');
  // Taken off the page while open: not open, and it does not block the next.
  h.dlg.remove();
  assert.equal(h.dlg.hasAttribute('open'), true);
  assert.equal(D.isOpen(h.dlg), false, 'off the page');
  assert.equal(D.open(h.$('next')), true, 'the next helper dialog opens');
  assert.equal(D.isOpen(h.$('next')), true);
  // Forgotten for good: put back on the page, still [open], it is not the
  // helper's, and its way out never runs the resolver it was opened with.
  D.close(h.$('next'));
  const closes = h.closes.length;
  h.doc.body.append(h.dlg);
  assert.equal(D.isOpen(h.dlg), false, 'back on the page: still forgotten');
  h.$('opener').focus();
  h.$('dlgCancel').click();
  assert.deepEqual([h.closes.length, h.dlg.hasAttribute('open'), h.doc.activeElement], [closes, true, h.$('opener')],
    'its way out is not the helper’s to run');
  await tick();  // the menu's observer answers while the window is still there
  h.dom.window.close();
});

test('G5 a way out with no type never submits; a fallback Escape inside a nested dialog is that dialog’s; the first control skips what cannot take focus', () => {
  const h = dialogFixture();
  const D = h.UX.dialog;
  const options = { returnFocus: () => h.$('opener') };
  const submits = [];
  h.doc.addEventListener('submit', event => { event.preventDefault(); submits.push(event.submitter && event.submitter.id); });
  // A Close with no type is a submit button of the form: closed as a cancel,
  // and the choice it closes on is not submitted.
  h.$('dlgCancel').insertAdjacentHTML('beforebegin', '<button id="dlgBare" data-dialog-close>Close</button>');
  // A dialog the page owns, already open, with the same untyped way out.
  h.doc.body.insertAdjacentHTML('beforeend', '<dialog id="pageOwn" open><form method="dialog"><button id="pageBare" data-dialog-close>Close</button></form></dialog>');
  D.open(h.dlg, options);
  // Given the type it means at open: never the form's default button, which
  // Enter in the filter would click (the commit after it is the default).
  assert.equal(h.$('dlgBare').getAttribute('type'), 'button');
  h.$('dlgBare').removeAttribute('type');  // one that loses it later is still stopped at its click
  h.$('dlgBare').click();
  assert.deepEqual([D.isOpen(h.dlg), h.closes, submits], [false, ['cancel'], []], 'closed as a cancel, nothing submitted');
  assert.equal(h.doc.activeElement, h.$('opener'));
  // The same button in a dialog the page owns keeps the browser's behaviour.
  const click = new h.dom.window.MouseEvent('click', { bubbles: true, cancelable: true });
  h.$('pageBare').dispatchEvent(click);
  assert.deepEqual([click.defaultPrevented, submits], [false, ['pageBare']], 'not the helper’s to stop');
  assert.equal(h.$('pageBare').hasAttribute('type'), false, 'and its type is left alone');
  h.$('pageOwn').remove();
  // A page dialog open inside the helper's (fallback mode): an Escape in it
  // is its own, and the helper's dialog stays open.
  D.open(h.dlg, options);
  h.$('dlgBody').insertAdjacentHTML('beforeend', '<dialog id="inner" open><button type="button" id="innerBtn">OK</button></dialog>');
  const escape = new h.dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
  h.$('innerBtn').dispatchEvent(escape);
  assert.deepEqual([escape.defaultPrevented, D.isOpen(h.dlg)], [false, true], 'the inner dialog’s Escape');
  h.key(h.$('dlgFilter'), { key: 'Escape' });
  assert.equal(D.isOpen(h.dlg), false, 'an Escape of its own still closes it');
  // The first control that takes focus: not a hidden input, a disabled
  // button, or one inside [hidden] or .d-none; an initialFocus that is a
  // hidden input falls back the same way.
  h.doc.body.insertAdjacentHTML('beforeend', `<dialog id="picky"><form method="dialog"><input type="hidden" id="pkHidden" name="token" value="1">
      <button type="button" id="pkOff" disabled>off</button><div hidden><button type="button" id="pkInHidden">h</button></div>
      <div class="d-none"><button type="button" id="pkInDNone">d</button></div><input type="search" id="pkReal"></form></dialog>`);
  for (const initialFocus of [undefined, () => h.$('pkHidden')]) {
    h.$('opener').focus();
    assert.equal(D.open(h.$('picky'), { initialFocus, returnFocus: () => h.$('opener') }), true);
    assert.equal(h.doc.activeElement, h.$('pkReal'), initialFocus ? 'initialFocus a hidden input' : 'no initialFocus');
    D.close(h.$('picky'));
  }
  h.dom.window.close();
});

test('C2 forced colours: every choice row draws a border, the chosen row a Highlight one, the row with the keyboard an outline (never the same property)', () => {
  // Forced colours drop box shadows and author backgrounds, the row's only
  // frame and checked mark otherwise; the radio itself is a clipped pixel.
  // (Checked in Chromium with forced colours emulated; this pins the rules.)
  const css = fs.readFileSync(path.join(root, 'static/css/global.css'), 'utf8');
  const rules = new Map();
  for (const match of css.matchAll(/@media \(forced-colors: active\) \{/g)) {
    let depth = 1, i = match.index + match[0].length;
    const start = i;
    while (depth) { if (css[i] === '{') depth++; else if (css[i] === '}') depth--; i++; }
    for (const [, selector, body] of css.slice(start, i - 1).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      for (const one of selector.split(',')) rules.set(one.trim(), (rules.get(one.trim()) || '') + body);
    }
  }
  const declared = (selector, property) => {
    const body = rules.get(selector) || '';
    const found = new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`).exec(body);
    return found ? found[1].trim() : '';
  };
  assert.match(declared('.ux-choice', 'border'), /^1px solid CanvasText$/, 'every row: a frame');
  assert.match(declared('.ux-choice:has(input:checked)', 'border'), /^\d+px solid Highlight$/, 'the chosen row: Highlight');
  assert.ok(parseInt(declared('.ux-choice:has(input:checked)', 'border'), 10) > 1, 'and thicker than the frame, not colour alone');
  assert.match(declared('.ux-choice:focus-within', 'outline'), /^2px solid CanvasText$/, 'the row with the keyboard: an outline');
  assert.equal(declared('.ux-choice:has(input:checked)', 'outline'), '', 'the chosen mark leaves the outline to focus');
  assert.equal(declared('.ux-choice:focus-within', 'border'), '', 'and focus leaves the border to the chosen mark');
  // Nothing moves when a row is chosen: on every edge the border and the
  // padding add up to the same, a tone row's thick start edge included
  // (its width comes from the rule outside the media query).
  const px = value => parseFloat(value) || 0;
  const box = selector => {
    const [block, inline = block] = declared(selector, 'padding').split(/\s+/);
    return { block: px(block), inline: px(inline) };
  };
  const frame = px(declared('.ux-choice', 'border'));
  const mark = px(declared('.ux-choice:has(input:checked)', 'border'));
  const plain = box('.ux-choice'), chosen = box('.ux-choice:has(input:checked)');
  assert.equal(frame + plain.block, mark + chosen.block, 'top and bottom stay put');
  assert.equal(frame + plain.inline, mark + chosen.inline, 'the sides stay put');
  const toneEdge = px(/^\.ux-choice\[data-tone\]\s*\{\s*border-inline-start:\s*([\d.]+px)/m.exec(css)?.[1]);
  assert.ok(toneEdge > frame, 'a tone row has a thick start edge');
  const toneChosenStart = px(declared('.ux-choice[data-tone]:has(input:checked)', 'padding-inline-start'));
  assert.equal(toneEdge + plain.inline, mark + toneChosenStart, 'a tone row’s start edge stays put');
});
