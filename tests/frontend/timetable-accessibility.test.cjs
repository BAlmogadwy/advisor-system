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
