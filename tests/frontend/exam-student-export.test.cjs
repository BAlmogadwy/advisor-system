/*
 * The "Student data" export dialog (static/js/exam-student-export.js) on the
 * real exam page: executed through tests/test_exam_frontend_interactions.py,
 * so both languages use the current Django template. The page script, its
 * hook and the dialog module run unmodified; only HTTP answers are fake.
 * The preflight and export answers here mirror the shapes the endpoint tests
 * pin (tests/test_exam_student_export_endpoint.py); the browser test
 * (tests/test_exam_student_export_browser.py) runs the real ones end to end.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');

assert.ok(process.env.EXAM_TEST_HTML, 'Run through pytest tests/test_exam_frontend_interactions.py');
const template = fs.readFileSync(process.env.EXAM_TEST_HTML, 'utf8');
const language = process.env.EXAM_TEST_LANGUAGE;
const AR = language === 'ar';
const say = (en, ar) => (AR ? ar : en);
const read = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const SOURCES = [
  ['shared-utils.js', read('shared-utils.js')],
  ['exam-review.js', read('exam-review.js')],
];
const PAGE = read('page-exam-timetable.js');
const EXPORTER = read('exam-student-export.js');

const settle = () => new Promise(resolve => setImmediate(resolve));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
// The debounce is 0 here: one timer turn, then the answer's promise chain.
const idle = async () => { await pause(2); for (let i = 0; i < 6; i++) await settle(); };

const PERIODS = ['08:00-10:00', '10:30-12:30', '13:00-15:00'];
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'];
const FINGERPRINT = 'a'.repeat(64);
const group = (key, gender, section, count, status = 'mapped') => ({
  section_key: key, gender, section, mapping_status: status, student_count: count,
  membership_fingerprint: FINGERPRINT, academic_year: '1448', term: '1', mapping_source: 'scraper_timetable',
});
const part = (key, count, index, total) => ({ section_key: key, student_count: count, room_group_index: index, room_group_count: total });
const COURSES = [
  { course_code: 'MATH101', source_course_code: 'MATH101', course_name: 'CALCULUS I', course_identity: 'math101-calculus', enrolled_count: 42, credit_hours: 3, programs: ['CS', 'IS'] },
  { course_code: 'CS101', source_course_code: 'CS101', course_name: 'PROGRAMMING I', course_identity: 'cs101-programming', enrolled_count: 10, credit_hours: 3, programs: ['CS'] },
  { course_code: 'PHYS103 (2)', source_course_code: 'PHYS103', course_name: 'PHYSICS FOR BUSINESS', course_identity: 'phys103-business', enrolled_count: 5, credit_hours: 3, programs: ['IS'] },
  // Two exams the timetable could not room, in one period; natural order puts CS9 first.
  { course_code: 'CS10', source_course_code: 'CS10', course_name: 'SEMINAR', course_identity: 'cs10-seminar', enrolled_count: 2, credit_hours: 1, programs: ['CS'] },
  { course_code: 'CS9', source_course_code: 'CS9', course_name: 'ETHICS', course_identity: 'cs9-ethics', enrolled_count: 3, credit_hours: 1, programs: ['CS'] },
  // An exam the timetable could not place at all.
  { course_code: 'GS104', source_course_code: 'GS104', course_name: 'ISLAMIC CULTURE', course_identity: 'gs104-culture', enrolled_count: 4, credit_hours: 2, programs: ['AI'] },
];

function savedRun() {
  const slots = DAYS.flatMap(day => PERIODS.map(period => ({ day, period })));
  slots.forEach((slot, index) => { slot.index = index; });
  const placed = { MATH101: 0, CS101: 2, 'PHYS103 (2)': 5, CS10: 4, CS9: 4 };
  // An unplaced exam can carry any slot index; it is still listed last.
  const overflow = { day: 'OVERFLOW', period: '', index: 1 };
  const rooms = {
    MATH101: [
      { room_code: 'M-A', gender: 'M', room_capacity: 20, section_parts: [part('term-section:1', 20, 1, 2)] },
      { room_code: 'M-B', gender: 'M', room_capacity: 20, section_parts: [part('term-section:1', 10, 2, 2)] },
      { room_code: 'F-A', gender: 'F', room_capacity: 20, section_parts: [part('term-section:2', 12, 1, 1)] },
    ],
    CS101: [{ room_code: 'M-C', gender: 'M', room_capacity: 20, section_parts: [part('term-section:3', 10, 1, 1)] }],
    'PHYS103 (2)': [{ room_code: 'M-A', gender: 'M', room_capacity: 20, section_parts: [part('term-section:5', 5, 1, 1)] }],
    CS9: [{ room_code: 'UNASSIGNED', gender: 'M', section_parts: [part('term-section:9', 3, 1, 1)] }],
    CS10: [{ room_code: 'UNASSIGNED', gender: 'F', section_parts: [part('unmapped:F:ambiguous', 2, 1, 1)] }],
    GS104: [],
  };
  const slotOf = code => (code in placed ? { ...slots[placed[code]], slot_index: placed[code] } : { ...overflow, slot_index: overflow.index });
  return {
    ok: true, run_id: 17, label: 'Final v3', created_at: '2026-09-23T21:10:00', primary_status: 'clean', status_flags: [],
    input_fingerprint: 'reviewed-inputs', enrollment_source: 'scraper_timetable',
    courses: COURSES.map(course => course.course_code), courses_count: 6, students_count: 66,
    schedule: COURSES.map(course => ({ ...course, ...slotOf(course.course_code), rooms: rooms[course.course_code] })),
    slots, qa: { max_per_day: 2 }, enrollment_scope: { programs: ['AI', 'CS', 'CS2', 'IS'], sections: ['F', 'M'] }, pinned: [],
    section_enrollment: {
      MATH101: [group('term-section:1', 'M', 'M1', 30), group('term-section:2', 'F', 'F1', 12)],
      CS101: [group('term-section:3', 'M', 'M2', 10)],
      'PHYS103 (2)': [group('term-section:5', 'M', 'M5', 5)],
      CS10: [group('unmapped:F:ambiguous', 'F', '', 2, 'ambiguous')],
      CS9: [group('term-section:9', 'M', 'M9', 3)],
      GS104: [group('term-section:7', 'M', 'M7', 4)],
    },
  };
}

function choices() {
  const section = (key, gender, name, rows, extra = {}) => ({ section_key: key, gender, section: name, mapping_status: 'mapped', membership: 'matches', rows, ...extra });
  return {
    exams: [
      { code: 'MATH101', name: 'CALCULUS I', course_code: 'MATH101', day: 'Sun', period: '08:00-10:00', slot_index: 0, scheduled: true, in_lists: true, rows: 42,
        sections: [section('term-section:1', 'M', 'M1', 30), section('term-section:2', 'F', 'F1', 12)] },
      { code: 'CS101', name: 'PROGRAMMING I', course_code: 'CS101', day: 'Sun', period: '13:00-15:00', slot_index: 2, scheduled: true, in_lists: true, rows: 12,
        sections: [section('term-section:3', 'M', 'M2', 10), section('unmapped:M:missing', 'M', '', 2, { mapping_status: 'missing', membership: 'new' })] },
      { code: 'CS9', name: 'ETHICS', course_code: 'CS9', day: 'Mon', period: '10:30-12:30', slot_index: 4, scheduled: true, in_lists: true, rows: 3,
        sections: [section('term-section:9', 'M', 'M9', 3), section('term-section:8', 'M', 'M8', 0, { membership: 'gone' })] },
      { code: 'CS10', name: 'SEMINAR', course_code: 'CS10', day: 'Mon', period: '10:30-12:30', slot_index: 4, scheduled: true, in_lists: true, rows: 2,
        sections: [section('unmapped:F:ambiguous', 'F', '', 2, { mapping_status: 'ambiguous' })] },
      { code: 'PHYS103 (2)', name: 'PHYSICS FOR BUSINESS', course_code: 'PHYS103', day: 'Mon', period: '13:00-15:00', slot_index: 5, scheduled: true, in_lists: true, rows: 5,
        sections: [section('term-section:5', 'M', 'M5', 5)] },
      { code: 'GS104', name: 'ISLAMIC CULTURE', course_code: 'GS104', day: '', period: '', slot_index: null, scheduled: false, in_lists: true, rows: 4,
        sections: [section('term-section:7', 'M', 'M7', 4)] },
    ],
    periods: [
      { slot_index: 0, day: 'Sun', day_no: 1, period: '08:00-10:00', rows: 42 },
      { slot_index: 2, day: 'Sun', day_no: 1, period: '13:00-15:00', rows: 12 },
      { slot_index: 4, day: 'Mon', day_no: 2, period: '10:30-12:30', rows: 5 },
      { slot_index: 5, day: 'Mon', day_no: 2, period: '13:00-15:00', rows: 5 },
    ],
    days: DAYS.map((day, index) => ({ day, day_no: index + 1, rows: { Sun: 54, Mon: 10 }[day] || 0 })),
    rooms: [
      { slot_index: 0, room_code: 'F-A', gender: 'F', building: '172', rows: 12 },
      { slot_index: 0, room_code: 'M-A', gender: 'M', building: '172', rows: 20 },
      { slot_index: 0, room_code: 'M-B', gender: 'M', building: '172', rows: 10 },
      { slot_index: 2, room_code: 'M-C', gender: 'M', building: '', rows: 10 },
      { slot_index: 5, room_code: 'M-A', gender: 'M', building: '172', rows: 5 },
    ],
    programs: ['AI', 'CS', 'CS2', 'IS'].map(program => ({ program, department: program.startsWith('CS') ? 'cs' : program.toLowerCase(), rows: 10 })),
    departments: [
      { id: 'ai-ds', name_en: 'AI & DS', name_ar: 'الذكاء الاصطناعي وعلوم البيانات', programs: ['AI'] },
      { id: 'cs', name_en: 'Computer Science', name_ar: 'علوم الحاسب', programs: ['CS', 'CS2'] },
      { id: 'is', name_en: 'Information Systems', name_ar: 'نظم المعلومات', programs: ['IS'] },
    ],
    groups: { M: 45, F: 12, U: 0 },
  };
}

function matches(overrides = {}) {
  return {
    ok: true, mode: 'sync',
    run: { id: 17, label: 'Final v3', saved_at: '2026-09-23T21:10:00+03:00', academic_year: '1448', term: '1' },
    check: {
      status: 'matches', sections_total: 6, sections_matching: 6, sections_changed: 0, sections_new: 0, sections_gone: 0,
      program_mix_changed: 0, exams_missing: [], changed: [], no_seat: 0, lists_code_saved: 'L-3F9A2C1D0B', lists_code_now: 'L-3F9A2C1D0B',
    },
    choices: choices(),
    counts: { rows: 57, students: 45, scope_rows: { section: 30, course: 42, room: 12, period: 42, day: 54, all: 57 }, groups: { M: 45, F: 12, U: 0 } },
    files: [
      { name: 'exam_students_all_r17_ar_M_<REF>.xlsx', gender: 'M', rows: 45 },
      { name: 'exam_students_all_r17_ar_F_<REF>.xlsx', gender: 'F', rows: 12 },
    ],
    download_name: 'exam_students_all_r17_ar_<REF>.zip',
    ...overrides,
  };
}

const json = (data, status = data.ok === false ? 409 : 200) => ({
  ok: status < 400, status, headers: { get: name => (name.toLowerCase() === 'content-type' ? 'application/json' : null) }, json: async () => data,
});
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const file = (name = 'exam_students_MATH101-M1_r17_en_7F3A2C1D.xlsx', reference = 'EXR-7F3A2C1D', type = XLSX) => ({
  ok: true, status: 200,
  headers: { get: key => ({ 'content-type': type, 'content-disposition': `attachment; filename="${name}"`, 'x-export-reference': reference })[key.toLowerCase()] || null },
  blob: async () => ({ size: 1024, type }),
});

// A fake server for the dialog's two endpoints; each call can be held.
function exportServer() {
  const calls = [];
  const queues = { preflight: [], export: [] };
  const answer = kind => (queues[kind].length ? queues[kind].shift() : kind === 'preflight' ? json(matches()) : file());
  return {
    calls,
    queue(kind, ...answers) { queues[kind].push(...answers); },
    requests: kind => calls.filter(call => call.kind === kind),
    body: (kind, index = -1) => JSON.parse(calls.filter(call => call.kind === kind).at(index).options.body),
    route(url, options) {
      const match = /^\/ops\/exam-timetable\/(\d+)\/students\/export\/(preflight\/)?$/.exec(url);
      if (!match) return undefined;
      const kind = match[2] ? 'preflight' : 'export';
      const call = { kind, url, options, runId: Number(match[1]) };
      calls.push(call);
      const next = answer(kind);
      if (typeof next === 'function') return next(call);
      return next;
    },
  };
}

// Held answers: the test decides when (and whether) the server replies.
function hold() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { answer: () => promise, release };
}

async function page(t, { run = savedRun(), server = exportServer(), browserFocus = false, storage = null, onRequest = null } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(template, { url: 'http://exam.test/exam-timetable/', runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  t.after(() => { window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  window.localStorage.setItem('exam-timetable-live-update', 'off');
  if (storage) Object.defineProperty(window, 'localStorage', { configurable: true, get: () => storage });
  const toasts = [];
  window.notify = { error: (...args) => toasts.push(['error', ...args]), success: (...args) => toasts.push(['success', ...args]) };
  window.dlg = { confirm: async () => false, prompt: async () => false };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  if (browserFocus) {
    // Focus as a browser keeps it (jsdom does neither): a hidden element, or
    // one in a closed dialog, cannot take it; a focused element that becomes
    // hidden drops it to the body. The PR #115 model, plus closed dialogs.
    const hiddenNow = element => !element.isConnected
      || Boolean(element.closest('[hidden], .d-none, dialog:not([open])'));
    const focus = window.HTMLElement.prototype.focus;
    window.HTMLElement.prototype.focus = function (...args) {
      if (hiddenNow(this)) return undefined;
      return focus.apply(this, args);
    };
    const active = Object.getOwnPropertyDescriptor(window.Document.prototype, 'activeElement');
    Object.defineProperty(window.document, 'activeElement', {
      configurable: true,
      get() {
        const at = active.get.call(this);
        if (at && at !== this.body && hiddenNow(at)) {
          at.blur();
          return active.get.call(this);
        }
        return at;
      },
    });
  }
  Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => false });
  Object.defineProperty(window.document, 'visibilityState', { configurable: true, get: () => 'visible' });
  const requests = [];
  server.window = window;
  window.fetch = async (url, options = {}) => {
    requests.push({ url, ...options });
    const routed = server.route(url, options);
    if (routed !== undefined) return routed;
    if (onRequest) {
      const answered = await onRequest(url, options);
      if (answered !== undefined) return answered;
    }
    let data;
    if (url === '/ops/exam-timetable/filters/') data = { ok: true, programs: ['AI', 'CS', 'CS2', 'IS'], sections: ['F', 'M'] };
    else if (url.startsWith('/ops/exam-timetable/list/')) data = { ok: true, runs: [{ id: 17, label: run.label }] };
    else if (url === '/ops/exam-timetable/17/') data = run;
    else if (url === '/ops/exam-timetable/preview-courses/') data = { ok: true, courses: COURSES };
    else if (String(url).split('?')[0] === '/ops/exam-timetable/jobs/active/') data = { ok: true, job: null };
    else {
      const error = new Error(`Unexpected HTTP request: ${url}`);
      errors.push(error);
      throw error;
    }
    return json(data, 200);
  };
  const downloads = [];
  const revoked = [];
  window.URL.createObjectURL = blob => { downloads.push({ blob }); return `blob:student-export-${downloads.length}`; };
  window.URL.revokeObjectURL = url => revoked.push(url);
  window.HTMLAnchorElement.prototype.click = function () {
    if (this.download) downloads.at(-1).saved = { href: this.href, filename: this.download };
  };
  window.__examJobPoll = { first: 0, quick: 0, steady: 0, slow: 0, slowAfter: 0, announce: 0, reveal: 0, stall: 0, minShown: 0, backoff: [0, 0, 0, 0], timeout: 2000, checkRetry: 30 };
  window.__examStudentExportTiming = { debounce: 0, revoke: 0 };
  const context = dom.getInternalVMContext();
  SOURCES.forEach(([filename, code]) => vm.runInContext(code, context, { filename }));
  vm.runInContext(`const LANGUAGE_CODE = ${JSON.stringify(language)};\n${PAGE}`, context, { filename: 'page-exam-timetable.js' });
  vm.runInContext(EXPORTER, context, { filename: 'exam-student-export.js' });
  const $ = id => window.document.getElementById(id);
  await settle();
  $('loadCoursesBtn').click();
  await settle();
  $('historyList').querySelector('.et-run-info').click();
  await settle();
  const emit = (element, type) => element.dispatchEvent(new window.Event(type, { bubbles: true }));
  const choose = (id, value) => { $(id).value = value; emit($(id), 'change'); };
  const tick = (element, on) => { if (element.checked !== on) element.click(); };
  const radio = (name, value) => window.document.querySelector(`input[name="${name}"][value="${value}"]`);
  const text = id => $(id).textContent.replace(/\s+/g, ' ').trim();
  return { window, $, emit, choose, tick, radio, text, server, requests, downloads, revoked, toasts };
}

async function opened(t, options = {}) {
  const ui = await page(t, options);
  ui.$('examStudentDataBtn').click();
  await idle();
  return ui;
}

const values = select => Array.from(select.options, option => option.value);
const labels = select => Array.from(select.options, option => option.textContent);
const dropExam = (ui, day, period = '08:00-10:00', code = 'CS101') => {
  const cell = Array.from(ui.$('schedGrid').querySelectorAll('td[data-day]')).find(item => item.dataset.day === day && item.dataset.period === period);
  assert.ok(cell, `Missing target ${day} ${period}`);
  const event = new ui.window.Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: { getData: () => code } });
  cell.dispatchEvent(event);
};
const scopeCount = (ui, kind) => ui.window.document.querySelector(`[data-scope-count="${kind}"]`).textContent;
const groupCount = (ui, gender) => ui.window.document.querySelector(`[data-group-count="${gender}"]`).textContent;
const SAVE_FIRST = say('Unsaved changes. Save Changes or Optimize before exporting.', 'توجد تغييرات غير محفوظة. احفظ التغييرات أو حسّن الجدول قبل التصدير.');
const PICKERS = { exam: 'MATH101', section_key: 'term-section:1', gender: 'M', slot_index: 0, room_code: 'F-A', day: 'Sun' };
const DEFAULT_BODY = {
  scope: { kind: 'all' }, programs: [], groups: ['M', 'F'], one_file_per_group: true,
  rows: 'all', contents: 'full', language: 'ar', dates: {},
};

// ── Entry points ────────────────────────────────────────────

test('Student data follows Department files in the save group and waits, with its reason, while moves are unsaved', async t => {
  const ui = await page(t);
  const button = ui.$('examStudentDataBtn');
  assert.equal(ui.$('departmentFilesBtn').nextElementSibling, button);
  assert.equal(button.closest('.et-save-controls'), ui.$('departmentFilesBtn').closest('.et-save-controls'));
  assert.equal(button.className, ui.$('departmentFilesBtn').className.replace(' d-none', ''));
  assert.equal(button.textContent, say('Student data', 'بيانات الطلاب'));
  assert.equal(button.getAttribute('aria-haspopup'), 'dialog');
  assert.equal(button.getAttribute('aria-controls'), 'examStudentExportDialog');
  assert.equal(button.getAttribute('aria-disabled'), 'false');
  assert.equal(ui.$(button.getAttribute('aria-describedby')).textContent, '');

  dropExam(ui, 'Mon');
  assert.equal(button.getAttribute('aria-disabled'), 'true');
  assert.equal(button.disabled, false, 'aria-disabled keeps the reason reachable by keyboard');
  assert.equal(ui.$(button.getAttribute('aria-describedby')).textContent, SAVE_FIRST);
  assert.equal(button.title, SAVE_FIRST);
  button.click();
  await idle();
  assert.equal(ui.$('examStudentExportDialog').open, false);
  assert.equal(ui.server.calls.length, 0);

  ui.$('undoExamBtn').click();
  await idle();
  assert.equal(button.getAttribute('aria-disabled'), 'false');
  assert.equal(ui.$('examStudentDataReason').textContent, '');
});

test('without a saved run on the board Student data says to save first and opens nothing', async t => {
  const run = savedRun();
  delete run.run_id;
  const ui = await page(t, { run });
  const button = ui.$('examStudentDataBtn');
  assert.equal(button.getAttribute('aria-disabled'), 'true');
  assert.equal(ui.$('examStudentDataReason').textContent, say('Save this timetable to export its student data.', 'احفظ هذا الجدول لتصدير بيانات طلابه.'));
  button.click();
  await idle();
  assert.equal(ui.$('examStudentExportDialog').open, false);
  assert.equal(ui.server.calls.length, 0);
});

test('the Department files dialog opens student data in place and focus comes back to Department files', async t => {
  const ui = await page(t, { browserFocus: true, onRequest: async url => (url.includes('/departments/?')
    ? json({ ok: true, departments: [{ id: 'cs', name: 'CS', programs: ['CS'], course_count: 1, student_sittings: 10 }], days: ['Sun'], genders: ['M'] }) : undefined) });
  ui.$('departmentFilesBtn').click();
  await idle();
  const link = ui.$('examDepartmentStudentLink');
  assert.equal(link.closest('dialog'), ui.$('examDepartmentDialog'));
  assert.equal(link.textContent, say('Export student data…', 'تصدير بيانات الطلاب…'));
  assert.match(link.parentElement.textContent, say(/Need student names and IDs\?/, /تحتاج أسماء الطلاب وأرقامهم الجامعية؟/));
  link.click();
  await idle();
  assert.equal(ui.$('examDepartmentDialog').open, false);
  assert.equal(ui.$('examStudentExportDialog').open, true);
  assert.equal(ui.window.document.activeElement, ui.$('examStudentExportTitle'));
  assert.equal(ui.server.requests('preflight').length, 1);
  ui.$('examStudentExportCancel').click();
  assert.equal(ui.$('examStudentExportDialog').open, false);
  assert.equal(ui.window.document.activeElement, ui.$('departmentFilesBtn'));
});

// ── Opening and the check ───────────────────────────────────

test('the dialog opens on its heading with the saved run in its pickers and saved figures while the lists are checked', async t => {
  const server = exportServer();
  const held = hold();
  server.queue('preflight', held.answer);
  const ui = await page(t, { server, browserFocus: true });
  ui.$('examStudentDataBtn').click();
  await idle();
  const dialog = ui.$('examStudentExportDialog');
  assert.equal(dialog.open, true);
  assert.equal(ui.window.document.activeElement, ui.$('examStudentExportTitle'));
  assert.equal(ui.$('examStudentExportTitle').getAttribute('tabindex'), '-1');
  assert.equal(dialog.getAttribute('aria-labelledby'), 'examStudentExportTitle');
  assert.equal(ui.text('examStudentExportTitle'), say('Export student data to Excel', 'تصدير بيانات الطلاب إلى Excel'));
  assert.equal(ui.text('examStudentExportSource'), say(
    'Timetable #17 “Final v3” · saved 2026-09-23 21:10 · 1448 term 1',
    'الجدول رقم 17 «Final v3» · حُفظ 2026-09-23 21:10 · الفصل 1 من عام 1448'));

  assert.equal(ui.$('examStudentExportCheck').dataset.state, 'checking');
  assert.equal(ui.text('examStudentExportCheckTitle'), say('Checking student lists…', 'جارٍ مطابقة قوائم الطلاب…'));
  const download = ui.$('examStudentExportDownload');
  assert.equal(download.getAttribute('aria-disabled'), 'true');
  assert.equal(download.getAttribute('aria-describedby'), 'examStudentExportReason');
  assert.equal(ui.text('examStudentExportReason'), say('Wait until the student lists are checked.', 'انتظر حتى تنتهي مطابقة قوائم الطلاب.'));

  // The saved run's own facts, before the server answers: exams in timetable
  // order (natural within a period, unplaced last), no OVERFLOW period or day.
  assert.deepEqual(values(ui.$('examStudentCourse')), ['MATH101', 'CS101', 'CS9', 'CS10', 'PHYS103 (2)', 'GS104']);
  assert.deepEqual(values(ui.$('examStudentSectionExam')), values(ui.$('examStudentCourse')));
  assert.equal(labels(ui.$('examStudentCourse'))[0], 'MATH101 · CALCULUS I');
  assert.equal(labels(ui.$('examStudentCourse')).at(-1), `GS104 · ISLAMIC CULTURE · ${say('Not scheduled', 'غير مجدول')}`);
  // Exam labels lead with a code: they read left to right in either language.
  assert.deepEqual([ui.$('examStudentCourse').dir, ui.$('examStudentSectionExam').dir], ['ltr', 'ltr']);
  assert.deepEqual(labels(ui.$('examStudentSection')), ['M1', 'F1']);
  assert.deepEqual(values(ui.$('examStudentPeriod')), ['0', '2', '4', '5']);
  assert.deepEqual(labels(ui.$('examStudentPeriod')), ['Sun 08:00-10:00', 'Sun 13:00-15:00', 'Mon 10:30-12:30', 'Mon 13:00-15:00']);
  assert.deepEqual(values(ui.$('examStudentRoomPeriod')), values(ui.$('examStudentPeriod')));
  assert.deepEqual(values(ui.$('examStudentRoom')), ['F-A', 'M-A', 'M-B']);
  assert.deepEqual(labels(ui.$('examStudentRoom')), ['F-A', 'M-A', 'M-B'].map((room, i) => `${room} · ${i ? say('Male', 'طلاب') : say('Female', 'طالبات')}`));
  assert.deepEqual(values(ui.$('examStudentDay')), DAYS);
  assert.equal(ui.radio('examStudentScope', 'all').checked, true);
  assert.equal(ui.radio('examStudentLanguage', 'ar').checked, true, 'Arabic files on first use, as Department files');
  assert.equal(ui.radio('examStudentContents', 'full').checked, true);
  assert.equal(ui.$('examStudentOneFile').checked, true);
  assert.equal(ui.window.document.querySelector('[data-group="U"]').hidden, true, 'no Not recorded group sits exams in this run');

  // Saved figures, said to be approximate, until the check answers.
  const about = n => say(`about ${n} rows`, `الأسطر: نحو ${n}`);
  assert.equal(scopeCount(ui, 'all'), about(66));
  assert.equal(scopeCount(ui, 'course'), about(42));
  assert.equal(scopeCount(ui, 'section'), about(30));
  assert.equal(scopeCount(ui, 'period'), about(42));
  assert.equal(scopeCount(ui, 'room'), about(12));
  assert.equal(scopeCount(ui, 'day'), about(42 + 10));
  assert.equal(groupCount(ui, 'M'), '');

  const [call] = server.requests('preflight');
  assert.equal(call.url, '/ops/exam-timetable/17/students/export/preflight/');
  assert.equal(call.options.method, 'POST');
  assert.equal(call.options.headers['X-CSRFToken'], 'test-csrf');
  assert.deepEqual(JSON.parse(call.options.body), { ...DEFAULT_BODY, pickers: PICKERS });
  held.release(json(matches()));
  await idle();
  assert.equal(ui.$('examStudentExportCheck').dataset.state, 'matches');
});

test('saved figures are offered only for the options they were saved for', async t => {
  const server = exportServer();
  const held = hold();
  server.queue('preflight', held.answer, held.answer, held.answer, held.answer);
  const ui = await page(t, { server });
  ui.$('examStudentDataBtn').click();
  await idle();
  // Before any answer the rooms are the saved run's: a part the timetable
  // could not room is no room to choose.
  ui.choose('examStudentRoomPeriod', '4');
  assert.deepEqual(labels(ui.$('examStudentRoom')), [say('None', 'لا يوجد')]);
  assert.equal(scopeCount(ui, 'room'), '');
  ui.choose('examStudentRoomPeriod', '0');
  ui.choose('examStudentRoom', 'M-B');
  await idle();
  assert.equal(scopeCount(ui, 'room'), say('about 10 rows', 'الأسطر: نحو 10'));
  assert.match(scopeCount(ui, 'all'), /66/);
  ui.tick(ui.window.document.querySelector('input[name="examStudentGroup"][value="F"]'), false);
  await idle();
  assert.equal(scopeCount(ui, 'all'), '', 'a filtered choice is not about the saved total');
  assert.equal(scopeCount(ui, 'course'), '');
});

test('a matching check fills every count, the groups, the file list and a plain Download', async t => {
  const ui = await opened(t);
  assert.equal(ui.$('examStudentExportCheck').dataset.state, 'matches');
  assert.equal(ui.$('examStudentExportCheckMark').textContent, '✓');
  assert.equal(ui.text('examStudentExportCheckTitle'), say('Student lists match this timetable', 'قوائم الطلاب مطابقة لهذا الجدول'));
  assert.equal(ui.text('examStudentExportCheckDetail'), say('6 of 6 sections identical · Lists L-3F9A2C1D0B', 'الشعب المطابقة: 6 من 6 · رمز القوائم L-3F9A2C1D0B'));
  assert.equal(ui.$('examStudentExportCheckDetail').querySelector('bdi:last-child').dir, 'ltr');
  const rows = n => say(`${n} rows`, `الأسطر: ${n}`);
  assert.deepEqual(['section', 'course', 'room', 'period', 'day', 'all'].map(kind => scopeCount(ui, kind)),
    [30, 42, 12, 42, 54, 57].map(rows));
  // The count is part of each radio's accessible name.
  const all = ui.radio('examStudentScope', 'all');
  assert.equal(all.getAttribute('aria-labelledby').split(' ').map(id => ui.text(id)).join(' '),
    `${say('Whole timetable', 'الجدول كاملاً')} ${rows(57)}`);
  assert.equal(groupCount(ui, 'M'), rows(45));
  assert.equal(groupCount(ui, 'F'), rows(12));
  assert.equal(ui.text('examStudentExportSummary'), say('2 files in one .zip · 57 rows · downloads now', 'ملفان في ملف مضغوط واحد · الأسطر: 57 · يُنزَّل الآن'));
  assert.deepEqual(Array.from(ui.$('examStudentExportFiles').querySelectorAll('.et-export-file-name'), node => [node.textContent, node.dir]), [
    ['exam_students_all_r17_ar_M_….xlsx', 'ltr'], ['exam_students_all_r17_ar_F_….xlsx', 'ltr'],
  ]);
  assert.equal(ui.text('examStudentExportPrivacyText'), say(
    'Contains student names and IDs. Share only with exam staff. This export is recorded under your name.',
    'يحتوي على أسماء الطلاب وأرقامهم الجامعية. شاركه مع منسوبي الاختبارات فقط. يُسجَّل هذا التصدير باسمك.'));
  const download = ui.$('examStudentExportDownload');
  assert.equal(download.textContent, say('Download', 'تنزيل'));
  assert.equal(download.getAttribute('aria-disabled'), 'false');
  assert.equal(ui.$('examStudentExportReason').hidden, true);

  // The server's reading of the run adds a section new since the save.
  ui.choose('examStudentCourse', 'CS101');
  assert.deepEqual(labels(ui.$('examStudentSection')), ['M2', say('Section not recorded · Male · new since save', 'الشعبة غير مسجلة · طلاب · جديدة بعد الحفظ')]);
  assert.deepEqual(Array.from(ui.$('examStudentProgramList').querySelectorAll('input'), input => [input.value, input.checked]),
    [['AI', true], ['CS', true], ['CS2', true], ['IS', true]]);
  assert.deepEqual(Array.from(ui.$('examStudentDepartments').querySelectorAll('button'), button => button.textContent),
    AR ? ['الذكاء الاصطناعي وعلوم البيانات', 'علوم الحاسب', 'نظم المعلومات'] : ['AI & DS', 'Computer Science', 'Information Systems']);
});

test('a changed check lists what changed, who has no seat, and marks the download', async t => {
  const server = exportServer();
  server.queue('preflight', json(matches({ check: {
    ...matches().check, status: 'changed', sections_total: 12, sections_matching: 5,
    changed: [
      { exam: 'MATH101', section: 'M1', mapping_status: 'mapped', gender: 'M', saved: 30, now: 32, membership: 'changed', program_mix: 'changed' },
      { exam: 'CS101', section: '', mapping_status: 'missing', gender: 'M', saved: 0, now: 2, membership: 'new', program_mix: 'changed' },
      { exam: 'PHYS103 (2)', section: 'M5', mapping_status: 'mapped', gender: 'M', saved: 5, now: 5, membership: 'matches', program_mix: 'changed' },
      { exam: 'CS9', section: 'M8', mapping_status: 'mapped', gender: 'M', saved: 2, now: 0, membership: 'gone', program_mix: 'changed' },
      { exam: 'CS10', section: '', mapping_status: 'ambiguous', gender: 'F', saved: 2, now: 3, membership: 'changed', program_mix: 'matches' },
      { exam: 'GS104', section: 'M7', mapping_status: 'mapped', gender: 'M', saved: 4, now: 4, membership: 'changed', program_mix: 'matches' },
      { exam: 'MATH101', section: 'F1', mapping_status: 'mapped', gender: 'F', saved: 12, now: 11, membership: 'changed', program_mix: 'changed' },
    ],
    exams_missing: ['CS9', 'GS104'],
    no_seat: 2,
  } })));
  const ui = await opened(t, { server });
  assert.equal(ui.$('examStudentExportCheck').dataset.state, 'changed');
  assert.equal(ui.$('examStudentExportCheckMark').textContent, '≠');
  assert.equal(ui.text('examStudentExportCheckTitle'), say('Student lists changed since this timetable was saved', 'تغيّرت قوائم الطلاب بعد حفظ هذا الجدول'));
  assert.equal(ui.text('examStudentExportCheckDetail'), say('7 of 12 sections differ. Every affected row is marked in the file.', 'الشعب المختلفة: 7 من 12. كل سطر متأثر مُعلَّم في الملف.'));
  assert.deepEqual(Array.from(ui.$('examStudentExportChanges').children, li => li.textContent), say(
    ['MATH101 M1: 30 → 32', 'CS101 Section not recorded: new since save (+2)', 'PHYS103 (2) M5: program mix changed',
      'CS9 M8: gone since save (−2)', 'CS10 Section unclear: 2 → 3', 'and 2 more', 'No longer in the lists: CS9 · GS104'],
    ['MATH101 M1: 30 → 32', 'CS101 الشعبة غير مسجلة: جديدة بعد الحفظ (+2)', 'PHYS103 (2) M5: تغيّر توزيع البرامج',
      'CS9 M8: غير موجودة الآن (−2)', 'CS10 الشعبة غير محددة: 2 → 3', 'شعب أخرى: 2', 'اختبارات لم تعد في القوائم: CS9 · GS104']));
  assert.ok(Array.from(ui.$('examStudentExportChanges').querySelectorAll('bdi')).every(bdi => bdi.dir === 'ltr'));
  assert.equal(ui.text('examStudentExportCheckNote'), say(
    'Rooms were sized for the saved counts; 2 students will have No seat. To resize rooms: Check changes, then Save Changes.',
    'حُسبت القاعات على الأعداد المحفوظة. الطلاب بلا مقعد: 2. لإعادة توزيع القاعات: افحص التغييرات ثم احفظها.'));
  assert.equal(ui.$('examStudentExportDownload').textContent, say('Download with changes marked', 'تنزيل مع إبراز التغييرات'));
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'false');

  server.queue('preflight', json(matches({ check: { ...matches().check, status: 'changed', changed: [
    { exam: 'MATH101', section: 'M1', mapping_status: 'mapped', gender: 'M', saved: 30, now: 31, membership: 'changed', program_mix: 'matches' },
  ], no_seat: 1 } })));
  ui.tick(ui.$('examStudentOneFile'), false);
  await idle();
  assert.equal(ui.text('examStudentExportCheckNote'), say(
    'Rooms were sized for the saved counts; 1 student will have No seat. To resize rooms: Check changes, then Save Changes.',
    'حُسبت القاعات على الأعداد المحفوظة. الطلاب بلا مقعد: 1. لإعادة توزيع القاعات: افحص التغييرات ثم احفظها.'));
});

test('a check with no students for these options says so and offers nothing to download', async t => {
  const server = exportServer();
  server.queue('preflight', json(matches({ counts: { rows: 0, students: 0, scope_rows: { all: 0 }, groups: { M: 0, F: 0, U: 0 } }, files: [], download_name: '' })));
  const ui = await opened(t, { server });
  assert.equal(scopeCount(ui, 'all'), say('0 rows', 'الأسطر: 0'));
  assert.equal(scopeCount(ui, 'course'), '', 'a scope the answer did not price shows nothing');
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'true');
  assert.equal(ui.text('examStudentExportReason'), say('No students match these choices.', 'لا يوجد طلاب مطابقون لهذه الاختيارات.'));
  assert.equal(ui.text('examStudentExportSummary'), '');
  assert.equal(ui.$('examStudentExportFiles').hidden, true);
  ui.$('examStudentExportDownload').click();
  await idle();
  assert.equal(server.requests('export').length, 0);
});

test('a group new since the save is offered, ticked and priced; a one-group run makes one file', async t => {
  const server = exportServer();
  server.queue('preflight', json(matches({ choices: { ...choices(), groups: { M: 45, F: 12, U: 1 } } })));
  const ui = await opened(t, { server });
  await idle();
  const notRecorded = ui.window.document.querySelector('[data-group="U"]');
  assert.equal(notRecorded.hidden, false);
  assert.equal(notRecorded.querySelector('input').checked, true);
  assert.equal(server.requests('preflight').length, 2, 'the options changed under the answer, so it asks again');
  assert.deepEqual(server.body('preflight').groups, ['M', 'F', 'U']);

  const run = savedRun();
  run.section_enrollment.MATH101 = run.section_enrollment.MATH101.filter(item => item.gender === 'M');
  run.section_enrollment.CS10 = [];
  const single = exportServer();
  single.queue('preflight', json(matches({ choices: { ...choices(), groups: { M: 45, F: 0, U: 0 } } })));
  const men = await opened(t, { run, server: single });
  assert.equal(men.window.document.querySelector('[data-group="F"]').hidden, true);
  assert.equal(men.$('examStudentOneFile').checked, false);
  assert.deepEqual(single.body('preflight', 0).groups, ['M']);
  assert.equal(single.body('preflight', 0).one_file_per_group, false);
});

test('a period whose exams have no room offers no room, and One room waits with its reason', async t => {
  const server = exportServer();
  const ui = await opened(t, { server });
  const asked = server.requests('preflight').length;
  ui.choose('examStudentRoomPeriod', '4');
  await idle();
  assert.equal(ui.radio('examStudentScope', 'room').checked, true);
  assert.deepEqual(labels(ui.$('examStudentRoom')), [say('None', 'لا يوجد')]);
  assert.equal(ui.$('examStudentRoom').disabled, true);
  assert.equal(ui.text('examStudentExportReason'), say('Complete the choice of exams.', 'أكمل اختيار الاختبارات.'));
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'true');
  assert.equal(server.requests('preflight').length, asked, 'an incomplete choice is not priced');
  // The unclear and gone sections are named as such.
  ui.choose('examStudentCourse', 'CS10');
  assert.deepEqual(labels(ui.$('examStudentSection')), [say('Section unclear · Female', 'الشعبة غير محددة · طالبات')]);
  ui.choose('examStudentCourse', 'CS9');
  assert.deepEqual(labels(ui.$('examStudentSection')), ['M9', `M8 · ${say('gone since save', 'غير موجودة الآن')}`]);
});

test('a refused run says why, shows no counts, and never downloads', async t => {
  for (const [answer, message] of [
    [json({ ok: false, code: 'rebuild_required', error: 'x' }), say('Rebuild and save this timetable to export student data.', 'أعد بناء هذا الجدول واحفظه لتصدير بيانات الطلاب.')],
    [json({ ok: false, code: 'lists_unavailable', error: 'x' }), say('No student timetables have been imported for this term.', 'لم تُستورد الجداول الدراسية للطلاب لهذا الفصل.')],
    [json({ ok: false, code: 'lists_term_mismatch', error: 'x', live_term: ['1448', '2'], saved_term: ['1448', '1'] }),
      say("Student lists can't be exported for this timetable: the imported student timetables are for 1448 term 2 and this exam timetable is for 1448 term 1.",
        'لا يمكن تصدير قوائم الطلاب لهذا الجدول: الجداول الدراسية المستوردة تخص الفصل 2 من عام 1448، وجدول الاختبارات هذا يخص الفصل 1 من عام 1448.')],
    [json({ ok: false, code: 'not_found', error: 'Run not found' }, 404), say('This saved timetable was deleted.', 'حُذف هذا الجدول المحفوظ.')],
  ]) {
    const server = exportServer();
    server.queue('preflight', answer);
    const ui = await opened(t, { server });
    assert.equal(ui.$('examStudentExportCheck').dataset.state, 'refused');
    assert.equal(ui.$('examStudentExportCheckMark').textContent, '✕');
    assert.equal(ui.text('examStudentExportCheckDetail'), message);
    assert.equal(ui.text('examStudentExportReason'), message);
    assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'true');
    assert.equal(scopeCount(ui, 'all'), '');
    assert.equal(ui.text('examStudentExportSummary'), '');
    ui.$('examStudentExportDownload').click();
    ui.tick(ui.$('examStudentOneFile'), false);
    await idle();
    assert.equal(server.requests('export').length, 0);
    assert.equal(server.requests('preflight').length, 1, 'a refused run is not asked again for every option');
    ui.$('examStudentExportCancel').click();
  }
});

// ── Options ─────────────────────────────────────────────────

test('an option change re-prices once, dims the old counts meanwhile, and a superseded answer is dropped', async t => {
  const server = exportServer();
  const ui = await opened(t, { server });
  const first = hold();
  const second = hold();
  server.queue('preflight', first.answer, second.answer);
  ui.tick(ui.window.document.querySelector('input[name="examStudentGroup"][value="F"]'), false);
  ui.tick(ui.$('examStudentOneFile'), false);
  await idle();
  assert.equal(server.requests('preflight').length, 2, 'two changes in one turn ask once');
  assert.deepEqual(server.body('preflight'), { ...DEFAULT_BODY, groups: ['M'], one_file_per_group: false, pickers: PICKERS });
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'true');
  assert.equal(ui.text('examStudentExportReason'), say('Updating the counts for these choices…', 'جارٍ تحديث الأعداد لهذه الاختيارات…'));
  assert.ok(ui.window.document.querySelector('[data-scope-count="all"]').classList.contains('is-waiting'));
  assert.equal(ui.$('examStudentExportScope').getAttribute('aria-busy'), 'true');

  ui.radio('examStudentRows', 'flagged').click();
  await idle();
  assert.equal(server.requests('preflight').length, 3);
  assert.equal(server.calls.filter(call => call.kind === 'preflight')[1].options.signal.aborted, true);
  assert.equal(server.body('preflight').rows, 'flagged');
  second.release(json(matches({ counts: { ...matches().counts, rows: 9, scope_rows: { ...matches().counts.scope_rows, all: 9 } },
    files: [{ name: 'exam_students_all_flagged_r17_ar_<REF>.xlsx', gender: null, rows: 9 }] })));
  await idle();
  first.release(json(matches({ counts: { ...matches().counts, rows: 999, scope_rows: { all: 999 } } })));
  await idle();
  assert.equal(scopeCount(ui, 'all'), say('9 rows', 'الأسطر: 9'));
  assert.equal(ui.text('examStudentExportSummary'), say('1 file · 9 rows · downloads now', 'ملف واحد · الأسطر: 9 · يُنزَّل الآن'));
  assert.equal(ui.window.document.querySelector('[data-scope-count="all"]').classList.contains('is-waiting'), false);
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'false');

  // Prepared for is not counted or named, so it asks nothing.
  ui.$('examStudentPreparedFor').value = 'Head of CS';
  ui.emit(ui.$('examStudentPreparedFor'), 'change');
  await idle();
  assert.equal(server.requests('preflight').length, 3);
});

test('a department shortcut picks exactly its programs, pressed again every program, and none ticked asks nothing', async t => {
  const server = exportServer();
  const ui = await opened(t, { server });
  const cs = ui.$('examStudentDepartments').querySelectorAll('button')[1];
  const ticked = () => Array.from(ui.$('examStudentProgramList').querySelectorAll('input:checked'), input => input.value);
  cs.click();
  await idle();
  assert.equal(cs.getAttribute('aria-pressed'), 'true');
  assert.deepEqual(ticked(), ['CS', 'CS2']);
  assert.equal(ui.$('examStudentAllPrograms').indeterminate, true);
  assert.deepEqual(server.body('preflight').programs, ['CS', 'CS2']);
  cs.click();
  await idle();
  assert.equal(cs.getAttribute('aria-pressed'), 'false');
  assert.deepEqual(ticked(), ['AI', 'CS', 'CS2', 'IS']);
  assert.equal(ui.$('examStudentAllPrograms').checked, true);
  assert.deepEqual(server.body('preflight').programs, [], 'every program is sent as none, the server\'s "all"');

  const asked = server.requests('preflight').length;
  ui.$('examStudentAllPrograms').click();
  await idle();
  assert.deepEqual(ticked(), []);
  assert.equal(server.requests('preflight').length, asked);
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'true');
  assert.equal(ui.text('examStudentExportReason'), say('Choose at least one program.', 'اختر برنامجاً واحداً على الأقل.'));
  ui.$('examStudentProgramList').querySelector('input[value="IS"]').click();
  await idle();
  assert.deepEqual(server.body('preflight').programs, ['IS']);

  // A half-typed date is neither priced nor sent; it waits with its reason.
  const sunday = ui.$('examStudentDates').querySelector('input[data-day="Sun"]');
  Object.defineProperty(sunday, 'validity', { configurable: true, value: { valid: false } });
  ui.emit(sunday, 'change');
  await idle();
  assert.equal(server.requests('preflight').length, asked + 1);
  assert.equal(ui.text('examStudentExportReason'), say('Enter a valid date or leave the field blank.', 'أدخل تاريخاً صحيحاً أو اترك الحقل فارغاً.'));
  delete sunday.validity;
  ui.emit(sunday, 'change');
  await idle();
  assert.equal(server.requests('preflight').length, asked + 2);

  ui.window.document.querySelectorAll('input[name="examStudentGroup"]').forEach(box => ui.tick(box, false));
  await idle();
  assert.equal(server.requests('preflight').length, asked + 2);
  assert.equal(ui.text('examStudentExportReason'), say('Tick at least one group.', 'اختر فئة واحدة على الأقل.'));
  ui.$('examStudentExportDownload').click();
  await idle();
  assert.equal(server.requests('export').length, 0);
});

test('choosing in a row picks that row, the paired pickers agree, and the scope travels as timetable facts only', async t => {
  const server = exportServer();
  const ui = await opened(t, { server });
  ui.choose('examStudentCourse', 'CS101');
  assert.equal(ui.$('examStudentSectionExam').value, 'CS101', 'the pickers agree at once, not only after the answer');
  await idle();
  assert.equal(ui.radio('examStudentScope', 'course').checked, true);
  assert.equal(ui.$('examStudentSectionExam').value, 'CS101');
  assert.deepEqual(server.body('preflight').scope, { kind: 'course', exam: 'CS101' });
  assert.deepEqual(server.body('preflight').pickers, { ...PICKERS, exam: 'CS101', section_key: 'term-section:3' });

  ui.choose('examStudentSection', ui.$('examStudentSection').options[1].value);
  await idle();
  assert.equal(ui.radio('examStudentScope', 'section').checked, true);
  assert.deepEqual(server.body('preflight').scope, { kind: 'section', exam: 'CS101', section_key: 'unmapped:M:missing', gender: 'M' });

  ui.choose('examStudentRoomPeriod', '5');
  assert.equal(ui.$('examStudentPeriod').value, '5');
  await idle();
  assert.equal(ui.radio('examStudentScope', 'room').checked, true);
  assert.deepEqual(values(ui.$('examStudentRoom')), ['M-A']);
  assert.deepEqual(server.body('preflight').scope, { kind: 'room', slot_index: 5, room_code: 'M-A' });

  ui.choose('examStudentDay', 'Mon');
  await idle();
  assert.deepEqual(server.body('preflight').scope, { kind: 'day', day: 'Mon' });
  ui.radio('examStudentScope', 'period').click();
  await idle();
  assert.deepEqual(server.body('preflight').scope, { kind: 'period', slot_index: 5 });
  const everything = JSON.stringify(server.calls.map(call => call.options.body));
  assert.doesNotMatch(everything, /\d{7}/, 'no student ID ever leaves the page');
});

test('a timetable with no placed exam offers no period, and One period waits with its reason', async t => {
  const run = savedRun();
  run.schedule = run.schedule.map(entry => ({ ...entry, day: 'OVERFLOW', period: '', rooms: [] }));
  const server = exportServer();
  server.queue('preflight', json(matches({ choices: { ...choices(), periods: [], rooms: [] } })));
  const ui = await opened(t, { run, server });
  assert.deepEqual(labels(ui.$('examStudentPeriod')), [say('None', 'لا يوجد')]);
  const asked = server.requests('preflight').length;
  ui.radio('examStudentScope', 'period').click();
  await idle();
  assert.equal(ui.text('examStudentExportReason'), say('Complete the choice of exams.', 'أكمل اختيار الاختبارات.'));
  assert.equal(server.requests('preflight').length, asked, 'no period is never sent as the first one');
});

// ── Download ────────────────────────────────────────────────

test('Download posts the chosen options and saves the file under the name and reference the server gave', async t => {
  const server = exportServer();
  const ui = await opened(t, { server });
  ui.choose('examStudentSection', ui.$('examStudentSection').options[1].value);
  ui.radio('examStudentLanguage', 'en').click();
  ui.$('examStudentDates').querySelector('input[data-day="Sun"]').value = '2026-12-13';
  ui.emit(ui.$('examStudentDates').querySelector('input[data-day="Sun"]'), 'change');
  ui.$('examStudentPreparedFor').value = '  Head of department  ';
  await idle();
  assert.deepEqual(server.body('preflight').dates, { Sun: '2026-12-13' });
  const held = hold();
  server.queue('export', held.answer);
  ui.$('examStudentExportDownload').click();
  await idle();
  const [call] = server.requests('export');
  assert.equal(call.url, '/ops/exam-timetable/17/students/export/');
  assert.equal(call.options.method, 'POST');
  assert.equal(call.options.headers['X-CSRFToken'], 'test-csrf');
  assert.deepEqual(JSON.parse(call.options.body), {
    scope: { kind: 'section', exam: 'MATH101', section_key: 'term-section:2', gender: 'F' },
    programs: [], groups: ['M', 'F'], one_file_per_group: true, rows: 'all', contents: 'full', language: 'en',
    dates: { Sun: '2026-12-13' }, prepared_for: 'Head of department',
  });
  const download = ui.$('examStudentExportDownload');
  assert.equal(download.textContent, say('Preparing…', 'جارٍ التجهيز…'));
  assert.equal(download.getAttribute('aria-busy'), 'true');
  assert.equal(download.getAttribute('aria-disabled'), 'true');
  assert.ok(Array.from(ui.window.document.querySelectorAll('.et-export-step')).every(fieldset => fieldset.disabled));
  download.click();
  await idle();
  assert.equal(server.requests('export').length, 1, 'a second click while preparing sends nothing');

  held.release(file());
  await idle();
  assert.deepEqual(ui.downloads.map(item => item.saved), [{ href: 'blob:student-export-1', filename: 'exam_students_MATH101-M1_r17_en_7F3A2C1D.xlsx' }]);
  assert.equal(ui.text('examStudentExportStatus'), say(
    'Downloaded exam_students_MATH101-M1_r17_en_7F3A2C1D.xlsx · Ref EXR-7F3A2C1D',
    'تم تنزيل exam_students_MATH101-M1_r17_en_7F3A2C1D.xlsx · المرجع EXR-7F3A2C1D'));
  assert.deepEqual(Array.from(ui.$('examStudentExportStatus').querySelectorAll('bdi'), bdi => [bdi.textContent, bdi.dir]),
    [['exam_students_MATH101-M1_r17_en_7F3A2C1D.xlsx', 'ltr'], ['EXR-7F3A2C1D', 'ltr']]);
  assert.deepEqual(ui.toasts, [['success',
    say('Downloaded \u2066exam_students_MATH101-M1_r17_en_7F3A2C1D.xlsx\u2069', 'تم تنزيل \u2066exam_students_MATH101-M1_r17_en_7F3A2C1D.xlsx\u2069'),
    say('Ref \u2066EXR-7F3A2C1D\u2069', 'المرجع \u2066EXR-7F3A2C1D\u2069')]]);
  await pause(5);
  assert.deepEqual(ui.revoked, ['blob:student-export-1']);
  assert.equal(ui.window.document.querySelector('a[download]'), null);
  assert.equal(download.textContent, say('Download', 'تنزيل'));
  assert.equal(download.getAttribute('aria-disabled'), 'false');
  assert.ok(Array.from(ui.window.document.querySelectorAll('.et-export-step')).every(fieldset => !fieldset.disabled));

  server.queue('export', file('exam_students_all_r17_ar_0A1B2C3D.zip', 'EXR-0A1B2C3D', 'application/zip'));
  download.click();
  await idle();
  assert.equal(ui.downloads.at(-1).saved.filename, 'exam_students_all_r17_ar_0A1B2C3D.zip');
});

test('language, contents and the scope are remembered for this viewer, and unreadable storage changes nothing', async t => {
  const ui = await opened(t);
  ui.radio('examStudentLanguage', 'en').click();
  ui.radio('examStudentContents', 'summary').click();
  ui.radio('examStudentScope', 'day').click();
  await idle();
  assert.equal(ui.text('examStudentExportPrivacyText'), say(
    'Summaries only, with no student names or IDs. This export is recorded under your name.',
    'ملخصات فقط، دون أسماء الطلاب أو أرقامهم الجامعية. يُسجَّل هذا التصدير باسمك.'));
  assert.deepEqual(JSON.parse(ui.window.localStorage.getItem('exam-student-export')), { scope: 'day', contents: 'summary', language: 'en' });
  ui.$('examStudentExportCancel').click();
  ui.$('examStudentDataBtn').click();
  await idle();
  assert.equal(ui.radio('examStudentLanguage', 'en').checked, true);
  assert.equal(ui.radio('examStudentContents', 'summary').checked, true);
  assert.equal(ui.radio('examStudentScope', 'day').checked, true);
  assert.deepEqual(ui.server.body('preflight').scope, { kind: 'day', day: 'Sun' });

  const broken = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() {} };
  const blocked = await opened(t, { storage: broken });
  assert.equal(blocked.radio('examStudentLanguage', 'ar').checked, true);
  blocked.radio('examStudentLanguage', 'en').click();
  await idle();
  assert.equal(blocked.server.body('preflight').language, 'en');
});

// ── Errors ──────────────────────────────────────────────────

test('a failed download is said in the footer with Try again, and trying again downloads', async t => {
  const server = exportServer();
  server.queue('export', json({ ok: false, code: 'audit_unavailable', error: 'x' }, 503));
  const ui = await opened(t, { server });
  ui.$('examStudentExportDownload').click();
  await idle();
  assert.equal(ui.$('examStudentExportError').hidden, false);
  assert.equal(ui.$('examStudentExportError').getAttribute('role'), 'alert');
  assert.equal(ui.text('examStudentExportErrorText'), say("Couldn't record this export, so no file was made. Try again.", 'تعذّر تسجيل هذا التصدير، لذا لم يُنشأ أي ملف. أعد المحاولة.'));
  assert.equal(ui.$('examStudentExportRetry').hidden, false);
  assert.equal(ui.downloads.length, 0);
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'false');
  ui.$('examStudentExportRetry').click();
  await idle();
  assert.equal(server.requests('export').length, 2);
  assert.equal(ui.$('examStudentExportError').hidden, true);
  assert.equal(ui.downloads.length, 1);

  for (const [answer, message] of [
    [json({ ok: false, code: 'export_failed', error: 'x', reference: 'EXR-0A1B2C3D' }, 500),
      say('The file could not be made. Nothing was downloaded. Reference EXR-0A1B2C3D.', 'تعذّر إنشاء الملف ولم يُنزَّل شيء. المرجع EXR-0A1B2C3D.')],
    [json({ ok: false, code: 'export_slot_busy', error: 'x' }, 503), say('Another export is being prepared. Try again in a moment.', 'يجري تجهيز تصدير آخر. أعد المحاولة بعد قليل.')],
    [json({ ok: false, code: 'empty_scope', error: 'x' }, 400), say('No students match these choices.', 'لا يوجد طلاب مطابقون لهذه الاختيارات.')],
    [json({ ok: false, error: 'An uncoded server message' }, 500), 'An uncoded server message'],
    [json({ ok: false }, 500), say('The file could not be downloaded. Try again.', 'تعذّر تنزيل الملف. أعد المحاولة.')],
    [{ ok: true, status: 200, redirected: true, url: 'http://exam.test/login/', headers: { get: () => 'text/html' } },
      say('Your session expired. Sign in in a new tab, then retry. Your changes remain in this tab.', 'انتهت جلسة تسجيل الدخول. سجّل الدخول في تبويب جديد ثم أعد المحاولة. تغييراتك محفوظة في هذا التبويب.')],
    [async () => { throw new ui.window.TypeError('Failed to fetch'); }, say("Couldn't reach the server. Check your connection, then try again.", 'تعذّر الوصول إلى الخادم. تحقق من الاتصال ثم أعد المحاولة.')],
    [{ ok: true, status: 200, headers: { get: () => 'text/html; charset=utf-8' }, blob: async () => ({}), json: async () => { throw new SyntaxError('html'); } },
      say('The server returned an unexpected response. Retry the request. Your draft has not been replaced.', 'أعاد الخادم استجابة غير متوقعة. أعد المحاولة. لم تُستبدل مسودتك.')],
  ]) {
    server.queue('export', answer);
    ui.$('examStudentExportDownload').click();
    await idle();
    assert.equal(ui.text('examStudentExportErrorText'), message);
    assert.equal(ui.downloads.length, 1);
  }
});

test('a failed check keeps Download waiting and Try again asks again', async t => {
  const server = exportServer();
  server.queue('preflight', async () => { throw new server.window.TypeError('Failed to fetch'); });
  const ui = await opened(t, { server });
  assert.equal(ui.$('examStudentExportCheck').dataset.state, 'unchecked');
  assert.equal(ui.text('examStudentExportCheckTitle'), say('Student lists not checked yet', 'لم تُطابَق قوائم الطلاب بعد'));
  assert.equal(ui.text('examStudentExportErrorText'), say("Couldn't reach the server. Check your connection, then try again.", 'تعذّر الوصول إلى الخادم. تحقق من الاتصال ثم أعد المحاولة.'));
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'true');
  assert.equal(ui.text('examStudentExportReason'), say('The counts could not be updated. Try again.', 'تعذّر تحديث الأعداد. أعد المحاولة.'));
  ui.$('examStudentExportDownload').click();
  await idle();
  assert.equal(server.requests('export').length, 0);

  server.queue('preflight', json({ ok: false, code: 'invalid_options', field: 'dates', error: 'W1-Sun is not a Sunday' }, 400));
  ui.$('examStudentExportRetry').click();
  await idle();
  assert.equal(ui.text('examStudentExportErrorText'), say(
    "Check the exam dates: each date must fall on its day's weekday, and dates must follow the order of the days.",
    'راجع تواريخ الاختبارات: يجب أن يوافق كل تاريخ يوم الأسبوع في تسميته، وأن تتبع التواريخ ترتيب الأيام.'));
  server.queue('preflight', json({ ok: false, code: 'invalid_options', field: 'scope.exam', error: 'Choose an exam from this timetable.' }, 400));
  ui.$('examStudentExportRetry').click();
  await idle();
  assert.equal(ui.text('examStudentExportErrorText'), say(
    'The choices available for this timetable changed. Close this dialog and open it again.',
    'تغيّرت الاختيارات المتاحة في هذا الجدول. أغلق النافذة ثم افتحها مجدداً.'));
  ui.$('examStudentExportRetry').click();
  await idle();
  assert.equal(server.requests('preflight').length, 4);
  assert.equal(ui.$('examStudentExportError').hidden, true);
  assert.equal(ui.$('examStudentExportCheck').dataset.state, 'matches');
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'false');
});

// ── Closing ─────────────────────────────────────────────────

test('Cancel, the close button and Escape close the dialog, stop a download, and give focus back to Student data', async t => {
  const server = exportServer();
  const held = hold();
  server.queue('export', held.answer);
  const ui = await opened(t, { server, browserFocus: true });
  ui.$('examStudentExportDownload').click();
  await idle();
  ui.$('examStudentExportCancel').click();
  assert.equal(ui.$('examStudentExportDialog').open, false);
  assert.equal(server.requests('export')[0].options.signal.aborted, true);
  assert.equal(ui.window.document.activeElement, ui.$('examStudentDataBtn'));
  held.release(file());
  await idle();
  assert.equal(ui.downloads.length, 0, 'a stopped download never saves a file');

  ui.$('examStudentDataBtn').click();
  await idle();
  assert.equal(ui.window.document.activeElement, ui.$('examStudentExportTitle'));
  const escape = new ui.window.Event('cancel', { cancelable: true });
  ui.$('examStudentExportDialog').dispatchEvent(escape);
  assert.equal(escape.defaultPrevented, true);
  assert.equal(ui.$('examStudentExportDialog').open, false);
  assert.equal(ui.window.document.activeElement, ui.$('examStudentDataBtn'));

  ui.$('examStudentDataBtn').click();
  await idle();
  ui.$('examStudentExportClose').click();
  assert.equal(ui.$('examStudentExportDialog').open, false);
  assert.equal(ui.window.document.activeElement, ui.$('examStudentDataBtn'));
  assert.equal(ui.$('examStudentExportClose').getAttribute('aria-label'), say('Close', 'إغلاق'));
});

test('a timetable loaded under the open dialog stops it exporting the old one', async t => {
  const server = exportServer();
  const ui = await opened(t, { server, onRequest: async url => (url === '/ops/exam-timetable/18/' ? json({ ...savedRun(), run_id: 18 }) : undefined) });
  await ui.window.loadRun(18);
  await idle();
  ui.$('examStudentExportDownload').click();
  await idle();
  assert.equal(server.requests('export').length, 0);
  assert.equal(ui.$('examStudentExportDownload').getAttribute('aria-disabled'), 'true');
  assert.equal(ui.text('examStudentExportErrorText'), say(
    'The timetable changed or another operation started. Close this dialog, then check and save changes before exporting student data.',
    'تغيّر الجدول أو بدأت عملية أخرى. أغلق هذه النافذة، ثم افحص التغييرات واحفظها قبل تصدير بيانات الطلاب.'));
  assert.equal(ui.$('examStudentExportRetry').hidden, true);
});

// ── Language and direction ──────────────────────────────────

test('every sentence comes from the template in the page language, codes stay left-to-right, and nothing is dir=auto', async t => {
  const ui = await opened(t);
  const { document } = ui.window;
  assert.equal(document.documentElement.lang, language);
  assert.equal(document.documentElement.dir, AR ? 'rtl' : 'ltr');
  const dialog = ui.$('examStudentExportDialog');
  assert.equal(dialog.querySelectorAll('[dir="auto"]').length, 0);
  assert.equal(document.querySelectorAll('#examStudentExportCopy ~ * [dir="auto"]').length, 0);
  const isolatedTexts = Array.from(dialog.querySelectorAll('bdi[dir="ltr"]'), bdi => bdi.textContent);
  for (const code of ['17', '2026-09-23 21:10', 'L-3F9A2C1D0B', 'exam_students_all_r17_ar_M_….xlsx', 'CS2', '57']) {
    assert.ok(isolatedTexts.includes(code), `${code} is isolated left-to-right`);
  }
  // The label is the user's text: isolated, in its own direction.
  const label = Array.from(ui.$('examStudentExportSource').querySelectorAll('bdi')).find(bdi => bdi.textContent === 'Final v3');
  assert.equal(label.hasAttribute('dir'), false);
  const legends = Array.from(dialog.querySelectorAll('.et-export-step > legend'), legend => legend.textContent.trim());
  assert.deepEqual(legends, AR ? ['1أي الاختبارات؟', '2أي الطلاب؟', '3الملف'] : ['1Which exams?', '2Which students?', '3File']);
  const scripts = /[؀-ۿ]/;
  const visible = Array.from(dialog.querySelectorAll('legend, label, button, p, li'))
    .filter(node => !node.closest('[hidden]') && node.textContent.trim() && !node.closest('.et-segmented'));
  for (const node of visible) {
    const words = node.textContent.replace(/[A-Z]{2,}[\w()\- ]*|\d[\d,:.\- →+−]*|L-[0-9A-F]+|exam_students\S*|Excel|Final v3|Sun|Mon|Tue|Wed|Thu|M-[ABC]|F-A|[MF]\d/g, '').trim();
    if (!words.replace(/[·:.,()…“”«»#✓≠✕\s-]/g, '')) continue;
    assert.equal(scripts.test(words), AR, `"${node.textContent.trim()}" is in the page language`);
  }
  // The file-language choice names each language in itself.
  assert.deepEqual(Array.from(dialog.querySelectorAll('.et-segmented span[lang]'), span => [span.lang, span.dir, span.textContent]),
    [['ar', 'rtl', 'العربية'], ['en', 'ltr', 'English']]);
});

test('groups, rows and scopes are radio and checkbox groups whose legends and labels name them', async t => {
  const ui = await opened(t);
  const dialog = ui.$('examStudentExportDialog');
  const scopes = Array.from(dialog.querySelectorAll('input[name="examStudentScope"]'));
  assert.deepEqual(scopes.map(input => input.value), ['section', 'course', 'room', 'period', 'day', 'all']);
  assert.ok(scopes.every(input => input.closest('fieldset').querySelector('legend')));
  for (const id of ['examStudentSectionExam', 'examStudentSection', 'examStudentCourse', 'examStudentRoomPeriod', 'examStudentRoom', 'examStudentPeriod', 'examStudentDay']) {
    assert.ok(ui.$(id).getAttribute('aria-label'), `${id} has a name`);
  }
  const rows = Array.from(dialog.querySelectorAll('input[name="examStudentRows"]'));
  assert.equal(rows[0].closest('fieldset').querySelector('legend').textContent, say('Rows', 'الأسطر'));
  assert.deepEqual(rows.map(input => input.closest('label').textContent), say(
    ['Every student', 'Only students with a clash or same-day exam'],
    ['جميع الطلاب', 'الطلاب الذين لديهم تعارض أو اختبار آخر في اليوم نفسه فقط']));
  const groups = Array.from(dialog.querySelectorAll('input[name="examStudentGroup"]')).filter(input => !input.closest('[hidden]'));
  assert.deepEqual(groups.map(input => input.closest('label').textContent), say(['Male45 rows', 'Female12 rows'], ['طلابالأسطر: 45', 'طالباتالأسطر: 12']));
  assert.equal(ui.$('examStudentGroups').querySelector('legend').textContent, say('Groups', 'الفئات'));
});
