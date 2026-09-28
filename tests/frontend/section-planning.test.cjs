/*
 * Section Planning (static/js/page-section-planning.js) on the page its real
 * view renders, in English and Arabic: run through
 * tests/test_section_planning_frontend.py. The page script runs unmodified;
 * only HTTP answers and the shared confirmation dialog are scripted. Any
 * request the suite does not expect fails the test.
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
const PAGE = read('page-section-planning.js');

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };

const LIMITS = '/ops/section-planning/limits/';
const COURSES = '/ops/section-planning/courses/';
const GENERATE = '/ops/section-planning/generate/';

function coursesFor(program) {
  const programmes = program ? program.split(',') : [];
  const limits = saved => (program ? saved : {});
  return [
    { course_code: 'AI491', department: 'AI', credit_hours: 2, is_external: false, default_max: 40,
      programme_max: program ? 5 : null, programmes, programme_limits: limits({ AI: 5 }) },
    { course_code: 'CS211', department: 'CS', credit_hours: 3, is_external: false, default_max: 40,
      programme_max: program ? 30 : null, programmes, programme_limits: limits({ AI: 30 }) },
    { course_code: 'MATH203', department: 'MATH', credit_hours: 3, is_external: true, default_max: 50,
      programme_max: null, programmes, programme_limits: {} },
    { course_code: 'AI1', department: 'AI', credit_hours: 3, is_external: false, default_max: 40,
      programme_max: program ? 30 : null, programmes, programme_limits: limits({ AI: 30 }),
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

async function page(t, { program = 'AI', limits = limitsServer(), confirm = async () => true, generate = () => answer(generated()), localDepartments = null } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(template, { url: 'http://planning.test/section-planning/', runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  t.after(() => { window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  const toasts = [];
  const dialogs = [];
  window.notify = { success: message => toasts.push(['success', message]), error: message => toasts.push(['error', message]) };
  window.dlg = { confirm: async options => { dialogs.push(options); return confirm(options); } };
  const requests = [];
  window.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url, method, body });
    if (method === 'GET' && url.startsWith(COURSES)) {
      const prog = new URL(url, 'http://planning.test').searchParams.get('program') || '';
      return answer({ ok: true, courses: coursesFor(prog) });
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
  return { window, $, emit, row, input, type, requests, writes, dialogs, toasts, text };
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
  assert.equal(ui.$('spAdvSaveDb').disabled, false);
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
  assert.equal(ui.$('spAdvSaveDb').disabled, true);
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
  assert.equal(ui.$('spAdvSaveDb').disabled, true, 'no programme on screen: nothing can be saved');
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

  assert.equal(ui.$('spAdvSaveDb').disabled, true);
  assert.equal(ui.$('spAdvSaveDb').title, say('Choose a programme first: limits are saved per programme.', 'اختر برنامجاً أولاً: الحدود تُحفظ لكل برنامج.'));
  ui.$('spAdvSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes(), []);
});

test('an invalid value blocks Save and is marked on its field', async t => {
  const ui = await page(t);
  await ui.type('AI491', '0');

  assert.ok(ui.row('AI491').classList.contains('sp-adv-invalid'));
  assert.equal(ui.input('AI491').getAttribute('aria-invalid'), 'true');
  assert.equal(ui.row('AI491').querySelector('.sp-adv-state').textContent, say('Whole number 1–500', 'رقم صحيح من 1 إلى 500'));
  assert.equal(ui.$('spAdvSaveDb').disabled, true);
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
