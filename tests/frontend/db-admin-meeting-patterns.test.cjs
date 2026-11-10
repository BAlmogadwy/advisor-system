/*
 * DB Admin's catalogue delete and Oracle import confirmations
 * (static/js/page-db-admin.js) say what happens to the weekly meetings a
 * programme set in Section Planning, before anything runs. Run through
 * tests/test_programme_meeting_pattern_imports.py on the page its real view
 * renders, in English and Arabic. The page script runs unmodified; HTTP
 * answers and the confirmation dialog are scripted, and any request the suite
 * does not expect fails it.
 *
 * The server sends the count of rows holding a vote only when there are any,
 * so a programme without votes confirms exactly as before.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');

assert.ok(process.env.DBA_TEST_HTML, 'Run through pytest tests/test_programme_meeting_pattern_imports.py');
const template = fs.readFileSync(process.env.DBA_TEST_HTML, 'utf8');
const AR = process.env.DBA_TEST_LANGUAGE === 'ar';
const say = (en, ar) => (AR ? ar : en);
const read = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const SHARED = read('shared-utils.js');
const PAGE = read('page-db-admin.js');

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };

function answer(data, status = 200) {
  return {
    ok: status >= 200 && status < 300, status, statusText: '',
    headers: { get: () => 'application/json' },
    json: async () => data,
  };
}

const PREVIEW_ROW = {
  code: 'STAT103', code_ar: '', en_name: 'PRINCIPLES OF STATISTICS', ar_name: '', credits: 3,
  level_number: 1, level_en: 'Level 1', level_ar: '', type: 'Mandatory', prereqs_str: '',
  delivery: '', is_online: 0,
};

/* `routes` maps "METHOD path" to a function of the request that answers it. */
async function page(t, routes) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(template, { url: 'http://admin.test/db-admin/', runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  t.after(() => { window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  const dialogs = [];
  window.notify = { success() {}, error() {}, warning() {} };
  window.dlg = { confirm: async options => { dialogs.push(options); return false; } };
  const requests = [];
  window.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const route = `${method} ${String(url).split('?')[0]}`;
    requests.push({ url, method });
    if (routes[route]) return answer(routes[route](url));
    const error = new Error(`Unexpected HTTP request: ${route}`);
    errors.push(error);
    throw error;
  };
  vm.runInContext(SHARED, dom.getInternalVMContext(), { filename: 'shared-utils.js' });
  vm.runInContext(PAGE, dom.getInternalVMContext(), { filename: 'page-db-admin.js' });
  const $ = id => window.document.getElementById(id);
  /* A dialog body as the reader sees it: its paragraphs' text. */
  const paragraphs = html => {
    const box = window.document.createElement('div');
    box.innerHTML = html;
    return [...box.querySelectorAll('p')].map(p => p.textContent.replace(/\s+/g, ' ').trim());
  };
  return { window, $, dialogs, requests, paragraphs };
}

async function previewCatalogue(ui, program) {
  ui.$('pProgram').value = program;
  ui.$('pProgram').dispatchEvent(new ui.window.Event('input', { bubbles: true }));
  ui.$('pPreview').click();
  await settle();
}

const CATALOGUE = '/ops/db/preview-delete-program-catalog/';

test('a catalogue delete says how many weekly-meetings choices go with it', async t => {
  const ui = await page(t, {
    [`GET ${CATALOGUE}`]: () => ({ program: 'AI2', requirements_count: 48, prerequisites_count: 60, meeting_patterns_count: 2 }),
  });
  await previewCatalogue(ui, 'AI2');

  ui.$('pDelete').click();
  await settle();

  assert.equal(ui.dialogs.length, 1);
  const said = ui.paragraphs(ui.dialogs[0].body);
  assert.equal(said.length, AR ? 2 : 3, said.join(' | '));
  assert.equal(said.at(-1), say(
    '2 of its courses have weekly meetings set in Section Planning. They are deleted with it; the result lists each one so it can be set again.',
    'لدى 2 من مقرراته لقاءات أسبوعية محددة في تخطيط الشعب، وتُحذف معه؛ وتُدرج النتيجة كل واحد منها لإعادة تحديده.',
  ));
  assert.equal(ui.dialogs[0].typed, 'DELETE AI2');
});

test('a catalogue without weekly meetings confirms exactly as before, and a new preview forgets the old count', async t => {
  const ui = await page(t, {
    [`GET ${CATALOGUE}`]: url => {
      const program = new URL(url, 'http://admin.test').searchParams.get('program');
      const data = { program, requirements_count: 10, prerequisites_count: 4 };
      if (program === 'AI2') data.meeting_patterns_count = 3;
      return data;
    },
  });
  await previewCatalogue(ui, 'AI2');
  await previewCatalogue(ui, 'CS2');

  ui.$('pDelete').click();
  await settle();

  assert.deepEqual(ui.paragraphs(ui.dialogs[0].body), say(
    ['This will permanently delete all requirements and prerequisites for CS2.', 'This cannot be undone.'],
    ['سيحذف هذا كتالوج البرنامج بالكامل لـ CS2. لا يمكن التراجع عن هذا.'],
  ));
});

async function previewOracle(ui, program) {
  ui.$('oProgram').value = program;
  const file = new ui.window.File(['code\tname'], 'plan.csv', { type: 'text/csv' });
  Object.defineProperty(ui.$('oFile'), 'files', { configurable: true, value: [file] });
  ui.$('oPreview').click();
  await settle();
}

const ORACLE = '/ops/db/preview-oracle-plan/';
const oraclePreview = held => () => ({
  ok: true, metadata: {}, summary: { total_courses: 1 }, warnings: [], preview_rows: [PREVIEW_ROW],
  existing_db: held ? { requirements: 48, prerequisites: 60, meeting_patterns: held } : { requirements: 48, prerequisites: 60 },
});

test('an Oracle import says which weekly-meetings choices it keeps, and a replace names the file', async t => {
  const ui = await page(t, { [`POST ${ORACLE}`]: oraclePreview(4) });
  await previewOracle(ui, 'AI2');

  ui.$('oImport').click();
  await settle();
  ui.$('oReplace').checked = true;
  ui.$('oImport').click();
  await settle();

  assert.equal(ui.dialogs.length, 2);
  const [upsert, replace] = ui.dialogs.map(dialog => ui.paragraphs(dialog.body));
  assert.equal(upsert.length, 2);
  assert.equal(upsert[1], say(
    '4 of its courses have weekly meetings set in Section Planning. A course keeps them only if it keeps its name and 3 credit hours; the result lists every one the import drops.',
    'لدى 4 من مقرراته لقاءات أسبوعية محددة في تخطيط الشعب. يحتفظ المقرر بها فقط إذا بقي اسمه وساعاته المعتمدة الثلاث؛ وتُدرج النتيجة كل ما يُسقطه الاستيراد.',
  ));
  assert.equal(replace[1], say(
    '4 of its courses have weekly meetings set in Section Planning. A course keeps them only if it keeps its name and 3 credit hours and is still in this file; the result lists every one the import drops.',
    'لدى 4 من مقرراته لقاءات أسبوعية محددة في تخطيط الشعب. يحتفظ المقرر بها فقط إذا بقي اسمه وساعاته المعتمدة الثلاث وبقي في هذا الملف؛ وتُدرج النتيجة كل ما يُسقطه الاستيراد.',
  ));
  assert.deepEqual(ui.requests.map(r => `${r.method} ${r.url}`), [`POST ${ORACLE}`], 'a cancelled import sends nothing');
});

test('an Oracle import of a programme without weekly meetings confirms exactly as before', async t => {
  let held = 2;
  const ui = await page(t, { [`POST ${ORACLE}`]: () => oraclePreview(held)() });
  await previewOracle(ui, 'AI2');
  held = 0;
  await previewOracle(ui, 'CS2');

  ui.$('oImport').click();
  await settle();

  assert.deepEqual(ui.paragraphs(ui.dialogs[0].body), [say(
    'This will insert 1 courses into the database for program CS2.',
    'سيُدرج هذا 1 مقرر في قاعدة البيانات للبرنامج CS2.',
  )]);
});
