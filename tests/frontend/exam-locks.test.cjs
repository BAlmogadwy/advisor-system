/*
 * Locked days and periods on the exam page: the lock buttons on the board, the
 * rules they keep before any request, the Check-then-Save flow that saves them,
 * the Build that keeps them, and the report of what is wrong inside them.
 * Executed through tests/test_exam_frontend_interactions.py in English and in
 * Arabic (the wrapper renders the template in each, for an editor and for a
 * viewer who may not save); the unmodified production page script runs
 * against fake HTTP answers only.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');

assert.ok(process.env.EXAM_TEST_HTML, 'Run through pytest tests/test_exam_frontend_interactions.py');
const template = fs.readFileSync(process.env.EXAM_TEST_HTML, 'utf8');
const committeeTemplate = fs.readFileSync(process.env.EXAM_TEST_COMMITTEE_HTML, 'utf8');
const readOnlyTemplate = fs.readFileSync(process.env.EXAM_TEST_READ_ONLY_HTML, 'utf8');
const language = process.env.EXAM_TEST_LANGUAGE;
const AR = language === 'ar';
const source = fs.readFileSync(path.join(__dirname, '../../static/js/page-exam-timetable.js'), 'utf8');
const sharedSource = fs.readFileSync(path.join(__dirname, '../../static/js/shared-utils.js'), 'utf8');
const reviewSource = fs.readFileSync(path.join(__dirname, '../../static/js/exam-review.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const settled = async (times = 4) => { for (let i = 0; i < times; i++) await settle(); };
// Codes, days and periods sit in left-to-right isolates inside Arabic text.
const ISOLATES = new RegExp(`[${String.fromCharCode(0x2066)}-${String.fromCharCode(0x2069)}]`, 'g');
const plain = text => String(text ?? '').replace(ISOLATES, '');

const courses = [
  { course_code: 'AI212', source_course_code: 'AI212', course_name: 'Machine Learning', course_identity: 'ai212-ml', enrolled_count: 40, credit_hours: 3, programs: ['AI'] },
  { course_code: 'AI225', source_course_code: 'AI225', course_name: 'Machine Learning Foundations', course_identity: 'ai225-ml', enrolled_count: 6, credit_hours: 4, programs: ['AI2'] },
  { course_code: 'CS111 (1)', source_course_code: 'CS111', course_name: 'Fundamentals of Programming', course_identity: 'cs111-fundamentals', enrolled_count: 31, credit_hours: 3, programs: ['CS'] },
  { course_code: 'CS111 (2)', source_course_code: 'CS111', course_name: 'Programming I', course_identity: 'cs111-programming', enrolled_count: 14, credit_hours: 3, programs: ['CS2'] },
  { course_code: 'IS201', source_course_code: 'IS201', course_name: 'Databases', course_identity: 'is201-db', enrolled_count: 22, credit_hours: 3, programs: ['IS'] },
  { course_code: 'MATH101', source_course_code: 'MATH101', course_name: 'Calculus I', course_identity: 'math101-calc', enrolled_count: 55, credit_hours: 4, programs: ['AI', 'CS'] },
];
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'];
const [P1, P2, P3] = ['08:00-10:00', '10:30-12:30', '13:00-15:00'];
const PERIODS = [P1, P2, P3];
const slotsOf = (days = DAYS, periods = PERIODS) => {
  const slots = days.flatMap(day => periods.map(period => ({ day, period })));
  slots.forEach((slot, index) => { slot.index = index; });
  return slots;
};
const PLACES = { AI212: ['Sun', P1], AI225: ['Sun', P2], 'CS111 (1)': ['Tue', P2], 'CS111 (2)': ['Wed', P1], IS201: ['Mon', P1], MATH101: ['Thu', P3] };
// Sun is locked whole, and Tue 10:30-12:30 on its own: four cells, three exams.
const LOCKS = [{ day: 'Sun' }, { day: 'Tue', period: P2 }];
const SCOPE = { programs: ['AI', 'AI2', 'CS', 'CS2', 'IS'], sections: ['F', 'M'] };

const lockKeys = locks => (locks || []).map(lock => [lock.day, lock.period || '']).sort();
// The server's input fingerprint moves with the lock set, as D5 says.
const fingerprint = locks => `inputs:${JSON.stringify(lockKeys(locks))}`;

// What core.services.exam_locks.exam_locks_qa reports for a board.
function lockReport(locks, schedule, slots, issues) {
  if (!locks.length) return undefined;
  const days = new Set(locks.filter(lock => !lock.period).map(lock => lock.day));
  const cells = new Set(locks.filter(lock => lock.period).map(lock => `${lock.day}|${lock.period}`));
  const list = slots.filter(slot => days.has(slot.day) || cells.has(`${slot.day}|${slot.period}`)).map(slot => ({
    day: slot.day, period: slot.period,
    courses: schedule.filter(entry => entry.day === slot.day && entry.period === slot.period)
      .map(entry => ({ course_code: entry.course_code, course_identity: entry.course_identity })),
  }));
  return { cells: list, locked_courses: list.reduce((count, cell) => count + cell.courses.length, 0), issues, issue_count: issues.length };
}

function savedRun({ locks = LOCKS, places = PLACES, assignRooms = true, issues = [], qa = {}, runId = 17, label = 'Locks fixture', rename = {} } = {}) {
  const slots = slotsOf();
  const schedule = courses.map(course => {
    const [day, period] = places[course.course_code];
    const code = rename[course.course_code] || course.course_code;
    return { ...course, course_code: code, day, period, slot_index: slots.find(slot => slot.day === day && slot.period === period).index, rooms: [] };
  });
  const report = lockReport(locks, schedule, slots, issues);
  return {
    ok: true, run_id: runId, label, primary_status: 'clean', status_flags: [], input_fingerprint: fingerprint(locks),
    enrollment_source: 'scraper_timetable', assign_rooms: assignRooms,
    courses: schedule.map(entry => entry.course_code), courses_count: schedule.length, students_count: 168,
    schedule, slots, enrollment_scope: SCOPE,
    credit_map: Object.fromEntries(schedule.map(entry => [entry.course_code, entry.credit_hours])),
    pinned: [], linked_exams: [],
    ...(locks.length ? { exam_locks: locks } : {}),
    qa: { max_per_day: 2, ...(report ? { exam_locks: report } : {}), ...qa },
  };
}

// The server's Check: the board and the locks as sent (missing: the saved ones).
function evaluated(payload, original) {
  const slots = slotsOf(payload.days, payload.periods);
  const locks = 'exam_locks' in payload ? payload.exam_locks : (original.exam_locks || []);
  const schedule = payload.base_schedule.map(entry => ({
    ...original.schedule.find(item => item.course_identity === entry.course_identity), ...entry,
    slot_index: slots.find(slot => slot.day === entry.day && slot.period === entry.period)?.index ?? slots.length, rooms: [],
  }));
  const { exam_locks: _report, ...qa } = original.qa;
  const report = lockReport(locks, schedule, slots, original.qa.exam_locks?.issues || []);
  const answer = {
    ...original, run_id: undefined, editor_revision: payload.editor_revision, input_fingerprint: fingerprint(locks), slots, schedule,
    pinned: payload.pinned, courses_count: payload.base_schedule.length, linked_exams: payload.linked_exams, assign_rooms: payload.assign_rooms,
    qa: { ...qa, ...(report ? { exam_locks: report } : {}) },
  };
  if (locks.length) answer.exam_locks = locks;
  else delete answer.exam_locks;
  return answer;
}

const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

async function page(t, { run = null, onRequest = null, loadCourses = true, liveUpdate = false, html = template, confirm = true } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(html, { url: 'http://exam.test/exam-timetable/', runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  window.localStorage.setItem('exam-timetable-live-update', liveUpdate ? 'on' : 'off');
  t.after(() => { dom.window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  const requests = [];
  const dialogs = [];
  const state = { confirm, copy: null };
  window.notify = { error() {}, success() {} };
  window.dlg = {
    confirm: async options => { dialogs.push(options); return state.confirm; },
    prompt: async () => state.copy ?? false,
  };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => false });
  Object.defineProperty(window.document, 'visibilityState', { configurable: true, get: () => 'visible' });
  window.fetch = async (url, options = {}) => {
    requests.push({ url, ...options });
    if (onRequest) {
      const answer = await onRequest(url, options);
      if (answer !== undefined) return answer;
    }
    const body = options.body ? JSON.parse(options.body) : null;
    if (url === '/ops/exam-timetable/filters/') return reply({ ok: true, programs: SCOPE.programs, sections: SCOPE.sections });
    if (url.startsWith('/ops/exam-timetable/list/')) return reply({ ok: true, runs: run ? [{ id: run.run_id, label: run.label }] : [] });
    if (run && url === `/ops/exam-timetable/${run.run_id}/`) return reply(run);
    if (url === '/ops/exam-timetable/preview-courses/') return reply({ ok: true, courses });
    if (url === '/ops/exam-timetable/draft-impact/') return reply(evaluated(body, run || savedRun()));
    if (url === '/ops/exam-timetable/build/' && body?.mode === 'save_loaded_changes') {
      return reply({ ...evaluated(body, run || savedRun()), run_id: 18, label: body.label });
    }
    if (url === '/ops/exam-timetable/build/') return reply({ ok: false, error: 'Fixture ends at request validation' });
    if (String(url).split('?')[0] === '/ops/exam-timetable/jobs/active/') return reply({ ok: true, job: null });
    const error = new Error(`Unexpected HTTP request: ${url}`);
    errors.push(error);
    throw error;
  };
  vm.runInContext(sharedSource, dom.getInternalVMContext(), { filename: 'shared-utils.js' });
  vm.runInContext(reviewSource, dom.getInternalVMContext(), { filename: 'exam-review.js' });
  window.__examJobPoll = { first: 0, quick: 0, steady: 0, slow: 0, slowAfter: 0, announce: 0, reveal: 0, stall: 0, minShown: 0, backoff: [0, 0, 0, 0], timeout: 2000, checkRetry: 30 };
  vm.runInContext(`const LANGUAGE_CODE = ${JSON.stringify(language)};\n${source}`, dom.getInternalVMContext(), { filename: 'page-exam-timetable.js' });
  const $ = id => window.document.getElementById(id);
  const emit = (element, type) => element.dispatchEvent(new window.Event(type, { bubbles: true }));
  const input = (element, value) => { element.value = value; emit(element, 'input'); };
  const select = (id, value) => { $(id).value = value; emit($(id), 'change'); };
  await settle();
  input($('etLabel'), 'Locks regression');
  if (loadCourses) {
    $('loadCoursesBtn').click();
    await settled();
  }
  return { window, $, emit, input, select, requests, dialogs, state };
}

async function loaded(t, options = {}) {
  const run = options.run || savedRun();
  const ui = await page(t, { ...options, run, loadCourses: false });
  ui.$('historyList').querySelector('.et-run-info').click();
  await settled();
  return ui;
}

const bodies = (ui, url) => ui.requests.filter(request => request.url === url).map(request => JSON.parse(request.body));
const checks = ui => bodies(ui, '/ops/exam-timetable/draft-impact/');
const builds = ui => bodies(ui, '/ops/exam-timetable/build/');
const lockButton = (ui, day, period) => Array.from(ui.$('schedGrid').querySelectorAll('[data-lock-day]'))
  .find(button => button.dataset.lockDay === day && (button.dataset.lockPeriod || '') === (period || ''));
const cell = (ui, day, period) => Array.from(ui.$('schedGrid').querySelectorAll('td[data-day]'))
  .find(item => item.dataset.day === day && item.dataset.period === period);
const card = (ui, code) => Array.from(ui.$('schedGrid').querySelectorAll('.et-course')).find(item => item.dataset.course === code);
const placement = (ui, code) => {
  const where = card(ui, code).closest('td');
  return [where.dataset.day, where.dataset.period];
};
const drop = (ui, code, day, period) => {
  const event = new ui.window.Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: { getData: () => code } });
  cell(ui, day, period).dispatchEvent(event);
};
const refusal = ui => (ui.$('examLockRefusal').hidden ? '' : plain(ui.$('examLockRefusal').textContent));
const lockRows = ui => Array.from(ui.$('examLockRows').querySelectorAll('tr'));

const TEXT = {
  lockDay: day => (AR ? `قفل اليوم ${day}` : `Lock day ${day}`),
  lockPeriod: (day, period) => (AR ? `قفل الفترة ${day} ${period}` : `Lock period ${day} ${period}`),
  locked: AR ? 'مقفل' : 'Locked',
  lockedIn: (day, period) => (AR ? `${day} ${period} مقفلة — ألغِ قفلها أولاً.` : `${day} ${period} is locked — unlock it first.`),
  lockedOut: (code, day, period) => (AR ? `${code} في ${day} ${period} المقفلة — ألغِ قفلها أولاً.` : `${code} is in locked ${day} ${period} — unlock it first.`),
  unsaved: (day, period) => (AR
    ? `احفظ تغييراتك أولاً: في ${day} ${period} تغييرات لم تُحفظ بعد. احفظ الجدول ثم اقفلها.`
    : `Save your changes first: ${day} ${period} has changes that are not saved yet. Save the timetable, then lock it.`),
  changes: count => (AR
    ? `${{ 1: 'تغيير واحد في الأقفال لم يُحفظ', 2: 'تغييران في الأقفال لم يُحفظا', 3: '3 تغييرات في الأقفال لم تُحفظ' }[count]} — انقر «فحص التغييرات» ثم «حفظ التغييرات».`
    : `${count} lock change${count === 1 ? '' : 's'} not saved — Check changes, then Save Changes.`),
};

test('a loaded run shows its saved locks by day and period, with named toggles and closed cards', async t => {
  const ui = await loaded(t);
  // One native toggle per day and per period cell, named by day and period.
  const rows = Array.from(ui.$('schedGrid').querySelectorAll('tbody th[scope="row"]'));
  assert.equal(rows.length, 5);
  for (const row of rows) {
    const toggles = row.querySelectorAll('button.et-lock-toggle');
    assert.equal(toggles.length, 1);
    assert.equal(toggles[0].type, 'button');
  }
  assert.equal(ui.$('schedGrid').querySelectorAll('td[data-day] button.et-lock-toggle').length, 15);
  const sun = lockButton(ui, 'Sun');
  assert.equal(plain(sun.getAttribute('aria-label')), TEXT.lockDay('Sun'));
  assert.equal(sun.getAttribute('aria-pressed'), 'true');
  assert.equal(lockButton(ui, 'Mon').getAttribute('aria-pressed'), 'false');
  assert.equal(plain(lockButton(ui, 'Mon').getAttribute('aria-label')), TEXT.lockDay('Mon'));
  const tue = lockButton(ui, 'Tue', P2);
  assert.equal(plain(tue.getAttribute('aria-label')), TEXT.lockPeriod('Tue', P2));
  assert.equal(tue.getAttribute('aria-pressed'), 'true');
  assert.equal(lockButton(ui, 'Tue', P1).getAttribute('aria-pressed'), 'false');
  // A period of a locked day is locked with it, and its toggle says so.
  assert.equal(lockButton(ui, 'Sun', P3).getAttribute('aria-pressed'), 'true');
  assert.match(plain(lockButton(ui, 'Sun', P3).title), AR ? /مقفلة مع يومها/ : /locked with its day/);
  // Locked cells: tinted, marked in words, and their cards closed.
  for (const [day, period] of [['Sun', P1], ['Sun', P2], ['Sun', P3], ['Tue', P2]]) {
    const locked = cell(ui, day, period);
    assert.equal(locked.dataset.locked, 'true', `${day} ${period}`);
    assert.ok(locked.classList.contains('et-locked'));
    assert.equal(locked.querySelector('.et-lock-badge').textContent, TEXT.locked);
  }
  assert.equal(cell(ui, 'Mon', P1).dataset.locked, undefined);
  assert.equal(cell(ui, 'Mon', P1).querySelector('.et-lock-badge'), null);
  assert.ok(rows[0].classList.contains('et-locked'));
  for (const code of ['AI212', 'AI225', 'CS111 (1)']) {
    const closed = card(ui, code);
    assert.equal(closed.getAttribute('draggable'), 'false', code);
    assert.equal(closed.dataset.locked, 'true');
    // Its name says so, whatever the review view adds to it.
    assert.ok(closed.getAttribute('aria-label').startsWith(`${code} — ${courses.find(course => course.course_code === code).course_name} (${TEXT.locked})`), closed.getAttribute('aria-label'));
    for (const action of ['[data-exam-pin]', '[data-exam-move]']) {
      const button = closed.querySelector(action);
      assert.equal(button.getAttribute('aria-disabled'), 'true');
      assert.equal(button.disabled, false, 'Reachable, to say why it does nothing');
    }
  }
  const open = card(ui, 'CS111 (2)');
  assert.equal(open.getAttribute('draggable'), 'true');
  assert.equal(open.querySelector('[data-exam-move]').getAttribute('aria-disabled'), null);
  // The bar and the builder list what is locked; nothing is unsaved.
  assert.equal(ui.$('lockBar').classList.contains('d-none'), false);
  assert.equal(plain(ui.$('lockCount').textContent), AR ? '4 فترات مقفلة · 3 اختبارات' : '4 locked cells · 3 exams');
  assert.deepEqual(lockRows(ui).map(row => JSON.parse(row.dataset.lockItem)), [['Sun', ''], ['Tue', P2]]);
  assert.equal(ui.$('examLockLine').hidden, true);
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
});

test('a viewer who may not save sees the locks and no lock buttons', async t => {
  const ui = await loaded(t, { html: readOnlyTemplate });
  assert.equal(ui.$('main-content').dataset.canEditExamTimetable, 'false');
  assert.equal(ui.window.document.querySelectorAll('.et-lock-toggle, [data-lock-remove]').length, 0);
  const mark = ui.$('schedGrid').querySelector('tbody th .et-lock-mark');
  assert.equal(mark.getAttribute('role'), 'img');
  assert.equal(plain(mark.getAttribute('aria-label')), AR ? 'Sun مقفل' : 'Sun is locked');
  assert.equal(ui.$('schedGrid').querySelectorAll('tbody th .et-lock-mark').length, 1);
  assert.equal(ui.$('schedGrid').querySelectorAll('.et-lock-badge').length, 4);
  assert.equal(cell(ui, 'Mon', P1).querySelector('.et-cell-lockbar'), null, 'An unlocked cell has no lock row at all');
  assert.equal(card(ui, 'AI212').getAttribute('draggable'), 'false');
  assert.equal(lockRows(ui).length, 2);
});

test('the Exam Committee may save, so it may lock', async t => {
  const ui = await loaded(t, { html: committeeTemplate });
  assert.equal(ui.$('main-content').dataset.canEditExamTimetable, 'true');
  assert.equal(ui.$('schedGrid').querySelectorAll('.et-lock-toggle').length, 20);
  lockButton(ui, 'Mon').click();
  assert.equal(lockButton(ui, 'Mon').getAttribute('aria-pressed'), 'true');
});

test('drops and moves into or out of a locked cell are refused before any request', async t => {
  const ui = await loaded(t);
  const before = ui.requests.length;
  drop(ui, 'CS111 (2)', 'Sun', P3);
  assert.deepEqual(placement(ui, 'CS111 (2)'), ['Wed', P1]);
  assert.equal(refusal(ui), TEXT.lockedIn('Sun', P3));
  assert.equal(ui.$('examLockRefusal').getAttribute('role'), 'alert');
  drop(ui, 'AI212', 'Mon', P3);
  assert.deepEqual(placement(ui, 'AI212'), ['Sun', P1]);
  assert.equal(refusal(ui), TEXT.lockedOut('AI212', 'Sun', P1));
  assert.equal(ui.$('undoExamBtn').disabled, true);
  // A locked card never starts a drag, and a locked cell takes none.
  const start = new ui.window.Event('dragstart', { bubbles: true, cancelable: true });
  Object.defineProperty(start, 'dataTransfer', { value: { setData() {} } });
  card(ui, 'AI225').dispatchEvent(start);
  assert.equal(start.defaultPrevented, true);
  const over = new ui.window.Event('dragover', { bubbles: true, cancelable: true });
  Object.defineProperty(over, 'dataTransfer', { value: {} });
  cell(ui, 'Tue', P2).dispatchEvent(over);
  assert.equal(over.defaultPrevented, false);
  assert.ok(cell(ui, 'Tue', P2).classList.contains('et-drag-refused'));
  // The Move dialog says so inside itself, and stays open.
  card(ui, 'CS111 (2)').querySelector('[data-exam-move]').click();
  assert.equal(ui.$('examMoveDialog').hasAttribute('open'), true);
  ui.$('examMoveDay').value = 'Tue';
  ui.$('examMovePeriod').value = P2;
  ui.$('confirmExamMove').click();
  assert.equal(ui.$('examMoveDialog').hasAttribute('open'), true);
  assert.equal(ui.$('examMoveError').hidden, false);
  assert.equal(plain(ui.$('examMoveError').textContent), TEXT.lockedIn('Tue', P2));
  assert.deepEqual(placement(ui, 'CS111 (2)'), ['Wed', P1]);
  ui.select('examMoveDay', 'Mon');
  assert.equal(ui.$('examMoveError').hidden, true);
  ui.$('confirmExamMove').click();
  assert.deepEqual(placement(ui, 'CS111 (2)'), ['Mon', P2]);
  assert.equal(refusal(ui), '', 'An edit that went through clears the refusal');
  // Move on a locked card explains itself and opens nothing.
  card(ui, 'AI212').querySelector('[data-exam-move]').click();
  assert.equal(ui.$('examMoveDialog').hasAttribute('open'), false);
  assert.equal(refusal(ui), TEXT.lockedOut('AI212', 'Sun', P1));
  assert.equal(ui.requests.length, before, 'Nothing was sent');
});

test('pins into or out of a locked cell are refused, from the card and from the pin editor', async t => {
  const ui = await loaded(t);
  const before = ui.requests.length;
  card(ui, 'AI212').querySelector('[data-exam-pin]').click();
  assert.equal(card(ui, 'AI212').querySelector('[data-exam-pin]').getAttribute('aria-pressed'), 'false');
  assert.equal(refusal(ui), TEXT.lockedOut('AI212', 'Sun', P1));
  // Into a locked cell.
  ui.select('examPinCourse', 'CS111 (2)');
  ui.select('examPinDay', 'Sun');
  ui.select('examPinPeriod', P3);
  ui.$('applyExamPin').click();
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 0);
  assert.equal(refusal(ui), TEXT.lockedIn('Sun', P3));
  // Out of one.
  ui.select('examPinCourse', 'CS111 (1)');
  ui.select('examPinDay', 'Mon');
  ui.select('examPinPeriod', P1);
  ui.$('applyExamPin').click();
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 0);
  assert.equal(refusal(ui), TEXT.lockedOut('CS111 (1)', 'Tue', P2));
  // An open cell still takes a pin.
  ui.select('examPinCourse', 'CS111 (2)');
  ui.select('examPinDay', 'Thu');
  ui.select('examPinPeriod', P1);
  ui.$('applyExamPin').click();
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 1);
  assert.equal(ui.requests.length, before);
});

test('a lock is editor state: it marks the board changed, and Check then Save carry it', async t => {
  const ui = await loaded(t, { run: savedRun({ locks: [{ day: 'Tue', period: P2 }] }) });
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  const mon = lockButton(ui, 'Mon');
  mon.focus();
  mon.click();
  // Drawn again, the toggle keeps the keyboard.
  assert.equal(ui.window.document.activeElement, lockButton(ui, 'Mon'));
  assert.equal(lockButton(ui, 'Mon').getAttribute('aria-pressed'), 'true');
  assert.equal(cell(ui, 'Mon', P3).dataset.locked, 'true');
  assert.equal(ui.$('examLockLine').hidden, false);
  assert.equal(plain(ui.$('examLockLine').textContent), TEXT.changes(1));
  assert.match(plain(ui.$('examChangeCount').textContent), AR ? /1 تغيير قفل/ : /1 lock change$/);
  assert.equal(ui.$('saveLoadedBtn').disabled, false);
  assert.equal(ui.$('examChangesContent').querySelector('[data-change-group="locks"] [data-change-kind="lock"]').dataset.changeLock, JSON.stringify(['Mon', '']));
  assert.equal(checks(ui).length, 0, 'Live update is off: nothing checked yet');
  // Undo and Redo reverse a lock like any edit.
  ui.$('undoExamBtn').click();
  assert.equal(lockButton(ui, 'Mon').getAttribute('aria-pressed'), 'false');
  assert.equal(ui.$('examLockLine').hidden, true);
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  ui.$('redoExamBtn').click();
  assert.equal(lockButton(ui, 'Mon').getAttribute('aria-pressed'), 'true');
  // Save before a Check: the page checks, and asks for the review a lock needs.
  ui.$('saveLoadedBtn').click();
  await settled();
  assert.equal(builds(ui).length, 0);
  assert.equal(checks(ui).length, 1);
  assert.equal(plain(ui.$('examCheckStatus').textContent).startsWith(AR ? 'تغييرات الأقفال تحتاج فحصاً' : 'Lock changes need a Check'), true);
  assert.match(plain(ui.$('examEditorRequestError').textContent), AR ? /افحص تغييرات الأقفال قبل الحفظ/ : /Review the lock changes before saving/);
  // The metrics comparison pauses, and says why.
  assert.ok(Array.from(ui.window.document.querySelectorAll('.et-kpi-delta')).some(delta => plain(delta.textContent) === (AR ? 'المقارنة متوقفة: تغيّرت الأقفال' : 'Comparison paused: locks changed')));
  ui.$('checkDraftBtn').click();
  await settled();
  const [, check] = checks(ui);
  assert.deepEqual(check.exam_locks, [{ day: 'Mon' }, { day: 'Tue', period: P2 }]);
  assert.equal(check.assign_rooms, true);
  ui.$('saveLoadedBtn').click();
  await settled();
  const [save] = builds(ui);
  assert.equal(save.mode, 'save_loaded_changes');
  assert.deepEqual(save.exam_locks, [{ day: 'Mon' }, { day: 'Tue', period: P2 }]);
  assert.equal(save.expected_input_fingerprint, fingerprint([{ day: 'Mon' }, { day: 'Tue', period: P2 }]));
  // Saved: the new run's locks are the page's, and nothing is unsaved.
  assert.equal(ui.$('examLockLine').hidden, true);
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  assert.equal(lockButton(ui, 'Mon').getAttribute('aria-pressed'), 'true');
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/18/export.xlsx');
});

test('unlocking one period of a locked day keeps its other periods locked', async t => {
  const ui = await loaded(t);
  lockButton(ui, 'Sun', P2).click();
  assert.equal(lockButton(ui, 'Sun').getAttribute('aria-pressed'), 'false');
  assert.deepEqual([P1, P2, P3].map(period => lockButton(ui, 'Sun', period).getAttribute('aria-pressed')), ['true', 'false', 'true']);
  assert.equal(card(ui, 'AI225').getAttribute('draggable'), 'true');
  assert.equal(card(ui, 'AI212').getAttribute('draggable'), 'false');
  assert.equal(plain(ui.$('examLockLine').textContent), TEXT.changes(3));
  ui.$('checkDraftBtn').click();
  await settled();
  assert.deepEqual(checks(ui)[0].exam_locks, [{ day: 'Sun', period: P1 }, { day: 'Sun', period: P3 }, { day: 'Tue', period: P2 }]);
});

test('a run without locks sends none; unlocking every lock sends []', async t => {
  const plainRun = await loaded(t, { run: savedRun({ locks: [] }) });
  drop(plainRun, 'CS111 (2)', 'Mon', P3);
  plainRun.$('checkDraftBtn').click();
  await settled();
  assert.equal('exam_locks' in checks(plainRun)[0], false, 'Exactly the request made before locks existed');
  assert.equal(plainRun.$('lockBar').classList.contains('d-none'), true);
  assert.equal(plainRun.$('examLockEditor').classList.contains('d-none'), true);

  const ui = await loaded(t);
  lockButton(ui, 'Sun').click();
  lockButton(ui, 'Tue', P2).click();
  assert.equal(plain(ui.$('examLockLine').textContent), TEXT.changes(2));
  ui.$('checkDraftBtn').click();
  await settled();
  assert.deepEqual(checks(ui)[0].exam_locks, []);
});

test('Optimize and Fix keep the saved locks by sending none, and wait while a lock change is unsaved', async t => {
  const ui = await loaded(t);
  drop(ui, 'CS111 (2)', 'Mon', P3);
  ui.$('optimizeLoadedBtn').click();
  await settled();
  ui.$('minChangeBtn').click();
  await settled();
  const [optimise, fix] = builds(ui);
  assert.equal(optimise.mode, 'optimize_loaded');
  assert.equal(fix.mode, 'minimum_change_repair');
  assert.equal('exam_locks' in optimise, false);
  assert.equal('exam_locks' in fix, false);
  assert.equal(optimise.previous_run_id, 17);

  lockButton(ui, 'Thu').click();
  const before = ui.requests.length;
  ui.$('optimizeLoadedBtn').click();
  await settled();
  assert.equal(ui.requests.length, before);
  const optimiseName = plain(ui.$('optimizeLoadedBtn').textContent).trim();
  assert.equal(refusal(ui), AR
    ? `تغييرات الأقفال لم تُحفظ بعد. انقر «فحص التغييرات» ثم «حفظ التغييرات»، ثم استخدم «${optimiseName}».`
    : `Lock changes are not saved yet. Check changes, then Save Changes, then use ${optimiseName}.`);
  ui.$('minChangeBtn').click();
  await settled();
  assert.equal(ui.requests.length, before);
  assert.match(refusal(ui), AR ? /إصلاح بأقل تغيير/ : /Fix with fewest moves/);
});

test('a cell that differs from the saved run is saved before it is locked', async t => {
  const ui = await loaded(t);
  drop(ui, 'CS111 (2)', 'Mon', P2);
  const before = ui.requests.length;
  lockButton(ui, 'Mon', P2).click();
  assert.equal(lockButton(ui, 'Mon', P2).getAttribute('aria-pressed'), 'false');
  assert.equal(refusal(ui), TEXT.unsaved('Mon', P2));
  // The cell it left has changed too, and a day lock names the first cell.
  lockButton(ui, 'Wed', P1).click();
  assert.equal(refusal(ui), TEXT.unsaved('Wed', P1));
  lockButton(ui, 'Mon').click();
  assert.equal(lockButton(ui, 'Mon').getAttribute('aria-pressed'), 'false');
  assert.equal(refusal(ui), TEXT.unsaved('Mon', P2));
  // An unchanged cell locks.
  lockButton(ui, 'Thu', P3).click();
  assert.equal(lockButton(ui, 'Thu', P3).getAttribute('aria-pressed'), 'true');
  assert.equal(refusal(ui), '');
  assert.equal(ui.requests.length, before);
});

test('a run saved without rooms is given rooms on its next Check and Save before a lock', async t => {
  const ui = await loaded(t, { run: savedRun({ locks: [], assignRooms: false }) });
  lockButton(ui, 'Mon').click();
  assert.equal(lockButton(ui, 'Mon').getAttribute('aria-pressed'), 'false');
  assert.equal(refusal(ui), AR
    ? 'حُفظ هذا الجدول دون قاعات، والقفل يُبقي القاعات المحفوظة. ستُوزَّع القاعات عند «فحص التغييرات» ثم «حفظ التغييرات»؛ اقفل بعد ذلك.'
    : 'This timetable was saved without rooms, and a lock keeps the saved rooms. Rooms will be assigned when you Check changes, then Save Changes; lock it after that.');
  assert.equal(plain(ui.$('examLockLine').textContent), AR
    ? 'ستُوزَّع القاعات عند «فحص التغييرات» ثم «حفظ التغييرات»، ثم يمكنك قفل الأيام والفترات.'
    : 'Rooms will be assigned when you Check changes, then Save Changes. Lock days and periods after that.');
  assert.equal(ui.$('saveLoadedBtn').disabled, false, 'Rooms are a change to save');
  ui.$('checkDraftBtn').click();
  await settled();
  assert.equal(checks(ui)[0].assign_rooms, true);
  assert.equal('exam_locks' in checks(ui)[0], false);
  ui.$('saveLoadedBtn').click();
  await settled();
  const [save] = builds(ui);
  assert.equal(save.assign_rooms, true);
  // Saved with rooms: now it locks.
  lockButton(ui, 'Mon').click();
  assert.equal(lockButton(ui, 'Mon').getAttribute('aria-pressed'), 'true');
  // Undo takes a rooms request back like any edit.
  const fresh = await loaded(t, { run: savedRun({ locks: [], assignRooms: false }) });
  lockButton(fresh, 'Mon').click();
  fresh.$('undoExamBtn').click();
  assert.equal(fresh.$('examLockLine').hidden, true);
  assert.equal(fresh.$('saveLoadedBtn').disabled, true);
});

test('a Build from a saved run with locks says what it keeps, and inherits them', async t => {
  const ui = await loaded(t);
  ui.$('loadCoursesBtn').click();
  await settled();
  // The builder lists the locks it keeps, from the saved run.
  assert.equal(ui.$('examLockEditor').classList.contains('d-none'), false);
  assert.equal(lockRows(ui).length, 2);
  const keeps = AR
    ? 'يُبقي البناء 4 فترات مقفلة (تضم 3 اختبارات) ويعيد بناء كل ما عداها حولها.'
    : 'Build keeps 4 locked cells (3 exams) and rebuilds everything else around them.';
  assert.equal(plain(ui.$('examLockNotice').textContent), keeps);
  assert.match(plain(ui.$('examBuildSummary').textContent), AR ? /· 2 مقفل$/ : /· 2 locked$/);
  // Cancelled: nothing is built.
  ui.state.confirm = false;
  ui.$('buildBtn').click();
  await settled();
  assert.equal(ui.dialogs.length, 1);
  assert.equal(ui.dialogs[0].title, AR ? 'البناء حول الفترات المقفلة؟' : 'Build around the locked cells?');
  assert.ok(plain(ui.dialogs[0].body).includes(keeps), ui.dialogs[0].body);
  assert.equal(ui.dialogs[0].confirmLabel, AR ? 'بناء' : 'Build');
  assert.equal(builds(ui).length, 0);
  // Confirmed: the saved run is named, and its locks are inherited.
  ui.state.confirm = true;
  ui.$('buildBtn').click();
  await settled();
  const [build] = builds(ui);
  assert.equal(build.previous_run_id, 17);
  assert.equal('exam_locks' in build, false);
  assert.equal('mode' in build, false);
  // An explicit unlock is the only way to let Build change a locked cell.
  lockRows(ui)[1].querySelector('[data-lock-remove]').click();
  assert.equal(lockRows(ui).length, 1);
  ui.$('buildBtn').click();
  await settled();
  assert.ok(plain(ui.dialogs.at(-1).body).includes(AR
    ? 'يُبقي البناء 3 فترات مقفلة (تضم اختبارين) ويعيد بناء كل ما عداها حولها.'
    : 'Build keeps 3 locked cells (2 exams) and rebuilds everything else around them.'));
  const second = builds(ui)[1];
  assert.equal(second.previous_run_id, 17);
  assert.deepEqual(second.exam_locks, [{ day: 'Sun' }]);
  lockRows(ui)[0].querySelector('[data-lock-remove]').click();
  assert.equal(ui.$('examLockEditor').classList.contains('d-none'), true);
  const dialogs = ui.dialogs.length;
  ui.$('buildBtn').click();
  await settled();
  assert.equal(ui.dialogs.length, dialogs, 'No locks, no question');
  const third = builds(ui)[2];
  assert.equal('exam_locks' in third, false);
  assert.equal('previous_run_id' in third, false, 'Exactly the Build made before locks existed');
});

test('a Build with locks is refused for an unselected locked exam, a changed start day or another scope', async t => {
  const ui = await loaded(t);
  ui.$('loadCoursesBtn').click();
  await settled();
  const box = Array.from(ui.$('courseList').querySelectorAll('input')).find(item => item.value === 'AI212');
  box.checked = false;
  ui.emit(box, 'change');
  assert.equal(plain(lockRows(ui)[0].querySelector('.et-lock-problem').textContent), AR
    ? 'AI212 في Sun 08:00-10:00 المقفلة غير محدد لهذا الجدول. أعد تحديده، أو ألغِ القفل.'
    : 'AI212 in locked Sun 08:00-10:00 is not selected for this timetable. Select it again, or unlock it.');
  ui.$('buildBtn').click();
  await settled();
  assert.equal(ui.dialogs.length, 0);
  assert.equal(builds(ui).length, 0);
  box.checked = true;
  ui.emit(box, 'change');
  assert.equal(lockRows(ui)[0].querySelector('.et-lock-problem'), null);
  // A start day that relabels the days: "Sun" would now be the last day.
  ui.select('etStartDay', 'Mon');
  assert.equal(plain(lockRows(ui)[0].querySelector('.et-lock-problem').textContent), AR
    ? 'Sun لم يعد ضمن إعدادات الجدول. ألغِ القفل، أو أعد الإعدادات.'
    : 'Sun is not in the timetable settings any more. Unlock it, or change the settings back.');
  assert.equal(lockRows(ui)[1].querySelector('.et-lock-problem'), null, 'Tue keeps its day identity');
  ui.$('buildBtn').click();
  await settled();
  assert.equal(builds(ui).length, 0);
  ui.select('etStartDay', 'Sun');
  assert.equal(lockRows(ui)[0].querySelector('.et-lock-problem'), null);
  // Other programs: the locked exams were saved for these.
  const program = Array.from(ui.$('progList').querySelectorAll('input')).find(item => item.value === 'IS');
  program.checked = false;
  ui.emit(program, 'change');
  await settled();
  ui.$('loadCoursesBtn').click();
  await settled();
  assert.equal(lockRows(ui).length, 2, 'The locks stay until they are unlocked');
  ui.$('buildBtn').click();
  await settled();
  assert.equal(builds(ui).length, 0);
  assert.equal(plain(ui.$('etStatus').textContent), AR
    ? 'حُفظت الأيام المقفلة لبرامج أو شعب أخرى. اختر البرامج والشعب المحفوظة، أو ألغِ قفل الأيام.'
    : 'The locked days were saved for other programs or sections. Choose the saved programs and sections, or unlock the days.');
});

test('lock state stays with its day and period when a course code is renumbered', async t => {
  let saved = null;
  const ui = await loaded(t, { onRequest: async (url, options) => {
    const body = options.body ? JSON.parse(options.body) : null;
    if (url === '/ops/exam-timetable/build/' && body?.mode === 'save_loaded_changes') {
      // The population changed: "CS111 (1)" is "CS111" now, the same exam.
      saved = { ...savedRun({ locks: body.exam_locks, rename: { 'CS111 (1)': 'CS111', 'CS111 (2)': 'CS112' }, runId: 18 }), label: body.label };
      return reply(saved);
    }
    return undefined;
  } });
  lockButton(ui, 'Thu').click();
  ui.$('checkDraftBtn').click();
  await settled();
  ui.$('saveLoadedBtn').click();
  await settled();
  assert.ok(saved);
  assert.equal(card(ui, 'CS111 (1)'), undefined);
  const renamed = card(ui, 'CS111');
  assert.deepEqual(placement(ui, 'CS111'), ['Tue', P2]);
  assert.equal(renamed.dataset.locked, 'true');
  assert.equal(renamed.getAttribute('draggable'), 'false');
  assert.equal(card(ui, 'CS112').dataset.locked, undefined);
  assert.equal(lockButton(ui, 'Tue', P2).getAttribute('aria-pressed'), 'true');
  drop(ui, 'CS111', 'Mon', P1);
  assert.equal(refusal(ui), TEXT.lockedOut('CS111', 'Tue', P2));
  // What the next Check sends is days and periods, never a course.
  lockButton(ui, 'Mon', P3).click();
  ui.$('checkDraftBtn').click();
  await settled();
  assert.deepEqual(checks(ui).at(-1).exam_locks, [{ day: 'Sun' }, { day: 'Mon', period: P3 }, { day: 'Tue', period: P2 }, { day: 'Thu' }]);
  // Load Courses names it "CS111 (1)" again: the saved "CS111" is the same
  // exam, by identity, so the Build keeps it without a word about selection.
  ui.$('loadCoursesBtn').click();
  await settled();
  assert.deepEqual(lockRows(ui).map(row => row.querySelector('.et-lock-problem')), [null, null, null]);
  ui.$('buildBtn').click();
  await settled();
  assert.ok(plain(ui.dialogs.at(-1).body).includes(AR
    ? 'يُبقي البناء 7 فترات مقفلة (تضم 4 اختبارات) ويعيد بناء كل ما عداها حولها.'
    : 'Build keeps 7 locked cells (4 exams) and rebuilds everything else around them.'), ui.dialogs.at(-1).body);
  assert.equal(builds(ui).at(-1).previous_run_id, 18);
  // Pinning the renumbered exam out of its locked cell is refused by identity.
  ui.select('examPinCourse', 'CS111 (1)');
  ui.select('examPinDay', 'Mon');
  ui.select('examPinPeriod', P1);
  ui.$('applyExamPin').click();
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 0);
  // No board is open: the setup's own status line says why.
  assert.equal(plain(ui.$('etStatus').textContent), TEXT.lockedOut('CS111 (1)', 'Tue', P2));
});

const ALL_ISSUES = [
  { kind: 'double_booking', day: 'Sun', period: P1, room_code: 'B1-101', courses: ['AI212', 'AI225'] },
  { kind: 'room_over_capacity', day: 'Sun', period: P1, room_code: 'B1-101', seated: 40, exam_capacity: 35 },
  { kind: 'clash', day: 'Sun', period: P1, courses: ['AI212', 'AI225'], student_count: 2 },
  { kind: 'bucket_day', day: 'Sun', program: 'AI', programme_term: 3, courses: ['AI212', 'AI225'] },
  { kind: 'registrations_changed', day: 'Tue', period: P2, course_code: 'CS111 (1)', section: 'M3', section_key: 'term-section:1', gender: 'M', saved_count: 30, live_count: 32, change: 'grew' },
  { kind: 'unassigned', day: 'Tue', period: P2, course_code: 'CS111 (1)', section: 'F1', student_count: 1 },
  { kind: 'room_unavailable', day: 'Sun', period: P2, room_code: 'OLD-9', courses: ['AI225'] },
  { kind: 'room_cohort_changed', day: 'Sun', period: P2, room_code: 'B2-7', saved_gender: 'F', gender: 'M' },
];

test('the Locked cells report groups every issue kind, worded, with the unlock note', async t => {
  const run = savedRun({ issues: ALL_ISSUES, qa: {
    rooms: { unassigned_room_sections: [{ course_code: 'CS111 (1)', course_identity: 'cs111-fundamentals', day: 'Tue', period: P2, section: 'F1', gender: 'F', student_count: 1, locked: true },
      { course_code: 'IS201', course_identity: 'is201-db', day: 'Mon', period: P1, section: 'M1', gender: 'M', student_count: 3 }] },
    same_slot_conflicts: [{ student_id: 4401001, slot_index: 0, courses: ['AI212', 'AI225'] }, { student_id: 4401002, slot_index: 3, courses: ['IS201', 'MATH101'] }],
  } });
  const ui = await loaded(t, { run });
  assert.equal(plain(ui.$('lockBarIssues').textContent), AR ? '8 مشكلات في الفترات المقفلة' : '8 issues in locked cells');
  assert.equal(ui.$('lockBarReview').hidden, false);
  ui.$('lockBarReview').click();
  assert.equal(ui.$('kpiDrill').classList.contains('d-none'), false);
  assert.equal(ui.$('kpiDrillTitle').textContent, AR ? 'الفترات المقفلة' : 'Locked cells');
  const note = plain(ui.$('kpiDrillNote').textContent);
  assert.ok(note.startsWith(AR ? 'مقفل — لم يُنقل؛ ألغِ القفل للإصلاح.' : 'Locked — not moved; unlock to fix.'), note);
  assert.ok(note.includes(AR ? 'المقاعد المحفوظة تتجاوز سعة الاختبار الحالية' : "Saved seats exceed today's exam capacity"), note);
  const rows = Array.from(ui.$('kpiDrillBody').querySelectorAll('tr'));
  // Grouped by kind, in the report's order within a kind.
  assert.deepEqual(rows.map(row => row.dataset.lockIssue), ['clash', 'bucket_day', 'registrations_changed', 'unassigned', 'room_unavailable', 'room_over_capacity', 'room_cohort_changed', 'double_booking']);
  const labels = rows.map(row => plain(row.cells[0].textContent));
  assert.deepEqual(labels, AR
    ? ['تعارض طلاب', 'فصل دراسي واحد في يوم واحد', 'تغيّرت التسجيلات', 'طلاب دون قاعة', 'قاعة لم تعد متاحة', 'تجاوز سعة الاختبار', 'تغيّرت فئة طلاب القاعة', 'قاعة محجوزة مرتين']
    : ['Student clash', 'Same study term, same day', 'Registrations changed', 'Students without a room', 'Room no longer available', 'Over exam capacity', 'Room student group changed', 'Room double booked']);
  const details = rows.map(row => plain(row.cells[2].textContent));
  assert.deepEqual(details, AR ? [
    'طالبان يؤديان AI212، AI225 في الوقت نفسه.',
    'AI، الفصل الدراسي 3: AI212، AI225 في اليوم نفسه.',
    'CS111 (1) M3 (طلاب (M)): تغيّر عدد الطلاب من 30 إلى 32؛ لا مقاعد محفوظة للطلاب الجدد.',
    'CS111 (1) F1: طالب واحد دون قاعة.',
    'OLD-9 لم تعد ضمن قاعات الاختبار الحالية (AI225).',
    'B1-101: المقاعد المحفوظة 40، وسعة الاختبار الحالية 35. المقاعد المحفوظة تتجاوز سعة الاختبار الحالية.',
    'حُفظت B2-7 لـطالبات (F)، وهي الآن لـطلاب (M).',
    'B1-101 محجوزة لـAI212، AI225 في الوقت نفسه.',
  ] : [
    '2 students sit AI212, AI225 at the same time.',
    'AI, study term 3: AI212, AI225 on the same day.',
    'CS111 (1) M3 (Male students (M)): 30 → 32 students; the new students have no saved seat.',
    'CS111 (1) F1: 1 student without a room.',
    "OLD-9 is not among today's exam rooms (AI225).",
    "B1-101: 40 saved seats, today's exam capacity 35. The saved seats exceed today's exam capacity.",
    'B2-7 was saved for Female students (F); it now serves Male students (M).',
    'B1-101 holds AI212, AI225 at the same time.',
  ]);
  assert.deepEqual(rows.map(row => plain(row.cells[1].textContent)), [`Sun · ${P1}`, 'Sun', `Tue · ${P2}`, `Tue · ${P2}`, `Sun · ${P2}`, `Sun · ${P1}`, `Sun · ${P2}`, `Sun · ${P1}`]);
  assert.doesNotMatch(ui.$('kpiDrillBody').textContent, /registrations_changed|room_over_capacity|_/);
  // Rows of the existing lists that sit inside locked cells get a lock mark.
  ui.$('kpiDrillClose').click();
  ui.window.document.querySelector('[data-drill="room-unassigned"]').click();
  const unassigned = Array.from(ui.$('kpiDrillBody').querySelectorAll('tr'));
  assert.equal(unassigned.length, 2);
  assert.equal(plain(unassigned[0].querySelector('.et-row-lock').getAttribute('aria-label')), AR ? 'مقفل — لم يُنقل؛ ألغِ القفل للإصلاح' : 'Locked — not moved; unlock to fix');
  assert.equal(unassigned[1].querySelector('.et-row-lock'), null);
  ui.window.document.querySelector('[data-drill="conflicts"]').click();
  const conflicts = Array.from(ui.$('kpiDrillBody').querySelectorAll('tr'));
  assert.ok(conflicts[0].querySelector('.et-row-lock'), 'Both courses are locked');
  assert.equal(conflicts[1].querySelector('.et-row-lock'), null);
});

test('before a Check, a new lock has no report yet, and the bar says so', async t => {
  const ui = await loaded(t, { run: savedRun({ locks: [] }) });
  lockButton(ui, 'Thu', P3).click();
  assert.equal(ui.$('lockBar').classList.contains('d-none'), false);
  assert.equal(plain(ui.$('lockCount').textContent), AR ? 'فترة مقفلة واحدة · اختبار واحد' : '1 locked cell · 1 exam');
  assert.equal(plain(ui.$('lockBarIssues').textContent), AR ? 'افحص التغييرات لمعرفة حال الفترات المقفلة.' : 'Check changes to report on the locked cells.');
  assert.equal(ui.$('lockBarReview').hidden, true);
  ui.$('checkDraftBtn').click();
  await settled();
  assert.equal(plain(ui.$('lockBarIssues').textContent), AR ? 'لا مشكلات في الفترات المقفلة' : 'No issues in locked cells');
  assert.equal(ui.$('lockBarReview').hidden, false);
});

test('Fix with fewest moves says what the locks kept and which breaks it could not touch', async t => {
  const ui = await loaded(t, { onRequest: async (url, options) => {
    const body = options.body ? JSON.parse(options.body) : null;
    if (url === '/ops/exam-timetable/build/' && body?.mode === 'minimum_change_repair') {
      return reply({ ok: true, saved: false, minimum_change: { status: 'OPTIMAL', moves: [], unseated: [], violations_before: 1, violations_after: 1, protected_count: 0, locked_count: 3, locked_violations: 1 } });
    }
    return undefined;
  } });
  ui.$('minChangeBtn').click();
  await settled();
  const report = plain(ui.$('examRepairReport').textContent);
  assert.ok(report.includes(AR
    ? 'بقيت مخالفة واحدة بين اختبارات مقفلة فقط: مقفلة — لم تُنقل؛ ألغِ القفل لإصلاحها.'
    : '1 rule break is among locked exams only: locked — not moved; unlock to fix.'), report);
  assert.ok(report.includes(AR ? 'بقيت الاختبارات في الفترات المقفلة في مواضعها (3 اختبارات).' : '3 exams in locked cells were not moved.'), report);
  assert.doesNotMatch(report, AR ? /مثبّتة أو نقلتَها/ : /between pinned exams/, 'The only break left is a locked one');
});

test('Copy keeps the locks, and loading the run again shows them', async t => {
  // A copy of a run saved with other locks: the page shows the copy's own.
  const copy = { ...savedRun({ locks: [{ day: 'Wed' }] }), run_id: 48, label: 'Locked copy' };
  const ui = await loaded(t, { onRequest: async url => (url.endsWith('/17/copy/') ? { ...reply(copy), status: 201 } : undefined) });
  lockButton(ui, 'Mon').click();
  ui.$('undoExamBtn').click();
  ui.state.copy = copy.label;
  ui.$('historyList').querySelector('.et-copy-btn').click();
  await settled();
  assert.equal(ui.$('etLabel').value, copy.label);
  assert.equal(lockButton(ui, 'Wed').getAttribute('aria-pressed'), 'true');
  assert.equal(lockButton(ui, 'Sun').getAttribute('aria-pressed'), 'false');
  assert.equal(lockButton(ui, 'Tue', P2).getAttribute('aria-pressed'), 'false');
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  assert.equal(ui.$('examLockLine').hidden, true);
  ui.$('historyList').querySelector('.et-run-info').click();
  await settled();
  assert.equal(lockButton(ui, 'Sun').getAttribute('aria-pressed'), 'true');
  assert.equal(lockButton(ui, 'Wed').getAttribute('aria-pressed'), 'false');
  assert.equal(ui.$('examLockLine').hidden, true);
});

test('linking into a locked cell, or unlinking a room shared inside one, is refused', async t => {
  const run = savedRun();
  // AI212 and AI225 share a saved room in locked Sun 08:00, as linked courses do.
  run.schedule.find(entry => entry.course_code === 'AI225').day = 'Sun';
  run.schedule.find(entry => entry.course_code === 'AI225').period = P1;
  run.schedule.find(entry => entry.course_code === 'AI225').slot_index = 0;
  run.schedule.find(entry => entry.course_code === 'AI212').rooms = [{ room_code: 'B1-1', room_shared_with: ['AI212', 'AI225'] }];
  run.schedule.find(entry => entry.course_code === 'AI225').rooms = [{ room_code: 'B1-1', room_shared_with: ['AI212', 'AI225'] }];
  run.linked_exams = [{ members: [{ course_identity: 'ai212-ml', course_code: 'AI212' }, { course_identity: 'ai225-ml', course_code: 'AI225' }] }];
  const ui = await loaded(t, { run });
  const pick = code => {
    ui.$('examLinkCourseSearch').focus();
    ui.input(ui.$('examLinkCourseSearch'), code);
    Array.from(ui.$('examLinkCourseResults').querySelectorAll('[role="option"]')).find(option => option.dataset.value === code).click();
  };
  pick('CS111 (1)');
  pick('IS201');
  ui.$('applyExamLink').click();
  assert.equal(plain(ui.$('examLinkNotice').textContent), AR
    ? 'CS111 (1) في Tue 10:30-12:30 المقفلة، فلا يُنقل أي مقرر إليها أو منها لربطه. ألغِ القفل أولاً.'
    : 'CS111 (1) is in locked Tue 10:30-12:30: no course can move into or out of it to link. Unlock it first.');
  assert.equal(ui.$('examLinkRows').querySelectorAll('tr').length, 1);
  assert.equal(ui.$('examLinkDialog').hasAttribute('open'), false);
  ui.$('examLinkRows').querySelector('[data-link-remove]').click();
  assert.equal(ui.$('examLinkRows').querySelectorAll('tr').length, 1, 'Still linked');
  assert.equal(plain(ui.$('examLinkNotice').textContent), AR
    ? 'تتشارك هذه المقررات المرتبطة قاعة في فترة مقفلة. ألغِ القفل قبل إلغاء ربطها.'
    : 'These linked courses share a room in a locked period. Unlock it before unlinking them.');
});

test('a deleted saved run is no longer where a Build keeps its locks from', async t => {
  const ui = await loaded(t, { onRequest: async url => (url === '/ops/exam-timetable/17/delete/' ? reply({ ok: true }) : undefined) });
  ui.$('loadCoursesBtn').click();
  await settled();
  assert.equal(lockRows(ui).length, 2);
  ui.$('historyList').querySelector('.et-del-btn').click();
  await settled();
  assert.equal(ui.requests.filter(request => request.url === '/ops/exam-timetable/17/delete/').length, 1);
  assert.equal(ui.$('examLockEditor').classList.contains('d-none'), true);
  const dialogs = ui.dialogs.length;
  ui.$('buildBtn').click();
  await settled();
  assert.equal(ui.dialogs.length, dialogs, 'Nothing is kept, so nothing is asked');
  const [build] = builds(ui);
  assert.equal('previous_run_id' in build, false);
  assert.equal('exam_locks' in build, false);
});
