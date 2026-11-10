/*
 * Section Planning's weekly-meetings panel (static/js/page-section-planning.js)
 * on the page its real view renders, in English and Arabic: run through
 * tests/test_section_planning_frontend.py. The page script runs unmodified;
 * only HTTP answers and (unless a test asks for the real static/js/dialog.js)
 * the shared confirmation dialog are scripted. Any request a test does not
 * expect fails it. (tests/frontend/section-planning.test.cjs cannot be
 * required for its harness: it exports nothing and runs its own tests on load.)
 *
 * A programme's vote on how a 3-credit course meets each week is data-safe: a
 * choice is a draft (no request on change), never a what-if for Generate,
 * dropped (and said) when the programmes on screen change, previewed and
 * confirmed row by row, then committed with the preview's token. A row is a
 * course identity (CODE::NAME), never a bare code.
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
const UX = read('shared-ux.js');
const DIALOG = read('dialog.js');
const PAGE = read('page-section-planning.js');

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

const MEET = '/ops/section-planning/meeting-patterns/';
const SAVE = '/ops/section-planning/meeting-patterns/save/';
const GENERATE = '/ops/section-planning/generate/';

const STAT = 'STAT103::PRINCIPLES_OF_PROBABILITY_AND_STATISTICS';
const MATH = 'MATH243::243_LINEAR_ALGEBRA_I';
const COE = 'COE226::DIGITAL_LOGIC_DESIGN';
const DB = 'IS250::DATABASE_SYSTEMS';
const BARE = 'CS250';

/* The three votes as the server words them (programme_meeting_patterns). */
const CHOICES = [
  { value: '', label: 'Not set', label_ar: 'غير محدد' },
  { value: 'standard', label: 'Standard (2 × 75 min)', label_ar: 'قياسي (٢ × ٧٥ دقيقة)' },
  { value: '2x100', label: '2 × 100 min', label_ar: '٢ × ١٠٠ دقيقة' },
];
const WORDS = Object.fromEntries(CHOICES.map(c => [c.value, say(c.label, c.label_ar)]));

function course(key, name, programmes, extra = {}) {
  const votes = [...new Set(programmes.map(([, v]) => v).filter(Boolean))];
  return {
    course_key: key, course_code: key.split('::')[0], course_name: name, credit_hours: 3,
    programmes: programmes.map(([program, meeting_pattern]) => ({ program, meeting_pattern })),
    status: votes.length > 1 ? 'mixed' : votes.length ? 'agree' : 'none',
    meeting_pattern: votes.length === 1 ? votes[0] : '',
    co_owners: [], same_code_elsewhere: [], clear_only: false, reason: null, ...extra,
  };
}

/* As the server answers for the programmes on screen. */
function coursesFor(program, overrides = {}) {
  if (program === 'CS2') {
    return [course(STAT, 'PRINCIPLES OF PROBABILITY AND STATISTICS', [['CS2', 'standard']])];
  }
  const rows = [
    course(BARE, '', [['AI2', '2x100']], { clear_only: true, reason: 'not_named' }),
    course(COE, 'DIGITAL LOGIC DESIGN', [['AI2', 'standard'], ['DS2', '2x100']],
      { co_owners: [{ program: 'AI', meeting_pattern: 'standard' }] }),
    course(DB, 'DATABASE SYSTEMS', [['AI2', ''], ['DS2', '']]),
    course(MATH, 'LINEAR ALGEBRA I', [['AI2', '2x100'], ['DS2', '2x100']], {
      co_owners: [{ program: 'AI', meeting_pattern: '' }, { program: 'DS', meeting_pattern: '' }],
      same_code_elsewhere: [{ course_key: 'MATH243::LINEAR_ALGEBRA', course_name: 'LINEAR ALGEBRA', programmes: ['CYP2'] }],
    }),
    course(STAT, 'PRINCIPLES OF PROBABILITY AND STATISTICS', [['AI2', '2x100'], ['DS2', '']], {
      co_owners: [{ program: 'CS2', meeting_pattern: 'standard' }, { program: 'IS2', meeting_pattern: '' }],
    }),
  ];
  return rows.map(row => ({ ...row, ...(overrides[row.course_key] || {}) }));
}

function answer(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}

const listed = (program, courses = coursesFor(program)) => answer({ ok: true, programs: program.split(','), courses, choices: CHOICES });

function planned(change, program, old) {
  return {
    course_key: change.course_key, course_code: change.course_key.split('::')[0], course_name: 'A NAME',
    program, credit_hours: 3, old, new: change.meeting_pattern,
    scope: change.all_programmes ? 'all_programmes' : 'programmes',
  };
}

/* The save endpoint: scripted answers, recorded requests. */
function saveServer({ preview, commit } = {}) {
  return body => {
    if (body.dry_run === true) {
      return preview ? preview(body) : answer({
        ok: true, dry_run: true, programs: body.programs, unchanged: 0, skipped: [], courses: [],
        preview_token: 'token-1', changes: body.changes.map(change => planned(change, body.programs[0], '')),
      });
    }
    return commit ? commit(body) : answer({ ok: true, dry_run: false, programs: body.programs, changed: [], changed_count: body.changes.length, unchanged: 0, skipped: [] });
  };
}

async function page(t, { program = 'AI2,DS2', open = true, section = 'M', listing = prog => listed(prog), save = saveServer(), confirm = async () => true, generate = () => answer({ ok: false, code: 'generate_failed' }, 500), realDialogs = false } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(template, { url: 'http://planning.test/section-planning/', runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  t.after(() => { window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  const dialogs = [];
  window.notify = { success: () => {}, error: () => {} };
  if (!realDialogs) window.dlg = { confirm: async options => { dialogs.push(options); return confirm(options); } };
  const requests = [];
  window.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url, method, body });
    if (method === 'GET' && url.startsWith(MEET)) {
      return listing(new URL(url, 'http://planning.test').searchParams.get('program') || '');
    }
    if (method === 'POST' && url === SAVE) return save(body);
    if (method === 'POST' && url === GENERATE) return generate(body);
    const error = new Error(`Unexpected HTTP request: ${method} ${url}`);
    errors.push(error);
    throw error;
  };
  const context = dom.getInternalVMContext();
  vm.runInContext(SHARED, context, { filename: 'shared-utils.js' });
  vm.runInContext(UX, context, { filename: 'shared-ux.js' });
  if (realDialogs) {
    window.requestAnimationFrame = callback => window.setTimeout(callback, 0);
    vm.runInContext(DIALOG, context, { filename: 'dialog.js' });
  }
  vm.runInContext(PAGE, context, { filename: 'page-section-planning.js' });
  const $ = id => window.document.getElementById(id);
  const emit = (element, type) => element.dispatchEvent(new window.Event(type, { bubbles: true }));
  if (section) $(`spSection${section}`).click();
  $('spProgram').value = program;
  emit($('spProgram'), 'change');
  if (open) $('spToggleMeet').click();
  await settle();
  const row = key => window.document.querySelector(`#spMeetBody tr[data-key="${key}"]`);
  const select = key => row(key).querySelector('.meet-select');
  const all = key => row(key).querySelector('.meet-all');
  const choose = async (key, value) => {
    select(key).value = value;
    emit(select(key), 'change');
    await settle();
  };
  const tick = async key => { all(key).click(); await settle(); };
  const writes = () => requests.filter(request => request.method !== 'GET');
  const lists = () => requests.filter(request => request.method === 'GET' && request.url.startsWith(MEET));
  const text = id => $(id).textContent.replace(/\s+/g, ' ').trim();
  const saveOff = () => $('spMeetSaveDb').getAttribute('aria-disabled') === 'true';
  const setProgram = async value => { $('spProgram').value = value; emit($('spProgram'), 'change'); await settle(); };
  const leave = () => {
    const event = new window.Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  };
  const key = (element, value) => {
    const event = new window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true });
    element.dispatchEvent(event);
    if (!event.defaultPrevented && value === 'Enter' && element.tagName === 'BUTTON') element.click();
  };
  return { window, $, emit, row, select, all, choose, tick, requests, writes, lists, dialogs, text, saveOff, setProgram, leave, key };
}

/* A dialog body as the page hands it over, parsed. */
const shown = body => new JSDOM(`<div>${body}</div>`).window.document;
const flat = node => node.textContent.replace(/\s+/g, ' ').trim();

/* ── the disclosure ── */

test('the weekly meetings are a second disclosure, closed, that asks nothing until opened', async t => {
  const ui = await page(t, { open: false });
  const toggle = ui.$('spToggleMeet');
  assert.equal(toggle.tagName, 'BUTTON');
  assert.equal(toggle.type, 'button');
  assert.equal(toggle.getAttribute('aria-controls'), 'spMeetPanel');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.querySelectorAll(':scope > svg.sp-chev').length, 1);
  assert.ok(ui.$('spMeetPanel').classList.contains('d-none'));
  assert.deepEqual(ui.requests, [], 'a programme typed asks nothing while the panel is closed');

  toggle.click();
  await settle();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.ok(!ui.$('spMeetPanel').classList.contains('d-none'));
  assert.deepEqual(ui.lists().map(r => new URL(r.url, 'http://planning.test').searchParams.get('program')), ['AI2,DS2']);

  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  toggle.click();
  await settle();
  assert.equal(ui.lists().length, 1, 'the same programmes are not asked for again');
});

test('with no programme on screen the panel says to choose one and asks nothing', async t => {
  const ui = await page(t, { program: '' });
  const choose = say('Choose a programme first: weekly meetings are set per programme.', 'اختر برنامجاً أولاً: اللقاءات الأسبوعية تُحدَّد لكل برنامج.');
  assert.deepEqual(ui.requests, []);
  assert.equal(ui.text('spMeetBody'), choose);
  assert.equal(ui.saveOff(), true);
  assert.equal(ui.$('spMeetSaveDb').title, choose);
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.deepEqual(ui.requests, []);
  assert.equal(ui.text('spStatus'), choose);
});

test('Save is a primary button outside the scope form, described by the panel\'s status line', async t => {
  const ui = await page(t);
  const save = ui.$('spMeetSaveDb');
  assert.equal(save.className, 'btn btn-primary btn-sm');
  assert.equal(save.hasAttribute('style'), false);
  assert.equal(save.type, 'button');
  assert.equal(save.form, null, 'Enter in a scope field never saves');
  assert.equal(save.getAttribute('aria-describedby'), 'spMeetDrafts');
  assert.equal(ui.$('spMeetDrafts').getAttribute('role'), 'status');
  assert.equal(ui.text('spMeetDrafts'), say('No unsaved changes', 'لا توجد تعديلات غير محفوظة'));
  assert.equal(save.textContent.trim(), say('Save weekly meetings…', 'حفظ اللقاءات الأسبوعية…'));
});

test('the panel says what a value does, in the page\'s language, with the engine\'s minutes', async t => {
  const ui = await page(t, { open: false });
  assert.equal(ui.text('spToggleMeet'), say('🗓 Advanced: Weekly meetings 0', '🗓 إعدادات متقدمة: اللقاءات الأسبوعية 0'));
  const note = ui.text('spMeetNote');
  assert.match(note, AR ? /للطلاب والطالبات على السواء/ : /Applies to timetables generated from now on, Male and Female alike\./);
  assert.match(note, AR ? /تحتفظ السيناريوهات المحفوظة بلقاءاتها الأسبوعية/ : /Saved scenarios keep their own weekly meetings\./);
  assert.ok(note.includes(say('the course keeps Standard (2 × 75 min); a programme that has not set it does not count.', 'يبقى المقرر على «قياسي (٢ × ٧٥ دقيقة)»، ولا يُحتسب برنامج لم يحدّد قيمة.')), note);
  assert.match(note, AR ? /تُضبط المقررات الاختيارية وخاناتها لكل جدول حالياً/ : /Electives and elective slots are set per timetable for now \(\+ Course \/ section → Weekly meetings\)\./);
  assert.deepEqual([...ui.window.document.querySelectorAll('.sp-meet-table thead th')].map(th => th.textContent.trim()), say(
    ['Course', 'Programmes on screen', 'Weekly meetings', 'All programmes'],
    ['المقرر', 'البرامج المعروضة', 'اللقاءات الأسبوعية', 'كل البرامج'],
  ));
});

/* ── the rows ── */

test('one row per course identity: code, name, each programme\'s vote and the server\'s words', async t => {
  const ui = await page(t);
  assert.deepEqual([...ui.window.document.querySelectorAll('#spMeetBody tr[data-key]')].map(tr => tr.dataset.key), [BARE, COE, DB, MATH, STAT]);
  assert.equal(ui.text('spMeetCount'), say('5 courses', '5 مقررات'));

  const math = ui.row(MATH);
  assert.equal(math.querySelector('bdi.cr-id').textContent, 'MATH243');
  assert.equal(math.querySelector('.sp-cell-sub bdi').textContent, 'LINEAR ALGEBRA I');
  assert.deepEqual([...math.querySelectorAll('.sp-meet-prog')].map(flat), [`AI2 ${WORDS['2x100']}`, `DS2 ${WORDS['2x100']}`]);
  assert.equal(ui.select(MATH).value, '2x100');
  assert.deepEqual([...ui.select(MATH).options].map(o => [o.value, o.textContent]), CHOICES.map(c => [c.value, say(c.label, c.label_ar)]));
  assert.equal(ui.select(MATH).getAttribute('aria-label'), say('Weekly meetings for MATH243 LINEAR ALGEBRA I', 'اللقاءات الأسبوعية لـ MATH243 LINEAR ALGEBRA I'));
  assert.equal(ui.select(DB).value, '', 'nobody has set it');
  assert.ok(!math.classList.contains('sp-adv-draft'), 'a saved value is no draft');
  assert.equal(ui.$('spMeetBadge').classList.contains('d-none'), true);
});

test('a mixed row shows that its programmes differ, and All programmes waits for a value', async t => {
  const ui = await page(t);
  const coe = ui.select(COE);
  assert.equal(coe.value, 'keep');
  assert.equal(coe.selectedOptions[0].textContent, say('Programmes differ', 'البرامج مختلفة'));
  assert.equal(ui.all(COE).disabled, true, 'a mixed row has no single value to widen');
  assert.ok(!ui.row(COE).classList.contains('sp-adv-draft'));

  await ui.choose(COE, '2x100');
  assert.equal(ui.all(COE).disabled, false);
  assert.ok(ui.row(COE).classList.contains('sp-adv-draft'), 'any value is a change on a mixed row');
  await ui.tick(COE);
  assert.equal(ui.all(COE).checked, true);

  await ui.choose(COE, 'keep');
  assert.equal(ui.all(COE).checked, false, 'nothing to widen again');
  assert.equal(ui.all(COE).disabled, true);
  assert.ok(!ui.row(COE).classList.contains('sp-adv-draft'));
  assert.equal(ui.$('spMeetBadge').classList.contains('d-none'), true);

  await ui.choose(COE, '');
  assert.ok(ui.row(COE).classList.contains('sp-adv-draft'), 'Not set clears every vote on screen: a change');
  assert.deepEqual(ui.writes(), []);
});

test('a row that may no longer vote offers only clearing, and says why', async t => {
  const ui = await page(t);
  assert.deepEqual([...ui.select(BARE).options].map(o => o.value), ['', '2x100']);
  assert.equal(ui.select(BARE).value, '2x100', 'it shows what the row still holds');
  assert.equal(flat(ui.row(BARE).querySelector('.sp-meet-clear-only')), say(
    'No longer offered (no course name in the study plan); it can only be cleared.',
    'لم يعد يُختار له (بلا اسم مقرر في خطة الدراسة)، ويمكن مسحه فقط.',
  ));
  assert.equal(ui.select(BARE).getAttribute('aria-label'), say('Weekly meetings for CS250', 'اللقاءات الأسبوعية لـ CS250'));

  await ui.choose(BARE, '');
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes()[0].body.changes, [{ course_key: BARE, meeting_pattern: '', all_programmes: false }]);
});

/* ── drafts ── */

test('a choice is a draft: nothing is sent, the badge counts it, and Discard drops it without a request', async t => {
  const ui = await page(t);
  await ui.choose(STAT, 'standard');

  assert.deepEqual(ui.writes(), []);
  assert.ok(ui.row(STAT).classList.contains('sp-adv-draft'));
  assert.equal(ui.row(STAT).querySelector('.sp-adv-state').textContent, say('Modified', 'معدَّل'));
  assert.equal(ui.text('spMeetBadge'), '1');
  assert.equal(ui.text('spMeetDrafts'), say('1 unsaved change', '1 تعديل غير محفوظ'));
  assert.equal(ui.saveOff(), false);
  assert.equal(ui.$('spMeetReset').disabled, false);

  ui.$('spMeetReset').focus();
  ui.$('spMeetReset').click();
  await settle();
  assert.equal(ui.select(STAT).value, '2x100');
  assert.ok(!ui.row(STAT).classList.contains('sp-adv-draft'));
  assert.ok(ui.$('spMeetBadge').classList.contains('d-none'));
  assert.equal(ui.saveOff(), true);
  assert.equal(ui.$('spMeetReset').disabled, true);
  assert.equal(ui.window.document.activeElement, ui.select(BARE), 'the keyboard stays in the panel');
  assert.deepEqual(ui.writes(), []);
});

test('weekly-meeting drafts never reach Generate', async t => {
  const ui = await page(t);
  await ui.choose(STAT, 'standard');
  await ui.choose(COE, '2x100');
  ui.$('spGenerate').click();
  await settle();
  const sent = ui.requests.filter(r => r.url === GENERATE);
  assert.equal(sent.length, 1);
  assert.deepEqual(Object.keys(sent[0].body).sort(),
    ['max_external', 'max_local_4cr', 'max_local_other', 'program', 'section', 'semester', 'year']);
  assert.equal(ui.requests.filter(r => r.url === SAVE).length, 0, 'Generate never saves');
});

test('leaving the page warns while a weekly-meeting draft is unsaved, and not after Discard', async t => {
  const ui = await page(t);
  assert.equal(ui.leave(), false);
  await ui.choose(DB, '2x100');
  assert.equal(ui.leave(), true);
  ui.$('spMeetReset').click();
  assert.equal(ui.leave(), false);
});

test('Reset discards the weekly-meeting drafts, asks nothing, and the panel asks for a programme', async t => {
  const ui = await page(t);
  await ui.choose(STAT, 'standard');

  ui.$('spReset').click();
  await settle();

  assert.deepEqual(ui.writes(), [], 'Reset never writes');
  assert.equal(ui.lists().length, 1, 'with no programme nothing is asked');
  assert.equal(ui.text('spMeetBody'), say('Choose a programme first: weekly meetings are set per programme.', 'اختر برنامجاً أولاً: اللقاءات الأسبوعية تُحدَّد لكل برنامج.'));
  assert.ok(ui.$('spMeetBadge').classList.contains('d-none'));
  assert.equal(ui.text('spMeetDrafts'), say('No unsaved changes', 'لا توجد تعديلات غير محفوظة'), 'discarded, not "dropped"');
  assert.equal(ui.leave(), false);
});

/* ── the programmes on screen own the drafts ── */

test('a programme change drops the drafts, says so, and never brings them back', async t => {
  const ui = await page(t);
  await ui.choose(STAT, '');

  await ui.setProgram('CS2');
  assert.deepEqual(ui.lists().map(r => new URL(r.url, 'http://planning.test').searchParams.get('program')), ['AI2,DS2', 'CS2']);
  assert.equal(ui.select(STAT).value, 'standard', 'CS2 saved Standard: no draft of AI2\'s');
  assert.ok(!ui.row(STAT).classList.contains('sp-adv-draft'));
  assert.equal(ui.text('spMeetDrafts'), say(
    '1 unsaved change for AI2, DS2 was dropped: the programmes on screen changed',
    `أُسقط 1 تعديل غير محفوظ لـ ${isle('AI2, DS2')}: تغيّرت البرامج المعروضة`,
  ));
  assert.ok(ui.$('spMeetBadge').classList.contains('d-none'));
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes(), [], 'nothing is saved for CS2 that was chosen for AI2 and DS2');

  await ui.choose(STAT, '2x100');   // CS2's own row: acting again, the note has been read
  assert.equal(ui.text('spMeetDrafts'), say('1 unsaved change', '1 تعديل غير محفوظ'));
  ui.$('spMeetReset').click();

  await ui.setProgram('AI2,DS2');
  assert.equal(ui.select(STAT).value, '2x100', 'the dropped draft does not come back');
  assert.equal(ui.text('spMeetDrafts'), say('No unsaved changes', 'لا توجد تعديلات غير محفوظة'));
  assert.equal(ui.leave(), false);
});

test('the same programmes typed another way keep the drafts', async t => {
  const ui = await page(t);
  await ui.choose(STAT, 'standard');

  await ui.setProgram(' ds2, ai2,DS2 ');

  assert.equal(ui.select(STAT).value, 'standard');
  assert.ok(ui.row(STAT).classList.contains('sp-adv-draft'));
  assert.equal(ui.text('spMeetDrafts'), say('1 unsaved change', '1 تعديل غير محفوظ'));
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes()[0].body.programs, ['AI2', 'DS2']);
});

test('an older list answering late never replaces the newer programmes\' rows', async t => {
  let answerCs;
  const slow = new Promise(resolve => { answerCs = resolve; });
  const ui = await page(t, { listing: prog => (prog === 'CS2' ? slow : listed(prog)) });

  await ui.setProgram('CS2');
  await ui.setProgram('AI2,DS2');
  answerCs(listed('CS2'));
  await settle();

  assert.equal(ui.window.document.querySelectorAll('#spMeetBody tr[data-key]').length, 5, 'AI2 and DS2\'s rows stay');
  await ui.choose(DB, '2x100');
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes()[0].body.programs, ['AI2', 'DS2']);
});

test('while a list loads, or after it failed, Save sends nothing; the refusal is said in the page\'s words', async t => {
  let answerXyz;
  const pending = new Promise(resolve => { answerXyz = resolve; });
  const ui = await page(t, { listing: prog => (prog === 'XYZ' ? pending : listed(prog)) });
  await ui.choose(STAT, 'standard');

  await ui.setProgram('XYZ');
  assert.equal(ui.saveOff(), true);
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes(), [], 'loading: the rows on screen are still AI2 and DS2\'s');
  assert.equal(ui.text('spStatus'), say('The weekly meetings are loading…', 'جارٍ تحميل اللقاءات الأسبوعية…'));

  answerXyz(answer({ ok: false, code: 'unknown_program', program: 'XYZ', error: 'server words' }, 400));
  await settle();
  assert.equal(ui.text('spStatus'), say('Unknown programme: XYZ', 'برنامج غير معروف: XYZ'));
  assert.equal(ui.$('spStatus').getAttribute('role'), 'alert');
  assert.equal(ui.saveOff(), true);
  assert.equal(ui.$('spMeetSaveDb').title, say("The weekly meetings didn't load, so nothing can be saved. Change the programme or try again.", 'لم تُحمَّل اللقاءات الأسبوعية، فلا يمكن الحفظ. غيّر البرنامج أو حاول مرة أخرى.'));
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes(), []);
  assert.equal(ui.select(STAT).value, 'standard', 'the draft is still there');
});

test('a list that fails before any row is shown says so in the table too', async t => {
  const ui = await page(t, { program: 'XYZ', listing: () => answer({ ok: false, code: 'unknown_program', program: 'XYZ' }, 400) });
  assert.equal(ui.text('spMeetBody'), say('Unknown programme: XYZ', 'برنامج غير معروف: XYZ'));
});

/* ── the review and the save ── */

test('Save previews, confirms course · programme · now → new · scope, then commits with the preview token', async t => {
  const ui = await page(t, {
    save: saveServer({
      preview: body => {
        const change = key => body.changes.find(c => c.course_key === key);
        return answer({
          ok: true, dry_run: true, programs: body.programs, unchanged: 1, skipped: [], courses: [], preview_token: 'token-7',
          changes: [
            { ...planned(change(STAT), 'AI2', '2x100'), course_name: 'PRINCIPLES OF PROBABILITY AND STATISTICS' },
            { ...planned(change(COE), 'AI2', 'standard'), course_name: 'DIGITAL LOGIC DESIGN' },
            { ...planned(change(COE), 'AI', 'standard'), course_name: 'DIGITAL LOGIC DESIGN' },
          ],
        });
      },
      commit: body => answer({ ok: true, dry_run: false, programs: body.programs, changed: [], changed_count: 3, unchanged: 1, skipped: [] }),
    }),
  });
  await ui.choose(STAT, 'standard');
  await ui.choose(COE, '2x100');
  await ui.tick(COE);

  ui.$('spMeetSaveDb').click();
  await settle();

  const posts = ui.writes();
  assert.equal(posts.length, 2);
  assert.deepEqual(posts[0].body, {
    programs: ['AI2', 'DS2'],
    changes: [
      { course_key: COE, meeting_pattern: '2x100', all_programmes: true },
      { course_key: STAT, meeting_pattern: 'standard', all_programmes: false },
    ],
    dry_run: true,
  });
  assert.deepEqual(posts[1].body, { ...posts[0].body, dry_run: false, preview_token: 'token-7' });

  assert.equal(ui.dialogs.length, 1);
  const [dialog] = ui.dialogs;
  assert.equal(dialog.kind, 'warning');
  assert.equal(dialog.initialFocus, 'cancel', 'a review starts on "Keep editing"');
  assert.equal(dialog.title, say('Save 3 weekly-meetings choices?', 'حفظ 3 من اختيارات اللقاءات الأسبوعية؟'));
  assert.equal(dialog.confirmText, say('Save 3 changes', 'حفظ 3 تغيير'));
  const doc = shown(dialog.body);
  const rows = [...doc.querySelectorAll('.sp-meet-review tbody tr')].map(tr => [
    tr.querySelector('bdi.cr-id').textContent, ...[...tr.querySelectorAll('td')].slice(1).map(flat)]);
  assert.deepEqual(rows, [
    ['STAT103', 'AI2', WORDS['2x100'], WORDS.standard, say('Programmes on screen', 'البرامج المعروضة')],
    ['COE226', 'AI2', WORDS.standard, WORDS['2x100'], say('All programmes', 'كل البرامج')],
    ['COE226', 'AI', WORDS.standard, WORDS['2x100'], say('All programmes', 'كل البرامج')],
  ]);
  assert.match(flat(doc.body), AR ? /1 صف مطابق أصلاً/ : /1 row already matches and will not change\./);
  assert.match(flat(doc.body), AR ? /للطلاب والطالبات/ : /Male and Female alike; saved scenarios keep their own\./);

  assert.equal(ui.text('spStatus'), say('Saved 3 weekly-meetings choices; each change is in the audit log.', 'حُفظ 3 من اختيارات اللقاءات الأسبوعية وسُجّل في سجل التدقيق.'));
  assert.equal(ui.lists().length, 2, 'the panel is read again from the server');
  assert.ok(ui.$('spMeetBadge').classList.contains('d-none'), 'the saved drafts are gone');
  assert.equal(ui.$('spMeetSaveDb').textContent, say('Save weekly meetings…', 'حفظ اللقاءات الأسبوعية…'));
});

test('the review names who else teaches the course, its other names and the draft timetables it reaches', async t => {
  const hostile = '<img src=x onerror="window.__xss=1">';
  const ui = await page(t, {
    save: saveServer({
      preview: body => answer({
        ok: true, dry_run: true, programs: body.programs, unchanged: 0, preview_token: 't',
        changes: [{ ...planned(body.changes[0], 'AI2', '2x100'), course_name: 'LINEAR ALGEBRA I' }],
        skipped: [{ course_key: MATH, course_code: 'MATH243', course_name: 'LINEAR ALGEBRA I', program: 'DS2', meeting_pattern: '', reason: 'placeholder' }],
        courses: [{
          course_key: MATH, course_code: 'MATH243', course_name: 'LINEAR ALGEBRA I',
          co_owners: [{ program: 'AI', meeting_pattern: '' }, { program: 'DS', meeting_pattern: 'standard' }],
          same_code_elsewhere: [{ course_key: 'MATH243::LINEAR_ALGEBRA', course_name: 'LINEAR ALGEBRA', programmes: ['CYP2'] }],
          scenarios: [
            { id: 669, name: 'Department hand-made timetable 1448-2', budget_pattern: '', programme_pattern: '2x100', status: 'agree', differs: true },
            { id: 663, name: hostile, budget_pattern: '2x100', programme_pattern: '2x100', status: 'agree', differs: false },
            { id: 662, name: 'IS run', budget_pattern: '', programme_pattern: '', status: 'mixed', differs: false },
            { id: 661, name: 'Old run', budget_pattern: '2x100', programme_pattern: '', status: 'none', differs: false },
          ],
        }],
      }),
    }),
    confirm: async () => false,
  });
  await ui.choose(MATH, 'standard');
  ui.$('spMeetSaveDb').click();
  await settle();

  const doc = shown(ui.dialogs[0].body);
  assert.equal(flat(doc.querySelector('.sp-meet-owners')), say(
    'Also taught by (each keeps its own value): AI Not set · DS Standard (2 × 75 min)',
    'يدرّسه أيضاً (يحتفظ كل برنامج بقيمته): AI غير محدد · DS قياسي (٢ × ٧٥ دقيقة)',
  ));
  assert.equal(flat(doc.querySelector('.sp-meet-other')), say(
    'MATH243 LINEAR ALGEBRA (CYP2) is planned under another name — set its own row.',
    'MATH243 LINEAR ALGEBRA (CYP2) مخطط باسم آخر — حدّد صفه الخاص.',
  ));
  assert.equal(flat(doc.querySelector('.sp-meet-skipped')), say('Not set on: MATH243 (DS2): an elective slot', 'لا يُطبَّق على: MATH243 (DS2): خانة اختيارية'));
  const links = [...doc.querySelectorAll('.sp-meet-scenarios li')];
  assert.deepEqual(links.map(li => li.querySelector('a').getAttribute('href')),
    ['/timetable-workspace/?scenario=669', '/timetable-workspace/?scenario=663', '/timetable-workspace/?scenario=662', '/timetable-workspace/?scenario=661']);
  assert.deepEqual(links.map(flat), say([
    'Department hand-made timetable 1448-2 (#669) — now Standard (2 × 75 min); the programme default is 2 × 100 min: open it and press “Use programme defaults”',
    `${hostile} (#663) — already matches the programme default`,
    'IS run (#662) — its programmes differ: it stays as it is',
    'Old run (#661) — no programme default there: it stays as it is',
  ], [
    'Department hand-made timetable 1448-2 (#669) — الآن قياسي (٢ × ٧٥ دقيقة)، وافتراضي البرامج ٢ × ١٠٠ دقيقة: افتحه واضغط «استخدام افتراضي البرامج»',
    `${hostile} (#663) — مطابق لافتراضي البرامج`,
    'IS run (#662) — برامجه مختلفة: يبقى كما هو',
    'Old run (#661) — لا قيمة لبرامجه: يبقى كما هو',
  ]));
  assert.equal(doc.querySelectorAll('img').length, 0, 'a scenario name is text, never markup');
  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true], 'cancelled: only the preview was asked for');
});

test('All programmes sends the widened scope, and its name says who else it reaches', async t => {
  const ui = await page(t);
  assert.equal(ui.all(STAT).getAttribute('aria-label'), say(
    'Apply the STAT103 PRINCIPLES OF PROBABILITY AND STATISTICS choice to every programme that teaches it, also: CS2 Standard (2 × 75 min), IS2 Not set',
    'تطبيق اختيار STAT103 PRINCIPLES OF PROBABILITY AND STATISTICS على كل البرامج التي تدرّسه، ومنها: CS2 قياسي (٢ × ٧٥ دقيقة)، IS2 غير محدد',
  ));
  assert.equal(flat(ui.row(STAT).querySelector('.sp-meet-owners')), say(
    'Also taught by: CS2 Standard (2 × 75 min) · IS2 Not set',
    'يدرّسه أيضاً: CS2 قياسي (٢ × ٧٥ دقيقة) · IS2 غير محدد',
  ));
  assert.equal(ui.row(DB).querySelector('.meet-all'), null, 'no other programme teaches it: nothing to widen');

  await ui.tick(STAT);   // the value it shows, to every programme
  assert.ok(ui.row(STAT).classList.contains('sp-adv-draft'), 'a ticked box is a pending change');
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.deepEqual(ui.writes()[0].body.changes, [{ course_key: STAT, meeting_pattern: '2x100', all_programmes: true }]);
});

test('cancelling the review writes nothing and keeps the drafts', async t => {
  const ui = await page(t, { confirm: async () => false });
  await ui.choose(STAT, 'standard');

  ui.$('spMeetSaveDb').click();
  await settle();

  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true]);
  assert.equal(ui.select(STAT).value, 'standard');
  assert.ok(ui.row(STAT).classList.contains('sp-adv-draft'));
  assert.equal(ui.lists().length, 1, 'nothing to read again');
});

for (const [where, status, data, words, reload] of [
  ['commit', 409, { ok: false, code: 'row_changed', error: 'x' }, ['A course changed while saving, so nothing was saved. Press Save again.', 'تغيّر مقرر أثناء الحفظ فلم يُحفظ شيء. اضغط حفظ مرة أخرى.'], true],
  ['commit', 409, { ok: false, code: 'preview_stale', error: 'x' }, ['The saved weekly meetings changed since you reviewed them. Press Save again to review the new list.', 'تغيّرت اللقاءات الأسبوعية المحفوظة منذ المراجعة. اضغط حفظ مرة أخرى لمراجعة القائمة الجديدة.'], true],
  ['commit', 503, { ok: false, code: 'audit_unavailable', error: 'x' }, ["Couldn't record the change in the audit log, so nothing was saved. Try again.", 'تعذّر تسجيل التغيير في سجل التدقيق، لذلك لم يُحفظ شيء. حاول مرة أخرى.'], false],
  ['preview', 400, { ok: false, code: 'course_not_in_programmes', course_key: STAT, course_code: 'STAT103', error: 'x' }, ['STAT103 is not taught by the programmes on screen.', `${isle('STAT103')} لا يُدرَّس في البرامج المعروضة.`], false],
  ['preview', 400, { ok: false, code: 'invalid_pattern', course_key: STAT, error: 'x' }, ['The weekly meetings chosen for STAT103 are not a valid choice.', `اختيار اللقاءات الأسبوعية لـ ${isle('STAT103')} غير صالح.`], false],
  ['preview', 403, { error: 'Insufficient role' }, ['You are not allowed to save weekly meetings.', 'لا تملك صلاحية حفظ اللقاءات الأسبوعية.'], false],
  ['preview', 429, { error: 'Rate limit exceeded.' }, ['Too many requests. Wait a moment and try again.', 'طلبات كثيرة. انتظر قليلاً ثم حاول.'], false],
]) {
  test(`a ${where} refused with ${data.code || status} is said in the page's words and keeps the typed draft`, async t => {
    const refuse = () => answer(data, status);
    const ui = await page(t, { save: saveServer(where === 'commit' ? { commit: refuse } : { preview: refuse }) });
    await ui.choose(STAT, 'standard');

    ui.$('spMeetSaveDb').click();
    await settle();

    assert.equal(ui.text('spStatus'), say(...words));
    assert.equal(ui.$('spStatus').getAttribute('role'), 'alert');
    assert.equal(ui.lists().length, reload ? 2 : 1, reload ? 'the saved values are read again' : 'nothing moved: no reload');
    assert.equal(ui.select(STAT).value, 'standard', 'what the user chose is kept');
    assert.ok(ui.row(STAT).classList.contains('sp-adv-draft'));
  });
}

test('a scope-only draft survives a reload that still shows the value it was ticked on', async t => {
  const ui = await page(t, { save: saveServer({ commit: () => answer({ ok: false, code: 'preview_stale' }, 409) }) });
  await ui.tick(STAT);
  ui.$('spMeetSaveDb').click();
  await settle();

  assert.equal(ui.lists().length, 2);
  assert.equal(ui.all(STAT).checked, true, 'still 2 × 100: the choice stands');
  assert.ok(ui.row(STAT).classList.contains('sp-adv-draft'));
  assert.equal(ui.text('spMeetDrafts'), say('1 unsaved change', '1 تعديل غير محفوظ'));
});

test('a scope-only draft whose saved value moved on a reload is cleared, and the panel says so', async t => {
  let moved = false;
  const ui = await page(t, {
    listing: prog => listed(prog, coursesFor(prog, moved ? { [STAT]: { meeting_pattern: 'standard', programmes: [{ program: 'AI2', meeting_pattern: 'standard' }, { program: 'DS2', meeting_pattern: '' }] } } : {})),
    save: saveServer({ commit: () => { moved = true; return answer({ ok: false, code: 'row_changed' }, 409); } }),
  });
  await ui.tick(STAT);   // ticked on 2 × 100
  ui.$('spMeetSaveDb').click();
  await settle();

  assert.equal(ui.select(STAT).value, 'standard', 'the value someone else saved');
  assert.equal(ui.all(STAT).checked, false, 'its All programmes is cleared: Standard everywhere was never asked for');
  assert.ok(!ui.row(STAT).classList.contains('sp-adv-draft'));
  assert.equal(ui.text('spMeetDrafts'), say(
    'STAT103: the saved value changed, so All programmes was cleared; tick it again to apply the value shown now',
    `${isle('STAT103')}: تغيّرت القيمة المحفوظة فأُلغي تحديد «كل البرامج»؛ حدّده مرة أخرى لتطبيق القيمة المعروضة الآن`,
  ));
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.equal(ui.writes().length, 2, 'nothing more is sent: only the refused preview and commit');
});

test('a draft for a course no longer listed is dropped on a reload, and said', async t => {
  let gone = false;
  const ui = await page(t, {
    listing: prog => listed(prog, coursesFor(prog).filter(c => !gone || c.course_key !== DB)),
    save: saveServer({ commit: () => { gone = true; return answer({ ok: false, code: 'preview_stale' }, 409); } }),
  });
  await ui.choose(DB, '2x100');
  ui.$('spMeetSaveDb').click();
  await settle();

  assert.equal(ui.row(DB), null);
  assert.equal(ui.text('spMeetDrafts'), say(
    '1 unsaved change dropped: the course no longer takes that choice for these programmes',
    'أُسقط 1 تعديل غير محفوظ: لم يعد المقرر يقبل هذا الاختيار لهذه البرامج',
  ));
  assert.ok(ui.$('spMeetBadge').classList.contains('d-none'));
  assert.equal(ui.leave(), false, 'nothing left to lose');
});

test('a preview with nothing to change drops those drafts, says what it could not set, and reloads', async t => {
  const ui = await page(t, {
    save: saveServer({
      preview: body => answer({
        ok: true, dry_run: true, programs: body.programs, unchanged: 1, preview_token: 't', changes: [], courses: [],
        skipped: [{ course_key: DB, course_code: 'IS250', course_name: 'DATABASE SYSTEMS', program: 'AI', meeting_pattern: '', reason: 'not_three_credits' }],
      }),
    }),
  });
  await ui.choose(DB, '2x100');
  ui.$('spMeetSaveDb').click();
  await settle();

  assert.equal(ui.text('spStatus'), say(
    'Nothing to change: the saved values already match. 1 row cannot take that choice.',
    'لا شيء يتغيّر: القيم المحفوظة مطابقة. 1 صف لا يقبل هذا الاختيار.',
  ));
  assert.equal(ui.dialogs.length, 0);
  assert.equal(ui.lists().length, 2, 'the panel was read again');
  assert.ok(!ui.row(DB).classList.contains('sp-adv-draft'));
});

test('a second press of Save while the review loads sends nothing more', async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const ui = await page(t, { save: saveServer({ preview: async body => { await gate; return saveServer()(body); } }) });
  await ui.choose(STAT, 'standard');

  ui.$('spMeetSaveDb').click();
  ui.$('spMeetSaveDb').click();
  await settle();
  assert.equal(ui.saveOff(), true, 'busy');
  assert.equal(ui.$('spMeetSaveDb').textContent, say('Checking…', 'جارٍ التحقق…'), 'reviewing is not saving');
  release();
  await settle();

  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true, false]);
  assert.equal(ui.dialogs.length, 1);
});

/* ── the real confirmation dialog ── */

test('Enter on "Keep editing" cancels a save: only the preview is sent, focus returns to Save', async t => {
  const ui = await page(t, { realDialogs: true });
  await ui.choose(STAT, 'standard');
  const save = ui.$('spMeetSaveDb');
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
  assert.ok(body && body.querySelector('.sp-meet-review'), 'the rows are the dialog\'s description');

  ui.key(cancel, 'Enter');
  await pause(260);
  await settle();

  assert.equal(ui.window.document.querySelector('.dlg-backdrop'), null);
  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true], 'Enter on Keep editing never commits');
  assert.ok(ui.row(STAT).classList.contains('sp-adv-draft'), 'the draft is kept');
  assert.equal(ui.window.document.activeElement, save);
});

test('a confirmed save in the real dialog leaves the keyboard on Save', async t => {
  const ui = await page(t, { realDialogs: true });
  await ui.choose(STAT, 'standard');
  ui.$('spMeetSaveDb').focus();
  ui.$('spMeetSaveDb').click();
  await settle();
  await pause(80);
  ui.window.document.querySelector('.dlg-backdrop .btn-confirm').click();
  await pause(260);
  await settle();

  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true, false]);
  assert.equal(ui.window.document.activeElement, ui.$('spMeetSaveDb'));
  assert.equal(ui.text('spMeetDrafts'), say('No unsaved changes', 'لا توجد تعديلات غير محفوظة'));
});
