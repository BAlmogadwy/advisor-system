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
