/* The design system's overflow strip (static/js/ux-controls.js, `.ux-overflow`;
 * stage 3, C2): a `.ux-seg` or `.ux-tabs` whose items may not fit gets a
 * scroll button at each inline end, flags for the ends with more to see, and
 * its active item kept in view clear of the cue — by moving the strip only.
 * jsdom has no layout, so the strip's geometry is a model: N items of fixed
 * widths in a 300px strip, scrollLeft clamped as a browser clamps it (0..max
 * left to right, -max..0 right to left). The real layout is pinned in
 * tests/test_ux_layout_primitives_browser.py. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/ux-controls.js'), 'utf8');
const VIEW = 300;
const CUE = 52;
const wait = (ms = 40) => new Promise(resolve => setTimeout(resolve, ms));

/* A strip of `widths.length` items; `active` marks one item the way `mark`
   says (aria-current, aria-selected, .active or a checked radio). */
function harness({ lang = 'en', dir = 'ltr', widths = [100, 100, 100, 100, 100, 100, 100, 100], active = -1, kind = 'seg', mark = 'current' } = {}) {
  const items = widths.map((_, i) => (kind === 'tabs'
    ? `<button type="button" role="tab" class="ux-tab" id="t${i}">Tab ${i}</button>`
    : mark === 'radio'
      ? `<label class="ux-seg-item"><input type="radio" name="r" value="${i}">Item ${i}</label>`
      : `<button type="button" class="ux-seg-item" data-i="${i}">Item ${i}</button>`)).join('');
  const scroller = kind === 'tabs' ? `<div class="ux-tabs" role="tablist" id="s">${items}</div>` : `<nav class="ux-seg" id="s">${items}</nav>`;
  const dom = new JSDOM(`<!doctype html><html lang="${lang}" dir="${dir}"><body><input id="field"><div class="ux-overflow" id="w">${scroller}</div></body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  const doc = window.document;
  const strip = doc.getElementById('s');
  const wrapper = doc.getElementById('w');
  // Direction as the browser computes it (jsdom does not).
  const real = window.getComputedStyle.bind(window);
  window.getComputedStyle = (element, pseudo) => {
    const style = real(element, pseudo);
    const direction = (element.closest && element.closest('[dir]') && element.closest('[dir]').getAttribute('dir')) || 'ltr';
    return new Proxy(style, { get: (target, key) => (key === 'direction' ? direction : typeof target[key] === 'function' ? target[key].bind(target) : target[key]) });
  };
  const model = { widths: [...widths], left: 0, view: VIEW };
  const total = () => model.widths.reduce((sum, w) => sum + w, 0);
  const max = () => Math.max(0, total() - model.view);
  Object.defineProperty(strip, 'clientWidth', { get: () => model.view });
  Object.defineProperty(strip, 'scrollWidth', { get: () => Math.max(model.view, total()) });
  Object.defineProperty(strip, 'scrollLeft', {
    get: () => model.left,
    set: value => { model.left = dir === 'rtl' ? Math.min(0, Math.max(-max(), value)) : Math.max(0, Math.min(max(), value)); },
  });
  const rect = (left, width) => ({ left, right: left + width, width, top: 0, bottom: 30, height: 30, x: left, y: 0 });
  strip.getBoundingClientRect = () => rect(0, model.view);
  const itemsOf = () => [...strip.children];
  const boxOf = item => {
    const i = itemsOf().indexOf(item);
    const before = model.widths.slice(0, i).reduce((sum, w) => sum + w, 0);
    const width = model.widths[i];
    // Content moves against the view: left to right from the strip's left
    // edge, right to left from its right edge.
    return dir === 'rtl' ? rect(model.view - before - width - model.left, width) : rect(before - model.left, width);
  };
  const stub = () => itemsOf().forEach(item => {
    item.getBoundingClientRect = () => boxOf(item);
    item.getClientRects = () => [boxOf(item)];
  });
  stub();
  if (active >= 0) {
    const item = itemsOf()[active];
    if (mark === 'current') item.setAttribute('aria-current', 'true');
    else if (mark === 'selected') item.setAttribute('aria-selected', 'true');
    else if (mark === 'checked') item.setAttribute('aria-checked', 'true');
    else if (mark === 'class') item.classList.add('active');
    else if (mark === 'radio') item.querySelector('input').checked = true;
  }
  window.eval(source);
  const buttons = () => [...wrapper.querySelectorAll(':scope > .ux-overflow-btn')];
  const button = which => wrapper.querySelector(`:scope > .ux-overflow-btn[data-ux-overflow-dir="${which}"]`);
  const flags = () => ['start', 'end'].filter(end => wrapper.hasAttribute(`data-overflow-${end}`));
  return { dom, window, doc, strip, wrapper, model, stub, buttons, button, flags, boxOf, itemsOf };
}

test('O1 wiring adds one button at each inline end, outside the scroller, for a pointer only; twice is once', async () => {
  for (const [lang, start, end] of [['en', 'Show previous', 'Show next'], ['ar', 'عرض السابق', 'عرض التالي']]) {
    const h = harness({ lang });
    const children = [...h.wrapper.children];
    assert.deepEqual(children.map(child => child.dataset.uxOverflowDir || child.id), ['start', 's', 'end']);
    for (const b of h.buttons()) {
      assert.equal(b.tagName, 'BUTTON');
      assert.equal(b.type, 'button');
      assert.equal(b.tabIndex, -1, 'no Tab stop');
      assert.equal(b.getAttribute('aria-hidden'), 'true', 'hidden from assistive technology');
      assert.ok(b.querySelector('svg.i-flip-rtl polyline'), 'a chevron that mirrors in Arabic');
    }
    assert.deepEqual([h.button('start').title, h.button('end').title], [start, end]);
    h.window.UXControls.wire();
    h.window.UXControls.overflow.wire(h.wrapper);
    assert.equal(h.buttons().length, 2);
    // A press never takes focus from wherever it is.
    const down = new h.window.MouseEvent('mousedown', { bubbles: true, cancelable: true });
    h.button('end').dispatchEvent(down);
    assert.equal(down.defaultPrevented, true);
    h.dom.window.close();
  }
});

test('O2 the wrapper says which ends have more to see, in either direction, and nothing when the items fit', async () => {
  const ltr = harness();
  await wait();
  assert.deepEqual(ltr.flags(), ['end']);
  ltr.strip.scrollLeft = 200;
  ltr.strip.dispatchEvent(new ltr.window.Event('scroll'));
  await wait();
  assert.deepEqual(ltr.flags(), ['start', 'end']);
  ltr.strip.scrollLeft = 500;
  ltr.strip.dispatchEvent(new ltr.window.Event('scroll'));
  await wait();
  assert.deepEqual(ltr.flags(), ['start']);
  ltr.dom.window.close();

  const rtl = harness({ dir: 'rtl', lang: 'ar' });
  await wait();
  assert.deepEqual(rtl.flags(), ['end']);
  rtl.strip.scrollLeft = -500;
  rtl.strip.dispatchEvent(new rtl.window.Event('scroll'));
  await wait();
  assert.deepEqual(rtl.flags(), ['start']);
  rtl.dom.window.close();

  const fits = harness({ widths: [100, 100] });
  await wait();
  assert.deepEqual(fits.flags(), []);
  fits.dom.window.close();
});

test('O3 a button pages the strip by its width less the two cues, toward its own end in either direction', async () => {
  const page = VIEW - 2 * CUE;
  const ltr = harness();
  await wait();
  ltr.button('end').click();
  assert.equal(ltr.strip.scrollLeft, page);
  ltr.button('start').click();
  assert.equal(ltr.strip.scrollLeft, 0);
  ltr.dom.window.close();

  const rtl = harness({ dir: 'rtl' });
  await wait();
  rtl.button('end').click();
  assert.equal(rtl.strip.scrollLeft, -page);
  rtl.button('start').click();
  assert.equal(rtl.strip.scrollLeft, 0);
  rtl.dom.window.close();
});

test('O4 the active item is brought into view clear of the cue at wiring, in either direction', async () => {
  const ltr = harness({ active: 5 });
  await wait();
  // Item 5 is at 500..600: it ends one cue in from the strip's right edge.
  assert.equal(ltr.strip.scrollLeft, 600 - (VIEW - CUE));
  assert.deepEqual([ltr.boxOf(ltr.itemsOf()[5]).left, ltr.boxOf(ltr.itemsOf()[5]).right], [VIEW - CUE - 100, VIEW - CUE]);
  ltr.dom.window.close();

  const rtl = harness({ active: 5, dir: 'rtl' });
  await wait();
  assert.equal(rtl.strip.scrollLeft, -(600 - (VIEW - CUE)));
  assert.deepEqual([rtl.boxOf(rtl.itemsOf()[5]).left, rtl.boxOf(rtl.itemsOf()[5]).right], [CUE, CUE + 100]);
  rtl.dom.window.close();

  // Already clear of the cues: the strip does not move.
  const still = harness({ active: 1 });
  await wait();
  assert.equal(still.strip.scrollLeft, 0);
  still.dom.window.close();
});

test('O5 every way of marking the active item counts: a tab’s .active, aria-selected, aria-checked, a checked radio', async () => {
  // (A tablist's tabs are marked by .active; the tabs controller writes aria-selected from it.)
  for (const [kind, mark] of [['tabs', 'class'], ['seg', 'selected'], ['seg', 'checked'], ['seg', 'radio']]) {
    const h = harness({ active: 6, kind, mark });
    await wait();
    assert.equal(h.strip.scrollLeft, 700 - (VIEW - CUE), `${kind} ${mark}`);
    h.dom.window.close();
  }
});

test('O6 a new active item, or the items drawn again, is revealed again; scrolling by hand is not undone', async () => {
  const h = harness({ active: 0 });
  await wait();
  assert.equal(h.strip.scrollLeft, 0);
  h.strip.scrollLeft = 300;
  h.strip.dispatchEvent(new h.window.Event('scroll'));
  await wait();
  assert.equal(h.strip.scrollLeft, 300, 'a scroll alone never reveals');
  // The page marks another item (at 400..500, past the cue): it is revealed.
  h.itemsOf()[0].removeAttribute('aria-current');
  h.itemsOf()[7].setAttribute('aria-current', 'true');
  await wait();
  assert.equal(h.strip.scrollLeft, 500, 'item 7 at the end: the strip moves as far as it goes');
  // The page draws the items again (a board renders), the first one current.
  h.strip.innerHTML = h.model.widths.map((_, i) => `<button type="button" class="ux-seg-item"${i === 1 ? ' aria-current="true"' : ''}>Item ${i}</button>`).join('');
  h.stub();
  await wait();
  // Item 1 (100..200 along the strip) now starts one cue in from its left edge.
  assert.equal(h.strip.scrollLeft, 100 - CUE);
  h.dom.window.close();
});

test('O7 an item too wide for the room between the cues is centred; one wider than the strip shows its start', async () => {
  const tight = harness({ widths: [100, 100, 250, 100, 100], active: 2 });
  await wait();
  // 250 in 300: 25px each side.
  assert.deepEqual([tight.boxOf(tight.itemsOf()[2]).left, tight.boxOf(tight.itemsOf()[2]).right], [25, 275]);
  tight.dom.window.close();

  for (const dir of ['ltr', 'rtl']) {
    const wide = harness({ widths: [100, 100, 420, 100], active: 2, dir });
    await wait();
    const box = wide.boxOf(wide.itemsOf()[2]);
    if (dir === 'ltr') assert.equal(box.left, 0);
    else assert.equal(box.right, VIEW);
    wide.dom.window.close();
  }
});

test('O8 refresh() measures again at once; a strip that fits keeps no flags and never moves', async () => {
  const h = harness({ widths: [100, 100], active: 1 });
  await wait();
  assert.deepEqual(h.flags(), []);
  // The items grow (a font arrived, a label changed) with no node changed.
  h.model.widths = [200, 200];
  h.window.UXControls.overflow.refresh(h.wrapper);
  // Item 1 (200..400) wants 152px; the strip goes as far as it can, 100px.
  assert.equal(h.strip.scrollLeft, 100);
  assert.deepEqual(h.flags(), ['start']);
  h.dom.window.close();
});
