/*
 * Section Planning (static/js/page-section-planning.js) on the page its real
 * view renders, in English and Arabic: run through
 * tests/test_section_planning_frontend.py. The page script runs unmodified;
 * only HTTP answers and (unless a test asks for the real static/js/dialog.js)
 * the shared confirmation dialog are scripted. Any request the suite does not
 * expect fails the test.
 *
 * Seat limits are data-safe: an edit is a draft (no request on input, change
 * or blur), Generate sends only edited values, Discard and Reset never write,
 * and a save is previewed, confirmed row by row, then committed with the
 * preview's token for the programmes on screen.
 *
 * A plan is for ONE section, Male (M) or Female (F): Generate asks nothing
 * of the server until one is chosen, the result says which it is for, and
 * nothing on the page shows M, F or a Total of the two.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');

assert.ok(process.env.SP_TEST_HTML, 'Run through pytest tests/test_section_planning_frontend.py');
const template = fs.readFileSync(process.env.SP_TEST_HTML, 'utf8');
const AR = process.env.SP_TEST_LANGUAGE === 'ar';
const say = (en, ar) => (AR ? ar : en);
/* A code as the page isolates it inside Arabic plain text (FSI ... PDI). */
const isle = code => String.fromCharCode(0x2068) + code + String.fromCharCode(0x2069);
const read = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const SHARED = read('shared-utils.js');
const UX = read('shared-ux.js');   // wireSortableTable, as base.html loads it
const DIALOG = read('dialog.js');
const PAGE = read('page-section-planning.js');

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

const LIMITS = '/ops/section-planning/limits/';
const COURSES = '/ops/section-planning/courses/';
const GENERATE = '/ops/section-planning/generate/';
const EXPORT = '/ops/section-planning/export/';
const SECTION_KEY = 'sectionPlanning.section';

/* As the server answers: with programmes, each one's saved limit; with none,
 * the lowest limit any programme declares (what Generate applies), read-only. */
function coursesFor(program) {
  const programmes = program ? program.split(',') : [];
  const limits = saved => (program ? saved : {});
  const unscoped = program ? {} : { limit_scope: 'lowest_declared' };
  return [
    { course_code: 'AI491', department: 'AI', credit_hours: 2, is_external: false, default_max: 40,
      programme_max: 5, programmes, programme_limits: limits({ AI: 5 }), ...unscoped },
    { course_code: 'CS211', department: 'CS', credit_hours: 3, is_external: false, default_max: 40,
      programme_max: 30, programmes, programme_limits: limits({ AI: 30 }), ...unscoped },
    { course_code: 'MATH203', department: 'MATH', credit_hours: 3, is_external: true, default_max: 50,
      programme_max: null, programmes, programme_limits: {}, ...unscoped },
    { course_code: 'AI1', department: 'AI', credit_hours: 3, is_external: false, default_max: 40,
      programme_max: 30, programmes, programme_limits: limits({ AI: 30 }), ...unscoped,
      slot_electives: program ? [{ program: 'AI', status: 'ready', courses: ['AI463'] }] : [] },
  ];
}

function planRow(code, extra = {}) {
  return {
    department: code.replace(/\d.*/, ''), course_key: code, course_code: code, course_name: `${code} name`,
    credit_hours: 3, is_external: false, total_students: 30, num_sections: 1, max_per_section: 40,
    avg_per_section: 30, fill_percent: 75, status: '', ...extra,
  };
}

function generated(plan = [planRow('AI491')], section = 'M') {
  const sections = plan.reduce((sum, row) => sum + row.num_sections, 0);
  return {
    ok: true, mode: 'single', year: 1448, semester: 1, section, student_count: 324, no_section: 0, plan,
    summary: { total_courses: plan.length, total_sections: sections, total_students: 30, avg_fill_percent: 75,
      departments: [{ department: 'AI', courses: plan.length, sections, students: 30, total_credits: 3 }] },
  };
}

function answer(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}

/* An .xlsx download as the server sends it: the file name in Content-Disposition. */
function fileAnswer(disposition) {
  return {
    ok: true, status: 200, json: async () => ({}), blob: async () => ({ size: 1 }),
    headers: { get: name => (name.toLowerCase() === 'content-disposition' ? disposition : null) },
  };
}

/* The server: scripted answers for the limits endpoint, recorded requests. */
function limitsServer({ preview, commit } = {}) {
  return (body) => {
    if (body.dry_run === true) {
      return preview ? preview(body) : answer({
        ok: true, dry_run: true, programs: body.programs, unchanged: 0, preview_token: 'token-1',
        changes: body.changes.map(change => ({
          course_code: change.course_code, program: body.programs[0], old: 5, new: change.max_capacity,
          scope: change.all_programmes ? 'all_programmes' : 'programmes',
        })),
      });
    }
    return commit ? commit(body) : answer({ ok: true, dry_run: false, changed: [], changed_count: body.changes.length, unchanged: 0 });
  };
}

/* `section`: the radio the user picks once the page is up (null: none).
 * `stored`: what this browser remembered from an earlier visit.
 * `storage`: 'throws' when reading localStorage itself throws (blocked site
 * data), 'fails' when its reads and writes throw. */
async function page(t, { program = 'AI', section = 'M', stored = null, storage = 'ok', limits = limitsServer(), confirm = async () => true, generate = body => answer(generated(undefined, body.section)), exportFile = () => fileAnswer(''), localDepartments = null, courses = prog => answer({ ok: true, courses: coursesFor(prog) }), realDialogs = false } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(template, { url: 'http://planning.test/section-planning/', runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  t.after(() => { window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  const toasts = [];
  const dialogs = [];
  window.notify = { success: message => toasts.push(['success', message]), error: message => toasts.push(['error', message]) };
  if (!realDialogs) window.dlg = { confirm: async options => { dialogs.push(options); return confirm(options); } };
  const requests = [];
  window.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url, method, body });
    if (method === 'GET' && url.startsWith(COURSES)) {
      return courses(new URL(url, 'http://planning.test').searchParams.get('program') || '');
    }
    if (method === 'POST' && url === LIMITS) return limits(body);
    if (method === 'POST' && url === GENERATE) return generate(body);
    if (method === 'POST' && url === EXPORT) return exportFile(body);
    const error = new Error(`Unexpected HTTP request: ${method} ${url}`);
    errors.push(error);
    throw error;
  };
  /* A download: the object URL and the link's click, recorded (jsdom has neither). */
  const downloads = [];
  window.URL.createObjectURL = () => 'blob:section-plan';
  window.URL.revokeObjectURL = () => {};
  window.HTMLAnchorElement.prototype.click = function click() { downloads.push(this.download); };
  if (localDepartments) window.document.getElementById('spLocalDepartments').textContent = JSON.stringify(localDepartments);
  if (stored) window.localStorage.setItem(SECTION_KEY, stored);
  const context = dom.getInternalVMContext();
  vm.runInContext(SHARED, context, { filename: 'shared-utils.js' });
  vm.runInContext(UX, context, { filename: 'shared-ux.js' });
  if (realDialogs) {
    window.requestAnimationFrame = callback => window.setTimeout(callback, 0);
    vm.runInContext(DIALOG, context, { filename: 'dialog.js' });
  }
  const refuse = () => { throw new window.DOMException('The operation is insecure.', 'SecurityError'); };
  if (storage === 'throws') Object.defineProperty(window, 'localStorage', { configurable: true, get: refuse });
  if (storage === 'fails') Object.assign(window.Storage.prototype, { getItem: refuse, setItem: refuse });
  vm.runInContext(PAGE, context, { filename: 'page-section-planning.js' });
  const $ = id => window.document.getElementById(id);
  const emit = (element, type) => element.dispatchEvent(new window.Event(type, { bubbles: true }));
  if (section) $(`spSection${section}`).click();
  $('spProgram').value = program;
  emit($('spProgram'), 'change');
  $('spToggleAdv').click();
  await settle();
  const row = code => window.document.querySelector(`#spAdvBody tr[data-code="${code}"]`);
  const input = code => row(code).querySelector('.adv-input');
  const type = async (code, value) => {
    const field = input(code);
    field.focus();
    field.value = value;
    emit(field, 'input');
    emit(field, 'change');
    field.blur();
    emit(field, 'blur');
    await settle();
  };
  const writes = () => requests.filter(request => request.method !== 'GET');
  const text = id => $(id).textContent.replace(/\s+/g, ' ').trim();
  const saveOff = () => $('spAdvSaveDb').getAttribute('aria-disabled') === 'true';
  /* A key as a browser delivers it: keydown, then (unless prevented) a
   * focused button's own activation. jsdom does not activate on its own. */
  const key = (element, value) => {
    const event = new window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true });
    element.dispatchEvent(event);
    if (!event.defaultPrevented && value === 'Enter' && element.tagName === 'BUTTON') element.click();
    return event;
  };
  const setProgram = async value => { $('spProgram').value = value; emit($('spProgram'), 'change'); await settle(); };
  const generates = () => requests.filter(request => request.url === GENERATE);
  return { window, $, emit, row, input, type, requests, writes, dialogs, toasts, text, saveOff, key, setProgram, generates, downloads };
}

test('editing a limit and leaving the field writes nothing; the row is a draft', async t => {
  const ui = await page(t);
  assert.equal(ui.input('AI491').value, '5', 'the saved limit is shown');
  assert.equal(ui.input('AI491').getAttribute('aria-label'), say('Seat limit for AI491', 'الحد الأقصى لشعبة AI491'));

  await ui.type('AI491', '6');

  assert.deepEqual(ui.writes(), []);
  assert.ok(ui.row('AI491').classList.contains('sp-adv-draft'));
  assert.equal(ui.row('AI491').querySelector('.sp-adv-state').textContent, say('Modified', 'معدَّل'));
  assert.equal(ui.text('spAdvBadge'), '1');
  assert.equal(ui.text('spAdvDrafts'), say('1 unsaved change', '1 تعديل غير محفوظ'));
  assert.equal(ui.saveOff(), false);
  assert.ok(!ui.row('CS211').classList.contains('sp-adv-draft'), 'an untouched saved limit is not a draft');
});

test('an emptied saved limit is restored on blur, never removed', async t => {
  const ui = await page(t);
  await ui.type('AI491', '');
  assert.equal(ui.input('AI491').value, '5');
  assert.deepEqual(ui.writes(), []);
  assert.ok(!ui.row('AI491').classList.contains('sp-adv-draft'));
});

test('Generate sends only edited limits as what-if, never the pre-filled saved ones', async t => {
  const ui = await page(t);
  ui.$('spGenerate').click();
  await settle();
  let sent = ui.requests.filter(r => r.url === GENERATE);
  assert.equal(sent.length, 1);
  assert.equal('course_overrides' in sent[0].body, false, 'saved limits are the server\'s to load');

  await ui.type('CS211', '28');
  ui.$('spGenerate').click();
  await settle();
  sent = ui.requests.filter(r => r.url === GENERATE);
  assert.deepEqual(sent[1].body.course_overrides, { CS211: 28 });
  assert.equal(ui.requests.filter(r => r.url === LIMITS).length, 0, 'Generate never saves');
});

test('Discard drops drafts and makes no request', async t => {
  const ui = await page(t);
  await ui.type('AI491', '7');
  ui.row('CS211').querySelector('.adv-all').click();
  await settle();
  assert.equal(ui.text('spAdvBadge'), '2');

  ui.$('spAdvReset').click();
  await settle();

  assert.equal(ui.input('AI491').value, '5');
  assert.equal(ui.row('CS211').querySelector('.adv-all').checked, false);
  assert.ok(ui.$('spAdvBadge').classList.contains('d-none'));
  assert.equal(ui.saveOff(), true);
  assert.deepEqual(ui.writes(), []);
});

test('Reset clears the scope and the drafts without touching saved limits', async t => {
  const ui = await page(t);
  await ui.type('AI491', '9');

  ui.$('spReset').click();
  await settle();

  assert.deepEqual(ui.writes(), [], 'Reset never writes');
  assert.equal(ui.$('spProgram').value, '');
  assert.equal(ui.window.document.querySelectorAll('#spAdvBody tr.sp-adv-draft').length, 0);
  assert.equal(ui.saveOff(), true, 'no programme on screen: nothing can be saved');
});

test('Save previews, confirms course · programme · old → new, then commits with the preview token', async t => {
  const ui = await page(t, {
    limits: limitsServer({
      preview: body => answer({
        ok: true, dry_run: true, programs: body.programs, unchanged: 1, preview_token: 'token-7',
        changes: [
          { course_code: 'AI491', program: 'AI', old: 5, new: 6, scope: 'programmes' },
          { course_code: 'CS211', program: 'AI', old: 30, new: 28, scope: 'all_programmes' },
          { course_code: 'CS211', program: 'DS', old: null, new: 28, scope: 'all_programmes' },
        ],
      }),
      commit: body => answer({ ok: true, dry_run: false, programs: body.programs, changed: [], changed_count: 3, unchanged: 1 }),
    }),
  });
  await ui.type('AI491', '6');
  await ui.type('CS211', '28');
  ui.row('CS211').querySelector('.adv-all').click();
  await settle();

  ui.$('spAdvSaveDb').click();
  await settle();

  const posts = ui.writes();
  assert.equal(posts.length, 2);
  assert.deepEqual(posts[0].body, {
    programs: ['AI'],
    changes: [
      { course_code: 'AI491', max_capacity: 6, all_programmes: false },
      { course_code: 'CS211', max_capacity: 28, all_programmes: true },
    ],
    dry_run: true,
  });
  assert.deepEqual(posts[1].body, { ...posts[0].body, dry_run: false, preview_token: 'token-7' });

  assert.equal(ui.dialogs.length, 1);
  const shown = new JSDOM(`<table>${ui.dialogs[0].body}</table>`).window.document;
  const rows = [...shown.querySelectorAll('.sp-limit-review tbody tr')].map(tr =>
    [...tr.querySelectorAll('td')].map(td => td.textContent.trim()));
  assert.deepEqual(rows, [
    ['AI491', 'AI', '5', '6', say('Programmes on screen', 'البرامج المعروضة')],
    ['CS211', 'AI', '30', '28', say('All programmes', 'كل البرامج')],
    ['CS211', 'DS', say('— (rule)', '— (القاعدة)'), '28', say('All programmes', 'كل البرامج')],
  ]);
  assert.match(ui.dialogs[0].body, AR ? /1 صف مطابق/ : /1 row already matches/);
  assert.equal(ui.dialogs[0].title, say('Save 3 seat limits?', 'حفظ 3 من حدود الشعب؟'));
  assert.equal(ui.text('spStatus'), say('Saved 3 seat limits; each change is in the audit log.', 'حُفظ 3 حد وسُجّل في سجل التدقيق.'));
  // The panel is reloaded from the server: the drafts are gone.
  assert.equal(ui.requests.filter(r => r.method === 'GET').length, 2);
  assert.ok(ui.$('spAdvBadge').classList.contains('d-none'));
});

test('cancelling the confirmation writes nothing and keeps the drafts', async t => {
  const ui = await page(t, { confirm: async () => false });
  await ui.type('AI491', '6');

  ui.$('spAdvSaveDb').click();
  await settle();

  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true], 'only the preview was asked for');
  assert.ok(ui.row('AI491').classList.contains('sp-adv-draft'));
  assert.equal(ui.input('AI491').value, '6');
});

test('Save is unavailable with no programme on screen', async t => {
  const ui = await page(t, { program: '' });
  await ui.type('AI491', '6');

  assert.equal(ui.saveOff(), true);
  assert.equal(ui.$('spAdvSaveDb').disabled, false, 'still focusable');
  assert.equal(ui.$('spAdvSaveDb').title, say('Choose a programme first: limits are saved per programme.', 'اختر برنامجاً أولاً: الحدود تُحفظ لكل برنامج.'));
  assert.equal(ui.$('spAdvSaveDb').getAttribute('aria-describedby'), 'spAdvDrafts');
  assert.equal(ui.text('spAdvDrafts'), say(
    '1 what-if change for Generate — choose a programme to save',
    '1 تعديل للحساب فقط — اختر برنامجاً لحفظه',
  ));
  ui.$('spAdvSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes(), []);
  assert.equal(ui.text('spStatus'), say('Choose a programme first: limits are saved per programme.', 'اختر برنامجاً أولاً: الحدود تُحفظ لكل برنامج.'));
});

test('an invalid value blocks Save and is marked on its field', async t => {
  const ui = await page(t);
  await ui.type('AI491', '0');

  assert.ok(ui.row('AI491').classList.contains('sp-adv-invalid'));
  assert.equal(ui.input('AI491').getAttribute('aria-invalid'), 'true');
  assert.equal(ui.row('AI491').querySelector('.sp-adv-state').textContent, say('Whole number 1–500', 'رقم صحيح من 1 إلى 500'));
  assert.equal(ui.saveOff(), true);
  assert.equal(ui.$('spAdvSaveDb').title, say('Fix the invalid values before saving.', 'صحّح القيم غير الصالحة قبل الحفظ.'));
  assert.equal(ui.text('spAdvDrafts'), say('1 value to fix before saving', '1 قيمة تحتاج تصحيحاً قبل الحفظ'));
  assert.equal(ui.$('spAdvReset').disabled, false);
  ui.$('spGenerate').click();
  await settle();
  assert.equal('course_overrides' in ui.requests.find(r => r.url === GENERATE).body, false, 'an invalid draft is no what-if');
});

test('Arabic-Indic digits are read as the number they show', async t => {
  const ui = await page(t);
  await ui.type('AI491', '٣٠');

  ui.$('spAdvSaveDb').click();
  await settle();

  assert.deepEqual(ui.writes()[0].body.changes, [{ course_code: 'AI491', max_capacity: 30, all_programmes: false }]);
});

test('removing a saved limit is its own confirmed action and keeps other drafts', async t => {
  let answers = [false, true];
  const ui = await page(t, { confirm: async () => answers.shift() });
  await ui.type('CS211', '33');

  ui.row('AI491').querySelector('.adv-remove').click();
  await settle();
  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true], 'cancelled: no commit');
  assert.deepEqual(ui.writes()[0].body.changes, [{ course_code: 'AI491', max_capacity: null, all_programmes: false }]);
  assert.equal(ui.dialogs[0].title, say('Remove the saved limit for AI491?', 'إزالة الحد المحفوظ لـ AI491؟'));

  ui.row('AI491').querySelector('.adv-remove').click();
  await settle();
  const commit = ui.writes().at(-1).body;
  assert.equal(commit.dry_run, false);
  assert.deepEqual(commit.changes, [{ course_code: 'AI491', max_capacity: null, all_programmes: false }]);
  // The reload keeps the other row's draft.
  assert.equal(ui.input('CS211').value, '33');
  assert.ok(ui.row('CS211').classList.contains('sp-adv-draft'));
});

test('a refused save is explained in the page\'s words and keeps the drafts', async t => {
  const ui = await page(t, {
    limits: limitsServer({ commit: () => answer({ ok: false, code: 'audit_unavailable', error: 'server words' }, 503) }),
  });
  await ui.type('AI491', '6');

  ui.$('spAdvSaveDb').click();
  await settle();

  assert.equal(ui.text('spStatus'), say(
    "Couldn't record the change in the audit log, so nothing was saved. Try again.",
    'تعذّر تسجيل التغيير في سجل التدقيق، لذلك لم يُحفظ شيء. حاول مرة أخرى.',
  ));
  assert.equal(ui.$('spStatus').getAttribute('role'), 'alert');
  assert.ok(ui.row('AI491').classList.contains('sp-adv-draft'));
});

/* ── "Our" departments come from the server ── */

function summaryPlan() {
  const plan = [planRow('AI491'), planRow('COE211'), planRow('MATH203')];
  const data = generated(plan);
  data.summary.departments = ['AI', 'COE', 'MATH'].map(department => ({ department, courses: 1, sections: 1, students: 30, total_credits: 3 }));
  return data;
}

/* The summary's groups in order, each with its departments. */
const summaryGroups = (ui, root = ui.$('spDeptSummary')) => [...root.querySelectorAll('tbody[data-group]')]
  .map(group => [group.dataset.group, [...group.querySelectorAll('tr[data-dept]')].map(tr => tr.dataset.dept)]);

test('the Department Summary lists every department: ours (the server\'s list, COE included), then the service departments', async t => {
  const ui = await page(t, { generate: () => answer(summaryPlan()) });
  assert.deepEqual(JSON.parse(ui.$('spLocalDepartments').textContent), ['AI', 'COE', 'CS', 'CYB', 'DS', 'IS']);

  ui.$('spGenerate').click();
  await settle();

  assert.deepEqual(summaryGroups(ui), [['ours', ['AI', 'COE']], ['service', ['MATH']]]);
  assert.deepEqual([...ui.window.document.querySelectorAll('#spDeptSummary .sp-sum-group th')].map(th => th.textContent),
    say(['Our departments', 'Service departments (sections to request)'], ['أقسامنا', 'الأقسام الخدمية (شعب تُطلب منها)']));
});

test('the page keeps no department list of its own', async t => {
  const ui = await page(t, { generate: () => answer(summaryPlan()), localDepartments: ['MATH'] });

  ui.$('spGenerate').click();
  await settle();

  assert.deepEqual(summaryGroups(ui), [['ours', ['MATH']], ['service', ['AI', 'COE']]]);
});

/* ── One section per plan: Male (M) or Female (F) ── */

const radios = ui => ['M', 'F'].map(code => ui.$(`spSection${code}`));
const choose = say('Choose Male (M) or Female (F).', 'اختر شطر الطلاب (M) أو الطالبات (F).');

test('the Section control is a required radio group, named in the page\'s language, inside the scope form', async t => {
  const ui = await page(t, { section: null });
  const group = ui.$('spSectionGroup');
  assert.equal(group.getAttribute('role'), 'radiogroup');
  assert.equal(group.getAttribute('aria-required'), 'true');
  assert.equal(ui.$(group.getAttribute('aria-labelledby')).textContent.trim(), say('Section', 'الشطر'));
  assert.equal(group.getAttribute('aria-describedby'), 'spSectionMsg');
  assert.deepEqual(radios(ui).map(r => [r.type, r.name, r.value, r.form === ui.$('spScopeForm')]),
    [['radio', 'spSection', 'M', true], ['radio', 'spSection', 'F', true]]);
  assert.deepEqual(radios(ui).map(r => r.labels[0].textContent.replace(/\s+/g, ' ').trim()),
    say(['Male (M)', 'Female (F)'], ['طلاب (M)', 'طالبات (F)']));
});

test('a first visit chooses nothing, and Generate then asks nothing of the server: an alert at the control takes the keyboard', async t => {
  const ui = await page(t, { section: null });
  assert.deepEqual(radios(ui).map(r => [r.checked, r.hasAttribute('checked')]), [[false, false], [false, false]]);

  ui.$('spGenerate').click();
  await settle();
  ui.$('spScopeForm').requestSubmit();   // Enter in a field
  await settle();

  assert.deepEqual(ui.generates(), [], 'no request without a section');
  assert.equal(ui.$('spSectionMsg').getAttribute('role'), 'alert');
  assert.equal(ui.text('spSectionMsg'), choose);
  assert.equal(ui.$('spSectionGroup').getAttribute('aria-invalid'), 'true');
  assert.equal(ui.window.document.activeElement, ui.$('spSectionM'), 'focus moves to the group');
  assert.equal(ui.text('spStatus'), '', 'one message, at the control');

  ui.$('spSectionF').click();
  assert.equal(ui.text('spSectionMsg'), '', 'choosing answers it');
  assert.equal(ui.$('spSectionGroup').hasAttribute('aria-invalid'), false);
  ui.$('spGenerate').click();
  await settle();
  assert.deepEqual(ui.generates().map(r => r.body.section), ['F']);
});

test('the choice is remembered in this browser, and a remembered choice is sent', async t => {
  const first = await page(t, { section: 'F' });
  assert.equal(first.window.localStorage.getItem(SECTION_KEY), 'F');

  const again = await page(t, { section: null, stored: 'F' });
  assert.deepEqual(radios(again).map(r => r.checked), [false, true]);
  again.$('spGenerate').click();
  await settle();
  assert.equal(again.generates()[0].body.section, 'F');

  const odd = await page(t, { section: null, stored: 'X' });
  assert.deepEqual(radios(odd).map(r => r.checked), [false, false], 'only M or F is ever restored');
});

for (const storage of ['throws', 'fails']) {
  test(`with browser storage that ${storage === 'throws' ? 'cannot be reached' : 'refuses reads and writes'}, the page still plans`, async t => {
    const ui = await page(t, { section: null, stored: 'F', storage });
    assert.deepEqual(radios(ui).map(r => r.checked), [false, false], 'nothing could be read');

    ui.$('spSectionM').click();
    ui.$('spGenerate').click();
    await settle();

    assert.deepEqual(ui.generates().map(r => r.body.section), ['M']);
    assert.equal(ui.text('spStatus'), say('Section plan generated successfully.', 'تم حساب خطة الشعب بنجاح.'));
  });
}

test('Reset clears the scope and keeps the chosen section: a preference, not a draft', async t => {
  const ui = await page(t, { section: 'F' });
  ui.$('spGenerate').click();
  await settle();

  ui.$('spReset').click();
  await settle();

  assert.deepEqual(radios(ui).map(r => r.checked), [false, true]);
  assert.equal(ui.window.localStorage.getItem(SECTION_KEY), 'F');
  assert.ok(ui.$('spResults').classList.contains('d-none'));
});

for (const [code, words] of [
  ['section_required', choose],
  ['section_invalid', say('That section is not valid: choose Male (M) or Female (F).', 'الشطر غير صالح: اختر شطر الطلاب (M) أو الطالبات (F).')],
]) {
  test(`a Generate refused as ${code} is said at the Section control, in the page's words`, async t => {
    const ui = await page(t, { section: 'F', generate: () => answer({ ok: false, code, error: 'server words' }, 400) });
    ui.$('spGenerate').click();
    await settle();

    assert.equal(ui.text('spSectionMsg'), words);
    assert.equal(ui.window.document.activeElement, ui.$('spSectionF'), 'the chosen radio takes the keyboard');
    assert.equal(ui.text('spStatus'), '');
  });
}

/* A plan as the server answers it for one section (section_plan_pipeline). */
function sectionPlan(section = 'F', { noSection = 0 } = {}) {
  const plan = [
    planRow('AI331', { total_students: 30, num_sections: 2, max_per_section: 25, credit_hours: 4 }),
    planRow('MATH203', { total_students: 12, num_sections: 1, max_per_section: 50 }),
  ];
  const data = generated(plan, section);
  data.student_count = 42;
  data.no_section = noSection;
  data.summary = summaryOf(plan);
  return data;
}

test('the result names what it is for: programme, term and section', async t => {
  for (const [program, section, heading] of [
    ['AI', 'F', say('AI · 1448 T1 · Female (F)', 'AI · 1448 ف1 · طالبات (F)')],
    ['', 'M', say('All programmes · 1448 T1 · Male (M)', 'كل البرامج · 1448 ف1 · طلاب (M)')],
    ['AI, DS', 'M', say('AI, DS · 1448 T1 · Male (M)', 'AI, DS · 1448 ف1 · طلاب (M)')],
  ]) {
    const ui = await page(t, { program, section, generate: body => answer(sectionPlan(body.section)) });
    ui.$('spGenerate').click();
    await settle();
    assert.equal(ui.text('spResultsScope'), heading, program || 'all');
    const codes = [...ui.$('spResultsScope').querySelectorAll('bdi')].map(b => b.textContent);
    assert.ok(codes.includes(`(${section})`) && codes.includes('1448'), codes.join('|'));
  }
});

test('Export sends the section the result on screen is for, even after the radio changed, and saves under the server\'s name', async t => {
  let disposition = 'attachment; filename="section_plan_1448_1_AI_F.xlsx"';
  const ui = await page(t, { section: 'F', generate: body => answer(sectionPlan(body.section)), exportFile: () => fileAnswer(disposition) });
  ui.$('spGenerate').click();
  await settle();

  ui.$('spSectionM').click();   // changed since: the result is still the women's
  ui.$('spExport').click();
  await settle();
  disposition = '';             // no name from the server: one made the same way
  ui.$('spExport').click();
  await settle();

  const sent = ui.requests.filter(r => r.url === EXPORT).map(r => r.body.section);
  assert.deepEqual(sent, ['F', 'F']);
  assert.deepEqual(ui.downloads, ['section_plan_1448_1_AI_F.xlsx', 'section_plan_1448_1_F.xlsx']);
  assert.equal(ui.text('spResultsScope'), say('AI · 1448 T1 · Female (F)', 'AI · 1448 ف1 · طالبات (F)'));
});

test('a result that arrives after the radio changed is named, and exported, for the section it was generated for', async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const ui = await page(t, {
    section: 'F',
    generate: async body => { await gate; return answer(sectionPlan(body.section)); },
    exportFile: () => fileAnswer('attachment; filename="section_plan_1448_1_AI_F.xlsx"'),
  });
  ui.$('spGenerate').click();
  await settle();
  ui.$('spSectionM').click();   // while the women's plan is being computed
  release();
  await settle();

  assert.equal(ui.text('spResultsScope'), say('AI · 1448 T1 · Female (F)', 'AI · 1448 ف1 · طالبات (F)'));
  ui.$('spExport').click();
  await settle();
  assert.equal(ui.requests.find(r => r.url === EXPORT).body.section, 'F');
});

test('a result shows its one section: no M, F or Total in the table, the phone cards, the KPIs or the summary', async t => {
  const ui = await page(t, { generate: body => answer(sectionPlan(body.section)) });
  ui.$('spGenerate').click();
  await settle();
  const doc = ui.window.document;

  assert.deepEqual([...doc.querySelectorAll('#spTable thead th')].map(th => th.textContent.trim()), say(
    ['#', 'Dept', 'Course', 'Course Name', 'Cr', 'Students', 'Sections', 'Max', 'Avg', 'Fill', 'Status'],
    ['#', 'القسم', 'المقرر', 'اسم المقرر', 'ساعات', 'الطلاب', 'الشعب', 'الحد الأقصى', 'المتوسط', 'الامتلاء', 'الحالة'],
  ));
  const tr = doc.querySelector('#spTable tbody tr[data-code="AI331"]');
  assert.equal(tr.querySelector('.sp-c-demand').textContent, '30');
  assert.equal(tr.querySelector('.sp-c-sections').textContent, '2');
  assert.equal(tr.querySelector('.sp-cell-sub'), null, 'no M · F line under the students');
  assert.equal(doc.querySelectorAll('.sp-sec-m, .sp-sec-f, .sp-sec-total, #spKpiStudentsSplit, #spKpiSectionsSplit, #spGenderNote').length, 0);
  assert.deepEqual([...doc.querySelectorAll('#spDeptSummary thead th')].map(th => th.textContent), say(
    ['Department', 'Sections', 'Courses', 'Seat demand', 'Teaching hours'],
    ['القسم', 'الشعب', 'المقررات', 'المقاعد المطلوبة', 'ساعات التدريس'],
  ));
  const text = doc.querySelector('.sp-page').textContent;
  assert.doesNotMatch(text, /\bM \d|\bF \d|M \+ F|Total sections|ذكور|إناث|مجموع الشعب/);
  assert.equal(ui.text('spKpiStudents'), '42');
  assert.equal(ui.text('spKpiSections'), '3');
});

for (const [n, en, ar] of [
  [1, '1 student has no recorded section and is not in this plan.', '1 طالب بلا شطر مسجَّل، فهو خارج هذه الخطة.'],
  [2, '2 students have no recorded section and are not in this plan.', '2 طالبان بلا شطر مسجَّل، فهما خارج هذه الخطة.'],
  [3, '3 students have no recorded section and are not in this plan.', '3 طلاب بلا شطر مسجَّل، فهم خارج هذه الخطة.'],
]) {
  test(`${n} student(s) with no recorded section are said to be in neither plan`, async t => {
    const ui = await page(t, { generate: body => answer(sectionPlan(body.section, { noSection: n })) });
    ui.$('spGenerate').click();
    await settle();
    assert.ok(!ui.$('spNoSectionNote').classList.contains('d-none'));
    assert.equal(ui.text('spNoSectionNote'), say(en, ar));
    assert.equal(ui.text('spKpiSections'), '3', 'and in no count');
  });
}

test('with every student\'s section recorded there is no note', async t => {
  const ui = await page(t, { generate: body => answer(sectionPlan(body.section)) });
  ui.$('spGenerate').click();
  await settle();
  assert.ok(ui.$('spNoSectionNote').classList.contains('d-none'));
  assert.equal(ui.text('spNoSectionNote'), '');
});
/* ── A resolved elective is planned under its slot ── */

test('the panel shows the elective a slot resolves to, under the slot', async t => {
  const ui = await page(t);
  const get = ui.requests.find(r => r.method === 'GET');
  const params = new URL(get.url, 'http://planning.test').searchParams;
  assert.equal(params.get('year'), ui.$('spYear').value);
  assert.equal(params.get('semester'), ui.$('spSemester').value);

  const sub = ui.window.document.querySelector('#spAdvBody tr.sp-adv-sub[data-slot-of="AI1"]');
  assert.ok(sub, 'a sub-row under AI1');
  assert.equal(sub.previousElementSibling.dataset.code, 'AI1');
  assert.equal(sub.textContent.replace(/\s+/g, ' ').trim(), say(
    "AI463 ← AI1 uses this slot's limit",
    'AI463 → AI1 بحد هذه الخانة',
  ));
  assert.equal(ui.window.document.querySelectorAll('#spAdvBody tr[data-code]').length, 4, 'the sub-row is no draft row');

  ui.$('spAdvSearch').value = 'ai463';
  ui.emit(ui.$('spAdvSearch'), 'input');
  assert.equal(sub.style.display, '');
  assert.equal(ui.row('AI491').style.display, 'none');
});

test('a resolved elective names its slot and where its limit comes from', async t => {
  const data = generated([planRow('AI463', {
    num_sections: 3, total_students: 70, max_per_section: 30, limit_source: 'slot', slots: ['AI1'], is_external: true,
  })]);
  data.electives = { dropped_total: 8, dropped: [
    { program: 'AI', slot: 'AI2', reason: 'not_published', students: 5 },
    { program: 'AI', slot: 'AI3', reason: 'no_eligible_course', students: 3 },
  ] };
  const ui = await page(t, { generate: () => answer(data) });
  ui.$('spGenerate').click();
  await settle();

  const tr = ui.window.document.querySelector('#spTable tbody tr[data-code="AI463"]');
  const slot = tr.querySelector('.sp-slot');
  assert.equal(slot.textContent.trim(), say('← AI1', '→ AI1'));
  assert.equal(slot.title, say('Fills elective slot AI1', 'يملأ الخانة الاختيارية AI1'));
  assert.equal(tr.querySelector('.sp-limit-src').textContent, say('AI1 limit', 'حد AI1'));
  assert.equal(ui.text('spElectiveNote'), say(
    '8 elective-slot requests became no course and are not in the plan: AI AI2 — not published for this term (5); AI AI3 — no course the student is eligible for (3).',
    '8 طلب لخانة اختيارية لم يصبح مقرراً وليس في الخطة: AI AI2 — غير منشورة لهذا الفصل (5); AI AI3 — لا مقرر مؤهَّل له الطالب (3).',
  ));
});

/* ── The real confirmation dialog: Enter on "Keep editing" never writes ── */

test('Enter on "Keep editing" cancels a save: only the preview is sent, focus returns to Save', async t => {
  const ui = await page(t, { realDialogs: true });
  await ui.type('AI491', '6');
  const save = ui.$('spAdvSaveDb');
  save.focus();
  save.click();
  await settle();
  await pause(80);

  const dialog = ui.window.document.querySelector('.dlg-backdrop');
  assert.ok(dialog, 'the review is open');
  const cancel = dialog.querySelector('.btn-cancel');
  assert.equal(cancel.textContent, say('Keep editing', 'متابعة التعديل'));
  assert.equal(ui.window.document.activeElement, cancel, 'a review starts on "Keep editing"');
  const body = ui.window.document.getElementById(dialog.getAttribute('aria-describedby'));
  assert.ok(body && body.querySelector('.sp-limit-review'), 'the rows are the dialog\'s description');

  ui.key(cancel, 'Enter');
  await pause(260);
  await settle();

  assert.equal(ui.window.document.querySelector('.dlg-backdrop'), null);
  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true], 'Enter on Cancel never commits');
  assert.ok(ui.row('AI491').classList.contains('sp-adv-draft'), 'the draft is kept');
  assert.equal(ui.window.document.activeElement, save);
});

test('Enter on "Keep editing" cancels a removal', async t => {
  const ui = await page(t, { realDialogs: true });
  const remove = ui.row('AI491').querySelector('.adv-remove');
  remove.focus();
  remove.click();
  await settle();
  await pause(80);

  const cancel = ui.window.document.querySelector('.dlg-backdrop .btn-cancel');
  assert.equal(ui.window.document.activeElement, cancel);
  ui.key(cancel, 'Enter');
  await pause(260);
  await settle();

  assert.deepEqual(ui.writes().map(w => [w.body.dry_run, w.body.changes[0].max_capacity]), [[true, null]]);
  assert.equal(ui.window.document.activeElement, remove);
});

test('a confirmed save and a confirmed removal put the keyboard back in the panel', async t => {
  const ui = await page(t, { realDialogs: true });
  await ui.type('AI491', '6');
  ui.$('spAdvSaveDb').focus();
  ui.$('spAdvSaveDb').click();
  await settle();
  assert.equal(ui.$('spAdvSaveDb').textContent, say('Checking…', 'جارٍ التحقق…'), 'reviewing is not saving');
  await pause(80);
  ui.window.document.querySelector('.dlg-backdrop .btn-confirm').click();
  await pause(260);
  await settle();

  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true, false]);
  assert.equal(ui.$('spAdvSaveDb').textContent, say('Save limits…', 'حفظ الحدود…'));
  assert.equal(ui.window.document.activeElement, ui.$('spAdvSearch'), 'nothing left to save: back to Search');

  ui.row('CS211').querySelector('.adv-remove').click();
  await settle();
  await pause(80);
  ui.window.document.querySelector('.dlg-backdrop .btn-confirm').click();
  await pause(260);
  await settle();
  assert.equal(ui.writes().at(-1).body.dry_run, false);
  assert.equal(ui.window.document.activeElement, ui.input('CS211'), 'the removed row\'s field, re-rendered');
});

test('Discard puts the keyboard on Search, not on the page', async t => {
  const ui = await page(t);
  await ui.type('AI491', '6');
  ui.$('spAdvReset').focus();
  ui.$('spAdvReset').click();
  assert.equal(ui.window.document.activeElement, ui.$('spAdvSearch'));
});

/* ── several programmes on screen ── */

for (const typed of ['AI,DS', 'AI, DS']) {
  test(`with "${typed}" on screen a save names both, and a limit only AI saved shows as mixed`, async t => {
    const ui = await page(t, { program: typed });
    assert.equal(ui.input('AI491').value, '', 'AI saved 5, DS nothing: no single value');
    assert.equal(ui.input('AI491').placeholder, say('mixed', 'مختلف'));
    assert.ok(!ui.row('AI491').classList.contains('sp-adv-draft'));

    await ui.type('AI491', '7');
    ui.$('spAdvSaveDb').click();
    await settle();
    assert.deepEqual(ui.writes()[0].body.programs, ['AI', 'DS']);
    assert.deepEqual(ui.writes()[0].body.changes, [{ course_code: 'AI491', max_capacity: 7, all_programmes: false }]);

    ui.row('CS211').querySelector('.adv-remove').click();
    await settle();
    const removal = ui.writes().filter(w => w.body.changes[0].course_code === 'CS211');
    assert.deepEqual(removal[0].body.programs, ['AI', 'DS']);
    assert.deepEqual(removal[0].body.changes, [{ course_code: 'CS211', max_capacity: null, all_programmes: false }]);
  });
}

/* ── refusals, repeats, leaving ── */

for (const [status, data, words] of [
  [400, { ok: false, code: 'course_not_in_programmes', course_code: 'AI491', error: 'server words' },
    ['AI491 is not taught by the programmes on screen.', 'AI491 لا يُدرَّس في البرامج المعروضة.']],
  [403, { error: 'Insufficient role' }, ['You are not allowed to save limits.', 'لا تملك صلاحية حفظ الحدود.']],
]) {
  test(`a refused preview (${status}) is explained, commits nothing and keeps the drafts`, async t => {
    const ui = await page(t, { limits: limitsServer({ preview: () => answer(data, status) }) });
    await ui.type('AI491', '6');
    ui.$('spAdvSaveDb').click();
    await settle();

    assert.equal(ui.text('spStatus'), say(...words));
    assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true]);
    assert.equal(ui.dialogs.length, 0);
    assert.ok(ui.row('AI491').classList.contains('sp-adv-draft'));
  });
}

test('a second press of Save while the review is loading sends nothing more', async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const ui = await page(t, { limits: limitsServer({ preview: async body => { await gate; return limitsServer()(body); } }) });
  await ui.type('AI491', '6');

  ui.$('spAdvSaveDb').click();
  ui.$('spAdvSaveDb').click();
  await settle();
  assert.equal(ui.saveOff(), true, 'busy');
  release();
  await settle();

  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true, false]);
  assert.equal(ui.dialogs.length, 1);
});

test('leaving the page warns while a draft is unsaved, and not after Discard', async t => {
  const ui = await page(t);
  const leave = () => {
    const event = new ui.window.Event('beforeunload', { cancelable: true });
    ui.window.dispatchEvent(event);
    return event.defaultPrevented;
  };
  assert.equal(leave(), false);
  await ui.type('AI491', '6');
  assert.equal(leave(), true);
  ui.$('spAdvReset').click();
  assert.equal(leave(), false);
});

test('a preview with nothing to change drops those drafts and reloads the saved limits', async t => {
  const ui = await page(t, { limits: limitsServer({ preview: body => answer({ ok: true, dry_run: true, programs: body.programs, unchanged: 1, preview_token: 't', changes: [] }) }) });
  ui.row('AI491').querySelector('.adv-all').click();
  await settle();
  assert.equal(ui.text('spAdvBadge'), '1');

  ui.$('spAdvSaveDb').click();
  await settle();

  assert.equal(ui.text('spStatus'), say('Nothing to change: the saved limits already match.', 'لا شيء يتغيّر: الحدود المحفوظة مطابقة.'));
  assert.equal(ui.requests.filter(r => r.method === 'GET').length, 2, 'the panel was reloaded');
  assert.ok(!ui.row('AI491').classList.contains('sp-adv-draft'));
  assert.ok(ui.$('spAdvBadge').classList.contains('d-none'));
  assert.equal(ui.dialogs.length, 0);
});

test('a save refused because the limits moved reloads them and keeps the drafts', async t => {
  const ui = await page(t, { limits: limitsServer({ commit: () => answer({ ok: false, code: 'preview_stale', error: 'x' }, 409) }) });
  await ui.type('AI491', '6');
  ui.$('spAdvSaveDb').click();
  await settle();

  assert.equal(ui.requests.filter(r => r.method === 'GET').length, 2, 'reloaded');
  assert.equal(ui.input('AI491').value, '6');
  assert.ok(ui.row('AI491').classList.contains('sp-adv-draft'));
  assert.match(ui.text('spStatus'), AR ? /تغيّرت الحدود المحفوظة/ : /The saved limits changed since you reviewed them/);
});

test('a reload after a refused save keeps what the user typed, never turns back a limit someone else saved', async t => {
  /* Meanwhile another user saved AI491 = 8 and CS211 = 25 for AI. */
  let movedOn = false;
  const moved = { AI491: 8, CS211: 25 };
  const ui = await page(t, {
    courses: prog => answer({ ok: true, courses: coursesFor(prog).map(c => (movedOn && moved[c.course_code]
      ? { ...c, programme_max: moved[c.course_code], programme_limits: { AI: moved[c.course_code] } } : c)) }),
    limits: limitsServer({ commit: () => { movedOn = true; return answer({ ok: false, code: 'preview_stale', error: 'x' }, 409); } }),
  });
  ui.row('CS211').querySelector('.adv-all').click();   // scope only: its value is the pre-filled 30
  await ui.type('MATH203', '45');                      // a value the user typed
  await settle();

  ui.$('spAdvSaveDb').click();
  await settle();
  assert.equal(ui.requests.filter(r => r.method === 'GET').length, 2, 'reloaded after the 409');

  assert.equal(ui.input('AI491').value, '8', 'a row nobody here edited shows the new saved limit');
  assert.ok(!ui.row('AI491').classList.contains('sp-adv-draft'));
  assert.equal(ui.input('CS211').value, '25', 'the scope-only draft shows the limit saved now, not the old 30');
  /* It was ticked on 30: applying 25 everywhere is not what the user chose. */
  assert.equal(ui.row('CS211').querySelector('.adv-all').checked, false, 'its All programmes is cleared');
  assert.ok(!ui.row('CS211').classList.contains('sp-adv-draft'));
  assert.equal(ui.input('MATH203').value, '45', 'a typed value is kept');
  assert.equal(ui.text('spAdvDrafts'), say(
    '1 unsaved change · CS211: the limit shown changed, so All programmes was cleared; tick it again to apply the limit shown now',
    `1 تعديل غير محفوظ · ${isle('CS211')}: تغيّر الحد المعروض فأُلغي تحديد «كل البرامج»؛ حدّده مرة أخرى لتطبيق الحد المعروض الآن`,
  ));

  ui.$('spAdvSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes()[2].body.changes, [
    { course_code: 'MATH203', max_capacity: 45, all_programmes: false },
  ], 'the next save never writes 30 over the other user\'s 25, nor 25 everywhere unasked');
});

/* A row changed only in scope ("All programmes" ticked on the limit it showed). */
const tickAll = async (ui, code) => { ui.row(code).querySelector('.adv-all').click(); await settle(); };

test('a scope-only draft survives a reload that still shows the limit it was ticked on', async t => {
  const ui = await page(t, { limits: limitsServer({ commit: () => answer({ ok: false, code: 'preview_stale', error: 'x' }, 409) }) });
  await tickAll(ui, 'CS211');
  ui.$('spAdvSaveDb').click();
  await settle();
  assert.equal(ui.requests.filter(r => r.method === 'GET').length, 2, 'reloaded after the 409');

  assert.equal(ui.input('CS211').value, '30');
  assert.equal(ui.row('CS211').querySelector('.adv-all').checked, true, 'still 30: the choice stands');
  assert.ok(ui.row('CS211').classList.contains('sp-adv-draft'));
  assert.equal(ui.text('spAdvDrafts'), say('1 unsaved change', '1 تعديل غير محفوظ'));
});

for (const [name, moved] of [
  ['becomes mixed', { programmes: ['AI', 'DS'], programme_limits: { AI: 30, DS: 35 } }],
  ['loses its limit', { programme_max: null, programme_limits: {} }],
]) {
  test(`a scope-only draft whose row ${name} on a reload is cleared, and the panel says so`, async t => {
    let movedOn = false;
    const ui = await page(t, {
      courses: prog => answer({ ok: true, courses: coursesFor(prog).map(c => (movedOn && c.course_code === 'CS211' ? { ...c, ...moved } : c)) }),
      limits: limitsServer({ commit: () => { movedOn = true; return answer({ ok: false, code: 'limit_changed', error: 'x' }, 409); } }),
    });
    await tickAll(ui, 'CS211');
    ui.$('spAdvSaveDb').click();
    await settle();

    assert.equal(ui.input('CS211').value, '');
    assert.equal(ui.row('CS211').querySelector('.adv-all').checked, false, 'a ticked box would claim a change nobody can save');
    assert.ok(ui.$('spAdvBadge').classList.contains('d-none'));
    assert.match(ui.text('spAdvDrafts'), AR ? /فأُلغي تحديد «كل البرامج»/ : /^CS211: the limit shown changed, so All programmes was cleared/);
    ui.$('spAdvSaveDb').click();
    await settle();
    assert.equal(ui.writes().length, 2, 'nothing more is sent: only the refused preview and commit');
  });
}

test('a programme change keeps a scope-only draft only while its row shows the same limit', async t => {
  const ui = await page(t);
  await tickAll(ui, 'CS211');
  assert.equal(ui.text('spAdvDrafts'), say('1 unsaved change', '1 تعديل غير محفوظ'));

  await ui.setProgram('AI,DS');   // AI saved 30, DS nothing: mixed
  assert.equal(ui.input('CS211').placeholder, say('mixed', 'مختلف'));
  assert.equal(ui.row('CS211').querySelector('.adv-all').checked, false);
  assert.match(ui.text('spAdvDrafts'), AR ? /تغيّر الحد المعروض/ : /^CS211: the limit shown changed/);
  ui.$('spAdvSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes(), [], 'no limit the user never saw is saved');

  /* Acting on the row again: the note has been read. */
  await ui.type('CS211', '28');
  assert.equal(ui.text('spAdvDrafts'), say('1 unsaved change', '1 تعديل غير محفوظ'));
});

/* ── the rows on screen are the programme a save names ── */

test('while a new programme\'s list loads, or after it failed, Save sends nothing', async t => {
  let answerDs;
  const pending = new Promise(resolve => { answerDs = resolve; });
  const ui = await page(t, { courses: prog => (prog === 'DS' ? pending : answer({ ok: true, courses: coursesFor(prog) })) });
  await ui.type('CS211', '45');

  await ui.setProgram('DS');
  assert.equal(ui.saveOff(), true);
  ui.$('spAdvSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes(), [], 'loading: the table on screen is still AI\'s');
  assert.equal(ui.text('spStatus'), say('The course list is loading…', 'جارٍ تحميل قائمة المقررات…'));

  answerDs(answer({ ok: false, error: 'boom' }, 500));
  await settle();
  assert.equal(ui.saveOff(), true);
  ui.$('spAdvSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes(), [], 'failed: nothing is saved for DS with AI\'s rows');
  assert.match(ui.text('spStatus'), AR ? /لم تُحمَّل قائمة المقررات/ : /The course list didn't load/);
  assert.equal(ui.input('CS211').value, '45', 'the draft is still there');
});

test('an older list answering late does not replace the newer programme\'s rows', async t => {
  let answerCs;
  const slow = new Promise(resolve => { answerCs = resolve; });
  const ui = await page(t, { courses: prog => (prog === 'CS' ? slow : answer({ ok: true, courses: coursesFor(prog).map(c => ({ ...c, programme_limits: prog === 'DS' ? {} : c.programme_limits })) })) });

  await ui.setProgram('CS');
  await ui.setProgram('DS');
  answerCs(answer({ ok: true, courses: [coursesFor('CS')[0]] }));
  await settle();

  assert.equal(ui.window.document.querySelectorAll('#spAdvBody tr[data-code]').length, 4, 'DS\'s rows stay');
  await ui.type('CS211', '31');
  ui.$('spAdvSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes()[0].body.programs, ['DS']);
});

test('a draft for a course the new programme does not list comes back with the course', async t => {
  const ui = await page(t, { courses: prog => answer({ ok: true, courses: coursesFor(prog).filter(c => prog !== 'CS' || c.course_code !== 'AI491') }) });
  await ui.type('AI491', '6');

  await ui.setProgram('CS');
  assert.equal(ui.row('AI491'), null);
  assert.equal(ui.text('spAdvDrafts'), say('1 unsaved change for courses not on screen', '1 تعديل غير محفوظ لمقررات غير معروضة'));
  assert.equal(ui.text('spAdvBadge'), '1');

  await ui.setProgram('AI');
  assert.equal(ui.input('AI491').value, '6');
  assert.ok(ui.row('AI491').classList.contains('sp-adv-draft'));
  assert.equal(ui.text('spAdvDrafts'), say('1 unsaved change', '1 تعديل غير محفوظ'));
});

/* ── no programme on screen: the panel starts from what Generate applies ── */

test('with no programme the panel shows the lowest declared limit, and edits are only what-ifs', async t => {
  const ui = await page(t, { program: '' });
  assert.equal(ui.input('AI491').value, '5');
  assert.equal(ui.row('AI491').querySelector('.sp-adv-src').textContent, say('lowest declared', 'أدنى حد معلن'));
  assert.equal(ui.row('AI491').querySelector('.adv-remove'), null, 'nothing to remove from here');

  await ui.type('AI491', '5');
  assert.ok(!ui.row('AI491').classList.contains('sp-adv-draft'), 'the same limit Generate applies is no change');
  await ui.type('AI491', '40');
  assert.ok(ui.row('AI491').classList.contains('sp-adv-draft'));
  ui.$('spGenerate').click();
  await settle();
  assert.deepEqual(ui.requests.find(r => r.url === GENERATE).body.course_overrides, { AI491: 40 });

  const event = new ui.window.Event('beforeunload', { cancelable: true });
  ui.window.dispatchEvent(event);
  assert.equal(event.defaultPrevented, false, 'a what-if that cannot be saved is not "unsaved"');
});

/* ── Generate and Export refusals in the page's words ── */

for (const [status, data, words] of [
  [500, { ok: false, code: 'generate_failed', error: 'The plan could not be computed.' }, ['The plan could not be computed.', 'تعذّر حساب الخطة.']],
  [429, { error: 'Rate limit exceeded. Please try again later.' }, ['Too many requests. Wait a moment and try again.', 'طلبات كثيرة. انتظر قليلاً ثم حاول.']],
  [400, { ok: false, code: 'invalid_term', error: 'semester must be 1, 2, or 3' }, ['The year or semester is not valid.', 'السنة أو الفصل غير صالح.']],
]) {
  test(`a refused Generate (${status}) is explained in the page's language`, async t => {
    const ui = await page(t, { generate: () => answer(data, status) });
    ui.$('spGenerate').click();
    await settle();
    assert.equal(ui.text('spStatus'), say(...words));
  });
}

/* ── The scope is a form: Enter in a field runs Generate, never Save ── */

test('the scope form submits through Generate only; Save and the limit fields are outside it', async t => {
  const ui = await page(t);
  const form = ui.$('spScopeForm');
  assert.equal(form.tagName, 'FORM');
  const submits = [...form.elements].filter(el => el.type === 'submit');
  assert.deepEqual(submits.map(el => el.id), ['spGenerate'], 'Enter clicks the first submit button: Generate');
  for (const id of ['spYear', 'spSemester', 'spProgram', 'spSectionM', 'spSectionF']) assert.equal(ui.$(id).form, form, id);
  assert.equal(ui.$('spAdvSaveDb').form, null, 'Save is never a submit of the scope form');
  assert.equal(ui.input('AI491').form, null, 'Enter in a limit field submits nothing');

  form.requestSubmit();   // what Enter in Year, Semester or Program does
  await settle();

  assert.equal(ui.requests.filter(r => r.url === GENERATE).length, 1);
  assert.deepEqual(ui.writes().filter(r => r.url !== GENERATE), [], 'Generate never saves');
  assert.equal(ui.window.location.pathname, '/section-planning/', 'no page load');
});

test('a second submit while Generate runs sends nothing more', async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const ui = await page(t, { generate: async () => { await gate; return answer(generated()); } });

  ui.$('spScopeForm').requestSubmit();
  ui.$('spScopeForm').requestSubmit();
  await settle();
  assert.equal(ui.requests.filter(r => r.url === GENERATE).length, 1);

  release();
  await settle();
  ui.$('spScopeForm').requestSubmit();
  await settle();
  assert.equal(ui.requests.filter(r => r.url === GENERATE).length, 2, 'free again once the first answered');
});

/* ── A phone shows every course's numbers ── */

/* A plan's summary as the server builds it (section_planning.compute_plan_summary). */
function summaryOf(rows) {
  const departments = {};
  rows.forEach(row => {
    const d = departments[row.department] ||= { department: row.department, courses: 0, sections: 0, students: 0, total_credits: 0 };
    d.courses += 1; d.sections += row.num_sections;
    d.students += row.total_students; d.total_credits += 3 * row.num_sections;
  });
  return {
    total_courses: rows.length, total_sections: rows.reduce((n, r) => n + r.num_sections, 0),
    total_students: rows.reduce((n, r) => n + r.total_students, 0), avg_fill_percent: 50,
    departments: Object.values(departments).sort((a, b) => a.department.localeCompare(b.department)),
  };
}

function multiPlan() {
  const ai = [planRow('AI491', { num_sections: 7 }), planRow('MATH203', { num_sections: 1 })];
  const ds = [planRow('DS201', { num_sections: 2 }), planRow('MATH203', { num_sections: 1 })];
  const combined = [planRow('AI491', { num_sections: 7, programs: ['AI'] }),
    planRow('DS201', { num_sections: 2, programs: ['DS'] }),
    planRow('MATH203', { num_sections: 2, programs: ['AI', 'DS'] })];
  return {
    ok: true, mode: 'multi', year: 1448, semester: 1, section: 'M', student_count: 60, no_section: 0,
    combined_plan: combined, combined_summary: summaryOf(combined),
    programs: [
      { program: 'AI', student_count: 1, plan: ai, summary: summaryOf(ai) },
      { program: 'DS', student_count: 2, plan: ds, summary: summaryOf(ds) },
    ],
  };
}

const heads = (_ui, table) => [...table.querySelectorAll('thead th')].map(th => th.textContent.trim());

test('every result cell carries its column header as its phone label, and a card keeps the numbers', async t => {
  const ui = await page(t, { generate: body => answer(sectionPlan(body.section)) });
  ui.$('spGenerate').click();
  await settle();

  const table = ui.$('spTable');
  assert.ok(table.classList.contains('mobile-cards'));
  assert.ok(table.parentElement.classList.contains('sp-table-scroll'), 'wider than the pane: it scrolls, not the page');
  const labels = heads(ui, table);
  const cells = [...table.querySelectorAll('tbody tr[data-code="AI331"] td')];
  assert.deepEqual(cells.map(td => td.dataset.label), labels);
  const hidden = cells.filter(td => td.classList.contains('mc-hide')).map(td => td.dataset.label);
  assert.deepEqual(hidden, say(['#', 'Dept', 'Cr', 'Avg'], ['#', 'القسم', 'ساعات', 'المتوسط']));
  const card = cls => cells.find(td => td.classList.contains(cls));
  assert.equal(card('sp-c-demand').querySelector('strong').textContent, '30');
  assert.equal(card('sp-c-sections').querySelector('strong').textContent, '2');
  assert.match(card('sp-c-max').textContent, /^25/);
  assert.ok(card('sp-c-fill').querySelector('.sp-fill'));
  assert.deepEqual(cells.filter(td => td.classList.contains('mc-primary')).map(td => td.dataset.label), labels.slice(2, 4));
  assert.equal(card('sp-c-status').innerHTML, '', 'no status: an empty cell a phone leaves out');
});

test('each programme\'s table has the server\'s header and the same phone cards', async t => {
  const ui = await page(t, { program: 'AI,DS', generate: () => answer(multiPlan()) });
  ui.$('spGenerate').click();
  await settle();

  const tables = [...ui.window.document.querySelectorAll('#spMultiPrograms table.sp-plan-table')];
  assert.equal(tables.length, 2);
  for (const table of tables) {
    assert.deepEqual(heads(ui, table), heads(ui, ui.$('spTable')));
    assert.ok(table.classList.contains('mobile-cards') && table.classList.contains('sp-plan-table'));
    assert.ok(table.parentElement.classList.contains('sp-table-scroll'));
    const row = table.querySelector('tbody tr[data-code]');
    assert.deepEqual([...row.children].map(td => td.dataset.label), heads(ui, table));
  }
});

/* ── Names, states and roles a screen reader and a keyboard rely on ── */

test('the disclosure toggles say what they open and whether it is open', async t => {
  const ui = await page(t);
  const caps = ui.$('spToggleCaps');
  assert.equal(caps.tagName, 'BUTTON');
  assert.equal(caps.type, 'button');
  assert.equal(caps.getAttribute('aria-controls'), 'spCapsWrap');
  assert.equal(caps.getAttribute('aria-expanded'), 'false');
  caps.click();
  assert.equal(caps.getAttribute('aria-expanded'), 'true');
  assert.ok(!ui.$('spCapsWrap').classList.contains('d-none'));
  caps.click();
  assert.equal(caps.getAttribute('aria-expanded'), 'false');
  assert.ok(ui.$('spCapsWrap').classList.contains('d-none'));

  const adv = ui.$('spToggleAdv');
  assert.equal(adv.getAttribute('aria-controls'), 'spAdvPanel');
  assert.equal(adv.getAttribute('aria-expanded'), 'true', 'the harness opened it');
});

test('every field has an accessible name in the page\'s language', async t => {
  const ui = await page(t);
  const label = id => ui.$(id).labels[0]?.textContent.trim();
  assert.deepEqual(['spYear', 'spSemester', 'spProgram', 'spDeptFilter'].map(label),
    say(['Year', 'Semester', 'Program', 'Filter Dept'], ['السنة', 'الفصل', 'البرنامج', 'تصفية القسم']));
  assert.deepEqual(['spCapLocal4', 'spCapLocalOther', 'spCapExternal'].map(label),
    say(['Local 4+ cr', 'Local other', 'External'], ['محلي 4+ ساعات', 'محلي أخرى', 'خارجي']));
  assert.equal(ui.$('spAdvSearch').getAttribute('aria-label'), say('Search courses', 'بحث عن مقرر'));
  for (const tr of ui.window.document.querySelectorAll('#spAdvBody tr[data-code]')) {
    const code = tr.dataset.code;
    assert.equal(tr.querySelector('.adv-input').getAttribute('aria-label'), say(`Seat limit for ${code}`, `الحد الأقصى لشعبة ${code}`));
    assert.match(tr.querySelector('.adv-all').getAttribute('aria-label'), new RegExp(code));
  }
});

test('the status line is polite, an error is an alert, and a warning is amber, never the error style', async t => {
  let refuse = false;
  const ui = await page(t, { generate: () => (refuse ? answer({ ok: false, code: 'invalid_term' }, 400) : answer(generated())) });
  const status = ui.$('spStatus');
  assert.equal(status.getAttribute('role'), 'status');
  assert.equal(status.getAttribute('aria-live'), 'polite');

  ui.$('spAdvSaveDb').click();   // nothing to save: a warning
  await settle();
  assert.equal(ui.text('spStatus'), say('Nothing to change: the saved limits already match.', 'لا شيء يتغيّر: الحدود المحفوظة مطابقة.'));
  assert.ok(status.classList.contains('sp-alert-warn'));
  assert.ok(!status.classList.contains('sp-alert-err'));
  assert.equal(status.getAttribute('role'), 'status');
  assert.equal(status.getAttribute('aria-live'), 'polite');

  refuse = true;
  ui.$('spGenerate').click();
  await settle();
  assert.ok(status.classList.contains('sp-alert-err'));
  assert.equal(status.getAttribute('role'), 'alert');
  assert.equal(status.getAttribute('aria-live'), 'assertive');

  refuse = false;
  ui.$('spGenerate').click();
  await settle();
  assert.ok(status.classList.contains('sp-alert-ok'));
  assert.equal(status.getAttribute('role'), 'status');
  assert.equal(status.getAttribute('aria-live'), 'polite');
});

test('a programme block opens and closes from the keyboard', async t => {
  const ui = await page(t, { program: 'AI,DS', generate: () => answer(multiPlan()) });
  ui.$('spGenerate').click();
  await settle();

  const toggles = [...ui.window.document.querySelectorAll('#spMultiPrograms .sp-prog-toggle')];
  assert.equal(toggles.length, 2);
  const [toggle] = toggles;
  assert.equal(toggle.tagName, 'BUTTON');
  assert.equal(toggle.type, 'button');
  assert.equal(toggle.parentElement.tagName, 'H5', 'still a heading');
  const body = ui.$(toggle.getAttribute('aria-controls'));
  assert.ok(body && body.contains(body.querySelector('table')));
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.ok(body.classList.contains('d-none'));

  toggle.focus();
  ui.key(toggle, 'Enter');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.ok(!body.classList.contains('d-none'));
  assert.equal(ui.window.document.activeElement, toggle);
  ui.key(toggle, 'Enter');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.ok(body.classList.contains('d-none'));
});

function sortPlan() {
  return generated([planRow('AI491', { total_students: 30 }), planRow('CS211', { total_students: 10 }), planRow('MATH203', { total_students: 20 })]);
}
const codes = table => [...table.querySelectorAll('tbody tr[data-code]')].map(tr => tr.dataset.code);
/* How many times one press sorted: the shared sorter sets the pressed
 * header's aria-sort twice per sort (reset, then the direction). */
function sortsPerPress(window, th) {
  const seen = new window.MutationObserver(() => {});
  seen.observe(th, { attributes: true, attributeFilter: ['aria-sort'] });
  th.click();
  const records = seen.takeRecords().length;
  seen.disconnect();
  return records / 2;
}

test('a column sorts once per press however many results came before, and a new result keeps the sort', async t => {
  const ui = await page(t, { generate: () => answer(sortPlan()) });
  for (let i = 0; i < 3; i++) { ui.$('spGenerate').click(); await settle(); }
  const table = ui.$('spTable');
  const students = table.querySelector('thead th:nth-child(6)');

  assert.equal(sortsPerPress(ui.window, students), 1);
  assert.equal(students.getAttribute('aria-sort'), 'ascending');
  assert.deepEqual(codes(table), ['CS211', 'MATH203', 'AI491']);
  assert.equal(sortsPerPress(ui.window, students), 1);
  assert.equal(students.getAttribute('aria-sort'), 'descending');
  assert.deepEqual(codes(table), ['AI491', 'MATH203', 'CS211']);

  ui.$('spGenerate').click();
  await settle();
  assert.equal(students.getAttribute('aria-sort'), 'descending', 'the header still tells the truth');
  assert.deepEqual(codes(table), ['AI491', 'MATH203', 'CS211']);
  students.click();
  assert.deepEqual(codes(table), ['CS211', 'MATH203', 'AI491']);
});

test('a programme table sorts once per press after being opened and closed', async t => {
  const data = multiPlan();
  data.programs[0].plan = sortPlan().plan;
  const ui = await page(t, { program: 'AI,DS', generate: () => answer(data) });
  ui.$('spGenerate').click();
  await settle();
  const toggle = ui.window.document.querySelector('#spMultiPrograms .sp-prog-toggle');
  for (let i = 0; i < 3; i++) toggle.click();   // open, close, open
  const table = ui.$(toggle.getAttribute('aria-controls')).querySelector('table.sp-plan-table');
  const students = table.querySelector('thead th:nth-child(6)');

  assert.equal(sortsPerPress(ui.window, students), 1);
  assert.equal(students.getAttribute('aria-sort'), 'ascending');
  assert.deepEqual(codes(table), ['CS211', 'MATH203', 'AI491']);
});

/* ── The Department Summary adds up to the KPIs ── */

const cellsOf = tr => Object.fromEntries([...tr.querySelectorAll('[data-col]')].map(td => [td.dataset.col, Number(td.textContent)]));

test('the summary\'s total row equals the KPIs, with a subtotal per group', async t => {
  const plan = [
    planRow('AI331', { num_sections: 3 }),
    planRow('CS211', { num_sections: 2 }),
    planRow('MATH203', { num_sections: 2 }),
  ];
  const data = generated(plan);
  data.summary = summaryOf(plan);
  const ui = await page(t, { generate: () => answer(data) });
  ui.$('spGenerate').click();
  await settle();

  const doc = ui.window.document;
  assert.deepEqual(summaryGroups(ui), [['ours', ['AI', 'CS']], ['service', ['MATH']]]);
  const total = cellsOf(doc.querySelector('#spDeptSummary tfoot tr[data-sum-total]'));
  assert.equal(String(total.sections), ui.text('spKpiSections'));
  assert.equal(String(total.courses), ui.text('spKpiCourses'));
  assert.deepEqual([total.sections, total.courses], [7, 3]);
  assert.equal(cellsOf(doc.querySelector('#spDeptSummary [data-subtotal="ours"]')).sections, 5);
  assert.equal(cellsOf(doc.querySelector('#spDeptSummary [data-subtotal="service"]')).sections, 2);
  const heads = [...doc.querySelectorAll('#spDeptSummary thead th')].map(th => th.textContent);
  assert.deepEqual(heads, say(
    ['Department', 'Sections', 'Courses', 'Seat demand', 'Teaching hours'],
    ['القسم', 'الشعب', 'المقررات', 'المقاعد المطلوبة', 'ساعات التدريس'],
  ));
  // Each number sits under its own header, in every row.
  for (const tr of doc.querySelectorAll('#spDeptSummary tr[data-dept], #spDeptSummary tfoot tr')) {
    assert.deepEqual([...tr.children].slice(1).map(td => td.dataset.col), ['sections', 'courses', 'students', 'total_credits']);
  }
});
test('one group needs no subtotal', async t => {
  const plan = [planRow('AI331', { num_sections: 2 })];
  const data = generated(plan);
  data.summary = summaryOf(plan);
  const ui = await page(t, { generate: () => answer(data) });
  ui.$('spGenerate').click();
  await settle();
  assert.deepEqual(summaryGroups(ui), [['ours', ['AI']]]);
  assert.equal(ui.window.document.querySelectorAll('#spDeptSummary .sp-sum-sub').length, 0);
  assert.equal(cellsOf(ui.window.document.querySelector('#spDeptSummary [data-sum-total]')).sections, 2);
});

test('several programmes: the summary totals the pooled KPIs, and each programme\'s totals its heading', async t => {
  const data = multiPlan();
  const ui = await page(t, { program: 'AI,DS', generate: () => answer(data) });
  ui.$('spGenerate').click();
  await settle();

  const doc = ui.window.document;
  const pooled = cellsOf(doc.querySelector('#spDeptSummary [data-sum-total]'));
  assert.equal(String(pooled.sections), ui.text('spKpiSections'));
  assert.equal(String(pooled.courses), ui.text('spKpiCourses'));
  assert.deepEqual(summaryGroups(ui), [['ours', ['AI', 'DS']], ['service', ['MATH']]]);

  const blocks = [...doc.querySelectorAll('#spMultiPrograms .sp-prog-block')];
  assert.equal(blocks.length, 2);
  blocks.forEach((block, i) => {
    const prog = data.programs[i];
    const total = cellsOf(block.querySelector('[data-sum-total]'));
    assert.deepEqual([total.courses, total.sections], [prog.summary.total_courses, prog.summary.total_sections]);
    const heading = block.querySelector('.sp-prog-toggle').textContent.replace(/\s+/g, ' ');
    assert.ok(heading.includes(`${prog.summary.total_sections} `), heading);
    assert.ok(block.querySelector('.sp-sum-panel h6').textContent.includes(prog.program));
  });
  assert.deepEqual(summaryGroups(ui, blocks[1]), [['ours', ['DS']], ['service', ['MATH']]]);
});

/* ── Arabic and English words agree with their numbers ── */

test('programme headings count in words that agree with the number, numbers and codes as LTR islands', async t => {
  const ui = await page(t, { program: 'AI,DS', generate: () => answer(multiPlan()) });
  ui.$('spGenerate').click();
  await settle();

  const headings = [...ui.window.document.querySelectorAll('#spMultiPrograms .sp-prog-toggle')];
  assert.deepEqual(headings.map(h => h.textContent.replace(/\s+/g, ' ').trim()), say(
    ['AI (1 student · 2 courses · 8 sections)', 'DS (2 students · 2 courses · 3 sections)'],
    ['AI (1 طالب · 2 مقرران · 8 شعب)', 'DS (2 طالبان · 2 مقرران · 3 شعب)'],
  ));
  const [ai] = headings;
  assert.equal(ai.querySelector('.sp-prog-code').tagName, 'BDI');
  assert.deepEqual([...ai.querySelectorAll('.sp-prog-count bdi')].map(b => b.textContent), ['1', '2', '8']);
  const summaryNumbers = [...ui.window.document.querySelectorAll('#spDeptSummary td[data-col]')];
  assert.ok(summaryNumbers.length && summaryNumbers.every(td => td.firstElementChild?.tagName === 'BDI'));
  assert.ok([...ui.window.document.querySelectorAll('#spDeptSummary tr[data-dept] th')].every(th => th.firstElementChild?.tagName === 'BDI'));
});

for (const [n, en, ar] of [
  [1, '1 course', '1 مقرر'], [2, '2 courses', '2 مقرران'], [3, '3 courses', '3 مقررات'],
  [11, '11 courses', '11 مقرراً'], [100, '100 courses', '100 مقرر'],
]) {
  test(`the course list counts ${n} as "${say(en, ar)}"`, async t => {
    const course = i => ({ course_code: `X${i}`, department: 'X', credit_hours: 3, is_external: false, default_max: 40,
      programme_max: null, programmes: ['AI'], programme_limits: {} });
    const ui = await page(t, { courses: () => answer({ ok: true, courses: Array.from({ length: n }, (_, i) => course(i)) }) });
    assert.equal(ui.text('spAdvCount'), say(en, ar));
  });
}

test('every disclosure control carries one chevron the stylesheet turns, and no arrow glyph', async t => {
  const ui = await page(t, { program: 'AI,DS', generate: () => answer(multiPlan()) });
  ui.$('spGenerate').click();
  await settle();
  const toggles = [ui.$('spToggleCaps'), ui.$('spToggleAdv'), ...ui.window.document.querySelectorAll('.sp-prog-toggle')];
  assert.equal(toggles.length, 4);
  for (const toggle of toggles) {
    const chevrons = toggle.querySelectorAll(':scope > svg.sp-chev');
    assert.equal(chevrons.length, 1, toggle.id || toggle.className);
    assert.equal(chevrons[0].getAttribute('aria-hidden'), 'true');
    assert.doesNotMatch(toggle.textContent, /[▶▼◀]/);
    assert.ok(toggle.hasAttribute('aria-expanded'));
  }
  const [, , prog] = toggles;
  prog.click();
  assert.equal(prog.getAttribute('aria-expanded'), 'true');
  assert.equal(prog.querySelectorAll('svg.sp-chev').length, 1, 'the same chevron, turned by CSS');
});

/* ── One announcement per outcome, from a region that is always there ── */

test('a successful Generate is announced once, by a status line that never leaves the page', async t => {
  const ui = await page(t);
  const status = ui.$('spStatus');
  assert.ok(!status.classList.contains('d-none'), 'in the page before its first message');
  const seen = new ui.window.MutationObserver(() => {});
  seen.observe(status, { attributes: true, attributeFilter: ['class'], attributeOldValue: true });

  ui.$('spGenerate').click();
  await settle();
  ui.$('spReset').click();   // clears the line
  await settle();
  assert.equal(ui.text('spStatus'), '');
  ui.$('spGenerate').click();
  await settle();

  const classes = [...seen.takeRecords().map(record => record.oldValue || ''), status.className];
  seen.disconnect();
  assert.deepEqual(classes.filter(value => value.split(/\s+/).includes('d-none')), [], 'never display:none');
  const done = say('Section plan generated successfully.', 'تم حساب خطة الشعب بنجاح.');
  assert.equal(ui.text('spStatus'), done);
  assert.equal(status.getAttribute('role'), 'status');
  assert.deepEqual(ui.toasts, [], 'not a toast as well');
  const saying = [...ui.window.document.querySelectorAll('[role="status"], [role="alert"], [aria-live]')]
    .filter(region => region.textContent.includes(done));
  assert.deepEqual(saying.map(region => region.id), ['spStatus']);
});

/* ── The summary's rows are named by their headers ── */

test('each summary row is named by a row header, the total too, and each programme\'s summary by its own heading', async t => {
  const data = multiPlan();
  const ui = await page(t, { program: 'AI,DS', generate: () => answer(data) });
  ui.$('spGenerate').click();
  await settle();
  const doc = ui.window.document;

  function rowHeaders(root, summary) {
    const rows = [...root.querySelectorAll('tbody tr[data-dept]')];
    assert.deepEqual(rows.map(tr => tr.dataset.dept).sort(), summary.departments.map(d => d.department).sort());
    for (const tr of rows) {
      const head = tr.firstElementChild;
      assert.equal(head.tagName, 'TH', tr.dataset.dept);
      assert.equal(head.getAttribute('scope'), 'row', tr.dataset.dept);
      assert.equal(head.textContent.trim(), tr.dataset.dept);
    }
    const total = root.querySelector('tfoot tr[data-sum-total]').firstElementChild;
    assert.equal(total.tagName, 'TH');
    assert.equal(total.getAttribute('scope'), 'row');
    assert.equal(total.textContent.trim(), say('Total', 'المجموع'));
  }

  rowHeaders(ui.$('spDeptSummary'), data.combined_summary);
  const blocks = [...doc.querySelectorAll('#spMultiPrograms .sp-prog-block')];
  assert.equal(blocks.length, data.programs.length);
  blocks.forEach((block, i) => {
    const prog = data.programs[i];
    const region = block.querySelector('.sp-sum-panel');
    assert.equal(region.tagName, 'SECTION');
    const title = doc.getElementById(region.getAttribute('aria-labelledby') || '');
    assert.ok(title && region.contains(title), `${prog.program}: the region is named by its heading`);
    assert.equal(title.tagName, 'H6');
    assert.equal(title.textContent.replace(/\s+/g, ' ').trim(), `${say('Department Summary', 'ملخص الأقسام')} · ${prog.program}`);
    rowHeaders(region, prog.summary);
  });
});
