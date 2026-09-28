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
const read = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const SHARED = read('shared-utils.js');
const DIALOG = read('dialog.js');
const PAGE = read('page-section-planning.js');

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

const LIMITS = '/ops/section-planning/limits/';
const COURSES = '/ops/section-planning/courses/';
const GENERATE = '/ops/section-planning/generate/';

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

function generated(plan = [planRow('AI491')]) {
  const sections = plan.reduce((sum, row) => sum + row.num_sections, 0);
  return {
    ok: true, mode: 'single', year: 1448, semester: 1, student_count: 324, plan,
    summary: { total_courses: plan.length, total_sections: sections, total_students: 30, avg_fill_percent: 75,
      departments: [{ department: 'AI', courses: plan.length, sections, students: 30, total_credits: 3 }] },
  };
}

function answer(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
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

async function page(t, { program = 'AI', limits = limitsServer(), confirm = async () => true, generate = () => answer(generated()), localDepartments = null, courses = prog => answer({ ok: true, courses: coursesFor(prog) }), realDialogs = false } = {}) {
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
    const error = new Error(`Unexpected HTTP request: ${method} ${url}`);
    errors.push(error);
    throw error;
  };
  if (localDepartments) window.document.getElementById('spLocalDepartments').textContent = JSON.stringify(localDepartments);
  const context = dom.getInternalVMContext();
  vm.runInContext(SHARED, context, { filename: 'shared-utils.js' });
  if (realDialogs) {
    window.requestAnimationFrame = callback => window.setTimeout(callback, 0);
    vm.runInContext(DIALOG, context, { filename: 'dialog.js' });
  }
  vm.runInContext(PAGE, context, { filename: 'page-section-planning.js' });
  const $ = id => window.document.getElementById(id);
  const emit = (element, type) => element.dispatchEvent(new window.Event(type, { bubbles: true }));
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
  return { window, $, emit, row, input, type, requests, writes, dialogs, toasts, text, saveOff, key, setProgram };
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

test('the Department Summary lists the departments the server calls ours, COE included', async t => {
  const ui = await page(t, { generate: () => answer(summaryPlan()) });
  assert.deepEqual(JSON.parse(ui.$('spLocalDepartments').textContent), ['AI', 'COE', 'CS', 'CYB', 'DS', 'IS']);

  ui.$('spGenerate').click();
  await settle();

  const shown = [...ui.window.document.querySelectorAll('#spDeptGrid .dept-name')].map(el => el.textContent);
  assert.deepEqual(shown, ['AI', 'COE']);
});

test('the page keeps no department list of its own', async t => {
  const ui = await page(t, { generate: () => answer(summaryPlan()), localDepartments: ['MATH'] });

  ui.$('spGenerate').click();
  await settle();

  const shown = [...ui.window.document.querySelectorAll('#spDeptGrid .dept-name')].map(el => el.textContent);
  assert.deepEqual(shown, ['MATH']);
});

/* ── Male and female are never planned together ── */

function cohortPlan({ noGender = 0 } = {}) {
  const row = planRow('AI331', {
    male_students: 12, female_students: 30, unknown_students: noGender,
    male_sections: 1, female_sections: 2, total_students: 42, num_sections: 3, max_per_section: 25,
  });
  const data = generated([row]);
  data.cohorts = { M: 12, F: 30, no_gender: noGender };
  data.student_count = 42 + noGender;
  Object.assign(data.summary, {
    total_sections: 3, male_sections: 1, female_sections: 2,
    no_gender: { students: noGender, seat_demand: noGender, courses: noGender ? 1 : 0 },
  });
  data.summary.departments = [{ department: 'AI', courses: 1, sections: 3, male_sections: 1, female_sections: 2, students: 42, total_credits: 12 }];
  return data;
}

test('Generate asks for both cohorts: there is no Section filter to send', async t => {
  const ui = await page(t);
  assert.equal(ui.$('spSection'), null);

  ui.$('spGenerate').click();
  await settle();

  const body = ui.requests.find(r => r.url === GENERATE).body;
  assert.equal('section' in body, false);
});

test('results show M, F and Total sections, and the KPIs split them', async t => {
  const ui = await page(t, { generate: () => answer(cohortPlan()) });
  ui.$('spGenerate').click();
  await settle();

  const tr = ui.window.document.querySelector('#spTable tbody tr[data-code="AI331"]');
  assert.equal(tr.querySelector('.sp-sec-m').textContent.trim(), '1');
  assert.equal(tr.querySelector('.sp-sec-f').textContent.trim(), '2');
  assert.equal(tr.querySelector('.sp-sec-total').textContent.trim(), '3');
  assert.equal(tr.querySelector('.sp-cell-sub').textContent.trim(), say('M 12 · F 30', 'ذكور 12 · إناث 30'));
  const heads = [...ui.window.document.querySelectorAll('#spTable thead th')].map(th => th.textContent.trim());
  assert.deepEqual(heads.slice(5, 9), say(
    ['Students', 'M sections', 'F sections', 'Total sections'],
    ['الطلاب', 'شعب الذكور', 'شعب الإناث', 'مجموع الشعب'],
  ));
  assert.equal(ui.text('spKpiSections'), '3');
  assert.equal(ui.text('spKpiSectionsSplit'), say('M 1 · F 2', 'ذكور 1 · إناث 2'));
  assert.equal(ui.text('spKpiStudentsSplit'), say('M 12 · F 30', 'ذكور 12 · إناث 30'));
  assert.match(ui.window.document.querySelector('#spDeptGrid .dept-stat').textContent, AR ? /ذكور 1 · إناث 2/ : /M 1 · F 2/);
  assert.ok(ui.$('spGenderNote').classList.contains('d-none'), 'nothing to report');
});

test('students with no recorded gender are stated, not pooled', async t => {
  const ui = await page(t, { generate: () => answer(cohortPlan({ noGender: 2 })) });
  ui.$('spGenerate').click();
  await settle();

  assert.ok(!ui.$('spGenderNote').classList.contains('d-none'));
  assert.equal(ui.text('spGenderNote'), say(
    '2 students have no recorded gender: 2 seats across 1 course, left out of every count. Record their gender, then generate again.',
    '2 طالب بلا جنس مسجّل: يحتاجون 2 مقعداً في 1 مقرر، وهم خارج كل الأعداد. سجّل جنسهم ثم أعد الحساب.',
  ));
  const tr = ui.window.document.querySelector('#spTable tbody tr[data-code="AI331"]');
  assert.equal(tr.querySelector('.sp-no-gender').textContent.trim(), say('+2 no gender', '+2 بلا جنس'));
  assert.equal(ui.text('spKpiSections'), '3', 'Total stays M + F');
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
    male_students: 70, female_students: 52, male_sections: 3, female_sections: 2, num_sections: 5,
    total_students: 122, max_per_section: 30, limit_source: 'slot', slots: ['AI1'], is_external: true,
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
  for (const id of ['spYear', 'spSemester', 'spProgram']) assert.equal(ui.$(id).form, form, id);
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

function multiPlan() {
  const ai = [planRow('AI491', { male_sections: 3, female_sections: 4, num_sections: 7 }), planRow('MATH203', { male_sections: 1, female_sections: 0, num_sections: 1 })];
  const ds = [planRow('DS201', { male_sections: 1, female_sections: 1, num_sections: 2 }), planRow('MATH203', { male_sections: 0, female_sections: 1, num_sections: 1 })];
  const combined = [planRow('AI491', { male_sections: 3, female_sections: 4, num_sections: 7, programs: ['AI'] }),
    planRow('DS201', { male_sections: 1, female_sections: 1, num_sections: 2, programs: ['DS'] }),
    planRow('MATH203', { male_sections: 1, female_sections: 1, num_sections: 2, programs: ['AI', 'DS'] })];
  const summaryOf = rows => {
    const departments = {};
    rows.forEach(row => {
      const d = departments[row.department] ||= { department: row.department, courses: 0, sections: 0, male_sections: 0, female_sections: 0, students: 0, total_credits: 0 };
      d.courses += 1; d.sections += row.num_sections; d.male_sections += row.male_sections; d.female_sections += row.female_sections;
      d.students += row.total_students; d.total_credits += 3 * row.num_sections;
    });
    return {
      total_courses: rows.length, total_sections: rows.reduce((n, r) => n + r.num_sections, 0),
      male_sections: rows.reduce((n, r) => n + r.male_sections, 0), female_sections: rows.reduce((n, r) => n + r.female_sections, 0),
      total_students: 0, avg_fill_percent: 50, departments: Object.values(departments).sort((a, b) => a.department.localeCompare(b.department)),
      no_gender: { students: 0, seat_demand: 0, courses: 0 },
    };
  };
  return {
    ok: true, mode: 'multi', year: 1448, semester: 1, student_count: 60, cohorts: { M: 25, F: 35, no_gender: 0 },
    combined_plan: combined, combined_summary: summaryOf(combined),
    programs: [
      { program: 'AI', student_count: 1, plan: ai, summary: summaryOf(ai) },
      { program: 'DS', student_count: 2, plan: ds, summary: summaryOf(ds) },
    ],
  };
}

const heads = (_ui, table) => [...table.querySelectorAll('thead th')].map(th => th.textContent.trim());

test('every result cell carries its column header as its phone label, and a card keeps the numbers', async t => {
  const ui = await page(t, { generate: () => answer(cohortPlan()) });
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
  assert.equal(card('sp-c-demand').querySelector('strong').textContent, '42');
  assert.equal(card('sp-sec-m').textContent, '1');
  assert.equal(card('sp-sec-f').textContent, '2');
  assert.equal(card('sp-sec-total').textContent, '3');
  assert.match(card('sp-c-max').textContent, /^25/);
  assert.ok(card('sp-c-fill').querySelector('.sp-fill'));
  assert.deepEqual(cells.filter(td => td.classList.contains('mc-primary')).map(td => td.dataset.label), labels.slice(2, 4));
  assert.equal(card('sp-c-status').innerHTML, '', 'no status: an empty cell a phone leaves out');
});

test('each programme\'s table has the server\'s header and the same phone cards', async t => {
  const ui = await page(t, { program: 'AI,DS', generate: () => answer(multiPlan()) });
  ui.$('spGenerate').click();
  await settle();

  const tables = [...ui.window.document.querySelectorAll('#spMultiPrograms table')];
  assert.equal(tables.length, 2);
  for (const table of tables) {
    assert.deepEqual(heads(ui, table), heads(ui, ui.$('spTable')));
    assert.ok(table.classList.contains('mobile-cards') && table.classList.contains('sp-plan-table'));
    assert.ok(table.parentElement.classList.contains('sp-table-scroll'));
    const row = table.querySelector('tbody tr[data-code]');
    assert.deepEqual([...row.children].map(td => td.dataset.label), heads(ui, table));
  }
});
