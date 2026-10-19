const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/timetable-workspace-accessibility.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'core/templates/core/timetable_workspace.html'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  // Exercise the real markup while removing server template tags and scripts.
  const html = template.replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only' });
  const { window } = dom;
  const doc = window.document;
  const activations = [];
  for (const [triggerId, menuId] of [['twGenerateMenu', 'twGenerateDropdown'], ['twOptimiseMenu', 'twOptimiseDropdown'], ['twExportMenu', 'twExportDropdown']]) {
    const trigger = doc.getElementById(triggerId);
    const menu = doc.getElementById(menuId);
    trigger.disabled = false;
    // Match the production click toggle, wired before the additive module.
    trigger.addEventListener('click', event => {
      event.stopPropagation();
      menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
    });
    menu.querySelectorAll('[role="menuitem"]').forEach(item => item.addEventListener('click', () => {
      menu.style.display = 'none';
      activations.push(item.dataset.engine || item.dataset.mode);
    }));
  }
  doc.querySelectorAll('[role="tablist"]').forEach(list => {
    list.querySelectorAll('[role="tab"]').forEach(tab => tab.addEventListener('click', () => {
      list.querySelectorAll('[role="tab"]').forEach(other => other.classList.remove('active'));
      tab.classList.add('active');
    }));
  });
  doc.getElementById('twBottomHandle').addEventListener('click', () => doc.getElementById('twBottom').classList.toggle('open'));
  window.eval(source);
  const key = (element, value) => element.dispatchEvent(new window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true }));
  return { dom, doc, key, activations };
}

test('Publish and menu actions use valid native buttons and preserve action data', () => {
  const h = harness();
  assert.equal(h.doc.getElementById('twPublish').tagName, 'BUTTON');
  assert.match(h.doc.getElementById('twPublish').textContent, /Publish/);
  for (const item of h.doc.querySelectorAll('[role="menuitem"]')) assert.equal(item.tagName, 'BUTTON');
  assert.equal(h.doc.querySelectorAll('[role="menuitem"]').length, 6);
  h.dom.window.close();
});

test('menus navigate using arrows and endpoints, Escape restores focus, and selection fires once', async () => {
  const h = harness();
  const trigger = h.doc.getElementById('twGenerateMenu');
  const items = [...h.doc.querySelectorAll('#twGenerateDropdown [role="menuitem"]')];
  trigger.focus();
  h.key(trigger, 'ArrowDown');
  assert.equal(trigger.getAttribute('aria-expanded'), 'true');
  assert.equal(h.doc.activeElement, items[0]);
  h.key(items[0], 'End');
  assert.equal(h.doc.activeElement, items[1]);
  h.key(items[1], 'ArrowDown');
  assert.equal(h.doc.activeElement, items[0]);
  h.key(items[0], 'Escape');
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(h.doc.activeElement, trigger);
  h.key(trigger, 'ArrowUp');
  assert.equal(h.doc.activeElement, items[1]);
  items[1].click();
  await tick();
  assert.deepEqual(h.activations, ['scheduler']);
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(h.doc.activeElement, trigger);
  h.dom.window.close();
});

test('only one menu stays open and outside click or leaving focus closes it', async () => {
  const h = harness();
  const gen = h.doc.getElementById('twGenerateMenu');
  const opt = h.doc.getElementById('twOptimiseMenu');
  gen.click(); opt.click();
  assert.equal(gen.getAttribute('aria-expanded'), 'false');
  assert.equal(opt.getAttribute('aria-expanded'), 'true');
  h.doc.getElementById('twSearch').focus();
  assert.equal(opt.getAttribute('aria-expanded'), 'false');
  gen.click();
  h.doc.getElementById('twSearch').click();
  await tick();
  assert.equal(gen.getAttribute('aria-expanded'), 'false');
  h.dom.window.close();
});

test('the Generate bar rises above the sticky command bar exactly while its menu is open', async () => {
  // The bar's backdrop-filter traps the menu's own z-index, so the stylesheet
  // raises the whole bar; use its exact selector and its resolved layer.
  const css = fs.readFileSync(path.join(root, 'static/css/timetable-workspace.css'), 'utf8');
  const globalCss = fs.readFileSync(path.join(root, 'static/css/global.css'), 'utf8');
  const [, selector, body] = /(#twGenBar:has\([^{]+\))\s*{([^}]*)}/.exec(css);
  const token = name => Number(new RegExp(`${name}:\\s*(\\d+)`).exec(globalCss)[1]);
  const [, base, plus] = /z-index:\s*calc\(var\((--z-[\w-]+)\)\s*\+\s*(\d+)\)/.exec(body);
  const raised = token(base) + Number(plus);
  const commandBar = Number(/\n\.tw-cmd-bar\s*{[^}]*?z-index:\s*(\d+)/.exec(globalCss)[1]);
  assert.match(body, /position:\s*relative/);
  assert.ok(raised > commandBar, `${raised} must clear the command bar's ${commandBar}`);
  for (const layer of ['--z-sidebar', '--z-drawer', '--z-modal', '--z-toast']) assert.ok(raised < token(layer), layer);

  const h = harness();
  const bar = h.doc.getElementById('twGenBar');
  const gen = h.doc.getElementById('twGenerateMenu');
  // jsdom's CSS parser rejects the menu's `border:1px solid var(--line)` and
  // drops its whole inline style; a browser keeps `display:none`, so start there.
  h.doc.getElementById('twGenerateDropdown').style.display = 'none';
  const isRaised = async () => { await tick(); return bar.matches(selector); };
  assert.equal(await isRaised(), false);
  gen.click();
  assert.equal(await isRaised(), true, 'click opens');
  h.key(h.doc.getElementById('twGenerateDropdown'), 'Escape');
  assert.equal(await isRaised(), false, 'Escape closes');
  h.key(gen, 'ArrowDown');
  assert.equal(await isRaised(), true, 'ArrowDown opens');
  h.key(h.doc.activeElement, 'Tab');
  assert.equal(await isRaised(), false, 'Tab closes');
  gen.click(); h.doc.getElementById('twOptimiseMenu').click();
  assert.equal(await isRaised(), false, 'opening Optimise closes it');
  gen.click(); h.doc.getElementById('twSearch').click();
  assert.equal(await isRaised(), false, 'outside click closes');
  gen.click(); h.doc.querySelector('#twGenerateDropdown [role="menuitem"]').click();
  assert.equal(await isRaised(), false, 'choosing an engine closes');
  h.dom.window.close();
});

test('a planner action that disables the trigger restores focus to the visible status', async () => {
  const h = harness();
  const trigger = h.doc.getElementById('twGenerateMenu');
  h.key(trigger, 'ArrowDown');
  const item = h.doc.activeElement;
  item.addEventListener('click', () => { trigger.disabled = true; });
  item.click();
  await tick();
  assert.equal(h.doc.activeElement, h.doc.getElementById('twGenStatus'));
  h.dom.window.close();
});

test('both tab groups use roving focus and update panel labels, including programmatic selection', async () => {
  const h = harness();
  for (const list of h.doc.querySelectorAll('[role="tablist"]')) {
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

test('Details hides collapsed contents from keyboard focus and follows programmatic opening', async () => {
  const h = harness();
  const bottom = h.doc.getElementById('twBottom');
  const handle = h.doc.getElementById('twBottomHandle');
  const content = h.doc.getElementById('twBottomContent');
  assert.equal(content.hidden, true);
  handle.click();
  assert.equal(handle.getAttribute('aria-expanded'), 'true');
  assert.equal(content.hidden, false);
  h.doc.getElementById('twTabDemand').focus();
  bottom.classList.remove('open');
  await tick();
  assert.equal(handle.getAttribute('aria-expanded'), 'false');
  assert.equal(content.hidden, true);
  assert.equal(h.doc.activeElement, handle);
  bottom.classList.add('open');
  await tick();
  assert.equal(content.hidden, false);
  h.dom.window.close();
});
