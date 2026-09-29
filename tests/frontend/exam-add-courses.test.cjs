/*
 * Adding courses to the saved timetable on the board: the Add courses list,
 * what it sends, the report it shows, and the setup list of a saved timetable.
 * The owner's words: every course of the timetable's programmes and sections
 * is listed without Load Courses, the courses already placed are marked and
 * never selected again, and only the new ones are ticked.
 * Executed through tests/test_exam_frontend_interactions.py in English and in
 * Arabic; the unmodified production page script runs against fake answers.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');

assert.ok(process.env.EXAM_TEST_HTML, 'Run through pytest tests/test_exam_frontend_interactions.py');
const template = fs.readFileSync(process.env.EXAM_TEST_HTML, 'utf8');
const readOnlyTemplate = fs.readFileSync(process.env.EXAM_TEST_READ_ONLY_HTML, 'utf8');
const language = process.env.EXAM_TEST_LANGUAGE;
const AR = language === 'ar';
const source = fs.readFileSync(path.join(__dirname, '../../static/js/page-exam-timetable.js'), 'utf8');
const sharedSource = fs.readFileSync(path.join(__dirname, '../../static/js/shared-utils.js'), 'utf8');
const reviewSource = fs.readFileSync(path.join(__dirname, '../../static/js/exam-review.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const settled = async (times = 6) => { for (let i = 0; i < times; i++) await settle(); };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const ISOLATES = new RegExp(`[${String.fromCharCode(0x2066)}-${String.fromCharCode(0x2069)}]`, 'g');
const plain = text => String(text ?? '').replace(ISOLATES, '').replace(/\s+/g, ' ').trim();

const SCOPE = { programs: ['AI', 'CS'], sections: ['F', 'M'] };
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'];
const PERIODS = ['08:00-10:00', '10:30-12:30', '13:00-15:00'];
const slotsOf = () => {
  const slots = DAYS.flatMap(day => PERIODS.map(period => ({ day, period })));
  slots.forEach((slot, index) => { slot.index = index; });
  return slots;
};
// Every course of the scope; the saved run holds the first three.
const courses = [
  { course_code: 'AI212', source_course_code: 'AI212', course_name: 'Machine Learning', course_identity: 'ai212-ml', enrolled_count: 40, credit_hours: 3, programs: ['AI'] },
  { course_code: 'CS111 (1)', source_course_code: 'CS111', course_name: 'Fundamentals of Programming', course_identity: 'cs111-fundamentals', enrolled_count: 31, credit_hours: 3, programs: ['CS'] },
  { course_code: 'IS201', source_course_code: 'IS201', course_name: 'Databases', course_identity: 'is201-db', enrolled_count: 22, credit_hours: 3, programs: ['CS'] },
  { course_code: 'CS111 (2)', source_course_code: 'CS111', course_name: 'Programming I', course_identity: 'cs111-programming', enrolled_count: 14, credit_hours: 3, programs: ['AI'] },
  { course_code: 'MATH101', source_course_code: 'MATH101', course_name: 'Calculus I', course_identity: 'math101-calc', enrolled_count: 55, credit_hours: 4, programs: ['AI', 'CS'], is_online: true },
  { course_code: 'ST210', source_course_code: 'ST210', course_name: 'Statistics', course_identity: 'st210-stats', enrolled_count: 18, credit_hours: 3, programs: ['CS'] },
];
const IN_RUN = courses.slice(0, 3);
const NEW = courses.slice(3);
const PLACES = { AI212: ['Sun', PERIODS[0]], 'CS111 (1)': ['Mon', PERIODS[1]], IS201: ['Tue', PERIODS[2]] };

function savedRun({ runId = 17, label = 'Part one', entries = IN_RUN, places = PLACES, extra = {} } = {}) {
  const slots = slotsOf();
  const schedule = entries.map(course => {
    const [day, period] = places[course.course_code];
    return { ...course, day, period, slot_index: slots.find(slot => slot.day === day && slot.period === period).index, rooms: [] };
  });
  return {
    ok: true, run_id: runId, label, primary_status: 'clean', status_flags: [], input_fingerprint: `inputs-${runId}`,
    enrollment_source: 'scraper_timetable', assign_rooms: true,
    courses: schedule.map(entry => entry.course_code), courses_count: schedule.length, students_count: 93,
    schedule, slots, enrollment_scope: SCOPE,
    credit_map: Object.fromEntries(schedule.map(entry => [entry.course_code, entry.credit_hours])),
    pinned: [], linked_exams: [], qa: { max_per_day: 2 }, ...extra,
  };
}

function scopeAnswer(run, { missing = [], termChanged = false } = {}) {
  const inRun = new Map(run.schedule.map(entry => [entry.course_identity, entry]));
  return {
    ok: true, run_id: run.run_id, scope: SCOPE, term_changed: termChanged, missing,
    courses: courses.map(course => {
      const entry = inRun.get(course.course_identity);
      return entry
        ? { ...course, in_timetable: true, timetable_course_code: entry.course_code, placement: { day: entry.day, period: entry.period } }
        : { ...course, in_timetable: false, add_code: course.course_code };
    }),
  };
}

// What the server saves: the source's board plus the added courses.
function addedRun(body, { report = {} } = {}) {
  const run = savedRun();
  const slots = slotsOf();
  const added = body.added_courses.map((item, index) => {
    const course = courses.find(c => c.course_identity === item.course_identity);
    const slot = slots[6 + index];
    return { ...course, day: slot.day, period: slot.period, slot_index: slot.index, rooms: [] };
  });
  const schedule = [...run.schedule, ...added];
  return {
    ...run, run_id: 18, label: body.label, editor_revision: body.editor_revision, schedule,
    courses: schedule.map(entry => entry.course_code), courses_count: schedule.length,
    rebuild_mode: 'added_courses_from_loaded', input_fingerprint: 'inputs-18',
    add_courses: {
      source_run_id: 17,
      added: added.map(entry => ({ course_code: entry.course_code, course_identity: entry.course_identity, course_name: entry.course_name, placed: { day: entry.day, period: entry.period } })),
      requested_count: added.length, placed_count: added.length, moves: [], not_placed: [], moved_weight: 0,
      protected_count: 0, locked_count: 0, untouched_violations: 0, violations_after: 0, widened: false,
      proven_minimal: true, status: 'OPTIMAL', rooms_changed: [], sections_changed: 0, unassigned_added: 0, ...report,
    },
  };
}

const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data, headers: { get: () => null } });

async function page(t, { run = savedRun(), html = template, onRequest = null, scope = null, loadCourses = false, url = 'http://exam.test/exam-timetable/', open = true } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(html, { url, runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  window.localStorage.setItem('exam-timetable-live-update', 'off');
  t.after(() => { dom.window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  const requests = [];
  const notes = [];
  window.notify = { error: (...args) => notes.push(args), success() {} };
  window.dlg = { confirm: async () => true, prompt: async () => false };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => false });
  Object.defineProperty(window.document, 'visibilityState', { configurable: true, get: () => 'visible' });
  window.fetch = async (url, options = {}) => {
    requests.push({ url, ...options });
    if (onRequest) {
      const answer = await onRequest(url, options);
      if (answer !== undefined) return answer;
    }
    if (url === '/ops/exam-timetable/filters/') return reply({ ok: true, programs: SCOPE.programs, sections: SCOPE.sections });
    if (url.startsWith('/ops/exam-timetable/list/')) return reply({ ok: true, runs: [{ id: run.run_id, label: run.label }] });
    if (url === `/ops/exam-timetable/${run.run_id}/`) return reply(run);
    if (url === `/ops/exam-timetable/${run.run_id}/scope-courses/`) return reply(scope || scopeAnswer(run));
    if (url === '/ops/exam-timetable/preview-courses/') return reply({ ok: true, courses });
    if (url === '/ops/exam-timetable/build/') {
      const body = JSON.parse(options.body);
      if (body.mode === 'add_courses') return reply({ ok: true, ...addedRun(body) });
      return reply({ ok: false, error: 'Fixture ends at request validation' }, 400);
    }
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
  await settle();
  if (loadCourses) {
    $('loadCoursesBtn').click();
    await settled();
  } else if (open) {
    $('historyList').querySelector('.et-run-info').click();
    await settled();
  }
  return { window, $, emit, input, requests, notes };
}

const urls = ui => ui.requests.map(request => request.url);
const scopeRequests = ui => ui.requests.filter(request => request.url.endsWith('/scope-courses/'));
const builds = ui => ui.requests.filter(request => request.url === '/ops/exam-timetable/build/').map(request => JSON.parse(request.body));
const addRows = ui => Array.from(ui.$('examAddCoursesList').querySelectorAll('.et-add-row'));
const shownCodes = ui => addRows(ui).filter(row => !row.hidden).map(row => row.querySelector('.et-course-code').textContent);
const box = (ui, code) => addRows(ui).find(row => row.querySelector('.et-course-code').textContent === code)?.querySelector('input');

async function openList(ui) {
  ui.$('addCoursesBtn').click();
  await settled();
}

const TEXT = AR ? {
  button: 'إضافة مقررات…',
  inTimetable: 'في الجدول',
  count: (x, y, n) => `عرض ${x} من ${y}`,
  noneTicked: 'حدّد مقرراً واحداً على الأقل لإضافته.',
  submitTwo: 'إضافة مقررين',
  submitNone: 'إضافة المقررات',
  unsaved: /احفظ تغييراتك، أو افتح الجدول المحفوظ مجدداً/,
  headline: /أُضيف مقرران دون نقل أي اختبار موجود/,
  outBadge: 'ليس في هذا الجدول',
  note: /أنت تعدّل جدولاً محفوظاً/,
  running: 'جارٍ إضافة المقررات إلى الجدول',
  outside: /لا يوجد طلاب لهذا المقرر في برامج هذا الجدول وشعبه/,
  missing: /لم تعد لـ/,
  term: /بُني هذا الجدول لفصل دراسي آخر/,
  moved: /نُقل اختبار واحد موجود لإفساح المجال، وهو أقل عدد ممكن/,
  found: /وهو أقل عدد وُجد/,
  bucket: /لبرنامج CS في المستوى 3 اختبار في كل يوم متاح/,
  fixed: /لا يمكن نقلها \(مثبتة أو مقفلة أو نقلتها أنت: AI212\)/,
  room: /لإفساح المجال لـ/,
  unavailable: /لم يعد لهذا المقرر تسجيلات في الجداول الدراسية المستوردة/,
  notAll: 'حُفظت النتيجة جدولاً جديداً، وبقي بعض المقررات المضافة دون موعد. راجع التقرير.',
  added: 'أُضيفت المقررات وحُفظت النتيجة جدولاً جديداً.',
  cannotSend: /^تعذّر إرسال إعدادات الجدول\. سبب مختبر\. أغلق القائمة وصحّحها في «إعدادات الجدول والمقررات والمواعيد المثبتة»\.$/,
} : {
  button: 'Add courses…',
  inTimetable: 'In timetable',
  count: (x, y, n) => `Showing ${x} of ${y} courses · ${n} chosen to add`,
  noneTicked: 'Tick at least one course to add.',
  submitTwo: 'Add 2 courses',
  submitNone: 'Add courses',
  unsaved: /Save your changes, or open the saved timetable again from Saved timetables/,
  headline: /Added 2 courses\. No existing exam moved\./,
  outBadge: 'Not in this timetable',
  note: /You are editing a saved timetable/,
  running: 'Adding courses to the timetable',
  outside: /This course has no students in this timetable's programmes and sections/,
  missing: /has no current registrations\. Remove it/,
  term: /This timetable was built for another academic term/,
  moved: /1 existing exam moved to make room: the fewest possible\./,
  found: /the fewest found/,
  bucket: /CS term 3 already has an exam on every open day/,
  fixed: /cannot move \(pinned, locked or moved by you: AI212\)/,
  room: /to make room for/,
  unavailable: /This course has no registrations in the imported student timetables any more/,
  notAll: 'Saved as a new timetable. Not every course could be placed: see the report.',
  added: 'Courses added. Saved as a new timetable.',
  cannotSend: /^The timetable settings could not be sent\. سبب مختبر\. Close this list and correct them under Timetable setup, courses and fixed times\.$/,
};

const ADD_JOB = '6f1c2a4e-0000-4000-8000-00000000add2';
const ADD_PLAN = ['read_board', 'place_exams', 'fewest_moves', 'check_rules', 'assign_rooms', 'save'];
// An Add that ended while the page was closed, as /jobs/active/ reports it.
const endedAdd = (extra = {}) => ({
  id: ADD_JOB, kind: 'add_courses', status: 'succeeded', mine: true, can_cancel: false, owner: 'Registrar',
  has_run: true, result_run_id: 18, error_code: '', cancelled_by: '', waiting_for: null, refused: false, stopping: false,
  submitted_at: '2026-09-29T10:00:00+00:00', started_at: '2026-09-29T10:00:01+00:00', finished_at: '2026-09-29T10:00:40+00:00', now: '2026-09-29T10:05:00+00:00',
  stages: ADD_PLAN.map(key => ({ key, state: 'done' })), current: { key: 'save', done: null, total: null }, ...extra,
});
const isActiveJobs = url => String(url).split('?')[0] === '/ops/exam-timetable/jobs/active/';
async function until(ui, predicate, message) {
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await pause(2);
  }
  assert.fail(message);
}

// ── (a) the list opens by itself with every course of the scope ─────────────

test('Add courses lists every course of the scope at once: no Load Courses, searchable by code, name and programme', async t => {
  const ui = await page(t);
  assert.equal(ui.$('addCoursesBtn').classList.contains('d-none'), false);
  assert.equal(plain(ui.$('addCoursesBtn').textContent), TEXT.button);
  const before = ui.requests.length;
  await openList(ui);
  const asked = urls(ui).slice(before);
  assert.deepEqual(asked, ['/ops/exam-timetable/17/scope-courses/']);
  assert.ok(!urls(ui).includes('/ops/exam-timetable/preview-courses/'), 'Load Courses is never needed');
  assert.equal(ui.$('examAddCoursesDialog').open, true);
  assert.equal(ui.window.document.activeElement, ui.$('examAddCoursesSearch'));
  assert.deepEqual(shownCodes(ui).sort(), courses.map(course => course.course_code).sort());
  if (!AR) assert.equal(ui.$('examAddCoursesCount').textContent, TEXT.count(6, 6, 0));
  else assert.match(plain(ui.$('examAddCoursesCount').textContent), /^عرض 6 من 6 مقررات/);
  ui.input(ui.$('examAddCoursesSearch'), 'math101');
  assert.deepEqual(shownCodes(ui), ['MATH101']);
  ui.input(ui.$('examAddCoursesSearch'), 'statistics');
  assert.deepEqual(shownCodes(ui), ['ST210']);
  ui.input(ui.$('examAddCoursesSearch'), 'ai');
  assert.deepEqual(shownCodes(ui).sort(), ['AI212', 'CS111 (2)', 'MATH101']);
  if (!AR) assert.equal(ui.$('examAddCoursesCount').textContent, TEXT.count(3, 6, 0));
  ui.input(ui.$('examAddCoursesSearch'), 'no such course');
  assert.deepEqual(shownCodes(ui), []);
  // It is asked for again each time the list opens.
  ui.$('cancelAddCourses').click();
  await openList(ui);
  assert.equal(scopeRequests(ui).length, 2);
});

// ── (b), (c) courses in the timetable cannot be chosen; none is pre-ticked ──

test('courses already in the timetable are marked, have no checkbox, and are never sent; nothing is ticked on open', async t => {
  const ui = await page(t);
  await openList(ui);
  for (const course of IN_RUN) {
    const row = addRows(ui).find(item => item.querySelector('.et-course-code').textContent === course.course_code);
    assert.equal(row.querySelector('input'), null, course.course_code);
    assert.ok(plain(row.querySelector('.et-add-badge').textContent).startsWith(TEXT.inTimetable), course.course_code);
    assert.ok(plain(row.textContent).includes(PLACES[course.course_code][1]));
  }
  const boxes = Array.from(ui.$('examAddCoursesList').querySelectorAll('input[type=checkbox]'));
  assert.equal(boxes.length, NEW.length);
  assert.equal(boxes.filter(item => item.checked).length, 0, 'Nothing is ticked when the list opens');
  assert.equal(plain(ui.$('confirmAddCourses').textContent), TEXT.submitNone);
  // Submitting nothing says so, sends nothing, and keeps the list open.
  ui.$('confirmAddCourses').click();
  await settled();
  assert.equal(builds(ui).length, 0);
  assert.equal(ui.$('examAddCoursesError').hidden, false);
  assert.equal(ui.$('examAddCoursesError').getAttribute('role'), 'alert');
  assert.equal(plain(ui.$('examAddCoursesError').textContent), TEXT.noneTicked);
  assert.equal(ui.window.document.activeElement, ui.$('examAddCoursesSearch'));
  assert.equal(ui.$('examAddCoursesDialog').open, true);
  // Tick two: the button says how many.
  box(ui, 'MATH101').click();
  box(ui, 'ST210').click();
  assert.equal(plain(ui.$('confirmAddCourses').textContent), TEXT.submitTwo);
  ui.$('examAddCoursesShow').value = 'chosen';
  ui.emit(ui.$('examAddCoursesShow'), 'change');
  assert.deepEqual(shownCodes(ui).sort(), ['MATH101', 'ST210']);
  ui.$('confirmAddCourses').click();
  await settled();
  const [sent] = builds(ui);
  assert.equal(sent.mode, 'add_courses');
  assert.equal(sent.previous_run_id, 17);
  assert.deepEqual(sent.added_courses.map(item => item.course_identity).sort(), ['math101-calc', 'st210-stats']);
  const inRun = new Set(IN_RUN.map(course => course.course_identity));
  assert.ok(!sent.added_courses.some(item => inRun.has(item.course_identity)), 'A course already placed is never sent');
  // The board sent is the saved one, untouched; locks are the server's to keep.
  assert.deepEqual(sent.base_schedule.map(entry => [entry.course_identity, entry.day, entry.period]).sort(),
    IN_RUN.map(course => [course.course_identity, ...PLACES[course.course_code]]).sort());
  assert.deepEqual(sent.selected_courses.sort(), IN_RUN.map(course => course.course_code).sort());
  assert.deepEqual(sent.pinned, []);
  assert.deepEqual(sent.linked_exams, []);
  assert.equal('exam_locks' in sent, false);
  assert.notEqual(sent.label, 'Part one', 'The new timetable gets a name of its own');
  assert.ok(sent.label.startsWith('Part one'));
});

test('the new run is shown with its report, and the history lists it', async t => {
  const ui = await page(t);
  await openList(ui);
  const historyBefore = ui.requests.filter(request => request.url.startsWith('/ops/exam-timetable/list/')).length;
  box(ui, 'MATH101').click();
  box(ui, 'ST210').click();
  ui.$('confirmAddCourses').click();
  await settled();
  assert.equal(ui.$('examAddCoursesDialog').open, false);
  const report = ui.$('examRepairReport');
  assert.match(plain(report.textContent), TEXT.headline);
  assert.ok(report.querySelector('[data-find-exam="math101-calc"]'), 'Each placed course can be found on the board');
  assert.equal(report.getAttribute('role'), 'status');
  assert.ok(ui.requests.filter(request => request.url.startsWith('/ops/exam-timetable/list/')).length > historyBefore);
  const cards = Array.from(ui.$('schedGrid').querySelectorAll('.et-course'), card => card.dataset.course);
  assert.ok(cards.includes('MATH101') && cards.includes('ST210'));
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/18/export.xlsx');
});

// ── (d) the setup list of a saved timetable ──────────────────────────────────

test('opening setup on a saved timetable lists the other courses of its scope, found by search, without discarding the board', async t => {
  const ui = await page(t);
  const signature = ui.window.eval('editorSignature()');
  assert.equal(scopeRequests(ui).length, 0, 'Nothing is asked for when the run opens');
  assert.equal(ui.$('courseOutsideGroup').hidden, true);
  ui.$('examSetupSummary').click();
  await settled();
  assert.equal(ui.$('examSetupDetails').open, true);
  assert.equal(scopeRequests(ui).length, 1);
  assert.equal(ui.$('courseOutsideGroup').hidden, false);
  const outside = Array.from(ui.$('courseOutsideList').querySelectorAll('.et-course-option'));
  assert.deepEqual(outside.map(row => row.querySelector('.et-course-code').textContent).sort(), NEW.map(course => course.course_code).sort());
  assert.equal(ui.$('courseOutsideList').querySelectorAll('input').length, 0, 'Other courses carry no checkbox');
  assert.ok(outside.every(row => plain(row.querySelector('.et-add-badge').textContent) === TEXT.outBadge));
  // The timetable's own courses say so.
  const own = Array.from(ui.$('courseList').querySelectorAll('.et-course-option'));
  assert.equal(own.length, IN_RUN.length);
  assert.ok(own.every(row => plain(row.querySelector('.et-add-badge').textContent) === TEXT.inTimetable));
  // Search reaches both lists.
  ui.input(ui.$('courseSearch'), 'statistics');
  assert.deepEqual(outside.filter(row => !row.hidden).map(row => row.querySelector('.et-course-code').textContent), ['ST210']);
  assert.equal(own.filter(row => !row.hidden).length, 0);
  assert.equal(ui.$('courseNoMatches').hidden, true);
  if (!AR) assert.equal(ui.$('courseSearchCount').textContent, 'Showing 1 of 6 courses · 3 in this timetable');
  // The board is still the saved one.
  assert.equal(ui.$('etResults').classList.contains('d-none'), false);
  assert.equal(ui.window.eval('editorSignature()'), signature);
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  // Selection presets never reach the other courses.
  ui.$('toggleAllCourses').click();
  assert.equal(ui.$('courseOutsideList').querySelectorAll('input').length, 0);
  assert.equal(ui.window.eval("getCheckedCourseEntries().length"), 0);
  ui.$('toggleAllCourses').click();
  assert.equal(ui.window.eval("getCheckedCourseEntries().length"), IN_RUN.length);
  assert.equal(ui.window.eval('editorSignature()'), signature);
  // Opening it again asks nothing more for the same run.
  ui.$('examSetupSummary').click();
  await settled();
  ui.$('examSetupSummary').click();
  await settled();
  assert.equal(scopeRequests(ui).length, 1);
});

test('the setup list offers Add courses with its search, and Load Courses still starts a new timetable', async t => {
  const ui = await page(t);
  assert.equal(ui.$('loadedRunBuildNote').hidden, false, 'A saved timetable says how to start a new one');
  assert.match(plain(ui.$('loadedRunBuildNote').textContent), TEXT.note);
  assert.equal(ui.$('loadCoursesBtn').getAttribute('aria-describedby'), 'loadedRunBuildNote');
  ui.$('examSetupSummary').click();
  await settled();
  ui.input(ui.$('courseSearch'), 'MATH');
  ui.$('courseOutsideAdd').click();
  await settled();
  assert.equal(ui.$('examAddCoursesDialog').open, true);
  assert.equal(ui.$('examAddCoursesSearch').value, 'MATH');
  assert.deepEqual(shownCodes(ui), ['MATH101']);
  assert.equal(ui.$('examAddCoursesList').querySelectorAll('input:checked').length, 0);
  ui.emit(ui.$('examAddCoursesDialog'), 'cancel');
  assert.equal(ui.$('examAddCoursesDialog').open, false);
  assert.equal(ui.window.document.activeElement, ui.$('courseOutsideAdd'), 'Escape returns to the button that opened it');
  ui.$('loadCoursesBtn').click();
  await settled();
  assert.equal(ui.$('courseOutsideGroup').hidden, true);
  assert.equal(ui.$('loadedRunBuildNote').hidden, true);
  assert.equal(ui.$('loadCoursesBtn').hasAttribute('aria-describedby'), false);
  assert.equal(ui.$('courseList').querySelectorAll('input:checked').length, courses.length);
  assert.equal(ui.$('courseList').querySelectorAll('.et-add-badge').length, 0, 'A new timetable marks nothing as in it');
});

test('after a Build, the setup list is the timetable itself: the courses left out are found under the other courses', async t => {
  let built = null;
  const ui = await page(t, {
    loadCourses: true,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/build/') return undefined;
      const body = JSON.parse(options.body);
      if (body.mode) return undefined;
      built = savedRun({ runId: 17, label: body.label });
      return reply(built);
    },
  });
  // Build three of six.
  ui.input(ui.$('etLabel'), 'Part one');
  for (const course of NEW) {
    const check = Array.from(ui.$('courseList').querySelectorAll('input')).find(item => item.value === course.course_code);
    check.click();
  }
  ui.$('buildBtn').click();
  await settled();
  assert.ok(built);
  assert.deepEqual(Array.from(ui.$('courseList').querySelectorAll('input'), item => item.value).sort(), IN_RUN.map(c => c.course_code).sort());
  assert.equal(ui.$('courseList').querySelectorAll('input:not(:checked)').length, 0);
  ui.$('examSetupSummary').click();
  await settled();
  assert.deepEqual(Array.from(ui.$('courseOutsideList').querySelectorAll('.et-course-code'), code => code.textContent).sort(), NEW.map(c => c.course_code).sort());
});

test('the setup list is filled when its search gets focus, with no click on the section', async t => {
  const ui = await page(t);
  // Opened by the page to point at a field - not by the registrar's click.
  ui.$('examSetupDetails').open = true;
  await settled();
  assert.equal(scopeRequests(ui).length, 0, 'Opening it for them asks for nothing');
  ui.$('courseSearch').focus();
  await settled();
  assert.equal(scopeRequests(ui).length, 1, 'Searching asks for the other courses');
  const outside = Array.from(ui.$('courseOutsideList').querySelectorAll('.et-course-option'));
  assert.deepEqual(outside.map(row => row.querySelector('.et-course-code').textContent).sort(), NEW.map(course => course.course_code).sort());
  ui.input(ui.$('courseSearch'), 'calculus');
  assert.deepEqual(outside.filter(row => !row.hidden).map(row => row.querySelector('.et-course-code').textContent), ['MATH101']);
  assert.equal(ui.$('courseNoMatches').hidden, true);
  ui.$('courseSearch').blur();
  ui.$('courseSearch').focus();
  await settled();
  assert.equal(scopeRequests(ui).length, 1, 'Asked once per run');
});

// ── a board with unsaved changes ─────────────────────────────────────────────

test('with unsaved edits Add courses says to save first, offers Save, and asks nothing; a new name alone is no edit', async t => {
  const ui = await page(t);
  ui.input(ui.$('etLabel'), 'Only the name changed');
  await openList(ui);
  assert.equal(ui.$('examAddCoursesDialog').open, true, 'A name is not a board edit');
  ui.$('cancelAddCourses').click();
  assert.equal(ui.window.document.activeElement, ui.$('addCoursesBtn'));
  // A real board edit: move an exam.
  const drop = ui.window.document.querySelector('td[data-day="Wed"][data-period="08:00-10:00"]');
  const event = new ui.window.Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: { getData: () => 'IS201' } });
  drop.dispatchEvent(event);
  await settled();
  assert.equal(ui.$('addCoursesBtn').disabled, false, 'Pressing it says why; a disabled button would not');
  const asked = scopeRequests(ui).length;
  ui.$('addCoursesBtn').click();
  await settled();
  assert.equal(ui.$('examAddCoursesDialog').open, false);
  assert.equal(scopeRequests(ui).length, asked);
  const banner = ui.$('examEditorRequestError');
  assert.equal(banner.hidden, false);
  assert.equal(banner.getAttribute('role'), 'alert');
  assert.match(plain(banner.textContent), TEXT.unsaved);
  assert.equal(ui.window.document.activeElement, ui.$('saveLoadedBtn'));
});

// ── as a background job ──────────────────────────────────────────────────────

test('as a job the panel follows the Add, names it, and shows the report', async t => {
  const JOB = '6f1c2a4e-0000-4000-8000-00000000add1';
  const plan = ['read_board', 'place_exams', 'fewest_moves', 'check_rules', 'assign_rooms', 'save'];
  let frame = 0;
  let submitted = null;
  const job = (status, extra = {}) => ({
    id: JOB, kind: 'add_courses', status, mine: true, can_cancel: true, owner: 'Registrar', has_run: false, result_run_id: null,
    error_code: '', cancelled_by: '', waiting_for: null, refused: false, stopping: false,
    submitted_at: '2026-09-29T10:00:00+00:00', started_at: '2026-09-29T10:00:01+00:00', finished_at: null, now: '2026-09-29T10:00:05+00:00',
    stages: plan.map(key => ({ key, state: status === 'succeeded' ? (key === 'fewest_moves' ? 'skipped' : 'done') : key === 'read_board' ? 'done' : key === 'place_exams' ? 'running' : 'pending' })),
    current: status === 'succeeded' ? { key: 'save', done: null, total: null } : { key: 'place_exams', done: 1, total: 5 }, ...extra,
  });
  const ui = await page(t, {
    onRequest: async (url, options) => {
      if (url === '/ops/exam-timetable/build/') {
        submitted = JSON.parse(options.body);
        return reply({ ok: true, job: job('queued') }, 202);
      }
      if (url === `/ops/exam-timetable/jobs/${JOB}/`) {
        frame += 1;
        return reply({ ok: true, job: frame < 3 ? job('running') : job('succeeded', { has_run: true, result_run_id: 18, finished_at: '2026-09-29T10:00:40+00:00' }) });
      }
      if (url === `/ops/exam-timetable/jobs/${JOB}/result/`) return reply({ ok: true, ...addedRun(submitted) });
      if (url === `/ops/exam-timetable/jobs/${JOB}/seen/`) return reply({ ok: true, marked: true });
      return undefined;
    },
  });
  await openList(ui);
  box(ui, 'MATH101').click();
  box(ui, 'CS111 (2)').click();
  ui.$('confirmAddCourses').click();
  const deadline = Date.now() + 3000;
  let sawTitle = false;
  while (Date.now() < deadline && !plain(ui.$('examRepairReport').textContent)) {
    if (plain(ui.$('examJobTitle').textContent) === TEXT.running) sawTitle = true;
    await pause(2);
  }
  assert.ok(sawTitle, 'The panel named the Add while it ran');
  assert.ok(frame >= 3, 'The job was followed to its end');
  const request = ui.requests.find(item => item.url === '/ops/exam-timetable/build/');
  assert.equal(request.headers['X-Exam-Jobs'], '1');
  assert.match(plain(ui.$('examRepairReport').textContent), TEXT.headline);
});

// ── refusals and what the list says before anything is sent ─────────────────

test('a refusal names its reason and course in the page language', async t => {
  const ui = await page(t, {
    onRequest: async url => (url === '/ops/exam-timetable/build/'
      ? reply({ ok: false, code: 'add_courses_outside_scope', field: 'added_courses[0]', courses: ['ST210'], error: 'server words' }, 400)
      : undefined),
  });
  await openList(ui);
  box(ui, 'ST210').click();
  ui.$('confirmAddCourses').click();
  await settled();
  const banner = ui.$('examEditorRequestError');
  assert.equal(banner.hidden, false);
  assert.match(plain(banner.textContent), TEXT.outside);
  assert.match(plain(banner.textContent), /ST210/);
  assert.doesNotMatch(banner.textContent, /server words/);
  assert.equal(ui.$('etResults').classList.contains('d-none'), false, 'The saved board is still on screen');
});

test('a refusal keeps the ticks and the name for the next opening; Cancel forgets them', async t => {
  let refuse = true;
  const ui = await page(t, {
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/build/' || !refuse) return undefined;
      return reply({ ok: false, code: 'add_courses_unavailable', field: 'added_courses[0]', courses: [], error: 'server words' }, 400);
    },
  });
  await openList(ui);
  box(ui, 'MATH101').click();
  box(ui, 'ST210').click();
  ui.$('examAddCoursesName').value = 'Autumn with the rest';
  ui.$('confirmAddCourses').click();
  await settled();
  assert.equal(ui.$('examAddCoursesDialog').open, false);
  assert.match(plain(ui.$('examEditorRequestError').textContent), TEXT.unavailable);
  await openList(ui);
  const ticked = () => Array.from(ui.$('examAddCoursesList').querySelectorAll('input:checked'), item => item.value).sort();
  assert.deepEqual(ticked(), ['math101-calc', 'st210-stats'], 'Nothing to tick again');
  assert.equal(ui.$('examAddCoursesName').value, 'Autumn with the rest');
  assert.equal(plain(ui.$('confirmAddCourses').textContent), TEXT.submitTwo);
  // Sent again, it is the same two; saved, they are used up.
  refuse = false;
  ui.$('confirmAddCourses').click();
  await settled();
  const sent = builds(ui).at(-1);
  assert.deepEqual(sent.added_courses.map(item => item.course_identity).sort(), ['math101-calc', 'st210-stats']);
  assert.equal(sent.label, 'Autumn with the rest');
  assert.equal(ui.window.eval('_addSent'), null);
});

test('a dismissed list forgets what a refused Add had ticked', async t => {
  const ui = await page(t, {
    onRequest: async url => (url === '/ops/exam-timetable/build/'
      ? reply({ ok: false, error: 'Another exam timetable action is running.', error_code: 'job_in_progress' }, 409)
      : undefined),
  });
  await openList(ui);
  box(ui, 'ST210').click();
  ui.$('confirmAddCourses').click();
  await settled();
  await openList(ui);
  assert.equal(ui.$('examAddCoursesList').querySelectorAll('input:checked').length, 1, 'Kept after a colleague held the queue');
  ui.$('cancelAddCourses').click();
  await openList(ui);
  assert.equal(ui.$('examAddCoursesList').querySelectorAll('input:checked').length, 0, 'Cancel means never mind');
});

test('an empty name in the setup does not stop Add: the list names the new timetable itself', async t => {
  const ui = await page(t);
  ui.input(ui.$('etLabel'), '');
  await openList(ui);
  box(ui, 'ST210').click();
  ui.$('examAddCoursesName').value = 'Named in the list';
  ui.$('confirmAddCourses').click();
  await settled();
  const [sent] = builds(ui);
  assert.ok(sent, 'Sent');
  assert.equal(sent.label, 'Named in the list');
  assert.equal(ui.$('examAddCoursesDialog').open, false);
});

test('settings that cannot be sent are named inside the list, with where to correct them', async t => {
  const ui = await page(t);
  await openList(ui);
  box(ui, 'ST210').click();
  // Whatever the setup refuses is said on its status line; the list repeats it.
  ui.window.eval("collectLoadedRunPayload = () => { $('etStatus').textContent = 'سبب مختبر.'; return null; }");
  ui.$('confirmAddCourses').click();
  await settled();
  assert.equal(builds(ui).length, 0);
  assert.equal(ui.$('examAddCoursesDialog').open, true, 'The ticks stay in front of the registrar');
  assert.match(plain(ui.$('examAddCoursesError').textContent), TEXT.cannotSend);
});

test('the status line never says the courses were added when some were not', async t => {
  for (const [placed, said] of [[0, TEXT.notAll], [1, TEXT.notAll], [2, TEXT.added]]) {
    const ui = await page(t, {
      onRequest: async (url, options) => {
        if (url !== '/ops/exam-timetable/build/') return undefined;
        const answer = addedRun(JSON.parse(options.body));
        answer.add_courses = {
          ...answer.add_courses, placed_count: placed,
          not_placed: answer.add_courses.added.slice(placed).map(row => ({ course_code: row.course_code, reason: 'blocked_by_clashes' })),
        };
        return reply(answer);
      },
    });
    await openList(ui);
    box(ui, 'MATH101').click();
    box(ui, 'ST210').click();
    ui.$('confirmAddCourses').click();
    await settled();
    assert.equal(plain(ui.$('etStatus').textContent), said, `${placed} of 2 placed`);
  }
});

test('an Add refused while the page was closed gives its reason in the page language', async t => {
  const ui = await page(t, {
    open: false,
    onRequest: async url => {
      if (isActiveJobs(url)) return reply({ ok: true, job: endedAdd({ has_run: false, result_run_id: null, refused: true }) });
      if (url === `/ops/exam-timetable/jobs/${ADD_JOB}/result/`) {
        return reply({ ok: false, code: 'add_courses_unavailable', courses: [], error: 'A course to add has no registrations in the imported student timetables any more.' }, 400);
      }
      if (url === `/ops/exam-timetable/jobs/${ADD_JOB}/seen/`) return reply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(ui, () => !ui.$('examJobPanel').hidden && plain(ui.$('examJobDetail').textContent).length > 0, 'The ended Add is shown');
  const detail = plain(ui.$('examJobDetail').textContent);
  assert.match(detail, TEXT.unavailable);
  if (AR) assert.doesNotMatch(detail, /A course to add/, 'Never the English server words on the Arabic page');
});

test('an Add finished while the page was closed opens with its report', async t => {
  const ui = await page(t, {
    open: false,
    onRequest: async url => {
      if (isActiveJobs(url)) return reply({ ok: true, job: endedAdd() });
      if (url === '/ops/exam-timetable/18/') {
        return reply(addedRun({ added_courses: [{ course_identity: 'math101-calc' }, { course_identity: 'st210-stats' }], label: 'Part one + added courses', editor_revision: 0 }));
      }
      if (url === `/ops/exam-timetable/jobs/${ADD_JOB}/seen/`) return reply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(ui, () => !ui.$('examJobOpen').hidden, 'The saved result is offered');
  ui.$('examJobOpen').click();
  await until(ui, () => plain(ui.$('examRepairReport').textContent).length > 0, 'The report is shown');
  assert.match(plain(ui.$('examRepairReport').textContent), TEXT.headline);
  assert.ok(ui.$('examRepairReport').querySelector('[data-find-exam="st210-stats"]'));
});

test('run courses without registrations block the Add before anything is sent', async t => {
  const run = savedRun();
  const ui = await page(t, { scope: scopeAnswer(run, { missing: [{ course_code: 'OLD100', course_identity: 'old100' }] }) });
  await openList(ui);
  assert.equal(ui.$('examAddCoursesMissing').hidden, false);
  assert.match(plain(ui.$('examAddCoursesMissing').textContent), TEXT.missing);
  assert.match(plain(ui.$('examAddCoursesMissing').textContent), /OLD100/);
  box(ui, 'ST210').click();
  ui.$('confirmAddCourses').click();
  await settled();
  assert.equal(builds(ui).length, 0);
  assert.match(plain(ui.$('examAddCoursesError').textContent), TEXT.missing);
});

test('a timetable of another term shows why, and lists nothing to add', async t => {
  const ui = await page(t, { scope: scopeAnswer(savedRun(), { termChanged: true }) });
  await openList(ui);
  assert.match(plain(ui.$('examAddCoursesList').textContent), TEXT.term);
  assert.equal(ui.$('examAddCoursesList').querySelectorAll('input').length, 0);
  ui.$('confirmAddCourses').click();
  await settled();
  assert.equal(builds(ui).length, 0);
});

test('a list that could not load says so and can be asked for again', async t => {
  let fail = true;
  const ui = await page(t, {
    onRequest: async url => {
      if (!url.endsWith('/scope-courses/') || !fail) return undefined;
      fail = false;
      return reply({ ok: false, error: 'down' }, 500);
    },
  });
  await openList(ui);
  assert.equal(ui.$('retryAddCourses').hidden, false);
  assert.equal(ui.$('examAddCoursesError').hidden, false);
  ui.$('retryAddCourses').click();
  await settled();
  assert.equal(ui.$('retryAddCourses').hidden, true);
  assert.equal(ui.$('examAddCoursesList').querySelectorAll('input').length, NEW.length);
});

// ── the report ───────────────────────────────────────────────────────────────

test('the report names what moved and why, what was left out and why, and never throws on a bad report', async t => {
  const ui = await page(t);
  const show = report => { ui.window.eval(`showAddCoursesReport(${JSON.stringify(report)})`); return plain(ui.$('examRepairReport').textContent); };
  const base = {
    requested_count: 3, placed_count: 2,
    added: [
      { course_code: 'MATH101', placed: { day: 'Wed', period: PERIODS[0] } },
      { course_code: 'ST210', placed: { day: 'Thu', period: PERIODS[0] } },
      { course_code: 'CS111 (2)', placed: null },
    ],
    moves: [{ course_code: 'IS201', from: { day: 'Tue', period: PERIODS[2] }, to: { day: 'Thu', period: PERIODS[2] }, made_room_for: ['MATH101'] }],
    not_placed: [
      { course_code: 'CS111 (2)', reason: 'bucket_days_full', blocked_by: { bucket: [{ program: 'CS', programme_term: 3, courses: ['IS201'] }] } },
    ],
    proven_minimal: true, status: 'OPTIMAL', rooms_changed: [{ day: 'Wed', period: PERIODS[0], gender: 'F' }, { day: 'Wed', period: PERIODS[0], gender: 'M' }],
    sections_changed: 2, untouched_violations: 1, locked_count: 3, unassigned_added: 1,
  };
  let text = show(base);
  assert.match(text, TEXT.moved);
  assert.match(text, TEXT.room);
  assert.match(text, TEXT.bucket);
  assert.match(text, /IS201/);
  text = show({ ...base, proven_minimal: false, status: 'FEASIBLE' });
  assert.match(text, TEXT.found);
  text = show({ ...base, not_placed: [{ course_code: 'CS111 (2)', reason: 'blocked_by_fixed_exams', blocked_by: { fixed: ['AI212'] } }] });
  assert.match(text, TEXT.fixed);
  for (const reason of ['only_locked_periods_free', 'search_limit', 'blocked_by_clashes']) {
    text = show({ ...base, not_placed: [{ course_code: 'CS111 (2)', reason }] });
    assert.ok(text.length > 40, reason);
  }
  // Malformed: the courses were added all the same; nothing throws.
  for (const bad of [null, 'text', { added: 'x', moves: 5, not_placed: {} }, { rooms_changed: 'x' }]) {
    show(bad);
    assert.ok(plain(ui.$('examRepairReport').textContent).length > 0);
  }
});

// ── accessibility and who may add ────────────────────────────────────────────

test('the list is a named, described dialog whose choices say what they add, and Cancel gives focus back', async t => {
  const ui = await page(t);
  const dialog = ui.$('examAddCoursesDialog');
  assert.equal(dialog.getAttribute('aria-labelledby'), 'examAddCoursesTitle');
  assert.equal(dialog.getAttribute('aria-describedby'), 'examAddCoursesHelp');
  assert.equal(ui.$('addCoursesBtn').getAttribute('aria-haspopup'), 'dialog');
  assert.equal(ui.$('addCoursesBtn').getAttribute('aria-controls'), 'examAddCoursesDialog');
  await openList(ui);
  const label = box(ui, 'ST210').getAttribute('aria-label');
  assert.equal(plain(label), AR ? 'إضافة ST210 — Statistics' : 'Add ST210 — Statistics');
  assert.equal(ui.$('examAddCoursesCount').getAttribute('role'), 'status');
  assert.ok(ui.window.document.querySelector('label[for="examAddCoursesSearch"]'));
  assert.ok(ui.window.document.querySelector('label[for="examAddCoursesShow"]'));
  assert.ok(ui.window.document.querySelector('label[for="examAddCoursesName"]'));
  // In order: search, show, the list, the name, then the buttons.
  const order = Array.from(dialog.querySelectorAll('input, select, button')).filter(item => !item.hidden).map(item => item.id || item.type);
  assert.deepEqual(order.slice(0, 2), ['examAddCoursesSearch', 'examAddCoursesShow']);
  assert.deepEqual(order.slice(-3), ['examAddCoursesName', 'cancelAddCourses', 'confirmAddCourses']);
  ui.$('cancelAddCourses').click();
  assert.equal(dialog.open, false);
  assert.equal(ui.window.document.activeElement, ui.$('addCoursesBtn'));
});

test('a viewer who may not save sees no way to add courses', async t => {
  const ui = await page(t, { html: readOnlyTemplate });
  assert.equal(ui.$('addCoursesBtn'), null);
  assert.equal(ui.$('examAddCoursesDialog'), null);
  assert.equal(ui.$('courseOutsideAdd'), null);
});

test('the job panel names an Add in the page language', async t => {
  const ui = await page(t);
  const titles = ui.window.eval("jobTitles('add_courses')");
  assert.equal(titles.running, TEXT.running);
  assert.notEqual(titles.done, ui.window.eval("jobTitles('build')").done);
});

test('a saved run opened by a link before the programme chips arrive is still unedited, and Add courses opens', async t => {
  let release;
  const filtersLate = new Promise(resolve => { release = resolve; });
  const ui = await page(t, {
    url: 'http://exam.test/exam-timetable/?run=17',
    open: false,
    onRequest: async url => {
      if (url !== '/ops/exam-timetable/filters/') return undefined;
      await filtersLate;
      return reply({ ok: true, programs: [...SCOPE.programs, 'DS'], sections: SCOPE.sections });
    },
  });
  await settled();
  assert.equal(ui.$('etResults').classList.contains('d-none'), false, 'The linked run is open');
  release();
  await settled();
  assert.deepEqual(Array.from(ui.$('progList').querySelectorAll('input:checked'), input => input.value), SCOPE.programs);
  assert.equal(ui.$('saveLoadedBtn').disabled, true, 'Chips arriving are not an edit');
  assert.equal(ui.window.eval('addCoursesBlockedReason()'), '');
  await openList(ui);
  assert.equal(ui.$('examAddCoursesDialog').open, true);
});
