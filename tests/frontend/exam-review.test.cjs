/* Real Django EN/AR template and complete production scripts, with explicit HTTP fixtures. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');

assert.ok(process.env.EXAM_TEST_HTML, 'Run through pytest tests/test_exam_frontend_interactions.py');
const template = fs.readFileSync(process.env.EXAM_TEST_HTML, 'utf8');
const language = process.env.EXAM_TEST_LANGUAGE;
const script = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const sharedSource = script('shared-utils.js');
const reviewSource = script('exam-review.js');
const pageSource = script('page-exam-timetable.js');
const settle = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));
const reply = data => ({ ok: true, status: 200, json: async () => clone(data) });
const periods = ['08:00-10:00', '10:30-12:30'];
const courses = [
  { course_code: 'CS111 (1)', source_course_code: 'CS111', course_name: 'Fundamentals of Programming', course_identity: 'cs111-fundamentals', enrolled_count: 20, programs: ['AI', 'DS'] },
  { course_code: 'CS111 (2)', source_course_code: 'CS111', course_name: 'Programming I', course_identity: 'cs111-programming', enrolled_count: 12, programs: ['CS'], is_online: true },
  { course_code: 'DS113', source_course_code: 'DS113', course_name: 'Data Science', course_identity: 'ds113-data', enrolled_count: 15, programs: ['AI'] },
  { course_code: 'CYB215', source_course_code: 'CYB215', course_name: 'Security Tools', course_identity: 'cyb215-security', enrolled_count: 17, programs: ['AI'] },
  { course_code: 'UNI101', source_course_code: 'UNI101', course_name: 'University Skills', course_identity: 'uni101-skills', enrolled_count: 4, programs: ['AI'] },
  { course_code: 'MATH101', source_course_code: 'MATH101', course_name: 'Calculus 1', course_identity: 'math101-calculus', enrolled_count: 11, programs: ['DS'] },
];

function savedRun() {
  const slots = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'].flatMap(day => periods.map(period => ({ day, period })));
  slots.forEach((slot, index) => { slot.index = index; });
  return {
    ok: true, run_id: 81, label: 'Review regression', enrollment_source: 'scraper_timetable',
    input_fingerprint: 'reviewed-source', primary_status: 'clean_with_approved_thin_conflicts',
    status_flags: ['approved_thin_conflicts'], courses_count: courses.length, students_count: 35,
    courses: courses.map(course => course.course_code), slots, assign_rooms: false,
    schedule: courses.map((course, index) => ({ ...course, ...slots[index], slot_index: index, rooms: [] })),
    pinned: [], enrollment_scope: { programs: ['AI', 'CS', 'DS'], sections: ['F', 'M'] },
    qa: { max_per_day: 2, thin_threshold: 4, thin_courses: [{ course_code: 'UNI101', enrolled_count: 4 }], thin_clash_risk: [] },
    conflicts: [], conflicts_count: 0,
    buckets_summary: [
      { program: 'AI', programme_term: 1, courses: ['CS111 (1)', 'CYB215'], course_count: 2 },
      { program: 'AI', programme_term: 2, courses: ['DS113', 'CYB215'], course_count: 2 },
      { program: 'CS', programme_term: 4, courses: ['CS111 (2)'], course_count: 1 },
      { program: 'DS', programme_term: 3, courses: ['CS111 (1)', 'MATH101'], course_count: 2 },
    ],
    exam_review: { version: 1, enrollment_source: 'scraper_timetable', student_overlaps: [
      { course_a: 'CS111 (1)', course_b: 'CS111 (2)', shared_students: 8 },
      { course_a: 'CS111 (1)', course_b: 'DS113', shared_students: 8 },
      { course_a: 'CS111 (1)', course_b: 'UNI101', shared_students: 3 },
      { course_a: 'CS111 (2)', course_b: 'CYB215', shared_students: 2 },
    ] },
  };
}

async function page(t, { run = savedRun(), onRequest = null } = {}) {
  const errors = [], requests = [], notifications = [], dialogs = [];
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(error.message));
  const dom = new JSDOM(template, { url: 'http://exam.test/ops/exam-timetable/', runScripts: 'outside-only', virtualConsole: console });
  const { window } = dom;
  window.localStorage.setItem('exam-timetable-live-update', 'off');
  window.notify = { error: (...args) => notifications.push(args), success() {} };
  window.dlg = { confirm: async value => { dialogs.push(value); return false; } };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  t.after(() => { window.close(); assert.deepEqual(errors, []); });
  let sourceRun = clone(run);
  window.fetch = async (url, options = {}) => {
    requests.push({ url, ...options });
    if (onRequest) {
      const response = await onRequest(url, options);
      if (response !== undefined) return response;
    }
    if (url === '/ops/exam-timetable/filters/') return reply({ ok: true, programs: ['AI', 'CS', 'DS'], sections: ['F', 'M'] });
    if (url.startsWith('/ops/exam-timetable/list/')) return reply({ ok: true, runs: [{ id: sourceRun.run_id, label: sourceRun.label, courses_count: courses.length, students_count: 35 }] });
    if (url === `/ops/exam-timetable/${sourceRun.run_id}/`) return reply(sourceRun);
    if (url === '/ops/exam-timetable/preview-courses/') return reply({ ok: true, courses });
    if (url === '/ops/exam-timetable/draft-impact/' || url === '/ops/exam-timetable/build/') {
      const payload = JSON.parse(options.body);
      const data = { ...clone(sourceRun), editor_revision: payload.editor_revision, run_id: undefined,
        schedule: payload.base_schedule.map(entry => ({ ...entry,
          slot_index: sourceRun.slots.findIndex(slot => slot.day === entry.day && slot.period === entry.period), rooms: [],
          enrolled_count: sourceRun.schedule.find(original => original.course_identity === entry.course_identity)?.enrolled_count,
        })), pinned: payload.pinned };
      if (url.endsWith('/build/')) { data.run_id = 82; sourceRun = clone(data); }
      return reply(data);
    }
    throw new Error(`Unexpected HTTP request: ${url}`);
  };
  vm.runInContext(sharedSource, dom.getInternalVMContext(), { filename: 'shared-utils.js' });
  vm.runInContext(reviewSource, dom.getInternalVMContext(), { filename: 'exam-review.js' });
  let reviewController;
  const createController = window.ExamReview.createController;
  window.ExamReview = { ...window.ExamReview, createController: options => {
    reviewController = createController(options);
    return reviewController;
  } };
  vm.runInContext(`const LANGUAGE_CODE = ${JSON.stringify(language)};\n${pageSource}`, dom.getInternalVMContext(), { filename: 'page-exam-timetable.js' });
  const $ = id => window.document.getElementById(id);
  const emit = (element, type) => element.dispatchEvent(new window.Event(type, { bubbles: true }));
  const select = (id, value) => { $(id).value = value; emit($(id), 'change'); };
  const input = (id, value) => { $(id).value = value; emit($(id), 'input'); };
  const chip = code => Array.from($('schedGrid').querySelectorAll('.et-course')).find(item => item.dataset.course === code);
  const key = (element, value) => element.dispatchEvent(new window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true }));
  await settle();
  $('historyList').querySelector('.et-run-info').click();
  await settle();
  assert.equal($('schedGrid').querySelectorAll('.et-course').length, courses.length);
  return { window, $, emit, select, input, chip, key, requests, notifications, dialogs, reviewController };
}

const shown = element => element && !element.hidden && !element.closest('[hidden],.d-none');
const termButtons = ui => Array.from(ui.$('examTermLegend').querySelectorAll('[data-exam-term]'));
const posts = ui => ui.requests.filter(request => request.method === 'POST');
const placements = ui => Array.from(ui.$('schedGrid').querySelectorAll('.et-course'), chip => [
  chip.dataset.course, chip.closest('td')?.dataset.day, chip.closest('td')?.dataset.period,
]);
const editorState = ui => ({
  placements: placements(ui), pins: ui.$('examPinRows').innerHTML,
  saveDisabled: ui.$('saveLoadedBtn').disabled, undoDisabled: ui.$('undoExamBtn').disabled,
  redoDisabled: ui.$('redoExamBtn').disabled, status: ui.$('kStatusPrimary').textContent,
  exportHref: ui.$('exportXlsx').getAttribute('href'), calculationState: ui.$('etResults').dataset.calculationState,
});

const relatedButton = (ui, code) => ui.chip(code).querySelector('[data-exam-related]');
const reviewFocus = ui => ui.$('schedGrid').querySelector('.et-related-focus');
const reviewedCount = (ui, code) => ui.chip(code).querySelector('.et-related-count')?.textContent.trim();
const focusedCode = ui => reviewFocus(ui)?.dataset.course;
const move = (ui, code, day, period = periods[0]) => {
  ui.chip(code).querySelector('[data-exam-move]').click();
  ui.select('examMoveDay', day);
  ui.select('examMovePeriod', period);
  ui.$('confirmExamMove').click();
};

test('term view exposes actual study plans, numbered legend buttons and text badges', async t => {
  const ui = await page(t);
  ui.select('examReviewMode', 'term');
  assert.ok(shown(ui.$('examReviewPlanField')));
  const planValues = Array.from(ui.$('examReviewPlan').options, option => option.value);
  for (const program of ['AI', 'CS', 'DS']) assert.ok(planValues.includes(program));
  ui.select('examReviewPlan', 'AI');
  assert.ok(shown(ui.$('examTermLegend')));
  for (const term of ['1', '2']) {
    const button = termButtons(ui).find(item => item.dataset.examTerm === term);
    assert.ok(button);
    assert.equal(button.tagName, 'BUTTON');
    assert.ok(button.textContent.includes(term));
    assert.ok(button.hasAttribute('aria-pressed'));
  }
  assert.ok(ui.chip('CS111 (1)').querySelector('.et-term-badge')?.textContent.includes('1'));
  assert.ok(ui.chip('DS113').querySelector('.et-term-badge')?.textContent.includes('2'));
  assert.ok(ui.chip('CYB215').querySelector('.et-term-badge')?.textContent.trim(), 'Mixed terms need a textual label');
  assert.ok(ui.chip('UNI101').querySelector('.et-term-badge')?.textContent.trim(), 'An unmapped course needs a textual label');
  assert.notEqual(ui.chip('CS111 (1)').dataset.examTerm, ui.chip('CS111 (2)').dataset.examTerm);
});

test('term filters change only visual emphasis and retain duplicate-course identity', async t => {
  const ui = await page(t);
  const before = editorState(ui), requestCount = posts(ui).length;
  ui.select('examReviewMode', 'term');
  ui.select('examReviewPlan', 'CS');
  const fourth = termButtons(ui).find(button => button.dataset.examTerm === '4');
  assert.ok(fourth);
  fourth.click();
  assert.equal(fourth.getAttribute('aria-pressed'), 'true');
  assert.ok(!ui.chip('CS111 (2)').classList.contains('et-review-muted'));
  assert.ok(ui.chip('CS111 (1)').classList.contains('et-review-muted'));
  assert.deepEqual(editorState(ui), before);
  assert.equal(posts(ui).length, requestCount);
});

test('student review uses exact per-pair counts, including thin courses absent from conflicts', async t => {
  const ui = await page(t);
  const before = editorState(ui), requestCount = posts(ui).length;
  ui.select('examReviewMode', 'students');
  relatedButton(ui, 'CS111 (1)').click();
  assert.equal(focusedCode(ui), 'CS111 (1)');
  for (const [code, count] of [['CS111 (2)', 8], ['DS113', 8], ['UNI101', 3]]) {
    assert.ok(ui.chip(code).classList.contains('et-related-match'));
    assert.ok(reviewedCount(ui, code).includes(String(count)));
  }
  assert.ok(ui.chip('CYB215').classList.contains('et-review-muted'));
  assert.ok(!reviewedCount(ui, 'CYB215'));
  assert.ok(shown(ui.$('examRelatedSummary')));
  assert.doesNotMatch(ui.$('examRelatedSummary').textContent, /\b19\b/, 'Pair counts cannot be summed into distinct students');
  assert.deepEqual(editorState(ui), before);
  assert.equal(posts(ui).length, requestCount);
});

test('related-course activation has its own native accessible button and does not hijack Pin or Move', async t => {
  const ui = await page(t);
  ui.select('examReviewMode', 'students');
  const button = relatedButton(ui, 'CS111 (2)');
  assert.equal(button.tagName, 'BUTTON');
  assert.equal(button.type, 'button');
  assert.equal(button.dataset.examRelated, courses[1].course_identity);
  assert.ok(button.getAttribute('aria-label').includes(courses[1].course_name));
  button.focus();
  assert.equal(ui.window.document.activeElement, button);
  button.click(); // Native button keyboard activation delegates to this same click handler.
  assert.equal(focusedCode(ui), 'CS111 (2)');
  assert.equal(ui.window.document.activeElement, button, 'Review paint preserves keyboard focus and card nodes');
  ui.chip('DS113').querySelector('[data-exam-pin]').click();
  assert.equal(focusedCode(ui), 'CS111 (2)');
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 1);
  ui.chip('CYB215').querySelector('[data-exam-move]').click();
  assert.equal(focusedCode(ui), 'CS111 (2)');
  assert.ok(ui.$('examMoveDialog').hasAttribute('open'));
});

test('ordinary course-body click remains inert and double-click pins once in review mode', async t => {
  const ui = await page(t);
  ui.select('examReviewMode', 'students');
  const chip = ui.chip('CS111 (1)');
  chip.click();
  assert.equal(ui.chip('CS111 (1)'), chip, 'A first click must not replace the dblclick target');
  assert.equal(focusedCode(ui), undefined);
  chip.dispatchEvent(new ui.window.MouseEvent('dblclick', { bubbles: true }));
  assert.equal(ui.$('examPinRows').querySelectorAll('tr').length, 1);
  assert.equal(focusedCode(ui), undefined);
});

test('review emphasis composes with course search and clearing focus preserves the search', async t => {
  const ui = await page(t);
  ui.input('schedFilter', 'CS111 (2)');
  const dimmed = () => Array.from(ui.$('schedGrid').querySelectorAll('.et-course.et-dim'), item => item.dataset.course).sort();
  const before = dimmed();
  assert.ok(before.includes('CS111 (1)'));
  ui.select('examReviewMode', 'students');
  relatedButton(ui, 'CS111 (1)').click();
  assert.deepEqual(dimmed(), before);
  ui.$('examRelatedClear').click();
  assert.equal(focusedCode(ui), undefined);
  assert.equal(ui.$('schedFilter').value, 'CS111 (2)');
  assert.deepEqual(dimmed(), before);
  ui.select('examReviewMode', 'term');
  ui.select('examReviewPlan', 'AI');
  termButtons(ui).find(button => button.dataset.examTerm === '1').click();
  assert.deepEqual(dimmed(), before);
});

test('review controls are labeled and neutral mode leaves editing state unchanged', async t => {
  const ui = await page(t);
  assert.equal(ui.$('examReviewMode').value, 'size');
  assert.equal(ui.$('examReviewPlan').value, '');
  assert.equal(ui.$('schedGrid').querySelectorAll('.et-term-badge').length, 0, 'Term badges require an explicit study-plan choice');
  assert.ok(ui.$('examReviewMode').labels.length);
  assert.ok(ui.$('examReviewPlan').labels.length);
  assert.equal(ui.$('examReviewStatus').getAttribute('role'), 'status');
  ui.select('examReviewMode', 'neutral');
  assert.ok(!shown(ui.$('examReviewPlanField')));
  assert.ok(!shown(ui.$('examTermLegend')));
  const before = editorState(ui), requestCount = posts(ui).length;
  for (const mode of ['size', 'term', 'students', 'neutral']) ui.select('examReviewMode', mode);
  assert.deepEqual(editorState(ui), before);
  assert.equal(posts(ui).length, requestCount);
  assert.deepEqual(ui.notifications, []);
});

test('plan-specific terms distinguish mixed membership, missing mapping and outside-plan courses', async t => {
  const ui = await page(t);
  ui.select('examReviewMode', 'term');
  ui.select('examReviewPlan', 'AI');
  assert.equal(ui.chip('CS111 (1)').dataset.examTerm, '1');
  assert.equal(ui.chip('CYB215').dataset.examTerm, 'multiple');
  assert.equal(ui.chip('UNI101').dataset.examTerm, 'unknown');
  assert.equal(ui.chip('CS111 (2)').dataset.examTerm, undefined);
  assert.ok(ui.chip('CS111 (2)').classList.contains('et-review-muted'));
  ui.select('examReviewPlan', 'DS');
  assert.equal(ui.chip('CS111 (1)').dataset.examTerm, '3', 'The same canonical course can belong to different terms in different plans');
  assert.equal(ui.chip('MATH101').dataset.examTerm, '3');
  assert.equal(ui.chip('CYB215').dataset.examTerm, undefined);
});

test('a sole plan is selected automatically without changing saved timetable state', async t => {
  const run = savedRun();
  run.buckets_summary = run.buckets_summary.filter(bucket => bucket.program === 'AI');
  run.enrollment_scope.programs = ['AI'];
  run.schedule.forEach(entry => { entry.programs = ['AI']; });
  const ui = await page(t, { run });
  assert.equal(ui.$('examReviewMode').value, 'size');
  ui.select('examReviewMode', 'term');
  assert.equal(ui.$('examReviewPlan').value, 'AI');
  assert.equal(ui.chip('DS113').dataset.examTerm, '2');
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
  assert.ok(ui.$('exportXlsx').getAttribute('href'));
  assert.equal(posts(ui).length, 0);
});

test('manual movement marks overlap snapshot stale and updates draft-time relationship without recalculation', async t => {
  const ui = await page(t);
  ui.select('examReviewMode', 'students');
  relatedButton(ui, 'CS111 (1)').click();
  const originalCount = Number(reviewedCount(ui, 'CS111 (2)').match(/\d+/)[0]);
  move(ui, 'CS111 (2)', 'Sun', periods[0]);
  assert.equal(focusedCode(ui), 'CS111 (1)');
  assert.equal(ui.$('etResults').dataset.calculationState, 'stale');
  assert.ok(ui.chip('CS111 (2)').classList.contains('et-related-conflict'));
  assert.equal(Number(reviewedCount(ui, 'CS111 (2)').match(/\d+/)[0]), originalCount);
  assert.match(ui.$('examReviewStatus').textContent, language === 'ar' ? /آخر|سابق|فحص/ : /last|previous|check/i);
  assert.equal(posts(ui).length, 0);
  ui.$('undoExamBtn').click();
  assert.equal(focusedCode(ui), 'CS111 (1)');
  assert.ok(!ui.chip('CS111 (2)').classList.contains('et-related-conflict'));
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
  ui.$('redoExamBtn').click();
  assert.equal(focusedCode(ui), 'CS111 (1)');
  assert.ok(ui.chip('CS111 (2)').classList.contains('et-related-conflict'));
});

test('Check and Save redraw preserve exact focused identity without restoring a different same-code course', async t => {
  const ui = await page(t);
  ui.select('examReviewMode', 'students');
  relatedButton(ui, 'CS111 (2)').click();
  move(ui, 'DS113', 'Thu');
  ui.$('checkDraftBtn').click();
  await settle();
  assert.equal(ui.$('etResults').dataset.calculationState, 'checked');
  assert.equal(focusedCode(ui), 'CS111 (2)');
  assert.ok(!ui.chip('CS111 (1)').classList.contains('et-related-focus'));
  assert.ok(!ui.$('saveLoadedBtn').disabled);
  ui.$('saveLoadedBtn').click();
  await settle();
  assert.equal(focusedCode(ui), 'CS111 (2)');
  assert.equal(ui.$('examReviewMode').value, 'students');
  assert.equal(posts(ui).filter(request => request.url.endsWith('/draft-impact/')).length, 1);
  assert.equal(posts(ui).filter(request => request.url.endsWith('/build/')).length, 1);
  assert.ok(ui.$('exportXlsx').getAttribute('href')?.includes('82'));
});

for (const variant of ['missing', 'old-version', 'wrong-source', 'blocked-run']) {
  test(`unavailable overlap metadata is explicit and never shown as fresh zero overlaps (${variant})`, async t => {
    const run = savedRun();
    if (variant === 'missing') delete run.exam_review;
    if (variant === 'old-version') run.exam_review.version = 99;
    if (variant === 'wrong-source') run.exam_review.enrollment_source = 'registered';
    if (variant === 'blocked-run') run.enrollment_source = 'registered';
    const ui = await page(t, { run });
    ui.select('examReviewMode', 'students');
    relatedButton(ui, 'CS111 (1)').click();
    assert.equal(ui.$('schedGrid').querySelectorAll('.et-related-count').length, 0);
    assert.ok(ui.$('examReviewStatus').textContent.trim());
    assert.match(ui.$('examReviewStatus').textContent, language === 'ar' ? /متاح|بناء|مصدر|فحص/ : /unavailable|rebuild|source|check/i);
  });
}

test('pure review model uses canonical identities and fails closed for malformed pair data', async t => {
  const ui = await page(t);
  const model = ui.window.ExamReview.createModel(savedRun());
  assert.equal(model.byCode.get('CS111 (1)').identity, 'cs111-fundamentals');
  assert.equal(model.byCode.get('CS111 (2)').identity, 'cs111-programming');
  assert.equal(model.overlaps.get('cs111-fundamentals').get('uni101-skills'), 3);
  assert.equal(model.overlaps.get('cs111-fundamentals').get('cs111-programming'), 8);
  assert.equal(model.overlapsAvailable, true);
  const invalidEdges = [
    { course_a: 'CS111', course_b: 'DS113', shared_students: 2 },
    { course_a: 'CS111 (1)', course_b: 'unknown', shared_students: 2 },
    { course_a: 'CS111 (1)', course_b: 'DS113', shared_students: -1 },
    { course_a: 'CS111 (1)', course_b: 'DS113', shared_students: 1.5 },
  ];
  for (const edge of invalidEdges) {
    const run = savedRun();
    run.exam_review.student_overlaps.push(edge);
    assert.equal(ui.window.ExamReview.createModel(run).overlapsAvailable, false);
  }
  const duplicate = savedRun();
  duplicate.schedule[1].course_identity = duplicate.schedule[0].course_identity;
  assert.equal(ui.window.ExamReview.createModel(duplicate).overlapsAvailable, false);
  const duplicateCode = savedRun();
  duplicateCode.schedule[1].course_code = duplicateCode.schedule[0].course_code;
  assert.equal(ui.window.ExamReview.createModel(duplicateCode).overlapsAvailable, false);
});

test('review relationship classifies exact time overlaps, adjacent actual days and unscheduled exams', async t => {
  const ui = await page(t);
  const slots = savedRun().slots;
  const relation = ui.window.ExamReview.relationship;
  const source = { day: 'Sun', period: periods[0] };
  assert.equal(relation(source, { day: 'Sun', period: periods[0] }, slots), 'same-period');
  assert.equal(relation(source, { day: 'Sun', period: periods[1] }, slots), 'same-day');
  assert.equal(relation(source, { day: 'Mon', period: periods[0] }, slots), 'adjacent-day');
  assert.equal(relation(source, { day: 'Thu', period: periods[0] }, slots), 'separated');
  assert.equal(relation(source, { day: 'OVERFLOW', period: '' }, slots), 'unscheduled');
});

test('program, prefix and multi-code timetable searches remain independent from review controls', async t => {
  const ui = await page(t);
  for (const query of ['AI', 'CS*', 'CS111 (1) DS113']) {
    ui.select('examReviewMode', 'neutral');
    ui.input('schedFilter', query);
    const searchState = () => Array.from(ui.$('schedGrid').querySelectorAll('.et-course'), item => [
      item.dataset.course, item.classList.contains('et-dim'), item.classList.contains('et-highlight'),
    ]);
    const before = searchState();
    ui.select('examReviewMode', 'students');
    relatedButton(ui, 'CS111 (1)').click();
    assert.deepEqual(searchState(), before, query);
    ui.select('examReviewMode', 'term');
    ui.select('examReviewPlan', 'AI');
    termButtons(ui).find(button => button.dataset.examTerm === '2').click();
    assert.deepEqual(searchState(), before, query);
    assert.equal(ui.$('schedFilter').value, query);
  }
  assert.equal(posts(ui).length, 0);
});

test('neutral mode retains separately labelled warnings for approved thin same-period overlaps', async t => {
  const run = savedRun();
  Object.assign(run.schedule.find(entry => entry.course_code === 'UNI101'), { day: 'Sun', period: periods[0], slot_index: 0 });
  const ui = await page(t, { run });
  const before = editorState(ui);
  ui.select('examReviewMode', 'neutral');
  for (const code of ['CS111 (1)', 'UNI101']) {
    const warning = ui.chip(code).querySelector('.et-review-warning');
    assert.ok(warning);
    assert.equal(warning.getAttribute('role'), 'img');
    assert.ok(warning.getAttribute('aria-label').includes('3'));
    assert.ok(warning.title);
    assert.equal(ui.chip(code).querySelector('.et-term-badge'), null);
  }
  assert.deepEqual(editorState(ui), before, 'A measured relationship must not rewrite approved policy status');
  assert.equal(posts(ui).length, 0);
});

test('filtered review models preserve measured pairs without mutating the checked snapshot or double-counting reversed edges', async t => {
  const ui = await page(t);
  const run = savedRun();
  run.exam_review.student_overlaps.push({ course_a: 'CS111 (2)', course_b: 'CS111 (1)', shared_students: 8 });
  const original = JSON.stringify(run);
  const model = ui.window.ExamReview.createModel(run, ['CS111 (1)', 'CS111 (2)']);
  assert.equal(model.byCode.size, 2);
  assert.equal(model.byIdentity.size, 2);
  assert.equal(model.overlapsAvailable, true);
  assert.equal(model.overlaps.get('cs111-fundamentals').size, 1);
  assert.equal(model.overlaps.get('cs111-fundamentals').get('cs111-programming'), 8);
  assert.equal(JSON.stringify(run), original);
  run.exam_review.student_overlaps.at(-1).shared_students = 7;
  const invalid = ui.window.ExamReview.createModel(run);
  assert.equal(invalid.overlapsAvailable, false);
  assert.ok(Array.from(invalid.overlaps.values()).every(pairs => pairs.size === 0));
});

test('focused-course search visibility status follows the current search rather than its previous value', async t => {
  const ui = await page(t);
  ui.select('examReviewMode', 'students');
  relatedButton(ui, 'CS111 (1)').click();
  const subdued = language === 'ar' ? /خافت بسبب/ : /subdued by/;
  assert.doesNotMatch(ui.$('examReviewStatus').textContent, subdued);
  ui.input('schedFilter', 'MATH101');
  await settle();
  assert.ok(ui.chip('CS111 (1)').classList.contains('et-dim'));
  assert.match(ui.$('examReviewStatus').textContent, subdued);
  ui.input('schedFilter', '');
  await settle();
  assert.ok(!ui.chip('CS111 (1)').classList.contains('et-dim'));
  assert.doesNotMatch(ui.$('examReviewStatus').textContent, subdued);
});

test('sparse high study terms retain exact numbered labels and do not alias palette terms', async t => {
  const run = savedRun();
  run.buckets_summary.find(bucket => bucket.program === 'AI' && bucket.programme_term === 2).programme_term = 17;
  const ui = await page(t, { run });
  ui.select('examReviewMode', 'term');
  ui.select('examReviewPlan', 'AI');
  const seventeenth = termButtons(ui).find(button => button.dataset.examTerm === '17');
  assert.ok(seventeenth);
  assert.match(seventeenth.textContent, /17/);
  assert.equal(termButtons(ui).some(button => button.dataset.examTerm === '2'), false);
  assert.equal(ui.chip('DS113').dataset.examTerm, '17');
  assert.match(ui.chip('DS113').querySelector('.et-term-badge').textContent, /17/);
  seventeenth.click();
  assert.equal(seventeenth.getAttribute('aria-pressed'), 'true');
  assert.ok(!ui.chip('DS113').classList.contains('et-review-muted'));
  assert.ok(ui.chip('CS111 (1)').classList.contains('et-review-muted'));
});

function sizeRun(counts = [25, 151]) {
  const run = savedRun();
  counts.forEach((count, index) => {
    if (count === undefined) delete run.schedule[index].enrolled_count;
    else run.schedule[index].enrolled_count = count;
  });
  run.primary_status = 'contains_workload_warnings';
  run.status_flags = ['daily_limit_exceeded'];
  Object.assign(run.qa, { max_per_day: 1, students_over_limit_per_day: 1,
    overload_details: [{ student_id: 'SIZE-1', day: 'Sun', count: 2,
      courses: courses.slice(0, 2).map(course => ({ code: course.course_code, credits: 3 })) }] });
  return run;
}
const openSizeDrill = ui => ui.$('kOver2').closest('.kpi-click').click();
const sizeDetail = (ui, identity) => Array.from(ui.$('kpiDrillBody').querySelectorAll('.et-drill-course[data-course-identity]'))
  .find(card => card.dataset.courseIdentity === identity);
function assertSize(card, count, band) {
  assert.equal(card.dataset.examSize, band);
  assert.equal(card.dataset.examTerm, undefined);
  assert.equal(card.getAttribute('aria-label'), card.title);
  if (band === 'unknown') assert.match(card.title, language === 'ar' ? /غير متاح/ : /unavailable/i);
  else {
    assert.ok(card.title.includes(`${count} `), `Exact count ${count} is available without a printed count badge`);
    assert.match(card.title, language === 'ar' ? /آخر تحقق/ : /last checked/i);
  }
}

test('Exam size is the default with an explained legend and no course-card count line', async t => {
  const ui = await page(t, { run: sizeRun() });
  assert.equal(ui.$('examReviewMode').value, 'size');
  assert.ok(shown(ui.$('examSizeLegend')));
  assert.ok(!shown(ui.$('examReviewPlanField')));
  assert.ok(!shown(ui.$('examTermLegend')));
  assert.deepEqual(Array.from(ui.$('examSizeLegend').querySelectorAll('.et-size-key bdi'), label => label.textContent),
    ['1–30', '31–60', '61–100', '101–150', '151–200', '201+']);
  for (const [index, count, band] of [[0, 25, '1-30'], [1, 151, '151-200']]) {
    const card = ui.chip(courses[index].course_code);
    assertSize(card, count, band);
    assert.ok(card.title.includes(courses[index].course_name));
    assert.equal(card.querySelector('.et-review-badges').textContent, '');
    assert.ok(card.querySelector('.et-course-short-name').textContent.trim().split(/\s+/).length <= 2);
  }
  assert.equal(posts(ui).length, 0);
});

for (const cases of [
  [[0, 'zero'], [1, '1-30'], [30, '1-30'], [31, '31-60'], [60, '31-60'], [61, '61-100']],
  [[100, '61-100'], [101, '101-150'], [150, '101-150'], [151, '151-200'], [200, '151-200'], [201, '201-plus']],
  [[null, 'unknown'], [undefined, 'unknown'], ['25', 'unknown'], ['', 'unknown']],
  [[-1, 'unknown'], [1.5, 'unknown'], [false, 'unknown'], ['0', 'unknown'], [Number.MAX_SAFE_INTEGER + 1, 'unknown']],
]) {
  test(`size categories preserve strict measured-count boundaries: ${cases.map(([count]) => String(count)).join(', ')}`, async t => {
    const ui = await page(t, { run: sizeRun(cases.map(([count]) => count)) });
    cases.forEach(([count, band], index) => assertSize(ui.chip(courses[index].course_code), count, band));
    assert.equal(shown(ui.$('examSizeZero')), cases.some(([, band]) => band === 'zero'));
    assert.equal(shown(ui.$('examSizeUnknown')), cases.some(([, band]) => band === 'unknown'));
    openSizeDrill(ui);
    cases.slice(0, 2).forEach(([count, band], index) => assertSize(sizeDetail(ui, courses[index].course_identity), count, band));
  });
}

test('size, term, shared-student and neutral views cleanly replace each other without editing or exporting changes', async t => {
  const ui = await page(t, { run: sizeRun() });
  openSizeDrill(ui);
  const before = editorState(ui), requestCount = posts(ui).length;
  for (const mode of ['term', 'students', 'neutral']) {
    ui.select('examReviewMode', mode);
    if (mode === 'term') ui.select('examReviewPlan', 'AI');
    assert.ok(!shown(ui.$('examSizeLegend')));
    for (const course of courses.slice(0, 2)) {
      for (const card of [ui.chip(course.course_code), sizeDetail(ui, course.course_identity)]) {
        assert.equal(card.dataset.examSize, undefined);
        assert.doesNotMatch(card.title, language === 'ar' ? /عدد الطلاب المسجلين/ : /enrolled students?/i);
        assert.ok(card.title.includes(course.course_name));
        if (mode !== 'term') assert.equal(card.dataset.examTerm, undefined);
      }
    }
  }
  ui.select('examReviewMode', 'size');
  for (const [index, count, band] of [[0, 25, '1-30'], [1, 151, '151-200']]) {
    assertSize(ui.chip(courses[index].course_code), count, band);
    assertSize(sizeDetail(ui, courses[index].course_identity), count, band);
  }
  assert.deepEqual(editorState(ui), before);
  assert.equal(posts(ui).length, requestCount);
});

test('size updates invalidate a still-open detail cache by exact canonical count and source provenance', async t => {
  const run = sizeRun();
  const ui = await page(t, { run });
  openSizeDrill(ui);
  const first = sizeDetail(ui, courses[0].course_identity);
  const second = sizeDetail(ui, courses[1].course_identity);
  const updated = clone(run);
  updated.schedule[0].enrolled_count = 76;
  updated.schedule[1].enrolled_count = 26;
  ui.reviewController.update(updated);
  assert.equal(sizeDetail(ui, courses[0].course_identity), first, 'A repaint must refresh retained detail nodes too');
  assert.equal(sizeDetail(ui, courses[1].course_identity), second);
  assertSize(first, 76, '61-100');
  assertSize(second, 26, '1-30');
  assertSize(ui.chip(courses[0].course_code), 76, '61-100');
  assertSize(ui.chip(courses[1].course_code), 26, '1-30');
  ui.reviewController.update(updated, { visibleCodes: courses.slice(1).map(course => course.course_code) });
  ui.select('examReviewMode', 'neutral');
  assert.equal(first.title, `${courses[0].course_code} — ${courses[0].course_name}`, 'Excluded QA references must not retain an obsolete size tooltip');
  assert.equal(first.getAttribute('aria-label'), first.title);
  assert.equal(first.dataset.examSize, undefined);
  ui.select('examReviewMode', 'size');
  ui.reviewController.update(updated, { blocked: true });
  assertSize(first, 76, '61-100');
  assertSize(second, 26, '1-30');
  updated.enrollment_source = 'registered';
  ui.reviewController.update(updated, { blocked: true });
  for (const card of [first, second, ui.chip(courses[0].course_code), ui.chip(courses[1].course_code)]) assertSize(card, undefined, 'unknown');
  assert.ok(shown(ui.$('examSizeUnknown')));
});

test('size view survives Move, Undo, Check and Save while refreshed counts replace the prior snapshot', async t => {
  const run = sizeRun();
  let refreshed = false;
  const ui = await page(t, { run, onRequest: async (url, options) => {
    if (!url.endsWith('/draft-impact/') && !url.endsWith('/build/')) return undefined;
    const payload = JSON.parse(options.body);
    const data = clone(run);
    data.editor_revision = payload.editor_revision;
    data.run_id = url.endsWith('/build/') ? 82 : undefined;
    data.pinned = payload.pinned;
    data.schedule = payload.base_schedule.map(entry => ({ ...entry, rooms: [],
      slot_index: run.slots.findIndex(slot => slot.day === entry.day && slot.period === entry.period),
      enrolled_count: entry.course_identity === courses[0].course_identity ? 76
        : run.schedule.find(original => original.course_identity === entry.course_identity).enrolled_count }));
    data.input_fingerprint = 'reviewed-source-new-count';
    refreshed = true;
    return reply(data);
  } });
  openSizeDrill(ui);
  move(ui, 'DS113', 'Thu');
  assert.equal(ui.$('examReviewMode').value, 'size');
  assertSize(ui.chip(courses[0].course_code), 25, '1-30');
  assertSize(sizeDetail(ui, courses[0].course_identity), 25, '1-30');
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null);
  ui.$('undoExamBtn').click();
  assert.equal(ui.$('examReviewMode').value, 'size');
  assert.ok(ui.$('exportXlsx').getAttribute('href')?.includes('81'));
  ui.$('redoExamBtn').click();
  ui.$('checkDraftBtn').click();
  await settle();
  assert.ok(refreshed);
  assert.equal(ui.$('examReviewMode').value, 'size');
  assertSize(ui.chip(courses[0].course_code), 76, '61-100');
  assertSize(sizeDetail(ui, courses[0].course_identity), 76, '61-100');
  assert.equal(ui.$('exportXlsx').getAttribute('href'), null, 'Changed enrollment data must be saved before export');
  ui.$('saveLoadedBtn').click();
  await settle();
  assert.equal(ui.$('examReviewMode').value, 'size');
  assertSize(ui.chip(courses[0].course_code), 76, '61-100');
  assert.ok(ui.$('exportXlsx').getAttribute('href')?.includes('82'));
  assert.equal(posts(ui).filter(request => request.url.endsWith('/draft-impact/')).length, 1);
  assert.equal(posts(ui).filter(request => request.url.endsWith('/build/')).length, 1);
});
