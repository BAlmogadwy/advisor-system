/*
 * Executed through tests/test_exam_frontend_interactions.py so both languages
 * use the current Django template. All DOM interactions execute the unmodified
 * production page script; only HTTP responses and global notifications are fake.
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
const language = process.env.EXAM_TEST_LANGUAGE;
const source = fs.readFileSync(path.join(__dirname, '../../static/js/page-exam-timetable.js'), 'utf8');
const sharedSource = fs.readFileSync(path.join(__dirname, '../../static/js/shared-utils.js'), 'utf8');
const dialogSource = fs.readFileSync(path.join(__dirname, '../../static/js/dialog.js'), 'utf8');
const reviewSource = fs.readFileSync(path.join(__dirname, '../../static/js/exam-review.js'), 'utf8');
const courses = [
  { course_code: 'CS111 (1)', source_course_code: 'CS111', course_name: 'Fundamentals of Programming', course_identity: 'cs111-fundamentals', enrolled_count: 31, credit_hours: 3, programs: ['AI'] },
  { course_code: 'CS111 (2)', source_course_code: 'CS111', course_name: 'Programming I', course_identity: 'cs111-programming', enrolled_count: 14, credit_hours: 3, programs: ['CS'], is_online: true },
];
const settle = () => new Promise(resolve => setImmediate(resolve));

function savedRun(periods = ['08:00-10:00', '10:30-12:30', '13:00-15:00']) {
  const slots = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'].flatMap(day =>
    periods.map(period => ({ day, period })));
  slots.forEach((slot, index) => { slot.index = index; });
  return {
    ok: true, run_id: 17, label: 'Saved regression fixture', primary_status: 'clean', status_flags: [], input_fingerprint: 'reviewed-inputs', enrollment_source: 'scraper_timetable',
    courses: courses.map(course => course.course_code), courses_count: 2, students_count: 45,
    schedule: courses.map((course, index) => ({ ...course, ...slots[index], slot_index: index, rooms: [] })),
    slots, qa: { max_per_day: 2 }, enrollment_scope: { programs: ['AI', 'CS'], sections: ['F', 'M'] },
    pinned: [{ course_code: courses[0].course_code, day: 'Sun', period: '08:00-10:00' }],
  };
}

// /jobs/active/, asked plainly or about an owed ending (?owed=<id>).
function isActive(url) {
  return String(url).split('?')[0] === '/ops/exam-timetable/jobs/active/';
}

async function page(t, { history = [], initialCourses = courses, run = null, loadCourses = true, onRequest = null, liveUpdate = true, realDialogs = false, committee = false, activeJob = { ok: true, job: null }, poll = {}, browserFocus = false } = {}) {
  const errors = [];
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(committee ? committeeTemplate : template, { url: 'http://exam.test/exam-timetable/', runScripts: 'outside-only', virtualConsole: console });
  const { window } = dom;
  window.localStorage.setItem('exam-timetable-live-update', liveUpdate ? 'on' : 'off');
  t.after(() => { dom.window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  const requests = [];
  const dialogs = [];
  const prompts = [];
  const notifications = [];
  const scrollCalls = [];
  let catalog = initialCourses;
  window.notify = { error: (...args) => notifications.push(args), success() {} };
  window.dlg = {
    confirm: async options => { dialogs.push(options); return false; },
    prompt: async options => { prompts.push(options); return false; },
  };
  window.HTMLElement.prototype.scrollIntoView = function (options) { scrollCalls.push({ element: this, options }); };
  if (browserFocus) {
    // Focus as a browser keeps it (jsdom does neither): a hidden element cannot
    // take it, and a focused element that is hidden drops it to the body.
    const hiddenNow = element => !element.isConnected || Boolean(element.closest('[hidden], .d-none'));
    const focus = window.HTMLElement.prototype.focus;
    window.HTMLElement.prototype.focus = function (...args) {
      if (hiddenNow(this)) return undefined;
      return focus.apply(this, args);
    };
    // Chromium's focus fixup is synchronous: read right after the hide,
    // activeElement is already the body.
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
    new window.MutationObserver(() => {
      const at = window.document.activeElement;
      if (at && at !== window.document.body && hiddenNow(at)) at.blur();
    }).observe(window.document, { attributes: true, subtree: true, childList: true, attributeFilter: ['hidden', 'class'] });
  }
  // jsdom reports a document as hidden unless told otherwise, and a hidden
  // tab polls nothing: a job would wait for ever. Visible, as in a browser.
  let hidden = false;
  Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => hidden });
  Object.defineProperty(window.document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
  const setHidden = value => { hidden = value; window.document.dispatchEvent(new window.Event('visibilitychange')); };
  window.fetch = async (url, options = {}) => {
    requests.push({ url, ...options });
    let data;
    if (onRequest) {
      const response = await onRequest(url, options);
      if (response !== undefined) return response;
    }
    if (url === '/ops/exam-timetable/filters/') data = { ok: true, programs: ['AI', 'CS'], sections: ['F', 'M'] };
    else if (url.startsWith('/ops/exam-timetable/list/')) data = { ok: true, runs: history };
    else if (run && url === `/ops/exam-timetable/${run.run_id}/`) data = run;
    else if (url === '/ops/exam-timetable/preview-courses/') data = { ok: true, courses: catalog };
    else if (url === '/ops/exam-timetable/draft-impact/') {
      const payload = JSON.parse(options.body);
      data = evaluatedRun(payload, run || savedRun());
    }
    else if (url === '/ops/exam-timetable/build/') data = { ok: false, error: 'Fixture ends at request validation' };
    else if (isActive(url)) data = activeJob;
    else {
      const error = new Error(`Unexpected HTTP request: ${url}`);
      errors.push(error);
      throw error;
    }
    return { ok: true, status: 200, json: async () => data };
  };
  vm.runInContext(sharedSource, dom.getInternalVMContext(), { filename: 'shared-utils.js' });
  if (realDialogs) {
    window.requestAnimationFrame = callback => window.setTimeout(callback, 0);
    vm.runInContext(dialogSource, dom.getInternalVMContext(), { filename: 'dialog.js' });
  }
  vm.runInContext(reviewSource, dom.getInternalVMContext(), { filename: 'exam-review.js' });
  // A job's polls wait nothing here; the page's own waits are for a real server.
  window.__examJobPoll = { first: 0, quick: 0, steady: 0, slow: 0, slowAfter: 0, announce: 0, reveal: 0, stall: 0, minShown: 0, backoff: [0, 0, 0, 0], timeout: 2000, checkRetry: 30, ...poll };
  vm.runInContext(`const LANGUAGE_CODE = ${JSON.stringify(language)};\n${source}`, dom.getInternalVMContext(), { filename: 'page-exam-timetable.js' });
  const $ = id => window.document.getElementById(id);
  const emit = (element, type) => element.dispatchEvent(new window.Event(type, { bubbles: true }));
  const input = (element, value) => { element.value = value; emit(element, 'input'); };
  const select = (id, value) => { $(id).value = value; emit($(id), 'change'); };
  const options = id => Array.from($(id).options, option => option.value).filter(Boolean);
  const rows = () => Array.from($('etPeriodsRepeater').querySelectorAll('.et-period-row'));
  const times = row => [row.querySelector('.et-period-start'), row.querySelector('.et-period-end')];
  const addPeriod = (start, end) => {
    $('etAddPeriod').click();
    const row = rows().at(-1);
    const [startInput, endInput] = times(row);
    input(startInput, start);
    input(endInput, end);
    return row;
  };
  await settle();
  input($('etLabel'), 'Interaction regression');
  if (loadCourses) {
    $('loadCoursesBtn').click();
    await settle();
    assert.equal($('courseList').querySelectorAll('input:checked').length, catalog.length);
  }
  return { window, $, emit, input, select, options, rows, times, addPeriod, requests, dialogs, prompts, notifications, scrollCalls, setHidden, setCourses(next) { catalog = next; } };
}

function evaluatedRun(payload, original = savedRun()) {
  const slots = payload.days.flatMap(day => payload.periods.map(period => ({ day, period })));
  slots.forEach((slot, index) => { slot.index = index; });
  return {
    ...original, run_id: undefined, editor_revision: payload.editor_revision,
    input_fingerprint: 'reviewed-inputs', slots,
    schedule: payload.base_schedule.map(entry => ({ ...entry, slot_index: slots.find(slot => slot.day === entry.day && slot.period === entry.period)?.index ?? slots.length, rooms: [] })),
    pinned: payload.pinned, courses_count: payload.base_schedule.length,
  };
}

function readyToPin(ui, period = '08:00-10:00') {
  ui.select('examPinCourse', courses[0].course_code);
  ui.select('examPinDay', 'Sun');
  ui.select('examPinPeriod', period);
}

const selectionCourses = [
  ...courses,
  { course_code: 'GS104', source_course_code: 'GS104', course_name: 'General Studies', course_identity: 'gs104-general', enrolled_count: 12, credit_hours: 2, programs: ['GS'] },
  { course_code: 'IS201', source_course_code: 'IS201', course_name: 'Information Systems', course_identity: 'is201-systems', enrolled_count: 20, credit_hours: 3, programs: ['IS'] },
];
const courseCodes = selectionCourses.map(course => course.course_code);
const visibleCourses = ui => Array.from(ui.$('courseList').querySelectorAll('.et-course-option'))
  .filter(row => !row.hidden).map(row => row.querySelector('input').value);
const selectedCourses = ui => Array.from(ui.$('courseList').querySelectorAll('input:checked'), input => input.value);
const isShown = element => !element.hidden && !element.closest('[hidden], .d-none');
const departmentCatalog = () => ({
  ok: true,
  departments: [
    { id: 'cs', name: language === 'ar' ? 'علوم الحاسب' : 'Computer Science', programs: ['CS', 'CS2'], course_count: 5, student_sittings: 91 },
    { id: 'ai-ds', name: language === 'ar' ? 'الذكاء الاصطناعي وعلوم البيانات' : 'Artificial Intelligence and Data Science', programs: ['AI', 'AI2', 'DS', 'DS2'], course_count: 8, student_sittings: 164 },
  ],
  days: ['Sun', 'Mon', 'Day 8'], genders: ['M', 'F', 'U'],
});
const departmentRequests = ui => ui.requests.filter(request => request.url.includes('/departments/export/'));
const selectedDepartments = ui => Array.from(ui.$('examDepartmentList').querySelectorAll('input:checked'), input => input.value);
const departmentResponse = (filename = 'exam-ai-ds.xlsx', options = {}) => ({
  ok: true, status: 200,
  headers: { get: name => ({ 'content-type': filename.endsWith('.zip') ? 'application/zip' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'content-disposition': `attachment; filename="${filename}"` })[name] || '' },
  blob: async () => ({ type: 'test-workbook' }), ...options,
});
function captureDepartmentDownloads(ui) {
  const downloads = [];
  const revoked = [];
  ui.window.URL.createObjectURL = () => 'blob:exam-departments';
  ui.window.URL.revokeObjectURL = url => revoked.push(url);
  ui.window.HTMLAnchorElement.prototype.click = function () { downloads.push({ href: this.href, filename: this.download }); };
  return { downloads, revoked };
}

test('department dialog defaults to combined AI/DS, Arabic files and all actual student groups with no invented dates', async t => {
  const ui = await loadedEditor(t, { onRequest: async url => url.includes('/departments/?') ? response(departmentCatalog()) : undefined });
  assert.ok(isShown(ui.$('departmentFilesBtn')));
  assert.equal(ui.$('departmentFilesBtn').getAttribute('aria-haspopup'), 'dialog');
  assert.match(ui.$('departmentFilesBtn').textContent, language === 'ar' ? /ملفات الأقسام/ : /Department files/);
  ui.$('departmentFilesBtn').click();
  await settle();
  assert.equal(ui.$('examDepartmentDialog').open, true);
  assert.equal(ui.$('examDepartmentDialog').getAttribute('aria-labelledby'), 'examDepartmentTitle');
  assert.deepEqual(selectedDepartments(ui), ['ai-ds']);
  assert.equal(ui.$('examDepartmentLanguage').value, 'ar');
  assert.deepEqual(Array.from(ui.$('examDepartmentGenders').querySelectorAll('input:checked'), input => input.value), ['M', 'F', 'U']);
  assert.match(ui.$('examDepartmentList').textContent, /AI, AI2, DS, DS2/);
  assert.match(ui.$('examDepartmentList').textContent, /8.*164/);
  assert.match(ui.$('examDepartmentGenders').textContent, language === 'ar' ? /غير مسجل/ : /Not recorded/);
  assert.equal(ui.$('examDepartmentDatesDetails').open, false);
  assert.deepEqual(Array.from(ui.$('examDepartmentDates').querySelectorAll('input'), input => [input.dataset.day, input.value]), [['Sun', ''], ['Mon', ''], ['Day 8', '']]);
  assert.equal(ui.$('examDepartmentAll').indeterminate, true);
  ui.$('examDepartmentAll').click();
  assert.deepEqual(selectedDepartments(ui), ['cs', 'ai-ds']);
  assert.equal(ui.$('examDepartmentAll').indeterminate, false);
  ui.$('examDepartmentAll').click();
  assert.deepEqual(selectedDepartments(ui), []);
  ui.$('closeExamDepartment').click();
  assert.equal(ui.$('examDepartmentDialog').open, false);
  assert.equal(ui.window.document.activeElement, ui.$('departmentFilesBtn'));
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx');
});

test('department dialog selects the first available department when AI/DS is absent', async t => {
  const data = departmentCatalog();
  data.departments = data.departments.slice(0, 1);
  data.genders = ['F'];
  const ui = await loadedEditor(t, { onRequest: async url => url.includes('/departments/?') ? response(data) : undefined });
  ui.$('departmentFilesBtn').click();
  await settle();
  assert.deepEqual(selectedDepartments(ui), ['cs']);
  assert.equal(ui.$('examDepartmentAll').checked, true);
  assert.deepEqual(Array.from(ui.$('examDepartmentGenders').querySelectorAll('input'), input => input.value), ['F']);
});

test('department files validate selection then download one workbook or a ZIP with explicit options and release blob URLs', async t => {
  const ui = await loadedEditor(t, { onRequest: async (url, options) => {
    if (url.includes('/departments/?')) return response(departmentCatalog());
    if (url.endsWith('/departments/export/')) return departmentResponse(JSON.parse(options.body).departments.length > 1 ? 'exam-departments.zip' : 'exam-ai-ds.xlsx');
  } });
  const { downloads, revoked } = captureDepartmentDownloads(ui);
  ui.$('departmentFilesBtn').click();
  await settle();
  ui.$('examDepartmentList').querySelector('input:checked').click();
  ui.$('downloadExamDepartments').click();
  assert.equal(departmentRequests(ui).length, 0);
  assert.match(ui.$('examDepartmentError').textContent, language === 'ar' ? /قسماً واحداً/ : /at least one department/);
  ui.$('examDepartmentList').querySelector('input[value="ai-ds"]').click();
  ui.$('examDepartmentGenders').querySelectorAll('input').forEach(input => input.click());
  ui.$('downloadExamDepartments').click();
  assert.equal(departmentRequests(ui).length, 0);
  assert.match(ui.$('examDepartmentError').textContent, language === 'ar' ? /فئة طلاب/ : /student group/);
  ui.$('examDepartmentGenders').querySelector('input[value="F"]').click();
  ui.$('examDepartmentLanguage').value = 'en';
  ui.$('examDepartmentDates').querySelector('input[data-day="Mon"]').value = '2026-12-21';
  ui.$('downloadExamDepartments').click();
  await settle();
  assert.deepEqual(JSON.parse(departmentRequests(ui)[0].body), { departments: ['ai-ds'], genders: ['F'], language: 'en', dates: { Mon: '2026-12-21' } });
  assert.equal(departmentRequests(ui)[0].headers['X-CSRFToken'], 'test-csrf');
  assert.deepEqual(downloads, [{ href: 'blob:exam-departments', filename: 'exam-ai-ds.xlsx' }]);
  assert.equal(ui.$('examDepartmentError').hidden, true);
  ui.$('examDepartmentAll').click();
  ui.$('downloadExamDepartments').click();
  await settle();
  assert.equal(downloads[1].filename, 'exam-departments.zip');
  await pause(1050);
  assert.deepEqual(revoked, ['blob:exam-departments', 'blob:exam-departments']);
  assert.equal(ui.window.document.querySelector('a[download]'), null);
});

test('department export blocks duplicate submissions, reports server errors and allows retry', async t => {
  let finish;
  let attempts = 0;
  const ui = await loadedEditor(t, { onRequest: async url => {
    if (url.includes('/departments/?')) return response(departmentCatalog());
    if (url.endsWith('/departments/export/')) {
      attempts++;
      if (attempts === 1) return new Promise(resolve => { finish = resolve; });
      return departmentResponse();
    }
  } });
  const { downloads } = captureDepartmentDownloads(ui);
  ui.$('departmentFilesBtn').click();
  await settle();
  ui.$('downloadExamDepartments').click();
  assert.equal(ui.$('downloadExamDepartments').disabled, true);
  assert.equal(ui.$('examDepartmentFields').disabled, true);
  ui.emit(ui.$('examDepartmentForm'), 'submit');
  assert.equal(attempts, 1);
  finish(response({ ok: false, error: 'Department data unavailable' }));
  await settle();
  assert.equal(ui.$('examDepartmentError').textContent, 'Department data unavailable');
  assert.equal(ui.$('downloadExamDepartments').disabled, false);
  assert.equal(downloads.length, 0);
  ui.$('downloadExamDepartments').click();
  await settle();
  assert.equal(downloads.length, 1);
});

test('older saved runs show localized Check and Save guidance when the operations snapshot is missing', async t => {
  const ui = await loadedEditor(t, { onRequest: async url => url.includes('/departments/?')
    ? response({ ok: false, code: 'operations_snapshot_required', error: 'English backend guidance' }) : undefined });
  assert.equal(ui.$('departmentFilesBtn').disabled, false);
  ui.$('departmentFilesBtn').click();
  await settle();
  assert.equal(ui.$('examDepartmentError').hidden, false);
  assert.match(ui.$('examDepartmentError').textContent, language === 'ar' ? /افحص التغييرات واحفظ/ : /Check changes and save/);
  assert.equal(ui.$('downloadExamDepartments').disabled, true);
  assert.equal(ui.$('examDepartmentFields').hidden, true);
});

test('department catalogue network errors stay visible and Escape restores focus', async t => {
  let ui;
  ui = await loadedEditor(t, { onRequest: async url => {
    if (url.includes('/departments/?')) throw new ui.window.TypeError('Network failure');
  } });
  ui.$('departmentFilesBtn').click();
  await settle();
  assert.match(ui.$('examDepartmentError').textContent, language === 'ar' ? /تحقق من الاتصال/ : /Check your connection/);
  const event = new ui.window.Event('cancel', { cancelable: true });
  ui.$('examDepartmentDialog').dispatchEvent(event);
  assert.equal(event.defaultPrevented, true);
  assert.equal(ui.$('examDepartmentDialog').open, false);
  assert.equal(ui.window.document.activeElement, ui.$('departmentFilesBtn'));
});

test('a catalogue response cannot enable exports after the editor changes', async t => {
  let finish;
  const ui = await loadedEditor(t, { onRequest: async url => url.includes('/departments/?')
    ? new Promise(resolve => { finish = resolve; }) : undefined });
  ui.$('departmentFilesBtn').click();
  ui.input(ui.$('etLabel'), 'New draft label');
  finish(response(departmentCatalog()));
  await settle();
  assert.equal(ui.$('examDepartmentFields').hidden, true);
  assert.equal(ui.$('downloadExamDepartments').disabled, true);
  assert.equal(ui.$('departmentFilesBtn').disabled, true);
  assert.match(ui.$('examDepartmentError').textContent, language === 'ar' ? /تغيّر الجدول/ : /timetable changed/);
  ui.emit(ui.$('examDepartmentForm'), 'submit');
  assert.equal(departmentRequests(ui).length, 0);
});

test('closing a loading department dialog discards its response and reopening starts a fresh request', async t => {
  const completions = [];
  const ui = await loadedEditor(t, { onRequest: async url => url.includes('/departments/?')
    ? new Promise(resolve => completions.push(resolve)) : undefined });
  ui.$('departmentFilesBtn').click();
  ui.$('closeExamDepartment').click();
  ui.$('departmentFilesBtn').click();
  assert.equal(completions.length, 2);
  completions[0](response(departmentCatalog()));
  await settle();
  assert.equal(ui.$('examDepartmentFields').hidden, true);
  assert.equal(ui.$('downloadExamDepartments').disabled, true);
  completions[1](response(departmentCatalog()));
  await settle();
  assert.equal(ui.$('downloadExamDepartments').disabled, false);
});

test('department downloads reject a redirected login page and preserve the chosen options for retry', async t => {
  const ui = await loadedEditor(t, { onRequest: async url => {
    if (url.includes('/departments/?')) return response(departmentCatalog());
    if (url.endsWith('/departments/export/')) return { ok: true, status: 200, redirected: true,
      url: 'http://exam.test/login/?next=/exam-timetable/', headers: { get: () => 'text/html' } };
  } });
  const { downloads } = captureDepartmentDownloads(ui);
  ui.$('departmentFilesBtn').click();
  await settle();
  ui.$('examDepartmentAll').click();
  ui.$('downloadExamDepartments').click();
  await settle();
  assert.equal(downloads.length, 0);
  assert.match(ui.$('examDepartmentError').textContent, language === 'ar' ? /انتهت جلسة تسجيل الدخول/ : /session expired/);
  assert.deepEqual(selectedDepartments(ui), ['cs', 'ai-ds']);
  assert.equal(ui.$('downloadExamDepartments').disabled, false);
});

test('department downloads are discarded when the loaded run changes during the request', async t => {
  let finish;
  const ui = await loadedEditor(t, { onRequest: async url => {
    if (url.includes('/departments/?')) return response(departmentCatalog());
    if (url.endsWith('/departments/export/')) return new Promise(resolve => { finish = resolve; });
    if (url === '/ops/exam-timetable/18/') return response({ ...savedRun(), run_id: 18 });
  } });
  const { downloads } = captureDepartmentDownloads(ui);
  ui.$('departmentFilesBtn').click();
  await settle();
  ui.$('downloadExamDepartments').click();
  await ui.window.loadRun(18);
  finish(departmentResponse());
  await settle();
  assert.equal(downloads.length, 0);
  assert.equal(ui.$('downloadExamDepartments').disabled, true);
  assert.match(ui.$('examDepartmentError').textContent, language === 'ar' ? /تغيّر الجدول/ : /timetable changed/);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/18/export.xlsx');
});

test('a draft edit during blob loading prevents an outdated department file download', async t => {
  let finishBlob;
  const ui = await loadedEditor(t, { onRequest: async url => {
    if (url.includes('/departments/?')) return response(departmentCatalog());
    if (url.endsWith('/departments/export/')) return departmentResponse('exam-ai-ds.xlsx', { blob: () => new Promise(resolve => { finishBlob = resolve; }) });
  } });
  const { downloads } = captureDepartmentDownloads(ui);
  ui.$('departmentFilesBtn').click();
  await settle();
  ui.$('downloadExamDepartments').click();
  await settle();
  dropExam(ui, 'Mon');
  finishBlob({ type: 'test-workbook' });
  await settle();
  assert.equal(downloads.length, 0);
  assert.equal(ui.$('downloadExamDepartments').disabled, true);
});

test('Check enables Save when only the department operations snapshot was added or changed', async t => {
  for (const existingSnapshot of [undefined, { schema_version: 1, rows: [{ program: 'AI', student_count: 1 }] }]) {
    const run = { ...savedRun(), pinned: [], operations_snapshot: existingSnapshot };
    const ui = await loadedEditor(t, { run, onRequest: async (url, options) => url === '/ops/exam-timetable/draft-impact/'
      ? response({ ...evaluatedRun(JSON.parse(options.body), run), operations_snapshot: { schema_version: 1, rows: [{ program: 'AI', student_count: 2 }] } }) : undefined });
    assert.equal(ui.$('saveLoadedBtn').disabled, true);
    ui.$('checkDraftBtn').click();
    await settle();
    assert.equal(ui.$('saveLoadedBtn').disabled, false);
    assert.equal(ui.$('departmentFilesBtn').disabled, true);
    assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  }
});
const displayedNumbers = element => Array.from(element.textContent.matchAll(/\d+/g), match => Number(match[0]));
const matchingPinCourses = ui => Array.from(ui.$('examPinCourseResults').querySelectorAll('[role="option"]'));
const searchPinCourses = (ui, query) => {
  ui.$('examPinCourseSearch').focus();
  ui.input(ui.$('examPinCourseSearch'), query);
  return matchingPinCourses(ui);
};
const pinSearchKey = (ui, key) => {
  const event = new ui.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  ui.$('examPinCourseSearch').dispatchEvent(event);
  return event;
};

test('the searchable course-to-pin control exposes a labeled combobox and associated listbox', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  const search = ui.$('examPinCourseSearch');
  assert.equal(search.getAttribute('role'), 'combobox');
  assert.equal(search.getAttribute('aria-autocomplete'), 'list');
  assert.equal(search.getAttribute('aria-controls'), 'examPinCourseResults');
  assert.ok(Array.from(search.labels, label => label.textContent).join(' ').trim());
  assert.equal(search.getAttribute('aria-expanded'), 'false');
  assert.equal(ui.$('examPinCourseResults').getAttribute('role'), 'listbox');
  assert.equal(ui.$('examPinCourse').hidden, true);
  assert.equal(ui.$('examPinCourseToggle').type, 'button');
  ui.$('examPinCourseToggle').click();
  assert.equal(search.getAttribute('aria-expanded'), 'true');
  assert.ok(isShown(ui.$('examPinCourseResults')));
  assert.equal(matchingPinCourses(ui).length, selectionCourses.length);
  assert.ok(displayedNumbers(ui.$('examPinCourseSearchStatus')).includes(selectionCourses.length));
  pinSearchKey(ui, 'Escape');
  assert.equal(search.getAttribute('aria-expanded'), 'false');
  assert.ok(!isShown(ui.$('examPinCourseResults')));
});

test('course-to-pin search accepts normalized codes and distinguishes same-code course names and plans', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  for (const query of ['cs111', 'CS 111', 'cs-111']) {
    const results = searchPinCourses(ui, query);
    assert.equal(results.length, 2);
    assert.ok(results.some(option => option.textContent.includes(courses[0].course_name)));
    assert.ok(results.some(option => option.textContent.includes(courses[1].course_name)));
    assert.ok(results.find(option => option.textContent.includes(courses[0].course_name)).textContent.includes('AI'));
    assert.equal(ui.$('examPinCourse').value, '', 'Matching text alone must never commit a course');
  }
  const nameMatches = searchPinCourses(ui, 'Fundamentals');
  assert.equal(nameMatches.length, 1);
  assert.ok(nameMatches[0].textContent.includes(courses[0].course_name));
  const planMatches = searchPinCourses(ui, 'AI');
  assert.equal(planMatches.length, 1);
  assert.ok(planMatches[0].textContent.includes(courses[0].course_name));
});

test('code-shaped pin searches do not combine a different course number with its program code', async t => {
  const programMatch = {
    course_code: 'GS111', source_course_code: 'GS111', course_name: 'University Skills',
    course_identity: 'gs111-skills', enrolled_count: 18, credit_hours: 2, programs: ['CS'],
  };
  // Put the program-only match first to prove ranking is based on the query,
  // rather than the order received from the server.
  const ui = await page(t, { initialCourses: [programMatch, ...courses] });
  const resultCodes = query => searchPinCourses(ui, query).map(option => option.dataset.value);
  for (const query of ['cs111', 'cs 111', 'CS-111']) {
    assert.deepEqual(resultCodes(query), [courses[0].course_code, courses[1].course_code]);
  }
  assert.deepEqual(resultCodes('CS'), [courses[0].course_code, courses[1].course_code, 'GS111']);
  assert.deepEqual(resultCodes('CS 111 (2)'), [courses[1].course_code]);
  assert.deepEqual(resultCodes('University Skills'), ['GS111']);
});

test('course-to-pin search treats numbered course names as names rather than unknown course codes', async t => {
  const numberedNames = [
    { course_code: 'MATH111', source_course_code: 'MATH111', course_name: 'Calculus 1', course_identity: 'math111-calculus', enrolled_count: 24, programs: ['CS'] },
    { course_code: 'MATH211', source_course_code: 'MATH211', course_name: 'Discrete Math 1', course_identity: 'math211-discrete', enrolled_count: 21, programs: ['CS'] },
  ];
  const ui = await page(t, { initialCourses: numberedNames });
  for (const course of numberedNames) {
    const results = searchPinCourses(ui, course.course_name);
    assert.deepEqual(results.map(option => option.dataset.value), [course.course_code]);
    assert.ok(results[0].textContent.includes(course.course_name));
  }
  assert.deepEqual(searchPinCourses(ui, 'MATH 111').map(option => option.dataset.value), ['MATH111']);
});

test('CS111 searches remain empty when only GS111 from the CS plan is eligible to pin', async t => {
  const programMatch = {
    course_code: 'GS111', source_course_code: 'GS111', course_name: 'University Skills',
    course_identity: 'gs111-skills', enrolled_count: 18, credit_hours: 2, programs: ['CS'],
  };
  const ui = await page(t, { initialCourses: [programMatch, ...courses] });
  for (const course of courses) {
    const checkbox = ui.$('courseList').querySelector(`input[value="${course.course_code}"]`);
    checkbox.checked = false;
    ui.emit(checkbox, 'change');
  }
  assert.deepEqual(ui.options('examPinCourse'), ['GS111']);
  for (const query of ['CS111', 'CS 111', 'CS-111']) {
    assert.deepEqual(searchPinCourses(ui, query), []);
    assert.equal(ui.$('examPinCourse').value, '');
    assert.equal(ui.$('applyExamPin').disabled, true);
  }
  assert.deepEqual(searchPinCourses(ui, 'CS').map(option => option.dataset.value), ['GS111'], 'A plain program-name search remains supported');
});

test('choosing search results pins distinct same-code courses to their own fixed times', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  for (const [course, period] of [[courses[0], '08:00-10:00'], [courses[1], '10:30-12:30']]) {
    const option = searchPinCourses(ui, 'cs111').find(option => option.textContent.includes(course.course_name));
    assert.ok(option);
    option.click();
    assert.equal(ui.$('examPinCourse').value, course.course_code);
    assert.ok(ui.$('examPinCourseSearch').value.includes(course.course_name));
    assert.equal(ui.$('examPinCourseSearch').getAttribute('aria-expanded'), 'false');
    ui.select('examPinDay', 'Sun');
    ui.select('examPinPeriod', period);
    ui.$('applyExamPin').click();
  }
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 2);
  ui.$('buildBtn').click();
  await settle();
  const payload = JSON.parse(ui.requests.find(request => request.url === '/ops/exam-timetable/build/').body);
  assert.deepEqual(payload.pinned, [
    { course_code: courses[0].course_code, day: 'Sun', period: '08:00-10:00' },
    { course_code: courses[1].course_code, day: 'Sun', period: '10:30-12:30' },
  ]);
});

test('typing an unmatched course clears the old value and survives day/period edits and Tab', async t => {
  const ui = await page(t);
  readyToPin(ui);
  assert.ok(ui.$('examPinCourseSearch').value.includes(courses[0].course_name), 'Hidden-select changes update the visible label');
  searchPinCourses(ui, 'unknown-course');
  assert.equal(ui.$('examPinCourse').value, '');
  assert.equal(ui.$('applyExamPin').disabled, true);
  assert.deepEqual(matchingPinCourses(ui), []);
  assert.ok(displayedNumbers(ui.$('examPinCourseSearchStatus')).includes(0));
  ui.select('examPinDay', 'Mon');
  ui.select('examPinPeriod', '10:30-12:30');
  assert.equal(ui.$('examPinCourseSearch').value, 'unknown-course');
  assert.equal(ui.$('examPinCourse').value, '');
  assert.equal(pinSearchKey(ui, 'Tab').defaultPrevented, false, 'Tab must retain normal keyboard navigation');
  assert.equal(ui.$('examPinCourseSearch').getAttribute('aria-expanded'), 'false');
  assert.equal(ui.$('examPinCourseSearch').value, 'unknown-course');
  assert.equal(ui.$('examPinCourse').value, '');
  assert.equal(ui.$('applyExamPin').disabled, true);
  ui.$('applyExamPin').click();
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 0);
});

test('unmatched blur never chooses the previous course or a first matching course', async t => {
  const ui = await page(t);
  readyToPin(ui);
  searchPinCourses(ui, 'cs111');
  assert.equal(matchingPinCourses(ui).length, 2);
  ui.$('examPinDay').focus();
  await settle();
  assert.equal(ui.$('examPinCourseSearch').getAttribute('aria-expanded'), 'false');
  assert.equal(ui.$('examPinCourseSearch').value, 'cs111');
  assert.equal(ui.$('examPinCourse').value, '');
  assert.equal(ui.$('applyExamPin').disabled, true);
  pinSearchKey(ui, 'Enter');
  assert.equal(ui.$('examPinCourse').value, '', 'Enter on a dismissed list must not implicitly commit a match');
});

test('Escape cancels a search and restores the previous confirmed course', async t => {
  const ui = await page(t);
  readyToPin(ui);
  searchPinCourses(ui, 'unknown-course');
  assert.equal(ui.$('examPinCourse').value, '');
  assert.equal(pinSearchKey(ui, 'Escape').defaultPrevented, true);
  assert.equal(ui.$('examPinCourse').value, courses[0].course_code);
  assert.ok(ui.$('examPinCourseSearch').value.includes(courses[0].course_name));
  assert.equal(ui.$('examPinCourseSearch').getAttribute('aria-expanded'), 'false');
  assert.ok(!ui.$('examPinCourseSearch').getAttribute('aria-activedescendant'));
  assert.equal(ui.$('applyExamPin').disabled, false);
});

test('course-search arrow navigation exposes an active option and Enter commits that exact course', async t => {
  const ui = await page(t);
  searchPinCourses(ui, 'cs111');
  for (const key of ['ArrowDown', 'ArrowUp', 'ArrowDown']) {
    assert.equal(pinSearchKey(ui, key).defaultPrevented, true);
    const activeId = ui.$('examPinCourseSearch').getAttribute('aria-activedescendant');
    const active = ui.window.document.getElementById(activeId);
    assert.equal(active?.getAttribute('role'), 'option');
    assert.ok(ui.$('examPinCourseResults').contains(active));
    assert.equal(ui.$('examPinCourse').value, '', 'Moving through results does not commit selection');
  }
  const active = ui.window.document.getElementById(ui.$('examPinCourseSearch').getAttribute('aria-activedescendant'));
  const expected = courses.find(course => active.textContent.includes(course.course_name));
  assert.ok(expected);
  assert.equal(pinSearchKey(ui, 'Enter').defaultPrevented, true);
  assert.equal(ui.$('examPinCourse').value, expected.course_code);
  assert.ok(ui.$('examPinCourseSearch').value.includes(expected.course_name));
  assert.equal(ui.$('examPinCourseSearch').getAttribute('aria-expanded'), 'false');
});

test('Escape restores canonical identity after renumbering and never restores a replacement sharing its old code', async t => {
  const programming = { ...courses[1], course_code: 'CS111' };
  const ui = await page(t, { initialCourses: [programming] });
  ui.select('examPinCourse', 'CS111');
  searchPinCourses(ui, 'unmatched');
  ui.setCourses(courses);
  ui.$('loadCoursesBtn').click();
  await settle();
  pinSearchKey(ui, 'Escape');
  assert.equal(ui.$('examPinCourse').value, courses[1].course_code);
  assert.ok(ui.$('examPinCourseSearch').value.includes(courses[1].course_name));

  searchPinCourses(ui, 'unmatched');
  ui.setCourses([{ ...courses[0], course_code: 'CS111' }]);
  ui.$('loadCoursesBtn').click();
  await settle();
  pinSearchKey(ui, 'Escape');
  assert.equal(ui.$('examPinCourse').value, '');
  assert.equal(ui.$('examPinCourseSearch').value, '');
});

test('course-to-pin results stay limited to selected courses and disable when none remain', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  searchPinCourses(ui, 'cs111');
  const deselect = code => {
    const checkbox = ui.$('courseList').querySelector(`input[value="${code}"]`);
    checkbox.checked = false;
    ui.emit(checkbox, 'change');
  };
  deselect(courses[0].course_code);
  const matches = matchingPinCourses(ui);
  assert.equal(matches.length, 1);
  assert.ok(matches[0].textContent.includes(courses[1].course_name));
  matches[0].click();
  assert.equal(ui.$('examPinCourse').value, courses[1].course_code);
  deselect(courses[1].course_code);
  assert.equal(ui.$('examPinCourse').value, '');
  assert.equal(ui.$('examPinCourseSearch').value, '');
  assert.deepEqual(searchPinCourses(ui, 'cs111'), []);
  deselect('GS104');
  deselect('IS201');
  assert.equal(ui.$('examPinCourseSearch').disabled, true);
  assert.equal(ui.$('examPinCourseToggle').disabled, true);
  assert.equal(ui.$('examPinCourseSearch').getAttribute('aria-expanded'), 'false');
  assert.equal(ui.$('applyExamPin').disabled, true);
});

test('course search matches code, name and plan with case-insensitive AND tokens without changing selection', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  for (const [query, expected] of [
    ['  cs111  ', [courses[0].course_code, courses[1].course_code]],
    [' PROGRAMMING   fundamentals ', [courses[0].course_code]],
    [' ai ', [courses[0].course_code]],
  ]) {
    ui.input(ui.$('courseSearch'), query);
    assert.deepEqual(visibleCourses(ui), expected);
    assert.deepEqual(selectedCourses(ui), courseCodes);
    assert.equal(ui.$('courseList').querySelectorAll('.et-course-option').length, 4);
    assert.deepEqual(displayedNumbers(ui.$('courseSearchCount')), [expected.length, 4]);
    assert.deepEqual(displayedNumbers(ui.$('courseCount')), [4, 4]);
  }
});

test('visibility and text search combine without clearing selections when nothing matches', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  ui.select('courseVisibility', 'online');
  assert.deepEqual(visibleCourses(ui), [courses[1].course_code]);
  ui.input(ui.$('courseSearch'), 'not-a-course');
  assert.deepEqual(visibleCourses(ui), []);
  assert.ok(isShown(ui.$('courseNoMatches')));
  assert.ok(!isShown(ui.$('courseEmptyState')));
  assert.deepEqual(selectedCourses(ui), courseCodes);
  ui.input(ui.$('courseSearch'), '');
  assert.deepEqual(visibleCourses(ui), [courses[1].course_code]);
  assert.ok(!isShown(ui.$('courseNoMatches')));
  ui.select('courseVisibility', 'all');
  assert.deepEqual(visibleCourses(ui), courseCodes);
});

test('visible-only bulk actions preserve hidden courses and both canonical same-code identities', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  ui.input(ui.$('courseSearch'), '(2)');
  assert.deepEqual(visibleCourses(ui), [courses[1].course_code]);
  ui.$('deselectVisibleCourses').click();
  const remaining = courseCodes.filter(code => code !== courses[1].course_code);
  assert.deepEqual(selectedCourses(ui), remaining);
  assert.deepEqual(ui.options('examPinCourse'), remaining);
  assert.deepEqual(displayedNumbers(ui.$('courseCount')), [3, 4]);
  ui.$('selectVisibleCourses').click();
  assert.deepEqual(selectedCourses(ui), courseCodes);
  assert.deepEqual(ui.options('examPinCourse'), courseCodes);
  ui.$('buildBtn').click();
  await settle();
  const payload = JSON.parse(ui.requests.find(request => request.url === '/ops/exam-timetable/build/').body);
  assert.deepEqual(payload.selected_courses, courseCodes);
  assert.deepEqual(payload.selected_course_entries.map(entry => entry.course_identity), selectionCourses.map(course => course.course_identity));
});

test('bulk changes in selected/unselected views process every currently visible course once', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  for (const code of [courses[0].course_code, 'GS104']) {
    const checkbox = ui.$('courseList').querySelector(`input[value="${code}"]`);
    checkbox.checked = false;
    ui.emit(checkbox, 'change');
  }
  ui.select('courseVisibility', 'unselected');
  assert.deepEqual(visibleCourses(ui), [courses[0].course_code, 'GS104']);
  ui.$('selectVisibleCourses').click();
  assert.deepEqual(selectedCourses(ui), courseCodes);
  assert.deepEqual(visibleCourses(ui), []);
  assert.ok(isShown(ui.$('courseNoMatches')));
  ui.select('courseVisibility', 'selected');
  assert.deepEqual(visibleCourses(ui), courseCodes);
  ui.$('deselectVisibleCourses').click();
  assert.deepEqual(selectedCourses(ui), []);
  assert.deepEqual(visibleCourses(ui), []);
  assert.deepEqual(ui.options('examPinCourse'), []);
});

for (const visibility of ['selected', 'unselected']) {
  test(`activating a focused checkbox in the ${visibility} view keeps focus on a visible control`, async t => {
    const ui = await page(t, { initialCourses: selectionCourses });
    if (visibility === 'unselected') ui.$('toggleAllCourses').click();
    ui.select('courseVisibility', visibility);
    const checkbox = code => ui.$('courseList').querySelector(`input[value="${code}"]`);
    const activate = code => {
      const input = checkbox(code);
      input.focus();
      assert.equal(ui.window.document.activeElement, input);
      // A focused checkbox's activation toggles it and emits change. jsdom
      // does not perform a browser's default Space-key activation, so click
      // exercises the same native checkbox activation/change behavior here.
      input.click();
      assert.equal(input.closest('.et-course-option').hidden, true);
    };

    activate(courses[0].course_code);
    assert.equal(ui.window.document.activeElement, checkbox(courses[1].course_code), 'Focus advances to the next visible row');
    activate('IS201');
    assert.equal(ui.window.document.activeElement, checkbox('GS104'), 'At the end of the list, focus moves to the previous visible row');
    activate('GS104');
    assert.equal(ui.window.document.activeElement, checkbox(courses[1].course_code));
    activate(courses[1].course_code);
    assert.deepEqual(visibleCourses(ui), []);
    assert.equal(ui.window.document.activeElement, ui.$('courseVisibility'), 'When no rows remain, focus returns to the visibility filter');
  });
}

test('global presets act on the full course set even while search hides most rows', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  ui.input(ui.$('courseSearch'), 'GS104');
  ui.$('toggleAllCourses').click();
  assert.deepEqual(selectedCourses(ui), []);
  ui.$('toggleAllCourses').click();
  assert.deepEqual(selectedCourses(ui), courseCodes);
  ui.$('selectComputerCourses').click();
  assert.deepEqual(selectedCourses(ui), [courses[0].course_code, courses[1].course_code, 'IS201']);
  ui.$('selectGeneralCourses').click();
  assert.deepEqual(selectedCourses(ui), ['GS104']);
  ui.$('toggleOnlineCourses').click();
  assert.deepEqual(selectedCourses(ui), [courses[1].course_code, 'GS104']);
  ui.$('toggleOnlineCourses').click();
  assert.deepEqual(selectedCourses(ui), ['GS104']);
});

test('hiding a pinned course in search does not remove its choice or invalidate its pin', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  readyToPin(ui);
  ui.$('applyExamPin').click();
  ui.input(ui.$('courseSearch'), 'GS104');
  assert.deepEqual(visibleCourses(ui), ['GS104']);
  assert.equal(ui.$('examPinCourse').value, courses[0].course_code);
  assert.deepEqual(ui.options('examPinCourse'), courseCodes);
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 1);
  assert.equal(ui.$('examPinRows').querySelector('.et-pin-invalid'), null);
});

test('the initial and changed-scope empty state is distinct from a search with no matches', async t => {
  const ui = await page(t, { initialCourses: selectionCourses, loadCourses: false });
  const initialSummary = ui.$('examBuildSummary').textContent;
  assert.match(initialSummary, language === 'ar' ? /حمّل المقررات/ : /Load courses/);
  assert.ok(isShown(ui.$('courseEmptyState')));
  assert.ok(!isShown(ui.$('courseNoMatches')));
  assert.equal(ui.$('buildBtn').disabled, true);
  ui.$('loadCoursesBtn').click();
  await settle();
  assert.ok(!isShown(ui.$('courseEmptyState')));
  ui.input(ui.$('courseSearch'), 'not-a-course');
  assert.ok(isShown(ui.$('courseNoMatches')));
  const program = ui.$('progList').querySelector('input');
  program.checked = false;
  ui.emit(program, 'change');
  assert.ok(isShown(ui.$('courseEmptyState')));
  assert.ok(!isShown(ui.$('courseNoMatches')));
  assert.equal(ui.$('courseList').querySelectorAll('input').length, 0);
  assert.equal(ui.$('buildBtn').disabled, true);
  assert.equal(ui.$('examBuildSummary').textContent, initialSummary);
});

test('changing scope after a zero-course response resets the preview for another load', async t => {
  const ui = await page(t, { initialCourses: [] });
  const program = ui.$('progList').querySelector('input');
  program.checked = false;
  ui.emit(program, 'change');
  assert.ok(isShown(ui.$('courseEmptyState')));
  assert.ok(!isShown(ui.$('courseNoMatches')));
  assert.ok(ui.$('coursePreview').classList.contains('d-none'));
  assert.equal(ui.$('courseList').querySelectorAll('input').length, 0);
  assert.equal(ui.$('buildBtn').disabled, true);
});

test('course search, visibility and bulk controls are labeled keyboard-operable controls', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  for (const id of ['courseSearch', 'courseVisibility']) {
    const control = ui.$(id);
    const name = control.getAttribute('aria-label') || Array.from(control.labels || [], label => label.textContent).join(' ');
    assert.ok(name.trim(), `${id} needs an accessible name`);
    assert.ok(control.tabIndex >= 0);
  }
  for (const id of ['selectVisibleCourses', 'deselectVisibleCourses', 'toggleAllCourses', 'selectComputerCourses', 'selectGeneralCourses', 'toggleOnlineCourses']) {
    const control = ui.$(id);
    assert.equal(control.tagName, 'BUTTON');
    assert.equal(control.type, 'button');
    assert.ok(control.textContent.trim());
  }
  for (const course of selectionCourses) {
    const input = ui.$('courseList').querySelector(`input[value="${course.course_code}"]`);
    const name = Array.from(input.labels, label => label.textContent).join(' ');
    assert.ok(name.includes(course.course_code));
    assert.ok(name.includes(course.course_name), 'The accessible name must distinguish same-code courses');
  }
});

test('build summary reflects all selections, valid settings and pins independently of course search', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  assert.deepEqual(displayedNumbers(ui.$('examBuildSummary')), [4, 5, 3, 0]);
  readyToPin(ui);
  ui.$('applyExamPin').click();
  assert.deepEqual(displayedNumbers(ui.$('examBuildSummary')), [4, 5, 3, 1]);
  ui.input(ui.$('etNumDays'), '6');
  ui.addPeriod('16', '17');
  assert.deepEqual(displayedNumbers(ui.$('examBuildSummary')), [4, 6, 4, 1]);
  ui.input(ui.$('courseSearch'), 'GS104');
  assert.deepEqual(displayedNumbers(ui.$('examBuildSummary')), [4, 6, 4, 1]);
  ui.$('deselectVisibleCourses').click();
  assert.deepEqual(displayedNumbers(ui.$('examBuildSummary')), [3, 6, 4, 1]);
  ui.$('clearExamPins').click();
  assert.deepEqual(displayedNumbers(ui.$('examBuildSummary')), [3, 6, 4, 0]);
});

test('added full-clock period reaches pin dropdown on input, before change or blur', async t => {
  const ui = await page(t);
  readyToPin(ui);
  ui.addPeriod('16:00', '17:00');
  assert.deepEqual(ui.options('examPinPeriod'), ['08:00-10:00', '10:30-12:30', '13:00-15:00', '16:00-17:00']);
  assert.equal(ui.$('examPinPeriod').value, '08:00-10:00', 'A valid existing selection is retained');
  assert.equal(ui.$('applyExamPin').disabled, false);
});

test('the reported bare-hour entry is available immediately and normalizes when committed', async t => {
  const ui = await page(t);
  const row = ui.addPeriod('16', '17');
  assert.ok(ui.options('examPinPeriod').includes('16:00-17:00'));
  const [start, end] = ui.times(row);
  // Browsers dispatch change when an edited text input loses focus. jsdom
  // cannot infer a user edit from assigning value, so dispatch that commit.
  ui.emit(start, 'change');
  ui.emit(end, 'change');
  assert.deepEqual([start.value, end.value], ['16:00', '17:00']);
  readyToPin(ui, '16:00-17:00');
  ui.$('applyExamPin').click();
  assert.match(ui.$('examPinRows').textContent, /16:00-17:00/);
  ui.$('buildBtn').click();
  await settle();
  const build = ui.requests.find(request => request.url === '/ops/exam-timetable/build/');
  assert.ok(build, 'A valid new period can be submitted');
  const payload = JSON.parse(build.body);
  assert.ok(payload.periods.includes('16:00-17:00'));
  assert.deepEqual(payload.pinned, [{ course_code: 'CS111 (1)', day: 'Sun', period: '16:00-17:00' }]);
});

test('editing an existing row synchronizes options on input and removes a stale selection', async t => {
  const ui = await page(t);
  readyToPin(ui, '13:00-15:00');
  const [start, end] = ui.times(ui.rows()[2]);
  ui.input(start, '14');
  ui.input(end, '16');
  assert.ok(ui.options('examPinPeriod').includes('14:00-16:00'));
  assert.ok(!ui.options('examPinPeriod').includes('13:00-15:00'));
  assert.equal(ui.$('examPinPeriod').value, '');
  assert.equal(ui.$('applyExamPin').disabled, true);
  ui.select('examPinPeriod', '14:00-16:00');
  assert.equal(ui.$('applyExamPin').disabled, false);
});

test('removing a period refreshes options and retains an unaffected choice', async t => {
  const ui = await page(t);
  readyToPin(ui);
  const added = ui.addPeriod('16', '17');
  added.querySelector('.et-period-remove').click();
  assert.ok(!ui.options('examPinPeriod').includes('16:00-17:00'));
  assert.equal(ui.$('examPinPeriod').value, '08:00-10:00');
  ui.rows()[0].querySelector('.et-period-remove').click();
  assert.equal(ui.$('examPinPeriod').value, '');
  assert.equal(ui.$('applyExamPin').disabled, true);
});

for (const [name, start, end] of [
  ['incomplete', '16', ''], ['out-of-range hour', '25', '26'],
  ['incomplete minutes', '16:', '18'], ['single-digit minute', '16:3', '18'],
  ['out-of-range minute', '16:70', '18'], ['reversed', '18', '17'],
  ['zero duration', '16', '16'], ['overlapping', '09', '11'],
  ['duplicate', '08', '10'],
]) {
  test(`${name} periods report a field error and prevent pinning/building`, async t => {
    const ui = await page(t);
    readyToPin(ui);
    ui.addPeriod(start, end);
    assert.equal(ui.$('applyExamPin').disabled, true);
    assert.ok(ui.$('etPeriodsRepeater').querySelector('[aria-invalid="true"]'), 'The invalid input has an accessible error state');
    ui.$('buildBtn').click();
    await settle();
    assert.ok(!ui.requests.some(request => request.url === '/ops/exam-timetable/build/'));
  });
}

test('adjacent periods are valid and empty period configuration cannot be pinned', async t => {
  const ui = await page(t);
  ui.addPeriod('15', '16');
  readyToPin(ui, '15:00-16:00');
  assert.equal(ui.$('applyExamPin').disabled, false);
  for (const row of ui.rows()) row.querySelector('.et-period-remove').click();
  assert.deepEqual(ui.options('examPinPeriod'), []);
  assert.equal(ui.$('examPinPeriod').disabled, true);
  assert.equal(ui.$('applyExamPin').disabled, true);
});

test('day edits refresh choices immediately and retain incompatible pins for review', async t => {
  const ui = await page(t);
  readyToPin(ui);
  ui.select('examPinDay', 'Thu');
  ui.$('applyExamPin').click();
  ui.input(ui.$('etNumDays'), '2');
  assert.deepEqual(ui.options('examPinDay'), ['Sun', 'Mon']);
  assert.equal(ui.$('examPinDay').value, '');
  assert.ok(ui.$('examPinRows').querySelector('.et-pin-invalid'));
  assert.match(ui.$('examPinRows').textContent, /Thu/);
  ui.$('buildBtn').click();
  await settle();
  assert.ok(!ui.requests.some(request => request.url === '/ops/exam-timetable/build/'));
  ui.input(ui.$('etNumDays'), '5');
  assert.equal(ui.$('examPinRows').querySelector('.et-pin-invalid'), null);
  ui.select('etStartDay', 'Mon');
  assert.deepEqual(ui.options('examPinDay'), ['Mon', 'Tue', 'Wed', 'Thu', 'Sun']);
});

test('period edits retain invalidated saved pins and allow correcting the configuration', async t => {
  const ui = await page(t);
  readyToPin(ui);
  ui.$('applyExamPin').click();
  const [start] = ui.times(ui.rows()[0]);
  ui.input(start, '08:30');
  assert.ok(ui.$('examPinRows').querySelector('.et-pin-invalid'));
  assert.match(ui.$('examPinRows').textContent, /08:00-10:00/);
  ui.input(start, '8');
  assert.equal(ui.$('examPinRows').querySelector('.et-pin-invalid'), null);
});

test('crossing the week-label boundary preserves existing pins and selected days', async t => {
  const ui = await page(t);
  readyToPin(ui);
  ui.$('applyExamPin').click();
  ui.input(ui.$('etNumDays'), '6');
  assert.equal(ui.$('examPinDay').value, 'W1-Sun');
  assert.match(ui.$('examPinRows').textContent, /W1-Sun/);
  assert.equal(ui.$('examPinRows').querySelector('.et-pin-invalid'), null);
  ui.input(ui.$('etNumDays'), '5');
  assert.equal(ui.$('examPinDay').value, 'Sun');
  const cells = ui.$('examPinRows').querySelector('tr').cells;
  assert.equal(cells[1].textContent.trim(), 'Sun');
  assert.equal(ui.$('examPinRows').querySelector('.et-pin-invalid'), null);

  ui.select('examPinDay', 'Thu');
  ui.input(ui.$('etNumDays'), '6');
  assert.equal(ui.$('examPinDay').value, 'W1-Thu', 'A pending unsubmitted day choice is also preserved');
  ui.input(ui.$('etNumDays'), '5');
  assert.equal(ui.$('examPinDay').value, 'Thu');
});

test('temporarily clearing the day count does not erase the meaning of a pin', async t => {
  const ui = await page(t);
  readyToPin(ui);
  ui.$('applyExamPin').click();
  ui.input(ui.$('etNumDays'), '');
  assert.ok(ui.$('examPinRows').querySelector('.et-pin-invalid'));
  assert.equal(ui.$('applyExamPin').disabled, true);
  assert.equal(ui.$('examPinDay').value, '');
  ui.input(ui.$('etNumDays'), '6');
  assert.match(ui.$('examPinRows').textContent, /W1-Sun/);
  assert.equal(ui.$('examPinRows').querySelector('.et-pin-invalid'), null);
  assert.equal(ui.$('examPinDay').value, 'W1-Sun', 'A temporary empty count must not erase the pending choice');
});

test('changing the start day does not silently move a first-week pin into the next week', async t => {
  const ui = await page(t);
  readyToPin(ui);
  ui.$('applyExamPin').click();
  ui.select('etStartDay', 'Mon');
  assert.ok(ui.options('examPinDay').includes('Sun'), 'The last Sunday now belongs to week 2');
  assert.equal(ui.$('examPinDay').value, '', 'The old Sunday choice must not become the following Sunday');
  assert.ok(ui.$('examPinRows').querySelector('.et-pin-invalid'), 'The removed week-1 Sunday must remain unresolved');
  ui.$('buildBtn').click();
  await settle();
  assert.ok(!ui.requests.some(request => request.url === '/ops/exam-timetable/build/'));
  ui.select('etStartDay', 'Sun');
  assert.equal(ui.$('examPinRows').querySelector('.et-pin-invalid'), null);
});

for (const pinned of [true, false]) {
  test(`loaded-run week-label edits block stale Save/Optimize placements (${pinned ? 'with' : 'without'} pins)`, async t => {
    const run = savedRun();
    if (!pinned) run.pinned = [];
    const ui = await page(t, { run, history: [{ id: run.run_id, label: run.label }] });
    ui.$('historyList').querySelector('.et-run-info').click();
    await settle();
    assert.equal(ui.$('kCourses').textContent, '2');
    assert.ok(ui.$('exportXlsx').getAttribute('href'));
    ui.input(ui.$('etNumDays'), '6');
    assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
    for (const id of ['saveLoadedBtn', 'optimizeLoadedBtn', 'minChangeBtn']) {
      ui.$(id).click();
      await settle();
      assert.ok(!ui.requests.some(request => request.url === '/ops/exam-timetable/build/'));
      assert.ok(ui.$('etStatus').classList.contains('alert-warning'), 'A blocked action must explain the required action');
    }
  });
}

test('extending a loaded timetable without renaming its used days remains saveable', async t => {
  const run = savedRun();
  for (const slot of run.slots) slot.day = `W1-${slot.day}`;
  run.slots.push(...['08:00-10:00', '10:30-12:30', '13:00-15:00'].map((period, index) => ({ index: 15 + index, day: 'W2-Sun', period })));
  for (const entry of [...run.schedule, ...run.pinned]) entry.day = `W1-${entry.day}`;
  const ui = await page(t, { run, history: [{ id: run.run_id, label: run.label }] });
  ui.$('historyList').querySelector('.et-run-info').click();
  await settle();
  assert.equal(ui.$('etNumDays').value, '6');
  ui.input(ui.$('etNumDays'), '7');
  ui.$('saveLoadedBtn').click();
  await settle();
  const request = ui.requests.find(request => request.url === '/ops/exam-timetable/build/');
  assert.ok(request);
  const payload = JSON.parse(request.body);
  assert.equal(payload.mode, 'save_loaded_changes');
  for (const placement of [...payload.base_schedule, ...payload.pinned]) {
    assert.ok(payload.days.includes(placement.day));
  }
  assert.equal(payload.pinned[0].day, 'W1-Sun');
});

test('removing a used period blocks loaded Save and Optimize with recovery guidance', async t => {
  const run = savedRun();
  run.pinned = [];
  const ui = await page(t, { run, history: [{ id: run.run_id, label: run.label }] });
  ui.$('historyList').querySelector('.et-run-info').click();
  await settle();
  ui.rows()[0].querySelector('.et-period-remove').click();
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  for (const id of ['saveLoadedBtn', 'optimizeLoadedBtn', 'minChangeBtn']) {
    ui.$(id).click();
    await settle();
    assert.ok(!ui.requests.some(request => request.url === '/ops/exam-timetable/build/'));
    assert.ok(ui.$('etStatus').classList.contains('alert-warning'));
    assert.match(ui.$('etStatus').textContent, language === 'ar' ? /حمّل المقررات/ : /Load Courses/);
  }
});

test('adding a period to a loaded timetable retains valid placements in the save request', async t => {
  const run = savedRun();
  run.pinned = [];
  const ui = await page(t, { run, history: [{ id: run.run_id, label: run.label }] });
  ui.$('historyList').querySelector('.et-run-info').click();
  await settle();
  ui.addPeriod('16', '17');
  assert.ok(ui.options('examPinPeriod').includes('16:00-17:00'));
  ui.$('saveLoadedBtn').click();
  await settle();
  const request = ui.requests.find(request => request.url === '/ops/exam-timetable/build/');
  assert.ok(request);
  const payload = JSON.parse(request.body);
  assert.equal(payload.mode, 'save_loaded_changes');
  assert.ok(payload.periods.includes('16:00-17:00'));
  assert.equal(payload.base_schedule.length, run.schedule.length);
  for (const placement of payload.base_schedule) {
    assert.ok(payload.days.includes(placement.day));
    assert.ok(payload.periods.includes(placement.period));
  }
});

test('a deselected course on a removed day does not block remaining valid placements', async t => {
  const run = savedRun();
  run.pinned = [];
  run.schedule[1] = { ...run.schedule[1], ...run.slots[3], slot_index: 3 };
  const ui = await page(t, { run, history: [{ id: run.run_id, label: run.label }] });
  ui.$('historyList').querySelector('.et-run-info').click();
  await settle();
  // The first course's W1 Sunday is removed; Monday remains the same date.
  ui.select('etStartDay', 'Mon');
  ui.input(ui.$('etNumDays'), '4');
  const checkbox = ui.$('courseList').querySelector(`input[value="${courses[0].course_code}"]`);
  checkbox.checked = false;
  ui.emit(checkbox, 'change');
  ui.$('saveLoadedBtn').click();
  await settle();
  const request = ui.requests.find(request => request.url === '/ops/exam-timetable/build/');
  assert.ok(request);
  const payload = JSON.parse(request.body);
  assert.deepEqual(payload.selected_courses, [courses[1].course_code]);
  assert.equal(payload.base_schedule.length, 1);
  assert.equal(payload.base_schedule[0].day, 'Mon');
  assert.ok(payload.days.includes(payload.base_schedule[0].day));
});

test('leaving a period field normalizes it even without a change event', async t => {
  const ui = await page(t);
  const [start, end] = ui.times(ui.addPeriod('16', '17'));
  assert.ok(ui.options('examPinPeriod').includes('16:00-17:00'));
  ui.emit(start, 'focusout');
  ui.emit(end, 'focusout');
  assert.deepEqual([start.value, end.value], ['16:00', '17:00']);
});

test('loading a four-period timetable preserves a pending choice of its fourth period', async t => {
  const run = savedRun(['08:00-10:00', '10:30-12:30', '13:00-15:00', '16:00-17:00']);
  run.pinned = [];
  const ui = await page(t, { run, history: [{ id: run.run_id, label: run.label }] });
  ui.addPeriod('16', '17');
  readyToPin(ui, '16:00-17:00');
  ui.$('historyList').querySelector('.et-run-info').click();
  await settle();
  assert.equal(ui.rows().length, 4);
  assert.equal(ui.$('examPinPeriod').value, '16:00-17:00');
  assert.equal(ui.$('applyExamPin').disabled, false);
});

test('Arabic hour digits and single-digit hours share canonical dropdown values', async t => {
  const ui = await page(t);
  ui.addPeriod('١٦', '١٧');
  ui.addPeriod('6:30', '7');
  assert.ok(ui.options('examPinPeriod').includes('16:00-17:00'));
  assert.ok(ui.options('examPinPeriod').includes('06:30-07:00'));
  readyToPin(ui, '16:00-17:00');
  assert.equal(ui.$('applyExamPin').disabled, false);
});

test('course picker preserves canonical identity when a course gains a numbered alias', async t => {
  const programming = { ...courses[1], course_code: 'CS111' };
  const ui = await page(t, { initialCourses: [programming] });
  ui.select('examPinCourse', 'CS111');
  ui.setCourses(courses);
  ui.$('loadCoursesBtn').click();
  await settle();
  assert.equal(ui.$('examPinCourse').value, 'CS111 (2)');
  assert.match(ui.$('examPinCourse').selectedOptions[0].textContent, /Programming I/);
});

test('course picker never silently selects a different course reusing the same code', async t => {
  const programming = { ...courses[1], course_code: 'CS111' };
  const fundamentals = { ...courses[0], course_code: 'CS111' };
  const ui = await page(t, { initialCourses: [programming] });
  ui.select('examPinCourse', 'CS111');
  ui.setCourses([fundamentals]);
  ui.$('loadCoursesBtn').click();
  await settle();
  assert.equal(ui.$('examPinCourse').value, '');
  assert.equal(ui.$('applyExamPin').disabled, true);
});

test('same-code courses remain distinct when pinned to separate periods', async t => {
  const ui = await page(t);
  readyToPin(ui);
  ui.$('applyExamPin').click();
  ui.select('examPinCourse', courses[1].course_code);
  ui.select('examPinPeriod', '10:30-12:30');
  ui.$('applyExamPin').click();
  const pinned = Array.from(ui.$('examPinRows').querySelectorAll('tr'));
  assert.equal(pinned.length, 2);
  assert.match(pinned[0].textContent, /Fundamentals of Programming.*08:00-10:00/s);
  assert.match(pinned[1].textContent, /Programming I.*10:30-12:30/s);
});

test('all period controls retain individual accessible names after add/remove', async t => {
  const ui = await page(t);
  ui.addPeriod('16', '17');
  ui.rows()[1].querySelector('.et-period-remove').click();
  const names = [];
  for (const row of ui.rows()) {
    for (const control of row.querySelectorAll('input, button')) {
      const name = control.getAttribute('aria-label') || Array.from(control.labels || [], label => label.textContent).join(' ');
      assert.ok(name.trim(), `Missing accessible name on ${control.outerHTML}`);
      names.push(name);
    }
  }
  assert.equal(new Set(names).size, names.length, 'Each start, end and remove control identifies its period');
});

test('history is keyboard reachable and run labels remain literal in the row and confirmation', async t => {
  const label = 'Final <June> "Group A" & B <img src=x onerror="alert(1)">';
  const ui = await page(t, { history: [{ id: 17, label, created_at: '2026-09-20T10:00:00Z' }] });
  const load = ui.$('historyList').querySelector('.et-run-info');
  assert.equal(load.tagName, 'BUTTON');
  assert.equal(load.type, 'button');
  assert.equal(load.tabIndex, 0);
  assert.equal(load.querySelector('strong').textContent, label);
  assert.equal(ui.$('historyList').querySelector('img, june'), null);
  const remove = ui.$('historyList').querySelector('.et-del-btn');
  assert.equal(remove.dataset.label, label);
  remove.click();
  await settle();
  assert.equal(ui.dialogs.length, 1);
  const body = ui.window.document.createElement('div');
  body.innerHTML = ui.dialogs[0].body;
  assert.equal(body.querySelector('strong').textContent, label);
  assert.equal(body.querySelector('img, june'), null);
});

test('KPI drilldowns support Enter and Space, announce expansion, and restore focus on close', async t => {
  const ui = await page(t);
  const cards = Array.from(ui.window.document.querySelectorAll('.kpi-click'));
  assert.ok(cards.length > 1);
  for (const card of cards) {
    assert.equal(card.getAttribute('role'), 'button');
    assert.equal(card.tabIndex, 0);
    assert.equal(card.getAttribute('aria-controls'), 'kpiDrill');
    assert.equal(card.getAttribute('aria-expanded'), 'false');
  }
  const card = cards[0];
  const key = (key, repeat = false) => {
    const event = new ui.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, repeat });
    card.dispatchEvent(event);
    assert.equal(event.defaultPrevented, true);
  };
  card.focus();
  key('Enter');
  assert.equal(ui.$('kpiDrill').classList.contains('d-none'), false);
  assert.equal(card.getAttribute('aria-expanded'), 'true');
  key('Enter', true);
  assert.equal(card.getAttribute('aria-expanded'), 'true', 'A held key must not repeatedly toggle the panel');
  key(' ');
  assert.equal(ui.$('kpiDrill').classList.contains('d-none'), true);
  assert.equal(card.getAttribute('aria-expanded'), 'false');
  key('Enter');
  ui.$('kpiDrillClose').focus();
  ui.$('kpiDrillClose').click();
  assert.equal(card.getAttribute('aria-expanded'), 'false');
  assert.equal(ui.window.document.activeElement, card);
});

const response = data => ({ ok: data.ok !== false, status: data.ok === false ? 409 : 200, json: async () => data });
const copyRequests = ui => ui.requests.filter(request => /\/\d+\/copy\/$/.test(request.url));
const copyButton = ui => ui.$('historyList').querySelector('.et-copy-btn');

test('shared prompt keeps its previous unconstrained and immediate-resolution defaults', async t => {
  const ui = await page(t, { realDialogs: true });
  const pending = ui.window.eval('dlg.prompt({ title: "Optional name", inputLabel: "Name" })');
  const dialog = ui.window.document.querySelector('.dlg-backdrop');
  const input = dialog.querySelector('.dlg-input');
  assert.equal(input.value, '');
  assert.equal(input.required, false);
  assert.equal(input.hasAttribute('maxlength'), false);
  assert.equal(dialog.querySelector('.btn-confirm').disabled, false);
  dialog.querySelector('.btn-confirm').click();
  assert.equal(await pending, true, 'The historical empty-input return value stays unchanged');
  assert.equal(dialog.isConnected, true, 'Existing callers still resolve before the closing animation');
  await pause(230);
  assert.equal(dialog.isConnected, false);
});

test('shared typed confirmations retain case-insensitive validation with optional close waiting', async t => {
  const ui = await page(t, { realDialogs: true });
  const pending = ui.window.eval('dlg.confirm({ title: "Confirm", typed: "DELETE", waitForClose: true })');
  const dialog = ui.window.document.querySelector('.dlg-backdrop');
  const button = dialog.querySelector('.btn-confirm');
  assert.equal(button.disabled, true);
  ui.input(dialog.querySelector('.dlg-input'), '  delete  ');
  assert.equal(button.disabled, false);
  button.click();
  assert.equal(dialog.isConnected, true);
  assert.equal(await pending, '  delete  ');
  assert.equal(dialog.isConnected, false);
});

test('Copy has a safe accessible name and an editable localized default within 120 characters', async t => {
  const label = 'Final <img src=x onerror="alert(1)"> & "Group" ' + 'ج'.repeat(100);
  const ui = await loadedEditor(t, { run: { ...savedRun(), label }, realDialogs: true });
  const button = copyButton(ui);
  assert.equal(button.tagName, 'BUTTON');
  assert.equal(button.getAttribute('aria-haspopup'), 'dialog');
  assert.equal(button.getAttribute('aria-label'), `${language === 'ar' ? 'نسخ الجدول' : 'Copy run'}: ${label}`);
  assert.ok(button.title);
  button.focus();
  button.click();
  const dialog = ui.window.document.querySelector('.dlg-backdrop');
  const input = dialog.querySelector('.dlg-input');
  const confirm = dialog.querySelector('.btn-confirm');
  assert.equal(dialog.querySelector('.dlg-body strong').textContent, label);
  assert.equal(dialog.querySelector('img'), null);
  assert.equal(input.maxLength, 120);
  assert.equal(input.required, true);
  assert.equal(input.value.length, 120);
  assert.ok(input.value.endsWith(language === 'ar' ? ' — نسخة' : ' — Copy'));
  assert.equal(confirm.textContent, language === 'ar' ? 'نسخ' : 'Copy');
  ui.input(input, '   ');
  assert.equal(confirm.disabled, true);
  dialog.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  assert.equal(copyRequests(ui).length, 0);
  ui.input(input, 'x'.repeat(121));
  assert.equal(confirm.disabled, true);
  ui.input(input, 'x'.repeat(120));
  assert.equal(confirm.disabled, false);
  dialog.querySelector('.btn-cancel').click();
  assert.equal(ui.$('historyList').inert, true, 'The copy lifecycle waits for physical dialog removal');
  await pause(230);
  assert.equal(ui.window.document.querySelector('.dlg-backdrop'), null);
  assert.equal(copyRequests(ui).length, 0);
  assert.equal(ui.$('historyList').inert, false);
  assert.equal(ui.window.document.activeElement, button);
});

test('Copy opens the returned snapshot, resets history to page 1, and blocks duplicate submissions and competing actions', async t => {
  const run = { ...savedRun(), pinned: [] };
  const copy = { ...run, run_id: 44, source_run_id: 17, label: 'Copied <May> & June' };
  let finish;
  let created = false;
  const ui = await loadedEditor(t, { run, onRequest: async url => {
    if (url.endsWith('/17/copy/')) return new Promise(resolve => { finish = () => { created = true; resolve({ ...response(copy), status: 201 }); }; });
    if (url.startsWith('/ops/exam-timetable/list/')) {
      const pageNumber = Number(new URL(url, 'http://exam.test').searchParams.get('page'));
      return response({ ok: true, page: pageNumber, total_pages: 2, total: 11,
        runs: created ? [{ id: 44, label: copy.label }, { id: 17, label: run.label }] : [{ id: 17, label: run.label }] });
    }
  } });
  ui.window.dlg.prompt = async options => { assert.equal(options.waitForClose, true); return `  ${copy.label}  `; };
  ui.$('historyPages').querySelector('[data-history-page="2"]').click();
  await settle();
  const before = ui.requests.length;
  const button = copyButton(ui);
  button.click();
  button.click();
  await settle();
  assert.equal(copyRequests(ui).length, 1);
  assert.deepEqual(JSON.parse(copyRequests(ui)[0].body), { label: copy.label });
  assert.equal(copyRequests(ui)[0].headers['X-CSRFToken'], 'test-csrf');
  assert.equal(ui.$('historyList').inert, true);
  assert.equal(ui.$('schedGrid').inert, true);
  assert.equal(ui.$('departmentFilesBtn').disabled, true);
  ui.$('historyList').querySelector('.et-run-info').click();
  ui.$('historyList').querySelector('.et-del-btn').click();
  ui.$('checkDraftBtn').click();
  button.click();
  assert.equal(ui.dialogs.length, 0);
  assert.equal(copyRequests(ui).length, 1);
  finish();
  await settle();
  await settle();
  assert.equal(ui.$('etLabel').value, copy.label);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/44/export.xlsx');
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  assert.equal(ui.$('undoExamBtn').disabled, true);
  assert.equal(ui.$('historyList').querySelector('.active').dataset.id, '44');
  assert.equal(ui.$('historyList').querySelector('.active strong').textContent, copy.label);
  assert.equal(ui.$('historyList').querySelector('may'), null);
  assert.deepEqual(ui.requests.slice(before).map(request => request.url), ['/ops/exam-timetable/17/copy/', '/ops/exam-timetable/list/?page=1']);
  assert.equal(ui.$('historyList').inert, false);
  assert.equal(ui.window.document.activeElement, ui.$('examEditHeading'));
});

test('canceling Copy name or discard confirmation preserves the current draft and creates nothing', async t => {
  for (const cancelAt of ['name', 'discard']) {
    const ui = await loadedEditor(t);
    dropExam(ui, 'Mon');
    if (cancelAt === 'discard') ui.window.dlg.prompt = async () => 'Second plan';
    copyButton(ui).click();
    await settle();
    assert.equal(copyRequests(ui).length, 0);
    assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
    assert.equal(ui.$('undoExamBtn').disabled, false);
    assert.equal(ui.$('saveLoadedBtn').disabled, false);
    assert.equal(ui.dialogs.length, cancelAt === 'discard' ? 1 : 0);
    if (cancelAt === 'discard') assert.equal(ui.dialogs[0].waitForClose, true);
    assert.equal(ui.$('historyList').inert, false);
  }
});

test('real Copy and discard dialogs close sequentially before the copy request and final focus', async t => {
  const copy = { ...savedRun(), run_id: 45, source_run_id: 17, label: 'Separate saved version' };
  const ui = await loadedEditor(t, { realDialogs: true, onRequest: async url => url.endsWith('/17/copy/') ? { ...response(copy), status: 201 } : undefined });
  dropExam(ui, 'Mon');
  copyButton(ui).click();
  const nameDialog = ui.window.document.querySelector('.dlg-backdrop');
  ui.input(nameDialog.querySelector('.dlg-input'), copy.label);
  nameDialog.querySelector('.btn-confirm').click();
  assert.equal(ui.window.document.querySelectorAll('.dlg-backdrop').length, 1);
  assert.equal(copyRequests(ui).length, 0);
  await pause(230);
  const discardDialog = ui.window.document.querySelector('.dlg-backdrop');
  assert.ok(discardDialog && discardDialog !== nameDialog);
  assert.equal(ui.window.document.querySelectorAll('.dlg-backdrop').length, 1);
  assert.match(discardDialog.textContent, language === 'ar' ? /تجاهل التغييرات/ : /Discard unsaved changes/);
  discardDialog.querySelector('.btn-confirm').click();
  assert.equal(copyRequests(ui).length, 0, 'No mutation before the discard dialog is removed');
  await pause(230);
  assert.equal(copyRequests(ui).length, 1);
  assert.equal(ui.window.document.querySelector('.dlg-backdrop'), null);
  assert.equal(ui.$('etLabel').value, copy.label);
  assert.deepEqual(placement(ui), ['Sun', '10:30-12:30'], 'Copy comes from the saved snapshot rather than the draft');
  assert.equal(ui.window.document.activeElement, ui.$('examEditHeading'));
});

for (const failure of ['conflict', 'auth', 'html', 'network']) {
  test(`Copy ${failure} errors preserve the current draft and release controls`, async t => {
    let ui;
    ui = await loadedEditor(t, { onRequest: async url => {
      if (!url.endsWith('/17/copy/')) return undefined;
      if (failure === 'conflict') return response({ ok: false, code: 'run_not_copyable', error: 'The saved timetable cannot be copied.' });
      if (failure === 'auth') return expiredSession();
      if (failure === 'html') return { ok: false, status: 500, headers: { get: () => 'text/html' } };
      throw new ui.window.TypeError('Connection unavailable');
    } });
    dropExam(ui, 'Mon');
    ui.window.dlg.prompt = async () => 'Second plan';
    ui.window.dlg.confirm = async () => true;
    const label = ui.$('etLabel').value;
    copyButton(ui).click();
    await settle();
    assert.equal(copyRequests(ui).length, 1);
    assert.equal(ui.$('etLabel').value, label);
    assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
    assert.equal(ui.$('undoExamBtn').disabled, false);
    assert.equal(ui.$('saveLoadedBtn').disabled, false);
    assert.equal(ui.$('historyList').inert, false);
    assert.equal(ui.$('examEditorRequestError').hidden, false);
    assert.equal(ui.$('examEditorRequestError').dataset.errorSource, 'copy');
    if (failure === 'auth') assert.ok(ui.$('examEditorRequestError').querySelector('a[target="_blank"]'));
    if (failure === 'conflict') assert.match(ui.$('examEditorRequestError').textContent, language === 'ar' ? /لا يمكن إنشاء نسخة/ : /cannot be copied/);
  });
}

test('a changed editor during Copy naming prevents the mutation', async t => {
  const ui = await loadedEditor(t);
  let finishName;
  ui.window.dlg.prompt = () => new Promise(resolve => { finishName = resolve; });
  copyButton(ui).click();
  ui.input(ui.$('etLabel'), 'Newer local draft');
  finishName('Second plan');
  await settle();
  assert.equal(copyRequests(ui).length, 0);
  assert.equal(ui.$('etLabel').value, 'Newer local draft');
  assert.match(ui.$('examEditorRequestError').textContent, language === 'ar' ? /تغيّر الجدول/ : /timetable changed/);
});

test('a late successful copy appears in history without replacing a newer draft', async t => {
  const copy = { ...savedRun(), run_id: 46, label: 'Second plan', source_run_id: 17 };
  let finish;
  let created = false;
  const ui = await loadedEditor(t, { onRequest: async url => {
    if (url.endsWith('/17/copy/')) return new Promise(resolve => { finish = () => { created = true; resolve({ ...response(copy), status: 201 }); }; });
    if (created && url.startsWith('/ops/exam-timetable/list/')) return response({ ok: true, runs: [{ id: 46, label: copy.label }, { id: 17, label: savedRun().label }] });
  } });
  ui.window.dlg.prompt = async () => copy.label;
  copyButton(ui).click();
  await settle();
  ui.input(ui.$('etLabel'), 'Newer local draft');
  finish();
  await settle();
  assert.equal(ui.$('etLabel').value, 'Newer local draft');
  assert.equal(ui.$('saveLoadedBtn').disabled, false);
  assert.equal(ui.$('historyList').querySelector('[data-id="46"] strong').textContent, copy.label);
  assert.equal(ui.$('historyList').querySelector('.active').dataset.id, '17');
  assert.match(ui.$('etStatus').textContent, language === 'ar' ? /احتُفظ بالجدول الحالي/ : /current timetable was kept/);
});
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const draftRequests = ui => ui.requests.filter(request => request.url === '/ops/exam-timetable/draft-impact/');
const buildRequests = ui => ui.requests.filter(request => request.url === '/ops/exam-timetable/build/');
const examChip = (ui, code = courses[1].course_code) => Array.from(ui.$('schedGrid').querySelectorAll('.et-course')).find(chip => chip.dataset.course === code);
const placement = (ui, code = courses[1].course_code) => {
  const cell = examChip(ui, code).closest('td');
  return [cell.dataset.day, cell.dataset.period];
};
const dropExam = (ui, day, period = '08:00-10:00', code = courses[1].course_code) => {
  const cell = Array.from(ui.$('schedGrid').querySelectorAll('td[data-day]')).find(cell => cell.dataset.day === day && cell.dataset.period === period);
  assert.ok(cell, `Missing target ${day} ${period}`);
  const event = new ui.window.Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: { getData: () => code } });
  cell.dispatchEvent(event);
};
async function loadedEditor(t, options = {}) {
  const run = options.run || { ...savedRun(), pinned: [] };
  const ui = await page(t, { run, history: [{ id: run.run_id, label: run.label }], liveUpdate: false, ...options });
  ui.$('historyList').querySelector('.et-run-info').click();
  await settle();
  return ui;
}

test('Exam Committee sees the shared history and can load and edit it without any Delete action', async t => {
  const run = { ...savedRun(), pinned: [], label: 'Created by another committee member' };
  const ui = await loadedEditor(t, { committee: true, run, history: [
    { id: 17, label: run.label, created_by: 'other-member' },
    { id: 18, label: 'Administrator timetable', created_by: 'administrator' },
  ] });
  assert.equal(ui.$('main-content').dataset.canDeleteExamTimetable, 'false');
  assert.deepEqual(Array.from(ui.$('menu').querySelectorAll('a'), a => a.getAttribute('href')), ['/exam-timetable/']);
  assert.match(ui.window.document.querySelector('.ph-chip-role').textContent, language === 'ar' ? /لجنة الاختبارات/ : /Exam Committee/);
  assert.equal(ui.$('historyList').querySelectorAll('.et-history-item').length, 2);
  assert.equal(ui.$('etLabel').value, run.label);
  assert.equal(ui.$('historyList').querySelectorAll('.et-del-btn').length, 0);
  assert.equal(ui.$('historyList').querySelectorAll('.et-copy-btn').length, 2);
  const before = ui.requests.length;
  await ui.window.deleteRun(17, run.label);
  assert.equal(ui.requests.length, before);
  assert.equal(ui.dialogs.length, 0);
  dropExam(ui, 'Mon');
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('saveLoadedBtn').disabled, false);
});

test('Exam Committee can copy a shared saved timetable and download its department workbook', async t => {
  const copy = { ...savedRun(), pinned: [], run_id: 48, label: 'Committee working copy', source_run_id: 17 };
  let created = false;
  const ui = await loadedEditor(t, { committee: true, onRequest: async url => {
    if (url.endsWith('/17/copy/')) { created = true; return { ...response(copy), status: 201 }; }
    if (created && url.startsWith('/ops/exam-timetable/list/')) return response({ ok: true, runs: [{ id: 48, label: copy.label }, { id: 17, label: savedRun().label }] });
    if (url.includes('/departments/?')) return response(departmentCatalog());
    if (url.endsWith('/departments/export/')) return departmentResponse();
  } });
  ui.window.dlg.prompt = async () => copy.label;
  copyButton(ui).click();
  await settle();
  await settle();
  assert.equal(copyRequests(ui).length, 1);
  assert.equal(ui.$('etLabel').value, copy.label);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/48/export.xlsx');
  assert.equal(ui.$('historyList').querySelectorAll('.et-del-btn').length, 0);
  const { downloads } = captureDepartmentDownloads(ui);
  ui.$('departmentFilesBtn').click();
  await settle();
  ui.$('downloadExamDepartments').click();
  await settle();
  assert.deepEqual(downloads, [{ href: 'blob:exam-departments', filename: 'exam-ai-ds.xlsx' }]);
  assert.equal(departmentRequests(ui)[0].url, '/ops/exam-timetable/48/departments/export/');
});

test('dragging moves one course without pinning, checking, optimizing or changing the other duplicate code', async t => {
  const ui = await loadedEditor(t);
  dropExam(ui, 'Mon');
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
  assert.deepEqual(placement(ui, courses[0].course_code), ['Sun', '08:00-10:00']);
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 0);
  assert.equal(examChip(ui).draggable, true);
  assert.equal(draftRequests(ui).length, 0);
  assert.equal(buildRequests(ui).length, 0);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  assert.equal(ui.$('etResults').dataset.calculationState, 'stale');
  assert.equal(ui.$('examSummaryCards').classList.contains('et-calculation-stale'), true);
  assert.equal(ui.$('undoExamBtn').disabled, false);
});

test('same-slot drops do not dirty the saved timetable or create undo entries', async t => {
  const ui = await loadedEditor(t, { liveUpdate: true });
  dropExam(ui, 'Sun', '10:30-12:30');
  await pause(520);
  assert.equal(ui.$('undoExamBtn').disabled, true);
  assert.equal(draftRequests(ui).length, 0);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx');
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
});

test('five manual moves and one Check refresh every result surface without moving or saving any exam', async t => {
  const run = savedRun();
  run.pinned = [];
  run.schedule[0].rooms = [{ room_code: 'OLD-ROOM' }];
  const ui = await loadedEditor(t, {
    run,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      const data = evaluatedRun(JSON.parse(options.body), run);
      data.students_count = 47;
      data.primary_status = 'requires_room_action';
      data.status_flags = ['room_action_required'];
      data.qa = { max_per_day: 2, slots_used: 2, max_exams_per_day_per_student: 3, students_over_limit_per_day: 2,
        conflict_count: 1, bucket_day_violations_count: 1, max_credit_load_per_day: 10, heavy_day_students: 3,
        same_slot_conflicts: [{ student_id: 'S123', courses: [courses[0].course_code, courses[1].course_code], slot_index: 0 }],
        rooms: { rooms_used: 4, avg_utilization: 0.75, unassigned_room_sections: [{}], room_double_bookings: [{}, {}] },
        thin_threshold: 4, thin_courses: [{ course: courses[0].course_code }], thin_clash_risk: [{}],
        multi_sitting_sections: 2, multi_sitting_details: [], building_footprint: { largest_slot_footprint_summary: 'Building A + B' } };
      data.schedule[0].rooms = [{ room_code: 'CHECKED-ROOM' }];
      return response(data);
    },
  });
  ui.$('kConflicts').closest('.kpi-click').click();
  for (const day of ['Mon', 'Tue', 'Wed', 'Thu', 'Mon']) dropExam(ui, day);
  assert.equal(draftRequests(ui).length, 0);
  assert.equal(ui.$('kpiDrill').classList.contains('et-calculation-stale'), true);
  assert.equal(ui.$('schedGrid').querySelector('.et-room-tag'), null, 'Compact grid does not display room assignments');
  const before = courses.map(course => placement(ui, course.course_code));
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(draftRequests(ui).length, 1);
  assert.equal(buildRequests(ui).length, 0);
  assert.deepEqual(courses.map(course => placement(ui, course.course_code)), before);
  for (const [id, value] of Object.entries({ kStudents: '47', kSlots: '2', kMaxDay: '3', kOver2: '2', kConflicts: '1', kMaxCredit: '10', kHeavyDay: '3', kRoomsUsed: '4', kRoomUtil: '75%', kRoomUnassigned: '1', kRoomDouble: '2', kThinCount: '1', kThinClash: '1', kMultiSittingCount: '2' })) {
    assert.equal(ui.$(id).textContent, value, id);
  }
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
  assert.equal(ui.$('kpiDrill').classList.contains('d-none'), false, 'The open detail stays open and refreshes');
  assert.match(ui.$('kpiDrillBody').textContent, /S123/);
  assert.doesNotMatch(ui.$('schedGrid').textContent, /CHECKED-ROOM/, 'Room calculations update outside the compact course cards');
  assert.doesNotMatch(ui.$('schedGrid').textContent, /OLD-ROOM/);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null, 'Checking never marks the draft saved');
  assert.equal(ui.$('undoExamBtn').disabled, false, 'Checking preserves manual history');
  assert.equal(JSON.parse(draftRequests(ui)[0].body).previous_run_id, 17);
});

test('double-click and visible Pin toggle the same identity; pinned exams cannot be dragged or moved', async t => {
  const ui = await loadedEditor(t);
  examChip(ui).dispatchEvent(new ui.window.MouseEvent('dblclick', { bubbles: true }));
  assert.equal(examChip(ui).draggable, false);
  assert.equal(examChip(ui).querySelector('[data-exam-pin]').getAttribute('aria-pressed'), 'true');
  assert.equal(examChip(ui).querySelector('.et-pin-lock').getAttribute('aria-hidden'), 'true');
  assert.equal(examChip(ui).querySelector('[data-exam-move]').disabled, true);
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 1);
  const start = new ui.window.Event('dragstart', { bubbles: true, cancelable: true });
  examChip(ui).dispatchEvent(start);
  assert.equal(start.defaultPrevented, true);
  dropExam(ui, 'Tue');
  assert.deepEqual(placement(ui), ['Sun', '10:30-12:30']);
  examChip(ui).querySelector('[data-exam-pin]').click();
  assert.equal(examChip(ui).draggable, true);
  assert.equal(examChip(ui).querySelector('.et-pin-lock'), null);
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 0);
  assert.equal(examChip(ui, courses[0].course_code).querySelector('[data-exam-pin]').getAttribute('aria-pressed'), 'false');
});

test('Move dialog supplies keyboard/touch placement controls using the same undoable move command', async t => {
  const ui = await loadedEditor(t);
  examChip(ui).querySelector('[data-exam-move]').click();
  assert.equal(ui.$('examMoveDialog').hasAttribute('open'), true);
  assert.match(ui.$('examMoveTitle').textContent, /Programming I/);
  ui.select('examMoveDay', 'Wed');
  ui.select('examMovePeriod', '13:00-15:00');
  ui.$('confirmExamMove').click();
  assert.equal(ui.$('examMoveDialog').hasAttribute('open'), false);
  assert.deepEqual(placement(ui), ['Wed', '13:00-15:00']);
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 0);
  assert.equal(ui.window.document.activeElement.dataset.examMove, courses[1].course_code);
  ui.$('undoExamBtn').click();
  assert.deepEqual(placement(ui), ['Sun', '10:30-12:30']);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx');
  ui.$('redoExamBtn').click();
  assert.deepEqual(placement(ui), ['Wed', '13:00-15:00']);
});

test('the fixed-time editor changes pin and placement in one Undo/Redo operation', async t => {
  const ui = await loadedEditor(t, { run: savedRun() });
  ui.select('examPinCourse', courses[0].course_code);
  ui.select('examPinDay', 'Thu');
  ui.select('examPinPeriod', '13:00-15:00');
  ui.$('applyExamPin').click();
  assert.deepEqual(placement(ui, courses[0].course_code), ['Thu', '13:00-15:00']);
  ui.$('undoExamBtn').click();
  assert.deepEqual(placement(ui, courses[0].course_code), ['Sun', '08:00-10:00']);
  assert.match(ui.$('examPinRows').textContent, /Sun.*08:00-10:00/s);
  assert.equal(ui.$('undoExamBtn').disabled, true);
  ui.$('redoExamBtn').click();
  assert.deepEqual(placement(ui, courses[0].course_code), ['Thu', '13:00-15:00']);
  assert.match(ui.$('examPinRows').textContent, /Thu.*13:00-15:00/s);
});

test('Undo after checking restores saved cards and export without another evaluation', async t => {
  const ui = await loadedEditor(t);
  dropExam(ui, 'Tue');
  ui.$('checkDraftBtn').click();
  await settle();
  ui.$('undoExamBtn').click();
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx');
  assert.equal(draftRequests(ui).length, 1);
  assert.equal(ui.$('redoExamBtn').disabled, false);
});

test('rapid moves coalesce and an older in-flight check cannot overwrite a newer revision', async t => {
  let finishFirst;
  let calls = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      calls++;
      const data = { ...evaluatedRun(JSON.parse(options.body)), students_count: calls === 1 ? 999 : 51 };
      if (calls === 1) return new Promise(resolve => { finishFirst = () => resolve(response(data)); });
      return response(data);
    },
  });
  dropExam(ui, 'Mon');
  dropExam(ui, 'Tue');
  dropExam(ui, 'Wed');
  await pause(520);
  assert.equal(calls, 1);
  dropExam(ui, 'Thu');
  await pause(520);
  assert.equal(calls, 1, 'Only one calculation may be in flight');
  finishFirst();
  await settle();
  assert.notEqual(ui.$('kStudents').textContent, '999');
  assert.equal(ui.$('etResults').dataset.calculationState, 'stale');
  await pause(520);
  assert.equal(calls, 2);
  assert.equal(ui.$('kStudents').textContent, '51');
  assert.deepEqual(placement(ui), ['Thu', '08:00-10:00']);
  assert.equal(buildRequests(ui).length, 0);
});

test('turning Live update off invalidates an in-flight response and remembers the preference', async t => {
  let finish;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      return new Promise(resolve => { finish = () => resolve(response({ ...evaluatedRun(JSON.parse(options.body)), students_count: 888 })); });
    },
  });
  dropExam(ui, 'Tue');
  await pause(520);
  ui.$('examLiveUpdate').checked = false;
  ui.emit(ui.$('examLiveUpdate'), 'change');
  finish();
  await settle();
  await pause(520);
  assert.equal(draftRequests(ui).length, 1);
  assert.notEqual(ui.$('kStudents').textContent, '888');
  assert.equal(ui.$('etResults').dataset.calculationState, 'stale');
  assert.equal(ui.window.localStorage.getItem('exam-timetable-live-update'), 'off');
});

test('Save checks an unchecked draft, submits reviewed inputs and saves exactly the visible placements', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/build/') return undefined;
      const payload = JSON.parse(options.body);
      return response({ ...evaluatedRun(payload), run_id: 19, rebuild_mode: 'loaded_schedule' });
    },
  });
  dropExam(ui, 'Thu');
  ui.$('saveLoadedBtn').click();
  assert.equal(ui.$('schedGrid').inert, true);
  assert.equal(ui.$('examEditToolbar').inert, true);
  await settle();
  assert.equal(draftRequests(ui).length, 1);
  assert.equal(buildRequests(ui).length, 1);
  const payload = JSON.parse(buildRequests(ui)[0].body);
  assert.equal(payload.mode, 'save_loaded_changes');
  assert.equal(payload.expected_input_fingerprint, 'reviewed-inputs');
  assert.equal(payload.previous_run_id, 17);
  assert.equal(payload.base_schedule.find(entry => entry.course_identity === courses[1].course_identity).day, 'Thu');
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/19/export.xlsx');
  assert.deepEqual(placement(ui), ['Thu', '08:00-10:00']);
  assert.equal(ui.$('undoExamBtn').disabled, true);
});

test('changed upstream inputs reject Save, preserve manual edits and require a fresh check', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? response({ ok: false, error_code: 'inputs_changed', error: 'Enrollment changed. Check again.' }) : undefined,
  });
  dropExam(ui, 'Mon');
  ui.$('saveLoadedBtn').click();
  await settle();
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
  assert.equal(ui.$('etResults').dataset.calculationState, 'error');
  assert.match(ui.$('draftImpactBanner').textContent, /Enrollment changed/);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  assert.equal(ui.$('schedGrid').inert, false);
  assert.equal(ui.$('undoExamBtn').disabled, false);
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(draftRequests(ui).length, 2);
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
});

test('Optimize is explicit and uses unsaved current positions, pins and enrollment scope', async t => {
  const ui = await loadedEditor(t);
  dropExam(ui, 'Wed');
  examChip(ui).querySelector('[data-exam-pin]').click();
  ui.$('optimizeLoadedBtn').click();
  await settle();
  assert.equal(draftRequests(ui).length, 0);
  assert.equal(buildRequests(ui).length, 1);
  const payload = JSON.parse(buildRequests(ui)[0].body);
  assert.equal(payload.mode, 'optimize_loaded');
  assert.equal(payload.base_schedule.find(entry => entry.course_identity === courses[1].course_identity).day, 'Wed');
  assert.deepEqual(payload.pinned, [{ course_code: courses[1].course_code, day: 'Wed', period: '08:00-10:00' }]);
  assert.deepEqual(payload.programs, ['AI', 'CS']);
  assert.deepEqual(payload.sections, ['F', 'M']);
});

test('Fix with fewest moves sends its own mode with the unsaved board and pins', async t => {
  const ui = await loadedEditor(t);
  dropExam(ui, 'Wed');
  examChip(ui).querySelector('[data-exam-pin]').click();
  assert.equal(ui.$('minChangeBtn').classList.contains('d-none'), false, 'Offered wherever Optimize is');
  ui.$('minChangeBtn').click();
  await settle();
  assert.equal(buildRequests(ui).length, 1);
  const payload = JSON.parse(buildRequests(ui)[0].body);
  assert.equal(payload.mode, 'minimum_change_repair', 'Must never be routed as a full re-solve');
  assert.equal(payload.base_schedule.find(entry => entry.course_identity === courses[1].course_identity).day, 'Wed');
  assert.deepEqual(payload.pinned, [{ course_code: courses[1].course_code, day: 'Wed', period: '08:00-10:00' }]);
});

function repairedRun(ui, minimumChange) {
  const data = evaluatedRun(JSON.parse(buildRequests(ui)[0].body));
  return response({ ...data, run_id: 91, minimum_change: minimumChange });
}

async function runRepair(t, minimumChange) {
  let ui;
  ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/' ? repairedRun(ui, minimumChange) : undefined,
  });
  dropExam(ui, 'Wed');
  ui.$('minChangeBtn').click();
  await settle();
  return ui;
}

const move = (code, from, to) => ({
  course_code: code, course_name: '',
  from: { day: from, period: '08:00-10:00' }, to: { day: to, period: '10:30-12:30' },
});
const solved = { unseated: [], violations_before: 1, violations_after: 0, proven_minimal: true, status: 'OPTIMAL' };

// jsdom reads text whether or not it can be seen, which is how an invisible
// report once passed every test here. Assert the thing a registrar needs.
function assertVisible(ui, element) {
  assert.ok(element.textContent.trim(), 'The report must say something');
  for (let node = element; node; node = node.parentElement) {
    assert.equal(node.hidden, false, `${node.id || node.tagName} hides the report`);
    assert.equal(node.classList?.contains('d-none'), false, `${node.id || node.tagName} hides the report`);
    if (node.tagName === 'DETAILS' && node !== element) {
      assert.equal(node.open, true, `${node.id || 'a details element'} is collapsed around the report`);
    }
  }
}

test('the repair report is shown where the registrar can see it, not in the collapsed setup', async t => {
  const ui = await runRepair(t, { ...solved, moves: [move('CS201', 'Mon', 'Tue')] });
  const report = ui.$('examRepairReport');
  assertVisible(ui, report);
  assert.equal(ui.$('examSetupDetails').open, false, 'Setup still collapses for a saved run');
  assert.equal(report.closest('#examSetupDetails'), null);
  assert.equal(report.getAttribute('role'), 'status');
});

test('a repair tells the registrar exactly which exams moved, and that it was the minimum', async t => {
  const ui = await runRepair(t, { ...solved, moves: [move('CS201', 'Mon', 'Tue')] });
  const report = ui.$('examRepairReport');
  assert.ok(report.classList.contains('is-clean'), 'A fully legal result is clean');
  assert.match(report.textContent, /CS201/);
  assert.match(report.textContent, language === 'ar' ? /أقل عدد ممكن/ : /the fewest possible/);
  assert.match(report.textContent, language === 'ar' ? /الجداول المحفوظة/ : /Saved timetables/);
  // Times sit inside LTR isolates: unisolated, Arabic paints 08:00-10:00 as 10:00-08:00.
  const times = [...report.querySelectorAll('bdi[dir="ltr"]')].map(node => node.textContent);
  assert.ok(times.some(text => text.includes('08:00-10:00')), 'The origin time must be isolated');
  assert.ok(times.some(text => text.includes('10:30-12:30')), 'The destination time must be isolated');
  // The arrow is aria-hidden, so the direction must be spoken in words.
  assert.match(report.querySelector('.visually-hidden').textContent, language === 'ar' ? /إلى/ : /to/);
});

// Arabic agrees in five forms; a 1-versus-more test gets every count from two up wrong.
for (const [count, arabic, english] of [
  [1, /تم نقل اختبار واحد/, /Moved 1 exam\b/],
  [2, /تم نقل اختبارين/, /Moved 2 exams/],
  [3, /تم نقل 3 اختبارات/, /Moved 3 exams/],
  [11, /تم نقل 11 اختباراً/, /Moved 11 exams/],
]) {
  test(`the move count agrees grammatically for ${count} exam${count === 1 ? '' : 's'}`, async t => {
    const moves = Array.from({ length: count }, (_, index) => move(`CS${200 + index}`, 'Mon', 'Tue'));
    const ui = await runRepair(t, { ...solved, moves });
    assert.match(ui.$('examRepairReport').textContent, language === 'ar' ? arabic : english);
  });
}

test('every moved exam is listed, with the long tail behind a disclosure rather than dropped', async t => {
  const moves = Array.from({ length: 9 }, (_, index) => move(`CS${300 + index}`, 'Mon', 'Tue'));
  const ui = await runRepair(t, { ...solved, moves });
  const report = ui.$('examRepairReport');
  for (const { course_code: code } of moves) assert.match(report.textContent, new RegExp(code));
  const more = report.querySelector('details');
  assert.ok(more, 'Moves past the first six must remain reachable');
  assert.match(more.querySelector('summary').textContent, /9/);
});

test('a repair that cannot clear the board names what went to Overflow and does not claim success', async t => {
  const ui = await runRepair(t, {
    moves: [], unseated: ['CS301', 'CS302'], violations_before: 3, violations_after: 1, proven_minimal: true, status: 'OPTIMAL',
  });
  const report = ui.$('examRepairReport');
  assert.ok(report.classList.contains('is-partial'), 'A board left with a clash is not clean');
  assert.equal(report.classList.contains('is-clean'), false);
  assert.match(report.textContent, /CS301/);
  assert.match(report.textContent, /CS302/);
  assert.match(report.textContent, language === 'ar' ? /فترة إضافية/ : /Overflow slot/);
  assert.match(report.textContent, language === 'ar' ? /بقيت مخالفة واحدة/ : /1 rule break remains/);
});

test('an exam that was never in a clash but moved to make room is explained', async t => {
  const ui = await runRepair(t, { ...solved, widened: true, proven_minimal: false, status: 'FEASIBLE',
    moves: [move('CS201', 'Mon', 'Tue'), move('CS202', 'Tue', 'Mon')] });
  assert.match(ui.$('examRepairReport').textContent, language === 'ar' ? /لإفساح المجال/ : /moved to make room/);
});

test('the Overflow message states the fact without claiming a cause it cannot vouch for', async t => {
  const ui = await runRepair(t, { moves: [], unseated: ['CS301'], violations_before: 1, violations_after: 0, proven_minimal: false, status: 'FEASIBLE' });
  const text = ui.$('examRepairReport').textContent;
  assert.match(text, /CS301/);
  assert.doesNotMatch(text, language === 'ar' ? /دون تحريك اختبار مثبّت/ : /without moving a pinned exam/);
});

test('a solver that did not finish is never reported as the registrar leaving clashes behind', async t => {
  const ui = await runRepair(t, {
    moves: [], unseated: [], violations_before: 3, violations_after: 3, proven_minimal: false, status: 'UNKNOWN',
  });
  const text = ui.$('examRepairReport').textContent;
  assert.match(text, language === 'ar' ? /تعذّر إكمال الإصلاح/ : /could not finish/);
  assert.doesNotMatch(text, language === 'ar' ? /نقلتَها منذ آخر حفظ/ : /you moved since the last save/);
});

test('a repair the solver could not prove minimal does not claim to be minimal', async t => {
  const ui = await runRepair(t, { ...solved, proven_minimal: false, status: 'FEASIBLE', moves: [move('CS201', 'Mon', 'Tue')] });
  assert.match(ui.$('examRepairReport').textContent, /CS201/);
  assert.doesNotMatch(ui.$('examRepairReport').textContent, language === 'ar' ? /أقل عدد ممكن/ : /fewest possible/);
});

test('a repair on a board with nothing wrong says so rather than reporting a move', async t => {
  const ui = await runRepair(t, { ...solved, violations_before: 0, moves: [] });
  assert.match(ui.$('examRepairReport').textContent, language === 'ar' ? /لا يوجد ما يحتاج إصلاحاً/ : /Nothing to repair/);
});

test('a repair that moves nothing keeps the draft on screen and says nothing was saved', async t => {
  let ui;
  ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? response({ ok: true, saved: false, minimum_change: { ...solved, moves: [], violations_after: 1, proven_minimal: false } })
      : undefined,
  });
  dropExam(ui, 'Wed');
  const dragged = courses[1].course_code;
  const cellHolds = () => Array.from(ui.$('schedGrid').querySelectorAll('td[data-day="Wed"][data-period="08:00-10:00"]'))
    .some(cell => cell.textContent.includes(dragged));
  assert.ok(cellHolds(), 'The drag landed');
  const historyLoads = () => ui.requests.filter(request => request.url.startsWith('/ops/exam-timetable/list/')).length;
  const before = historyLoads();

  ui.$('minChangeBtn').click();
  await settle();

  const report = ui.$('examRepairReport');
  assertVisible(ui, report);
  assert.match(report.textContent, language === 'ar' ? /اختبارات مثبّتة أو نقلتَها/ : /exams you moved since the last save/);
  assert.doesNotMatch(report.textContent, language === 'ar' ? /حُفظت النتيجة/ : /Saved as a new timetable/);
  assert.match(ui.$('etStatus').textContent, language === 'ar' ? /لم يُحفظ شيء/ : /nothing was saved/);
  assert.doesNotMatch(ui.$('etStatus').className, /alert-success|alert-danger/);
  assert.ok(cellHolds(), 'The registrar\'s unsaved drag was thrown away');
  assert.equal(historyLoads(), before, 'Nothing was saved, so the history has nothing new to show');
});

test('the report clears as soon as the board is edited again', async t => {
  const ui = await runRepair(t, { ...solved, moves: [move('CS201', 'Mon', 'Tue')] });
  assert.ok(ui.$('examRepairReport').textContent.trim());
  dropExam(ui, 'Thu');
  await settle();
  assert.equal(ui.$('examRepairReport').textContent, '', 'A stale report would describe a board that no longer exists');
});

test('a malformed report degrades instead of turning a saved repair into an error', async t => {
  const ui = await runRepair(t, { ...solved, moves: [null, { course_code: 'CS201' }, move('CS202', 'Mon', 'Tue')] });
  assert.match(ui.$('examRepairReport').textContent, /CS202/);
  assert.doesNotMatch(ui.$('etStatus').className, /alert-danger/);
});

test('every server value is escaped in the repair report', async t => {
  const payload = '<img src=x onerror=alert(1)>';
  const ui = await runRepair(t, {
    ...solved,
    unseated: [payload],
    violations_after: 1,
    moves: [{ course_code: payload, course_name: '', from: { day: payload, period: payload }, to: { day: 'Tue', period: '08:00-10:00' } }],
  });
  assert.equal(ui.$('examRepairReport').querySelector('img'), null, 'Server text must never become markup');
  assert.match(ui.$('examRepairReport').textContent, /<img src=x/);
});

function assertRepairMatchesOptimize(ui, when) {
  for (const property of ['disabled', 'hidden']) {
    const read = id => property === 'hidden' ? ui.$(id).classList.contains('d-none') : ui.$(id).disabled;
    assert.equal(read('minChangeBtn'), read('optimizeLoadedBtn'), `${when}: repair ${property} must match Optimize`);
  }
}

test('Fix with fewest moves is offered exactly when Optimize is', async t => {
  const fresh = await page(t, { loadCourses: true });
  await settle();
  assertRepairMatchesOptimize(fresh, 'before any timetable is loaded');
  assert.equal(fresh.$('minChangeBtn').classList.contains('d-none'), true, 'Nothing to repair yet');

  const ui = await loadedEditor(t);
  assertRepairMatchesOptimize(ui, 'once a timetable is loaded');
  assert.equal(ui.$('minChangeBtn').disabled, false);

  ui.$('minChangeBtn').click();
  // While the repair is in flight the whole toolbar is inert, so neither this
  // nor Optimize can be fired a second time over the same board.
  assert.equal(ui.$('examEditToolbar').inert, true);
  await settle();
  assertRepairMatchesOptimize(ui, 'after a repair request finishes');
  assert.equal(ui.$('examEditToolbar').inert, false, 'A failed repair must permit retry');
});

test('both buttons explain themselves, in the page language', async t => {
  const ui = await loadedEditor(t);
  const fix = ui.$('minChangeBtn').getAttribute('title');
  const optimize = ui.$('optimizeLoadedBtn').getAttribute('title');
  assert.match(fix, language === 'ar' ? /مثبّت أو نقلتَه منذ آخر حفظ/ : /since the last save/);
  assert.match(optimize, language === 'ar' ? /نقلتَها دون تثبيتها/ : /you moved and did not pin/);
  assert.match(ui.$('examEditHelp').textContent, language === 'ar' ? /إصلاح بأقل تغيير/ : /Fix with fewest moves/);
});

test('failed or placement-changing check responses leave the draft visibly unchecked and retryable', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      const data = evaluatedRun(JSON.parse(options.body));
      data.schedule[1].day = 'Fri';
      return response(data);
    },
  });
  dropExam(ui, 'Mon');
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('etResults').dataset.calculationState, 'error');
  assert.equal(ui.$('checkDraftBtn').disabled, false);
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
});

test('policy edits invalidate cards and unsaved edits require confirmation before loading or reloading courses', async t => {
  const ui = await loadedEditor(t);
  ui.input(ui.$('etMaxPerDay'), '3');
  assert.equal(ui.$('etResults').dataset.calculationState, 'stale');
  assert.equal(draftRequests(ui).length, 0);
  const requestsBefore = ui.requests.length;
  ui.$('historyList').querySelector('.et-run-info').click();
  await settle();
  assert.equal(ui.dialogs.length, 1);
  assert.equal(ui.requests.length, requestsBefore);
  ui.$('loadCoursesBtn').click();
  await settle();
  assert.equal(ui.dialogs.length, 2);
  assert.equal(ui.requests.length, requestsBefore);
  assert.equal(ui.$('etMaxPerDay').value, '3');
  const event = new ui.window.Event('beforeunload', { cancelable: true });
  ui.window.dispatchEvent(event);
  assert.equal(event.defaultPrevented, true);
});

test('keyboard Undo/Redo only handles timetable commands and leaves normal input editing alone', async t => {
  const ui = await loadedEditor(t);
  dropExam(ui, 'Tue');
  const inputEvent = new ui.window.KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true, cancelable: true });
  ui.$('schedFilter').dispatchEvent(inputEvent);
  assert.equal(inputEvent.defaultPrevented, false);
  assert.deepEqual(placement(ui), ['Tue', '08:00-10:00']);
  const undo = new ui.window.KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true, cancelable: true });
  examChip(ui).querySelector('[data-exam-pin]').dispatchEvent(undo);
  assert.equal(undo.defaultPrevented, true);
  assert.deepEqual(placement(ui), ['Sun', '10:30-12:30']);
  examChip(ui).querySelector('[data-exam-pin]').dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'z', ctrlKey: true, shiftKey: true, bubbles: true, cancelable: true }));
  assert.deepEqual(placement(ui), ['Tue', '08:00-10:00']);
});

test('checking unchanged placements with changed source results makes Save available and blocks the old export', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      const data = { ...evaluatedRun(JSON.parse(options.body)), input_fingerprint: 'changed-inputs', students_count: 99 };
      data.schedule[0].rooms = [{ room_code: 'NEW-ROOM' }];
      return response(data);
    },
  });
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('kStudents').textContent, '99');
  assert.doesNotMatch(ui.$('schedGrid').textContent, /NEW-ROOM/, 'Room data does not expand compact grid cards');
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
  assert.equal(ui.$('saveLoadedBtn').disabled, false);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  assert.match(ui.$('examCheckStatus').textContent, language === 'ar' ? /غير محفوظة/ : /Unsaved/);
  const leave = new ui.window.Event('beforeunload', { cancelable: true });
  ui.window.dispatchEvent(leave);
  assert.equal(leave.defaultPrevented, true, 'Fresh source results also need discard protection');
});

test('an identical Check remains saved and ignores only regenerated snapshot timestamps', async t => {
  const run = savedRun();
  run.pinned = [];
  run.qa.enrolment_snapshot = { source_hash: 'same-source', snapshot_timestamp: '2026-09-20T10:00:00Z' };
  const ui = await loadedEditor(t, {
    run,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      const data = evaluatedRun(JSON.parse(options.body), run);
      data.qa = { ...run.qa, enrolment_snapshot: { ...run.qa.enrolment_snapshot, snapshot_timestamp: '2026-09-20T11:00:00Z' } };
      return response(data);
    },
  });
  ui.$('toggleMatrix').click();
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx');
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
  assert.equal(ui.$('conflictMatrix').classList.contains('d-none'), false, 'An open matrix stays open');
});

test('manual Check loading and errors stay visible even when no editor values changed', async t => {
  let finish;
  const ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/draft-impact/'
      ? new Promise(resolve => { finish = () => resolve(response({ ok: false, error: 'Unable to read rooms' })); }) : undefined,
  });
  ui.$('checkDraftBtn').click();
  assert.equal(ui.$('etResults').dataset.calculationState, 'loading');
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  finish();
  await settle();
  assert.equal(ui.$('etResults').dataset.calculationState, 'error');
  assert.match(ui.$('draftImpactBanner').textContent, /Unable to read rooms/);
  assert.equal(ui.$('draftImpactBanner').classList.contains('d-none'), false);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
});

for (const baselineKnown of [true, false]) {
  test(`automatic pre-Save checking requires explicit review of ${baselineKnown ? 'changed' : 'unknown'} source inputs`, async t => {
    const run = savedRun();
    run.pinned = [];
    if (!baselineKnown) delete run.input_fingerprint;
    const ui = await loadedEditor(t, {
      run,
      onRequest: async (url, options) => {
        if (url === '/ops/exam-timetable/draft-impact/') return response({
          ...evaluatedRun(JSON.parse(options.body), run), input_fingerprint: 'new-reviewed-inputs', students_count: 88,
        });
        if (url === '/ops/exam-timetable/build/') return response({
          ...evaluatedRun(JSON.parse(options.body), run), run_id: 21, input_fingerprint: 'new-reviewed-inputs', students_count: 88,
        });
        return undefined;
      },
    });
    dropExam(ui, 'Mon');
    ui.$('saveLoadedBtn').click();
    await settle();
    assert.equal(buildRequests(ui).length, 0, 'Save must not silently accept changed source data');
    assert.equal(ui.$('etResults').dataset.calculationState, 'review');
    assert.equal(ui.$('kStudents').textContent, '88');
    assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
    ui.emit(ui.$('examLiveUpdate'), 'change');
    assert.equal(ui.$('etResults').dataset.calculationState, 'review', 'Changing live preference cannot dismiss a required source review');
    ui.$('checkDraftBtn').click();
    await settle();
    assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
    ui.$('saveLoadedBtn').click();
    await settle();
    assert.equal(buildRequests(ui).length, 1);
    assert.equal(JSON.parse(buildRequests(ui)[0].body).expected_input_fingerprint, 'new-reviewed-inputs');
    assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/21/export.xlsx');
  });
}

test('canceling a filter change preserves both the dirty timetable and the prior filter selection', async t => {
  const ui = await loadedEditor(t);
  dropExam(ui, 'Mon');
  const filter = ui.$('progList').querySelector('input[value="AI"]');
  const originalCount = ui.$('progCount').textContent;
  filter.checked = false;
  ui.emit(filter, 'change');
  await settle();
  assert.equal(ui.dialogs.length, 1);
  assert.equal(filter.checked, true);
  assert.equal(ui.$('progCount').textContent, originalCount);
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
  assert.equal(ui.$('etResults').classList.contains('d-none'), false);
  assert.equal(ui.$('saveLoadedBtn').disabled, false);
});

const emptyRoomQa = () => ({
  rooms_available: 0, rooms_used: 0, total_demand: 0, total_capacity_used: 0, avg_utilization: 0,
  unassigned_room_sections: [], room_double_bookings: [], invigilators_per_day: {},
  invigilators_total: 0, invigilators_total_M: 0, invigilators_total_F: 0,
});

test('report comparison normalizes the real no-room build and evaluator QA shapes', async t => {
  const run = savedRun();
  run.pinned = [];
  run.assign_rooms = false;
  // Fresh build omits all three fields when room assignment is disabled.
  assert.equal(run.qa.rooms, undefined);
  assert.equal(run.qa.room_feasibility_violations, undefined);
  assert.equal(run.qa.rebalance_moves, undefined);
  const ui = await loadedEditor(t, {
    run,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      const payload = JSON.parse(options.body);
      assert.equal(payload.assign_rooms, false);
      const data = evaluatedRun(payload, run);
      data.qa = { ...run.qa, rooms: emptyRoomQa(), room_feasibility_violations: [], rebalance_moves: 0 };
      return response(data);
    },
  });
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('kRoomsUsed').textContent, '0');
  assert.equal(ui.$('kRoomUnassigned').textContent, '0');
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx');
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
});

test('report comparison ignores historical optimizer rebalance counts when current QA is identical', async t => {
  const run = savedRun();
  run.pinned = [];
  run.qa = { ...run.qa, rooms: emptyRoomQa(), room_feasibility_violations: [], rebalance_moves: 6 };
  const ui = await loadedEditor(t, {
    run,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      const data = evaluatedRun(JSON.parse(options.body), run);
      data.qa = { ...run.qa, rebalance_moves: 0 };
      return response(data);
    },
  });
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx');
});

test('report comparison retains real room demand and feasibility changes even with an unchanged fingerprint', async t => {
  const run = savedRun();
  run.pinned = [];
  const ui = await loadedEditor(t, {
    run,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      const data = evaluatedRun(JSON.parse(options.body), run);
      data.qa = { ...run.qa,
        rooms: { ...emptyRoomQa(), total_demand: 31, unassigned_room_sections: [{ course_code: courses[0].course_code, student_count: 31 }] },
        room_feasibility_violations: [{ course_code: courses[0].course_code, student_count: 31 }], rebalance_moves: 0 };
      return response(data);
    },
  });
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('kRoomUnassigned').textContent, '1');
  assert.equal(ui.$('saveLoadedBtn').disabled, false);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
});

test('loading a saved run immediately enables Optimize without a settings edit or Check', async t => {
  const ui = await loadedEditor(t);
  assert.equal(ui.$('optimizeLoadedBtn').disabled, false);
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  assert.equal(ui.$('examEditToolbar').inert, false);
  ui.$('optimizeLoadedBtn').click();
  assert.equal(ui.$('optimizeLoadedBtn').disabled, true);
  assert.equal(ui.$('examEditToolbar').inert, true);
  await settle();
  assert.equal(buildRequests(ui).length, 1, 'Direct Load → Optimize must submit without an intervening action');
  assert.equal(JSON.parse(buildRequests(ui)[0].body).mode, 'optimize_loaded');
  assert.equal(ui.$('optimizeLoadedBtn').disabled, false, 'A failed optimization must permit retry');
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  assert.equal(ui.$('examEditToolbar').inert, false);
});

for (const action of ['build', 'save_loaded_changes', 'optimize_loaded']) {
  for (const succeeds of [true, false]) {
    test(`${action} ${succeeds ? 'success' : 'failure'} restores action controls after leaving busy state`, async t => {
      const options = {
        onRequest: async (url, request) => {
          if (url !== '/ops/exam-timetable/build/') return undefined;
          if (!succeeds) return response({ ok: false, error: 'Temporary failure; retry' });
          const payload = JSON.parse(request.body);
          return response(action === 'build'
            ? { ...savedRun(), run_id: 23, pinned: [] }
            : { ...evaluatedRun(payload), run_id: 23, rebuild_mode: action === 'optimize_loaded' ? 'optimized_from_loaded' : 'loaded_schedule' });
        },
      };
      const ui = action === 'build' ? await page(t, options) : await loadedEditor(t, options);
      if (action === 'save_loaded_changes') dropExam(ui, 'Mon');
      const buttonId = action === 'build' ? 'buildBtn' : action === 'save_loaded_changes' ? 'saveLoadedBtn' : 'optimizeLoadedBtn';
      assert.equal(ui.$(buttonId).disabled, false);
      ui.$(buttonId).click();
      assert.equal(ui.$(buttonId).disabled, true);
      assert.equal(ui.$('examSettingsControls').inert, true);
      await settle();
      assert.equal(buildRequests(ui).length, 1);
      assert.equal(ui.$('examSettingsControls').inert, false);
      assert.equal(ui.$('examEditToolbar').inert, false);
      if (action === 'build' && !succeeds) {
        assert.equal(ui.$('buildBtn').disabled, false);
        assert.equal(ui.$('optimizeLoadedBtn').disabled, true);
      } else {
        assert.equal(ui.$('optimizeLoadedBtn').disabled, false, 'An available loaded timetable can be optimized after finalization');
        assert.equal(ui.$('saveLoadedBtn').disabled, !(action === 'save_loaded_changes' && !succeeds));
      }
    });
  }
}

function workspaceRun() {
  const run = savedRun();
  run.pinned = [];
  run.qa = {
    max_per_day: 2, conflict_count: 2, slots_used: 2, students_over_limit_per_day: 1,
    heavy_day_students: 1, max_credit_load_per_day: 9,
    same_slot_conflicts: [{ student_id: 'S1', slot_index: 1, courses: courses.map(course => course.course_code) }],
    overload_details: [{ student_id: 'S1', day: 'Sun', count: 3, courses: [{ code: courses[1].course_code, credits: 3 }] }],
    heavy_day_details: [{ student_id: 'S1', day: 'Sun', total_credits: 9, penalty: 120, courses: [{ code: courses[1].course_code, credits: 3 }] }],
    rooms: {
      ...emptyRoomQa(),
      unassigned_room_sections: [{ course_code: courses[1].course_code, day: 'Sun', period: '10:30-12:30', section: 'F01', gender: 'F', student_count: 14 }],
      room_double_bookings: [{ slot_index: 1, room_code: 'ROOM-A', courses: courses.map(course => course.course_code) }],
    },
  };
  return run;
}
const deltaText = (ui, id = 'kConflicts') => ui.$(id).parentElement.querySelector('.et-kpi-delta').textContent;
const changedRows = (ui, kind = 'placement') => Array.from(ui.$('examChangesContent').querySelectorAll(`[data-change-kind="${kind}"]`));
const drillAction = (ui, action, identity = courses[1].course_identity) => Array.from(ui.$('kpiDrillBody').querySelectorAll(`[data-${action}-exam]`)).find(button => button.getAttribute(`data-${action}-exam`) === identity);


test('the compact workspace preserves configuration and only collapses setup and full summary for new results', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun() });
  assert.equal(ui.$('examSetupDetails').open, false);
  assert.equal(ui.$('examSummaryDetails').open, false);
  assert.equal(ui.$('etLabel').value, 'Saved regression fixture');
  assert.equal(ui.$('etNumDays').value, '5');
  assert.equal(ui.rows().length, 3);
  assert.match(ui.$('examRunSummary').textContent, /Saved regression fixture.*2.*45/);
  assert.equal(ui.$('examQuickMetrics').querySelector('[data-open-drill="conflicts"] .et-quick-value').textContent, '2');
  assert.equal(ui.$('examQuickMetrics').querySelector('[data-open-drill="overload"] .et-quick-value').textContent, '1');
  assert.equal(ui.$('examQuickMetrics').querySelector('[data-open-drill="room-unassigned"] .et-quick-value').textContent, '1');
  assert.match(ui.$('examLiveMode').textContent, language === 'ar' ? /متوقف/ : /off/);
  ui.$('examSetupDetails').open = true;
  ui.$('examSummaryDetails').open = true;
  ui.$('examChangesDetails').open = true;
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('examSetupDetails').open, true);
  assert.equal(ui.$('examSummaryDetails').open, true);
  assert.equal(ui.$('examChangesDetails').open, true);
  assert.equal(ui.$('etLabel').value, 'Saved regression fixture');
});

test('metric deltas compare each checked result to the saved baseline and never claim unchecked improvements', async t => {
  const run = workspaceRun();
  let checks = 0;
  const ui = await loadedEditor(t, {
    run,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      checks++;
      return response({ ...evaluatedRun(JSON.parse(options.body), run), qa: { ...run.qa, conflict_count: checks === 1 ? 1 : 0 } });
    },
  });
  dropExam(ui, 'Mon');
  assert.doesNotMatch(deltaText(ui), /→|←/);
  assert.ok(ui.$('kConflicts').parentElement.querySelector('.et-kpi-delta').classList.contains('is-stale'));
  assert.match(ui.$('examQuickMetrics').textContent, language === 'ar' ? /سابق/ : /previous/);
  ui.$('checkDraftBtn').click();
  await settle();
  assert.match(deltaText(ui), /2.*1.*−1/);
  dropExam(ui, 'Tue');
  ui.$('checkDraftBtn').click();
  await settle();
  assert.match(deltaText(ui), /2.*0.*−2/, 'Second Check still compares with the saved value 2');
  assert.equal(ui.$('kConflicts').parentElement.querySelector('.et-kpi-delta').classList.contains('is-better'), true);
  assert.equal(ui.$('examQuickMetrics').querySelector('[data-open-drill="conflicts"] .et-quick-delta').textContent, '2 → 0 (−2)');
});

test('metric comparisons pause when timetable input fingerprints change while placement changes remain visible', async t => {
  const run = workspaceRun();
  const ui = await loadedEditor(t, {
    run,
    onRequest: async (url, options) => url === '/ops/exam-timetable/draft-impact/'
      ? response({ ...evaluatedRun(JSON.parse(options.body), run), input_fingerprint: 'new-policy-inputs', qa: { ...run.qa, conflict_count: 0 } }) : undefined,
  });
  dropExam(ui, 'Mon');
  ui.input(ui.$('etMaxPerDay'), '3');
  ui.$('checkDraftBtn').click();
  await settle();
  assert.doesNotMatch(deltaText(ui), /→|←|−2/);
  assert.match(deltaText(ui), language === 'ar' ? /المقارنة متوقفة/ : /Comparison paused/);
  assert.equal(changedRows(ui).length, 1);
  assert.match(changedRows(ui)[0].textContent, /Programming I.*Sun.*10:30-12:30.*Mon.*08:00-10:00/s);
});

test('placement review counts unique canonical exams, separates pins and returns to zero on undo', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun() });
  dropExam(ui, 'Mon', '08:00-10:00', courses[0].course_code);
  dropExam(ui, 'Tue', '13:00-15:00', courses[0].course_code);
  assert.equal(changedRows(ui).length, 1, 'Repeated moves of one course count once');
  dropExam(ui, 'Wed');
  assert.equal(changedRows(ui).length, 2);
  assert.deepEqual(changedRows(ui).map(row => row.dataset.changeIdentity), courses.map(course => course.course_identity));
  assert.match(changedRows(ui)[0].textContent, /Fundamentals of Programming.*Sun.*08:00-10:00.*Tue.*13:00-15:00/s);
  assert.match(changedRows(ui)[1].textContent, /Programming I/);
  examChip(ui, courses[0].course_code).querySelector('[data-exam-pin]').click();
  assert.equal(changedRows(ui, 'pin').length, 1);
  assert.equal(changedRows(ui).length, 2);
  ui.$('undoExamBtn').click();
  assert.equal(changedRows(ui, 'pin').length, 0);
  ui.$('undoExamBtn').click();
  assert.equal(changedRows(ui).length, 1);
  ui.$('undoExamBtn').click();
  assert.equal(changedRows(ui).length, 1);
  ui.$('undoExamBtn').click();
  assert.equal(changedRows(ui).length, 0);
  assert.equal(changedRows(ui, 'pin').length, 0);
  assert.match(ui.$('examChangeCount').textContent, /^0.*0/);
  ui.$('redoExamBtn').click();
  assert.equal(changedRows(ui).length, 1);
});

test('Save establishes a new change-review and metric baseline', async t => {
  const run = workspaceRun();
  const ui = await loadedEditor(t, {
    run,
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/build/' && url !== '/ops/exam-timetable/draft-impact/') return undefined;
      const result = { ...evaluatedRun(JSON.parse(options.body), run), qa: { ...run.qa, conflict_count: 1 } };
      if (url === '/ops/exam-timetable/build/') result.run_id = 24;
      return response(result);
    },
  });
  dropExam(ui, 'Tue');
  ui.$('saveLoadedBtn').click();
  await settle();
  assert.equal(changedRows(ui).length, 0);
  assert.equal(changedRows(ui, 'pin').length, 0);
  assert.match(deltaText(ui), /1.*1/);
  assert.equal(ui.$('examChangesDetails').open, false);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/24/export.xlsx');
});

test('Find from conflicts targets the exact same-code course while preserving search, selection and requests', async t => {
  const run = workspaceRun();
  run.buckets_summary = [
    { program: 'AI', programme_term: 1, courses: [courses[0].course_code] },
    { program: 'CS', programme_term: 2, courses: [courses[1].course_code] },
  ];
  const ui = await loadedEditor(t, { run });
  ui.select('examReviewMode', 'term');
  ui.select('examReviewPlan', 'AI');
  ui.input(ui.$('schedFilter'), 'NOT-A-MATCH');
  ui.$('examQuickMetrics').querySelector('[data-open-drill="conflicts"]').click();
  assert.equal(ui.$('kpiDrill').classList.contains('d-none'), false);
  assert.equal(ui.$('examSummaryDetails').open, false, 'Quick detail works outside the collapsed full summary');
  for (const course of courses) {
    const card = Array.from(ui.$('kpiDrillBody').querySelectorAll('.et-drill-course.et-course-card'))
      .find(element => element.querySelector('strong')?.textContent.trim() === course.course_code);
    assert.ok(card?.title.includes(course.course_name), 'Compact detail cards retain the full canonical name in their tooltip');
  }
  const detailCard = identity => Array.from(ui.$('kpiDrillBody').querySelectorAll('[data-course-identity]'))
    .find(card => card.dataset.courseIdentity === identity);
  assert.equal(detailCard(courses[0].course_identity).dataset.examTerm, '1');
  assert.equal(detailCard(courses[1].course_identity).dataset.examTerm, undefined);
  ui.select('examReviewPlan', 'CS');
  assert.equal(detailCard(courses[0].course_identity).dataset.examTerm, undefined);
  assert.equal(detailCard(courses[1].course_identity).dataset.examTerm, '2');
  assert.ok(courses.every(course => !detailCard(course.course_identity).classList.contains('et-review-muted')), 'Review selection must never hide actionable QA details');
  ui.select('examReviewMode', 'neutral');
  assert.ok(courses.every(course => detailCard(course.course_identity).dataset.examTerm === undefined));
  const requests = ui.requests.length;
  const selections = selectedCourses(ui);
  drillAction(ui, 'find').click();
  assert.equal(ui.$('schedFilter').value, 'NOT-A-MATCH');
  assert.equal(ui.window.document.activeElement, examChip(ui));
  assert.equal(examChip(ui).classList.contains('et-found-exam'), true);
  assert.equal(examChip(ui, courses[0].course_code).classList.contains('et-found-exam'), false);
  assert.deepEqual(selectedCourses(ui), selections);
  assert.equal(ui.requests.length, requests);
  assert.match(ui.$('examFindStatus').textContent, /Programming I/);
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(examChip(ui).classList.contains('et-found-exam'), true);
});

for (const type of ['overload', 'heavy', 'room-unassigned', 'room-double']) {
  test(`${type} details resolve exact names and Move uses the shared unpinned command`, async t => {
    const ui = await loadedEditor(t, { run: workspaceRun() });
    ui.window.document.querySelector(`.kpi-click[data-drill="${type}"]`).click();
    assert.match(ui.$('kpiDrillBody').textContent, /Programming I/);
    const move = drillAction(ui, 'move');
    assert.equal(move.disabled, false);
    assert.equal(move.type, 'button');
    assert.ok(move.getAttribute('aria-label').includes(courses[1].course_name));
    assert.ok(move.title.includes(courses[1].course_name), 'Enabled icon actions retain their identifying tooltip');
    const requests = ui.requests.length;
    move.click();
    assert.equal(ui.$('examMoveDialog').hasAttribute('open'), true);
    assert.match(ui.$('examMoveTitle').textContent, /Programming I/);
    assert.equal(ui.$('examMoveDay').value, 'Sun');
    assert.equal(ui.$('examMovePeriod').value, '10:30-12:30');
    ui.select('examMoveDay', 'Thu');
    ui.$('confirmExamMove').click();
    assert.deepEqual(placement(ui), ['Thu', '10:30-12:30']);
    assert.deepEqual(placement(ui, courses[0].course_code), ['Sun', '08:00-10:00']);
    assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 0);
    assert.equal(ui.requests.length, requests);
    assert.equal(ui.$('undoExamBtn').disabled, false);
    assert.equal(drillAction(ui, 'move').disabled, true, 'Details become stale immediately after the move');
    assert.equal(ui.$('examDrillNotice').hidden, false);
  });
}

test('stale drill details keep their checked slot labels and Find uses the current placement', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun() });
  ui.$('kConflicts').closest('.kpi-click').click();
  assert.match(ui.$('kpiDrillBody').textContent, /Sun 10:30-12:30/);
  ui.addPeriod('07:00', '08:00');
  ui.select('examPinCourse', courses[1].course_code);
  ui.select('examPinDay', 'Tue');
  ui.select('examPinPeriod', '07:00-08:00');
  ui.$('applyExamPin').click();
  assert.match(ui.$('kpiDrillBody').textContent, /Sun 10:30-12:30/, 'Old slot_index=1 must still refer to the checked slot');
  assert.equal(ui.$('examDrillNotice').hidden, false);
  drillAction(ui, 'find').click();
  assert.deepEqual(placement(ui), ['Tue', '07:00-08:00']);
  assert.equal(ui.window.document.activeElement, examChip(ui));
  assert.equal(drillAction(ui, 'move').disabled, true);
  assert.match(drillAction(ui, 'move').title, language === 'ar' ? /التثبيت/ : /Unpin/);
});

test('drill actions disable ambiguous, removed, pinned and busy course references with visible reasons', async t => {
  const run = workspaceRun();
  run.pinned = [{ course_code: courses[0].course_code, day: 'Sun', period: '08:00-10:00' }];
  run.qa.same_slot_conflicts[0].courses.push('CS111');
  let finish;
  const ui = await loadedEditor(t, {
    run,
    onRequest: async url => url === '/ops/exam-timetable/build/' ? new Promise(resolve => { finish = () => resolve(response({ ok: false, error: 'Retry' })); }) : undefined,
  });
  ui.$('kConflicts').closest('.kpi-click').click();
  assert.equal(drillAction(ui, 'move', courses[0].course_identity).disabled, true);
  assert.match(drillAction(ui, 'move', courses[0].course_identity).closest('.et-drill-course').textContent, language === 'ar' ? /التثبيت/ : /Unpin/);
  assert.ok(Array.from(ui.$('kpiDrillBody').querySelectorAll('[data-find-exam=""]')).every(button => button.disabled));
  const unchecked = ui.$('courseList').querySelector(`input[value="${courses[1].course_code}"]`);
  unchecked.checked = false;
  ui.emit(unchecked, 'change');
  assert.equal(drillAction(ui, 'find').disabled, true);
  unchecked.checked = true;
  ui.emit(unchecked, 'change');
  ui.$('optimizeLoadedBtn').click();
  assert.equal(drillAction(ui, 'find').disabled, true);
  assert.equal(drillAction(ui, 'move').disabled, true);
  finish();
  await settle();
  assert.equal(drillAction(ui, 'find').disabled, false);
});

test('automatic checks preserve focus in the expanded change review and keep its list current', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun(), liveUpdate: true });
  dropExam(ui, 'Mon');
  ui.$('examChangesDetails').open = true;
  const find = changedRows(ui)[0].querySelector('[data-find-exam]');
  find.focus();
  await pause(520);
  assert.equal(ui.$('examChangesDetails').open, true);
  assert.equal(ui.window.document.activeElement.dataset.findExam, courses[1].course_identity);
  assert.equal(changedRows(ui).length, 1);
  assert.match(changedRows(ui)[0].textContent, /Sun.*Mon/s);
  assert.equal(draftRequests(ui).length, 1);
});

test('user-initiated quick and keyboard drill actions navigate and focus their visible detail heading', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun() });
  ui.scrollCalls.length = 0;
  const quick = ui.$('examQuickMetrics').querySelector('[data-open-drill="conflicts"]');
  quick.click();
  assert.equal(ui.window.document.activeElement, ui.$('kpiDrillTitle'));
  assert.equal(ui.$('kpiDrillTitle').getAttribute('tabindex'), '-1');
  assert.equal(ui.scrollCalls.length, 1);
  assert.equal(ui.scrollCalls[0].element, ui.$('kpiDrillTitle'));
  assert.equal(ui.scrollCalls[0].options.block, 'start');
  ui.$('kpiDrillClose').click();
  assert.equal(ui.window.document.activeElement, quick);
  ui.$('examSummaryDetails').open = true;
  ui.$('kHeavyDay').closest('.kpi-click').dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  assert.equal(ui.window.document.activeElement, ui.$('kpiDrillTitle'));
  assert.equal(ui.$('kpiDrill').dataset.type, 'heavy');
  assert.equal(ui.scrollCalls.length, 2);
});

test('automatic evaluation refreshes an open drill without navigation or focus theft', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun(), liveUpdate: true });
  ui.$('examQuickMetrics').querySelector('[data-open-drill="conflicts"]').click();
  dropExam(ui, 'Tue');
  examChip(ui).focus();
  ui.scrollCalls.length = 0;
  await pause(520);
  assert.equal(draftRequests(ui).length, 1);
  assert.equal(ui.$('kpiDrill').classList.contains('d-none'), false);
  assert.equal(ui.window.document.activeElement, examChip(ui));
  assert.equal(ui.scrollCalls.length, 0, 'A live result refresh must not scroll back to the open detail');
});

test('successful Load navigates once to the editor after its controls are released', async t => {
  const ui = await loadedEditor(t);
  assert.equal(ui.window.document.activeElement, ui.$('examEditHeading'));
  assert.equal(ui.$('examEditToolbar').inert, false);
  const navigation = ui.scrollCalls.filter(call => call.element === ui.$('examScheduleWorkspace'));
  assert.equal(navigation.length, 1);
  assert.equal(navigation[0].options.block, 'start');
});

test('successful Build navigates to the editor while later Save preserves the editing position', async t => {
  const ui = await page(t, {
    onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/build/') return undefined;
      const payload = JSON.parse(options.body);
      return response(payload.base_schedule ? { ...evaluatedRun(payload), run_id: 28 } : { ...savedRun(), run_id: 27, pinned: [] });
    },
  });
  ui.$('buildBtn').click();
  await settle();
  assert.equal(ui.window.document.activeElement, ui.$('examEditHeading'));
  assert.equal(ui.$('examEditToolbar').inert, false);
  assert.equal(ui.scrollCalls.filter(call => call.element === ui.$('examScheduleWorkspace')).length, 1);
  dropExam(ui, 'Mon');
  ui.scrollCalls.length = 0;
  // A click focuses the button, and the button goes inert: focus is lost.
  ui.window.document.activeElement.blur();
  ui.$('saveLoadedBtn').click();
  await settle();
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/28/export.xlsx');
  assert.equal(ui.scrollCalls.length, 0, 'Saving an existing draft must keep the current viewport');
  assert.equal(ui.$('saveLoadedBtn').disabled, true, 'Nothing is unsaved now');
  assert.equal(ui.window.document.activeElement, ui.$('examEditHeading'), 'so focus goes to the board, never the page body');
});

test('numeric comparisons and changed slot labels have isolated left-to-right order in both languages', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun() });
  assert.ok(Array.from(ui.$('examQuickMetrics').querySelectorAll('.et-quick-delta')).every(element => element.dir === 'ltr'));
  dropExam(ui, 'Tue');
  const slots = changedRows(ui)[0].querySelectorAll('.et-change-times bdi');
  assert.equal(slots.length, 2);
  assert.ok(Array.from(slots).every(element => element.dir === 'ltr'));
  assert.equal(slots[0].textContent, 'Sun · 10:30-12:30');
  assert.equal(slots[1].textContent, 'Tue · 08:00-10:00');
});

test('compact course cards keep two-word names and accessible icons without exposing credits or room labels', async t => {
  const run = savedRun();
  run.credit_map = { [courses[0].course_code]: 3, [courses[1].course_code]: 4 };
  run.schedule[0].rooms = [{ room_code: 'SECRET-ROOM-LABEL' }];
  const ui = await loadedEditor(t, { run });
  for (const course of courses) {
    const chip = examChip(ui, course.course_code);
    assert.ok(chip.querySelector('.et-course-short-name').textContent.trim().split(/\s+/).length <= 2);
    assert.equal(chip.querySelector('.et-course-full-name').textContent, course.course_name);
    assert.ok(chip.getAttribute('aria-label').includes(course.course_name));
    assert.ok(chip.title.includes(course.course_name));
    assert.equal(chip.querySelectorAll('.et-credit-tag, .et-room-tag').length, 0);
    for (const button of chip.querySelectorAll('button')) {
      assert.equal(button.type, 'button');
      assert.equal(button.textContent.trim(), '', 'Actions are icons, with accessible names instead of printed labels');
      assert.ok(button.querySelector('svg[aria-hidden="true"]'));
      assert.ok(button.getAttribute('aria-label').includes(course.course_name));
      assert.ok(button.title.includes(course.course_name));
    }
  }
  assert.equal(examChip(ui, courses[0].course_code).querySelector('.et-course-short-name').textContent, 'Fundamentals of…');
  assert.ok(examChip(ui, courses[0].course_code).querySelector('.et-pin-lock'));
  assert.ok(examChip(ui).querySelector('.et-course-online[role="img"][aria-label]'));
  assert.doesNotMatch(ui.$('schedGrid').textContent, /SECRET-ROOM-LABEL|3cr|4cr/);
  dropExam(ui, 'Mon');
  ui.$('checkDraftBtn').click();
  await settle();
  const payload = JSON.parse(draftRequests(ui)[0].body);
  assert.deepEqual(payload.base_schedule.map(entry => entry.course_name), courses.map(course => course.course_name), 'Name abbreviation is presentation only');
});

test('compact names skip copied number prefixes without changing full names or letter-bearing words', async t => {
  for (const [name, expected] of [
    [') 321 COMPUTER ORGANIZATION & ARCHITECTURE II', 'COMPUTER ORGANIZATION…'],
    [') 224 DIGITAL LOGIC DESIGN', 'DIGITAL LOGIC…'],
    ['345 WEB APPLICATION DEVELOPMENT', 'WEB APPLICATION…'],
    ['( ٣٢١ هندسة البرمجيات المتقدمة', 'هندسة البرمجيات…'],
    ['3D COMPUTER GRAPHICS', '3D COMPUTER…'],
    ['C++ PROGRAMMING', 'C++ PROGRAMMING'],
  ]) {
    const run = savedRun();
    run.schedule[0].course_name = name;
    const ui = await loadedEditor(t, { run });
    const chip = examChip(ui, courses[0].course_code);
    assert.equal(chip.querySelector('.et-course-short-name').textContent, expected);
    assert.equal(chip.querySelector('.et-course-full-name').textContent, name);
    assert.ok(chip.getAttribute('aria-label').includes(name));
    assert.ok(chip.title.includes(name));
    ui.$('checkDraftBtn').click();
    await settle();
    assert.equal(JSON.parse(draftRequests(ui)[0].body).base_schedule[0].course_name, name);
  }
});

test('the full-height schedule uses native table header semantics and accessible editing controls', async t => {
  const run = workspaceRun();
  run.schedule[1].day = 'OVERFLOW';
  run.schedule[1].period = 'OVERFLOW';
  const ui = await loadedEditor(t, { run });
  const table = ui.$('schedGrid').querySelector('table.et-grid');
  assert.ok(table.caption.textContent.trim());
  assert.equal(table.tHead.rows.length, 1);
  assert.ok(Array.from(table.tHead.querySelectorAll('th')).every(header => header.scope === 'col'));
  assert.ok(Array.from(table.tBodies[0].querySelectorAll('th')).every(header => header.scope === 'row'));
  assert.ok(Array.from(table.tBodies[0].querySelectorAll('th')).every(header => header.querySelector('.et-grid-day-label')));
  assert.ok(examChip(ui).closest('.et-slot-courses'));
  assert.equal(examChip(ui).querySelector('[data-exam-pin]').disabled, true, 'An overflow exam cannot be pinned without an assigned slot');
  assert.equal(examChip(ui).querySelector('[data-exam-move]').disabled, false);
  assert.equal(ui.$('schedGrid').getAttribute('role'), 'region');
  assert.equal(ui.$('schedGrid').tabIndex, 0);
  assert.ok(ui.$(ui.$('schedGrid').getAttribute('aria-labelledby')));
  assert.equal(ui.$('examEditOptions'), null);
  assert.equal(ui.$('examLiveUpdate').closest('details'), null);
  assert.equal(ui.$('optimizeLoadedBtn').closest('details'), null);
  assert.equal(ui.$('examEditToolbar').closest('#examScheduleWorkspace'), ui.$('examScheduleWorkspace'));
});

test('manual moves, Undo and live evaluation anchor a visible course and retain negative RTL grid offsets', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun(), liveUpdate: true });
  const main = ui.$('main-content');
  main.style.overflowY = 'auto';
  Object.defineProperty(main, 'scrollHeight', { value: 5000, configurable: true });
  Object.defineProperty(main, 'clientHeight', { value: 700, configurable: true });
  main.getBoundingClientRect = () => ({ top: 0, bottom: 700, left: 0, right: 1100, width: 1100, height: 700 });
  main.scrollTop = 1850;
  const grid = ui.$('schedGrid');
  grid.getBoundingClientRect = () => ({ top: 250 - main.scrollTop, bottom: 4250 - main.scrollTop, left: 100, right: 1000, width: 900, height: 4000 });
  let emptyPeriod = false;
  const originalRect = ui.window.HTMLElement.prototype.getBoundingClientRect;
  ui.window.HTMLElement.prototype.getBoundingClientRect = function () {
    const earlierRowShrank = placement(ui)[0] === 'Tue';
    if (emptyPeriod && this.matches('#schedGrid tbody tr')) {
      const visibleDay = this.querySelector('th[scope="row"]').textContent === 'Thu';
      const top = grid.getBoundingClientRect().top + (visibleDay ? 1800 - (earlierRowShrank ? 120 : 0) : 100);
      return { top, bottom: top + 300, left: 100, right: 1000, width: 900, height: 300 };
    }
    if (!this.classList.contains('et-course')) return originalRect.call(this);
    const offset = this.dataset.course === courses[0].course_code ? 1800 - (earlierRowShrank ? 120 : 0) : 100;
    const top = grid.getBoundingClientRect().top + offset;
    return { top, bottom: top + 60, left: emptyPeriod ? 1300 : 300, right: emptyPeriod ? 1480 : 480, width: 180, height: 60 };
  };
  const visibleCourseTop = () => examChip(ui, courses[0].course_code).getBoundingClientRect().top;
  assert.equal(visibleCourseTop(), 200);
  grid.scrollLeft = language === 'ar' ? -420 : 420;
  examChip(ui).querySelector('[data-exam-pin]').focus();
  ui.scrollCalls.length = 0;
  dropExam(ui, 'Tue');
  assert.equal(main.scrollTop, 1730);
  assert.equal(visibleCourseTop(), 200);
  assert.equal(grid.scrollTop, 0);
  assert.equal(grid.scrollLeft, language === 'ar' ? -420 : 420);
  await pause(520);
  assert.equal(draftRequests(ui).length, 1);
  assert.equal(main.scrollTop, 1730);
  assert.equal(visibleCourseTop(), 200);
  assert.equal(grid.scrollTop, 0);
  assert.equal(grid.scrollLeft, language === 'ar' ? -420 : 420);
  assert.equal(ui.window.document.activeElement, examChip(ui).querySelector('[data-exam-pin]'));
  ui.$('undoExamBtn').click();
  assert.equal(main.scrollTop, 1850);
  assert.equal(visibleCourseTop(), 200, 'Undo must compensate for an earlier row expanding again');
  assert.equal(grid.scrollTop, 0);
  assert.equal(grid.scrollLeft, language === 'ar' ? -420 : 420);
  emptyPeriod = true;
  const visibleDayTop = () => Array.from(grid.querySelectorAll('tbody tr'))
    .find(row => row.querySelector('th[scope="row"]').textContent === 'Thu').getBoundingClientRect().top;
  dropExam(ui, 'Tue');
  assert.equal(main.scrollTop, 1730);
  assert.equal(visibleDayTop(), 200, 'An empty visible period anchors its day when no course card is horizontally visible');
  ui.$('undoExamBtn').click();
  assert.equal(main.scrollTop, 1850);
  assert.equal(visibleDayTop(), 200);
  assert.equal(grid.scrollTop, 0);
  assert.equal(grid.scrollLeft, language === 'ar' ? -420 : 420);
  assert.equal(ui.scrollCalls.length, 0, 'A check or Undo must not explicitly navigate the page');
});

test('Find uses page navigation and corrects only sticky day-column occlusion horizontally', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun() });
  ui.$('kConflicts').closest('.kpi-click').click();
  const main = ui.$('main-content');
  main.style.overflowY = 'auto';
  Object.defineProperty(main, 'scrollHeight', { value: 4000, configurable: true });
  Object.defineProperty(main, 'clientHeight', { value: 700, configurable: true });
  main.getBoundingClientRect = () => ({ top: 0, bottom: 700, left: 0, right: 1100, width: 1100, height: 700 });
  main.scrollTop = 1200;
  ui.$('examEditToolbar').getBoundingClientRect = () => ({ top: 24, bottom: 164, left: 100, right: 1000, width: 900, height: 140 });
  const grid = ui.$('schedGrid');
  grid.scrollLeft = language === 'ar' ? -300 : 300;
  grid.getBoundingClientRect = () => ({ top: 100, bottom: 600, left: 100, right: 900, width: 800, height: 500 });
  grid.querySelector('thead').getBoundingClientRect = () => ({ height: 60 });
  const chip = examChip(ui);
  chip.closest('tr').querySelector('th').getBoundingClientRect = () => ({ width: 80 });
  chip.getBoundingClientRect = () => ({ top: 120, bottom: 165, left: language === 'ar' ? 790 : 130, right: language === 'ar' ? 870 : 210, width: 80, height: 45 });
  ui.window.dispatchEvent(new ui.window.Event('resize'));
  assert.equal(grid.style.getPropertyValue('--exam-grid-row-header-width'), '80px');
  ui.scrollCalls.length = 0;
  drillAction(ui, 'find').click();
  assert.equal(ui.window.document.activeElement, chip);
  assert.equal(ui.scrollCalls[0].element, chip);
  assert.equal(main.scrollTop, 1148, 'Find clears the actual sticky toolbar even with a 24px page-padding inset');
  assert.equal(grid.scrollTop, 0, 'Period headings scroll away; Find must not create vertical grid scrolling');
  assert.equal(grid.scrollLeft, language === 'ar' ? -242 : 242, 'Horizontal correction respects the sticky row header and signed RTL scrolling');
});

test('confirming a dialog Move reveals the new position without making live checks navigate', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun() });
  examChip(ui).querySelector('[data-exam-move]').click();
  ui.select('examMoveDay', 'Thu');
  ui.scrollCalls.length = 0;
  ui.$('confirmExamMove').click();
  assert.deepEqual(placement(ui), ['Thu', '10:30-12:30']);
  assert.equal(ui.window.document.activeElement, examChip(ui).querySelector('[data-exam-move]'));
  assert.equal(ui.scrollCalls.length, 1);
  assert.equal(ui.scrollCalls[0].element, examChip(ui));
  ui.scrollCalls.length = 0;
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.scrollCalls.length, 0);
});

for (const visible of [true, false]) {
  test(`live checking ${visible ? 'anchors a visible later course' : 'does not navigate an offscreen timetable'} when preceding details and rows shrink`, async t => {
    const run = workspaceRun();
    let completeCheck;
    const ui = await loadedEditor(t, {
      run, liveUpdate: true,
      onRequest: async (url, options) => url === '/ops/exam-timetable/draft-impact/'
        ? new Promise(resolve => { completeCheck = () => resolve(response({ ...evaluatedRun(JSON.parse(options.body), run), qa: { ...run.qa, conflict_count: 0, same_slot_conflicts: [] } })); }) : undefined,
    });
    ui.$('kConflicts').closest('.kpi-click').click();
    const main = ui.$('main-content');
    main.style.overflowY = 'auto';
    Object.defineProperty(main, 'scrollHeight', { value: 5000, configurable: true });
    Object.defineProperty(main, 'clientHeight', { value: 700, configurable: true });
    main.getBoundingClientRect = () => ({ top: 0, bottom: 700, left: 0, right: 1100, width: 1100, height: 700 });
    main.scrollTop = 2800;
    const grid = ui.$('schedGrid');
    grid.scrollLeft = language === 'ar' ? -210 : 210;
    // Loading first shortens the toolbar by 32px; the returned report then
    // shrinks the preceding conflict detail by 280px and an earlier timetable
    // row by 120px. Anchoring only the table's top would lose the visible course.
    grid.getBoundingClientRect = () => {
      const checked = ui.$('kConflicts').textContent === '0';
      const compactToolbar = ui.$('etResults').dataset.calculationState === 'loading' || checked;
      const contentTop = (visible ? 1000 : 3700) - (compactToolbar ? 32 : 0) - (checked ? 280 : 0);
      const top = contentTop - main.scrollTop;
      return { top, bottom: top + 4000, left: 100, right: 1000, width: 900, height: 4000 };
    };
    const originalRect = ui.window.HTMLElement.prototype.getBoundingClientRect;
    ui.window.HTMLElement.prototype.getBoundingClientRect = function () {
      if (this.matches('#schedGrid tbody th[scope="row"]')) return { top: 0, bottom: 700, left: language === 'ar' ? 920 : 100, right: language === 'ar' ? 1000 : 180, width: 80, height: 700 };
      if (!this.classList.contains('et-course')) return originalRect.call(this);
      const checked = ui.$('kConflicts').textContent === '0';
      const occluded = this.dataset.course === courses[0].course_code;
      const offset = occluded ? 2000 : 2000 - (checked ? 120 : 0);
      const top = grid.getBoundingClientRect().top + offset;
      // The first DOM card is vertically visible but entirely covered by the
      // sticky day column. It must never become the viewport's course anchor.
      return { top, bottom: top + 60, left: occluded ? (language === 'ar' ? 930 : 100) : 300,
        right: occluded ? (language === 'ar' ? 1000 : 170) : 480, width: occluded ? 70 : 180, height: 60 };
    };
    const initialTop = examChip(ui).getBoundingClientRect().top;
    ui.scrollCalls.length = 0;
    dropExam(ui, 'Mon');
    await pause(520);
    assert.equal(draftRequests(ui).length, 1);
    assert.equal(ui.$('etResults').dataset.calculationState, 'loading');
    assert.equal(main.scrollTop, visible ? 2768 : 2800);
    if (visible) assert.equal(examChip(ui).getBoundingClientRect().top, initialTop, 'Pending to loading keeps the same course position');
    completeCheck();
    await settle();
    assert.equal(ui.$('kConflicts').textContent, '0');
    assert.equal(ui.$('kpiDrill').classList.contains('d-none'), false);
    assert.equal(main.scrollTop, visible ? 2368 : 2800);
    if (visible) assert.equal(examChip(ui).getBoundingClientRect().top, initialTop, 'A preceding row-height change must not displace the visible course');
    assert.equal(grid.scrollTop, 0);
    assert.equal(grid.scrollLeft, language === 'ar' ? -210 : 210);
    assert.equal(ui.scrollCalls.length, 0, 'Anchoring never invokes explicit navigation');
  });
}

test('period column sizing follows the evaluated period count and resets for an empty schedule', async t => {
  const ui = await loadedEditor(t, { run: workspaceRun() });
  assert.equal(ui.$('schedGrid').style.getPropertyValue('--exam-period-count'), '3');
  ui.addPeriod('16', '17');
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('schedGrid').style.getPropertyValue('--exam-period-count'), '4');
  assert.equal(ui.$('schedGrid').querySelectorAll('thead th[scope="col"]').length, 5);
  const empty = await loadedEditor(t, { run: { ...savedRun(), pinned: [], schedule: [], courses: [], courses_count: 0 } });
  assert.equal(empty.$('schedGrid').style.getPropertyValue('--exam-period-count'), '1');
});

test('selection presets dismiss after action, Escape or outside click without trapping focus', async t => {
  const ui = await page(t, { initialCourses: selectionCourses });
  const menu = ui.$('selectComputerCourses').closest('details');
  const summary = menu.querySelector('summary');
  for (const id of ['selectComputerCourses', 'selectGeneralCourses', 'toggleAllCourses', 'toggleOnlineCourses']) {
    menu.open = true;
    ui.$(id).focus();
    ui.$(id).click();
    assert.equal(menu.open, false);
    assert.equal(ui.window.document.activeElement, summary);
  }
  menu.open = true;
  ui.$('selectComputerCourses').focus();
  ui.$('selectComputerCourses').dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(menu.open, false);
  assert.equal(ui.window.document.activeElement, summary);
  menu.open = true;
  ui.$('courseSearch').focus();
  ui.$('courseSearch').click();
  assert.equal(menu.open, false);
  assert.equal(ui.window.document.activeElement, ui.$('courseSearch'));
});

for (const action of ['buildBtn', 'checkDraftBtn', 'saveLoadedBtn', 'optimizeLoadedBtn']) {
  test(`${action} rejects invalid tiny-course thresholds without changing their value or sending requests`, async t => {
    const ui = action === 'buildBtn' ? await page(t) : await loadedEditor(t);
    ui.$('etRelaxThin').checked = true;
    ui.emit(ui.$('etRelaxThin'), 'change');
    assert.equal(ui.$('etThinThreshold').disabled, false);
    for (const invalid of ['', '0', '-1', '11', '2.5', 'four', '4students']) {
      ui.input(ui.$('etThinThreshold'), invalid);
      const before = ui.requests.length;
      ui.$(action).click();
      await settle();
      assert.equal(ui.requests.length, before, invalid);
      assert.equal(ui.$('etThinThreshold').value, invalid);
      assert.equal(ui.window.document.activeElement, ui.$('etThinThreshold'));
      assert.ok(/1.*10/.test(ui.$('etStatus').textContent));
    }
  });
}

test('Shuffle and tiny-course policy post their exact enabled values and disable relaxation explicitly', async t => {
  const ui = await page(t);
  ui.$('etRandomize').checked = false;
  ui.emit(ui.$('etRandomize'), 'change');
  ui.$('etRelaxThin').checked = true;
  ui.emit(ui.$('etRelaxThin'), 'change');
  for (const threshold of [1, 10]) {
    ui.input(ui.$('etThinThreshold'), String(threshold));
    ui.$('buildBtn').click();
    await settle();
    const payload = JSON.parse(buildRequests(ui).at(-1).body);
    assert.equal(payload.thin_conflict_threshold, threshold);
    assert.equal(payload.randomize, false);
  }
  ui.input(ui.$('etThinThreshold'), 'invalid');
  ui.$('etRelaxThin').checked = false;
  ui.emit(ui.$('etRelaxThin'), 'change');
  assert.equal(ui.$('etThinThreshold').disabled, true);
  ui.$('buildBtn').click();
  await settle();
  assert.equal(JSON.parse(buildRequests(ui).at(-1).body).thin_conflict_threshold, 0);
});

function matrixRun() {
  const run = workspaceRun();
  const third = { course_code: 'GS999 (1)', course_name: 'Third Course', course_identity: 'gs999-third', programs: ['GS'] };
  run.courses.push(third.course_code);
  run.schedule.push({ ...third, ...run.slots[2], slot_index: 2, rooms: [] });
  run.courses_count = 3;
  run.conflicts = [{ course_a: courses[0].course_code, course_b: courses[1].course_code, shared: 3 }];
  return run;
}

test('matrix mouse and keyboard actions highlight exact aliases without changing course selection', async t => {
  const ui = await loadedEditor(t, { run: matrixRun() });
  const selected = selectedCourses(ui);
  const requests = ui.requests.length;
  ui.$('toggleMatrix').click();
  assert.equal(ui.$('toggleMatrix').getAttribute('aria-expanded'), 'true');
  const table = ui.$('matrixGrid').querySelector('table');
  table.tHead.rows[0].cells[1].click();
  assert.equal(ui.$('schedFilter').value, courses[0].course_code);
  assert.deepEqual(Array.from(ui.$('schedGrid').querySelectorAll('.et-highlight'), chip => chip.dataset.course), [courses[0].course_code]);
  assert.equal(ui.window.document.activeElement, examChip(ui, courses[0].course_code));
  const pair = table.tBodies[0].rows[0].cells[2];
  pair.focus();
  pair.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true }));
  assert.deepEqual(Array.from(ui.$('schedGrid').querySelectorAll('.et-highlight'), chip => chip.dataset.course), courses.map(course => course.course_code));
  table.tBodies[0].rows[1].cells[0].dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  assert.deepEqual(Array.from(ui.$('schedGrid').querySelectorAll('.et-highlight'), chip => chip.dataset.course), [courses[1].course_code]);
  ui.input(ui.$('schedFilter'), 'CS*');
  assert.equal(ui.$('schedGrid').querySelectorAll('.et-highlight').length, 2);
  ui.input(ui.$('schedFilter'), '');
  assert.equal(ui.$('schedGrid').querySelectorAll('.et-highlight, .et-dim').length, 0);
  assert.deepEqual(selectedCourses(ui), selected);
  assert.equal(ui.requests.length, requests);
  ui.$('toggleMatrix').click();
  assert.equal(ui.$('toggleMatrix').getAttribute('aria-expanded'), 'false');
});

test('matrix labels remain literal and unrelated edges do not prevent the timetable from rendering', async t => {
  const run = matrixRun();
  const special = 'X<literal> "1"';
  run.courses[2] = special;
  run.schedule[2].course_code = special;
  run.conflicts.push({ course_a: 'REMOVED', course_b: courses[0].course_code, shared: 1 });
  const ui = await loadedEditor(t, { run });
  const header = ui.$('matrixGrid').querySelector('thead tr').lastElementChild;
  assert.equal(header.textContent, special);
  assert.equal(header.querySelector('literal'), null);
  assert.equal(ui.$('schedGrid').querySelectorAll('.et-course').length, 3);
});

test('matrix zoom has enforced limits and Fit is idempotent after any prior zoom', async t => {
  const ui = await loadedEditor(t, { run: matrixRun() });
  Object.defineProperty(ui.$('etMatrixViewport'), 'clientWidth', { value: 800, configurable: true });
  Object.defineProperty(ui.$('etMatrixScaler'), 'scrollWidth', { value: 1000, configurable: true });
  for (let i = 0; i < 15; i++) ui.$('etZoomIn').click();
  assert.equal(ui.$('etZoomLevel').textContent, '200%');
  assert.equal(ui.$('etZoomIn').disabled, true);
  for (let i = 0; i < 4; i++) {
    ui.$('etZoomFit').click();
    assert.equal(ui.$('etZoomLevel').textContent, '80%');
  }
  for (let i = 0; i < 20; i++) ui.$('etZoomOut').click();
  assert.equal(ui.$('etZoomLevel').textContent, '50%');
  assert.equal(ui.$('etZoomOut').disabled, true);
  ui.$('etZoomFit').click();
  assert.equal(ui.$('etZoomLevel').textContent, '80%');
  Object.defineProperty(ui.$('etMatrixScaler'), 'scrollWidth', { value: 4000, configurable: true });
  ui.$('etZoomFit').click();
  assert.equal(ui.$('etZoomLevel').textContent, '20%', 'Fit can display a full college-size matrix');
  assert.equal(ui.$('etZoomOut').disabled, true);
  ui.$('etZoomIn').click();
  assert.equal(ui.$('etZoomLevel').textContent, '30%');
  ui.$('etZoomOut').click();
  assert.equal(ui.$('etZoomLevel').textContent, '20%');
  Object.defineProperty(ui.$('etMatrixScaler'), 'scrollWidth', { value: 0, configurable: true });
  ui.$('etZoomFit').click();
  assert.equal(ui.$('etZoomLevel').textContent, '20%', 'Hidden/empty geometry does not produce an invalid scale');
});

test('fullscreen matrix restores focus, background and Escape behavior after button exits', async t => {
  const ui = await loadedEditor(t, { run: matrixRun() });
  const panel = ui.$('matrixPanel');
  const parent = panel.parentElement;
  const button = ui.$('matrixFullscreen');
  ui.window.document.body.style.overflow = 'clip';
  for (let i = 0; i < 3; i++) {
    button.focus();
    button.click();
    assert.equal(panel.parentElement, ui.window.document.body);
    assert.equal(panel.getAttribute('aria-modal'), 'true');
    assert.equal(ui.$('toggleMatrix').getAttribute('aria-expanded'), 'true');
    assert.equal(ui.window.document.activeElement, button);
    const reverseTab = new ui.window.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true });
    button.dispatchEvent(reverseTab);
    assert.equal(reverseTab.defaultPrevented, true);
    assert.ok(panel.contains(ui.window.document.activeElement));
    if (i < 2) button.click();
    else ui.window.document.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    assert.equal(panel.parentElement, parent);
    assert.equal(panel.classList.contains('matrix-fullscreen'), false);
    assert.equal(panel.hasAttribute('aria-modal'), false);
    assert.equal(ui.window.document.body.style.overflow, 'clip');
    assert.equal(ui.window.document.activeElement, button);
    assert.ok(!ui.window.document.querySelector('main').closest('[inert]'));
  }
  ui.window.document.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(panel.classList.contains('matrix-fullscreen'), false, 'No old Escape listener reopens the panel');
  button.click();
  ui.$('matrixGrid').querySelector('thead [role="button"]').click();
  assert.equal(panel.classList.contains('matrix-fullscreen'), false, 'A matrix action exposes its destination');
  assert.equal(ui.window.document.activeElement, examChip(ui, courses[0].course_code));
});

test('matrix scrolling and expanded state survive a complete Check', async t => {
  const ui = await loadedEditor(t, { run: matrixRun() });
  ui.$('toggleMatrix').click();
  ui.$('etMatrixViewport').scrollTop = 123;
  ui.$('etMatrixViewport').scrollLeft = language === 'ar' ? -240 : 240;
  const matrixKey = ui.$('matrixGrid').querySelector('tbody [data-matrix-key]').dataset.matrixKey;
  ui.$('matrixGrid').querySelector('tbody [data-matrix-key]').focus();
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('toggleMatrix').getAttribute('aria-expanded'), 'true');
  assert.equal(ui.$('etMatrixViewport').scrollTop, 123);
  assert.equal(ui.$('etMatrixViewport').scrollLeft, language === 'ar' ? -240 : 240);
  assert.equal(ui.window.document.activeElement.dataset.matrixKey, matrixKey);
});

test('history pagination announces the current page and ignores older requests arriving last', async t => {
  const pending = new Map();
  const pageData = page => ({ ok: true, page, total_pages: 9, total: 89, runs: [{ id: page, label: `Page ${page} run` }] });
  const ui = await page(t, { loadCourses: false, onRequest: async url => {
    if (!url.startsWith('/ops/exam-timetable/list/')) return undefined;
    const number = Number(new URL(url, 'http://exam.test').searchParams.get('page'));
    return number === 1 ? response(pageData(1)) : new Promise(resolve => pending.set(number, resolve));
  } });
  const buttons = Array.from(ui.$('historyPages').querySelectorAll('button'));
  assert.ok(buttons.every(button => button.type === 'button' && button.getAttribute('aria-label')));
  assert.equal(buttons[0].disabled, true);
  assert.equal(ui.$('historyPages').querySelector('[aria-current="page"]').textContent, '1');
  ui.$('historyPages').querySelector('[data-history-page="2"]').click();
  ui.$('historyPages').querySelector('[data-history-page="8"]').click();
  assert.equal(ui.$('historyList').getAttribute('aria-busy'), 'true');
  pending.get(8)(response(pageData(8)));
  await settle();
  pending.get(2)(response(pageData(2)));
  await settle();
  assert.ok(ui.$('historyList').textContent.includes('Page 8 run'));
  assert.equal(ui.$('historyPages').querySelector('[aria-current="page"]').textContent, '8');
  assert.equal(ui.$('historyList').hasAttribute('aria-busy'), false);
  assert.ok(ui.$('historyShowing').textContent.includes('71-80'));
});

test('confirmed deletion of the loaded run resets its draft, export and editor without destroying the reusable screen', async t => {
  const ui = await loadedEditor(t, { onRequest: async url => url.endsWith('/delete/') ? response({ ok: true }) : undefined });
  dropExam(ui, 'Mon');
  ui.window.dlg.confirm = async () => true;
  ui.$('historyList').querySelector('.et-del-btn').click();
  await settle();
  const removed = ui.requests.find(request => request.url.endsWith('/delete/'));
  assert.deepEqual(JSON.parse(removed.body), { confirm: 'DELETE' });
  assert.equal(ui.$('etResults').classList.contains('d-none'), true);
  assert.equal(ui.$('exportXlsx').hasAttribute('href'), false);
  assert.equal(ui.$('examSetupDetails').open, true);
  assert.equal(ui.$('buildBtn').disabled, false);
  assert.equal(ui.$('undoExamBtn').disabled, true);
  const leaving = new ui.window.Event('beforeunload', { cancelable: true });
  ui.window.dispatchEvent(leaving);
  assert.equal(leaving.defaultPrevented, false);
});

for (const type of ['thin-courses', 'thin-clash', 'multi-sitting']) {
  test(`${type} summary control opens its own checked details with exact course actions`, async t => {
    const run = workspaceRun();
    Object.assign(run.qa, {
      thin_threshold: 4,
      thin_courses: [{ course_code: courses[1].course_code, total_students: 4, dropped_edges: 1, neighbours: [courses[0].course_code] }],
      thin_clash_risk: [{ student_id: 'THIN-1', slot_index: 1, courses: [courses[0].course_code, courses[1].course_code] }],
      multi_sitting_sections: 1,
      multi_sitting_details: [{ course_code: courses[1].course_code, course_identity: courses[1].course_identity, section: 'F01', enrolment: 14, max_room_cap: 10, sittings: 2, slots: [0, 1], rooms: ['R1', 'R2'], incomplete: false }],
    });
    const ui = await loadedEditor(t, { run });
    ui.$('examSummaryDetails').open = true;
    ui.window.document.querySelector(`[data-drill="${type}"]`).click();
    assert.equal(ui.$('kpiDrill').dataset.type, type);
    assert.ok(ui.$('kpiDrillBody').textContent.includes(courses[1].course_name));
    const find = drillAction(ui, 'find');
    assert.equal(find.disabled, false);
    find.click();
    assert.equal(ui.window.document.activeElement, examChip(ui));
    ui.$('kpiDrillClose').click();
    assert.equal(ui.$('kpiDrill').classList.contains('d-none'), true);
  });
}

test('required setup values reject empty, out-of-range and noninteger input before Build', async t => {
  const ui = await page(t);
  for (const [id, invalid, restored] of [
    ['etLabel', '  ', 'Valid name'],
    ['etNumDays', '0', '5'], ['etNumDays', '61', '5'], ['etNumDays', '2.5', '5'],
    ['etMaxPerDay', '0', '2'], ['etMaxPerDay', '11', '2'], ['etMaxPerDay', '2.5', '2'],
  ]) {
    ui.input(ui.$(id), invalid);
    ui.$('buildBtn').click();
    await settle();
    assert.equal(buildRequests(ui).length, 0, `${id}: ${invalid}`);
    assert.equal(ui.window.document.activeElement, ui.$(id));
    ui.input(ui.$(id), restored);
  }
});

for (const scope of ['progList', 'secList']) {
  test(`an empty ${scope} cannot reload everyone and its canceled discard restores the displayed count`, async t => {
    const ui = await loadedEditor(t);
    dropExam(ui, 'Mon');
    const count = ui.$(scope === 'progList' ? 'progCount' : 'secCount');
    const before = count.textContent;
    const box = ui.$(scope).querySelector('input');
    box.checked = false;
    ui.emit(box, 'change');
    await settle();
    assert.equal(count.textContent, before);
    assert.equal(box.checked, true);
    ui.window.dlg.confirm = async () => true;
    for (const checkbox of ui.$(scope).querySelectorAll('input')) {
      checkbox.checked = false;
      ui.emit(checkbox, 'change');
      await settle();
    }
    assert.equal(ui.$('etResults').classList.contains('d-none'), true);
    const requests = ui.requests.length;
    ui.$('loadCoursesBtn').click();
    await settle();
    assert.equal(ui.requests.length, requests);
    assert.ok(count.textContent.includes('0/'));
  });
}

test('both Move dialog cancel paths preserve placement, focus and saved state', async t => {
  const ui = await loadedEditor(t);
  const before = placement(ui);
  const requests = ui.requests.length;
  for (const escape of [false, true]) {
    examChip(ui).querySelector('[data-exam-move]').click();
    ui.select('examMoveDay', 'Thu');
    if (escape) ui.$('examMoveDialog').dispatchEvent(new ui.window.Event('cancel', { cancelable: true }));
    else ui.$('cancelExamMove').click();
    assert.deepEqual(placement(ui), before);
    assert.equal(ui.window.document.activeElement, examChip(ui).querySelector('[data-exam-move]'));
    assert.equal(ui.$('undoExamBtn').disabled, true);
    assert.equal(ui.$('saveLoadedBtn').disabled, true);
    assert.equal(ui.$('examMoveDialog').open, false);
  }
  assert.equal(ui.requests.length, requests);
});

test('both Unpin all controls and individual fixed-row removal share undoable pin commands', async t => {
  const ui = await loadedEditor(t, { run: savedRun() });
  examChip(ui).querySelector('[data-exam-pin]').click();
  for (const id of ['clearExamPins', 'clearPins']) {
    ui.$(id).click();
    assert.equal(ui.$('examPinRows').querySelectorAll('[data-pin-remove]').length, 0);
    ui.$('undoExamBtn').click();
    assert.equal(ui.$('examPinRows').querySelectorAll('[data-pin-remove]').length, 2);
  }
  ui.$('examPinRows').querySelector('[data-pin-remove]').click();
  assert.equal(ui.$('examPinRows').querySelectorAll('[data-pin-remove]').length, 1);
  ui.$('undoExamBtn').click();
  assert.equal(ui.$('examPinRows').querySelectorAll('[data-pin-remove]').length, 2);
});

test('failed history paging retains the current list, clears busy state and remains retryable', async t => {
  let fail = true;
  const ui = await page(t, { loadCourses: false, onRequest: async url => {
    if (!url.startsWith('/ops/exam-timetable/list/')) return undefined;
    const number = Number(new URL(url, 'http://exam.test').searchParams.get('page'));
    if (number === 2 && fail) return response({ ok: false, error: 'History unavailable' });
    return response({ ok: true, page: number, total_pages: 2, total: 11, runs: [{ id: number, label: `Run ${number}` }] });
  } });
  const errors = [];
  ui.window.notify.error = (...message) => errors.push(message);
  ui.$('historyPages').querySelector('[data-history-page="2"]').click();
  await settle();
  assert.equal(errors.length, 1);
  assert.ok(ui.$('historyList').textContent.includes('Run 1'));
  assert.equal(ui.$('historyList').hasAttribute('aria-busy'), false);
  fail = false;
  ui.$('historyPages').querySelector('[data-history-page="2"]').click();
  await settle();
  assert.ok(ui.$('historyList').textContent.includes('Run 2'));
  assert.equal(ui.$('historyPages').lastElementChild.disabled, true);
});

test('unchanged metric comparisons are suppressible only for a fresh matching input baseline', async t => {
  const run = workspaceRun();
  let fingerprint = run.input_fingerprint;
  const ui = await loadedEditor(t, { run, onRequest: async (url, options) => url === '/ops/exam-timetable/draft-impact/'
    ? response({ ...evaluatedRun(JSON.parse(options.body), run), input_fingerprint: fingerprint,
      qa: { ...run.qa, rooms: { ...run.qa.rooms, rooms_used: 2 } } }) : undefined });
  const delta = id => ui.$(id).parentElement.querySelector('.et-kpi-delta');
  assert.equal(delta('kConflicts').classList.contains('is-unchanged'), true);
  dropExam(ui, 'Mon');
  assert.equal(delta('kConflicts').classList.contains('is-unchanged'), false);
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(delta('kConflicts').classList.contains('is-unchanged'), true);
  assert.equal(delta('kRoomsUsed').classList.contains('is-unchanged'), false, 'A changed neutral room count remains visible');
  fingerprint = 'different-inputs';
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('examSummaryCards').querySelectorAll('.is-unchanged').length, 0);
  assert.equal(ui.$('examQuickMetrics').querySelectorAll('.is-unchanged').length, 0);
});

for (const checkTiming of ['before', 'during', 'after']) {
  test(`one Undo removes a new pin after a canceled scope change ${checkTiming} live evaluation`, async t => {
    const run = savedRun();
    let finishCheck;
    let cancelScope;
    const ui = await loadedEditor(t, { run, liveUpdate: true, onRequest: async (url, options) => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      if (checkTiming === 'during') return new Promise(resolve => { finishCheck = () => resolve(response(evaluatedRun(JSON.parse(options.body), run))); });
      return response(evaluatedRun(JSON.parse(options.body), run));
    } });
    ui.$('selectComputerCourses').click();
    assert.equal(ui.$('saveLoadedBtn').disabled, true, 'The preset made no selection change');
    assert.equal(ui.$('undoExamBtn').disabled, true);
    examChip(ui).querySelector('[data-exam-pin]').click();
    assert.equal(examChip(ui).querySelector('[data-exam-pin]').getAttribute('aria-pressed'), 'true');
    if (checkTiming !== 'before') await pause(520);
    const checkbox = ui.$('progList').querySelector('input[value="AI"]');
    const count = ui.$('progCount').textContent;
    ui.window.dlg.confirm = () => new Promise(resolve => { cancelScope = () => resolve(false); });
    checkbox.click();
    assert.equal(checkbox.checked, true, 'Scope is restored while confirmation is open');
    if (checkTiming === 'during') {
      finishCheck();
      await settle();
    }
    cancelScope();
    await settle();
    assert.equal(checkbox.checked, true);
    assert.equal(ui.$('progCount').textContent, count);
    ui.$('undoExamBtn').click();
    assert.equal(examChip(ui).querySelector('[data-exam-pin]').getAttribute('aria-pressed'), 'false');
    assert.equal(examChip(ui, courses[0].course_code).querySelector('[data-exam-pin]').getAttribute('aria-pressed'), 'true');
    assert.equal(ui.$('undoExamBtn').disabled, true, 'Cancellation added no extra undo command');
    assert.equal(ui.$('redoExamBtn').disabled, false);
    assert.equal(ui.$('saveLoadedBtn').disabled, true);
    assert.equal(ui.$('exportXlsx').getAttribute('href'), `/ops/exam-timetable/${run.run_id}/export.xlsx`);
  });
}

test('the real shared discard dialog preserves one-step Undo on Cancel and reloads the same run on Confirm', async t => {
  const ui = await loadedEditor(t, { run: savedRun(), realDialogs: true });
  ui.$('selectComputerCourses').click();
  examChip(ui).querySelector('[data-exam-pin]').click();
  ui.$('progList').querySelector('input[value="AI"]').click();
  ui.window.document.querySelector('.dlg-backdrop .btn-cancel').click();
  await pause(230);
  ui.$('undoExamBtn').click();
  assert.equal(examChip(ui).querySelector('[data-exam-pin]').getAttribute('aria-pressed'), 'false');
  assert.equal(ui.$('undoExamBtn').disabled, true);
  examChip(ui).querySelector('[data-exam-pin]').click();
  ui.$('historyList').querySelector('.et-run-info').click();
  ui.window.document.querySelector('.dlg-backdrop .btn-confirm').click();
  await pause(230);
  assert.equal(ui.requests.filter(request => request.url === '/ops/exam-timetable/17/').length, 2);
  assert.equal(examChip(ui).querySelector('[data-exam-pin]').getAttribute('aria-pressed'), 'false');
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  assert.equal(ui.$('undoExamBtn').disabled, true);
});

const expiredSession = () => ({ ok: true, status: 200, redirected: true,
  url: 'http://exam.test/login/?next=%2Fops%2Fexam-timetable%2F17%2F',
  headers: { get: () => 'text/html; charset=utf-8' },
  json: async () => { throw new SyntaxError('Unexpected token <'); } });

for (const action of ['load', 'courses', 'check', 'save-check', 'save', 'optimize', 'delete']) {
  test(`expired-session ${action} shows recovery outside collapsed setup and preserves the draft and Undo`, async t => {
    let expired = false;
    const fails = url => ({
      load: url === '/ops/exam-timetable/17/',
      courses: url === '/ops/exam-timetable/preview-courses/',
      check: url === '/ops/exam-timetable/draft-impact/',
      'save-check': url === '/ops/exam-timetable/draft-impact/',
      save: url === '/ops/exam-timetable/build/',
      optimize: url === '/ops/exam-timetable/build/',
      delete: url === '/ops/exam-timetable/17/delete/',
    })[action];
    const ui = await loadedEditor(t, { onRequest: async url => expired && fails(url) ? expiredSession() : undefined });
    dropExam(ui, 'Mon');
    ui.$('examSetupDetails').open = false;
    ui.window.dlg.confirm = async () => true;
    expired = true;
    const button = action === 'load' ? ui.$('historyList').querySelector('.et-run-info')
      : action === 'delete' ? ui.$('historyList').querySelector('.et-del-btn')
      : ui.$({ courses: 'loadCoursesBtn', check: 'checkDraftBtn', 'save-check': 'saveLoadedBtn', save: 'saveLoadedBtn', optimize: 'optimizeLoadedBtn' }[action]);
    button.click();
    await settle();
    await settle();
    assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
    assert.equal(ui.$('undoExamBtn').disabled, false);
    assert.equal(ui.$('etResults').classList.contains('d-none'), false);
    for (const id of ['examRequestError', 'examEditorRequestError']) {
      const banner = ui.$(id);
      assert.equal(banner.hidden, false);
      assert.equal(banner.closest('#examSetupDetails'), null);
      assert.doesNotMatch(banner.textContent, /Unexpected token|SyntaxError|<html/);
      const link = banner.querySelector('a');
      assert.equal(link.target, '_blank');
      assert.equal(link.rel, 'noopener');
      assert.equal(new URL(link.href).origin, 'http://exam.test');
      assert.equal(new URL(link.href).pathname, '/login/');
    }
    assert.equal(ui.notifications.length, 1);
    assert.equal([ui.$('examRequestError'), ui.$('examEditorRequestError')].filter(banner => banner.getAttribute('role') === 'alert').length, 1);
    assert.equal(ui.window.location.pathname, '/exam-timetable/');
    ui.$('undoExamBtn').click();
    assert.deepEqual(placement(ui), ['Sun', '10:30-12:30']);
    if (action === 'save-check') assert.equal(buildRequests(ui).length, 0);
  });
}

test('initial filters and history authentication failures show a recovery banner without leaving the screen', async t => {
  const ui = await page(t, { loadCourses: false, onRequest: async url =>
    url === '/ops/exam-timetable/filters/' || url.startsWith('/ops/exam-timetable/list/') ? expiredSession() : undefined });
  assert.equal(ui.$('examRequestError').hidden, false);
  assert.ok(ui.$('examRequestError').querySelector('a[target="_blank"]'));
  assert.equal(ui.notifications.length, 2);
  assert.equal(ui.window.location.pathname, '/exam-timetable/');
});

test('expired-session Build retains setup and selected courses for retry', async t => {
  const ui = await page(t, { onRequest: async url => url === '/ops/exam-timetable/build/' ? expiredSession() : undefined });
  const selected = selectedCourses(ui);
  ui.$('buildBtn').click();
  await settle();
  assert.deepEqual(selectedCourses(ui), selected);
  assert.equal(ui.$('buildBtn').disabled, false);
  assert.equal(ui.$('examRequestError').hidden, false);
  assert.equal(ui.$('etLabel').value, 'Interaction regression');
});

test('retry after signing in uses the rotated CSRF cookie and clears session recovery banners', async t => {
  let expired = true;
  const ui = await loadedEditor(t, { onRequest: async url =>
    expired && url === '/ops/exam-timetable/draft-impact/' ? expiredSession() : undefined });
  dropExam(ui, 'Mon');
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('examEditorRequestError').hidden, false);
  ui.window.document.cookie = 'csrftoken=rotated-login-token; path=/';
  expired = false;
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(draftRequests(ui).at(-1).headers['X-CSRFToken'], 'rotated-login-token');
  assert.equal(ui.$('examRequestError').hidden, true);
  assert.equal(ui.$('examEditorRequestError').hidden, true);
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
});

test('HTML server failures and network errors never expose a JSON parser error or erase a draft', async t => {
  let failure = 'html';
  let ui;
  ui = await loadedEditor(t, { onRequest: async url => {
    if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
    if (failure === 'network') throw new ui.window.TypeError('Failed to fetch');
    return { ok: false, status: 500, headers: { get: () => 'text/html' }, json: async () => { throw new SyntaxError('Unexpected token <'); } };
  } });
  dropExam(ui, 'Mon');
  for (const next of ['html', 'network']) {
    failure = next;
    ui.$('checkDraftBtn').click();
    await settle();
    assert.equal(ui.$('examEditorRequestError').hidden, false);
    assert.doesNotMatch(ui.$('examEditorRequestError').textContent, /Unexpected token|Failed to fetch|SyntaxError/);
    assert.equal(ui.$('examEditorRequestError').querySelector('a'), null);
    assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
    assert.equal(ui.$('undoExamBtn').disabled, false);
  }
});

test('an unrelated successful history request cannot hide a Check CSRF recovery message', async t => {
  let denied = true;
  const ui = await loadedEditor(t, { onRequest: async url => {
    if (url.startsWith('/ops/exam-timetable/list/')) return response({ ok: true, page: 1, total_pages: 2, total: 11, runs: [{ id: 17, label: 'Saved run' }] });
    if (denied && url === '/ops/exam-timetable/draft-impact/') return {
      ok: false, status: 403, headers: { get: () => 'text/html' }, json: async () => { throw new SyntaxError('Unexpected token <'); },
    };
    return undefined;
  } });
  dropExam(ui, 'Mon');
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('examEditorRequestError').hidden, false);
  ui.$('historyPages').querySelector('[data-history-page="2"]').click();
  await settle();
  assert.equal(ui.$('examEditorRequestError').hidden, false);
  assert.ok(ui.$('examEditorRequestError').querySelector('a[target="_blank"]'));
  denied = false;
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('examEditorRequestError').hidden, true);
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
});

test('unassigned room details keep official section names literal and room-group numbering separate', async t => {
  const run = workspaceRun();
  const official = ['F11/1 + <Honors>', 'شعبة أ / ب'];
  run.qa.rooms.unassigned_room_sections = [{
    course_code: courses[1].course_code, course_identity: courses[1].course_identity,
    day: 'Sun', period: '10:30-12:30', section: official.join(' + '), gender: 'F', student_count: 14,
    room_group: 'F11/1 + <Honors>:1/2;شعبة أ / ب:2/2',
    section_parts: official.map((section, index) => ({ section, section_key: `official-${index}`, term_section_id: 100 + index,
      mapping_status: 'mapped', gender: 'F', student_count: 7, room_group_index: index + 1, room_group_count: 2 })),
  }];
  const ui = await loadedEditor(t, { run });
  ui.window.document.querySelector('[data-drill="room-unassigned"]').click();
  assert.deepEqual(Array.from(ui.$('kpiDrillBody').querySelectorAll('.et-official-section'), element => element.textContent), official);
  assert.equal(ui.$('kpiDrillBody').querySelector('honors'), null);
  const groups = Array.from(ui.$('kpiDrillBody').querySelectorAll('.et-room-group'), element => element.textContent);
  assert.deepEqual(groups, language === 'ar' ? ['مجموعة قاعة 1 من 2', 'مجموعة قاعة 2 من 2'] : ['Room group 1 of 2', 'Room group 2 of 2']);
  assert.equal(ui.$('kRoomUnassigned').textContent, '1', 'The card counts unassigned room groups, not teaching sections');
  assert.match(ui.$('kRoomUnassigned').parentElement.querySelector('.k').textContent, language === 'ar' ? /مجموعات/ : /groups/);
  assert.equal(drillAction(ui, 'find').dataset.findExam, courses[1].course_identity);
  const compactBefore = examChip(ui).querySelector('.et-course-short-name').textContent;
  drillAction(ui, 'find').click();
  assert.equal(ui.window.document.activeElement, examChip(ui));
  assert.equal(examChip(ui).querySelector('.et-course-short-name').textContent, compactBefore);
});

test('split-room summaries preserve same-named teaching sections in distinct course identities and show one exam time', async t => {
  const run = workspaceRun();
  run.qa.multi_sitting_sections = 2;
  run.qa.multi_sitting_details = courses.map((course, index) => ({
    ...course, section: 'M7/أ + <Official>', section_key: `term-section-${index}`, term_section_id: 200 + index,
    mapping_status: 'mapped', gender: 'M', enrolment: 14, max_room_cap: 10, sittings: 2,
    slots: [index, index], rooms: ['R1', index ? 'UNASSIGNED' : 'R2'], incomplete: Boolean(index),
  }));
  const ui = await loadedEditor(t, { run });
  ui.window.document.querySelector('[data-drill="multi-sitting"]').click();
  assert.match(ui.$('kpiDrillTitle').textContent, language === 'ar' ? /قاعات/ : /across rooms/);
  assert.doesNotMatch(ui.$('kpiDrillHead').textContent, /Sittings|الجلسات/);
  const rows = Array.from(ui.$('kpiDrillBody').rows);
  assert.equal(rows.length, 2);
  rows.forEach((row, index) => {
    assert.equal(row.querySelector('.et-official-section').textContent, 'M7/أ + <Official>');
    assert.equal(row.querySelector('official'), null);
    assert.match(row.cells[1].textContent, language === 'ar' ? /طلاب \(M\)/ : /Male students \(M\)/);
    assert.equal(row.cells[4].textContent, '2');
    assert.equal(row.cells[5].querySelectorAll('.badge').length, 1, 'Two rooms do not imply two exam times');
    assert.equal(row.querySelector('[data-find-exam]').dataset.findExam, courses[index].course_identity);
  });
  assert.match(rows[1].cells[6].textContent, language === 'ar' ? /دون قاعة/ : /Unassigned/);
  assert.doesNotMatch(rows[1].cells[6].textContent, /UNASSIGNED/);
  rows[1].querySelector('[data-find-exam]').click();
  assert.equal(ui.window.document.activeElement, examChip(ui));
});

function sectionGapRun() {
  const run = workspaceRun();
  run.qa.section_mapping = {
    mapped_sections: 2, mapped_enrollments: 38, missing_enrollments: 3, ambiguous_enrollments: 4,
    details: courses.map((course, index) => ({ ...course, gender: index ? 'F' : 'M', student_count: index ? 4 : 3,
      mapping_status: index ? 'ambiguous' : 'missing', reason: index ? 'multiple_recorded_sections' : 'no_recorded_section' })),
  };
  return run;
}

test('mapping gaps remain visible outside collapsed summaries and offer exact Find actions with clear bilingual reasons', async t => {
  const ui = await loadedEditor(t, { run: sectionGapRun() });
  assert.equal(ui.$('examSummaryDetails').open, false);
  assert.equal(ui.$('examSectionMappingNotice').hidden, false);
  assert.equal(ui.$('examSectionMappingNotice').closest('details'), null);
  assert.match(ui.$('examSectionMappingText').textContent, /3.*4/);
  assert.match(ui.$('examSectionMappingText').textContent, language === 'ar' ? /تسجيل مقرر/ : /course enrollments/);
  ui.$('examSectionMappingReview').click();
  assert.equal(ui.$('kpiDrill').dataset.type, 'section-mapping');
  const rows = Array.from(ui.$('kpiDrillBody').rows);
  assert.match(rows[0].textContent, language === 'ar' ? /الشعبة غير مسجلة/ : /Section not recorded/);
  assert.match(rows[1].textContent, language === 'ar' ? /عدة شعب محتملة/ : /Multiple possible sections/);
  assert.doesNotMatch(ui.$('kpiDrillBody').textContent, /no_recorded_section|multiple_recorded_sections/);
  assert.equal(ui.$('kpiDrillBody').querySelector('[data-move-exam]'), null, 'Moving an exam cannot resolve a missing teaching-section record');
  rows[1].querySelector('[data-find-exam]').click();
  assert.equal(ui.window.document.activeElement, examChip(ui));
  ui.$('kpiDrillClose').click();
  assert.equal(ui.window.document.activeElement, ui.$('examSectionMappingReview'));
});

test('missing and ambiguous section labels are explicit while old blank data remains unknown', async t => {
  const run = workspaceRun();
  run.qa.rooms.unassigned_room_sections = ['missing', 'ambiguous', undefined].map((mapping_status, index) => ({
    course_code: courses[1].course_code, section: '', mapping_status, section_key: `gap-${index}`, gender: 'F', student_count: 1,
    day: 'Sun', period: '10:30-12:30',
  }));
  const ui = await loadedEditor(t, { run });
  ui.window.document.querySelector('[data-drill="room-unassigned"]').click();
  const labels = Array.from(ui.$('kpiDrillBody').querySelectorAll('.et-official-section'), element => element.textContent);
  assert.deepEqual(labels, language === 'ar'
    ? ['الشعبة غير مسجلة', 'عدة شعب محتملة', 'بيانات الشعبة غير متاحة']
    : ['Section not recorded', 'Multiple possible sections', 'Section information unavailable']);
  assert.equal(ui.$('examSectionMappingNotice').hidden, true, 'Old rows without the mapping report do not invent gap totals');
});

test('Check refreshes section-mapping diagnostics while preserving scope, placements and stale-report honesty', async t => {
  const run = sectionGapRun();
  const ui = await loadedEditor(t, { run, onRequest: async (url, options) => url === '/ops/exam-timetable/draft-impact/'
    ? response({ ...evaluatedRun(JSON.parse(options.body), run), input_fingerprint: 'resolved-section-inputs',
      qa: { ...run.qa, section_mapping: { mapped_sections: 4, mapped_enrollments: 45, missing_enrollments: 0, ambiguous_enrollments: 0, details: [] } } }) : undefined });
  const selected = selectedCourses(ui);
  const scope = Array.from(ui.$('secList').querySelectorAll('input:checked'), input => input.value);
  ui.$('examSectionMappingReview').click();
  dropExam(ui, 'Mon');
  assert.match(ui.$('examSectionMappingText').textContent, language === 'ar' ? /آخر تحقق/ : /Last checked/);
  assert.equal(ui.$('examDrillNotice').hidden, false);
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('examSectionMappingNotice').hidden, true);
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
  assert.deepEqual(selectedCourses(ui), selected);
  assert.deepEqual(Array.from(ui.$('secList').querySelectorAll('input:checked'), input => input.value), scope);
  assert.equal(ui.$('saveLoadedBtn').disabled, false, 'New source section mapping is evaluated but not yet saved');
  ui.$('kpiDrillClose').click();
  assert.equal(ui.window.document.activeElement, ui.$('examEditHeading'));
});

test('F/M scope controls say student groups and retain the same API filter values', async t => {
  const ui = await page(t);
  assert.match(ui.$('examScopeHeading').textContent, language === 'ar' ? /فئات الطلاب/ : /student groups/);
  for (const [value, english, arabic] of [['F', 'Female students', 'طالبات'], ['M', 'Male students', 'طلاب']]) {
    const checkbox = ui.$('secList').querySelector(`input[value="${value}"]`);
    assert.ok(checkbox.closest('label').textContent.includes(language === 'ar' ? arabic : english));
  }
  assert.deepEqual(JSON.parse(ui.requests.find(request => request.url === '/ops/exam-timetable/preview-courses/').body).sections, ['F', 'M']);
});

test('course loading explains its actual imported-timetable population and handles an empty source without fallback', async t => {
  const ui = await page(t, { initialCourses: [] });
  assert.match(ui.$('examEnrollmentSource').textContent, language === 'ar' ? /التسجيلات الفعلية.*الجداول الدراسية المستوردة/ : /actual enrollments.*imported student timetables/);
  assert.match(ui.$('etStatus').textContent, language === 'ar' ? /لا توجد تسجيلات فعلية/ : /No actual enrollments/);
  assert.equal(ui.$('courseList').querySelectorAll('input').length, 0);
  assert.equal(ui.$('buildBtn').disabled, true);
  assert.equal(buildRequests(ui).length, 0);
});

test('a saved timetable from an earlier source stays visible but requires explicit course reload and rebuild', async t => {
  const run = savedRun();
  delete run.enrollment_source;
  const ui = await loadedEditor(t, { run, liveUpdate: true });
  assert.equal(ui.$('examSourceReviewNotice').hidden, false);
  assert.match(ui.$('examCheckStatus').textContent, language === 'ar' ? /مصدر المقررات/ : /Course source review required/);
  assert.deepEqual(selectedCourses(ui), courses.map(course => course.course_code));
  assert.deepEqual(placement(ui), ['Sun', '10:30-12:30']);
  for (const id of ['checkDraftBtn', 'saveLoadedBtn', 'optimizeLoadedBtn']) assert.equal(ui.$(id).disabled, true);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  assert.match(ui.$('exportDraftNotice').textContent, language === 'ar' ? /مصدر المقررات/ : /course source/);
  const before = ui.requests.length;
  ui.$('reviewExamCourseSource').click();
  assert.equal(ui.$('examSetupDetails').open, true);
  assert.equal(ui.window.document.activeElement, ui.$('loadCoursesBtn'));
  assert.equal(ui.requests.length, before, 'Review does not replace the draft or fetch a new population');
  dropExam(ui, 'Mon');
  await pause(500);
  assert.equal(draftRequests(ui).length, 0, 'Live check cannot evaluate a pre-policy source');
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  ui.$('loadCoursesBtn').click();
  await settle();
  assert.equal(ui.dialogs.length, 1);
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00'], 'Cancel retains the changed saved timetable');
  ui.window.dlg.confirm = async () => true;
  ui.setCourses([courses[0]]);
  ui.$('loadCoursesBtn').click();
  await settle();
  assert.deepEqual(selectedCourses(ui), [courses[0].course_code]);
  assert.equal(ui.$('etResults').classList.contains('d-none'), true);
  assert.equal(ui.$('buildBtn').disabled, false);
  assert.equal(buildRequests(ui).length, 0, 'Reloading requires an explicit new Build');
});

const unavailableCourseResponse = () => response({ ok: false, code: 'courses_unavailable', unavailable_courses: ['CS112', '<CS111 (2)>'], error: 'Selected courses unavailable' });

for (const action of ['check', 'save-precheck', 'save', 'optimize']) {
  test(`${action} rejects non-actual courses without dropping placements, pins, scope or Undo history`, async t => {
    const run = savedRun();
    const ui = await loadedEditor(t, { run, onRequest: async (url, options) => {
      if (url === '/ops/exam-timetable/draft-impact/' && ['check', 'save-precheck'].includes(action)) return unavailableCourseResponse();
      if (url === '/ops/exam-timetable/build/') return unavailableCourseResponse();
      return undefined;
    } });
    dropExam(ui, 'Mon');
    const selection = selectedCourses(ui);
    const pins = ui.$('examPinRows').textContent;
    if (action === 'save') {
      ui.$('checkDraftBtn').click();
      await settle();
    }
    ui.$(action === 'check' ? 'checkDraftBtn' : action === 'optimize' ? 'optimizeLoadedBtn' : 'saveLoadedBtn').click();
    await settle();
    assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
    assert.deepEqual(selectedCourses(ui), selection);
    assert.equal(ui.$('examPinRows').textContent, pins);
    assert.equal(ui.$('undoExamBtn').disabled, false);
    assert.equal(ui.$('examEditorRequestError').hidden, false);
    assert.equal(ui.$('examEditorRequestError').dataset.errorKind, 'courses-unavailable');
    assert.match(ui.$('examEditorRequestError').textContent, language === 'ar' ? /تسجيلات فعلية/ : /actual enrollments/);
    assert.ok(ui.$('examEditorRequestError').textContent.includes('<CS111 (2)>'));
    assert.equal(ui.$('examEditorRequestError').querySelector('cs111'), null);
    assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
    if (action === 'save-precheck') assert.equal(buildRequests(ui).length, 0, 'A rejected automatic Check must not POST Save');
    const before = ui.requests.length;
    ui.$('examEditorRequestError').querySelector('.et-source-review-action').click();
    assert.equal(ui.$('examSetupDetails').open, true);
    assert.equal(ui.window.document.activeElement, ui.$('loadCoursesBtn'));
    assert.equal(ui.requests.length, before);
    ui.$('undoExamBtn').click();
    assert.deepEqual(placement(ui), ['Sun', '10:30-12:30']);
    assert.equal(ui.$('exportXlsx').getAttribute('href'), null, 'Undo cannot make rejected source data current again');
  });
}

test('fresh Build rejects an unavailable course and course reload recovers without retaining or adding it', async t => {
  const ui = await page(t, { onRequest: async url => url === '/ops/exam-timetable/build/' ? unavailableCourseResponse() : undefined });
  ui.$('buildBtn').click();
  await settle();
  assert.equal(ui.$('examRequestError').dataset.errorKind, 'courses-unavailable');
  assert.deepEqual(selectedCourses(ui), courses.map(course => course.course_code));
  assert.equal(ui.$('etResults').classList.contains('d-none'), true);
  ui.setCourses([courses[0]]);
  ui.$('loadCoursesBtn').click();
  await settle();
  assert.deepEqual(selectedCourses(ui), [courses[0].course_code]);
  assert.equal(ui.$('examRequestError').hidden, true);
});

test('a valid explicit Check clears an unavailable-course rejection while retaining the saved run identity', async t => {
  let reject = true;
  const ui = await loadedEditor(t, { onRequest: async url => url === '/ops/exam-timetable/draft-impact/' && reject ? unavailableCourseResponse() : undefined });
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  reject = false;
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('examEditorRequestError').hidden, true);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx');
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
});

test('a server enrollment-source rejection requires review and rebuild while keeping the whole current draft', async t => {
  const ui = await loadedEditor(t, { onRequest: async url => url === '/ops/exam-timetable/draft-impact/'
    ? response({ ok: false, code: 'enrollment_source_changed', error: 'Source policy changed' }) : undefined });
  dropExam(ui, 'Mon');
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('examSourceReviewNotice').hidden, false);
  assert.deepEqual(placement(ui), ['Mon', '08:00-10:00']);
  assert.deepEqual(selectedCourses(ui), courses.map(course => course.course_code));
  for (const id of ['checkDraftBtn', 'saveLoadedBtn', 'optimizeLoadedBtn']) assert.equal(ui.$(id).disabled, true);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  ui.$('examQuickMetrics').querySelector('[data-open-drill="conflicts"]').click();
  assert.match(ui.$('examDrillNotice').textContent, language === 'ar' ? /مصدر المقررات وأعد البناء/ : /course source and rebuild/);
  assert.doesNotMatch(ui.$('examDrillNotice').textContent, /Check changes/);
  ui.$('undoExamBtn').click();
  assert.deepEqual(placement(ui), ['Sun', '10:30-12:30']);
  assert.equal(ui.$('examSourceReviewNotice').hidden, false);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
});

test('section-review status is localized as a warning and its flag precedes workload and logistics flags', async t => {
  const run = sectionGapRun();
  run.primary_status = 'requires_section_review';
  run.status_flags = ['multi_sitting_required', 'heavy_credit_day', 'section_mapping_incomplete'];
  const ui = await loadedEditor(t, { run });
  assert.equal(ui.$('kStatusPrimary').classList.contains('bg-warning'), true);
  assert.match(ui.$('kStatusPrimary').textContent, language === 'ar' ? /شعب المقررات تحتاج مراجعة/ : /Teaching sections need review/);
  assert.match(ui.$('kStatusFlags').firstElementChild.textContent, language === 'ar' ? /ربط شعب المقررات غير مكتمل/ : /teaching-section mapping incomplete/);
  assert.equal(ui.$('examSectionMappingNotice').hidden, false);
});

test('a repair on a board with exams already in Overflow does not call the board clean', async t => {
  const ui = await runRepair(t, {
    moves: [], unseated: [], already_overflow: 2, violations_before: 0, violations_after: 0, proven_minimal: true, status: 'OPTIMAL',
  });
  const report = ui.$('examRepairReport');
  assert.doesNotMatch(report.textContent, language === 'ar' ? /لا يوجد ما يحتاج إصلاحاً/ : /Nothing to repair/);
  assert.match(report.textContent, language === 'ar' ? /فترة إضافية/ : /Overflow slot/);
  assert.match(report.textContent, language === 'ar' ? /تحسين الجدول الحالي/ : /Optimize current timetable/);
  assert.ok(report.classList.contains('is-partial'), 'Exams left in Overflow are not a finished board');
});

test('a throttled action says how long to wait, in the page language', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? {
          ok: false,
          status: 429,
          headers: { get: name => (name === 'Retry-After' ? '42' : 'application/json') },
          json: async () => ({ error: 'Rate limit exceeded. Please try again later.' }),
        }
      : undefined,
  });
  dropExam(ui, 'Wed');
  ui.$('minChangeBtn').click();
  await settle();
  const banner = ui.$('examEditorRequestError');
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /42/, 'The wait the server sent must be shown');
  assert.match(banner.textContent, language === 'ar' ? /انتظر/ : /Wait 42 seconds/);
  assert.doesNotMatch(banner.textContent, /Rate limit exceeded/, 'The raw English server text must not leak through');
});

/* ── Background jobs: the page follows an action the server runs ── */

const JOB_ID = '6f1c2a4e-0000-4000-8000-000000000001';
const JOB_B = '6f1c2a4e-0000-4000-8000-000000000002';
const JOB_PLAN = {
  build: ['enrolments', 'conflicts', 'place_exams', 'check_rules', 'assign_rooms', 'balance_invigilators', 'save'],
  optimize_loaded: ['read_board', 'place_exams', 'check_rules', 'assign_rooms', 'balance_invigilators', 'save'],
  minimum_change_repair: ['read_board', 'fewest_moves', 'check_rules', 'assign_rooms', 'save'],
  save_loaded_changes: ['read_board', 'check_rules', 'assign_rooms', 'balance_invigilators', 'save'],
};
const allDone = kind => Object.fromEntries(JOB_PLAN[kind].map(key => [key, 'done']));

function jobFrame(kind, status, states = {}, current = null, extra = {}) {
  return {
    id: JOB_ID, kind, status, mine: true, can_cancel: true, owner: 'Registrar', has_run: false, result_run_id: null, error_code: '',
    cancelled_by: '', waiting_for: null, refused: false, stopping: false,
    submitted_at: '2026-09-23T10:00:00+00:00', started_at: '2026-09-23T10:00:01+00:00', finished_at: null, now: '2026-09-23T10:00:05+00:00',
    stages: JOB_PLAN[kind].map(key => ({ key, state: states[key] || 'pending' })), current, ...extra,
  };
}
const finishedFrame = (kind, extra = {}) => jobFrame(kind, 'succeeded', allDone(kind), { key: 'save', done: null, total: null },
  { has_run: true, result_run_id: 93, finished_at: '2026-09-23T10:01:43+00:00', ...extra });
const failedFrame = (kind, extra = {}) => jobFrame(kind, 'failed', { [JOB_PLAN[kind][0]]: 'done', [JOB_PLAN[kind][1]]: 'stopped' },
  { key: JOB_PLAN[kind][1], done: null, total: null }, { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00', ...extra });
const jobReply = (data, status = 200, headers = {}) => ({
  ok: status < 400, status, json: async () => data, headers: { get: name => headers[name] ?? null },
});
const pollUrl = id => `/ops/exam-timetable/jobs/${id}/`;
const seenUrl = id => `/ops/exam-timetable/jobs/${id}/seen/`;
const historyLoads = ui => ui.requests.filter(request => request.url.startsWith('/ops/exam-timetable/list/')).length;
const seenPosts = ui => ui.requests.filter(request => request.url === seenUrl(JOB_ID)).length;
const stageStates = ui => Array.from(ui.$('examJobStages').children, item => item.className.replace('et-job-stage is-', ''));
const buttonLabel = button => button.textContent.replace(/\s+/g, ' ').trim();
const statusHidden = ui => ui.$('etStatus').classList.contains('d-none');

// Waits by the clock, not by turns of the event loop: the page's own polls
// are timers, and even a zero-delay timer is a millisecond away.
async function until(predicate, message = 'Timed out waiting for the page') {
  const deadline = Date.now() + 3000;
  while (!predicate() && Date.now() < deadline) await pause(2);
  assert.ok(predicate(), typeof message === 'function' ? message() : message);
}

// A fake server running one job. The test moves it on with advance(); each poll
// meanwhile answers the current frame, as a real server would while it works.
function jobServer(kind, frames, { result = null, resultStatus = 200 } = {}) {
  const server = { index: 0, polls: 0, results: 0, cancels: 0, failures: 0, gone: false, override: null };
  server.advance = () => { server.index = Math.min(server.index + 1, frames.length - 1); };
  server.onRequest = async (url, options) => {
    if (server.override) {
      const answer = await server.override(url, options);
      if (answer !== undefined) return answer;
    }
    if (url === '/ops/exam-timetable/build/') {
      server.submitted = JSON.parse(options.body);
      return jobReply({ ok: true, job: jobFrame(kind, 'queued') }, 202);
    }
    if (url === pollUrl(JOB_ID)) {
      server.polls += 1;
      if (server.gone) return jobReply({ ok: false, error_code: 'job_not_found', error: 'Job not found' }, 404);
      if (server.failures > 0) {
        server.failures -= 1;
        throw new TypeError('Failed to fetch');
      }
      return jobReply({ ok: true, job: frames[server.index] });
    }
    if (url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`) {
      server.results += 1;
      if (typeof result === 'function') return result();
      if (result) return jobReply(result, resultStatus);
      return response({ ...evaluatedRun(server.submitted), run_id: 93 });
    }
    if (url === `/ops/exam-timetable/jobs/${JOB_ID}/cancel/`) {
      server.cancels += 1;
      return jobReply({ ok: true, job: frames[server.index] }, 202);
    }
    if (url === seenUrl(JOB_ID)) return jobReply({ ok: true, marked: true });
    return undefined;
  };
  return server;
}

const OPTIMISED = [
  jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'running' }, { key: 'place_exams', done: 3, total: 10 }),
  jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'done', check_rules: 'running' }, { key: 'check_rules', done: null, total: null }),
  finishedFrame('optimize_loaded', { stages: JOB_PLAN.optimize_loaded.map(key => ({ key, state: ['assign_rooms', 'balance_invigilators'].includes(key) ? 'skipped' : 'done' })) }),
];
const TEXT = {
  exams310: language === 'ar' ? 'تم توزيع 3 من 10 اختبارات' : '3 of 10 exams placed',
  leave: language === 'ar' ? /يمكنك مغادرة الصفحة؛ إن عدت خلال ساعة/ : /You can leave this page\. If you come back within an hour/,
  optimized: language === 'ar' ? 'تم تحسين الجدول' : 'Timetable optimized',
  optimizing: language === 'ar' ? 'جارٍ تحسين الجدول الحالي' : 'Optimizing the current timetable',
  optimizeFailed: language === 'ar' ? 'تعذّر تحسين الجدول' : 'Optimization failed',
  optimizeStopped: language === 'ar' ? 'أُوقف تحسين الجدول' : 'Optimization stopped',
  optimizeLost: language === 'ar' ? 'تعذّرت متابعة تحسين الجدول' : 'Lost track of the optimization',
  building: language === 'ar' ? 'جارٍ بناء الجدول' : 'Building the timetable',
  savedOwn: language === 'ar' ? /حُفظ، وهو معروض أدناه/ : /Saved\. It is open below/,
  stopping: language === 'ar' ? /جارٍ الإيقاف/ : /Stopping/,
  couldNotStop: language === 'ar' ? /تعذّر الإيقاف/ : /Could not stop it/,
  tooLate: language === 'ar' ? /قبل إيقافها، وحُفظ الجدول الجديد/ : /before it could be stopped\. The new timetable was saved/,
  tooLateNoRun: language === 'ar' ? /قبل إيقافها، ولم يُحفظ جدول جديد/ : /before it could be stopped, without saving a new timetable/,
  stoppedOnScreen: language === 'ar' ? /أُوقفت العملية قبل حفظ أي شيء، والجدول المعروض لم يتغيّر/ : /Stopped before it saved anything\. The timetable on screen is unchanged/,
  reference: /6f1c2a4e/,
};

// The job panel's sentences, as the page says them in each language.
const W = language === 'ar' ? {
  draft: 'التغييرات غير المحفوظة موجودة فقط في الصفحة التي أُجريت فيها ما دامت مفتوحة.',
  recheck: {
    away: 'فإن كانت تلك الصفحة ما تزال مفتوحة فافحص التغييرات فيها ثم احفظها، وإلا فافتح الجدول من «الجداول المحفوظة» وأعد إجراء التغييرات.',
    followed: 'إن كانت الصفحة التي حفظت منها ما تزال مفتوحة فافحص التغييرات فيها ثم احفظها، وإلا فافتح الجدول من «الجداول المحفوظة» وأعد إجراء التغييرات.',
    own: 'افحص التغييرات ثم احفظها.',
  },
  nothingSaved: 'لم يُحفظ شيء.',
  advice: {
    server_error: 'أعد تنفيذ العملية، وإن أخفقت مجدداً فتواصل مع الدعم الفني واذكر المرجع أدناه.',
    server_restarted: 'ابدأ العملية من جديد، وإن توقفت مجدداً فتواصل مع الدعم الفني واذكر المرجع أدناه.',
    timed_out: 'إن تجاوزت العملية المدة مجدداً فتواصل مع الدعم الفني واذكر المرجع أدناه.',
    never_started: 'أعد تنفيذ العملية لاحقاً.',
  },
  reason: { inputs_changed: 'تغيّرت بيانات مصدر الجدول بعد آخر فحص.', check_required: 'لم تكن التغييرات قد فُحصت.' },
  runDeleted: 'حُذف هذا الجدول من «الجداول المحفوظة».',
  openedGone: 'الجدول الذي حاولت فتحه حُذف من «الجداول المحفوظة».',
  savedGone: 'الجدول الذي حفظته هذه العملية حُذف من «الجداول المحفوظة».',
  away: 'بينما كانت الصفحة مغلقة',
} : {
  draft: 'Unsaved changes exist only on the page they were made on, while it stays open.',
  recheck: {
    away: 'If that page is still open, check the changes there, then save; otherwise open the timetable from Saved timetables and make them again.',
    followed: 'If the page you saved from is still open, check the changes there, then save; otherwise open the timetable from Saved timetables and make them again.',
    own: 'Check the changes, then save.',
  },
  nothingSaved: 'Nothing was saved.',
  advice: {
    server_error: 'Try the action again; if it fails again, contact support and quote the reference below.',
    server_restarted: 'Start the action again; if the server stops it again, contact support and quote the reference below.',
    timed_out: 'If the action runs out of time again, contact support and quote the reference below.',
    never_started: 'Try the action again later.',
  },
  reason: { inputs_changed: "the timetable's source data changed after the last check.", check_required: 'the changes had not been checked.' },
  runDeleted: 'That timetable was deleted from Saved timetables.',
  openedGone: 'The timetable you tried to open has been deleted from Saved timetables.',
  savedGone: 'The timetable this action saved has since been deleted from Saved timetables.',
  away: 'While this page was closed',
};
const endsWith = (text, ...parts) => assert.ok(text.endsWith(parts.join(' ')), `Said: ${text}`);

async function optimiseAsJob(t, frames = OPTIMISED, { poll = {}, serverOptions = {}, beforeClick = null, activeJob } = {}) {
  const server = jobServer('optimize_loaded', frames, serverOptions);
  const ui = await loadedEditor(t, { poll, onRequest: server.onRequest, ...(activeJob ? { activeJob } : {}) });
  if (beforeClick) beforeClick(ui, server);
  ui.$('optimizeLoadedBtn').click();
  return { ui, server };
}

function watchPanel(ui) {
  const seen = { shownAt: null };
  new ui.window.MutationObserver(() => {
    if (!ui.$('examJobPanel').hidden && seen.shownAt === null) seen.shownAt = Date.now();
  }).observe(ui.$('examJobPanel'), { attributes: true, attributeFilter: ['hidden'] });
  return seen;
}

// ── what a running job shows ──

test('a job shows the stage it is on, and a bar only for work the server counted', async t => {
  const { ui, server } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  assert.equal(ui.$('examJobPanel').hidden, false);
  assert.equal(stageStates(ui).join(), 'done,running,pending,pending,pending,pending');
  const bar = ui.$('examJobBar');
  assert.equal(bar.hidden, false);
  assert.equal(bar.getAttribute('role'), 'progressbar');
  assert.equal(bar.getAttribute('aria-valuenow'), '30');
  assert.equal(ui.$('examJobDetail').textContent, TEXT.exams310, 'The count alone: the stage is named in the list');
  assert.match(ui.$('examJobNote').textContent, TEXT.leave);

  server.advance();
  await until(() => stageStates(ui)[2] === 'running');
  assert.equal(bar.hidden, true, 'A stage that counts nothing gets no bar');
  for (const name of ['role', 'aria-label', 'aria-valuemin', 'aria-valuemax', 'aria-valuenow', 'aria-valuetext']) {
    assert.equal(bar.getAttribute(name), null, `${name} left behind`);
  }
  assert.equal(ui.$('examJobDetail').textContent, '');
});

test('each counted stage says what it counts', async t => {
  const rooms = jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'done', check_rules: 'done', assign_rooms: 'running' }, { key: 'assign_rooms', done: 12, total: 40 });
  const { ui, server } = await optimiseAsJob(t, [rooms, jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'running' }, { key: 'place_exams', done: 0, total: 1 })]);
  await until(() => stageStates(ui)[3] === 'running');
  assert.equal(ui.$('examJobDetail').textContent, language === 'ar' ? 'القاعات للفترة 12 من 40' : 'Rooms for period 12 of 40');
  assert.equal(ui.$('examJobBar').hidden, false);
  server.advance();
  if (language === 'en') await until(() => ui.$('examJobDetail').textContent === '0 of 1 exam placed');
});

test('the invigilator search is a ceiling, told in words and never drawn as a bar', async t => {
  const balancing = jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'done', check_rules: 'done', assign_rooms: 'done', balance_invigilators: 'running' }, { key: 'balance_invigilators', done: 5, total: 84 });
  const { ui } = await optimiseAsJob(t, [balancing]);
  await until(() => stageStates(ui)[4] === 'running');
  assert.equal(ui.$('examJobBar').hidden, true);
  assert.equal(ui.$('examJobDetail').textContent, language === 'ar' ? 'المحاولة 5 (الحد الأقصى 84)' : 'Attempt 5 (up to 84)');
});

test('a queued job says what it is waiting for, and only once the server has said', async t => {
  const { ui, server } = await optimiseAsJob(t, [
    jobFrame('optimize_loaded', 'queued'),
    jobFrame('optimize_loaded', 'queued', {}, null, { waiting_for: { kind: 'check', seconds: 1 } }),
    jobFrame('optimize_loaded', 'queued', {}, null, { waiting_for: { kind: 'planner', seconds: 240 } }),
  ]);
  await until(() => !ui.$('examJobPanel').hidden);
  // Queued until its thread takes it: not yet a wait for anything.
  assert.doesNotMatch(ui.$('examJobDetail').textContent, /busy|مشغول/);
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimizing);
  server.advance();
  await until(() => (language === 'ar' ? /الخادم مشغول بعملية أخرى على الجدول/ : /busy with another timetable action\. Yours starts/).test(ui.$('examJobDetail').textContent));
  assert.equal(ui.$('examJobTitle').textContent, language === 'ar' ? 'تحسين الجدول في الانتظار' : 'Waiting to optimize the timetable');
  server.advance();
  await until(() => (language === 'ar' ? /تخطيط الجدول الدراسي/ : /A timetable-planner run is using the server\. Yours starts automatically/).test(ui.$('examJobDetail').textContent));
});
test('the panel can be seen and used while the builder is busy', async t => {
  const { ui } = await optimiseAsJob(t);
  await until(() => !ui.$('examJobPanel').hidden);
  const panel = ui.$('examJobPanel');
  assert.equal(panel.closest('[inert]'), null, 'Nothing it lives in is inert');
  assert.equal(panel.closest('details'), null, 'It is not inside a section that collapses');
  assert.equal(ui.$('examJobCancel').hidden, false);
  assert.equal(ui.$('examJobCancel').getAttribute('aria-disabled'), 'false');
  assert.equal(ui.$('examEditToolbar').inert, true, 'The editor is locked meanwhile');
});

test('the page asks for a job; nothing else is answered with one', async t => {
  const { ui } = await optimiseAsJob(t);
  await until(() => !ui.$('examJobPanel').hidden);
  const submit = ui.requests.find(request => request.url === '/ops/exam-timetable/build/');
  assert.equal(submit.headers['X-Exam-Jobs'], '1');
});

test('a stage is announced once, not on every count', async t => {
  const ticks = [1, 2, 3, 4].map(done => jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'running' }, { key: 'place_exams', done, total: 10 }));
  const { ui, server } = await optimiseAsJob(t, ticks);
  const said = [];
  const observer = new ui.window.MutationObserver(() => said.push(ui.$('examJobLive').textContent));
  observer.observe(ui.$('examJobLive'), { childList: true, characterData: true, subtree: true });
  for (let step = 1; step <= 3; step += 1) {
    const shown = new RegExp(language === 'ar' ? `تم توزيع ${step} من 10` : `^${step} of 10`);
    await until(() => shown.test(ui.$('examJobDetail').textContent));
    server.advance();
  }
  await until(() => said.filter(Boolean).length > 0);
  await pause(30);
  observer.disconnect();
  assert.equal(said.filter(Boolean).length, 1, `Announced: ${JSON.stringify(said)}`);
  assert.match(said.filter(Boolean)[0], language === 'ar' ? /الخطوة 2 من 6/ : /Step 2 of 6/);
});

test('a stage passed before the announcement delay is never announced', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED.slice(0, 2), { poll: { announce: 300 } });
  const said = [];
  const observer = new ui.window.MutationObserver(() => said.push(ui.$('examJobLive').textContent));
  observer.observe(ui.$('examJobLive'), { childList: true, characterData: true, subtree: true });
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  await until(() => stageStates(ui)[2] === 'running');
  await pause(450);
  observer.disconnect();
  assert.deepEqual(said.filter(Boolean), [language === 'ar' ? 'الخطوة 3 من 6: التحقق من التعارضات والحد اليومي' : 'Step 3 of 6: Checking clashes and daily limits']);
});

// ── showing it only when it is worth showing ──

test('a job that ends before it is worth showing never flashes a panel', async t => {
  let seen;
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[2]], {
    poll: { reveal: 200, stall: 400 },
    beforeClick: ui => { seen = watchPanel(ui); },
  });
  await until(() => server.results === 1 && /alert-success/.test(ui.$('etStatus').className));
  await pause(300);
  assert.equal(seen.shownAt, null, 'A sub-second job flashed the panel');
});

test('a short job seen running is still not shown before the threshold', async t => {
  let seen;
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, {
    // Three quick polls land far inside the threshold; three steady ones would not.
    poll: { reveal: 2000, stall: 4000, first: 20, quick: 20, steady: 1000, slow: 1000, slowAfter: 60000 },
    beforeClick: (ui, server) => {
      seen = watchPanel(ui);
      server.override = async url => {
        if (url !== pollUrl(JOB_ID)) return undefined;
        server.polls += 1;
        return jobReply({ ok: true, job: server.polls <= 3 ? OPTIMISED[0] : OPTIMISED[2] });
      };
    },
  });
  await until(() => server.results === 1);
  await pause(50);
  assert.equal(server.polls, 4);
  assert.equal(seen.shownAt, null, 'Ended before the threshold, so never shown');
});
test('a job still running past the threshold is shown, and stays long enough to read', async t => {
  let seen;
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, {
    // The stall reveal is beyond the wait below, so only a poll can show it.
    poll: { reveal: 60, stall: 10000, first: 10, quick: 10, steady: 10, minShown: 300 },
    beforeClick: (ui, server) => {
      seen = watchPanel(ui);
      // Running until the page has shown it, then finished at the next poll.
      server.override = async url => {
        if (url !== pollUrl(JOB_ID)) return undefined;
        server.polls += 1;
        return jobReply({ ok: true, job: seen.shownAt === null ? OPTIMISED[0] : OPTIMISED[2] });
      };
    },
  });
  await until(() => seen.shownAt !== null, 'Shown once seen running past the threshold');
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized);
  const gap = Date.now() - seen.shownAt;
  assert.ok(gap >= 250, `Replaced after ${gap} ms`);
  assert.equal(server.results, 1);
});
test('a job whose answers never come is shown anyway, without claiming the server is busy', async t => {
  const { ui } = await optimiseAsJob(t, OPTIMISED, {
    poll: { reveal: 50, stall: 80, timeout: 5000 },
    beforeClick: (ui, server) => { server.override = async url => (url === pollUrl(JOB_ID) ? new Promise(() => {}) : undefined); },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimizing, 'Nothing was heard of a wait');
  assert.doesNotMatch(ui.$('examJobDetail').textContent, /busy|مشغول/);
});
test('focus moves to the panel when a job starts', async t => {
  const { ui } = await optimiseAsJob(t);
  await until(() => !ui.$('examJobPanel').hidden);
  assert.equal(ui.window.document.activeElement, ui.$('examJobTitle'), 'The clicked button is inert now');
  assert.ok(ui.scrollCalls.some(call => call.element === ui.$('examJobPanel')), 'and it is brought into view');
});

// ── how it ends ──

test('a finished job ends in a done state and renders the result it saved', async t => {
  const { ui, server } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  server.advance();
  await until(() => server.results === 1 && /alert-success/.test(ui.$('etStatus').className));
  assert.equal(ui.$('examJobPanel').hidden, false, 'It says what happened');
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimized);
  assert.match(ui.$('examJobDetail').textContent, TEXT.savedOwn);
  assert.match(ui.$('examJobClock').textContent, language === 'ar' ? /استغرق\s*1:42/ : /Took\s*1:42/, 'Finished minus started, not now');
  assert.equal(ui.$('examJobCancel').hidden, true);
  assert.equal(ui.$('examJobClose').hidden, false);
  assert.equal(ui.window.document.activeElement, ui.$('examEditHeading'), 'Focus goes to the result');
});

test('a failure names the stage, gives a reference, and is said once, in the panel', async t => {
  const failed = jobFrame('optimize_loaded', 'failed', { read_board: 'done', place_exams: 'stopped' }, { key: 'place_exams', done: 4, total: 10 }, { error_code: 'server_error' });
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], failed]);
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  assert.equal(stageStates(ui).join(), 'done,stopped,pending,pending,pending,pending', 'A stage never reached is never shown as done');
  assert.equal(server.results, 0, 'A failed job has no result to apply');
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimizeFailed);
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /توقفت العملية عند «توزيع الاختبارات على الأيام والفترات»/ : /It stopped at “Placing exams in days and periods”/);
  assert.match(ui.$('examJobNote').textContent, TEXT.reference);
  await until(() => statusHidden(ui), 'Not repeated in the status line');
  assert.equal(ui.$('examRequestError').hidden, true, 'nor in a banner');
  assert.equal(ui.$('examJobPanel').querySelectorAll('[aria-current]').length, 0);
  assert.equal(seenPosts(ui), 1, 'Shown, so not shown again on the next page load');
});

test('a job the server restart killed, and one that ran too long, say which', async t => {
  for (const [code, pattern] of [['server_restarted', /server restarted|أُعيد تشغيل الخادم/], ['timed_out', /time limit|الحد الأقصى لمدة التنفيذ/]]) {
    const failed = jobFrame('optimize_loaded', 'failed', { read_board: 'done', place_exams: 'stopped' }, { key: 'place_exams', done: 4, total: 10 }, { error_code: code });
    const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], failed]);
    await until(() => stageStates(ui).includes('running'));
    server.advance();
    await until(() => pattern.test(ui.$('examJobDetail').textContent), () => `${code}: ${ui.$('examJobDetail').textContent}`);
  }
});

test('Stop waits for the server, and a stopped job is news, not an error', async t => {
  const cancelled = jobFrame('optimize_loaded', 'cancelled', { read_board: 'done', place_exams: 'stopped' }, { key: 'place_exams', done: 3, total: 10 }, { error_code: 'cancelled' });
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], cancelled]);
  await until(() => stageStates(ui).includes('running'));
  ui.$('examJobCancel').click();
  await until(() => server.cancels === 1);
  assert.equal(ui.$('examJobCancel').getAttribute('aria-disabled'), 'true');
  assert.match(ui.$('examJobDetail').textContent, TEXT.stopping);
  assert.match(ui.$('examJobLive').textContent, TEXT.stopping, 'Said, not only shown');
  await pause(10);
  assert.equal(ui.$('examJobPanel').classList.contains('is-cancelled'), false, 'Not stopped until the server says so');
  assert.equal(ui.$('examJobCancel').getAttribute('aria-disabled'), 'true', 'Accepted is not stopped: still stopping');
  assert.match(ui.$('examJobDetail').textContent, TEXT.stopping);

  server.advance();
  await until(() => ui.$('examJobPanel').classList.contains('is-cancelled'));
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimizeStopped);
  assert.match(ui.$('examJobDetail').textContent, TEXT.stoppedOnScreen);
  assert.equal(ui.$('examJobPanel').classList.contains('is-failed'), false);
  assert.equal(statusHidden(ui), true);
  assert.equal(server.results, 0);
});

test('a stop the server refuses gives Stop back and says so while it is true', async t => {
  const { ui, server } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  server.override = async url => url === `/ops/exam-timetable/jobs/${JOB_ID}/cancel/` ? jobReply({ ok: false }, 500) : undefined;
  ui.$('examJobCancel').click();
  await until(() => ui.$('examJobCancel').getAttribute('aria-disabled') === 'false' && TEXT.couldNotStop.test(ui.$('examJobNote').textContent));
  await pause(20);
  assert.match(ui.$('examJobNote').textContent, TEXT.couldNotStop, 'The answer outlives the next poll');
  assert.equal(ui.$('examJobDetail').textContent, TEXT.exams310, 'The count goes on');
  server.advance();
  server.advance();
  await until(() => server.results === 1 && /alert-success/.test(ui.$('etStatus').className));
  assert.doesNotMatch(ui.$('examJobNote').textContent, TEXT.couldNotStop, 'Not "still running" once it has ended');
});

const NOTHING_MOVED = { ok: true, saved: false, minimum_change: { moves: [], unseated: [], violations_before: 0, violations_after: 0, already_overflow: 0, status: 'OPTIMAL' } };
for (const [name, ended, expected, detail, result] of [
  ['saved a timetable', finishedFrame('optimize_loaded'), TEXT.tooLate, TEXT.savedOwn, null],
  ['saved nothing', finishedFrame('optimize_loaded', { has_run: false, result_run_id: null }), TEXT.tooLateNoRun, /report under the editing toolbar|التقرير أسفل شريط أدوات التعديل/, NOTHING_MOVED],
]) {
  test(`a stop that arrives after the job ${name} says so`, async t => {
    const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], OPTIMISED[1], ended], { serverOptions: { result } });
    await until(() => stageStates(ui).includes('running'));
    server.override = async url => {
      if (url !== `/ops/exam-timetable/jobs/${JOB_ID}/cancel/`) return undefined;
      server.cancels += 1;
      return jobReply({ ok: false, error_code: 'job_finished', error: 'x', job: ended }, 409);
    };
    ui.$('examJobCancel').click();
    await until(() => server.cancels === 1 && expected.test(ui.$('examJobNote').textContent));
    server.advance();
    server.advance();
    await until(() => server.results === 1);
    await until(() => detail.test(ui.$('examJobDetail').textContent));
    assert.match(ui.$('examJobNote').textContent, expected, 'Still true once it has ended');
    if (result) assert.doesNotMatch(ui.$('examJobDetail').textContent, TEXT.savedOwn, 'Never "Saved" beside "without saving"');
  });
}

test('a stop that arrives after the job failed claims nothing was saved', async t => {
  const failed = jobFrame('optimize_loaded', 'failed', { read_board: 'done', place_exams: 'stopped' }, { key: 'place_exams', done: 4, total: 10 }, { error_code: 'server_error' });
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], failed]);
  await until(() => stageStates(ui).includes('running'));
  server.override = async url => {
    if (url !== `/ops/exam-timetable/jobs/${JOB_ID}/cancel/`) return undefined;
    server.cancels += 1;
    return jobReply({ ok: false, error_code: 'job_finished', error: 'x', job: failed }, 409);
  };
  ui.$('examJobCancel').click();
  await until(() => server.cancels === 1 && ui.$('examJobCancel').getAttribute('aria-disabled') === 'false');
  assert.doesNotMatch(ui.$('examJobNote').textContent, TEXT.tooLate);
  assert.doesNotMatch(ui.$('examJobLive').textContent, TEXT.tooLate);
  server.advance();
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
});

test('a stopped Build says it saved nothing, and nothing about a timetable on screen', async t => {
  const frames = [
    jobFrame('build', 'running', { enrolments: 'done', conflicts: 'running' }, { key: 'conflicts', done: null, total: null }),
    jobFrame('build', 'cancelled', { enrolments: 'done', conflicts: 'stopped' }, { key: 'conflicts', done: null, total: null }, { error_code: 'cancelled' }),
  ];
  const server = jobServer('build', frames);
  const ui = await page(t, { onRequest: server.onRequest });
  ui.$('buildBtn').click();
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  await until(() => ui.$('examJobPanel').classList.contains('is-cancelled'));
  assert.equal(ui.$('examJobTitle').textContent, language === 'ar' ? 'أُوقف بناء الجدول' : 'Build stopped');
  assert.equal(ui.$('examJobDetail').textContent, language === 'ar' ? 'أُوقفت العملية قبل حفظ أي شيء.' : 'Stopped before it saved anything.');
  await until(() => statusHidden(ui), 'The visible status line does not say it a second time');
});

test('the owner of a job someone else stopped is told who', async t => {
  const stopped = jobFrame('optimize_loaded', 'cancelled', { read_board: 'done', place_exams: 'stopped' }, { key: 'place_exams', done: 3, total: 10 }, { error_code: 'cancelled', cancelled_by: 'Sara Admin' });
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], stopped]);
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  await until(() => ui.$('examJobDetail').textContent.includes('⁨Sara Admin⁩'));
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /أُوقفت العملية بطلب من/ : /Stopped by/);
});

test('a result that cannot be fetched is offered, not reported as a failure', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { serverOptions: { result: () => { throw new TypeError('Failed to fetch'); } } });
  await until(() => stageStates(ui).includes('running'));
  const before = historyLoads(ui);
  server.advance();
  server.advance();
  await until(() => /could not be shown here|تعذّر عرضه هنا/.test(ui.$('examJobDetail').textContent));
  assert.equal(server.results, 3, 'Asked again before giving up: the run is already saved');
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimized, 'It was saved: a done title, never a red one');
  assert.equal(ui.$('examJobPanel').classList.contains('is-failed'), false);
  assert.equal(ui.$('examJobOpen').hidden, false, 'Offered, so nobody saves it twice');
  assert.equal(ui.$('examJobCancel').hidden, true);
  assert.doesNotMatch(ui.$('examJobNote').textContent, TEXT.reference);
  await until(() => historyLoads(ui) > before, 'Saved timetables reloaded');
});

test('a Fix that moved nothing, once shown, says why and returns to the board', async t => {
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], OPTIMISED[2]], {
    serverOptions: { result: { ok: true, saved: false, minimum_change: { moves: [], unseated: [], violations_before: 0, violations_after: 0, already_overflow: 0, status: 'OPTIMAL' } } },
  });
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  await until(() => server.results === 1);
  await until(() => /report under the editing toolbar|التقرير أسفل شريط أدوات التعديل/.test(ui.$('examJobDetail').textContent));
  assert.equal(ui.$('examJobPanel').hidden, false, 'Not a panel that vanishes under the reader');
  await until(() => ui.window.document.activeElement === ui.$('examEditHeading'), 'Back to the board, where the report is');
});

test('a Fix that moved nothing, never shown, is reported as before', async t => {
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[2]], {
    poll: { reveal: 500, stall: 1000 },
    serverOptions: { result: { ok: true, saved: false, minimum_change: { moves: [], unseated: [], violations_before: 0, violations_after: 0, already_overflow: 0, status: 'OPTIMAL' } } },
  });
  await until(() => server.results === 1);
  await until(() => /nothing was saved|لم يُحفظ شيء/.test(ui.$('etStatus').textContent));
  assert.equal(ui.$('examJobPanel').hidden, true);
});

test('a result the server has deleted is explained in the page language', async t => {
  const { ui } = await optimiseAsJob(t, [OPTIMISED[2]], {
    poll: { reveal: 500, stall: 1000 },
    serverOptions: { result: { ok: false, error_code: 'run_deleted', error: 'That run was deleted.' }, resultStatus: 410 },
  });
  await until(() => /deleted|حُذف/.test(ui.$('etStatus').textContent));
  if (language === 'ar') assert.doesNotMatch(ui.$('etStatus').textContent, /That run was deleted/);
});

test('a server that cannot start the action says so in the page language', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? jobReply({ ok: false, error_code: 'job_not_started', error: 'The server could not start this action.' }, 503)
      : undefined,
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => /could not start this action|تعذّر على الخادم بدء/.test(ui.$('etStatus').textContent + ui.$('examEditorRequestError').textContent));
  if (language === 'ar') assert.doesNotMatch(ui.$('examEditorRequestError').textContent, /The server could not/);
});

// ── when the connection or the session fails ──

test('a lost connection says it is reconnecting, clears it, and never says the job failed', async t => {
  const { ui, server } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  // Down until the page has said so: with no waits in tests, a single
  // recovered poll would replace the message before anything could read it.
  server.failures = Infinity;
  await until(() => /reconnect|إعادة الاتصال/.test(ui.$('examJobDetail').textContent));
  assert.match(ui.$('examJobLive').textContent, /reconnect|إعادة الاتصال/, 'Said once when it starts');
  assert.equal(ui.$('examJobPanel').classList.contains('is-failed'), false);
  server.failures = 0;
  await until(() => ui.$('examJobDetail').textContent === TEXT.exams310, 'The message clears once contact returns');
  server.advance();
  server.advance();
  await until(() => server.results === 1);
});

test('a poll that never answers counts as a lost connection', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { poll: { timeout: 30 } });
  await until(() => stageStates(ui).includes('running'));
  server.override = async url => (url === pollUrl(JOB_ID) ? new Promise(() => {}) : undefined);
  await until(() => /reconnect|إعادة الاتصال/.test(ui.$('examJobDetail').textContent));
});

test('a failed poll waits the backoff before asking again', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { poll: { backoff: [150, 150, 150, 150] } });
  await until(() => stageStates(ui).includes('running'));
  server.failures = Infinity;
  const before = server.polls;
  await pause(60);
  assert.ok(server.polls - before <= 2, `Polled ${server.polls - before} times inside one backoff`);
});

test('a session that ends mid-job keeps following it, and picks up again after sign-in', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { poll: { slow: 20 } });
  await until(() => stageStates(ui).includes('running'));
  let signedIn = false;
  server.override = async url => {
    if (url !== pollUrl(JOB_ID) || signedIn) return undefined;
    server.polls += 1;
    return { ok: false, status: 401, json: async () => ({}), headers: { get: () => null } };
  };
  await until(() => /session expired|انتهت جلسة/.test(ui.$('examJobDetail').textContent));
  const link = ui.$('examJobNote').querySelector('a[target="_blank"]');
  assert.ok(link && link.getAttribute('href').includes('/login/'), 'The way back in, in the panel');
  assert.equal(ui.$('examRequestError').hidden, true, 'Not also in a banner that says "retry"');
  assert.equal(ui.$('examEditorRequestError').hidden, true);
  assert.equal(ui.$('examJobPanel').classList.contains('is-failed'), false, 'It did not fail: it is still running');
  assert.equal(ui.$('examJobPanel').classList.contains('is-lost'), false);
  assert.equal(ui.$('examJobCancel').hidden, true, 'Stop would only be refused');
  const polls = server.polls;
  await until(() => server.polls > polls + 1, 'Still asking, slowly');
  assert.equal(ui.notifications.length, 0, 'No error toast: nothing failed');
  assert.match(ui.$('examJobLive').textContent, /session expired|انتهت جلسة/, 'Said once');
  signedIn = true;
  await until(() => ui.$('examJobDetail').textContent === TEXT.exams310, 'Picked up again');
  assert.equal(ui.$('examJobNote').querySelector('a'), null, 'The link goes once signed in');
  server.advance();
  server.advance();
  await until(() => server.results === 1 && ui.$('examJobTitle').textContent === TEXT.optimized, 'and its result is applied');
});

test('access withdrawn mid-job stops following it, and does not say it stopped', async t => {
  const { ui, server } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  server.override = async url => {
    if (url !== pollUrl(JOB_ID)) return undefined;
    server.polls += 1;
    return jobReply({ error: 'Exam Committee or SUPER_ADMIN access required' }, 403);
  };
  await until(() => ui.$('examJobPanel').classList.contains('is-lost'));
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimizeLost);
  assert.match(ui.$('examJobDetail').textContent, /no longer have access|لم تعد لديك صلاحية/);
  const polls = server.polls;
  await pause(40);
  assert.equal(server.polls, polls);
  if (language === 'ar') assert.doesNotMatch(ui.$('examJobDetail').textContent, /access required/);
});

test('a job that no longer exists is reported plainly, with Saved timetables reloaded', async t => {
  const { ui, server } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  const before = historyLoads(ui);
  server.gone = true;
  await until(() => ui.$('examJobPanel').classList.contains('is-lost'));
  assert.match(ui.$('examJobDetail').textContent, /Saved timetables|الجداول المحفوظة/);
  await until(() => historyLoads(ui) > before, 'The list the message points to was refreshed');
  assert.equal(ui.$('examJobClose').hidden, false);
  assert.equal(ui.$('examJobPanel').querySelectorAll('[aria-current]').length, 0, 'An ended job is on no step');
  assert.equal(ui.$('examJobClock').textContent, '', 'An ending never seen has no length to state');
});

test('a hidden tab asks nothing, and asks at once when it is shown again', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { poll: { first: 5000, steady: 5000, slow: 5000 }, beforeClick: ui => ui.setHidden(true) });
  await pause(40);
  ui.setHidden(false);
  await until(() => server.polls >= 1, 'Shown: asked at once, not after the wait');
});

test('a hidden tab polls nothing, however short the waits', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { poll: { first: 20, quick: 20, steady: 20, slow: 20 }, beforeClick: ui => ui.setHidden(true) });
  await pause(150);
  assert.equal(server.polls, 0, 'Hidden: nothing asked');
  ui.setHidden(false);
  await until(() => server.polls >= 1);
});
// ── someone else's job, a refusal, and a page opened later ──

function theirs(kind, status, states, key, extra = {}) {
  return jobFrame(kind, status, states, key ? { key, done: null, total: null } : null, { mine: false, can_cancel: false, owner: 'Huda', ...extra });
}

test('a refused action is shown where the registrar is looking, with whose job it waits for', async t => {
  const running = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  // Started after this page opened: the page learns of it only from the refusal.
  let started = false;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        started = true;
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda', stage: 'conflicts' } }, 409);
      }
      if (isActive(url)) return jobReply({ ok: true, job: started ? running : null });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: running });
      return undefined;
    },
  });
  assert.equal(ui.$('examJobPanel').hidden, true, 'Nothing was running when the page opened');
  const action = buttonLabel(ui.$('optimizeLoadedBtn'));
  ui.$('optimizeLoadedBtn').click();
  await until(() => ui.$('examJobNote').textContent.includes(action));
  assert.ok(ui.$('examJobNote').textContent.includes('⁨Huda⁩'));
  assert.match(ui.$('examJobNote').textContent, language === 'ar' ? /لم يبدأ/ : /did not start/);
  assert.equal(ui.window.document.activeElement, ui.$('examJobTitle'), 'Where the answer is');
  assert.match(ui.$('examJobLive').textContent, language === 'ar' ? /لم يبدأ/ : /did not start/, 'and said');
  assert.equal(ui.$('examJobCancel').hidden, true);
  await until(() => statusHidden(ui), 'Not an error, and not said twice');
  assert.equal(ui.$('examEditorRequestError').hidden, true);
});

test('refused because my own job is still running says so', async t => {
  const mineRunning = jobFrame('build', 'running', { enrolments: 'done', conflicts: 'running' }, { key: 'conflicts', done: null, total: null });
  let started = false;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        started = true;
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: true, owner: 'me' } }, 409);
      }
      if (isActive(url)) return jobReply({ ok: true, job: started ? mineRunning : null });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: mineRunning });
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => /a timetable action you started is still running|ما زالت عملية بدأتها/.test(ui.$('examJobNote').textContent));
  assert.equal(ui.$('examJobCancel').hidden, false, 'My own job, from another tab: it can be stopped here');
});

function isSeen(element) {
  return !element.hidden && !element.closest('[hidden], .d-none, details:not([open])');
}

test('a refusal whose job has already ended is told at the board: try again now', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409)
      : undefined,
  });
  const action = buttonLabel(ui.$('optimizeLoadedBtn'));
  ui.$('optimizeLoadedBtn').click();
  await until(() => isSeen(ui.$('examEditorNotice')));
  const notice = ui.$('examEditorNotice').textContent;
  assert.ok(notice.includes(action), notice);
  assert.match(notice, language === 'ar' ? /أعد المحاولة الآن/ : /Try again now/);
  assert.equal(ui.$('examJobLive').textContent, notice, 'and said aloud');
  ui.$('examSetupDetails').open = true;
  assert.equal(isSeen(ui.$('etStatus')), false, 'Nothing stale waits in the setup section');
});

test('a refusal is told at the board even with an older ending on screen', async t => {
  let submits = 0;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        submits += 1;
        return submits === 1
          ? jobReply({ ok: true, job: jobFrame('optimize_loaded', 'queued') }, 202)
          : jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409);
      }
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: failedFrame('optimize_loaded') });
      if (url === seenUrl(JOB_ID)) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => !ui.$('examJobClose').hidden && !ui.$('optimizeLoadedBtn').disabled);
  ui.$('optimizeLoadedBtn').click();
  await until(() => isSeen(ui.$('examEditorNotice')));
  assert.match(ui.$('examEditorNotice').textContent, language === 'ar' ? /أعد المحاولة الآن/ : /Try again now/);
});
test("while someone else's job runs, the actions it would refuse are unavailable", async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  let index = 0;
  const ui = await loadedEditor(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  await until(() => ui.$('optimizeLoadedBtn').disabled && ui.$('minChangeBtn').disabled);
  assert.match(ui.$('examJobNote').textContent, language === 'ar' ? /لا يمكن البناء أو التحسين/ : /are unavailable until it finishes/);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  await until(() => !ui.$('optimizeLoadedBtn').disabled && !ui.$('minChangeBtn').disabled, 'Free again once it has ended');
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /يمكنك الآن استخدام البناء/ : /You can use Build, Optimize, Fix and Save again\.$/);
  assert.equal(seenPosts(ui), 0, "Someone else's job is not this registrar's to mark seen");
});

test("a page opened while someone else's job runs follows it, names them, and offers no stop", async t => {
  const running = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts', { owner: 'Huda Saleh' });
  const ui = await page(t, {
    activeJob: { ok: true, job: running },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: running }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  assert.equal(ui.$('examJobCancel').hidden, true, 'Only the person who started it may stop it');
  assert.equal(ui.$('examJobTitle').textContent, TEXT.building);
  assert.ok(ui.$('examJobNote').textContent.includes('⁨Huda Saleh⁩'), 'The name is bidi-isolated');
  assert.notEqual(ui.window.document.activeElement, ui.$('examJobTitle'), 'Opening the page does not move focus');
});

test("a colleague's job that saved a timetable says whose it is and offers to open it", async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda', result_run_id: 77 }),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url === '/ops/exam-timetable/77/') return response({ ...savedRun(), run_id: 77 });
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => !ui.$('examJobOpen').hidden);
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /حُفظت النتيجة جدولاً جديداً بطلب من/ : /Saved as a new timetable by/);
  ui.$('examJobOpen').click();
  await until(() => ui.$('examJobPanel').hidden);
  assert.equal(seenPosts(ui), 0);
});

test("a page reopened during the registrar's own job can stop it, then offers the result", async t => {
  const frames = [
    jobFrame('build', 'running', { enrolments: 'done', conflicts: 'running' }, { key: 'conflicts', done: null, total: null }),
    finishedFrame('build'),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  assert.equal(ui.$('examJobCancel').hidden, false, 'Own job: it can be stopped');
  ui.$('examJobCancel').focus();
  const before = historyLoads(ui);
  index = 1;
  await until(() => !ui.$('examJobOpen').hidden, 'Own finished job offered');
  assert.ok(historyLoads(ui) > before, 'Saved timetables reloaded');
  assert.equal(ui.window.document.activeElement, ui.$('examJobTitle'), 'Stop had focus and is gone: focus stays in the panel');
});

test('a job that failed while the page was closed is shown once', async t => {
  const ui = await page(t, {
    activeJob: { ok: true, job: failedFrame('build') },
    onRequest: async url => (url === seenUrl(JOB_ID) ? jobReply({ ok: true, marked: true }) : undefined),
  });
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  assert.equal(ui.$('examJobTitle').textContent, language === 'ar' ? 'تعذّر بناء الجدول' : 'Build failed');
  assert.equal(ui.$('examJobClose').hidden, false);
  await until(() => seenPosts(ui) === 1, 'Marked seen, so the next page load does not show it again');
});

test('a Save of mine refused while the page was closed is shown once, with why', async t => {
  let results = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: finishedFrame('save_loaded_changes', { has_run: false, result_run_id: null, refused: true }) },
    onRequest: async url => {
      if (url !== `/ops/exam-timetable/jobs/${JOB_ID}/result/`) return undefined;
      results += 1;
      // A refusal the page has no words of its own for: the server's, kept in order.
      return jobReply({ ok: false, error: 'The course list changed.' }, 409);
    },
  });
  await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
  assert.equal(ui.$('examJobTitle').textContent, language === 'ar' ? 'لم تُحفظ التغييرات' : 'The changes were not saved');
  assert.match(ui.$('examJobDetail').textContent, /The course list changed\./);
  if (language === 'ar') assert.ok(ui.$('examJobDetail').textContent.includes('⁨The course list changed.⁩'), 'Isolated inside Arabic');
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /لم يُحفظ جدول جديد/ : /No new timetable was saved/);
  assert.equal(results, 1, 'Its stored answer was read, which also marks it seen');
  assert.equal(ui.$('examJobOpen').hidden, true);
  assert.equal(ui.$('examJobClose').hidden, false);
});

test("a colleague's refused Build is not headlined as built, and frees the actions", async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda', has_run: false, result_run_id: null, refused: true }),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
  assert.equal(ui.$('examJobTitle').textContent, language === 'ar' ? 'لم يُبنَ جدول' : 'No timetable was built');
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /يمكنك الآن استخدام البناء/ : /You can use Build, Optimize, Fix and Save again\.$/);
  assert.match(ui.$('examJobLive').textContent, language === 'ar' ? /^لم يُبنَ جدول/ : /^No timetable was built/);
});

test("a colleague's Fix that moved nothing is a finished Fix, not a refusal", async t => {
  const frames = [
    theirs('minimum_change_repair', 'running', { read_board: 'done', fewest_moves: 'running' }, 'fewest_moves'),
    finishedFrame('minimum_change_repair', { mine: false, can_cancel: false, owner: 'Huda', has_run: false, result_run_id: null }),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => !ui.$('examJobClose').hidden);
  assert.equal(ui.$('examJobTitle').textContent, language === 'ar' ? 'اكتمل الإصلاح' : 'Fix finished');
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /لم يُنقل أي اختبار/ : /No exam was moved/);
});
test('Open loads the saved run and only then stops offering it', async t => {
  let seen = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: finishedFrame('build', { result_run_id: 77 }) },
    onRequest: async url => {
      if (url === '/ops/exam-timetable/77/') return response({ ...savedRun(), run_id: 77 });
      if (url === seenUrl(JOB_ID)) { seen += 1; return jobReply({ ok: true, marked: true }); }
      return undefined;
    },
  });
  await until(() => !ui.$('examJobOpen').hidden);
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /بينما كانت الصفحة مغلقة/ : /while this page was closed/);
  assert.equal(seen, 0, 'Offered, not yet seen');
  ui.$('examJobOpen').click();
  await until(() => ui.requests.some(request => request.url === '/ops/exam-timetable/77/'));
  await until(() => seen === 1 && ui.$('examJobPanel').hidden);
  assert.equal(ui.requests.some(request => request.url.endsWith('/result/')), false, 'Opened by its run, not by fetching a 1 MB result');
});

test('Open keeps offering the result when the registrar keeps the draft on screen', async t => {
  let seen = 0;
  const ui = await loadedEditor(t, {
    activeJob: { ok: true, job: finishedFrame('build', { result_run_id: 77 }) },
    onRequest: async url => url === seenUrl(JOB_ID) ? (seen += 1, jobReply({ ok: true, marked: true })) : undefined,
  });
  await until(() => !ui.$('examJobOpen').hidden);
  dropExam(ui, 'Mon');
  let answer;
  ui.window.dlg.confirm = options => { ui.dialogs.push(options); return new Promise(resolve => { answer = resolve; }); };
  const asked = ui.dialogs.length;
  ui.$('examJobOpen').click();
  await until(() => ui.dialogs.length === asked + 1, 'Asked before discarding the draft');
  assert.equal(ui.$('examJobOpen').getAttribute('aria-disabled'), 'true', 'Busy while it loads');
  assert.equal(ui.$('examJobOpen').disabled, false, 'aria-disabled, never disabled: focus stays on it');
  ui.$('examJobOpen').click();
  await pause(10);
  assert.equal(ui.dialogs.length, asked + 1, 'A second click starts nothing');
  answer(false);
  await until(() => ui.$('examJobOpen').getAttribute('aria-disabled') === 'false');
  assert.equal(seen, 0, 'Declined: still unseen');
  assert.equal(ui.$('examJobPanel').hidden, false);
  assert.equal(ui.requests.some(request => request.url === '/ops/exam-timetable/77/'), false);
});
test('Close stops offering a result and puts focus somewhere it can be seen', async t => {
  let seen = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: finishedFrame('build') },
    onRequest: async url => url === seenUrl(JOB_ID) ? (seen += 1, jobReply({ ok: true, marked: true })) : undefined,
  });
  await until(() => !ui.$('examJobClose').hidden);
  ui.$('examJobClose').focus();
  ui.$('examJobClose').click();
  await until(() => seen === 1);
  assert.equal(ui.$('examJobPanel').hidden, true);
  assert.equal(ui.window.document.activeElement, ui.$('examSetupSummary'), 'Nothing is loaded, so the setup, not a hidden heading');
});

test('Close after my own failed action returns focus to the button that started it', async t => {
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], failedFrame('optimize_loaded')]);
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  await until(() => !ui.$('examJobClose').hidden && !ui.$('optimizeLoadedBtn').disabled);
  ui.$('examJobClose').click();
  assert.equal(ui.$('examJobPanel').hidden, true);
  assert.equal(ui.window.document.activeElement, ui.$('optimizeLoadedBtn'));
});

test('a followed job that disappears ends as lost, with Close', async t => {
  const running = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  let gone = false;
  const ui = await page(t, {
    activeJob: { ok: true, job: running },
    onRequest: async url => url === pollUrl(JOB_ID)
      ? (gone ? jobReply({ ok: false, error_code: 'job_not_found', error: 'Job not found' }, 404) : jobReply({ ok: true, job: running }))
      : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  const before = historyLoads(ui);
  gone = true;
  await until(() => ui.$('examJobPanel').classList.contains('is-lost'));
  await until(() => historyLoads(ui) > before, 'The list the message points to was refreshed');
  assert.equal(ui.$('examJobClose').hidden, false);
  assert.equal(ui.$('examJobTitle').textContent, language === 'ar' ? 'تعذّرت متابعة بناء الجدول' : 'Lost track of the build');
});

test("stopping a colleague's job asks first, naming them", async t => {
  const running = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts', { can_cancel: true, owner: 'Huda' });
  const ui = await page(t, {
    activeJob: { ok: true, job: running },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: running }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  assert.equal(ui.$('examJobCancel').hidden, false, 'A SUPER_ADMIN may stop it');
  ui.$('examJobCancel').click();
  await until(() => ui.dialogs.length === 1);
  assert.ok(ui.dialogs[0].title.includes('Huda'));
  assert.equal(ui.dialogs[0].confirmLabel, language === 'ar' ? 'إيقاف العملية' : 'Stop it');
  await pause(20);
  assert.equal(ui.requests.some(request => request.url.endsWith('/cancel/')), false, 'Declined: nothing stopped');
  assert.equal(ui.$('examJobCancel').getAttribute('aria-disabled'), 'false');
});

test('a job ending in the background keeps the keyboard where it was in Saved timetables', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  let index = 0;
  const ui = await page(t, {
    history: [{ id: 17, label: 'One' }, { id: 18, label: 'Two' }],
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  await until(() => ui.$('historyList').querySelectorAll('.et-copy-btn').length === 2);
  ui.$('historyList').querySelectorAll('.et-copy-btn')[1].focus();
  const before = historyLoads(ui);
  index = 1;
  await until(() => historyLoads(ui) > before && !ui.$('examJobOpen').hidden);
  await settle();
  const focused = ui.window.document.activeElement;
  assert.ok(focused.classList.contains('et-copy-btn'), `Focus is on ${focused.id || focused.className}`);
  assert.equal(focused.dataset.id, '18');
});

// ── two jobs: the panel follows one, and never the wrong one ──

test('a new action takes away an ended panel at once, so its buttons never act on the wrong job', async t => {
  let submits = 0;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        submits += 1;
        return jobReply({ ok: true, job: { ...jobFrame('optimize_loaded', 'queued'), id: submits === 1 ? JOB_ID : JOB_B } }, 202);
      }
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: failedFrame('optimize_loaded') });
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: { ...OPTIMISED[0], id: JOB_B } });
      if (url === seenUrl(JOB_ID)) return jobReply({ ok: true, marked: true });
      return undefined;
    },
    poll: { reveal: 5000, stall: 5000 },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => !ui.$('examJobClose').hidden && !ui.$('optimizeLoadedBtn').disabled);
  ui.scrollCalls.length = 0;
  ui.$('optimizeLoadedBtn').click();
  await until(() => submits === 2);
  await until(() => ui.$('examJobPanel').hidden, 'The ended panel goes at once');
  await pause(30);
  assert.equal(ui.$('examJobPanel').hidden, true, 'and the new job is not shown before its time');
  assert.equal(ui.scrollCalls.some(call => call.element === ui.$('examJobPanel')), false, 'No jump to the top');
});

test('after an ended panel, a quick action is still never shown and never waits', async t => {
  let submits = 0;
  const ui = await loadedEditor(t, {
    onRequest: async (url, options) => {
      if (url === '/ops/exam-timetable/build/') {
        submits += 1;
        return jobReply({ ok: true, job: { ...jobFrame('optimize_loaded', 'queued'), id: submits === 1 ? JOB_ID : JOB_B } }, 202);
      }
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: failedFrame('optimize_loaded') });
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: { ...OPTIMISED[2], id: JOB_B } });
      if (url === `/ops/exam-timetable/jobs/${JOB_B}/result/`) return response({ ...evaluatedRun(JSON.parse(ui.requests.filter(r => r.url === '/ops/exam-timetable/build/').at(-1).body)), run_id: 94 });
      if (url === seenUrl(JOB_ID)) return jobReply({ ok: true, marked: true });
      return undefined;
    },
    poll: { reveal: 400, stall: 2000, minShown: 1500 },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => !ui.$('examJobClose').hidden && !ui.$('optimizeLoadedBtn').disabled);
  const seen = watchPanel(ui);
  ui.scrollCalls.length = 0;
  const started = Date.now();
  ui.$('optimizeLoadedBtn').click();
  await until(() => /alert-success/.test(ui.$('etStatus').className));
  assert.ok(Date.now() - started < 1000, `Took ${Date.now() - started} ms: no minimum-shown wait for a panel never shown`);
  assert.equal(seen.shownAt, null);
  assert.equal(ui.$('examJobPanel').hidden, true);
  assert.equal(ui.scrollCalls.some(call => call.element === ui.$('examJobPanel')), false);
});
test('a page-load answer arriving after my own action started never takes its panel', async t => {
  const theirsRunning = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  let releaseActive;
  const activeAnswered = new Promise(resolve => { releaseActive = resolve; });
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (isActive(url)) {
        await activeAnswered;
        return jobReply({ ok: true, job: theirsRunning });
      }
      if (url === '/ops/exam-timetable/build/') return jobReply({ ok: true, job: { ...jobFrame('optimize_loaded', 'queued'), id: JOB_B } }, 202);
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: theirsRunning });
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: { ...OPTIMISED[0], id: JOB_B } });
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => stageStates(ui).length === 6, 'Following my optimise');
  releaseActive();
  for (let sample = 0; sample < 30; sample += 1) {
    await pause(2);
    assert.equal(stageStates(ui).length, 6, `sample ${sample}: another job's stages were drawn`);
  }
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimizing);
  assert.equal(ui.$('examJobCancel').hidden, false, 'My running job kept its Stop');
});

// ── Check ──

test('a Check turned away because the solver is busy waits, and is not an error', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/draft-impact/'
      ? jobReply({ ok: false, error_code: 'solver_busy', error: 'Another timetable action is using the solver.', holder: { kind: 'planner', seconds: 240 } }, 503)
      : undefined,
  });
  dropExam(ui, 'Wed');
  ui.$('checkDraftBtn').click();
  await until(() => ui.requests.some(request => request.url === '/ops/exam-timetable/draft-impact/'));
  await until(() => /Waiting for the server|في انتظار الخادم/.test(ui.$('examCheckStatus').textContent));
  assert.match(ui.$('draftImpactBanner').textContent, language === 'ar' ? /تخطيط الجدول الدراسي.*أعد الفحص عند انتهائها/ : /timetable-planner run is using the server\. Check again when it finishes/);
  assert.equal(ui.$('examEditorRequestError').hidden, true, 'No error banner');
  assert.equal(ui.notifications.length, 0, 'and no error toast');
  if (language === 'ar') assert.doesNotMatch(ui.$('draftImpactBanner').textContent, /solver/);
});

test('a live check the solver turned away is tried again, quietly', async t => {
  let asked = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 30 },
    onRequest: async url => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      asked += 1;
      return asked === 1 ? jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'check', seconds: 1 } }, 503) : undefined;
    },
  });
  dropExam(ui, 'Tue');
  await until(() => asked >= 1, 'The live check ran');
  await until(() => asked >= 2, 'and ran again after the wait');
  await until(() => /Checked|تم التحقق/.test(ui.$('examCheckStatus').textContent));
  assert.equal(ui.notifications.length, 0);
});

// ── what assistive technology is told ──

test('the panel is labelled, and each stage says its state in words', async t => {
  const { ui } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  assert.equal(ui.$('examJobLive').getAttribute('role'), 'status');
  assert.equal(ui.$('examJobLive').getAttribute('aria-live'), 'polite');
  assert.equal(ui.$('examJobPanel').contains(ui.$('examJobLive')), false, 'Not inside the panel that starts hidden');
  assert.equal(ui.$('examJobPanel').getAttribute('aria-labelledby'), 'examJobTitle');
  const stages = ui.$('examJobStages');
  assert.equal(stages.querySelectorAll('[aria-current]').length, 1);
  assert.equal(stages.querySelector('.is-running').getAttribute('aria-current'), 'step');
  assert.match(stages.querySelector('.is-running').textContent, language === 'ar' ? /\(قيد التنفيذ\)/ : /\(in progress\)/);
  assert.match(stages.querySelector('.is-done').textContent, language === 'ar' ? /\(تمّ\)/ : /\(done\)/);
  assert.match(stages.querySelector('.is-pending').textContent, language === 'ar' ? /\(في الانتظار\)/ : /\(not started\)/);
});

test('the stage list is not rewritten while nothing in it changes', async t => {
  const ticks = [1, 2, 3].map(done => jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'running' }, { key: 'place_exams', done, total: 10 }));
  const { ui, server } = await optimiseAsJob(t, ticks);
  await until(() => stageStates(ui).includes('running'));
  const first = ui.$('examJobStages').firstElementChild;
  server.advance();
  server.advance();
  await until(() => ui.$('examJobDetail').textContent.startsWith(language === 'ar' ? 'تم توزيع 3' : '3 of'));
  assert.equal(ui.$('examJobStages').firstElementChild, first, "A screen reader's place in the list survives a count");
});

test("someone else's job is announced when it starts, with whose it is, not stage by stage", async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'running', { enrolments: 'done', conflicts: 'done', place_exams: 'running' }, 'place_exams'),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => stageStates(ui)[1] === 'running');
  const said = ui.$('examJobLive').textContent;
  assert.ok(said.startsWith(language === 'ar' ? 'جارٍ بناء الجدول.' : 'Building the timetable.'), said);
  assert.ok(said.includes('⁨Huda⁩'), said);
  index = 1;
  await until(() => stageStates(ui)[2] === 'running');
  await pause(30);
  assert.equal(ui.$('examJobLive').textContent, said);
});

test('elapsed time is measured on the server clock, not this computer', async t => {
  const { ui } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  // Started 10:00:01 by the server, which said it was 10:00:05 when it took the job.
  assert.match(ui.$('examJobClock').textContent, /0:0[4-6]/);
  assert.match(ui.$('examJobClock').textContent, language === 'ar' ? /المدة المنقضية/ : /Elapsed/);
});

test("while someone else's job runs, Build and Save are unavailable too", async t => {
  const running = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  const fresh = await page(t, {
    activeJob: { ok: true, job: running },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: running }) : undefined,
  });
  await until(() => !fresh.$('examJobPanel').hidden);
  assert.equal(fresh.$('buildBtn').disabled, true, 'Courses are loaded, but the lane is held');
  const loaded = await loadedEditor(t, {
    activeJob: { ok: true, job: running },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: running }) : undefined,
  });
  await until(() => !loaded.$('examJobPanel').hidden);
  dropExam(loaded, 'Mon');
  await settle();
  assert.equal(loaded.$('saveLoadedBtn').disabled, true, 'An unsaved draft, but the lane is held');
});

test('a Check turned away by an exam job shows that job, and checks the moment it ends', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  let index = 0;
  let started = false;
  let checks = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, job: started ? frames[0] : null });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      checks += 1;
      if (checks > 1) return undefined;
      started = true;
      return jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'exam_job', seconds: 3 } }, 503);
    },
  });
  dropExam(ui, 'Tue');
  await until(() => checks === 1 && /Waiting for the server|في انتظار الخادم/.test(ui.$('examCheckStatus').textContent));
  await until(() => !ui.$('examJobPanel').hidden, 'The job holding the solver is shown');
  assert.match(ui.$('draftImpactBanner').textContent, language === 'ar' ? /جدول الاختبارات/ : /Another exam timetable action is using the server/);
  index = 1;
  await until(() => checks === 2, 'Checked as soon as it ended, not after the long wait');
});

test("a busy answer's Retry-After says when to try again", async t => {
  let checks = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      checks += 1;
      return checks === 1
        ? jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'check', seconds: 1 } }, 503, { 'Retry-After': '1' })
        : undefined;
    },
  });
  dropExam(ui, 'Tue');
  await until(() => checks === 1);
  await until(() => checks === 2, 'Asked again after the second the server named');
});

test('a Save the busy solver turned away says to save again, in the page language', async t => {
  const ui = await loadedEditor(t, {
    liveUpdate: false,
    onRequest: async url => url === '/ops/exam-timetable/draft-impact/'
      ? jobReply({ ok: false, error_code: 'solver_busy', error: 'Another timetable action is using the solver.', holder: { kind: 'planner', seconds: 240 } }, 503)
      : undefined,
  });
  dropExam(ui, 'Wed');
  ui.$('saveLoadedBtn').click();
  await until(() => /Waiting for the server|في انتظار الخادم/.test(ui.$('examCheckStatus').textContent));
  assert.match(ui.$('draftImpactBanner').textContent, language === 'ar' ? /أعد الحفظ عند انتهائها/ : /Save again when it finishes/);
  await until(() => /Try again when it finishes|أعد المحاولة عند انتهائها/.test(ui.$('etStatus').textContent));
  assert.match(ui.$('etStatus').className, /alert-info/, 'Not an error');
  if (language === 'ar') assert.doesNotMatch(ui.$('etStatus').textContent, /solver/);
  assert.equal(ui.notifications.length, 0);
  assert.equal(ui.requests.some(request => request.url === '/ops/exam-timetable/build/'), false, 'Nothing was saved');
});

// ── the final review's findings ──

test('a job known to have ended is never shown as running while its result loads', async t => {
  let seen;
  let release;
  const resultArrives = new Promise(resolve => { release = resolve; });
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[2]], {
    poll: { reveal: 200, stall: 100 },
    beforeClick: (ui, server) => {
      seen = watchPanel(ui);
      server.override = async url => {
        if (url !== `/ops/exam-timetable/jobs/${JOB_ID}/result/`) return undefined;
        server.results += 1;
        await resultArrives;
        return response({ ...evaluatedRun(server.submitted), run_id: 93 });
      };
    },
  });
  await until(() => server.results === 1);
  await pause(250);
  assert.equal(seen.shownAt, null, 'The stall reveal did not fire for a job already over');
  release();
  await until(() => /alert-success/.test(ui.$('etStatus').className));
  assert.equal(seen.shownAt, null);
});

test('a shown job that has ended offers no Stop while its result loads', async t => {
  let release;
  const resultArrives = new Promise(resolve => { release = resolve; });
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, {
    beforeClick: (ui, server) => {
      server.override = async url => {
        if (url !== `/ops/exam-timetable/jobs/${JOB_ID}/result/`) return undefined;
        server.results += 1;
        await resultArrives;
        return response({ ...evaluatedRun(server.submitted), run_id: 93 });
      };
    },
  });
  await until(() => stageStates(ui).includes('running'));
  assert.equal(ui.$('examJobCancel').hidden, false);
  server.advance();
  server.advance();
  await until(() => server.results === 1);
  assert.equal(ui.$('examJobCancel').hidden, true, 'Nothing is left to stop');
  assert.match(ui.$('examJobClock').textContent, language === 'ar' ? /استغرق/ : /Took/, 'The clock stops at its end');
  release();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized);
});

test('a Check turned away by an exam job shows it even with an older ending on screen', async t => {
  const colleague = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  let checks = 0;
  let started = false;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    activeJob: { ok: true, job: failedFrame('build') },
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, job: started ? colleague : failedFrame('build') });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague });
      if (url === seenUrl(JOB_ID)) return jobReply({ ok: true, marked: true });
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      checks += 1;
      started = true;
      return jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'exam_job', seconds: 2 } }, 503);
    },
  });
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'), 'An older ending on screen');
  dropExam(ui, 'Tue');
  await until(() => checks >= 1);
  await until(() => ui.$('examJobTitle').textContent === TEXT.building, 'The job holding the solver is shown');
  await until(() => ui.$('optimizeLoadedBtn').disabled && ui.$('minChangeBtn').disabled);
});

test('a live check waiting on a busy solver does not repeat itself', async t => {
  let checks = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 15 },
    onRequest: async url => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      checks += 1;
      return jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'check', seconds: 1 } }, 503);
    },
  });
  dropExam(ui, 'Tue');
  await until(() => checks >= 1);
  await until(() => /Waiting for the server|في انتظار الخادم/.test(ui.$('examCheckStatus').textContent));
  const said = [];
  let repaints = 0;
  new ui.window.MutationObserver(() => said.push(ui.$('examCheckStatus').textContent))
    .observe(ui.$('examCheckStatus'), { childList: true, characterData: true, subtree: true });
  new ui.window.MutationObserver(() => { repaints += 1; })
    .observe(ui.$('examChangesContent'), { childList: true });
  const before = checks;
  await until(() => checks >= before + 4, 'It kept trying');
  assert.equal(said.length, 0, `The check line spoke again: ${JSON.stringify(said)}`);
  assert.equal(repaints, 0, 'Nothing changed, so the board was not redrawn');
  assert.match(ui.$('examCheckStatus').textContent, /Waiting for the server|في انتظار الخادم/);
});

test('a poll whose body never arrives counts as a lost connection', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { poll: { timeout: 40 } });
  await until(() => stageStates(ui).includes('running'));
  server.override = async url => url === pollUrl(JOB_ID)
    ? { ok: true, status: 200, json: () => new Promise(() => {}), headers: { get: name => (name === 'content-type' ? 'application/json' : null) } }
    : undefined;
  await until(() => /reconnect|إعادة الاتصال/.test(ui.$('examJobDetail').textContent));
});

test('a job ending in the background keeps the keyboard on the pager', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  let index = 0;
  const runs = Array.from({ length: 10 }, (_, i) => ({ id: 30 + i, label: `Run ${i}` }));
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url.startsWith('/ops/exam-timetable/list/')) return response({ ok: true, runs, total: 25, total_pages: 3, page: 1 });
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  await until(() => ui.$('historyPages').querySelector('[data-history-nav="next"]'));
  ui.$('historyPages').querySelector('[data-history-nav="next"]').focus();
  const before = historyLoads(ui);
  index = 1;
  await until(() => historyLoads(ui) > before && !ui.$('examJobOpen').hidden);
  await settle();
  assert.equal(ui.window.document.activeElement.dataset.historyNav, 'next');
});

test('a focused run pushed off the page leaves the keyboard on the row now in its place', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  let index = 0;
  let runs = Array.from({ length: 10 }, (_, i) => ({ id: 40 - i, label: `Run ${i}` }));
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url.startsWith('/ops/exam-timetable/list/')) return response({ ok: true, runs, total: 10, total_pages: 1, page: 1 });
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  await until(() => ui.$('historyList').querySelectorAll('.et-copy-btn').length === 10);
  ui.$('historyList').querySelectorAll('.et-copy-btn')[9].focus();
  // The colleague's new run arrives at the top, pushing the tenth row off.
  runs = [{ id: 41, label: 'New' }, ...runs.slice(0, 9)];
  const before = historyLoads(ui);
  index = 1;
  await until(() => historyLoads(ui) > before && !ui.$('examJobOpen').hidden);
  await settle();
  const focused = ui.window.document.activeElement;
  assert.ok(focused.classList.contains('et-copy-btn'), `Focus is on ${focused.tagName}.${focused.className}`);
  assert.equal(focused.closest('.et-history-item').dataset.id, '32', 'The row now tenth');
});

test('Optimize with unsaved edits says to keep the page open, not that it may be left', async t => {
  const { ui } = await optimiseAsJob(t, OPTIMISED, { beforeClick: ui => dropExam(ui, 'Mon') });
  await until(() => stageStates(ui).includes('running'));
  assert.match(ui.$('examJobNote').textContent, language === 'ar' ? /أبقِ هذه الصفحة مفتوحة/ : /Keep this page open/);
});

test("a stop found on opening names no timetable on screen: this page never had one", async t => {
  const stopped = jobFrame('optimize_loaded', 'cancelled', { read_board: 'done', place_exams: 'stopped' }, { key: 'place_exams', done: 3, total: 10 },
    { error_code: 'cancelled', finished_at: '2026-09-23T10:00:30+00:00' });
  const ui = await page(t, {
    activeJob: { ok: true, job: stopped },
    onRequest: async url => (url === seenUrl(JOB_ID) ? jobReply({ ok: true, marked: true }) : undefined),
  });
  await until(() => ui.$('examJobPanel').classList.contains('is-cancelled'));
  // Said to be the registrar's own, then how it ended - with no timetable on
  // screen - and where the draft it took is: only on the page that asked.
  const detail = ui.$('examJobDetail').textContent;
  assert.ok(detail.startsWith(W.away), `Said: ${detail}`);
  endsWith(detail, language === 'ar' ? 'أُوقفت العملية قبل حفظ أي شيء.' : 'Stopped before it saved anything.', W.draft);
});

test('an action whose report could not be loaded, and that saved nothing, never says "Saved"', async t => {
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], finishedFrame('optimize_loaded', { has_run: false, result_run_id: null })], {
    serverOptions: { result: () => { throw new TypeError('Failed to fetch'); } },
  });
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  await until(() => /could not be loaded here|تعذّر تحميل تقريرها/.test(ui.$('examJobDetail').textContent));
  assert.doesNotMatch(ui.$('examJobDetail').textContent, /^Saved|^حُفظ/);
  assert.equal(ui.$('examJobOpen').hidden, true);
});

test("a colleague's job waiting its turn is shown as waiting, not as running", async t => {
  const waiting = theirs('build', 'queued', {}, null, { started_at: null, can_cancel: true, owner: 'Huda' });
  const ui = await page(t, {
    activeJob: { ok: true, job: waiting },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: waiting }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  assert.equal(ui.$('examJobTitle').textContent, language === 'ar' ? 'بناء الجدول في الانتظار' : 'Waiting to build the timetable');
  assert.match(ui.$('examJobNote').textContent, language === 'ar' ? /^أُضيفت هذه العملية إلى الانتظار.*بطلب من/ : /^Requested by/);
  if (language === 'ar') assert.doesNotMatch(ui.$('examJobNote').textContent, /طلبها/, 'A verb that agrees with any name');
  assert.doesNotMatch(ui.$('examJobNote').textContent, /Started|بدأت/);
  assert.equal(ui.$('examJobClock').textContent, '', 'No running time for a job that has not run');
  ui.$('examJobCancel').click();
  await until(() => ui.dialogs.length === 1);
  assert.match(ui.dialogs[0].body, language === 'ar' ? /ولم تبدأ بعد/ : /has not started/);
});

test("stopping a job whose owner's account is gone names nobody", async t => {
  const running = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts', { can_cancel: true, owner: '' });
  const ui = await page(t, {
    activeJob: { ok: true, job: running },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: running }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  ui.$('examJobCancel').click();
  await until(() => ui.dialogs.length === 1);
  assert.equal(ui.dialogs[0].title, language === 'ar' ? 'إيقاف هذه العملية؟' : 'Stop this action?');
  assert.doesNotMatch(ui.dialogs[0].body, /will see|وسيظهر/);
});

test('a job that ends while the stop dialog is open is still announced', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts', { can_cancel: true }),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { can_cancel: true, error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  let answer;
  const dialogButton = ui.window.document.createElement('button');
  const main = ui.$('main-content');
  ui.window.dlg.confirm = options => {
    ui.dialogs.push(options);
    // As dialog.js does: the page is hidden from assistive technology, and
    // focus is in the dialog until it closes.
    main.setAttribute('aria-hidden', 'true');
    ui.window.document.body.append(dialogButton);
    dialogButton.focus();
    return new Promise(resolve => {
      answer = value => { main.removeAttribute('aria-hidden'); dialogButton.remove(); resolve(value); };
    });
  };
  ui.$('examJobCancel').click();
  await until(() => ui.dialogs.length === 1);
  const before = ui.$('examJobLive').textContent;
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  await pause(20);
  assert.equal(ui.$('examJobLive').textContent, before, 'Not said into a page nobody can hear');
  answer(true);
  await until(() => /^Build failed|^تعذّر بناء الجدول/.test(ui.$('examJobLive').textContent), 'Its ending said again');
  assert.equal(ui.window.document.activeElement, ui.$('examJobTitle'));
  assert.equal(ui.requests.some(request => request.url.endsWith('/cancel/')), false, 'Nothing left to stop');
});

test('an offer is announced with what ended, not only what happened to it', async t => {
  const ui = await page(t, { activeJob: { ok: true, job: finishedFrame('minimum_change_repair', { result_run_id: 77 }) } });
  await until(() => !ui.$('examJobOpen').hidden);
  assert.match(ui.$('examJobLive').textContent, language === 'ar' ? /^اكتمل الإصلاح\./ : /^Fix finished\./);
});

test("a colleague's job that never started does not tell the watcher to try again", async t => {
  const frames = [
    theirs('build', 'queued', {}, null, { started_at: null }),
    theirs('build', 'failed', {}, null, { started_at: null, error_code: 'never_started', finished_at: '2026-09-23T10:30:00+00:00' }),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  assert.doesNotMatch(ui.$('examJobDetail').textContent, /Try again later|أعد المحاولة لاحقاً/);
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /يمكنك الآن استخدام البناء/ : /You can use Build, Optimize, Fix and Save again/);
});

test('a job someone has asked to stop offers no second Stop on another page', async t => {
  const stopping = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts', { can_cancel: true, stopping: true });
  const ui = await page(t, {
    activeJob: { ok: true, job: { ...stopping, stopping: false } },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: stopping }) : undefined,
  });
  await until(() => TEXT.stopping.test(ui.$('examJobDetail').textContent));
  assert.equal(ui.$('examJobCancel').getAttribute('aria-disabled'), 'true');
});

test("a refusal behind a colleague's job still waiting its turn says so", async t => {
  const waiting = theirs('build', 'queued', {}, null, { started_at: null });
  let refused = false;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        refused = true;
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409);
      }
      if (isActive(url)) return jobReply({ ok: true, job: refused ? waiting : null });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: waiting });
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => /did not start|لم يبدأ/.test(ui.$('examJobNote').textContent));
  assert.match(ui.$('examJobNote').textContent, language === 'ar' ? /تنتظر عملية أخرى على الجدول دورها/ : /is waiting its turn, requested by/);
});

test('an ending on screen is not replaced by an older one of mine', async t => {
  let submits = 0;
  let actives = 0;
  const olderOffer = { ...finishedFrame('optimize_loaded', { result_run_id: 88 }), id: JOB_B };
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        submits += 1;
        return jobReply({ ok: true, job: jobFrame('optimize_loaded', 'queued') }, 202);
      }
      if (isActive(url)) {
        actives += 1;
        return jobReply({ ok: true, job: actives === 1 ? null : olderOffer });
      }
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: failedFrame('optimize_loaded') });
      if (url === seenUrl(JOB_ID)) return jobReply({ ok: true, marked: true });
      if (url === '/ops/exam-timetable/draft-impact/') {
        return jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'exam_job', seconds: 2 } }, 503);
      }
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => ui.$('examJobPanel').classList.contains('is-failed') && !ui.$('optimizeLoadedBtn').disabled);
  dropExam(ui, 'Tue');
  await until(() => actives >= 2, 'The job holding the solver was looked for');
  await pause(30);
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimizeFailed, 'Still the ending the registrar was reading');
  assert.equal(ui.$('examJobOpen').hidden, true);
});

test('a result found on opening does not take the panel from an action started meanwhile', async t => {
  let release;
  const storedAnswer = new Promise(resolve => { release = resolve; });
  const refusedAway = finishedFrame('save_loaded_changes', { has_run: false, result_run_id: null, refused: true });
  const ui = await loadedEditor(t, {
    activeJob: { ok: true, job: refusedAway },
    onRequest: async url => {
      if (url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`) {
        await storedAnswer;
        return jobReply({ ok: false, error_code: 'inputs_changed', error: 'The course list changed.' }, 409);
      }
      if (url === '/ops/exam-timetable/build/') return jobReply({ ok: true, job: { ...jobFrame('optimize_loaded', 'queued'), id: JOB_B } }, 202);
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: { ...OPTIMISED[0], id: JOB_B } });
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizing);
  release();
  await pause(30);
  assert.equal(ui.$('examJobTitle').textContent, TEXT.optimizing, 'The action running now keeps the panel');
  assert.equal(ui.$('examJobCancel').hidden, false);
});

test("closing the panel does not make the board's check line speak", async t => {
  const ui = await loadedEditor(t, {
    activeJob: { ok: true, job: finishedFrame('build') },
    onRequest: async url => (url === seenUrl(JOB_ID) ? jobReply({ ok: true, marked: true }) : undefined),
  });
  await until(() => !ui.$('examJobClose').hidden);
  const said = [];
  new ui.window.MutationObserver(() => said.push(ui.$('examCheckStatus').textContent))
    .observe(ui.$('examCheckStatus'), { childList: true, characterData: true, subtree: true });
  ui.$('examJobClose').click();
  await pause(20);
  assert.deepEqual(said, [], 'Nothing it says changed');
});

// ── the round-three verification's findings ──

test('my infeasible Build found on opening says which courses do not fit', async t => {
  const ui = await page(t, {
    activeJob: { ok: true, job: finishedFrame('build', { has_run: false, result_run_id: null, refused: true }) },
    onRequest: async url => url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`
      ? jobReply({ ok: false, feasibility_error: true, status: 'feasibility_error', violations: [{ program: 'AI', programme_term: 3, bucket_size: 7, num_days: 5, courses: ['AI301', 'AI302'] }] }, 400)
      : undefined,
  });
  await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
  const detail = ui.$('examJobDetail').textContent;
  assert.match(detail, /AI301/);
  assert.match(detail, language === 'ar' ? /الجدول غير ممكن/ : /Infeasible schedule/);
  assert.doesNotMatch(detail, /Request failed|فشل الطلب/);
});

for (const code of ['inputs_changed', 'check_required']) {
  test(`a Save refused for ${code}, found on opening, gives its reason, where the draft is, and what to do`, async t => {
    const ui = await page(t, {
      activeJob: { ok: true, job: finishedFrame('save_loaded_changes', { has_run: false, result_run_id: null, refused: true }) },
      onRequest: async url => url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`
        ? jobReply({ ok: false, error_code: code, error: 'Source inputs changed since the check.' }, 409)
        : undefined,
    });
    await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
    // The reason, where the draft is, then a step for either case.
    endsWith(ui.$('examJobDetail').textContent, W.reason[code], W.draft, W.recheck.away);
    assert.doesNotMatch(ui.$('examJobDetail').textContent, /Source inputs changed/);
  });
}
function activeAnswers(first, later) {
  let asked = 0;
  return () => { asked += 1; return jobReply({ ok: true, ...(asked === 1 ? first : later) }); };
}

test('an outcome of mine waiting behind a colleague’s job is shown after theirs, as mine', async t => {
  const colleague = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  // The same kind as the colleague's: only the wording can tell them apart.
  const mine = { ...failedFrame('build'), id: JOB_B };
  const active = activeAnswers({ job: colleague[0], ending: mine }, { job: mine });
  let index = 0;
  let seenMine = 0;
  const ui = await page(t, {
    onRequest: async url => {
      if (isActive(url)) return active();
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague[index] });
      if (url === seenUrl(JOB_B)) { seenMine += 1; return jobReply({ ok: true, marked: true }); }
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building, "Following the colleague's job");
  index = 1;
  await until(() => !ui.$('examJobOpen').hidden, "Their saved timetable is offered first");
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /حُفظت النتيجة جدولاً جديداً بطلب من/ : /Saved as a new timetable by/);
  assert.equal(seenMine, 0, 'Mine not shown yet');
  ui.$('examJobClose').click();
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'), 'Then mine');
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /^بينما كانت الصفحة مغلقة، انتهت العملية التي بدأتها/ : /^While this page was closed, the action you started/);
  assert.match(ui.$('examJobLive').textContent, language === 'ar' ? /بينما كانت الصفحة مغلقة/ : /While this page was closed/);
  await until(() => seenMine === 1);
  // Owed once, asked for once.
  const asked = activeAsks(ui);
  ui.$('examJobClose').click();
  await pause(40);
  assert.equal(activeAsks(ui), asked, 'Not asked for again');
});
test('a refusal notice goes when another timetable is opened', async t => {
  const ui = await loadedEditor(t, {
    history: [{ id: 17, label: 'One' }, { id: 18, label: 'Two' }],
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409);
      }
      if (url === '/ops/exam-timetable/18/') return response({ ...savedRun(), run_id: 18 });
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => !ui.$('examEditorNotice').hidden);
  await ui.window.loadRun(18);
  await settle();
  assert.equal(ui.$('examEditorNotice').hidden, true);
  assert.equal(ui.$('examEditorNotice').textContent, '');
});

test('a refusal notice goes when the panel starts following a job', async t => {
  let started = false;
  const colleague = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409);
      }
      if (isActive(url)) return jobReply({ ok: true, job: started ? colleague : null });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague });
      if (url === '/ops/exam-timetable/draft-impact/') {
        started = true;
        return jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'exam_job', seconds: 2 } }, 503);
      }
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => !ui.$('examEditorNotice').hidden, 'Refused, and the job had ended');
  dropExam(ui, 'Tue');
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  assert.equal(ui.$('examEditorNotice').hidden, true, '"Try again now" is no longer true');
});

test('a refusal is said once when the setup section is open', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409)
      : undefined,
  });
  ui.$('examSetupDetails').open = true;
  const liveBefore = ui.$('examJobLive').textContent;
  ui.$('optimizeLoadedBtn').click();
  await until(() => /Try again now|أعد المحاولة الآن/.test(ui.$('etStatus').textContent));
  assert.equal(ui.$('examJobLive').textContent, liveBefore, 'Not also said through the job live region');
});

test('a check left waiting is run once an action that changed nothing ends', async t => {
  let checks = 0;
  let planner = true;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        planner = false;
        return jobReply({ ok: true, job: jobFrame('optimize_loaded', 'queued') }, 202);
      }
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: failedFrame('optimize_loaded') });
      if (url === seenUrl(JOB_ID)) return jobReply({ ok: true, marked: true });
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      checks += 1;
      return planner ? jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'planner', seconds: 60 } }, 503) : undefined;
    },
  });
  dropExam(ui, 'Tue');
  await until(() => /Waiting for the server|في انتظار الخادم/.test(ui.$('examCheckStatus').textContent));
  const before = checks;
  ui.$('optimizeLoadedBtn').click();
  await until(() => ui.$('examJobPanel').classList.contains('is-failed') && !ui.$('optimizeLoadedBtn').disabled);
  await until(() => checks > before, 'The owed check runs');
  await until(() => !/Waiting for the server|في انتظار الخادم/.test(ui.$('examCheckStatus').textContent));
});

test('Stop pressed after the session ended does not claim to be stopping', async t => {
  const { ui, server } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  // The session ends just before Stop is pressed: every request is refused.
  let signedIn = false;
  server.override = async url => {
    if (signedIn || ![pollUrl(JOB_ID), `/ops/exam-timetable/jobs/${JOB_ID}/cancel/`].includes(url)) return undefined;
    if (url.endsWith('/cancel/')) server.cancels += 1;
    else server.polls += 1;
    return { ok: false, status: 401, json: async () => ({}), headers: { get: () => null } };
  };
  ui.$('examJobCancel').focus();
  ui.$('examJobCancel').click();
  await until(() => ui.$('examJobNote').querySelector('a[target="_blank"]'));
  assert.doesNotMatch(ui.$('examJobDetail').textContent, TEXT.stopping);
  assert.equal(ui.$('examJobCancel').hidden, true);
  assert.equal(ui.window.document.activeElement, ui.$('examJobTitle'), 'Stop had focus and is gone');
  signedIn = true;
  await until(() => !ui.$('examJobCancel').hidden, 'Signed in again, Stop is back');
  assert.equal(ui.$('examJobCancel').getAttribute('aria-disabled'), 'false');
  ui.$('examJobCancel').click();
  await until(() => server.cancels === 2);
});

test('a stop that reached the job anyway does not also say it could not stop it', async t => {
  const stopping = { ...OPTIMISED[0], stopping: true };
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], stopping]);
  await until(() => stageStates(ui).includes('running'));
  server.override = async url => {
    if (url !== `/ops/exam-timetable/jobs/${JOB_ID}/cancel/`) return undefined;
    server.cancels += 1;
    throw new TypeError('Failed to fetch');
  };
  ui.$('examJobCancel').click();
  await until(() => server.cancels === 1 && TEXT.couldNotStop.test(ui.$('examJobNote').textContent));
  server.advance();
  await until(() => TEXT.stopping.test(ui.$('examJobDetail').textContent));
  assert.doesNotMatch(ui.$('examJobNote').textContent, TEXT.couldNotStop);
  assert.equal(ui.$('examJobCancel').getAttribute('aria-disabled'), 'true');
});

test('a job known to be over says so while its result loads, and announces no stage', async t => {
  let release;
  const resultArrives = new Promise(resolve => { release = resolve; });
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, {
    poll: { announce: 40 },
    beforeClick: (ui, server) => {
      server.override = async url => {
        if (url !== `/ops/exam-timetable/jobs/${JOB_ID}/result/`) return undefined;
        server.results += 1;
        await resultArrives;
        return response({ ...evaluatedRun(server.submitted), run_id: 93 });
      };
    },
  });
  await until(() => stageStates(ui).includes('running'));
  const said = [];
  new ui.window.MutationObserver(() => said.push(ui.$('examJobLive').textContent))
    .observe(ui.$('examJobLive'), { childList: true, characterData: true, subtree: true });
  server.index = 2;
  await until(() => server.results === 1);
  const sinceOver = said.length;
  await pause(120);
  assert.equal(ui.$('examJobTitle').textContent, language === 'ar' ? 'انتهى تحسين الجدول، جارٍ تحميل النتيجة' : 'Optimization finished, loading the result');
  assert.doesNotMatch(ui.$('examJobNote').textContent, TEXT.leave);
  assert.ok(said.slice(sinceOver).every(text => !/Step \d|الخطوة/.test(text)), `Said after it ended: ${JSON.stringify(said.slice(sinceOver))}`);
  release();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized);
});

test('a pager that collapses leaves the keyboard on Saved timetables, not on a hidden button', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  let index = 0;
  let pages = 3;
  const runs = Array.from({ length: 10 }, (_, i) => ({ id: 30 + i, label: `Run ${i}` }));
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url.startsWith('/ops/exam-timetable/list/')) return response({ ok: true, runs, total: pages * 10, total_pages: pages, page: 1 });
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  await until(() => ui.$('historyPages').querySelector('[data-history-nav="next"]'));
  ui.$('historyPages').querySelector('[data-history-nav="next"]').focus();
  pages = 1;
  const before = historyLoads(ui);
  index = 1;
  await until(() => historyLoads(ui) > before && !ui.$('examJobOpen').hidden);
  await settle();
  assert.equal(ui.window.document.activeElement, ui.$('examHistorySummary'));
});

test('signed in again with focus on the sign-in link, focus stays in the panel', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { poll: { slow: 20 } });
  await until(() => stageStates(ui).includes('running'));
  let signedIn = false;
  server.override = async url => {
    if (url !== pollUrl(JOB_ID) || signedIn) return undefined;
    server.polls += 1;
    return { ok: false, status: 401, json: async () => ({}), headers: { get: () => null } };
  };
  await until(() => ui.$('examJobNote').querySelector('a'));
  ui.$('examJobNote').querySelector('a').focus();
  signedIn = true;
  await until(() => !ui.$('examJobNote').querySelector('a'));
  assert.equal(ui.window.document.activeElement, ui.$('examJobTitle'));
});

test('an ending while another dialog hides the page is said once the page is back', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  const before = ui.$('examJobLive').textContent;
  ui.$('main-content').setAttribute('aria-hidden', 'true');
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  await pause(20);
  assert.equal(ui.$('examJobLive').textContent, before, 'Nobody can hear it now');
  ui.$('main-content').removeAttribute('aria-hidden');
  await until(() => /^Build failed|^تعذّر بناء الجدول/.test(ui.$('examJobLive').textContent), 'Said when the page is back');
});

test('refused behind my own job that is still waiting its turn says so', async t => {
  const mineWaiting = jobFrame('build', 'queued', {}, null, { started_at: null, waiting_for: { kind: 'planner', seconds: 90 } });
  let started = false;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        started = true;
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: true, owner: 'me', status: 'queued' } }, 409);
      }
      if (isActive(url)) return jobReply({ ok: true, job: started ? mineWaiting : null });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: mineWaiting });
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => /did not start|لم يبدأ/.test(ui.$('examJobNote').textContent));
  assert.match(ui.$('examJobNote').textContent, language === 'ar' ? /تنتظر دورها/ : /waiting its turn/);
  assert.doesNotMatch(ui.$('examJobNote').textContent, /still running|قيد التنفيذ/);
});

test('a refusal the page could not follow names a waiting job as waiting', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda', status: 'queued' } }, 409);
      }
      if (isActive(url)) return jobReply({ ok: false }, 500);
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => !ui.$('examEditorNotice').hidden);
  assert.match(ui.$('examEditorNotice').textContent, language === 'ar' ? /تنتظر عملية أخرى على الجدول دورها/ : /is waiting its turn, requested by/);
});

test('a refusal notice goes when the courses are loaded afresh', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409)
      : undefined,
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => !ui.$('examEditorNotice').hidden);
  ui.$('loadCoursesBtn').click();
  await until(() => ui.$('examEditorNotice').hidden);
});

test('an ending while the department files dialog is open is said once it closes', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  const before = ui.$('examJobLive').textContent;
  ui.$('examDepartmentDialog').setAttribute('open', '');
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  await pause(20);
  assert.equal(ui.$('examJobLive').textContent, before, 'The rest of the page is inert while it is open');
  ui.$('examDepartmentDialog').removeAttribute('open');
  await until(() => /^Build failed|^تعذّر بناء الجدول/.test(ui.$('examJobLive').textContent));
});

test('an outcome of mine waiting behind a colleague’s job is shown even if that job is lost', async t => {
  const running = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  const mine = { ...failedFrame('optimize_loaded'), id: JOB_B };
  const active = activeAnswers({ job: running, ending: mine }, { job: mine });
  let gone = false;
  const ui = await page(t, {
    onRequest: async url => {
      if (isActive(url)) return active();
      if (url === pollUrl(JOB_ID)) return gone ? jobReply({ ok: false, error_code: 'job_not_found' }, 404) : jobReply({ ok: true, job: running });
      if (url === seenUrl(JOB_B)) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  gone = true;
  await until(() => ui.$('examJobPanel').classList.contains('is-lost'), 'What happened to theirs is said first');
  ui.$('examJobClose').click();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizeFailed);
});
test('an older outcome of mine is not shown over a newer action of mine', async t => {
  const colleague = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  const mine = { ...failedFrame('build'), id: '6f1c2a4e-0000-4000-8000-000000000003' };
  // Once my new action has ended and its result was read, it is my latest.
  const active = activeAnswers({ job: colleague[0], ending: mine }, { job: null });
  let index = 0;
  const ui = await loadedEditor(t, {
    poll: { reveal: 500, stall: 1000 },
    onRequest: async url => {
      if (isActive(url)) return active();
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague[index] });
      if (url === '/ops/exam-timetable/build/') return jobReply({ ok: true, job: { ...jobFrame('optimize_loaded', 'queued'), id: JOB_B } }, 202);
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: { ...OPTIMISED[2], id: JOB_B } });
      if (url === `/ops/exam-timetable/jobs/${JOB_B}/result/`) return response({ ...savedRun(), run_id: 94 });
      if (url.includes('/seen/')) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed') && !ui.$('optimizeLoadedBtn').disabled);
  // The registrar goes straight on to an action of their own, which succeeds.
  ui.$('optimizeLoadedBtn').click();
  await until(() => /alert-success/.test(ui.$('etStatus').className));
  await pause(40);
  assert.doesNotMatch(ui.$('examJobDetail').textContent, /^While this page was closed|^بينما كانت الصفحة مغلقة/,
    'The older failure is not the answer to the newer action');
});
test('a Stop answered with a page that is not the server’s answer is not taken as accepted', async t => {
  const { ui, server } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  server.override = async url => url === `/ops/exam-timetable/jobs/${JOB_ID}/cancel/`
    ? { ok: true, status: 200, json: async () => { throw new SyntaxError('not json'); }, headers: { get: name => (name === 'content-type' ? 'text/html' : null) } }
    : undefined;
  ui.$('examJobCancel').click();
  await until(() => TEXT.couldNotStop.test(ui.$('examJobNote').textContent));
  assert.equal(ui.$('examJobCancel').getAttribute('aria-disabled'), 'false', 'Stop can be tried again');
});

test('a failed Stop of a job another page has already stopped stays stopping', async t => {
  const stopping = { ...OPTIMISED[0], stopping: true };
  const { ui, server } = await optimiseAsJob(t, [OPTIMISED[0], stopping]);
  await until(() => stageStates(ui).includes('running'));
  let releaseCancel;
  const cancelAnswered = new Promise(resolve => { releaseCancel = resolve; });
  let stoppingSeen = false;
  server.override = async url => {
    if (url === `/ops/exam-timetable/jobs/${JOB_ID}/cancel/`) {
      server.cancels += 1;
      await cancelAnswered;
      throw new TypeError('Failed to fetch');
    }
    // After the stopping frame, no poll comes to put things right.
    if (url === pollUrl(JOB_ID) && stoppingSeen) return new Promise(() => {});
    if (url === pollUrl(JOB_ID) && server.index === 1) stoppingSeen = true;
    return undefined;
  };
  ui.$('examJobCancel').click();
  await until(() => server.cancels === 1);
  server.advance();
  await until(() => stoppingSeen);
  await pause(10);
  releaseCancel();
  await pause(20);
  assert.match(ui.$('examJobDetail').textContent, TEXT.stopping);
  assert.doesNotMatch(ui.$('examJobNote').textContent, TEXT.couldNotStop);
  assert.equal(ui.$('examJobCancel').getAttribute('aria-disabled'), 'true');
});
test('refused behind my own waiting job the page could not follow says it is waiting', async t => {
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: true, owner: 'me', status: 'queued' } }, 409);
      }
      if (isActive(url)) return jobReply({ ok: false }, 500);
      return undefined;
    },
  });
  ui.$('optimizeLoadedBtn').click();
  await until(() => !ui.$('examEditorNotice').hidden);
  assert.match(ui.$('examEditorNotice').textContent, language === 'ar' ? /تنتظر دورها/ : /waiting its turn/);
});

test('my own job is announced as waiting only once the server says what for', async t => {
  const { ui, server } = await optimiseAsJob(t, [
    jobFrame('optimize_loaded', 'queued'),
    jobFrame('optimize_loaded', 'queued', {}, null, { waiting_for: { kind: 'planner', seconds: 240 } }),
  ]);
  await until(() => !ui.$('examJobPanel').hidden);
  await pause(20);
  assert.doesNotMatch(ui.$('examJobLive').textContent, /busy|مشغول|planner|تخطيط/);
  server.advance();
  await until(() => /planner|تخطيط/.test(ui.$('examJobLive').textContent), 'Said once the wait is real');
});

test('a Stop answered by the sign-in page says signed out at once', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { poll: { steady: 5000, slow: 5000 } });
  await until(() => stageStates(ui).includes('running'));
  server.override = async url => {
    if (url !== `/ops/exam-timetable/jobs/${JOB_ID}/cancel/`) return undefined;
    server.cancels += 1;
    return { ok: false, status: 401, json: async () => ({}), headers: { get: () => null } };
  };
  const polls = server.polls;
  ui.$('examJobCancel').click();
  await until(() => ui.$('examJobNote').querySelector('a[target="_blank"]'), 'Signed out, before any poll could say so');
  assert.equal(server.polls, polls);
  assert.doesNotMatch(ui.$('examJobNote').textContent, TEXT.couldNotStop);
});

// ── the round-five verification's findings ──

test('a refusal said in the setup section goes when the panel follows a new job', async t => {
  let started = false;
  const colleague = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409);
      }
      if (isActive(url)) return jobReply({ ok: true, job: started ? colleague : null });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague });
      if (url === '/ops/exam-timetable/draft-impact/') {
        started = true;
        return jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'exam_job', seconds: 2 } }, 503);
      }
      return undefined;
    },
  });
  ui.$('examSetupDetails').open = true;
  ui.$('optimizeLoadedBtn').click();
  await until(() => /Try again now|أعد المحاولة الآن/.test(ui.$('etStatus').textContent));
  dropExam(ui, 'Tue');
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  assert.doesNotMatch(ui.$('etStatus').textContent, /Try again now|أعد المحاولة الآن/, '"Try again now" is no longer true');
});

test('clearing a refusal from the status line leaves what was written there since', async t => {
  let started = false;
  const colleague = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (url === '/ops/exam-timetable/build/') {
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409);
      }
      if (isActive(url)) return jobReply({ ok: true, job: started ? colleague : null });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague });
      if (url === '/ops/exam-timetable/draft-impact/') {
        started = true;
        return jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'exam_job', seconds: 2 } }, 503);
      }
      return undefined;
    },
  });
  ui.$('examSetupDetails').open = true;
  ui.$('optimizeLoadedBtn').click();
  await until(() => /Try again now|أعد المحاولة الآن/.test(ui.$('etStatus').textContent));
  // Something else has the status line now.
  ui.$('etStatus').textContent = 'Courses reloaded.';
  dropExam(ui, 'Tue');
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  assert.equal(ui.$('etStatus').textContent, 'Courses reloaded.', 'Not the notice\u2019s to clear');
});

test('a Save turned away keeps saying to save again once the builder is free', async t => {
  let checks = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      checks += 1;
      return jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'planner', seconds: 60 } }, 503);
    },
  });
  dropExam(ui, 'Wed');
  await until(() => checks >= 1);
  const before = checks;
  ui.$('saveLoadedBtn').click();
  await until(() => checks > before && /Save again when it finishes|أعد الحفظ عند انتهائها/.test(ui.$('draftImpactBanner').textContent));
  await pause(40);
  assert.match(ui.$('draftImpactBanner').textContent, /Save again when it finishes|أعد الحفظ عند انتهائها/, 'Not overwritten by a background check');
});

test('a reconnecting notice held while the page was hidden is not said once contact is back', async t => {
  const { ui, server } = await optimiseAsJob(t);
  await until(() => stageStates(ui).includes('running'));
  ui.$('main-content').setAttribute('aria-hidden', 'true');
  server.failures = Infinity;
  await until(() => /reconnect|إعادة الاتصال/.test(ui.$('examJobDetail').textContent));
  server.failures = 0;
  await until(() => ui.$('examJobDetail').textContent === TEXT.exams310, 'Contact is back');
  const said = [];
  new ui.window.MutationObserver(() => said.push(ui.$('examJobLive').textContent))
    .observe(ui.$('examJobLive'), { childList: true, characterData: true, subtree: true });
  ui.$('main-content').removeAttribute('aria-hidden');
  await pause(30);
  assert.ok(said.every(text => !/reconnect|إعادة الاتصال/.test(text)), `Said: ${JSON.stringify(said)}`);
});

test('an ending while the rest of the page is inert is said once it is not', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  const before = ui.$('examJobLive').textContent;
  // As the fullscreen conflict matrix does to everything behind it.
  ui.$('main-content').setAttribute('inert', '');
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  await pause(20);
  assert.equal(ui.$('examJobLive').textContent, before, 'Nobody can hear it now');
  ui.$('main-content').removeAttribute('inert');
  await until(() => /^Build failed|^تعذّر بناء الجدول/.test(ui.$('examJobLive').textContent));
});

test('a job ending while a Saved-timetables dialog is open keeps the keyboard on its button', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  let index = 0;
  const ui = await page(t, {
    history: [{ id: 17, label: 'One' }, { id: 18, label: 'Two' }],
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  await until(() => ui.$('historyList').querySelectorAll('.et-del-btn').length === 2);
  const opener = ui.$('historyList').querySelectorAll('.et-del-btn')[1];
  opener.focus();
  let answer;
  const main = ui.$('main-content');
  const dialogButton = ui.window.document.createElement('button');
  ui.window.dlg.confirm = options => {
    // As dialog.js: the page is hidden, focus is in the dialog, and on close
    // it is given back to the button that opened it.
    const from = ui.window.document.activeElement;
    main.setAttribute('aria-hidden', 'true');
    ui.window.document.body.append(dialogButton);
    dialogButton.focus();
    return new Promise(resolve => {
      answer = value => { main.removeAttribute('aria-hidden'); dialogButton.remove(); from.focus(); resolve(value); };
    });
  };
  opener.click();
  const before = historyLoads(ui);
  index = 1;
  await until(() => !ui.$('examJobOpen').hidden);
  await pause(20);
  assert.equal(historyLoads(ui), before, 'The list is not re-rendered under an open dialog');
  answer(false);
  await until(() => historyLoads(ui) > before, 'Reloaded once the page is back');
  await settle();
  const focused = ui.window.document.activeElement;
  assert.ok(focused.classList.contains('et-del-btn'), `Focus is on ${focused.tagName}.${focused.className}`);
  assert.equal(focused.dataset.id, '18');
});

test('a Build of mine turned down for courses without enrollments says so in the page language', async t => {
  const ui = await page(t, {
    activeJob: { ok: true, job: finishedFrame('build', { has_run: false, result_run_id: null, refused: true }) },
    onRequest: async url => url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`
      ? jobReply({ ok: false, code: 'courses_unavailable', error: 'Some selected courses have no enrollments.', unavailable_courses: ['CS499'] }, 400)
      : undefined,
  });
  await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
  assert.match(ui.$('examJobDetail').textContent, /CS499/);
  assert.doesNotMatch(ui.$('examJobDetail').textContent, /Some selected courses have no enrollments/);
  // A Build takes no draft with it: there is none to speak of.
  assert.doesNotMatch(ui.$('examJobDetail').textContent, /unsaved changes|تغييرات غير محفوظة/);
});

test('a reason that already ends its sentence gets no second full stop', async t => {
  const ui = await page(t, {
    activeJob: { ok: true, job: finishedFrame('build', { has_run: false, result_run_id: null, refused: true }) },
    onRequest: async url => url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`
      ? jobReply({ ok: false, error: 'The exam period has no usable days.' }, 400)
      : undefined,
  });
  await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
  const detail = ui.$('examJobDetail').textContent;
  assert.match(detail, /no usable days\.[⁦-⁩]*$/);
  assert.doesNotMatch(detail, /\.[⁦-⁩]*\./, `Said: ${detail}`);
});

test('stopping a colleague’s job waits for the dialog to hand focus back first', async t => {
  const running = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts', { can_cancel: true });
  const ui = await page(t, {
    activeJob: { ok: true, job: running },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: running }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  ui.$('examJobCancel').click();
  await until(() => ui.dialogs.length === 1);
  assert.equal(ui.dialogs[0].waitForClose, true);
});

test('an outcome of mine behind a colleague’s job is shown once their timetable is opened', async t => {
  const colleague = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda', result_run_id: 77 }),
  ];
  const mine = { ...failedFrame('optimize_loaded'), id: JOB_B };
  const active = activeAnswers({ job: colleague[0], ending: mine }, { job: mine });
  let index = 0;
  const ui = await page(t, {
    onRequest: async url => {
      if (isActive(url)) return active();
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague[index] });
      if (url === '/ops/exam-timetable/77/') return response({ ...savedRun(), run_id: 77 });
      if (url === seenUrl(JOB_B)) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  index = 1;
  await until(() => !ui.$('examJobOpen').hidden);
  ui.$('examJobOpen').click();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizeFailed, 'Mine, once theirs is open');
});
test('a Save turned away while a colleague’s job ran is not checked in its place when that job ends', async t => {
  const colleague = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  let index = 0;
  let started = false;
  let checks = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: false,
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, job: started ? colleague[0] : null });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague[index] });
      if (url !== '/ops/exam-timetable/draft-impact/') return undefined;
      checks += 1;
      started = true;
      return jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'exam_job', seconds: 5 } }, 503);
    },
  });
  dropExam(ui, 'Wed');
  ui.$('examLiveUpdate').checked = true;
  ui.$('saveLoadedBtn').click();
  await until(() => ui.$('examJobTitle').textContent === TEXT.building && /Save again when it finishes|أعد الحفظ عند انتهائها/.test(ui.$('draftImpactBanner').textContent));
  const before = checks;
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  await pause(40);
  assert.equal(checks, before, 'Nothing was checked in place of the Save the registrar has to repeat');
  assert.match(ui.$('draftImpactBanner').textContent, /Save again when it finishes|أعد الحفظ عند انتهائها/);
});

test('a signed-out notice held while the page was hidden is not said once signed in again', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, { poll: { slow: 10 } });
  await until(() => stageStates(ui).includes('running'));
  let signedIn = false;
  server.override = async url => {
    if (url !== pollUrl(JOB_ID) || signedIn) return undefined;
    server.polls += 1;
    return { ok: false, status: 401, json: async () => ({}), headers: { get: () => null } };
  };
  ui.$('main-content').setAttribute('aria-hidden', 'true');
  await until(() => /session expired|انتهت جلسة/.test(ui.$('examJobDetail').textContent));
  signedIn = true;
  await until(() => ui.$('examJobDetail').textContent === TEXT.exams310, 'Signed in again');
  const said = [];
  new ui.window.MutationObserver(() => said.push(ui.$('examJobLive').textContent))
    .observe(ui.$('examJobLive'), { childList: true, characterData: true, subtree: true });
  ui.$('main-content').removeAttribute('aria-hidden');
  await pause(30);
  assert.ok(said.every(text => !/session expired|انتهت جلسة/.test(text)), `Said: ${JSON.stringify(said)}`);
});

test('a stage held while the page was hidden is not said once the job is over', async t => {
  let release;
  const resultArrives = new Promise(resolve => { release = resolve; });
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, {
    poll: { announce: 0 },
    beforeClick: (ui, server) => {
      ui.$('main-content').setAttribute('aria-hidden', 'true');
      server.override = async url => {
        if (url !== `/ops/exam-timetable/jobs/${JOB_ID}/result/`) return undefined;
        server.results += 1;
        await resultArrives;
        return response({ ...evaluatedRun(server.submitted), run_id: 93 });
      };
    },
  });
  await until(() => stageStates(ui).includes('running'));
  await pause(20);
  server.index = 2;
  await until(() => server.results === 1, 'Over, its result on the way');
  const said = [];
  new ui.window.MutationObserver(() => said.push(ui.$('examJobLive').textContent))
    .observe(ui.$('examJobLive'), { childList: true, characterData: true, subtree: true });
  ui.$('main-content').removeAttribute('aria-hidden');
  await pause(30);
  assert.ok(said.every(text => !/Step \d|الخطوة/.test(text)), `Said: ${JSON.stringify(said)}`);
  release();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized);
});

test('an ending while the conflict matrix is fullscreen is said once it closes', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  let index = 0;
  const ui = await loadedEditor(t, {
    run: matrixRun(),
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  ui.$('toggleMatrix').click();
  ui.$('matrixFullscreen').click();
  assert.ok(ui.$('matrixPanel').classList.contains('matrix-fullscreen'));
  const before = ui.$('examJobLive').textContent;
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  await pause(20);
  assert.equal(ui.$('examJobLive').textContent, before, 'Everything behind the matrix is inert');
  ui.$('matrixFullscreen').click();
  await until(() => /^Build failed|^تعذّر بناء الجدول/.test(ui.$('examJobLive').textContent), 'Said once the matrix closes');
});

// ── the round-six verification's findings ──

test('a job ending while the real Delete dialog is open keeps the keyboard on its button', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  let index = 0;
  const ui = await page(t, {
    realDialogs: true,
    history: [{ id: 17, label: 'One' }, { id: 18, label: 'Two' }],
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  await until(() => ui.$('historyList').querySelectorAll('.et-del-btn').length === 2);
  const opener = ui.$('historyList').querySelectorAll('.et-del-btn')[1];
  opener.focus();
  opener.click();
  await until(() => ui.window.document.querySelector('.dlg-backdrop'));
  // As in a browser: the dialog has the keyboard, not the button that opened it.
  await until(() => ui.window.document.activeElement.closest('.dlg-backdrop'), 'The dialog takes focus');
  const before = historyLoads(ui);
  index = 1;
  await until(() => !ui.$('examJobOpen').hidden);
  // The list answers at once: sooner than the dialog gives focus back.
  ui.window.document.querySelector('.dlg-backdrop .btn-cancel').click();
  await until(() => historyLoads(ui) > before, 'Reloaded once the dialog is gone');
  await pause(300);
  const focused = ui.window.document.activeElement;
  assert.ok(focused.classList.contains('et-del-btn'), `Focus is on ${focused.tagName}.${focused.className}`);
  assert.equal(focused.closest('.et-history-item').dataset.id, '18');
});

test('a Save of mine turned down for courses without enrollments, found on return, calls no draft unchanged', async t => {
  const ui = await page(t, {
    activeJob: { ok: true, job: finishedFrame('save_loaded_changes', { has_run: false, result_run_id: null, refused: true }) },
    onRequest: async url => url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`
      ? jobReply({ ok: false, code: 'courses_unavailable', error: 'x', unavailable_courses: ['CS499'] }, 400)
      : undefined,
  });
  await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
  assert.match(ui.$('examJobDetail').textContent, /CS499/);
  assert.doesNotMatch(ui.$('examJobDetail').textContent, /draft is unchanged|لم تتغير مسودتك/i);
  // Listed as the live page lists them, with its step; then where the draft
  // is - and no "check again": checking would not bring the courses back.
  endsWith(ui.$('examJobDetail').textContent, language === 'ar'
    ? 'المستوردة. المقررات غير المتاحة: \u2066CS499\u2069. حمّل المقررات لمراجعة القائمة الحالية.'
    : 'student timetables. Unavailable courses: CS499. Load Courses to review the current list.', W.draft);
});

test('my own saved timetable already open on the board is not offered again', async t => {
  const colleague = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  // My job saved run 17 while I was away - the run this page has open.
  const mine = { ...finishedFrame('build', { result_run_id: 17 }), id: JOB_B };
  const active = activeAnswers({ job: colleague[0], ending: mine }, { job: mine });
  let index = 0;
  let seenMine = 0;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (isActive(url)) return active();
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague[index] });
      if (url === seenUrl(JOB_B)) { seenMine += 1; return jobReply({ ok: true, marked: true }); }
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  index = 1;
  await until(() => !ui.$('examJobClose').hidden && ui.$('examJobPanel').classList.contains('is-failed'));
  ui.$('examJobClose').click();
  await until(() => seenMine === 1, 'Marked seen: it is on the board');
  assert.equal(ui.$('examJobPanel').hidden, true, 'Not offered to be opened again');
});

test('a job ending while the real discard-changes dialog is open keeps the keyboard on its row', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  let index = 0;
  const run = { ...savedRun(), pinned: [] };
  const ui = await loadedEditor(t, {
    run,
    realDialogs: true,
    history: [{ id: run.run_id, label: 'One' }, { id: 18, label: 'Two' }],
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  // Unsaved changes: opening another timetable asks first.
  dropExam(ui, 'Tue');
  await until(() => ui.$('historyList').querySelectorAll('.et-run-info').length === 2);
  const opener = ui.$('historyList').querySelectorAll('.et-run-info')[1];
  opener.focus();
  opener.click();
  await until(() => ui.window.document.querySelector('.dlg-backdrop'), 'The discard-changes dialog opens');
  await until(() => ui.window.document.activeElement.closest('.dlg-backdrop'), 'The dialog takes focus');
  const before = historyLoads(ui);
  index = 1;
  await until(() => !ui.$('examJobOpen').hidden);
  // The list answers at once: sooner than the dialog gives focus back.
  ui.window.document.querySelector('.dlg-backdrop .btn-cancel').click();
  await until(() => historyLoads(ui) > before, 'Reloaded once the dialog is gone');
  await pause(300);
  const focused = ui.window.document.activeElement;
  assert.ok(focused.classList.contains('et-run-info'), `Focus is on ${focused.tagName}.${focused.className}`);
  assert.equal(focused.closest('.et-history-item').dataset.id, '18');
});

test('a newer action of mine drops the older outcome owed: the server offers only my latest', async t => {
  const colleague = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  const older = { ...failedFrame('build'), id: '6f1c2a4e-0000-4000-8000-000000000003' };
  const active = activeAnswers({ job: colleague[0], ending: older }, { job: null });
  const own = [{ ...OPTIMISED[0], id: JOB_B }, { ...OPTIMISED[2], id: JOB_B }];
  let index = 0;
  let step = 0;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (isActive(url)) return active();
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague[index] });
      if (url === '/ops/exam-timetable/build/') return jobReply({ ok: true, job: { ...jobFrame('optimize_loaded', 'queued'), id: JOB_B } }, 202);
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: own[step] });
      if (url === `/ops/exam-timetable/jobs/${JOB_B}/result/`) return response({ ...savedRun(), run_id: 94 });
      if (url.includes('/seen/')) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed') && !ui.$('optimizeLoadedBtn').disabled);
  ui.$('optimizeLoadedBtn').click();
  await until(() => stageStates(ui).includes('running'), 'My own action is shown');
  step = 1;
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized && !ui.$('examJobClose').hidden, 'Mine ended, on show');
  const asked = activeAsks(ui);
  ui.$('examJobClose').click();
  await pause(40);
  assert.equal(activeAsks(ui), asked, 'Nothing older is owed: not asked again');
  assert.equal(ui.$('examJobPanel').hidden, true);
});

// Its acknowledgement lost, a job this page has shown is still unseen news to
// the server: the page itself remembers it has shown it.
const busyCheck = () => jobReply({ ok: false, error_code: 'solver_busy', error: 'busy', holder: { kind: 'exam_job', seconds: 2 } }, 503);
const activeAsks = ui => ui.requests.filter(request => isActive(request.url)).length;

test('my own failure shown here is not offered again as news from while the page was closed', async t => {
  const server = jobServer('optimize_loaded', [OPTIMISED[0], failedFrame('optimize_loaded')]);
  let ended = false;
  let seenTries = 0;
  server.override = async url => {
    if (url === seenUrl(JOB_ID)) { seenTries += 1; throw new TypeError('Failed to fetch'); }
    if (isActive(url)) return jobReply({ ok: true, job: ended ? failedFrame('optimize_loaded') : null });
    if (url === '/ops/exam-timetable/draft-impact/') return busyCheck();
    return undefined;
  };
  const ui = await loadedEditor(t, { liveUpdate: true, poll: { checkRetry: 60000 }, onRequest: server.onRequest });
  ui.$('optimizeLoadedBtn').click();
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  ended = true;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  await until(() => seenTries === 1, 'Marked seen - and the mark is lost');
  ui.$('examJobClose').click();
  const asked = activeAsks(ui);
  // A check the builder turns away looks for the job holding it.
  dropExam(ui, 'Tue');
  await until(() => activeAsks(ui) > asked, 'The server is asked');
  await pause(40);
  assert.equal(ui.$('examJobPanel').hidden, true, 'Already shown here');
});

test('my own saved timetable offered here is not offered again as news from while the page was closed', async t => {
  const frames = [
    jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'running' }, { key: 'place_exams', done: 3, total: 10 }),
    finishedFrame('optimize_loaded'),
  ];
  let index = 0;
  let seenTries = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, job: frames[index] });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url === seenUrl(JOB_ID)) { seenTries += 1; throw new TypeError('Failed to fetch'); }
      if (url === '/ops/exam-timetable/draft-impact/') return busyCheck();
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizing, 'Following my own job, found running');
  index = 1;
  await until(() => !ui.$('examJobOpen').hidden, 'Its timetable is offered');
  ui.$('examJobClose').click();
  await until(() => seenTries === 1, 'Marked seen - and the mark is lost');
  const asked = activeAsks(ui);
  dropExam(ui, 'Tue');
  await until(() => activeAsks(ui) > asked, 'The server is asked');
  await pause(40);
  assert.equal(ui.$('examJobPanel').hidden, true, 'Already offered here');
});

// ── the round-seven verification's findings ──

// A job of the colleague's that saves run 93, and one the registrar is shown.
const HUDA_SAVES = [
  theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
  finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
];
const run93 = () => ({ ...savedRun(), run_id: 93, label: 'Huda build' });
const offered = ui => !ui.$('examJobPanel').hidden && !ui.$('examJobOpen').hidden;

test('Copy keeps the keyboard on its row when a job ending reloads the list under the dialogs', async t => {
  let index = 0;
  const history = [{ id: 17, label: 'One' }, { id: 18, label: 'Two' }];
  const ui = await loadedEditor(t, {
    realDialogs: true,
    history,
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: HUDA_SAVES[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  // Unsaved changes: the copy asks before replacing them.
  dropExam(ui, 'Tue');
  await until(() => ui.$('historyList').querySelectorAll('.et-copy-btn').length === 2);
  const opener = ui.$('historyList').querySelectorAll('.et-copy-btn')[1];
  opener.focus();
  opener.click();
  await until(() => ui.window.document.activeElement.closest('.dlg-backdrop'), 'The name dialog takes focus');
  // Her job saved run 93: the list, newest first, now has it at the top.
  history.unshift({ id: 93, label: 'Huda build' });
  index = 1;
  await until(() => offered(ui), 'Huda’s job ends under the dialog');
  ui.window.document.querySelector('.dlg-backdrop .btn-confirm').click();
  // The list reloads as the name dialog goes; the discard dialog opens.
  await until(() => ui.window.document.querySelector('.dlg-backdrop')?.textContent.match(/Discard unsaved changes|تجاهل التغييرات/), 'The discard dialog opens');
  await until(() => ui.window.document.activeElement.closest('.dlg-backdrop'), 'It takes focus');
  ui.window.document.querySelector('.dlg-backdrop .btn-cancel').click();
  await pause(300);
  const focused = ui.window.document.activeElement;
  assert.ok(focused.classList.contains('et-copy-btn'), `Focus is on ${focused.tagName}.${focused.id || focused.className}`);
  assert.equal(focused.closest('.et-history-item').dataset.id, '18');
});

// The list's own reload keeps the keyboard's place here (focus is still on the
// row when it re-renders): this guards the Copy's own ending, which used to
// send focus to the Saved timetables summary.
test('a failed Copy keeps the keyboard on its row when a job ending reloaded the list meanwhile', async t => {
  let index = 0;
  const ui = await loadedEditor(t, {
    realDialogs: true,
    history: [{ id: 17, label: 'One' }, { id: 18, label: 'Two' }],
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: HUDA_SAVES[index] });
      // Slower than the list: it has been re-rendered by the time this fails.
      if (url.endsWith('/18/copy/')) { await pause(30); return jobReply({ ok: false, error: 'Server error' }, 500); }
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  await until(() => ui.$('historyList').querySelectorAll('.et-copy-btn').length === 2);
  const opener = ui.$('historyList').querySelectorAll('.et-copy-btn')[1];
  opener.focus();
  opener.click();
  await until(() => ui.window.document.activeElement.closest('.dlg-backdrop'), 'The name dialog takes focus');
  index = 1;
  await until(() => offered(ui));
  ui.window.document.querySelector('.dlg-backdrop .btn-confirm').click();
  await until(() => copyRequests(ui).length === 1);
  await until(() => /alert-danger/.test(ui.$('etStatus').className), 'The copy failed');
  await pause(50);
  const focused = ui.window.document.activeElement;
  assert.ok(focused.classList.contains('et-copy-btn'), `Focus is on ${focused.tagName}.${focused.id || focused.className}`);
  assert.equal(focused.closest('.et-history-item').dataset.id, '18');
});

test('my own saved timetable found on opening stops being offered once it is opened from Saved timetables', async t => {
  let seen = 0;
  const ui = await page(t, {
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Mine' }],
    activeJob: { ok: true, job: finishedFrame('build') },
    onRequest: async url => {
      if (url === '/ops/exam-timetable/93/') return response({ ...run93(), label: 'Mine' });
      if (url === seenUrl(JOB_ID)) { seen += 1; return jobReply({ ok: true, marked: true }); }
      return undefined;
    },
  });
  await until(() => offered(ui));
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  await until(() => ui.$('examJobPanel').hidden, 'The offer has been taken up');
  await until(() => seen === 1, 'And acknowledged');
});

test('a colleague’s saved timetable stops being offered once it is opened from Saved timetables', async t => {
  let index = 0;
  let seen = 0;
  const ui = await page(t, {
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Huda build' }],
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: HUDA_SAVES[index] });
      if (url === '/ops/exam-timetable/93/') return response(run93());
      if (url.includes('/seen/')) { seen += 1; return jobReply({ ok: true, marked: true }); }
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => offered(ui));
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  // "It is not open on this page" is no longer true: the panel goes.
  await until(() => ui.$('examJobPanel').hidden, 'The offer has been taken up');
  await pause(20);
  assert.equal(seen, 0, 'Not the registrar’s to acknowledge');
});

test('an offered timetable deleted meanwhile is said to be deleted, and not offered again', async t => {
  let index = 0;
  let gets = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: HUDA_SAVES[index] });
      if (url === '/ops/exam-timetable/93/') { gets += 1; return jobReply({ ok: false, code: 'run_not_found', error: 'Run not found' }, 404); }
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => offered(ui));
  ui.$('examJobOpen').focus();
  ui.$('examJobOpen').click();
  await until(() => ui.$('examJobOpen').hidden, 'Nothing is left to open');
  assert.equal(ui.$('examJobDetail').textContent, W.runDeleted);
  // Said where the load failed, too - in the page language, naming the one asked for.
  assert.ok(ui.$('etStatus').textContent.includes(W.openedGone), 'In the page language, not the server’s');
  assert.equal(ui.window.document.activeElement, ui.$('examJobTitle'), 'Open had focus; it is gone');
  assert.equal(gets, 1);
});

test('deleting the offered timetable from Saved timetables says so in the panel', async t => {
  let index = 0;
  const ui = await page(t, {
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Huda build' }],
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: HUDA_SAVES[index] });
      if (url === '/ops/exam-timetable/93/delete/') return jobReply({ ok: true });
      return undefined;
    },
  });
  ui.window.dlg.confirm = async () => true;
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => offered(ui));
  await until(() => ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-del-btn'));
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-del-btn').click();
  await until(() => ui.$('examJobOpen').hidden, 'Nothing is left to open');
  assert.equal(ui.$('examJobDetail').textContent, W.runDeleted);
  assert.equal(ui.$('examJobLive').textContent, W.runDeleted, 'Nothing else said it: the panel does');
});

test('an owed outcome of mine is asked for about itself, and asked again when the answer does not come', async t => {
  const running = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  const failed = theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' });
  const mine = { ...failedFrame('optimize_loaded'), id: JOB_B };
  let asked = 0;
  let index = 0;
  const ui = await page(t, {
    onRequest: async url => {
      if (isActive(url)) {
        asked += 1;
        if (asked === 1) return jobReply({ ok: true, job: running, ending: mine });
        if (asked === 2) throw new TypeError('Failed to fetch');
        return jobReply({ ok: true, job: mine });
      }
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: index ? failed : running });
      if (url === seenUrl(JOB_B)) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  ui.$('examJobClose').click();
  // No further click: the lost answer is asked for again.
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizeFailed, 'Mine, after the retry');
  const owed = ui.requests.filter(request => isActive(request.url)).slice(1).map(request => request.url);
  assert.deepEqual(owed, [`/ops/exam-timetable/jobs/active/?owed=${JOB_B}`, `/ops/exam-timetable/jobs/active/?owed=${JOB_B}`]);
});

test('an owed outcome of mine the server no longer offers is not shown from memory', async t => {
  const colleague = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  const mine = { ...failedFrame('optimize_loaded'), id: JOB_B };
  // Seen in another tab meanwhile: the server has nothing to show.
  const active = activeAnswers({ job: colleague[0], ending: mine }, { job: null });
  let index = 0;
  const ui = await page(t, {
    onRequest: async url => {
      if (isActive(url)) return active();
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague[index] });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  const asked = activeAsks(ui);
  ui.$('examJobClose').click();
  await until(() => activeAsks(ui) > asked, 'The server is asked');
  await pause(40);
  assert.equal(ui.$('examJobPanel').hidden, true, 'The server decides');
});

test('an owed outcome of mine answered while the panel shows another ending waits for that one to close', async t => {
  const first = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  const firstFailed = theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' });
  const second = { ...theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'), id: JOB_B };
  const secondFailed = { ...firstFailed, id: JOB_B };
  const mineId = '6f1c2a4e-0000-4000-8000-000000000003';
  const mine = { ...failedFrame('optimize_loaded'), id: mineId };
  let plain = 0;
  let firstIndex = 0;
  let secondIndex = 0;
  let releaseOwed = null;
  let owedAsks = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (isActive(url) && url.includes('?owed=')) {
        owedAsks += 1;
        // The first re-ask is slow: the panel changes hands before it answers.
        if (owedAsks === 1) await new Promise(resolve => { releaseOwed = resolve; });
        return jobReply({ ok: true, job: mine });
      }
      if (isActive(url)) {
        plain += 1;
        // Past the hour by now: only the owed ask still brings it back.
        return jobReply({ ok: true, job: plain === 1 ? first : (secondIndex ? secondFailed : second), ...(plain === 1 ? { ending: mine } : {}) });
      }
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: firstIndex ? firstFailed : first });
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: secondIndex ? secondFailed : second });
      if (url === '/ops/exam-timetable/draft-impact/') return busyCheck();
      if (url.includes('/seen/')) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  firstIndex = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  ui.$('examJobClose').click();
  await until(() => releaseOwed, 'The owed ending is asked for');
  // Meanwhile a check is turned away: the page follows the job holding it,
  // which ends before the owed answer comes.
  dropExam(ui, 'Tue');
  await until(() => ui.$('examJobTitle').textContent === TEXT.building && !ui.$('examJobPanel').hidden);
  secondIndex = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  releaseOwed();
  await pause(40);
  assert.notEqual(ui.$('examJobTitle').textContent, TEXT.optimizeFailed, 'Not over the ending on screen');
  ui.$('examJobClose').click();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizeFailed, 'Mine, once that one is closed');
});

test('my own saved timetable, replaced on the panel before I acted on it, is offered again as this page saw it', async t => {
  const mineRunning = jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'running' }, { key: 'place_exams', done: 3, total: 10 }, { can_cancel: true });
  const mineSaved = finishedFrame('optimize_loaded');
  const huda = [
    { ...theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'), id: JOB_B },
    { ...theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }), id: JOB_B },
  ];
  let mineIndex = 0;
  let hudaIndex = 0;
  let hudaStarted = false;
  let seenMine = 0;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (isActive(url)) {
        if (url.includes('?owed=')) return jobReply({ ok: true, job: mineSaved });
        // Unacknowledged, mine is still news to the server.
        return jobReply({ ok: true, job: hudaStarted ? huda[hudaIndex] : (mineIndex ? mineSaved : mineRunning), ...(hudaStarted ? { ending: mineSaved } : {}) });
      }
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: mineIndex ? mineSaved : mineRunning });
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: huda[hudaIndex] });
      if (url === '/ops/exam-timetable/build/') {
        hudaStarted = true;
        return jobReply({ ok: false, error_code: 'job_in_progress', error: 'x', active_job: { kind: 'build', mine: false, owner: 'Huda' } }, 409);
      }
      if (url === seenUrl(JOB_ID)) { seenMine += 1; return jobReply({ ok: true, marked: true }); }
      return undefined;
    },
  });
  // Found running on opening - mine, from another tab - and it saves.
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizing);
  mineIndex = 1;
  await until(() => offered(ui), 'Mine is offered');
  // Before acting on it, Optimize: Huda has just started, so it is refused and
  // her job takes the panel.
  ui.$('optimizeLoadedBtn').click();
  await until(() => ui.$('examJobTitle').textContent === TEXT.building, 'Her job takes the panel');
  hudaIndex = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  ui.$('examJobClose').click();
  await until(() => offered(ui), 'Mine is offered again');
  assert.equal(ui.$('examJobDetail').textContent, language === 'ar' ? 'اكتملت العملية التي بدأتها وحُفظ الجدول.' : 'The timetable action you started finished and was saved.',
    'As this page saw it end, not "while this page was closed"');
  assert.equal(seenMine, 0, 'Still not acted on');
});

test('a colleague’s refused action followed here says nothing about unsaved changes', async t => {
  const frames = [
    theirs('optimize_loaded', 'running', { read_board: 'done', place_exams: 'running' }, 'place_exams'),
    finishedFrame('optimize_loaded', { mine: false, can_cancel: false, owner: 'Huda', has_run: false, result_run_id: null, refused: true }),
  ];
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: frames[index] }) : undefined,
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
  const detail = ui.$('examJobDetail').textContent;
  assert.match(detail, language === 'ar' ? /يمكنك الآن استخدام البناء/ : /You can use Build, Optimize, Fix and Save again\.$/);
  assert.ok(!detail.includes(W.draft), `Said: ${detail}`);
});

test('a failure of mine followed from another tab, whose acknowledgement is lost, is not shown again', async t => {
  const frames = [
    jobFrame('optimize_loaded', 'running', { read_board: 'done', place_exams: 'running' }, { key: 'place_exams', done: 3, total: 10 }),
    failedFrame('optimize_loaded'),
  ];
  let index = 0;
  let seenTries = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, job: frames[index] });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url === seenUrl(JOB_ID)) { seenTries += 1; throw new TypeError('Failed to fetch'); }
      if (url === '/ops/exam-timetable/draft-impact/') return busyCheck();
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizing, 'Mine, from another tab');
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  await until(() => seenTries === 1, 'Marked seen - and the mark is lost');
  ui.$('examJobClose').click();
  const asked = activeAsks(ui);
  dropExam(ui, 'Tue');
  await until(() => activeAsks(ui) > asked, 'The server is asked');
  await pause(40);
  assert.equal(ui.$('examJobPanel').hidden, true, 'Already shown here');
});

for (const code of ['server_error', 'server_restarted', 'timed_out', 'never_started']) {
  test(`a failure of mine (${code}) found on opening says where the draft is before advising`, async t => {
    const ui = await page(t, {
      activeJob: { ok: true, job: failedFrame('optimize_loaded', { error_code: code }) },
      onRequest: async url => (url === seenUrl(JOB_ID) ? jobReply({ ok: true, marked: true }) : undefined),
    });
    await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
    const detail = ui.$('examJobDetail').textContent;
    assert.ok(detail.includes([W.nothingSaved, W.draft, W.advice[code]].join(' ')), `Said: ${detail}`);
    // Today's time needs no date.
    assert.doesNotMatch(detail, language === 'ar' ? /يوم \d/ : / on \d/);
  });
}
for (const code of ['inputs_changed', 'check_required']) {
  test(`a Save of mine refused for ${code} in another tab, followed here, says what to do there`, async t => {
    const frames = [
      jobFrame('save_loaded_changes', 'running', { read_board: 'done' }, { key: 'read_board', done: null, total: null }),
      finishedFrame('save_loaded_changes', { has_run: false, result_run_id: null, refused: true }),
    ];
    let index = 0;
    const ui = await page(t, {
      activeJob: { ok: true, job: frames[0] },
      onRequest: async url => {
        if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
        if (url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`) return jobReply({ ok: false, error_code: code, error: 'x' }, 409);
        return undefined;
      },
    });
    await until(() => !ui.$('examJobPanel').hidden);
    index = 1;
    await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
    const detail = ui.$('examJobDetail').textContent;
    endsWith(detail, W.reason[code], W.recheck.followed);
    assert.ok(!detail.includes(W.away) && !detail.includes(W.draft), 'Followed here, not found on return');
  });
}
test('an owed outcome of mine is dropped, not asked for again, once an action of my own is accepted', async t => {
  const colleague = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts'),
    theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' }),
  ];
  const older = { ...failedFrame('build'), id: JOB_B };
  const JOB_C = '6f1c2a4e-0000-4000-8000-000000000003';
  const own = [{ ...OPTIMISED[0], id: JOB_C }, { ...OPTIMISED[2], id: JOB_C }];
  let index = 0;
  let step = 0;
  let submitted = false;
  let releaseOwed = null;
  const ui = await loadedEditor(t, {
    onRequest: async url => {
      if (isActive(url) && url.includes('?owed=')) {
        // Slow: my own action is accepted before it answers.
        await new Promise(resolve => { releaseOwed = resolve; });
        return jobReply({ ok: true, job: own[step] });
      }
      if (isActive(url)) return jobReply({ ok: true, job: colleague[index], ending: older });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: colleague[index] });
      if (url === '/ops/exam-timetable/build/') { submitted = true; return jobReply({ ok: true, job: { ...jobFrame('optimize_loaded', 'queued'), id: JOB_C } }, 202); }
      if (url === pollUrl(JOB_C)) return jobReply({ ok: true, job: own[step] });
      if (url === `/ops/exam-timetable/jobs/${JOB_C}/result/`) return response({ ...savedRun(), run_id: 94 });
      if (url.includes('/seen/')) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed') && !ui.$('optimizeLoadedBtn').disabled);
  ui.$('examJobClose').click();
  await until(() => releaseOwed, 'The owed ending is asked for');
  ui.$('optimizeLoadedBtn').click();
  await until(() => submitted && stageStates(ui).includes('running'), 'My own action is accepted and shown');
  releaseOwed();
  step = 1;
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized && !ui.$('examJobClose').hidden, 'Mine ended, on show');
  ui.$('examJobClose').click();
  await pause(40);
  const owedAsks = ui.requests.filter(request => isActive(request.url) && request.url.includes('?owed=')).length;
  assert.equal(owedAsks, 1, 'Superseded by my own action: not asked for again');
});

test('my own outcome whose report could not load is shown again with its reason, as this page saw it end', async t => {
  let results = 0;
  const final = finishedFrame('optimize_loaded', { has_run: false, result_run_id: null, refused: true });
  const server = jobServer('optimize_loaded', [OPTIMISED[0], final], {
    result: () => {
      results += 1;
      // Lost three times while the page watched it end; answered later.
      if (results <= 3) throw new TypeError('Failed to fetch');
      return jobReply({ ok: false, error_code: 'inputs_changed', error: 'x' }, 409);
    },
  });
  let ended = false;
  server.override = async url => {
    // Its answer never read, it is still news to the server.
    if (isActive(url)) return jobReply({ ok: true, job: ended ? final : null });
    if (url === '/ops/exam-timetable/draft-impact/') return busyCheck();
    return undefined;
  };
  const ui = await loadedEditor(t, { liveUpdate: true, poll: { checkRetry: 60000 }, onRequest: server.onRequest });
  ui.$('optimizeLoadedBtn').click();
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  ended = true;
  await until(() => /could not be loaded here|تعذّر تحميل تقريرها/.test(ui.$('examJobDetail').textContent));
  ui.$('examJobClose').click();
  const asked = activeAsks(ui);
  dropExam(ui, 'Tue');
  await until(() => activeAsks(ui) > asked, 'The server is asked');
  await until(() => /source data changed|بيانات مصدر الجدول/.test(ui.$('examJobDetail').textContent), 'Shown again, with its reason now');
  const detail = ui.$('examJobDetail').textContent;
  assert.ok(!detail.includes(W.away), 'This page saw it end');
  // The draft is on this very page: the step is this page's.
  endsWith(detail, W.reason.inputs_changed, W.recheck.own);
});

// ── the round-eight verification's findings ──

test('a job whose timetable was opened before this page heard it ended offers nothing', async t => {
  let index = 0;
  let seen = 0;
  const ui = await page(t, {
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Huda build' }],
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: HUDA_SAVES[index] });
      if (url === '/ops/exam-timetable/93/') return response(run93());
      if (url.includes('/seen/')) { seen += 1; return jobReply({ ok: true, marked: true }); }
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  // Her run is committed; this page's next poll has not come yet.
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  await until(() => ui.$('historyList').querySelector('.et-history-item.active')?.dataset.id === '93', 'Run 93 is on the board');
  index = 1;
  await until(() => ui.$('examJobPanel').hidden, 'Nothing to offer: it is open already');
  await pause(20);
  assert.equal(ui.$('examJobPanel').hidden, true);
  assert.equal(seen, 0, 'Not the registrar’s to acknowledge');
});

test('the offered timetable, deleted, opened from Saved timetables: the panel says so quietly and the list drops it', async t => {
  let index = 0;
  const history = [{ id: 17, label: 'One' }, { id: 93, label: 'Huda build' }];
  const ui = await page(t, {
    history,
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: HUDA_SAVES[index] });
      if (url === '/ops/exam-timetable/93/') return jobReply({ ok: false, code: 'run_not_found', error: 'Run not found' }, 404);
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => offered(ui));
  const said = ui.$('examJobLive').textContent;
  const loads = historyLoads(ui);
  // Deleted meanwhile, by someone else.
  history.splice(1, 1);
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  await until(() => ui.$('examJobOpen').hidden, 'Nothing is left to open');
  assert.equal(ui.$('examJobDetail').textContent, W.runDeleted);
  assert.ok(ui.$('etStatus').textContent.includes(W.openedGone));
  assert.equal(ui.$('examJobLive').textContent, said, 'The failed load has said it aloud: not again');
  await until(() => historyLoads(ui) > loads && !ui.$('historyList').querySelector('.et-history-item[data-id="93"]'), 'The list no longer shows it');
});

test('deleting the timetable my own action saved and opened stops the panel calling it open below', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED);
  let saved = false;
  server.override = async url => {
    if (url.startsWith('/ops/exam-timetable/list/')) {
      return jobReply({ ok: true, runs: saved ? [{ id: 93, label: 'Optimized' }, { id: 17, label: 'One' }] : [{ id: 17, label: 'One' }] });
    }
    if (url === '/ops/exam-timetable/93/delete/') return jobReply({ ok: true });
    return undefined;
  };
  ui.window.dlg.confirm = async () => true;
  await until(() => stageStates(ui).includes('running'));
  saved = true;
  server.advance();
  server.advance();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized && !ui.$('examJobClose').hidden, 'Saved and open below');
  await until(() => ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-del-btn'));
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-del-btn').click();
  await until(() => ui.$('examJobDetail').textContent === W.runDeleted, 'Said to be deleted');
  assert.equal(ui.$('examJobLive').textContent, W.runDeleted, 'And said aloud: nothing else did');
  await pause(20);
  assert.equal(ui.$('examJobPanel').hidden, false, 'The deleted notice stays up as the board empties');
});

test('opening another timetable stops the panel calling my own result open below', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED);
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  server.advance();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized && !ui.$('examJobClose').hidden, 'Saved and open below');
  // Run 17, the one this page had before.
  ui.$('historyList').querySelector('.et-history-item[data-id="17"] .et-run-info').click();
  await until(() => ui.$('examJobPanel').hidden, 'No longer true: the panel goes');
});

test('a Save of mine followed from another page, shown again after its report loads, keeps its step', async t => {
  const frames = [
    jobFrame('save_loaded_changes', 'running', { read_board: 'done' }, { key: 'read_board', done: null, total: null }),
    finishedFrame('save_loaded_changes', { has_run: false, result_run_id: null, refused: true }),
  ];
  let index = 0;
  let results = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, job: frames[index] });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`) {
        results += 1;
        if (results <= 3) throw new TypeError('Failed to fetch');
        return jobReply({ ok: false, error_code: 'inputs_changed', error: 'x' }, 409);
      }
      if (url === '/ops/exam-timetable/draft-impact/') return busyCheck();
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  // Its report lost three times: no reason, and not acknowledged.
  await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
  ui.$('examJobClose').click();
  const asked = activeAsks(ui);
  dropExam(ui, 'Tue');
  await until(() => activeAsks(ui) > asked, 'The server is asked');
  await until(() => ui.$('examJobDetail').textContent.includes(W.reason.inputs_changed), 'Shown again, with its reason');
  endsWith(ui.$('examJobDetail').textContent, W.reason.inputs_changed, W.recheck.followed);
});

test('a Save of mine found on opening, shown again after its report loads, keeps where the draft is and its step', async t => {
  const refused = finishedFrame('save_loaded_changes', { has_run: false, result_run_id: null, refused: true });
  let results = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    activeJob: { ok: true, job: refused },
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, job: refused });
      if (url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`) {
        results += 1;
        if (results <= 3) throw new TypeError('Failed to fetch');
        return jobReply({ ok: false, error_code: 'check_required', error: 'x' }, 409);
      }
      if (url === '/ops/exam-timetable/draft-impact/') return busyCheck();
      return undefined;
    },
  });
  await until(() => /could not be loaded here|تعذّر تحميل تقريرها/.test(ui.$('examJobDetail').textContent) || ui.$('examJobPanel').classList.contains('is-refused'));
  ui.$('examJobClose').click();
  const asked = activeAsks(ui);
  dropExam(ui, 'Tue');
  await until(() => activeAsks(ui) > asked, 'The server is asked');
  await until(() => ui.$('examJobDetail').textContent.includes(W.reason.check_required), 'Shown again, with its reason');
  const detail = ui.$('examJobDetail').textContent;
  assert.ok(!detail.includes(W.away), 'Not news from while the page was closed a second time');
  endsWith(detail, W.reason.check_required, W.draft, W.recheck.away);
});

test('an ending of mine from another day says which day', async t => {
  const failed = failedFrame('optimize_loaded', {
    submitted_at: '2026-09-22T10:00:00+00:00',
    finished_at: '2026-09-22T10:00:30+00:00',
    now: '2026-09-23T10:00:05+00:00',
  });
  const ui = await page(t, {
    activeJob: { ok: true, job: failed },
    onRequest: async url => (url === seenUrl(JOB_ID) ? jobReply({ ok: true, marked: true }) : undefined),
  });
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /بدأتها يوم 22 سبتمبر الساعة / : /you started on 22 Sept? at /);
});

test('an owed outcome of mine the server cannot give is asked for a bounded number of times, then when the panel is next free', async t => {
  const first = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  const firstFailed = theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' });
  const second = { ...first, id: JOB_B };
  const secondFailed = { ...firstFailed, id: JOB_B };
  const mineId = '6f1c2a4e-0000-4000-8000-000000000003';
  const mine = { ...failedFrame('optimize_loaded'), id: mineId };
  let plain = 0;
  let firstIndex = 0;
  let secondIndex = 0;
  let serverBack = false;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (isActive(url) && url.includes('?owed=')) {
        if (!serverBack) throw new TypeError('Failed to fetch');
        return jobReply({ ok: true, job: mine });
      }
      if (isActive(url)) {
        plain += 1;
        return jobReply({ ok: true, job: plain === 1 ? first : (secondIndex ? secondFailed : second), ...(plain === 1 ? { ending: mine } : {}) });
      }
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: firstIndex ? firstFailed : first });
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: secondIndex ? secondFailed : second });
      if (url === '/ops/exam-timetable/draft-impact/') return busyCheck();
      if (url.includes('/seen/')) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  const owedAsks = () => ui.requests.filter(request => isActive(request.url) && request.url.includes('?owed=')).length;
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  firstIndex = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  ui.$('examJobClose').click();
  // Once, then once per backoff step: never a loop against a server that is down.
  await until(() => owedAsks() === 5, () => `Asked ${owedAsks()} times`);
  await pause(60);
  assert.equal(owedAsks(), 5, 'Bounded');
  // Still owed: asked for again when the panel is next free.
  serverBack = true;
  dropExam(ui, 'Tue');
  await until(() => ui.$('examJobTitle').textContent === TEXT.building && !ui.$('examJobPanel').hidden, 'Another job takes the panel');
  secondIndex = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  ui.$('examJobClose').click();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizeFailed, 'Mine, at last');
  assert.equal(owedAsks(), 6);
});

test('an owed outcome of mine is carried through a second colleague’s job', async t => {
  const huda = theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts');
  const hudaFailed = theirs('build', 'failed', { enrolments: 'done', conflicts: 'stopped' }, 'conflicts', { error_code: 'server_error', finished_at: '2026-09-23T10:00:30+00:00' });
  const sara = { ...huda, id: JOB_B, owner: 'Sara' };
  const saraFailed = { ...hudaFailed, id: JOB_B, owner: 'Sara' };
  const mineId = '6f1c2a4e-0000-4000-8000-000000000003';
  const mine = { ...failedFrame('optimize_loaded'), id: mineId };
  let hudaIndex = 0;
  let saraIndex = 0;
  let owed = 0;
  const ui = await page(t, {
    onRequest: async url => {
      if (isActive(url) && url.includes('?owed=')) {
        owed += 1;
        // Sara started as Huda's ended: the owed ending comes back beside hers.
        return jobReply({ ok: true, ...(owed === 1 ? { job: saraIndex ? saraFailed : sara, ending: mine } : { job: mine }) });
      }
      if (isActive(url)) return jobReply({ ok: true, job: huda, ending: mine });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: hudaIndex ? hudaFailed : huda });
      if (url === pollUrl(JOB_B)) return jobReply({ ok: true, job: saraIndex ? saraFailed : sara });
      if (url.includes('/seen/')) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  hudaIndex = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  ui.$('examJobClose').click();
  await until(() => ui.$('examJobTitle').textContent === TEXT.building && !ui.$('examJobPanel').hidden, 'Sara’s job takes the panel');
  saraIndex = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-failed'));
  ui.$('examJobClose').click();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizeFailed, 'Mine, after hers');
  assert.equal(owed, 2);
});

test('a copy opened on the board stops the panel calling my own result open below', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED);
  let saved = false;
  server.override = async url => {
    if (url.startsWith('/ops/exam-timetable/list/')) {
      return jobReply({ ok: true, runs: saved ? [{ id: 93, label: 'Optimized' }, { id: 17, label: 'One' }] : [{ id: 17, label: 'One' }] });
    }
    if (url === '/ops/exam-timetable/93/copy/') return { ...response({ ...savedRun(), run_id: 94, source_run_id: 93, label: 'Optimized copy' }), status: 201 };
    return undefined;
  };
  ui.window.dlg.prompt = async () => 'Optimized copy';
  await until(() => stageStates(ui).includes('running'));
  saved = true;
  server.advance();
  server.advance();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized && !ui.$('examJobClose').hidden, 'Saved and open below');
  await until(() => ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-copy-btn'));
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-copy-btn').click();
  await until(() => copyRequests(ui).length === 1);
  await until(() => ui.$('examJobPanel').hidden, 'The copy is on the board now: the panel goes');
});

// ── the round-nine verification's findings ──

async function ownSavedOpenBelow(t) {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED);
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  server.advance();
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimized && !ui.$('examJobClose').hidden, 'Saved and open below');
  return { ui, server };
}

test('a programme filter change, emptying the board, stops the panel calling my result open below', async t => {
  const { ui } = await ownSavedOpenBelow(t);
  const chip = ui.$('progList').querySelector('input');
  chip.checked = !chip.checked;
  chip.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
  await until(() => ui.$('examJobPanel').hidden, 'Nothing is open below now');
});

test('Load Courses, emptying the board, stops the panel calling my result open below', async t => {
  const { ui } = await ownSavedOpenBelow(t);
  ui.$('loadCoursesBtn').click();
  await until(() => ui.$('examJobPanel').hidden, 'Nothing is open below now');
});

test('an action whose saved timetable was deleted before its answer was read says so, not "this timetable"', async t => {
  const { ui, server } = await optimiseAsJob(t, OPTIMISED, {
    serverOptions: { result: { ok: false, error_code: 'run_deleted', error: 'That run was deleted.' }, resultStatus: 410 },
  });
  await until(() => stageStates(ui).includes('running'));
  server.advance();
  server.advance();
  await until(() => [ui.$('etStatus'), ui.$('examEditorRequestError')].some(element => element.textContent.includes(W.savedGone)), 'Said, naming the saved one');
  for (const element of [ui.$('etStatus'), ui.$('examEditorRequestError')]) {
    assert.ok(!element.textContent.includes(W.runDeleted), `Said: ${element.textContent}`);
  }
});

test('a Stop dialog closing after the panel went for the run on the board leaves the keyboard on the board', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts', { can_cancel: true }),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  let index = 0;
  const ui = await page(t, {
    browserFocus: true,
    realDialogs: true,
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Huda build' }],
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url === '/ops/exam-timetable/93/') return response(run93());
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden && !ui.$('examJobCancel').hidden);
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  await until(() => ui.$('historyList').querySelector('.et-history-item.active')?.dataset.id === '93');
  ui.$('examJobCancel').focus();
  ui.$('examJobCancel').click();
  await until(() => ui.window.document.activeElement.closest('.dlg-backdrop'), 'The stop dialog takes focus');
  index = 1;
  await until(() => ui.$('examJobPanel').hidden, 'Its run is on the board: the panel goes');
  ui.window.document.querySelector('.dlg-backdrop .btn-cancel').click();
  await pause(300);
  assert.equal(ui.window.document.activeElement, ui.$('examScheduleHeading'), `Focus is on ${ui.window.document.activeElement.id || ui.window.document.activeElement.tagName}`);
});

test('the panel going for the run on the board takes the keyboard out of it', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts', { can_cancel: true }),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  let index = 0;
  const ui = await page(t, {
    browserFocus: true,
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Huda build' }],
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url === '/ops/exam-timetable/93/') return response(run93());
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden && !ui.$('examJobCancel').hidden);
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  await until(() => ui.$('historyList').querySelector('.et-history-item.active')?.dataset.id === '93');
  ui.$('examJobCancel').focus();
  index = 1;
  await until(() => ui.$('examJobPanel').hidden);
  assert.equal(ui.window.document.activeElement, ui.$('examScheduleHeading'), `Focus is on ${ui.window.document.activeElement.id || ui.window.document.activeElement.tagName}`);
});

test('a saved timetable of mine from another day says which day it was saved', async t => {
  const saved = finishedFrame('build', {
    submitted_at: '2026-09-22T10:00:00+00:00',
    finished_at: '2026-09-22T10:01:43+00:00',
    now: '2026-09-23T10:00:05+00:00',
  });
  const ui = await page(t, { activeJob: { ok: true, job: saved } });
  await until(() => offered(ui));
  assert.match(ui.$('examJobDetail').textContent, language === 'ar' ? /وحُفظ الجدول يوم 22 سبتمبر الساعة / : /it was saved on 22 Sept? at /);
});

test('deleting, or failing to open, another timetable leaves the offer alone', async t => {
  let index = 0;
  const ui = await page(t, {
    history: [{ id: 17, label: 'One' }, { id: 18, label: 'Two' }, { id: 93, label: 'Huda build' }],
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: HUDA_SAVES[index] });
      if (url === '/ops/exam-timetable/17/delete/') return jobReply({ ok: true });
      if (url === '/ops/exam-timetable/18/') return jobReply({ ok: false, code: 'run_not_found', error: 'Run not found' }, 404);
      return undefined;
    },
  });
  ui.window.dlg.confirm = async () => true;
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => offered(ui));
  const detail = ui.$('examJobDetail').textContent;
  ui.$('historyList').querySelector('.et-history-item[data-id="17"] .et-del-btn').click();
  await until(() => ui.requests.some(request => request.url === '/ops/exam-timetable/17/delete/'));
  await pause(30);
  ui.$('historyList').querySelector('.et-history-item[data-id="18"] .et-run-info').click();
  await until(() => ui.$('etStatus').textContent.includes(W.openedGone));
  await pause(20);
  assert.ok(offered(ui), 'Run 93 is still there to open');
  assert.equal(ui.$('examJobDetail').textContent, detail);
});

test('an outcome of mine owed behind a job whose run is already on the board is shown when that panel goes', async t => {
  const mine = { ...failedFrame('optimize_loaded'), id: JOB_B };
  let index = 0;
  const ui = await page(t, {
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Huda build' }],
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, ...(url.includes('?owed=') ? { job: mine } : { job: HUDA_SAVES[0], ending: mine }) });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: HUDA_SAVES[index] });
      if (url === '/ops/exam-timetable/93/') return response(run93());
      if (url.includes('/seen/')) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  await until(() => ui.$('historyList').querySelector('.et-history-item.active')?.dataset.id === '93');
  index = 1;
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizeFailed, 'Mine, as hers goes');
});

test('my own job from another page, whose run is already on the board, is acknowledged as it ends', async t => {
  const frames = [
    jobFrame('build', 'running', { enrolments: 'done', conflicts: 'running' }, { key: 'conflicts', done: null, total: null }),
    finishedFrame('build'),
  ];
  let index = 0;
  let seen = 0;
  const ui = await page(t, {
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Mine' }],
    activeJob: { ok: true, job: frames[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url === '/ops/exam-timetable/93/') return response(run93());
      if (url === seenUrl(JOB_ID)) { seen += 1; return jobReply({ ok: true, marked: true }); }
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  await until(() => ui.$('historyList').querySelector('.et-history-item.active')?.dataset.id === '93');
  index = 1;
  await until(() => ui.$('examJobPanel').hidden);
  await until(() => seen === 1, 'Its news taken up: acknowledged');
});

test('a Save of mine followed from another page keeps its step however many times it is shown again', async t => {
  const frames = [
    jobFrame('save_loaded_changes', 'running', { read_board: 'done' }, { key: 'read_board', done: null, total: null }),
    finishedFrame('save_loaded_changes', { has_run: false, result_run_id: null, refused: true }),
  ];
  let index = 0;
  let results = 0;
  const ui = await loadedEditor(t, {
    liveUpdate: true,
    poll: { checkRetry: 60000 },
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, job: frames[index] });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url === `/ops/exam-timetable/jobs/${JOB_ID}/result/`) {
        results += 1;
        // Lost on two showings: read on the third.
        if (results <= 6) throw new TypeError('Failed to fetch');
        return jobReply({ ok: false, error_code: 'inputs_changed', error: 'x' }, 409);
      }
      if (url === '/ops/exam-timetable/draft-impact/') return busyCheck();
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  index = 1;
  await until(() => ui.$('examJobPanel').classList.contains('is-refused'));
  for (const day of ['Tue', 'Wed']) {
    const seenResults = results;
    ui.$('examJobClose').click();
    const asked = activeAsks(ui);
    dropExam(ui, day);
    await until(() => activeAsks(ui) > asked, 'The server is asked');
    await until(() => results > seenResults && !ui.$('examJobPanel').hidden && ui.$('examJobPanel').classList.contains('is-refused'), 'Shown again');
  }
  await until(() => ui.$('examJobDetail').textContent.includes(W.reason.inputs_changed), 'With its reason, at last');
  const detail = ui.$('examJobDetail').textContent;
  endsWith(detail, W.reason.inputs_changed, W.recheck.followed);
  assert.ok(!detail.includes(W.draft), 'Still followed from another page, not found on return');
});

// ── the round-ten verification's findings ──

test('a Stop dialog closing after my owed outcome took the panel leaves the keyboard on its title', async t => {
  const frames = [
    theirs('build', 'running', { enrolments: 'done', conflicts: 'running' }, 'conflicts', { can_cancel: true }),
    finishedFrame('build', { mine: false, can_cancel: false, owner: 'Huda' }),
  ];
  const mine = { ...failedFrame('optimize_loaded'), id: JOB_B };
  let index = 0;
  const ui = await page(t, {
    browserFocus: true,
    realDialogs: true,
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Huda build' }],
    onRequest: async url => {
      if (isActive(url)) return jobReply({ ok: true, ...(url.includes('?owed=') ? { job: mine } : { job: frames[0], ending: mine }) });
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: frames[index] });
      if (url === '/ops/exam-timetable/93/') return response(run93());
      if (url.includes('/seen/')) return jobReply({ ok: true, marked: true });
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden && !ui.$('examJobCancel').hidden);
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  await until(() => ui.$('historyList').querySelector('.et-history-item.active')?.dataset.id === '93');
  ui.$('examJobCancel').focus();
  ui.$('examJobCancel').click();
  await until(() => ui.window.document.activeElement.closest('.dlg-backdrop'), 'The stop dialog takes focus');
  index = 1;
  await until(() => ui.$('examJobTitle').textContent === TEXT.optimizeFailed, 'My owed outcome takes the panel');
  ui.window.document.querySelector('.dlg-backdrop .btn-cancel').click();
  await pause(300);
  assert.equal(ui.window.document.activeElement, ui.$('examJobTitle'), `Focus is on ${ui.window.document.activeElement.id || ui.window.document.activeElement.tagName}`);
});

test('the panel going for the run on the board leaves the keyboard where it was when it was elsewhere', async t => {
  let index = 0;
  const ui = await page(t, {
    browserFocus: true,
    history: [{ id: 17, label: 'One' }, { id: 93, label: 'Huda build' }],
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => {
      if (url === pollUrl(JOB_ID)) return jobReply({ ok: true, job: HUDA_SAVES[index] });
      if (url === '/ops/exam-timetable/93/') return response(run93());
      return undefined;
    },
  });
  await until(() => !ui.$('examJobPanel').hidden);
  ui.$('historyList').querySelector('.et-history-item[data-id="93"] .et-run-info').click();
  await until(() => ui.$('historyList').querySelector('.et-history-item.active')?.dataset.id === '93');
  // The registrar is typing in the board's filter.
  ui.$('schedFilter').focus();
  index = 1;
  await until(() => ui.$('examJobPanel').hidden);
  await pause(20);
  assert.equal(ui.window.document.activeElement, ui.$('schedFilter'), 'Not taken from where the registrar is working');
});

test('a filter change leaves a colleague’s running job and its offer on the panel', async t => {
  let index = 0;
  const ui = await page(t, {
    activeJob: { ok: true, job: HUDA_SAVES[0] },
    onRequest: async url => url === pollUrl(JOB_ID) ? jobReply({ ok: true, job: HUDA_SAVES[index] }) : undefined,
  });
  const toggle = () => {
    const chip = ui.$('progList').querySelector('input');
    chip.checked = !chip.checked;
    chip.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
  };
  await until(() => ui.$('examJobTitle').textContent === TEXT.building);
  toggle();
  await pause(20);
  assert.equal(ui.$('examJobPanel').hidden, false, 'Still following her job');
  assert.equal(ui.$('examJobTitle').textContent, TEXT.building);
  index = 1;
  await until(() => offered(ui), 'Her ending is offered');
  toggle();
  await pause(20);
  assert.ok(offered(ui), 'Her timetable is still there to open');
});

// ── the round-eleven verification's findings ──

// In a browser, hiding the panel under the keyboard drops it to the body; the
// action's own ending then puts it where the registrar works.
test('my own Optimize turned down while the keyboard is on Stop leaves it on the board', async t => {
  const server = jobServer('optimize_loaded', [OPTIMISED[0], finishedFrame('optimize_loaded', { has_run: false, result_run_id: null, refused: true })], {
    result: { ok: false, error_code: 'inputs_changed', error: 'Source inputs changed since the check.' }, resultStatus: 409,
  });
  const ui = await loadedEditor(t, { browserFocus: true, onRequest: server.onRequest });
  ui.$('optimizeLoadedBtn').click();
  await until(() => stageStates(ui).includes('running') && !ui.$('examJobCancel').hidden);
  ui.$('examJobCancel').focus();
  server.advance();
  await until(() => ui.$('examJobPanel').hidden, 'Turned down: reported where it always was');
  await pause(20);
  assert.equal(ui.window.document.activeElement, ui.$('examEditHeading'));
});

test('my own Build turned down while the keyboard is on Stop gives it back to Build', async t => {
  const server = jobServer('build', [
    jobFrame('build', 'running', { enrolments: 'done', conflicts: 'running' }, { key: 'conflicts', done: null, total: null }),
    finishedFrame('build', { has_run: false, result_run_id: null, refused: true }),
  ], { result: { ok: false, error: 'The exam period has no usable days.' }, resultStatus: 400 });
  const ui = await page(t, { browserFocus: true, onRequest: server.onRequest });
  ui.$('buildBtn').click();
  await until(() => stageStates(ui).includes('running') && !ui.$('examJobCancel').hidden);
  ui.$('examJobCancel').focus();
  server.advance();
  await until(() => ui.$('examJobPanel').hidden && /alert-danger/.test(ui.$('etStatus').className), 'Turned down: reported where it always was');
  await pause(20);
  assert.equal(ui.window.document.activeElement, ui.$('buildBtn'));
});
