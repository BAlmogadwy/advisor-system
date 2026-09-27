/*
 * The Timetable page's view tabs, each card's "N students" link and the
 * course drawer (static/js/exam-roster-drawer.js with the shared roster and
 * the phase-1 export), on the real page HTML and the real endpoints'
 * recorded answers. The page script, its hooks and every module run
 * unmodified. Run through tests/test_exam_rosters_frontend.py in English and
 * in Arabic (forced).
 */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const H = require('./roster-harness.cjs');

const { fixture, AR, say, RUN, idle, json, hold, rosterServer, loadPage } = H;
const SCRIPTS = [
  ['shared-utils.js', H.read('shared-utils.js')],
  ['exam-review.js', H.read('exam-review.js')],
  ['page-exam-timetable.js', `const LANGUAGE_CODE = ${JSON.stringify(fixture.language)};\n${H.read('page-exam-timetable.js')}`],
  ['exam-student-export.js', H.read('exam-student-export.js')],
  ['exam-roster.js', H.read('exam-roster.js')],
  ['exam-roster-drawer.js', H.read('exam-roster-drawer.js')],
];
const SAVE_FIRST = say('Unsaved changes. Save Changes or Optimize before exporting.', 'توجد تغييرات غير محفوظة. احفظ التغييرات أو حسّن الجدول قبل التصدير.');
const MATH_ROWS = () => fixture.rosters['{"exam": "MATH101", "kind": "course"}'].rows;

function timetableRoutes({ detail = fixture.timetable.detail, list = fixture.timetable.list } = {}) {
  return async url => {
    const path = url.split('?')[0];
    if (path === '/ops/exam-timetable/filters/') return json(fixture.timetable.filters);
    if (path === '/ops/exam-timetable/list/') return json(list);
    if (path === `/ops/exam-timetable/${RUN}/`) return json(detail);
    if (path === '/ops/exam-timetable/preview-courses/') return json(fixture.timetable.courses);
    if (path === '/ops/exam-timetable/jobs/active/') return json({ ok: true, job: null });
    return undefined;
  };
}

async function timetable(t, { url = 'http://exam.test/exam-timetable/', server = rosterServer(), load = true, routes = {}, ...options } = {}) {
  const ui = await loadPage(t, {
    page: 'timetable', url, server, scripts: SCRIPTS, extraRoute: timetableRoutes(routes),
    before: window => window.localStorage.setItem('exam-timetable-live-update', 'off'),
    ...options,
  });
  if (load) {
    ui.$('loadCoursesBtn').click();
    await idle();
    ui.$('historyList').querySelector('.et-run-info').click();
    await idle();
  }
  ui.card = code => [...ui.$('schedGrid').querySelectorAll('.et-course')].find(card => card.dataset.course === code);
  ui.link = code => ui.card(code)?.querySelector('.et-review-badges > .et-roster-link') || null;
  ui.drawer = ui.$('examRosterDrawer');
  ui.rows = () => [...ui.$('examRosterDrawerBody').querySelectorAll('tr.et-roster-row')];
  ui.tabs = () => [...ui.$('examRosterDrawerBody').querySelectorAll('[role="tab"]')];
  ui.menuItem = kind => ui.$('examRosterDrawerMenu').querySelector(`[data-download="${kind}"]`);
  return ui;
}

async function openDrawer(ui, code = 'MATH101') {
  ui.link(code).focus();
  ui.link(code).click();
  await idle();
}

const dropExam = (ui, code, day, period) => {
  const cell = [...ui.$('schedGrid').querySelectorAll('td[data-day]')].find(item => item.dataset.day === day && item.dataset.period === period);
  const event = new ui.window.Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: { getData: () => code } });
  cell.dispatchEvent(event);
};

// ── View tabs ───────────────────────────────────────────────

test('the view tabs are links: Timetable is current, and Student lists follows the saved run on the board', async t => {
  const ui = await timetable(t, { load: false });
  const nav = ui.document.querySelector('nav.et-view-tabs');
  assert.equal(nav.getAttribute('aria-label'), say('Exam timetable views', 'عروض جدول الاختبارات'));
  const [timetableTab, listsTab] = nav.querySelectorAll('a');
  assert.equal(timetableTab.getAttribute('aria-current'), 'page');
  assert.equal(listsTab.hasAttribute('aria-current'), false);
  assert.equal(ui.text(listsTab), say('Student lists', 'قوائم الطلاب'));
  assert.equal(listsTab.getAttribute('href'), '/exam-timetable/rosters/');
  assert.equal(listsTab.getAttribute('aria-disabled'), 'false');
  ui.$('historyList').querySelector('.et-run-info').click();
  await idle();
  assert.equal(listsTab.getAttribute('href'), `/exam-timetable/rosters/?run=${RUN}`);
  assert.ok(nav.previousElementSibling.classList.contains('page-intro'), 'the tabs open the content, under the page intro');
});

test('with nothing saved anywhere Student lists waits and says why', async t => {
  const ui = await timetable(t, { load: false, routes: { list: { ok: true, runs: [], page: 1, total_pages: 1, total: 0 } } });
  const tab = ui.$('examRosterTab');
  assert.equal(tab.getAttribute('aria-disabled'), 'true');
  assert.equal(tab.getAttribute('aria-describedby'), 'examRosterTabReason');
  assert.equal(ui.$('examRosterTabReason').hidden, false);
  assert.equal(ui.text('examRosterTabReason'), say('Save or load a timetable first.', 'احفظ جدولاً أو حمّل جدولاً محفوظاً أولاً.'));
  const click = new ui.window.MouseEvent('click', { bubbles: true, cancelable: true });
  assert.equal(tab.dispatchEvent(click), false, 'the click goes nowhere');
});

// ── The card link ───────────────────────────────────────────

test('each card\'s first badge is its "N students" link, counted from the saved run with no request', async t => {
  const ui = await timetable(t);
  const saved = fixture.timetable.detail.section_enrollment.MATH101.reduce((sum, group) => sum + group.student_count, 0);
  const link = ui.link('MATH101');
  assert.ok(link, 'the card has its link');
  assert.equal(link.parentElement.firstElementChild, link);
  assert.equal(link.tagName, 'BUTTON');
  assert.equal(ui.text(link), say(`${saved} students`, `الطلاب: ${saved}`));
  assert.equal(link.getAttribute('aria-label'), say(`Open the student list for MATH101, ${saved} students`, `فتح قائمة طلاب MATH101، عدد الطلاب: ${saved}`));
  assert.equal(link.getAttribute('aria-haspopup'), 'dialog');
  assert.equal(link.querySelector('bdi').dir, 'ltr');
  assert.equal(ui.server.calls.length, 0, 'the count asks the server nothing');
  // Tab order on the card: Pin, Move, then this link.
  const focusable = [...ui.card('MATH101').querySelectorAll('button')].filter(button => !button.classList.contains('et-related-action'));
  assert.deepEqual(focusable.map(button => button.dataset.examPin ? 'pin' : button.dataset.examMove ? 'move' : 'students'), ['pin', 'move', 'students']);
  // It is never a drag handle, and a double click on it pins nothing. (The
  // fixture pins every exam: unpin one first, so its card can be dragged.)
  ui.card('CS101').querySelector('[data-exam-pin]').click();
  await idle();
  assert.equal(ui.card('CS101').getAttribute('draggable'), 'true');
  const dragStart = target => {
    const drag = new ui.window.Event('dragstart', { bubbles: true, cancelable: true });
    Object.defineProperty(drag, 'dataTransfer', { value: { setData() {}, effectAllowed: '' } });
    return target.dispatchEvent(drag);
  };
  assert.equal(dragStart(ui.card('CS101').querySelector('.et-course-code')), true, 'the card itself drags');
  assert.equal(dragStart(ui.link('CS101')), false, 'its link does not');
  ui.link('CS101').dispatchEvent(new ui.window.MouseEvent('dblclick', { bubbles: true }));
  await idle();
  assert.equal(ui.card('CS101').classList.contains('et-pinned'), false);
});

test('the link stays first whatever the review view paints, and a card missing from the saved run has none', async t => {
  const detail = H.clone(fixture.timetable.detail);
  delete detail.section_enrollment.CS101;
  const ui = await timetable(t, { routes: { detail } });
  assert.equal(ui.link('CS101'), null);
  // A study plan's term badges painted first: the link still leads once the grid is drawn again.
  const mode = ui.$('examReviewMode');
  mode.value = 'term';
  mode.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
  const plan = ui.$('examReviewPlan');
  plan.value = [...plan.options].find(option => option.value === 'CS').value;
  plan.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
  await idle();
  ui.run('renderScheduleGrid(_currentResultData.schedule, _currentResultData.slots)');
  await idle();
  const painted = ui.card('MATH101').querySelector('.et-review-badges');
  assert.ok(painted.children.length > 1, 'a term badge is painted beside the link');
  assert.ok(painted.firstElementChild.classList.contains('et-roster-link'));
  const select = ui.$('examReviewMode');
  for (const mode of ['term', 'students', 'neutral', 'size']) {
    select.value = mode;
    select.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
    await idle();
    const badges = ui.card('MATH101').querySelector('.et-review-badges');
    assert.equal(badges.firstElementChild.classList.contains('et-roster-link'), true, mode);
    assert.equal(badges.querySelectorAll('.et-roster-link').length, 1, mode);
  }
});

// ── The drawer ──────────────────────────────────────────────

test('the link opens the drawer on its heading with the saved run named, then its audited list; Esc returns focus to the link', async t => {
  const ui = await timetable(t);
  await openDrawer(ui);
  assert.equal(ui.drawer.open, true);
  assert.equal(ui.drawer.getAttribute('aria-labelledby'), 'examRosterDrawerTitle');
  assert.equal(ui.drawer.getAttribute('aria-describedby'), 'examRosterDrawerSource');
  assert.equal(ui.document.activeElement, ui.$('examRosterDrawerTitle'));
  assert.equal(ui.text('examRosterDrawerTitle'), 'MATH101 · CALCULUS I');
  assert.equal(ui.text('examRosterDrawerMeta'), say('Sun 08:00-10:00 · Students: 43 · Rooms: 3', 'Sun 08:00-10:00 · عدد الطلاب: 43 · عدد القاعات: 3'));
  assert.match(ui.text('examRosterDrawerSource'), say(
    new RegExp(`^Saved timetable #${RUN} “${fixture.index.run.label}” · saved \\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d · Lists checked \\d\\d:\\d\\d:\\d\\d · ✓ Lists match the saved timetable$`),
    new RegExp(`^الجدول المحفوظ رقم ${RUN} «${fixture.index.run.label}» · حُفظ \\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d · وقت مطابقة القوائم: \\d\\d:\\d\\d:\\d\\d · ✓ القوائم مطابقة للجدول المحفوظ$`)));
  assert.deepEqual(ui.server.bodies('roster'), [{ scope: { kind: 'course', exam: 'MATH101' } }]);
  assert.equal(ui.server.requests('roster')[0].options.method, 'POST');
  assert.equal(ui.rows().length, 43);
  assert.equal(ui.$('examRosterDrawerNotice').hidden, true, 'nothing unsaved, nothing to say');
  assert.equal(ui.text('examRosterDrawerLive'), say('MATH101: 43 students · Clash 17 · Same day 16', 'MATH101: عدد الطلاب 43 · تعارض: 17 · اليوم نفسه: 16'));
  const open = ui.$('examRosterDrawerOpenPage');
  assert.equal(open.getAttribute('href'), `/exam-timetable/rosters/?run=${RUN}&view=course&course=MATH101`);
  assert.equal(open.target, '_blank');
  assert.equal(open.rel, 'noopener');
  assert.equal(ui.text(open.querySelector('.visually-hidden')), say('(opens in a new tab)', '(يفتح في تبويب جديد)'));
  const cancel = new ui.window.Event('cancel', { cancelable: true });
  ui.drawer.dispatchEvent(cancel);
  assert.equal(ui.drawer.open, false);
  assert.equal(ui.document.activeElement, ui.link('MATH101'));
  // Nothing of the list stays in the page once the drawer is closed.
  assert.equal(ui.rows().length, 0);
  const left = ui.drawer.textContent;
  assert.ok(MATH_ROWS().every(row => !left.includes(row.name) && !left.includes(String(row.student_id))));
});

test('closing finds the card\'s link again when the grid re-rendered meanwhile', async t => {
  const ui = await timetable(t);
  await openDrawer(ui);
  const before = ui.link('MATH101');
  ui.run('renderScheduleGrid(_currentResultData.schedule, _currentResultData.slots)');
  await idle();
  assert.notEqual(ui.link('MATH101'), before, 'the grid re-rendered under the drawer');
  ui.$('examRosterDrawerClose').click();
  assert.equal(ui.drawer.open, false);
  assert.equal(ui.document.activeElement, ui.link('MATH101'));
});

test('with a move unsaved the drawer shows the saved run, says so and where the draft has it, and downloads wait', async t => {
  const ui = await timetable(t);
  ui.card('CS101').querySelector('[data-exam-pin]').click();
  await idle();
  dropExam(ui, 'CS101', 'Mon', '08:00-10:00');
  await idle();
  await openDrawer(ui, 'CS101');
  assert.equal(ui.text('examRosterDrawerMeta').split(' · ')[0], 'Sun 13:00-15:00', 'the saved placement, not the draft');
  assert.equal(ui.$('examRosterDrawerNotice').hidden, false);
  assert.equal(ui.text('examRosterDrawerUnsaved'), say(
    'Showing the saved timetable. Your unsaved move is not included, and downloads wait until you save.',
    'يُعرض الجدول المحفوظ دون تعديلك غير المحفوظ، ويتوقف التنزيل حتى الحفظ.'));
  assert.equal(ui.$('examRosterDrawerDraft').hidden, false);
  assert.equal(ui.text('examRosterDrawerDraft'), say('In your draft this exam is at Mon 08:00-10:00.', 'هذا الاختبار في مسودتك في Mon 08:00-10:00.'));
  const download = ui.$('examRosterDrawerDownload');
  assert.equal(download.getAttribute('aria-disabled'), 'true');
  assert.equal(download.getAttribute('aria-describedby'), 'examRosterDrawerReason');
  assert.equal(ui.text('examRosterDrawerReason'), SAVE_FIRST);
  download.click();
  await idle();
  assert.equal(ui.$('examRosterDrawerMenu').hidden, true);
  assert.equal(ui.server.requests('export').length, 0);
  // Another exam, not moved: the notice, without a draft line.
  ui.$('examRosterDrawerClose').click();
  await openDrawer(ui, 'MATH101');
  assert.equal(ui.$('examRosterDrawerNotice').hidden, false);
  assert.equal(ui.$('examRosterDrawerDraft').hidden, true);
});

test('a pin changed but nothing moved: the drawer still says the draft is not what it shows', async t => {
  const ui = await timetable(t);
  ui.card('CS101').querySelector('[data-exam-pin]').click();
  await idle();
  await openDrawer(ui);
  assert.equal(ui.$('examRosterDrawerNotice').hidden, false);
  assert.equal(ui.text('examRosterDrawerUnsaved'), say(
    'Showing the saved timetable. Your unsaved changes are not included, and downloads wait until you save.',
    'يُعرض الجدول المحفوظ دون تعديلاتك غير المحفوظة، ويتوقف التنزيل حتى الحفظ.'));
  assert.equal(ui.$('examRosterDrawerDraft').hidden, true);
});

test('a flag opens the other exam in the drawer; Back returns to the list already shown without asking again', async t => {
  const ui = await timetable(t);
  await openDrawer(ui);
  const clash = ui.$('examRosterDrawerBody').querySelector('a.et-flag--clash');
  const other = clash.dataset.exam;
  assert.equal(clash.getAttribute('href'), `/exam-timetable/rosters/?run=${RUN}&view=course&course=${other}`);
  assert.equal(ui.follow(clash), false);
  await idle();
  assert.deepEqual(ui.server.bodies('roster').map(body => body.scope.exam), ['MATH101', other]);
  assert.equal(ui.text('examRosterDrawerTitle').split(' · ')[0], other);
  assert.equal(ui.document.activeElement, ui.$('examRosterDrawerTitle'));
  const back = ui.$('examRosterDrawerBack');
  assert.equal(back.hidden, false);
  assert.equal(ui.text('examRosterDrawerBackText'), say('Back to MATH101', 'العودة إلى MATH101'));
  assert.equal(back.querySelector('.et-back-glyph').getAttribute('aria-hidden'), 'true');
  back.click();
  await idle();
  assert.equal(ui.text('examRosterDrawerTitle').split(' · ')[0], 'MATH101');
  assert.equal(back.hidden, true);
  assert.equal(ui.server.requests('roster').length, 2, 'Back re-shows the list it had');
  assert.equal(ui.rows().length, 43);
  assert.equal(ui.drawer.open, true);
});

test('a failed audit in the drawer shows no student and says why; Try again asks again', async t => {
  const server = rosterServer();
  server.queue('roster', json({ ok: false, code: 'audit_unavailable', error: 'x' }, 503));
  const ui = await timetable(t, { server });
  await openDrawer(ui);
  const alert = ui.$('examRosterDrawerBody').querySelector('.et-roster-error');
  assert.equal(ui.text(alert.querySelector('span')), say(
    "Couldn't record this access, so the list wasn't shown. Try again.", 'تعذّر تسجيل هذا الاطلاع، لذا لم تُعرض القائمة. حاول مرة أخرى.'));
  assert.equal(ui.rows().length, 0);
  assert.equal(ui.$('examRosterDrawerDownload').getAttribute('aria-disabled'), 'true', 'nothing to download until the list is shown');
  alert.querySelector('button').click();
  await idle();
  assert.equal(ui.rows().length, 43);
  assert.equal(ui.$('examRosterDrawerDownload').getAttribute('aria-disabled'), 'false');
});

// ── Download ────────────────────────────────────────────────

test('Download is a menu: arrows move, Esc closes to its button, and the section item waits for a section tab', async t => {
  const ui = await timetable(t);
  await openDrawer(ui);
  const button = ui.$('examRosterDrawerDownload');
  const menu = ui.$('examRosterDrawerMenu');
  button.click();
  await idle();
  assert.equal(menu.hidden, false);
  assert.equal(button.getAttribute('aria-expanded'), 'true');
  assert.equal(menu.getAttribute('role'), 'menu');
  const section = ui.menuItem('section');
  assert.equal(section.getAttribute('aria-disabled'), 'true');
  assert.equal(ui.$('examRosterDrawerSectionHint').hidden, false);
  assert.equal(section.getAttribute('aria-describedby'), 'examRosterDrawerSectionHint');
  assert.equal(ui.document.activeElement, ui.menuItem('course'), 'focus starts on the first item that works');
  ui.key(ui.menuItem('course'), 'ArrowDown');
  assert.equal(ui.document.activeElement, ui.menuItem('more'));
  ui.key(ui.menuItem('more'), 'End');
  assert.equal(ui.document.activeElement.dataset.language, 'en');
  ui.key(ui.document.activeElement, 'Escape');
  assert.equal(menu.hidden, true);
  assert.equal(button.getAttribute('aria-expanded'), 'false');
  assert.equal(ui.document.activeElement, button);
  assert.equal(ui.drawer.open, true, 'Esc closed the menu, not the drawer');
  // The dialog's own Esc (its cancel event) also closes an open menu first.
  button.click();
  await idle();
  ui.drawer.dispatchEvent(new ui.window.Event('cancel', { cancelable: true }));
  assert.equal(menu.hidden, true);
  assert.equal(ui.drawer.open, true);
  assert.equal(ui.document.activeElement, button);
  // A section tab chosen, the section item names it.
  ui.tabs()[1].click();
  await idle();
  button.click();
  await idle();
  assert.equal(section.getAttribute('aria-disabled'), 'false');
  assert.equal(ui.text(section.querySelector('.et-menu-label')), say('Section M1 · Excel', 'الشعبة M1 · إكسل'));
  assert.equal(ui.document.activeElement, section);
});

test('All sections and This section download through the export endpoint, in the remembered file language', async t => {
  const ui = await timetable(t, { before: window => {
    window.localStorage.setItem('exam-timetable-live-update', 'off');
    window.localStorage.setItem('exam-student-export', JSON.stringify({ scope: 'day', contents: 'summary', language: 'ar' }));
  } });
  await openDrawer(ui);
  ui.$('examRosterDrawerDownload').click();
  await idle();
  ui.menuItem('course').click();
  await idle();
  assert.deepEqual(ui.server.bodies('export'), [{
    scope: { kind: 'course', exam: 'MATH101' }, programs: [], groups: ['M', 'F', 'U'], one_file_per_group: false,
    rows: 'all', contents: 'full', language: 'ar', dates: {},
  }]);
  assert.equal(ui.server.requests('export')[0].options.headers['X-CSRFToken'].length > 0, true);
  assert.equal(ui.downloads.at(-1).saved.filename, `exam_students_MATH101_r${RUN}_ar_7F3A2C1D.xlsx`);
  const done = say(`Downloaded exam_students_MATH101_r${RUN}_ar_7F3A2C1D.xlsx · Ref EXR-7F3A2C1D`, `تم تنزيل exam_students_MATH101_r${RUN}_ar_7F3A2C1D.xlsx · المرجع EXR-7F3A2C1D`);
  assert.equal(ui.text('examRosterDrawerStatus'), done);
  assert.equal(ui.text('examRosterDrawerLive'), done);
  assert.equal(ui.document.activeElement, ui.$('examRosterDrawerDownload'));
  // English for the file, remembered with the dialog's own preference.
  ui.$('examRosterDrawerDownload').click();
  await idle();
  const english = ui.$('examRosterDrawerMenu').querySelector('[data-language="en"]');
  english.click();
  await idle();
  assert.equal(english.getAttribute('aria-checked'), 'true');
  assert.deepEqual(JSON.parse(ui.window.localStorage.getItem('exam-student-export')), { scope: 'day', contents: 'summary', language: 'en' },
    'the other remembered choices of the dialog stay');
  ui.key(english, 'Escape');
  ui.tabs()[2].click();
  await idle();
  ui.$('examRosterDrawerDownload').click();
  await idle();
  ui.menuItem('section').click();
  await idle();
  assert.deepEqual(ui.server.bodies('export').at(-1), {
    scope: { kind: 'section', exam: 'MATH101', section_key: 'term-section:2', gender: 'F' }, programs: [], groups: ['F'],
    one_file_per_group: false, rows: 'all', contents: 'full', language: 'en', dates: {},
  });
});

test('a refused download says why in place, with Try again', async t => {
  const server = rosterServer();
  server.queue('export', json({ ok: false, code: 'audit_unavailable', error: 'x' }, 503));
  const ui = await timetable(t, { server });
  await openDrawer(ui);
  ui.$('examRosterDrawerDownload').click();
  await idle();
  ui.menuItem('course').click();
  await idle();
  assert.equal(ui.$('examRosterDrawerError').hidden, false);
  assert.equal(ui.text('examRosterDrawerErrorText'), say("Couldn't record this export, so no file was made. Try again.", 'تعذّر تسجيل هذا التصدير، لذا لم يُنشأ أي ملف. أعد المحاولة.'));
  assert.equal(ui.downloads.length, 0);
  ui.$('examRosterDrawerRetry').click();
  await idle();
  assert.equal(ui.server.requests('export').length, 2);
  assert.equal(ui.$('examRosterDrawerError').hidden, true);
  assert.equal(ui.downloads.length, 1);
});

test('More options opens the phase-1 dialog preset to the section on screen; closing it returns focus to Download', async t => {
  const ui = await timetable(t);
  await openDrawer(ui);
  ui.tabs()[1].click();
  await idle();
  ui.$('examRosterDrawerDownload').click();
  await idle();
  ui.menuItem('more').click();
  await idle();
  const dialog = ui.$('examStudentExportDialog');
  assert.equal(dialog.open, true);
  assert.equal(ui.document.activeElement, ui.$('examStudentExportTitle'));
  assert.equal(ui.document.querySelector('input[name="examStudentScope"]:checked').value, 'section');
  assert.deepEqual(JSON.parse(ui.$('examStudentSection').value), ['MATH101', 'term-section:1', 'M']);
  assert.deepEqual(ui.server.bodies('preflight')[0].scope, { kind: 'section', exam: 'MATH101', section_key: 'term-section:1', gender: 'M' });
  ui.$('examStudentExportCancel').click();
  assert.equal(dialog.open, false);
  assert.equal(ui.drawer.open, true);
  assert.equal(ui.document.activeElement, ui.$('examRosterDrawerDownload'));
});

// ── Show in timetable ───────────────────────────────────────

test('?run=&focus= from Student lists loads that saved run, finds the exam, and cleans the address', async t => {
  const ui = await timetable(t, { load: false, url: `http://exam.test/exam-timetable/?run=${RUN}&focus=CS101` });
  await idle();
  assert.ok(ui.requests.some(request => request.url === `/ops/exam-timetable/${RUN}/`));
  assert.equal(ui.window.location.search, '');
  const card = ui.card('CS101');
  assert.ok(card.classList.contains('et-found-exam'));
  assert.equal(ui.document.activeElement, card);
  assert.equal(ui.$('examRosterTab').getAttribute('href'), `/exam-timetable/rosters/?run=${RUN}`);
});

test('an address that is not a run id loads nothing', async t => {
  const ui = await timetable(t, { load: false, url: 'http://exam.test/exam-timetable/?run=1e3&focus=CS101' });
  await idle();
  assert.equal(ui.requests.some(request => /^\/ops\/exam-timetable\/\d+\/$/.test(request.url)), false);
  assert.equal(ui.card('CS101'), undefined);
});

// ── Review round ────────────────────────────────────────────

test('the drawer says the whole run\'s check from its own build, and this exam\'s changed sections beside it', async t => {
  const ui = await timetable(t, { server: rosterServer({ changed: true }) });
  const source = () => ui.text('examRosterDrawerSource');
  // CS101's own sections are as saved, but the run's lists are not: a row's
  // flags follow every exam's lists, so the drawer never says they match.
  const cs101 = H.rosterOf({ kind: 'course', exam: 'CS101' }, { changed: true });
  assert.ok(cs101.sections.every(section => section.membership === 'matches' && section.program_mix === 'matches'));
  assert.equal(cs101.check.status, 'changed');
  await openDrawer(ui, 'CS101');
  assert.ok(source().endsWith(say('· ≠ Sections changed since saving: 2', '· ≠ الشعب المتغيرة بعد الحفظ: 2')), source());
  assert.equal(ui.$('examRosterDrawerSource').querySelector('[data-check]').dataset.check, 'changed');
  assert.equal(ui.$('examRosterDrawerSource').querySelector('.et-check-own'), null, 'nothing of CS101 itself changed');
  assert.ok(source().includes(ui.window.ExamRoster.clock(cs101.checked_at)), 'the check and its time are one build');
  ui.$('examRosterDrawerClose').click();
  await openDrawer(ui, 'MATH101');
  assert.ok(source().endsWith(say('· ≠ Sections changed since saving: 2 · Changed in this exam: 2', '· ≠ الشعب المتغيرة بعد الحفظ: 2 · المتغيرة في هذا الاختبار: 2')), source());
});

test('a drag that selects a name and ends over the backdrop keeps the drawer open; a press and a click on the backdrop close it', async t => {
  const ui = await timetable(t);
  await openDrawer(ui);
  const name = ui.rows()[0].querySelector('.et-col-name');
  // A selection dragged out of the panel: the press is on a name, the click lands on the dialog.
  name.dispatchEvent(new ui.window.PointerEvent('pointerdown', { bubbles: true }));
  ui.drawer.dispatchEvent(new ui.window.MouseEvent('click', { bubbles: true }));
  assert.equal(ui.drawer.open, true);
  // A click with no press (a synthetic one) is no backdrop press either.
  ui.drawer.dispatchEvent(new ui.window.MouseEvent('click', { bubbles: true }));
  assert.equal(ui.drawer.open, true);
  ui.drawer.dispatchEvent(new ui.window.PointerEvent('pointerdown', { bubbles: true }));
  ui.drawer.dispatchEvent(new ui.window.MouseEvent('click', { bubbles: true }));
  assert.equal(ui.drawer.open, false);
  assert.equal(ui.document.activeElement, ui.link('MATH101'));
});

// ── Beside the same-day card (master's #120, on the same page) ──

test('the same-day card and its pair detail work beside the card links: Find lands on a card that keeps its link, and the drawer leaves the detail open', async t => {
  const ui = await timetable(t);
  const { qa } = fixture.timetable.detail;
  const pairs = qa.same_day_exam_pairs;
  // The real build measured both, so neither side can pass on a stub.
  assert.ok(Number.isInteger(qa.multi_exam_day_students) && qa.multi_exam_day_students > 0, 'the build counted students');
  assert.ok(Array.isArray(pairs) && pairs.length > 1, 'and recorded their exam pairs');
  assert.equal(ui.text('kMultiExamDay'), String(qa.multi_exam_day_students));
  const card = ui.$('kMultiExamDay').closest('.kpi-click');
  card.focus();
  card.click();
  await idle();
  assert.equal(ui.$('kpiDrill').classList.contains('d-none'), false);
  assert.equal(ui.$('kpiDrill').dataset.type, 'same-day-pairs');
  assert.equal(card.getAttribute('aria-expanded'), 'true');
  assert.equal(ui.text('kpiDrillTitle'), say('Exam pairs on the same day', 'أزواج الاختبارات في اليوم نفسه'));
  assert.equal(ui.$('kpiDrillNote').hidden, false, 'the note says why pairs outnumber the card');
  const rows = [...ui.$('kpiDrillBody').querySelectorAll('tr')];
  assert.equal(rows.length, pairs.length);
  assert.deepEqual(rows.map(row => ui.text(row.querySelector('.et-pair-count'))), pairs.map(pair => String(pair.student_count)));
  assert.deepEqual(rows.map(row => [...row.querySelectorAll('[data-course-identity]')].map(item => item.querySelector('.et-course-code')?.textContent.trim())),
    pairs.map(pair => pair.courses.map(exam => exam.code)));
  // The drill's exam cards are not the grid's: they never grow a student link.
  assert.equal(ui.$('kpiDrillBody').querySelector('.et-roster-link'), null);

  // Find from the detail lands on the grid card, which still leads with its link.
  const code = pairs[0].courses[0].code;
  const saved = fixture.timetable.detail.section_enrollment[code].reduce((sum, group) => sum + group.student_count, 0);
  rows[0].querySelector('[data-find-exam]').click();
  await idle();
  assert.equal(ui.document.activeElement, ui.card(code));
  assert.ok(ui.card(code).classList.contains('et-found-exam'));
  const link = ui.link(code);
  assert.ok(link, 'the found card keeps its link');
  assert.equal(link.parentElement.firstElementChild, link);
  assert.equal(ui.text(link), say(`${saved} students`, `الطلاب: ${saved}`));

  // The link opens that exam's drawer; closing it leaves the detail as it was.
  await openDrawer(ui, code);
  assert.equal(ui.drawer.open, true);
  assert.equal(ui.text('examRosterDrawerTitle').split(' · ')[0], code);
  assert.equal(ui.rows().length, H.rosterOf({ kind: 'course', exam: code }).rows.length);
  ui.drawer.dispatchEvent(new ui.window.Event('cancel', { cancelable: true }));
  assert.equal(ui.drawer.open, false);
  assert.equal(ui.document.activeElement, ui.link(code));
  assert.equal(ui.$('kpiDrill').dataset.type, 'same-day-pairs');
  assert.equal(ui.$('kpiDrill').classList.contains('d-none'), false);
  assert.equal(ui.$('kpiDrillBody').querySelectorAll('tr').length, pairs.length);
  assert.equal(ui.text('kMultiExamDay'), String(qa.multi_exam_day_students));
});
