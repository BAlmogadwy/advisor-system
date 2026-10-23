/* Execute the unmodified production dialog sections with a DOM and fake HTTP. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

const settle = () => new Promise(resolve => setImmediate(resolve));
const catalogue = [{ id: 1, course_code: 'VX401', course_name: 'Valid elective' }];
const placeholders = [{ course_code: 'VE1' }];
const valid = { placeholder_code: 'VE1', course_code: 'VX401', elective_id: 1 };
const hidden = {
  catalogue: { placeholder_code: 'VE1', course_code: 'HIDDEN', elective_id: 2 },
  placeholder: { placeholder_code: 'UNKNOWN', course_code: 'VX401', elective_id: 1 },
  identity: { placeholder_code: 'VE1', course_code: 'VX401', elective_id: 2 },
  duplicate: valid,
};
const sharedUtils = fs.readFileSync(path.join(__dirname, '../../static/js/shared-utils.js'), 'utf8');
const PAYLOAD = '<img src=x onerror="window.__xss=(window.__xss||0)+1">';

/* Today's markup for the plain VX401 fixture (VE1 selected), recorded from
   master before the dialogs moved to safeHtml (the A1 As-built note names the
   recorder): escaping must not change a byte of it. */
const GOLDEN = JSON.parse(fs.readFileSync(path.join(__dirname, '../fixtures/elective_markup_golden.json'), 'utf8'));

async function dialog(t, split, mappings, data = {}) {
  // The main page maps the open scenario's programme (one here); the split page
  // reads its boards.
  const dom = new JSDOM('<button id="twMapElectives"></button><input id="twYear" value="1448"><input id="twTerm" value="1">', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const context = dom.getInternalVMContext();
  const document = dom.window.document;
  const errors = [], writes = [], markup = [];
  let modal;
  const request = async (url, options = {}) => {
    if (options.method === 'POST') {
      writes.push(JSON.parse(options.body));
      return { ok: true, total: 1 };
    }
    if (url.includes('/catalogue/')) return { items: data.catalogue || catalogue };
    if (url.includes('/mapping/')) return mappings === null ? null : { items: mappings };
    if (url.includes('/placeholders/')) return { items: data.placeholders || placeholders };
    throw new Error(`Unexpected request ${url}`);
  };
  Object.assign(context, {
    IS_AR: false, $: id => document.getElementById(id),
    S: { scenarioMeta: { academic_year: '1448', term: '1' }, boards: [{ program: 'EVA' }] },
    TW: { scenario: { programs: ['EVA'] } },
    notify: { error: message => errors.push(message), success() {} },
    api: request, twFetch: request,
    openModal: options => {
      modal = options;
      document.body.insertAdjacentHTML('beforeend', options.body);
      // Record exactly what split writes into the list, before the DOM reparses it.
      const list = document.getElementById('twsElecList');
      const own = Object.getOwnPropertyDescriptor(dom.window.Element.prototype, 'innerHTML');
      Object.defineProperty(list, 'innerHTML', {
        get() { return own.get.call(this); },
        set(value) { markup.push(String(value)); own.set.call(this, value); },
      });
    },
    dlg: { confirm: async options => { markup.push(String(options.body)); document.body.insertAdjacentHTML('beforeend', options.body); return true; } },
  });
  // base.html loads shared-utils (esc, safeHtml) on every page, before the page.
  vm.runInContext(sharedUtils, context, { filename: 'shared-utils.js' });
  const name = `page-timetable-workspace${split ? '-split' : ''}.js`;
  const source = fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
  const start = source.indexOf(split ? 'function openElectivesModal()' : "$('twMapElectives').addEventListener");
  const end = source.indexOf(split ? '/* ── Bottom panel' : '/* ── V2 Optimiser', start);
  assert.ok(start >= 0 && end > start, 'Production dialog section must exist');
  vm.runInContext(source.slice(start, end), context, { filename: name });
  if (split) {
    vm.runInContext('openElectivesModal()', context);
    await settle();
    if (data.check) document.querySelectorAll('#twsElecList .elec-chk').forEach(box => { box.checked = true; });
    await modal.buttons.find(button => button.variant === 'primary').onClick();
  } else {
    const confirm = context.dlg.confirm;
    // Tick every box before the dialog "closes", as a user would.
    if (data.check) context.dlg.confirm = async options => { const r = await confirm(options); document.querySelectorAll('.elec-chk').forEach(box => { box.checked = true; }); return r; };
    document.getElementById('twMapElectives').click();
    await settle();
  }
  const list = split ? document.getElementById('twsElecList') : document.body;
  return { errors, writes, markup, list, window: dom.window };
}

for (const split of [false, true]) {
  const page = split ? 'split' : 'main';
  test(`${page} does not save after a failed mapping load`, async t => {
    const result = await dialog(t, split, null);
    assert.equal(result.writes.length, 0);
  });
  for (const [kind, mapping] of Object.entries(hidden)) {
    test(`${page} blocks hidden ${kind} mapping instead of posting a partial replacement`, async t => {
      const result = await dialog(t, split, [valid, mapping]);
      assert.equal(result.writes.length, 0);
      assert.match(result.errors[0], /Repair catalogue ownership, plan placeholders, or duplicate mappings/);
    });
  }
  test(`${page} saves a fully represented mapping`, async t => {
    const result = await dialog(t, split, [valid]);
    assert.deepEqual(result.errors, []);
    assert.deepEqual(result.writes[0].mappings, [{ placeholder_code: 'VE1', course_code: 'VX401' }]);
  });
  test(`${page} renders catalogue and placeholder markup as text, never as elements`, async t => {
    const result = await dialog(t, split, [], {
      catalogue: [{ id: 1, course_code: 'X101', course_name: PAYLOAD, prerequisites_csv: PAYLOAD }],
      placeholders: [{ course_code: 'PE1', programme_term: 5, type: PAYLOAD, credit_hours: 3 }],
    });
    assert.deepEqual(result.errors, []);
    assert.equal(result.list.querySelectorAll('img').length, 0);
    assert.equal(result.window.__xss, undefined);
    const text = result.list.textContent;
    // The name and the placeholder type on both pages; the prerequisites on the main page.
    assert.equal(text.split(PAYLOAD).length - 1, split ? 2 : 3, text);
    assert.equal(result.list.querySelectorAll('.elec-chk').length, 1);
  });
  test(`${page} round-trips codes with quotes and ampersands through data-*`, async t => {
    const ph = `P"E'1&`, code = `X"1'<&`, name = `R&D "Lab" <1>`;
    const result = await dialog(t, split, [], {
      catalogue: [{ id: 1, course_code: code, course_name: name }],
      placeholders: [{ course_code: ph }],
      check: true,
    });
    assert.deepEqual(result.errors, []);
    const box = result.list.querySelector('.elec-chk');
    assert.equal(box.dataset.ph, ph);
    assert.equal(box.dataset.code, code);
    // Each piece of text is escaped exactly once: shown as typed, never as "&amp;".
    const label = box.closest('label');
    assert.ok(label.textContent.includes(code) && label.textContent.includes(name), label.textContent);
    assert.ok(!label.textContent.includes('&amp;') && !label.textContent.includes('&quot;'), label.textContent);
    assert.ok(result.list.textContent.includes(ph), result.list.textContent);
    assert.deepEqual(result.writes[0].mappings, [{ placeholder_code: ph, course_code: code }]);
  });
  test(`${page} markup is unchanged for plain data`, async t => {
    const result = await dialog(t, split, [valid]);
    assert.equal(result.markup.at(-1), GOLDEN[page]);
  });
}
