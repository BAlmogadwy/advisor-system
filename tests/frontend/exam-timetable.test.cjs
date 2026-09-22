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

async function page(t, { history = [], initialCourses = courses, run = null, loadCourses = true, onRequest = null, liveUpdate = true, realDialogs = false, committee = false } = {}) {
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
  return { window, $, emit, input, select, options, rows, times, addPeriod, requests, dialogs, prompts, notifications, scrollCalls, setCourses(next) { catalog = next; } };
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

test('a repair tells the registrar exactly which exams moved, and that it was the minimum', async t => {
  let ui;
  ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? repairedRun(ui, {
          moves: [{ course_code: 'CS201', course_name: 'Algorithms', from: { day: 'Mon', period: '08:00-10:00' }, to: { day: 'Tue', period: '10:30-12:30' } }],
          unseated: [], violations_before: 1, violations_after: 0, proven_minimal: true, status: 'OPTIMAL',
        })
      : undefined,
  });
  dropExam(ui, 'Wed');
  ui.$('minChangeBtn').click();
  await settle();
  const status = ui.$('etStatus');
  assert.ok(status.classList.contains('alert-success'), 'A fully legal result is a success');
  assert.match(status.textContent, /CS201/);
  assert.match(status.textContent, language === 'ar' ? /أقل عدد ممكن/ : /the fewest possible/);
  // Times sit inside LTR isolates: unisolated, Arabic paints 08:00-10:00 as 10:00-08:00.
  const times = [...status.querySelectorAll('bdi[dir="ltr"]')].map(node => node.textContent);
  assert.ok(times.some(text => text.includes('08:00-10:00')), 'The origin time must be isolated');
  assert.ok(times.some(text => text.includes('10:30-12:30')), 'The destination time must be isolated');
});

test('a repair that cannot clear the board says what is left and why, and does not claim success', async t => {
  let ui;
  ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? repairedRun(ui, { moves: [], unseated: ['CS301'], violations_before: 2, violations_after: 1, proven_minimal: true, status: 'OPTIMAL' })
      : undefined,
  });
  dropExam(ui, 'Wed');
  ui.$('minChangeBtn').click();
  await settle();
  const status = ui.$('etStatus');
  assert.ok(status.classList.contains('alert-warning'), 'A board left with a clash is not a success');
  assert.equal(status.classList.contains('alert-success'), false);
  assert.match(status.textContent, language === 'ar' ? /الفائض/ : /Overflow/);
  assert.match(status.textContent, language === 'ar' ? /ثبّتها أو نقلتها/ : /you pinned or moved/);
});

test('a repair the solver could not prove minimal does not claim to be minimal', async t => {
  let ui;
  ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? repairedRun(ui, {
          moves: [{ course_code: 'CS201', course_name: '', from: { day: 'Mon', period: '08:00-10:00' }, to: { day: 'Tue', period: '08:00-10:00' } }],
          unseated: [], violations_before: 1, violations_after: 0, proven_minimal: false, status: 'FEASIBLE',
        })
      : undefined,
  });
  dropExam(ui, 'Wed');
  ui.$('minChangeBtn').click();
  await settle();
  assert.match(ui.$('etStatus').textContent, /CS201/);
  assert.doesNotMatch(ui.$('etStatus').textContent, language === 'ar' ? /أقل عدد ممكن/ : /fewest possible/);
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

test('a repair on a board with nothing wrong says so rather than reporting a move', async t => {
  let ui;
  ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? repairedRun(ui, { moves: [], unseated: [], violations_before: 0, violations_after: 0, proven_minimal: true, status: 'OPTIMAL' })
      : undefined,
  });
  dropExam(ui, 'Wed');
  ui.$('minChangeBtn').click();
  await settle();
  assert.match(ui.$('etStatus').textContent, language === 'ar' ? /لا يوجد ما يحتاج إصلاحاً/ : /Nothing to repair/);
});

test('course names from the server are escaped in the repair summary', async t => {
  let ui;
  ui = await loadedEditor(t, {
    onRequest: async url => url === '/ops/exam-timetable/build/'
      ? repairedRun(ui, {
          moves: [{ course_code: '<img src=x onerror=alert(1)>', course_name: '', from: { day: 'Mon', period: '08:00-10:00' }, to: { day: 'Tue', period: '08:00-10:00' } }],
          unseated: [], violations_before: 1, violations_after: 0, proven_minimal: true, status: 'OPTIMAL',
        })
      : undefined,
  });
  dropExam(ui, 'Wed');
  ui.$('minChangeBtn').click();
  await settle();
  assert.equal(ui.$('etStatus').querySelector('img'), null, 'Server text must never become markup');
  assert.match(ui.$('etStatus').textContent, /<img src=x/);
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
  ui.$('saveLoadedBtn').click();
  await settle();
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/28/export.xlsx');
  assert.equal(ui.scrollCalls.length, 0, 'Saving an existing draft must keep the current viewport');
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
