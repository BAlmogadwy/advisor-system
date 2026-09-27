/*
 * Linked exams on the exam page: the builder section, the alignment dialog,
 * the grouped cards and everything that moves, pins, undoes, checks, saves or
 * refuses them. Executed through tests/test_exam_frontend_interactions.py in
 * English and in Arabic (the wrapper renders the template in each); the
 * unmodified production page script runs against fake HTTP answers only.
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
const source = fs.readFileSync(path.join(__dirname, '../../static/js/page-exam-timetable.js'), 'utf8');
const sharedSource = fs.readFileSync(path.join(__dirname, '../../static/js/shared-utils.js'), 'utf8');
const reviewSource = fs.readFileSync(path.join(__dirname, '../../static/js/exam-review.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const settled = async (times = 4) => { for (let i = 0; i < times; i++) await settle(); };

const courses = [
  { course_code: 'AI212', source_course_code: 'AI212', course_name: 'Machine Learning', course_identity: 'ai212-ml', enrolled_count: 40, credit_hours: 3, programs: ['AI'] },
  { course_code: 'AI225', source_course_code: 'AI225', course_name: 'Machine Learning Foundations', course_identity: 'ai225-ml', enrolled_count: 6, credit_hours: 4, programs: ['AI2'] },
  { course_code: 'CS111 (1)', source_course_code: 'CS111', course_name: 'Fundamentals of Programming', course_identity: 'cs111-fundamentals', enrolled_count: 31, credit_hours: 3, programs: ['CS'] },
  { course_code: 'CS111 (2)', source_course_code: 'CS111', course_name: 'Programming I', course_identity: 'cs111-programming', enrolled_count: 14, credit_hours: 3, programs: ['CS2'], is_online: true },
];
const byCode = Object.fromEntries(courses.map(course => [course.course_code, course]));
const member = code => ({ course_identity: byCode[code].course_identity, course_code: code });
const PERIODS = ['08:00-10:00', '10:30-12:30', '13:00-15:00'];

// The counts core.services.linked_exams.linked_exams_qa returns, for fixtures.
function linkQa(links) {
  const codes = links.flatMap(link => link.members.map(item => courses.find(course => course.course_identity === item.course_identity)));
  return {
    links: links.length,
    courses: codes.length,
    students_in_two_linked_courses: 0,
    mixed_credit_links: links.filter(link => new Set(link.members.map(item => courses.find(course => course.course_identity === item.course_identity).credit_hours)).size > 1).length,
    online_courses: codes.filter(course => course.is_online).length,
  };
}

function savedRun({ placements = { AI212: 0, AI225: 0, 'CS111 (1)': 3, 'CS111 (2)': 7 }, links = [{ members: [member('AI212'), member('AI225')] }], qa = {} } = {}) {
  const slots = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'].flatMap(day => PERIODS.map(period => ({ day, period })));
  slots.forEach((slot, index) => { slot.index = index; });
  return {
    ok: true, run_id: 17, label: 'Linked fixture', primary_status: 'clean', status_flags: [], input_fingerprint: 'reviewed-inputs', enrollment_source: 'scraper_timetable',
    courses: courses.map(course => course.course_code), courses_count: courses.length, students_count: 91,
    schedule: courses.map(course => ({ ...course, ...slots[placements[course.course_code]], slot_index: placements[course.course_code], rooms: [] })),
    slots, enrollment_scope: { programs: ['AI', 'AI2', 'CS', 'CS2'], sections: ['F', 'M'] },
    credit_map: Object.fromEntries(courses.map(course => [course.course_code, course.credit_hours])),
    pinned: [], linked_exams: links,
    qa: { max_per_day: 2, ...(links.length ? { linked_exams: linkQa(links) } : {}), ...qa },
  };
}

// The server's Check: the board as sent, the links as sent, their counts.
function evaluated(payload, original) {
  const slots = payload.days.flatMap(day => payload.periods.map(period => ({ day, period })));
  slots.forEach((slot, index) => { slot.index = index; });
  const links = (payload.linked_exams || []).map(link => ({ members: link.members.map(({ course_identity, course_code }) => ({ course_identity, course_code })) }));
  const { linked_exams: _, ...qa } = original.qa;
  return {
    ...original, run_id: undefined, editor_revision: payload.editor_revision, input_fingerprint: 'reviewed-inputs', slots,
    schedule: payload.base_schedule.map(entry => ({ ...original.schedule.find(item => item.course_identity === entry.course_identity), ...entry,
      slot_index: slots.find(slot => slot.day === entry.day && slot.period === entry.period)?.index ?? slots.length, rooms: [] })),
    pinned: payload.pinned, courses_count: payload.base_schedule.length, linked_exams: links,
    qa: { ...qa, ...(links.length ? { linked_exams: { ...linkQa(links), ...(original.qa.linked_exams ? { students_in_two_linked_courses: original.qa.linked_exams.students_in_two_linked_courses } : {}) } } : {}) },
  };
}

const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

async function page(t, { run = null, onRequest = null, loadCourses = true, liveUpdate = false, catalog = courses } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(template, { url: 'http://exam.test/exam-timetable/', runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  window.localStorage.setItem('exam-timetable-live-update', liveUpdate ? 'on' : 'off');
  t.after(() => { dom.window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  const requests = [];
  const notifications = [];
  window.notify = { error: (...args) => notifications.push(args), success() {} };
  window.dlg = { confirm: async () => false, prompt: async () => false };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => false });
  Object.defineProperty(window.document, 'visibilityState', { configurable: true, get: () => 'visible' });
  window.fetch = async (url, options = {}) => {
    requests.push({ url, ...options });
    if (onRequest) {
      const answer = await onRequest(url, options);
      if (answer !== undefined) return answer;
    }
    if (url === '/ops/exam-timetable/filters/') return reply({ ok: true, programs: ['AI', 'AI2', 'CS', 'CS2'], sections: ['F', 'M'] });
    if (url.startsWith('/ops/exam-timetable/list/')) return reply({ ok: true, runs: run ? [{ id: run.run_id, label: run.label }] : [] });
    if (run && url === `/ops/exam-timetable/${run.run_id}/`) return reply(run);
    if (url === '/ops/exam-timetable/preview-courses/') return reply({ ok: true, courses: catalog });
    if (url === '/ops/exam-timetable/draft-impact/') return reply(evaluated(JSON.parse(options.body), run || savedRun()));
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
  input($('etLabel'), 'Linked regression');
  if (loadCourses) {
    $('loadCoursesBtn').click();
    await settled();
  }
  return { window, $, emit, input, select, requests, notifications };
}

async function loaded(t, options = {}) {
  const run = options.run || savedRun();
  const ui = await page(t, { ...options, run, loadCourses: false });
  ui.$('historyList').querySelector('.et-run-info').click();
  await settled();
  return ui;
}

const isShown = element => !element.hidden && !element.closest('[hidden], .d-none');
const bodies = (ui, url) => ui.requests.filter(request => request.url === url).map(request => JSON.parse(request.body));
const linkOptions = ui => Array.from(ui.$('examLinkCourseResults').querySelectorAll('[role="option"]'), option => option.dataset.value);
// As a registrar opens it: focus it (or click it when it already has focus).
const openLinkPicker = (ui, query = '') => {
  ui.$('examLinkCourseSearch').focus();
  ui.$('examLinkCourseSearch').click();
  if (query) ui.input(ui.$('examLinkCourseSearch'), query);
  return linkOptions(ui);
};
const addToLink = (ui, code) => {
  openLinkPicker(ui, code);
  const option = Array.from(ui.$('examLinkCourseResults').querySelectorAll('[role="option"]')).find(item => item.dataset.value === code);
  assert.ok(option, `No option for ${code}`);
  option.click();
};
const link = (ui, ...codes) => {
  for (const code of codes) addToLink(ui, code);
  ui.$('applyExamLink').click();
};
const linkRows = ui => Array.from(ui.$('examLinkRows').querySelectorAll('tr'));
const rowCodes = row => Array.from(row.querySelectorAll('.et-link-code'), node => node.textContent);
const card = (ui, code) => Array.from(ui.$('schedGrid').querySelectorAll('.et-course')).find(item => item.dataset.course === code);
const placement = (ui, code) => {
  const cell = card(ui, code).closest('td');
  return cell.dataset.day ? [cell.dataset.day, cell.dataset.period] : ['OVERFLOW', ''];
};
const drop = (ui, code, day, period) => {
  const cell = Array.from(ui.$('schedGrid').querySelectorAll('td[data-day]')).find(item => item.dataset.day === day && item.dataset.period === period);
  const event = new ui.window.Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: { getData: () => code } });
  cell.dispatchEvent(event);
};
const pinnedIdentities = ui => Array.from(ui.$('examPinRows').querySelectorAll('tr'), row => row.dataset.pinIdentity).sort();

test('the Linked exams section links selected courses, and Build carries the links by identity', async t => {
  const ui = await page(t);
  assert.ok(isShown(ui.$('examLinkEditor')));
  assert.match(ui.$('examLinkHeading').textContent, AR ? /اختبارات مرتبطة/ : /Linked exams/);
  assert.match(ui.$('examLinkNotice').textContent, AR ? /لا توجد اختبارات مرتبطة/ : /No linked exams/);
  assert.equal(ui.$('applyExamLink').disabled, true);
  // Only courses selected for this timetable are offered.
  const online = Array.from(ui.$('courseList').querySelectorAll('input')).find(box => box.value === 'CS111 (2)');
  online.checked = false;
  ui.emit(online, 'change');
  assert.deepEqual(openLinkPicker(ui), ['AI212', 'AI225', 'CS111 (1)']);
  online.checked = true;
  ui.emit(online, 'change');
  assert.deepEqual(openLinkPicker(ui), ['AI212', 'AI225', 'CS111 (1)', 'CS111 (2)']);

  addToLink(ui, 'AI212');
  assert.match(ui.$('examLinkNotice').textContent, AR ? /أضف مقرراً آخر/ : /Add at least one more course/);
  assert.equal(ui.$('applyExamLink').disabled, true);
  // Keyboard: type, then Enter on the highlighted match.
  ui.$('examLinkCourseSearch').focus();
  ui.input(ui.$('examLinkCourseSearch'), 'ai225');
  const enter = new ui.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  ui.$('examLinkCourseSearch').dispatchEvent(enter);
  assert.equal(enter.defaultPrevented, true);
  const chips = Array.from(ui.$('examLinkPending').querySelectorAll('.et-link-chip'), chip => chip.querySelector('bdi[dir="ltr"]').textContent);
  assert.deepEqual(chips, ['AI212', 'AI225']);
  assert.ok(!openLinkPicker(ui).includes('AI212'), 'A chosen course is not offered twice');
  assert.equal(ui.$('applyExamLink').disabled, false);
  assert.equal(ui.$('applyExamLink').textContent, AR ? 'ربط مقررين' : 'Link 2 courses');

  ui.$('applyExamLink').click();
  assert.equal(linkRows(ui).length, 1);
  assert.deepEqual(rowCodes(linkRows(ui)[0]), ['AI212', 'AI225']);
  assert.ok(linkRows(ui)[0].querySelector('bdi[dir="ltr"].et-link-code'), 'Codes are isolated left-to-right');
  assert.equal(ui.$('examLinkCount').textContent, '(1)');
  assert.match(ui.$('examLinkNotice').textContent, AR ? /تم ربط AI212، AI225/ : /Linked AI212, AI225/);
  assert.match(ui.$('examLinkRows').textContent, AR ? /يُحدد عند البناء/ : /Chosen when you build/);
  assert.equal(ui.$('examLinkPending').hidden, true);
  assert.deepEqual(openLinkPicker(ui), ['CS111 (1)', 'CS111 (2)'], 'A linked course is in one link only');
  assert.match(ui.$('examBuildSummary').textContent, AR ? /· 1 مرتبط$/ : /· 1 linked$/);
  // With every course linked or chosen, the search stays usable and says so.
  addToLink(ui, 'CS111 (1)');
  addToLink(ui, 'CS111 (2)');
  assert.equal(ui.$('examLinkCourseSearch').disabled, false);
  assert.equal(ui.window.document.activeElement, ui.$('examLinkCourseSearch'));
  assert.deepEqual(openLinkPicker(ui), []);
  assert.equal(ui.$('examLinkCourseEmpty').hidden, false);
  for (const identity of ['cs111-fundamentals', 'cs111-programming']) ui.$('examLinkPending').querySelector(`[data-link-pending-remove="${identity}"]`).click();

  ui.$('buildBtn').click();
  await settled();
  assert.deepEqual(bodies(ui, '/ops/exam-timetable/build/').at(-1).linked_exams, [{ members: [member('AI212'), member('AI225')] }]);

  linkRows(ui)[0].querySelector('[data-link-remove]').click();
  assert.equal(linkRows(ui).length, 0);
  assert.match(ui.$('examLinkNotice').textContent, AR ? /أُلغي ربط AI212، AI225/ : /Unlinked AI212, AI225/);
  ui.$('buildBtn').click();
  await settled();
  assert.deepEqual(bodies(ui, '/ops/exam-timetable/build/').at(-1).linked_exams, [], 'No links is sent as [], never left out');
});

test('a chosen course can be taken back, and a deselected linked course blocks Build beside its link', async t => {
  const ui = await page(t);
  addToLink(ui, 'CS111 (1)');
  addToLink(ui, 'AI225');
  const remove = ui.$('examLinkPending').querySelector('[data-link-pending-remove="cs111-fundamentals"]');
  assert.match(remove.getAttribute('aria-label'), /CS111 \(1\)/);
  remove.click();
  assert.deepEqual(Array.from(ui.$('examLinkPending').querySelectorAll('bdi'), node => node.textContent), ['AI225']);
  assert.equal(ui.window.document.activeElement.dataset.linkPendingRemove, 'ai225-ml', 'Focus stays among the chosen courses');
  addToLink(ui, 'AI212');
  ui.$('applyExamLink').click();
  const box = Array.from(ui.$('courseList').querySelectorAll('input')).find(item => item.value === 'AI225');
  box.checked = false;
  ui.emit(box, 'change');
  const row = linkRows(ui)[0];
  assert.ok(row.classList.contains('et-link-invalid'));
  assert.match(row.querySelector('.et-link-problem').textContent, AR ? /أعد تحديد المقررات المرتبطة/ : /Reselect the linked courses/);
  ui.$('buildBtn').click();
  await settled();
  assert.equal(bodies(ui, '/ops/exam-timetable/build/').length, 0, 'Nothing is sent while a link cannot be honoured');
  assert.equal(ui.window.document.activeElement, row.querySelector('[data-link-remove]'));
  assert.match(ui.$('etStatus').textContent, AR ? /راجع الاختبارات المرتبطة/ : /Resolve the highlighted linked exams/);
  box.checked = true;
  ui.emit(box, 'change');
  assert.ok(!linkRows(ui)[0].classList.contains('et-link-invalid'));
});

test('a loaded run restores its links before its saved signature: an unedited Check changes nothing', async t => {
  const ui = await loaded(t);
  assert.equal(linkRows(ui).length, 1);
  assert.deepEqual(rowCodes(linkRows(ui)[0]), ['AI212', 'AI225']);
  assert.equal(ui.$('saveLoadedBtn').disabled, true);
  ui.$('checkDraftBtn').click();
  await settled();
  const [check] = bodies(ui, '/ops/exam-timetable/draft-impact/');
  assert.deepEqual(check.linked_exams, [{ members: [member('AI212'), member('AI225')] }]);
  assert.match(ui.$('examCheckStatus').textContent, AR ? /تم التحقق · محفوظ/ : /Checked · Saved/);
  assert.equal(ui.$('saveLoadedBtn').disabled, true, 'The check found nothing unsaved');
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx');
  assert.equal(ui.$('examChangesSummary').textContent, AR ? 'مراجعة التغييرات (0)' : 'Review changes (0)');
  assert.match(ui.$('examChangesContent').textContent, AR ? /روابطها/ : /linked exams match/);
});

test('linked cards are drawn as one group, each naming its partner as an isolated code', async t => {
  const ui = await loaded(t);
  const group = ui.$('schedGrid').querySelector('.et-link-group');
  assert.ok(group);
  assert.deepEqual(Array.from(group.querySelectorAll('.et-course'), item => item.dataset.course), ['AI212', 'AI225']);
  assert.equal(group.getAttribute('role'), 'group');
  assert.equal(group.getAttribute('aria-label'), AR ? 'اختبار مرتبط: AI212، AI225' : 'Linked exam: AI212, AI225');
  assert.deepEqual([group.closest('td').dataset.day, group.closest('td').dataset.period], ['Sun', '08:00-10:00']);
  const label = card(ui, 'AI212').querySelector('.et-link-label');
  assert.equal(label.textContent.trim(), AR ? 'مع AI225' : 'with AI225');
  assert.equal(label.querySelector('bdi').getAttribute('dir'), 'ltr');
  assert.equal(card(ui, 'AI225').querySelector('.et-link-label bdi').textContent, 'AI212');
  assert.equal(card(ui, 'CS111 (1)').querySelector('.et-link-label'), null);
  assert.equal(card(ui, 'CS111 (1)').closest('.et-link-group'), null);
  assert.match(card(ui, 'AI212').querySelector('[data-exam-pin]').getAttribute('aria-label'), AR ? /^تثبيت الاختبارات المرتبطة: AI212/ : /^Pin linked exams: AI212/);
  assert.match(card(ui, 'AI212').querySelector('[data-exam-move]').getAttribute('aria-label'), AR ? /^نقل الاختبارات المرتبطة: AI212/ : /^Move linked exams: AI212/);
  assert.match(card(ui, 'CS111 (1)').querySelector('[data-exam-move]').getAttribute('aria-label'), AR ? /^نقل: CS111/ : /^Move: CS111/);
  assert.equal(ui.$('linkBar').classList.contains('d-none'), false);
  assert.equal(ui.$('linkCount').textContent, AR ? 'اختبار مرتبط واحد' : '1 linked exam');
});

test('drag and the Move dialog move a link as one, and one Undo puts it back', async t => {
  const ui = await loaded(t);
  drop(ui, 'AI225', 'Mon', '10:30-12:30');
  assert.deepEqual(placement(ui, 'AI212'), ['Mon', '10:30-12:30']);
  assert.deepEqual(placement(ui, 'AI225'), ['Mon', '10:30-12:30']);
  ui.$('undoExamBtn').click();
  assert.deepEqual(placement(ui, 'AI212'), ['Sun', '08:00-10:00']);
  assert.deepEqual(placement(ui, 'AI225'), ['Sun', '08:00-10:00']);
  assert.equal(ui.$('undoExamBtn').disabled, true, 'The whole move was one step');
  ui.$('redoExamBtn').click();
  assert.deepEqual(placement(ui, 'AI212'), ['Mon', '10:30-12:30']);
  ui.$('undoExamBtn').click();

  card(ui, 'AI212').querySelector('[data-exam-move]').click();
  assert.equal(ui.$('examMoveTitle').textContent, AR ? 'نقل الاختبارات المرتبطة: AI212، AI225' : 'Move linked exams: AI212, AI225');
  assert.match(ui.$('examMoveHelp').textContent, AR ? /تُنقل المقررات المرتبطة معاً/ : /Linked courses move together/);
  ui.select('examMoveDay', 'Wed');
  ui.select('examMovePeriod', '13:00-15:00');
  ui.$('confirmExamMove').click();
  assert.deepEqual(placement(ui, 'AI212'), ['Wed', '13:00-15:00']);
  assert.deepEqual(placement(ui, 'AI225'), ['Wed', '13:00-15:00']);
  // An unlinked exam still moves alone, under its own title and help.
  card(ui, 'CS111 (1)').querySelector('[data-exam-move]').click();
  assert.match(ui.$('examMoveTitle').textContent, AR ? /^نقل الاختبار: CS111 \(1\)/ : /^Move exam: CS111 \(1\)/);
  assert.match(ui.$('examMoveHelp').textContent, AR ? /اختر اليوم والفترة/ : /Choose the destination day and period/);
  ui.select('examMoveDay', 'Thu');
  ui.select('examMovePeriod', '08:00-10:00');
  ui.$('confirmExamMove').click();
  assert.deepEqual(placement(ui, 'CS111 (1)'), ['Thu', '08:00-10:00']);
  assert.deepEqual(placement(ui, 'AI212'), ['Wed', '13:00-15:00']);
  const save = bodies(ui, '/ops/exam-timetable/draft-impact/');
  assert.equal(save.length, 0, 'Live update is off: nothing was checked yet');
});

test('the pin toggle and the pin editor pin and unpin the whole link', async t => {
  const ui = await loaded(t);
  card(ui, 'AI225').querySelector('[data-exam-pin]').click();
  assert.deepEqual(pinnedIdentities(ui), ['ai212-ml', 'ai225-ml']);
  for (const code of ['AI212', 'AI225']) {
    assert.ok(card(ui, code).classList.contains('et-pinned'));
    assert.equal(card(ui, code).getAttribute('draggable'), 'false');
    assert.equal(card(ui, code).querySelector('[data-exam-move]').disabled, true);
  }
  const dragstart = new ui.window.Event('dragstart', { bubbles: true, cancelable: true });
  card(ui, 'AI212').dispatchEvent(dragstart);
  assert.equal(dragstart.defaultPrevented, true, 'A pinned link cannot be dragged by either card');
  drop(ui, 'AI212', 'Mon', '08:00-10:00');
  assert.deepEqual(placement(ui, 'AI212'), ['Sun', '08:00-10:00']);
  card(ui, 'AI212').querySelector('[data-exam-pin]').click();
  assert.deepEqual(pinnedIdentities(ui), []);

  ui.$('examSetupDetails').open = true;
  ui.select('examPinCourse', 'AI225');
  ui.select('examPinDay', 'Wed');
  ui.select('examPinPeriod', '13:00-15:00');
  ui.$('applyExamPin').click();
  assert.deepEqual(pinnedIdentities(ui), ['ai212-ml', 'ai225-ml']);
  assert.deepEqual(placement(ui, 'AI212'), ['Wed', '13:00-15:00']);
  assert.match(linkRows(ui)[0].querySelector('.et-link-time').textContent, AR ? /Wed · 13:00-15:00.*موعد ثابت/ : /Wed · 13:00-15:00.*Fixed time/);
  ui.select('examPinCourse', 'AI212');
  assert.equal(ui.$('applyExamPin').textContent, AR ? 'تحديث التثبيت' : 'Update pin', 'A linked course shares its link\'s pin');
  ui.$('examPinRows').querySelector('[data-pin-remove="ai212-ml"]').click();
  assert.deepEqual(pinnedIdentities(ui), [], 'Unpinning one member unpins its link');
  ui.$('undoExamBtn').click();
  assert.deepEqual(pinnedIdentities(ui), ['ai212-ml', 'ai225-ml']);
});

test('a link pinned through one member is pinned whole: neither card drags, and one click frees it', async t => {
  const run = savedRun();
  run.pinned = [{ course_code: 'AI225', day: 'Sun', period: '08:00-10:00' }];
  const ui = await loaded(t, { run });
  for (const code of ['AI212', 'AI225']) {
    assert.ok(card(ui, code).classList.contains('et-pinned'), code);
    assert.equal(card(ui, code).getAttribute('draggable'), 'false', code);
    assert.equal(card(ui, code).querySelector('[data-exam-pin]').getAttribute('aria-pressed'), 'true', code);
    assert.equal(card(ui, code).querySelector('[data-exam-move]').disabled, true, code);
  }
  assert.match(linkRows(ui)[0].querySelector('.et-link-time').textContent, AR ? /موعد ثابت/ : /Fixed time/);
  drop(ui, 'AI212', 'Mon', '08:00-10:00');
  assert.deepEqual(placement(ui, 'AI212'), ['Sun', '08:00-10:00']);
  assert.match(ui.$('etStatus').textContent, AR ? /ألغِ تثبيت الاختبار/ : /Unpin to move/);
  card(ui, 'AI212').querySelector('[data-exam-pin]').click();
  assert.deepEqual(pinnedIdentities(ui), []);
  assert.equal(card(ui, 'AI212').getAttribute('draggable'), 'true');
});

test('linking courses that sit apart asks for the one time they share; Undo takes the link and the move back', async t => {
  // The larger course (CS111 (1), 31 students) sits later in the week.
  const ui = await loaded(t, { run: savedRun({ placements: { AI212: 0, AI225: 0, 'CS111 (1)': 14, 'CS111 (2)': 7 } }) });
  ui.$('examSetupDetails').open = true;
  link(ui, 'CS111 (1)', 'CS111 (2)');
  const dialog = ui.$('examLinkDialog');
  assert.ok(dialog.open || dialog.hasAttribute('open'));
  const radios = Array.from(ui.$('examLinkOptions').querySelectorAll('input[type="radio"]'));
  assert.equal(radios.length, 2);
  assert.deepEqual(radios.map(radio => radio.closest('label').querySelector('bdi').textContent), ['Tue · 10:30-12:30', 'Thu · 13:00-15:00'], 'In timetable order');
  assert.deepEqual(radios.map(radio => radio.checked), [false, true], 'The larger course keeps its time by default');
  assert.equal(ui.window.document.activeElement, radios[1]);
  assert.match(radios[0].closest('label').textContent, AR ? /الموعد الحالي لـ/ : /where CS111 \(2\) sits now/);
  assert.equal(radios[0].closest('label').querySelector('small bdi').textContent, 'CS111 (2)');
  assert.match(ui.$('examLinkDialogHelp').textContent, AR ? /مواعيد مختلفة/ : /different times/);
  assert.deepEqual(Array.from(ui.$('examLinkDialogMembers').querySelectorAll('li'), item => item.textContent), ['CS111 (1)Thu · 13:00-15:00', 'CS111 (2)Tue · 10:30-12:30']);
  assert.equal(linkRows(ui).length, 1, 'Nothing is linked before a time is chosen');

  ui.$('cancelExamLink').click();
  assert.ok(!(dialog.open || dialog.hasAttribute('open')));
  assert.equal(ui.window.document.activeElement, ui.$('applyExamLink'));
  assert.deepEqual(placement(ui, 'CS111 (1)'), ['Thu', '13:00-15:00']);
  assert.equal(ui.$('undoExamBtn').disabled, true, 'A cancelled link leaves no Undo step');

  ui.$('applyExamLink').click();
  ui.$('examLinkOptions').querySelectorAll('input')[0].click();
  ui.$('confirmExamLink').click();
  assert.ok(!(dialog.open || dialog.hasAttribute('open')));
  assert.deepEqual(placement(ui, 'CS111 (1)'), ['Tue', '10:30-12:30']);
  assert.deepEqual(placement(ui, 'CS111 (2)'), ['Tue', '10:30-12:30']);
  assert.equal(linkRows(ui).length, 2);
  assert.ok(card(ui, 'CS111 (1)').closest('.et-link-group'));
  ui.$('undoExamBtn').click();
  assert.equal(linkRows(ui).length, 1);
  assert.deepEqual(placement(ui, 'CS111 (1)'), ['Thu', '13:00-15:00']);
  assert.equal(card(ui, 'CS111 (1)').closest('.et-link-group'), null);
  assert.equal(ui.$('undoExamBtn').disabled, true, 'Linking and moving was one step');
});

test('a pinned course\'s time is the only choice, and courses pinned apart are never linked', async t => {
  const ui = await loaded(t);
  card(ui, 'CS111 (2)').querySelector('[data-exam-pin]').click();
  ui.$('examSetupDetails').open = true;
  link(ui, 'CS111 (1)', 'CS111 (2)');
  const radios = Array.from(ui.$('examLinkOptions').querySelectorAll('input[type="radio"]'));
  assert.equal(radios.length, 1);
  assert.equal(radios[0].checked, true);
  assert.equal(radios[0].closest('label').querySelector('bdi').textContent, 'Tue · 10:30-12:30');
  assert.match(ui.$('examLinkDialogHelp').textContent, AR ? /CS111 \(2\) مثبّت/ : /CS111 \(2\) is pinned/);
  ui.$('confirmExamLink').click();
  assert.deepEqual(placement(ui, 'CS111 (1)'), ['Tue', '10:30-12:30']);
  assert.ok(pinnedIdentities(ui).includes('cs111-fundamentals'), 'The link is pinned as its pinned member was');
  ui.$('undoExamBtn').click();
  assert.deepEqual(pinnedIdentities(ui), ['cs111-programming']);

  card(ui, 'CS111 (1)').querySelector('[data-exam-pin]').click();
  link(ui, 'CS111 (1)', 'CS111 (2)');
  const dialog = ui.$('examLinkDialog');
  assert.ok(!(dialog.open || dialog.hasAttribute('open')));
  assert.equal(linkRows(ui).length, 1);
  assert.match(ui.$('examLinkNotice').textContent, AR ? /مثبتة في مواعيد مختلفة/ : /are pinned to different times/);
  assert.ok(ui.$('examLinkNotice').classList.contains('text-danger'));
});

test('a split link from a saved run is flagged and blocks Check until a move of either member mends it', async t => {
  const ui = await loaded(t, { run: savedRun({ placements: { AI212: 0, AI225: 4, 'CS111 (1)': 3, 'CS111 (2)': 7 } }) });
  const row = linkRows(ui)[0];
  assert.ok(row.classList.contains('et-link-invalid'));
  assert.match(row.querySelector('.et-link-problem').textContent, AR ? /تُعقد المقررات المرتبطة في مواعيد مختلفة/ : /The linked courses sit at different times/);
  ui.$('checkDraftBtn').click();
  await settled();
  assert.equal(bodies(ui, '/ops/exam-timetable/draft-impact/').length, 0, 'A split link is never sent');
  drop(ui, 'AI212', 'Mon', '10:30-12:30');
  assert.deepEqual(placement(ui, 'AI212'), ['Mon', '10:30-12:30']);
  assert.deepEqual(placement(ui, 'AI225'), ['Mon', '10:30-12:30']);
  assert.ok(!linkRows(ui)[0].classList.contains('et-link-invalid'));
  assert.ok(card(ui, 'AI212').closest('.et-link-group'));
  ui.$('checkDraftBtn').click();
  await settled();
  assert.deepEqual(bodies(ui, '/ops/exam-timetable/draft-impact/').at(-1).linked_exams, [{ members: [member('AI212'), member('AI225')] }]);
});

test('a link edit needs a Check before Save, and Review changes lists it in its own group', async t => {
  const ui = await loaded(t);
  ui.$('examSetupDetails').open = true;
  linkRows(ui)[0].querySelector('[data-link-remove]').click();
  assert.match(ui.$('examCheckStatus').textContent, AR ? /تغييرات لم يتم التحقق منها/ : /Changes not checked/);
  assert.equal(ui.$('saveLoadedBtn').disabled, false);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  assert.equal(ui.$('examChangesSummary').textContent, AR ? 'مراجعة التغييرات (1)' : 'Review changes (1)');
  assert.match(ui.$('examChangeCount').textContent, AR ? /1 تغيير ربط/ : /1 link changes/);
  const group = ui.$('examChangesContent').querySelector('[data-change-group="links"]');
  assert.ok(group);
  assert.match(group.querySelector('h4').textContent, AR ? /الاختبارات المرتبطة/ : /Linked exams/);
  assert.deepEqual(Array.from(group.querySelectorAll('.et-change-course bdi'), node => node.textContent), ['AI212', 'AI225']);
  assert.match(group.querySelector('.et-change-times').textContent, AR ? /المحفوظ: مرتبطة.*الحالي: غير مرتبطة/ : /Saved: Linked.*Current: Not linked/);
  // No card on the board is grouped once unlinked.
  assert.equal(ui.$('schedGrid').querySelector('.et-link-group'), null);

  ui.$('saveLoadedBtn').click();
  await settled(8);
  const [check] = bodies(ui, '/ops/exam-timetable/draft-impact/');
  assert.deepEqual(check.linked_exams, [], 'Save checked the unlinked board first');
  const save = bodies(ui, '/ops/exam-timetable/build/').at(-1);
  assert.equal(save.mode, 'save_loaded_changes');
  assert.deepEqual(save.linked_exams, []);
});

test('Optimise and Fix carry the links, and Fix says it cannot separate linked courses', async t => {
  const ui = await loaded(t, { onRequest: async (url, options) => {
    if (url !== '/ops/exam-timetable/build/') return undefined;
    const body = JSON.parse(options.body);
    if (body.mode === 'minimum_change_repair') {
      return reply({ ok: true, saved: false, minimum_change: { status: 'OPTIMAL', moves: [], unseated: [], violations_before: 0, violations_after: 0, linked_clash_students: 2 } });
    }
    return undefined;
  } });
  ui.$('minChangeBtn').click();
  await settled(8);
  const fix = bodies(ui, '/ops/exam-timetable/build/').at(-1);
  assert.equal(fix.mode, 'minimum_change_repair');
  assert.deepEqual(fix.linked_exams, [{ members: [member('AI212'), member('AI225')] }]);
  const report = ui.$('examRepairReport');
  assert.match(report.textContent, AR ? /طالبان مسجلان في مقررين مرتبطين/ : /2 students are registered in two linked courses/);
  assert.doesNotMatch(report.textContent, AR ? /لا يوجد ما يحتاج إصلاحاً/ : /Nothing to repair/);
  assert.ok(report.classList.contains('is-partial'));
  ui.$('optimizeLoadedBtn').click();
  await settled(8);
  const optimise = bodies(ui, '/ops/exam-timetable/build/').at(-1);
  assert.equal(optimise.mode, 'optimize_loaded');
  assert.deepEqual(optimise.linked_exams, [{ members: [member('AI212'), member('AI225')] }]);
});

test('a refused Build is shown beside the link and course it names, never as a raw page', async t => {
  const ui = await page(t, { onRequest: async url => (url === '/ops/exam-timetable/build/'
    ? reply({ ok: false, code: 'linked_exams_course_not_selected', field: 'linked_exams[1].members[1].course_identity', error: 'Linked course X is not selected for this timetable.' }, 400)
    : undefined) });
  link(ui, 'AI212', 'AI225');
  link(ui, 'CS111 (1)', 'CS111 (2)');
  ui.$('buildBtn').click();
  await settled();
  const [first, second] = linkRows(ui);
  assert.ok(!first.classList.contains('et-link-invalid'));
  assert.ok(second.classList.contains('et-link-invalid'));
  assert.match(second.querySelector('.et-link-problem').textContent, AR ? /مقرر مرتبط غير محدد لهذا الجدول/ : /A linked course is not selected for this timetable/);
  const members = Array.from(second.querySelectorAll('.et-link-member'));
  assert.deepEqual(members.map(item => item.classList.contains('is-invalid')), [false, true]);
  assert.match(ui.$('etStatus').textContent, AR ? /الاختبار المرتبط CS111 \(1\) \+ CS111 \(2\)/ : /Linked exam CS111 \(1\) \+ CS111 \(2\): A linked course is not selected/);
  assert.doesNotMatch(ui.$('etStatus').textContent, /Linked course X|linked_exams\[/, 'The page words the refusal itself');
  const banner = ui.$('examRequestError');
  assert.equal(banner.hidden, false);
  const review = banner.querySelector('button');
  assert.equal(review.textContent, AR ? 'مراجعة الاختبارات المرتبطة' : 'Review linked exams');
  review.click();
  assert.equal(ui.window.document.activeElement, second.querySelector('[data-link-remove]'));
  second.querySelector('[data-link-remove]').click();
  assert.equal(ui.$('examLinkRows').querySelector('.et-link-invalid'), null, 'The refusal goes with the links it judged');
});

test('a refused Check names the split link in the builder and at the board', async t => {
  const ui = await loaded(t, { onRequest: async url => (url === '/ops/exam-timetable/draft-impact/'
    ? reply({ ok: false, code: 'linked_exams_split', field: 'linked_exams[0]', error: 'Linked courses AI212, AI225 must sit at the same day and period.' }, 400)
    : undefined) });
  drop(ui, 'CS111 (1)', 'Thu', '08:00-10:00');
  ui.$('checkDraftBtn').click();
  await settled();
  const banner = ui.$('examEditorRequestError');
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, AR ? /يجب أن تُعقد المقررات المرتبطة في اليوم والفترة نفسيهما/ : /Linked exam AI212 \+ AI225: Linked courses must sit at the same day and period/);
  assert.match(ui.$('examCheckStatus').textContent, AR ? /تعذر التحقق/ : /Check failed/);
  assert.ok(linkRows(ui)[0].classList.contains('et-link-invalid'));
  assert.match(linkRows(ui)[0].querySelector('.et-link-problem').textContent, AR ? /انقلها معاً ثم تحقق مجدداً/ : /Move them together, then check again/);
  assert.equal(banner.querySelector('button').textContent, AR ? 'مراجعة الاختبارات المرتبطة' : 'Review linked exams');
});

test('a refusal that names no link is said in the section itself', async t => {
  const ui = await page(t, { onRequest: async url => (url === '/ops/exam-timetable/build/'
    ? reply({ ok: false, code: 'linked_exams_pins_disagree', field: 'pinned', error: 'Linked courses AI212, AI225 are pinned to different times.' }, 400)
    : undefined) });
  link(ui, 'AI212', 'AI225');
  ui.$('buildBtn').click();
  await settled();
  assert.equal(ui.$('examLinkRows').querySelector('.et-link-invalid'), null);
  assert.equal(ui.$('examLinkNotice').textContent, AR
    ? 'المقررات المرتبطة مثبتة في مواعيد مختلفة. ثبّتها في موعد واحد أو ألغِ تثبيتها.'
    : 'Linked courses are pinned to different times. Pin them to one time, or unpin them.');
  assert.ok(ui.$('examLinkNotice').classList.contains('text-danger'));
});

test('warnings from the last check never block saving, and say when they are the last check\'s', async t => {
  const run = savedRun({ qa: { linked_exams: { links: 1, courses: 2, students_in_two_linked_courses: 3, mixed_credit_links: 1, online_courses: 1 } } });
  const ui = await loaded(t, { run });
  const items = Array.from(ui.$('examLinkWarningList').querySelectorAll('li'), item => item.textContent);
  assert.equal(ui.$('examLinkWarnings').hidden, false);
  assert.deepEqual(items, AR
    ? ['3 طلاب مسجلون في مقررين مرتبطين: يؤدي كلٌّ منهم ورقتين في وقت واحد (تعارض اختبار مرتبط).', 'ربط واحد يجمع مقررات بساعات معتمدة مختلفة.', 'مقرر مرتبط واحد يُدرَّس عن بُعد.']
    : ['3 students are registered in two linked courses: each sits two papers at one time (a linked-exam clash).', '1 link joins courses with different credit hours.', '1 linked course is taught online.']);
  assert.match(ui.$('linkBarWarnings').textContent, AR ? /^3 طلاب مسجلون/ : /^3 students are registered/);
  assert.equal(ui.$('exportXlsx').getAttribute('href'), '/ops/exam-timetable/17/export.xlsx', 'Warnings hold nothing back');
  drop(ui, 'CS111 (1)', 'Thu', '08:00-10:00');
  assert.match(ui.$('examLinkWarningList').textContent, AR ? /^آخر تحقق: 3 طلاب/ : /^Last checked: 3 students/);
  assert.match(ui.$('linkBarWarnings').textContent, AR ? /^آخر تحقق:/ : /^Last checked:/);
  assert.equal(ui.$('saveLoadedBtn').disabled, false);
  ui.$('checkDraftBtn').click();
  await settled();
  assert.match(ui.$('examLinkWarningList').textContent, AR ? /^3 طلاب/ : /^3 students/, 'Checked again, the warnings are current');
  assert.equal(ui.$('saveLoadedBtn').disabled, false, 'A warning never disables Save');
});

test('the conflict detail names a linked-exam clash from the server\'s own classification', async t => {
  const run = savedRun({ qa: {
    conflict_count: 2,
    same_slot_conflicts: [
      { student_id: 4401001, slot_index: 0, courses: ['AI212', 'AI225'] },
      { student_id: 4401002, slot_index: 3, courses: ['CS111 (1)', 'CS111 (2)'] },
    ],
    manual_override_details: [
      { kind: 'linked_same_slot', student_id: 4401001, slot_index: 0, courses: ['AI212', 'AI225'] },
      { kind: 'same_slot', student_id: 4401002, slot_index: 3, courses: ['CS111 (1)', 'CS111 (2)'] },
    ],
  } });
  const ui = await loaded(t, { run });
  ui.window.document.querySelector('[data-open-drill="conflicts"]').click();
  const badges = Array.from(ui.$('kpiDrillBody').querySelectorAll('tr td:first-child .badge'), badge => badge.textContent);
  assert.deepEqual(badges, AR ? ['تعارض اختبار مرتبط', 'طالب'] : ['Linked-exam clash', 'Student']);
  assert.equal(ui.$('kpiDrillNote').hidden, false);
  assert.match(ui.$('kpiDrillNote').textContent, AR ? /لا يفصل «إصلاح بأقل تغيير» المقررات المرتبطة/ : /Fix with fewest moves never separates linked courses/);
});

test('the review controller knows a linked partner: it sits with the focused exam even with no shared students', async t => {
  const run = savedRun();
  run.exam_review = { version: 1, enrollment_source: 'scraper_timetable', student_overlaps: [{ course_a: 'AI212', course_b: 'CS111 (1)', shared_students: 5 }] };
  const ui = await loaded(t, { run });
  const { relationship } = ui.window.ExamReview;
  const a = { day: 'Sun', period: '08:00-10:00' };
  assert.equal(relationship(a, { ...a }, run.slots, true), 'linked');
  assert.equal(relationship(a, { ...a }, run.slots), 'same-period');
  assert.equal(relationship(a, { day: 'OVERFLOW' }, run.slots, true), 'linked', 'Linked wins over every placement');
  card(ui, 'AI212').querySelector('[data-exam-related]').click();
  const partner = card(ui, 'AI225');
  assert.ok(partner.classList.contains('et-related-match'));
  assert.ok(!partner.classList.contains('et-review-muted'));
  assert.equal(partner.querySelector('.et-related-count').textContent, AR ? 'اختبار مرتبط، في الموعد نفسه' : 'linked exam, same time');
  assert.match(card(ui, 'CS111 (1)').querySelector('.et-related-count').textContent, AR ? /^5 مشترك/ : /^5 shared/);
  assert.ok(card(ui, 'CS111 (2)').classList.contains('et-review-muted'));
});

test('Load Courses keeps the links by identity, whatever the display codes become', async t => {
  const run = savedRun({ links: [{ members: [member('CS111 (1)'), member('AI212')] }], placements: { AI212: 3, AI225: 0, 'CS111 (1)': 3, 'CS111 (2)': 7 } });
  // The population changed: the two CS111 courses swapped their numbers.
  const catalog = courses.map(course => ({ ...course, course_code: course.course_code === 'CS111 (1)' ? 'CS111 (2)' : course.course_code === 'CS111 (2)' ? 'CS111 (1)' : course.course_code }));
  const ui = await loaded(t, { run, catalog });
  assert.deepEqual(rowCodes(linkRows(ui)[0]), ['CS111 (1)', 'AI212']);
  ui.$('loadCoursesBtn').click();
  await settled();
  assert.deepEqual(rowCodes(linkRows(ui)[0]), ['CS111 (2)', 'AI212']);
  assert.match(linkRows(ui)[0].textContent, /Fundamentals of Programming/);
  assert.match(ui.$('examLinkRows').textContent, AR ? /يُحدد عند البناء/ : /Chosen when you build/);
  ui.$('buildBtn').click();
  await settled();
  assert.deepEqual(bodies(ui, '/ops/exam-timetable/build/').at(-1).linked_exams, [{ members: [
    { course_identity: 'cs111-fundamentals', course_code: 'CS111 (2)' }, member('AI212'),
  ] }]);
});
