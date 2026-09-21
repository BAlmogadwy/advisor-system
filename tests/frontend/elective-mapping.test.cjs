/* Execute the unmodified production dialog sections with a DOM and fake HTTP. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

const settle = () => new Promise(resolve => setImmediate(resolve));
const catalogue = [{ id: 1, course_code: 'VX401', course_name: 'Valid elective' }];
const valid = { placeholder_code: 'VE1', course_code: 'VX401', elective_id: 1 };
const hidden = {
  catalogue: { placeholder_code: 'VE1', course_code: 'HIDDEN', elective_id: 2 },
  placeholder: { placeholder_code: 'UNKNOWN', course_code: 'VX401', elective_id: 1 },
  identity: { placeholder_code: 'VE1', course_code: 'VX401', elective_id: 2 },
  duplicate: valid,
};

async function dialog(t, split, mappings) {
  const dom = new JSDOM('<button id="twMapElectives"></button><input id="twYear" value="1448"><input id="twTerm" value="1"><input id="twProgram" value="EVA">', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const context = dom.getInternalVMContext();
  const document = dom.window.document;
  const errors = [], writes = [];
  let modal;
  const request = async (url, options = {}) => {
    if (options.method === 'POST') {
      writes.push(JSON.parse(options.body));
      return { ok: true, total: 1 };
    }
    if (url.includes('/catalogue/')) return { items: catalogue };
    if (url.includes('/mapping/')) return mappings === null ? null : { items: mappings };
    if (url.includes('/placeholders/')) return { items: [{ course_code: 'VE1' }] };
    throw new Error(`Unexpected request ${url}`);
  };
  Object.assign(context, {
    IS_AR: false, $: id => document.getElementById(id), esc: text => String(text),
    S: { scenarioMeta: { academic_year: '1448', term: '1' }, boards: [{ program: 'EVA' }] },
    notify: { error: message => errors.push(message), success() {} },
    api: request, twFetch: request,
    openModal: options => { modal = options; document.body.insertAdjacentHTML('beforeend', options.body); },
    dlg: { confirm: async options => { document.body.insertAdjacentHTML('beforeend', options.body); return true; } },
  });
  const name = `page-timetable-workspace${split ? '-split' : ''}.js`;
  const source = fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
  const start = source.indexOf(split ? 'function openElectivesModal()' : "$('twMapElectives').addEventListener");
  const end = source.indexOf(split ? '/* ── Bottom panel' : '/* ── V2 Optimiser', start);
  assert.ok(start >= 0 && end > start, 'Production dialog section must exist');
  vm.runInContext(source.slice(start, end), context, { filename: name });
  if (split) {
    vm.runInContext('openElectivesModal()', context);
    await settle();
    await modal.buttons.find(button => button.variant === 'primary').onClick();
  } else {
    document.getElementById('twMapElectives').click();
    await settle();
  }
  return { errors, writes };
}

for (const split of [false, true]) {
  test(`${split ? 'split' : 'main'} does not save after a failed mapping load`, async t => {
    const result = await dialog(t, split, null);
    assert.equal(result.writes.length, 0);
  });
  for (const [kind, mapping] of Object.entries(hidden)) {
    test(`${split ? 'split' : 'main'} blocks hidden ${kind} mapping instead of posting a partial replacement`, async t => {
      const result = await dialog(t, split, [valid, mapping]);
      assert.equal(result.writes.length, 0);
      assert.match(result.errors[0], /Repair catalogue ownership, plan placeholders, or duplicate mappings/);
    });
  }
  test(`${split ? 'split' : 'main'} saves a fully represented mapping`, async t => {
    const result = await dialog(t, split, [valid]);
    assert.deepEqual(result.errors, []);
    assert.deepEqual(result.writes[0].mappings, [{ placeholder_code: 'VE1', course_code: 'VX401' }]);
  });
}
