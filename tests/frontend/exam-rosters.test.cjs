/*
 * The Student lists page (static/js/page-exam-rosters.js with the shared
 * roster, static/js/exam-roster.js, and the phase-1 export dialog) on the
 * real page HTML and the real endpoints' recorded answers. Run through
 * tests/test_exam_rosters_frontend.py in English and in Arabic (forced).
 */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const H = require('./roster-harness.cjs');

const { fixture, AR, say, RUN, idle, json, hold, rosterServer, loadPage } = H;
const SCRIPTS = [
  ['exam-roster.js', H.read('exam-roster.js')],
  ['page-exam-rosters.js', H.read('page-exam-rosters.js')],
  ['exam-student-export.js', H.read('exam-student-export.js')],
];
const PAGE_URL = `http://exam.test/exam-timetable/rosters/?run=${RUN}`;
const IDS = Object.keys(fixture.lookups).filter(id => fixture.lookups[id].found);
const NAMES = IDS.map(id => fixture.lookups[id].rows[0].name);

async function rosters(t, { url = PAGE_URL, server = rosterServer(), ...options } = {}) {
  const ui = await loadPage(t, {
    page: 'rosters', url, server, scripts: SCRIPTS,
    before: window => { window.djCsrfToken = 'test-csrf'; },
    ...options,
  });
  ui.rooms = () => [...ui.$('examRostersRooms').querySelectorAll('.et-nav-item')];
  ui.courses = () => [...ui.$('examRostersCourses').querySelectorAll('.et-nav-item')];
  ui.item = code => ui.rooms().find(item => item.dataset.room === code) || ui.courses().find(item => item.dataset.course === code);
  ui.rows = () => [...ui.$('examRostersRoster').querySelectorAll('tr.et-roster-row')];
  ui.groupRows = () => [...ui.$('examRostersRoster').querySelectorAll('tr.et-roster-group-row')].map(row => ui.text(row));
  ui.chips = () => [...ui.$('examRostersRoster').querySelectorAll('.et-flag-chip')];
  ui.tabs = () => [...ui.$('examRostersRoster').querySelectorAll('[role="tab"]')];
  ui.chip = flag => ui.chips().find(chip => chip.dataset.flag === flag);
  ui.address = () => new URL(ui.window.location.href);
  return ui;
}

const room = (ui, code) => ui.rooms().find(item => item.dataset.room === code);
async function chooseRoom(ui, code) {
  room(ui, code).click();
  await idle();
}
async function chooseCourse(ui, code) {
  ui.$('examRostersViews').querySelector('[data-view="course"]').click();
  await idle();
  ui.courses().find(item => item.dataset.course === code).click();
  await idle();
}

// No student ID or name ever reaches an address, the history or a GET.
function assertNoStudentInAddress(ui) {
  const places = [ui.window.location.href, JSON.stringify(ui.history), ...ui.requests.filter(request => !request.method || request.method === 'GET').map(request => request.url)];
  for (const place of places) {
    for (const id of IDS) assert.ok(!place.includes(id), `student ${id} in ${place}`);
    for (const name of NAMES) assert.ok(!place.includes(name) && !place.includes(encodeURIComponent(name)), `a name in ${place}`);
  }
}

// ── Landing ─────────────────────────────────────────────────

test('the page opens on the first period with its rooms, names its run and check, and its address holds the run, view and period only', async t => {
  const ui = await rosters(t);
  assert.equal(ui.server.requests('index').length, 1);
  assert.equal(ui.server.requests('roster').length, 0, 'no student list is asked for until a room or course is chosen');
  const days = [...ui.$('examRostersDays').querySelectorAll('[role="tab"]')];
  assert.deepEqual(days.map(tab => tab.dataset.day), ['Sun', 'Mon']);
  assert.deepEqual(days.map(tab => tab.getAttribute('aria-selected')), ['true', 'false']);
  assert.equal(ui.$('examRostersDayPanel').getAttribute('aria-labelledby'), days[0].id);
  const periods = [...ui.$('examRostersPeriods').querySelectorAll('[role="radio"]')];
  assert.deepEqual(periods.map(chip => ui.text(chip.querySelector('bdi'))), ['08:00-10:00', '13:00-15:00']);
  assert.deepEqual(periods.map(chip => chip.getAttribute('aria-checked')), ['true', 'false']);
  // Distinct students, as the day tab and the file's Period summary count
  // them: every IS201 student also sits MATH101 in this period (60 sittings).
  assert.deepEqual([fixture.index.slots[0].sittings, fixture.index.slots[0].students], [60, 43]);
  assert.equal(periods[0].getAttribute('aria-label'), say('08:00-10:00, 43 students', '08:00-10:00، عدد الطلاب: 43'));
  assert.equal(ui.text(periods[0].querySelector('small')), '43');
  assert.equal(ui.text(days[0].querySelector('small')), '43', 'every Sunday student sits the morning period');
  // One exam week: no week column is drawn or reserved.
  assert.ok(ui.$('examRostersDays').classList.contains('is-one-week'));
  assert.equal(ui.$('examRostersDays').querySelectorAll('.et-week-label').length, 0);
  const expectedRooms = fixture.index.rooms.filter(item => item.slot_index === 0 && !item.online).map(item => item.room_code);
  assert.deepEqual(ui.rooms().filter(item => item.dataset.room).map(item => item.dataset.room), expectedRooms);
  // The timetable could not room MATH101's student with no cohort: that
  // section waits under its own danger heading, last.
  const headings = [...ui.$('examRostersRooms').querySelectorAll('.et-nav-heading')];
  assert.deepEqual(headings.map(node => ui.text(node)), [say('Not assigned · 1 section', 'لم تُحدَّد لها قاعة · عدد الشعب: 1')]);
  assert.ok(headings[0].classList.contains('is-danger'));
  assert.equal(ui.text(ui.$('examRostersCheck')), say('· ✓ Lists match the saved timetable', '· ✓ القوائم مطابقة للجدول المحفوظ'));
  assert.match(ui.text('examRostersChecked'), say(/^Lists checked \d\d:\d\d:\d\d$/, /^وقت مطابقة القوائم: \d\d:\d\d:\d\d$/));
  assert.equal(ui.text('examRostersLists'), say('Imported student timetables · 1448 term 1', 'الجداول الدراسية المستوردة للطلاب · الفصل 1 من عام 1448'));
  assert.equal(ui.$('examRostersExport').getAttribute('aria-disabled'), 'false');
  assert.equal(ui.$('examRostersPane').getAttribute('aria-labelledby'), 'examRostersPaneEmpty');
  assert.equal(ui.text('examRostersPaneEmpty'), say('Choose a room or a course to see its students.', 'اختر قاعة أو مقرراً لعرض طلابه.'));
  assert.deepEqual([...ui.address().searchParams.entries()], [['run', String(RUN)], ['view', 'room'], ['slot', '0']]);
  assertNoStudentInAddress(ui);
});

// ── By room ─────────────────────────────────────────────────

test('choosing a room loads its list once, audited by POST, with focus kept on the room and Back returning to the previous room', async t => {
  const ui = await rosters(t);
  const before = ui.window.history.length;
  const target = room(ui, 'F-A');
  target.focus();
  target.click();
  await idle();
  assert.deepEqual(ui.server.bodies('roster'), [{ scope: { kind: 'room', slot_index: 0, room_code: 'F-A' } }]);
  const call = ui.server.requests('roster')[0];
  assert.equal(call.options.method, 'POST');
  assert.equal(call.options.headers['X-CSRFToken'], 'test-csrf');
  assert.equal(ui.window.history.length, before + 1, 'a room is a history entry');
  assert.deepEqual([...ui.address().searchParams.entries()], [['run', String(RUN)], ['view', 'room'], ['slot', '0'], ['room', 'F-A']]);
  // Focus stays in the navigator, on the room, which says it is current.
  assert.equal(ui.document.activeElement, room(ui, 'F-A'));
  assert.equal(room(ui, 'F-A').getAttribute('aria-current'), 'true');
  assert.equal(room(ui, 'F-A').tabIndex, 0);
  assert.equal(ui.$('examRostersPane').getAttribute('aria-labelledby'), 'examRostersPaneTitle');
  assert.equal(ui.text('examRostersPaneTitle'), say('Room F-A', 'القاعة F-A'));
  assert.equal(ui.text('examRostersPaneMeta'), say(
    'Sun 08:00-10:00 · Seats: 12 of 20 · Building 172, floor 1 · In this room: MATH101 F1 (12)',
    'Sun 08:00-10:00 · المقاعد: 12 من 20 · المبنى 172، الدور 1 · في هذه القاعة: MATH101 F1 (12)'));
  assert.equal(ui.rows().length, 12);
  assert.equal(ui.text('examRostersLive'), say('Room F-A: 12 students · Clash 6 · Same day 6', 'القاعة F-A: عدد الطلاب 12 · تعارض: 6 · اليوم نفسه: 6'));

  await chooseRoom(ui, 'M-B');
  assert.equal(ui.text('examRostersPaneTitle'), say('Room M-B', 'القاعة M-B'));
  assert.equal(ui.document.activeElement, room(ui, 'M-B'));
  ui.window.history.back();
  await idle();
  assert.equal(ui.address().searchParams.get('room'), 'F-A');
  assert.equal(ui.text('examRostersPaneTitle'), say('Room F-A', 'القاعة F-A'));
  assert.equal(room(ui, 'F-A').getAttribute('aria-current'), 'true');
  // The navigator was drawn again: focus is on the room Back chose, not lost.
  assert.equal(ui.document.activeElement, room(ui, 'F-A'));
  assert.equal(room(ui, 'F-A').tabIndex, 0);
  assert.equal(room(ui, 'M-B').hasAttribute('aria-current'), false);
  assert.deepEqual(ui.server.bodies('roster').map(body => body.scope.room_code), ['F-A', 'M-B', 'F-A']);
  assertNoStudentInAddress(ui);
});

test('a room\'s items read seats, sections, parts and flags as words; rooms sort by building, floor, then code', async t => {
  const ui = await rosters(t);
  const items = ui.rooms().filter(item => item.dataset.room);
  const expected = fixture.index.rooms.filter(item => item.slot_index === 0);
  assert.deepEqual(items.map(item => item.dataset.room), expected.map(item => item.room_code));
  const split = room(ui, 'M-C');
  const facts = expected.find(item => item.room_code === 'M-C');
  assert.equal(ui.text(split.querySelector('.et-nav-figure')), `${facts.seated_now}/${facts.capacity}`);
  assert.equal(ui.text(split.querySelector('.et-nav-detail')), say('MATH101 M1 · part 2 of 2', 'MATH101 M1 · الجزء 2 من 2'));
  // Part 1 of the split section sits CS and IS students: their flags as words.
  const first = expected.find(item => item.room_code === 'M-B');
  assert.ok(first.flags.clash > 0 && first.flags.same_day > 0);
  assert.equal(ui.text(room(ui, 'M-B').querySelector('.et-nav-detail')), say(
    `MATH101 M1 · part 1 of 2 · Clash ${first.flags.clash} · Same day ${first.flags.same_day}`,
    `MATH101 M1 · الجزء 1 من 2 · تعارض: ${first.flags.clash} · اليوم نفسه: ${first.flags.same_day}`));
  assert.ok(room(ui, 'M-B').querySelector('.et-nav-detail .is-clash'));
  // Codes and counts are isolated left to right; nothing guesses a direction.
  assert.equal(split.querySelector('.et-nav-code bdi').dir, 'ltr');
  assert.equal(ui.document.querySelectorAll('[dir="auto"]').length, 0);
});

test('periods, groups and Needs review narrow the rooms; an empty period says so', async t => {
  const ui = await rosters(t);
  const all = ui.rooms().length;
  ui.$('examRostersGroups').querySelector('[data-group="F"]').click();
  await idle();
  const female = fixture.index.rooms.filter(item => item.slot_index === 0 && item.gender === 'F').map(item => item.room_code);
  assert.deepEqual(ui.rooms().map(item => item.dataset.room), female);
  assert.equal(ui.$('examRostersGroups').querySelector('[data-group="F"]').getAttribute('aria-checked'), 'true');
  assert.equal(ui.document.activeElement, ui.$('examRostersGroups').querySelector('[data-group="F"]'));
  ui.$('examRostersGroups').querySelector('[data-group="all"]').click();
  await idle();
  assert.equal(ui.rooms().length, all);
  // Needs review: rooms with something to check, and the unroomed section.
  const review = ui.$('examRostersRoomReview');
  assert.equal(ui.text(review), say('Needs review (1)', 'تحتاج مراجعة: 1'));
  review.click();
  await idle();
  assert.equal(review.getAttribute('aria-pressed'), 'true');
  assert.deepEqual(ui.rooms().map(item => item.dataset.sectionGroup || item.dataset.room), ['MATH101|unmapped:U:missing|U']);
  review.click();
  await idle();
  // The Monday afternoon period: its own rooms; the day tab moves the period.
  ui.$('examRostersDays').querySelector('[data-day="Mon"]').click();
  await idle();
  const monday = ui.$('examRostersDays').querySelector('[data-day="Mon"]');
  assert.equal(ui.document.activeElement, monday);
  assert.equal(monday.getAttribute('aria-selected'), 'true');
  assert.equal(ui.address().searchParams.get('slot'), '2');
  assert.deepEqual(ui.rooms().map(item => item.dataset.room), fixture.index.rooms.filter(item => item.slot_index === 2).map(item => item.room_code));
  assert.ok(ui.history.every(([method]) => method === 'replaceState'), 'days and periods replace, never push');
  assert.equal(ui.server.requests('roster').length, 0);
});

// ── The roster ──────────────────────────────────────────────

const MATH = () => fixture.rosters['{"exam": "MATH101", "kind": "course"}'];

test('a course opens with section tabs and flag chips that carry their counts, and part groups naming rooms and ID ranges', async t => {
  const ui = await rosters(t);
  await chooseCourse(ui, 'MATH101');
  assert.deepEqual(ui.server.bodies('roster'), [{ scope: { kind: 'course', exam: 'MATH101' } }]);
  const math = MATH();
  assert.equal(ui.text('examRostersPaneTitle'), 'MATH101 · CALCULUS I');
  assert.equal(ui.text('examRostersPaneMeta'), say('Sun 08:00-10:00 · Students: 43 · Rooms: M-B, M-C, F-A', 'Sun 08:00-10:00 · عدد الطلاب: 43 · القاعات: M-B, M-C, F-A'));
  assert.equal(ui.$('examRostersShowInTimetable').hidden, false);
  assert.equal(ui.$('examRostersShowInTimetable').getAttribute('href'), `/exam-timetable/?run=${RUN}&focus=MATH101`);
  const tabs = ui.tabs();
  assert.deepEqual(tabs.map(tab => ui.text(tab)), [
    say('All 43', 'الكل 43'), 'M1 30', 'F1 12', say('Section not recorded · Not recorded 1', 'الشعبة غير مسجلة · غير مسجل 1'),
  ]);
  assert.deepEqual(tabs.map(tab => tab.getAttribute('aria-selected')), ['true', 'false', 'false', 'false']);
  assert.ok(tabs[3].classList.contains('is-unrecorded'));
  const panel = ui.$('examRostersRosterListPanel');
  assert.equal(panel.getAttribute('role'), 'tabpanel');
  assert.equal(panel.getAttribute('aria-labelledby'), tabs[0].id);
  assert.ok(tabs.every(tab => tab.getAttribute('aria-controls') === panel.id));
  assert.deepEqual(ui.chips().map(chip => ui.text(chip)), say(
    ['All 43', 'Clash 17', 'Same day 16', 'No seat 0', 'Section not recorded 1'],
    ['الكل: 43', 'تعارض: 17', 'اليوم نفسه: 16', 'بلا مقعد: 0', 'الشعبة غير مسجلة: 1']));
  assert.equal(ui.chip('all').getAttribute('aria-checked'), 'true');
  assert.equal(ui.$('examRostersRoster').querySelector('.et-flag-chips').getAttribute('role'), 'radiogroup');
  assert.equal(ui.rows().length, 43);
  assert.equal(ui.text(ui.$('examRostersRosterListCount')), say('Showing 43 of 43', 'المعروض: 43 من 43'));
  const [m1a, m1b] = math.sections[0].parts;
  const f1 = math.sections[1].parts[0];
  assert.deepEqual(ui.groupRows(), say([
    `M1 · M-B (1 of 2) · Students: 20 · IDs ${m1a.first_id}–${m1a.last_id} Split across 2 rooms by student ID: ${m1a.first_id}–${m1a.last_id} in M-B, ${m1b.first_id}–${m1b.last_id} in M-C`,
    `M1 · M-C (2 of 2) · Students: 10 · IDs ${m1b.first_id}–${m1b.last_id}`,
    `F1 · F-A · Students: 12 · IDs ${f1.first_id}–${f1.last_id}`,
    "Section not recorded · Not recorded · Not assigned · Students: 1 The imported timetables don't name a section for these students, so they have no room yet.",
  ], [
    `M1 · M-B (1 من 2) · عدد الطلاب: 20 · الأرقام ${m1a.first_id}–${m1a.last_id} موزعة حسب الرقم الجامعي (عدد القاعات: 2): من ${m1a.first_id} إلى ${m1a.last_id} في M-B, من ${m1b.first_id} إلى ${m1b.last_id} في M-C`,
    `M1 · M-C (2 من 2) · عدد الطلاب: 10 · الأرقام ${m1b.first_id}–${m1b.last_id}`,
    `F1 · F-A · عدد الطلاب: 12 · الأرقام ${f1.first_id}–${f1.last_id}`,
    'الشعبة غير مسجلة · غير مسجل · لم تُحدَّد · عدد الطلاب: 1 لا تحدد الجداول المستوردة شعبة لهؤلاء الطلاب، لذا لم تُحدَّد لهم قاعة بعد.',
  ]));
  // An ID range is one left-to-right run in either language.
  const range = [...ui.$('examRostersRoster').querySelectorAll('.et-group-facts bdi[dir="ltr"]')].find(node => node.textContent.includes('–'));
  assert.equal(range.textContent, `${m1a.first_id}–${m1a.last_id}`);
  const groupHead = ui.$('examRostersRoster').querySelector('.et-roster-group-head');
  assert.equal(groupHead.getAttribute('scope'), 'rowgroup');
  assert.equal(groupHead.getAttribute('colspan'), '7');
  // The rows are the server's rows, in seat order.
  assert.deepEqual(ui.rows().map(row => Number(row.querySelector('.et-col-id').textContent)), math.rows.map(row => row.student_id));
  assert.equal(ui.$('examRostersRoster').querySelector('caption').textContent, say(
    'MATH101, 43 students, in seat order: section, room, then student ID',
    'MATH101، عدد الطلاب: 43، بترتيب المقاعد: الشعبة ثم القاعة ثم الرقم الجامعي'));
});

test('a row shows the allowed fields only, codes left to right, a clash stripe and flags as words linking to the other exam', async t => {
  const ui = await rosters(t);
  await chooseCourse(ui, 'MATH101');
  const data = MATH().rows.find(row => row.clash && row.same_day);
  const row = ui.rows().find(item => item.querySelector('.et-col-id').textContent === String(data.student_id));
  assert.ok(row.classList.contains('is-clash'));
  const cells = [...row.children].map(cell => cell.className);
  assert.deepEqual(cells, ['et-col-number', 'et-col-id', 'et-col-name', 'et-col-program', 'et-col-section', 'et-col-room', 'et-col-flags']);
  assert.equal(row.querySelector('.et-col-id bdi').dir, 'ltr');
  assert.equal(row.querySelector('.et-col-name bdi').textContent, data.name);
  assert.equal(row.querySelector('.et-col-name bdi').hasAttribute('dir'), false, 'names keep their own direction');
  assert.equal(ui.text(row.querySelector('.et-col-room')), say(`${data.room} ${data.part} of 2`, `${data.room} ${data.part} من 2`));
  const [clash, sameDay] = row.querySelectorAll('.et-col-flags a.et-flag');
  assert.equal(clash.dataset.exam, data.clash_with[0]);
  assert.equal(clash.getAttribute('href'), `/exam-timetable/rosters/?run=${RUN}&view=course&course=${data.clash_with[0]}`);
  assert.equal(clash.querySelector('.visually-hidden').textContent, say(
    ` — Another exam at the same time: ${data.clash_with[0]}, Sun 08:00-10:00`,
    ` — اختبار آخر في الوقت نفسه: ${data.clash_with[0]}، Sun 08:00-10:00`));
  assert.ok(ui.text(clash).startsWith(say(`Clash · ${data.clash_with[0]}`, `تعارض · ${data.clash_with[0]}`)));
  assert.ok(ui.text(sameDay).startsWith(say(`Same day · ${data.same_day_with[0]}`, `اليوم نفسه · ${data.same_day_with[0]}`)));
  // A clash link opens the other exam's list here, as a history entry.
  const before = ui.window.history.length;
  // A modified click is the browser's (a new tab): the page leaves it be.
  assert.equal(ui.follow(clash, { ctrlKey: true }), true);
  assert.equal(ui.window.history.length, before);
  assert.equal(ui.follow(clash), false, 'a plain click is handled here');
  await idle();
  assert.equal(ui.window.history.length, before + 1);
  assert.equal(ui.address().searchParams.get('course'), data.clash_with[0]);
  assert.equal(ui.text('examRostersPaneTitle').split(' · ')[0], data.clash_with[0]);
  assert.equal(ui.document.activeElement, ui.$('examRostersPaneTitle'));
  // The one row without a section: a dash, no room, and the words why.
  ui.window.history.back();
  await idle();
  const missing = MATH().rows.find(item => item.section_status !== 'mapped');
  const unrecorded = ui.rows().find(item => item.querySelector('.et-col-id').textContent === String(missing.student_id));
  assert.equal(ui.text(unrecorded.querySelector('.et-col-section')), '—');
  assert.equal(ui.text(unrecorded.querySelector('.et-col-room')), say('Not assigned', 'لم تُحدَّد'));
  assert.ok(ui.text(unrecorded.querySelector('.et-col-flags')).startsWith(say('Section not recorded', 'الشعبة غير مسجلة')));
});

test('flag chips filter the list, say the count politely, and turn an empty result into good news', async t => {
  const ui = await rosters(t);
  await chooseCourse(ui, 'MATH101');
  ui.chip('clash').click();
  await idle();
  assert.equal(ui.chip('clash').getAttribute('aria-checked'), 'true');
  assert.equal(ui.document.activeElement, ui.chip('clash'));
  assert.equal(ui.rows().length, 17);
  assert.ok(ui.rows().every(row => row.classList.contains('is-clash')));
  assert.equal(ui.text(ui.$('examRostersRosterListCount')), say('Showing 17 of 43', 'المعروض: 17 من 43'));
  assert.equal(ui.text('examRostersLive'), say('Showing 17 of 43', 'المعروض: 17 من 43'));
  // Chips are a radiogroup: arrows choose, mirrored right to left.
  ui.key(ui.chip('clash'), AR ? 'ArrowLeft' : 'ArrowRight');
  await idle();
  assert.equal(ui.chip('same_day').getAttribute('aria-checked'), 'true');
  assert.equal(ui.rows().length, 16);
  ui.chip('no_seat').click();
  await idle();
  assert.equal(ui.rows().length, 0);
  const empty = ui.$('examRostersRoster').querySelector('.et-roster-empty');
  assert.equal(empty.hidden, false);
  assert.ok(empty.classList.contains('is-good'));
  assert.equal(ui.text(empty), say('✓ Every student in MATH101 has a seat.', '✓ لكل طالب في MATH101 مقعد.'));
  assert.equal(empty.querySelector('.et-roster-clear'), null, 'good news needs no clearing');
  // A text filter with no match is not good news: it offers Clear filters.
  ui.chip('all').click();
  await idle();
  const filter = ui.$('examRostersRoster').querySelector('.et-roster-filter');
  ui.type(filter, 'zzzz');
  await idle();
  assert.equal(ui.text(empty.querySelector('p')), say('No students match these filters.', 'لا يوجد طلاب مطابقون لهذه التصفية.'));
  assert.equal(ui.text(empty.querySelector('.et-roster-clear')), say('Clear filters', 'مسح التصفية'));
  assert.equal(empty.classList.contains('is-good'), false);
  empty.querySelector('.et-roster-clear').click();
  await idle();
  assert.equal(filter.value, '');
  assert.equal(ui.rows().length, 43);
  assert.equal(ui.server.requests('roster').length, 1, 'filtering asks the server nothing (and writes no audit row)');
});

test('section tabs choose a section with arrows (mirrored right to left), Home and End; the chips count within it', async t => {
  const ui = await rosters(t);
  await chooseCourse(ui, 'MATH101');
  ui.tabs()[1].click();
  await idle();
  assert.equal(ui.tabs()[1].getAttribute('aria-selected'), 'true');
  assert.equal(ui.document.activeElement, ui.tabs()[1]);
  assert.equal(ui.rows().length, 30);
  assert.equal(ui.text(ui.chip('all')), say('All 30', 'الكل: 30'));
  assert.equal(ui.$('examRostersRosterListPanel').getAttribute('aria-labelledby'), ui.tabs()[1].id);
  ui.key(ui.tabs()[1], AR ? 'ArrowLeft' : 'ArrowRight');
  await idle();
  assert.equal(ui.tabs()[2].getAttribute('aria-selected'), 'true');
  assert.equal(ui.rows().length, 12);
  ui.key(ui.tabs()[2], 'End');
  await idle();
  assert.equal(ui.tabs()[3].getAttribute('aria-selected'), 'true');
  ui.key(ui.tabs()[3], 'Home');
  await idle();
  assert.equal(ui.tabs()[0].getAttribute('aria-selected'), 'true');
  assert.equal(ui.document.activeElement, ui.tabs()[0]);
  ui.key(ui.tabs()[0], AR ? 'ArrowRight' : 'ArrowLeft');
  await idle();
  assert.equal(ui.tabs()[3].getAttribute('aria-selected'), 'true', 'back from the first wraps to the last');
});

test('a column header sorts (aria-sort ascending, descending, then seat order) and a flat list loses its part groups', async t => {
  const ui = await rosters(t);
  await chooseCourse(ui, 'MATH101');
  const header = key => ui.$('examRostersRoster').querySelector(`button[data-sort="${key}"]`);
  const ids = () => ui.rows().map(row => Number(row.querySelector('.et-col-id').textContent));
  header('id').click();
  await idle();
  assert.equal(header('id').closest('th').getAttribute('aria-sort'), 'ascending');
  assert.deepEqual(ids(), [...ids()].sort((a, b) => a - b));
  assert.equal(ui.groupRows().length, 0);
  assert.equal(ui.document.activeElement, header('id'));
  assert.match(ui.$('examRostersRoster').querySelector('caption').textContent, say(/sorted by Student ID$/, /مرتبة حسب الرقم الجامعي$/));
  header('id').click();
  await idle();
  assert.equal(header('id').closest('th').getAttribute('aria-sort'), 'descending');
  assert.deepEqual(ids(), [...ids()].sort((a, b) => b - a));
  header('flags').click();
  await idle();
  assert.equal(header('id').closest('th').getAttribute('aria-sort'), 'none');
  const clashes = ui.rows().map(row => row.classList.contains('is-clash'));
  assert.deepEqual(clashes, [...clashes].sort((a, b) => Number(b) - Number(a)), 'clashes first');
  header('flags').click();
  await idle();
  header('flags').click();
  await idle();
  assert.equal(header('flags').closest('th').getAttribute('aria-sort'), 'none');
  assert.deepEqual(ids(), MATH().rows.map(row => row.student_id), 'the third press returns to seat order');
  assert.equal(ui.groupRows().length, 4);
});

test('the ID or name filter matches ID prefixes and every word of a name; Esc clears it first', async t => {
  const ui = await rosters(t);
  await chooseCourse(ui, 'MATH101');
  const filter = ui.$('examRostersRoster').querySelector('.et-roster-filter');
  assert.equal(filter.getAttribute('aria-label'), say('Filter by ID or name', 'تصفية بالرقم الجامعي أو الاسم'));
  ui.type(filter, '440200');
  await idle();
  assert.deepEqual(ui.rows().map(row => row.querySelector('.et-col-id').textContent), MATH().rows.map(row => String(row.student_id)).filter(id => id.startsWith('440200')));
  ui.type(filter, 'student   4401005 testname');
  await idle();
  assert.deepEqual(ui.rows().map(row => row.querySelector('.et-col-id').textContent), ['4401005']);
  ui.key(filter, 'Escape');
  await idle();
  assert.equal(filter.value, '');
  assert.equal(ui.rows().length, 43);
});

const NO_SEAT_NOTE = say(
  'No seat: this section has more students now than when the timetable was saved. Rooms were sized for the saved count, and seats go in student ID order.',
  'بلا مقعد: عدد طلاب الشعبة الآن أكبر مما كان عند حفظ الجدول. حُددت القاعات للعدد المحفوظ، وتُوزَّع المقاعد بترتيب الأرقام الجامعية.');
const NO_SEAT_FULL = say(
  ' — No seat: this section has more students now than when the timetable was saved, and seats go in student ID order.',
  ' — بلا مقعد: عدد طلاب الشعبة الآن أكبر مما كان عند حفظ الجدول، وتُوزَّع المقاعد بترتيب الأرقام الجامعية.');
const CHANGED_NOTE = say('Enrolment in M1 changed since saving: 30 then, 31 now. Rooms were sized for 30.',
  'تغيّر عدد المسجلين في M1 بعد الحفظ: 30 عند الحفظ و31 الآن. حُسبت القاعات على 30.');
const NEW_NOTE = say('M9 is new since saving; no seats were sized for its students.', 'M9 جديدة بعد الحفظ، ولم يُحسب لطلابها مكان في القاعات.');
const CHANGED_MATH = () => H.rosterOf({ kind: 'course', exam: 'MATH101' }, { changed: true });

test('a section changed since the save is marked in its tab, part group and rows; its No seat student is the highest ID, and nothing claims when anyone enrolled', async t => {
  const ui = await rosters(t, { server: rosterServer({ changed: true }) });
  assert.equal(ui.text(ui.$('examRostersCheck')), say('· ≠ Sections changed since saving: 2', '· ≠ الشعب المتغيرة بعد الحفظ: 2'));
  assert.equal(ui.$('examRostersCheck').querySelector('[data-check]').dataset.check, 'changed');
  await chooseCourse(ui, 'MATH101');
  const tab = label => ui.tabs().find(item => item.querySelector('.et-tab-label').textContent === label);
  assert.ok(tab('M1').classList.contains('is-changed'));
  assert.ok(tab('M1').querySelector('.et-tab-dot'));
  assert.equal(tab('M1').querySelector('.visually-hidden').textContent, say(' changed', ' متغيرة'));
  assert.equal(tab('M9').querySelector('.visually-hidden').textContent, say(' new', ' جديدة'));
  assert.equal(ui.text(ui.chip('no_seat')), say('No seat 3', 'بلا مقعد: 3'));
  tab('M1').click();
  await idle();
  ui.chip('no_seat').click();
  await idle();
  // A LOWER ID than every member joined after the save; seats go in ID order,
  // so the one left without a seat is an original member, the highest ID -
  // and the late joiner has a seat.
  const math = CHANGED_MATH();
  const m1 = math.rows.filter(item => item.section === 'M1');
  const late = m1.find(item => item.name === 'LATE JOINER');
  assert.ok(['whole', 'split'].includes(late.room_basis));
  assert.ok(late.student_id < Math.min(...m1.filter(item => item !== late).map(item => item.student_id)));
  const [row] = ui.rows();
  assert.equal(ui.rows().length, 1);
  assert.equal(Number(row.querySelector('.et-col-id').textContent), Math.max(...m1.map(item => item.student_id)));
  assert.equal(ui.text(row.querySelector('.et-col-room')), say('No seat', 'بلا مقعد'));
  assert.ok(row.querySelector('.et-col-room .et-room-noseat'));
  assert.ok(ui.text(row.querySelector('.et-col-section')).endsWith(say('changed', 'متغيرة')));
  // Why, as the file says it: a fact of the section, never of the student.
  assert.equal(row.querySelector('.et-flag--noseat .visually-hidden').textContent, NO_SEAT_FULL);
  assert.deepEqual(ui.groupRows(), [`M1 · ${say('No seat · Students: 1', 'بلا مقعد · عدد الطلاب: 1')} ${CHANGED_NOTE} ${NO_SEAT_NOTE}`]);
  // The new section: both its students have no seat, and it says it is new.
  tab('M9').click();
  await idle();
  assert.equal(ui.rows().length, 2);
  assert.deepEqual(ui.groupRows(), [`M9 · ${say('No seat · Students: 2', 'بلا مقعد · عدد الطلاب: 2')} ${NEW_NOTE}`]);
  // In the whole list the section's change is said once, on its first part.
  tab(say('All', 'الكل')).click();
  await idle();
  ui.chip('all').click();
  await idle();
  const groups = ui.groupRows();
  assert.ok(groups[0].includes(CHANGED_NOTE));
  assert.equal(groups.filter(text => text.includes(CHANGED_NOTE)).length, 1);
  assert.ok(groups.find(text => text.includes(NO_SEAT_NOTE)).startsWith(`M1 · ${say('No seat', 'بلا مقعد')}`));
  assert.ok(ui.$('examRostersRoster').querySelector('.et-roster-group-head').classList.contains('is-changed'));
  assert.equal(/enrolled|سُجّل/.test(ui.text('examRostersRoster')), false, 'no claim about when anyone enrolled');
});

test('By room lists students with no seat under their period, never in a room; each opens its section at its No seat rows, as a history entry', async t => {
  const ui = await rosters(t, { server: rosterServer({ changed: true }) });
  const index = fixture.changed.index;
  const slotRooms = index.rooms.filter(item => item.slot_index === 0);
  // A room item counts only its own list, which never holds a student without a seat.
  for (const item of ui.rooms().filter(node => node.dataset.room)) {
    assert.equal(/No seat|بلا مقعد/.test(ui.text(item)), false, item.dataset.room);
  }
  const headings = [...ui.$('examRostersRooms').querySelectorAll('.et-nav-heading')].map(node => ui.text(node));
  // Each heading names its unit: one section is not roomed; three students have no seat.
  assert.deepEqual(headings, [say('Not assigned · 1 section', 'لم تُحدَّد لها قاعة · عدد الشعب: 1'), say('No seat · 3 students', 'بلا مقعد · عدد الطلاب: 3')]);
  const noSeat = ui.rooms().filter(item => item.dataset.from === 'no_seat');
  assert.deepEqual(noSeat.map(item => [ui.text(item.querySelector('.et-nav-code')), ui.text(item.querySelector('.et-nav-figure')), ui.text(item.querySelector('.et-nav-detail'))]), [
    ['MATH101 M1', '1', say('No seat 1 · changed', 'بلا مقعد: 1 · متغيرة')],
    ['MATH101 M9', '2', say('No seat 2 · new', 'بلا مقعد: 2 · جديدة')],
  ]);
  const review = slotRooms.filter(item => item.review.length).length + index.not_assigned.length + index.no_seat.length;
  assert.equal(ui.text('examRostersRoomReview'), say(`Needs review (${review})`, `تحتاج مراجعة: ${review}`));
  ui.$('examRostersRoomReview').click();
  await idle();
  assert.ok(ui.rooms().some(item => item.dataset.from === 'no_seat'), 'they need review');
  ui.$('examRostersRoomReview').click();
  await idle();
  // The new section: its list opens on its No seat rows, and is a history entry.
  const before = ui.window.history.length;
  const m9 = ui.rooms().find(item => item.dataset.from === 'no_seat' && ui.text(item).includes('M9'));
  const m9Item = index.no_seat.find(item => item.section === 'M9');
  m9.click();
  await idle();
  assert.deepEqual(ui.server.bodies('roster').at(-1), { scope: { kind: 'section', exam: 'MATH101', section_key: m9Item.section_key, gender: 'M' } });
  assert.equal(ui.window.history.length, before + 1);
  assert.equal(ui.text('examRostersPaneTitle'), 'MATH101 · CALCULUS I · M9');
  assert.equal(ui.chip('no_seat').getAttribute('aria-checked'), 'true');
  assert.equal(ui.rows().length, m9Item.no_seat);
  assert.equal(ui.rooms().find(item => item.dataset.from === 'no_seat' && ui.text(item).includes('M9')).getAttribute('aria-current'), 'true');
  assert.equal(ui.address().searchParams.has('section'), false, 'the address keeps run, view and period only');
  // The changed section: its one No seat row, the section's words for why.
  const m1 = ui.rooms().find(item => item.dataset.from === 'no_seat' && ui.text(item).includes('M1'));
  m1.click();
  await idle();
  assert.equal(ui.rows().length, 1);
  assert.ok(ui.groupRows()[0].endsWith(NO_SEAT_NOTE));
  // Back returns to the new section's list, as it was shown.
  ui.window.history.back();
  await idle();
  assert.equal(ui.text('examRostersPaneTitle'), 'MATH101 · CALCULUS I · M9');
  assert.equal(ui.chip('no_seat').getAttribute('aria-checked'), 'true');
  assert.equal(ui.document.activeElement, ui.rooms().find(item => item.getAttribute('aria-current') === 'true'));
  // A room of the same period, same build: no chip that could promise a seat to all.
  await chooseRoom(ui, 'M-B');
  assert.equal(ui.chip('no_seat'), undefined);
  assert.equal(H.rosterOf({ kind: 'room', slot_index: 0, room_code: 'M-B' }, { changed: true }).counts.no_seat, 0);
  assertNoStudentInAddress(ui);
});

// ── By course ───────────────────────────────────────────────

test('By course lists exams under their periods and filters them by program, clashes and review, or sorts them flat', async t => {
  const ui = await rosters(t);
  ui.$('examRostersViews').querySelector('[data-view="course"]').click();
  await idle();
  assert.equal(ui.$('examRostersViews').querySelector('[data-view="course"]').getAttribute('aria-checked'), 'true');
  assert.equal(ui.$('examRostersByRoom').hidden, true);
  assert.deepEqual([...ui.address().searchParams.entries()], [['run', String(RUN)], ['view', 'course']]);
  const headings = () => [...ui.$('examRostersCourses').querySelectorAll('.et-nav-heading')].map(node => ui.text(node));
  assert.deepEqual(headings(), ['Sun 08:00-10:00 · 2', 'Sun 13:00-15:00 · 1', 'Mon 08:00-10:00 · 1', 'Mon 13:00-15:00 · 1']);
  for (const node of ui.$('examRostersCourses').querySelectorAll('.et-nav-heading')) {
    assert.deepEqual([...node.childNodes].map(child => child.nodeName), ['SPAN'], 'the words are one inline run');
  }
  assert.deepEqual(ui.courses().map(item => item.dataset.course), ['IS201', 'MATH101', 'CS101', 'PHYS103 (1)', 'PHYS103 (2)']);
  const math = ui.courses().find(item => item.dataset.course === 'MATH101');
  assert.equal(ui.text(math.querySelector('.et-nav-figure')), '43');
  assert.equal(math.querySelector('.et-nav-figure').getAttribute('aria-label'), say('43 students', 'عدد الطلاب: 43'));
  assert.equal(ui.text(math.querySelector('.et-nav-detail')), say(
    'CALCULUS I · Clash 17 · Same day 16 · Section not recorded · No room', 'CALCULUS I · تعارض: 17 · اليوم نفسه: 16 · الشعبة غير مسجلة · لم تُحدَّد قاعة'));
  const program = ui.$('examRostersProgram');
  assert.deepEqual([...program.options].map(option => option.textContent), [say('All programs', 'جميع البرامج'), 'AI · 10', 'CS · 11', 'CS2 · 6', 'IS · 16']);
  program.value = 'AI';
  program.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
  await idle();
  assert.deepEqual(ui.courses().map(item => item.dataset.course), ['MATH101']);
  program.value = '';
  program.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
  await idle();
  const clash = ui.$('examRostersHasClash');
  assert.equal(ui.text(clash), say('Has clashes (2)', 'فيها تعارض: 2'));
  clash.click();
  await idle();
  assert.equal(clash.getAttribute('aria-pressed'), 'true');
  assert.deepEqual(ui.courses().map(item => item.dataset.course), ['IS201', 'MATH101']);
  clash.click();
  await idle();
  ui.$('examRostersCourseReview').click();
  await idle();
  assert.deepEqual(ui.courses().map(item => item.dataset.course), ['MATH101']);
  ui.$('examRostersCourseReview').click();
  await idle();
  const sort = ui.$('examRostersSort');
  sort.value = 'students';
  sort.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
  await idle();
  assert.deepEqual(headings(), [], 'a flat sort has no period headings');
  const students = fixture.index.exams.map(exam => [exam.code, exam.students]).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'en', { numeric: true }));
  assert.deepEqual(ui.courses().map(item => item.dataset.course), students.map(([code]) => code));
  // One tab stop in the list; Up and Down move it; Home and End jump.
  const items = ui.courses();
  assert.deepEqual(items.map(item => item.tabIndex), items.map((_, i) => (i === 0 ? 0 : -1)));
  items[0].focus();
  ui.key(items[0], 'ArrowDown');
  assert.equal(ui.document.activeElement, items[1]);
  assert.equal(items[1].tabIndex, 0);
  ui.key(items[1], 'End');
  assert.equal(ui.document.activeElement, items[items.length - 1]);
  ui.key(items[items.length - 1], 'Home');
  assert.equal(ui.document.activeElement, items[0]);
});

test('an address naming a course or a room opens it; one naming what the run lacks opens nothing', async t => {
  const byCourse = await rosters(t, { url: `${PAGE_URL}&view=course&course=CS101` });
  assert.deepEqual(byCourse.server.bodies('roster'), [{ scope: { kind: 'course', exam: 'CS101' } }]);
  assert.equal(byCourse.courses().find(item => item.dataset.course === 'CS101').getAttribute('aria-current'), 'true');
  const byRoom = await rosters(t, { url: `${PAGE_URL}&view=room&slot=1&room=M-A` });
  const scope = byRoom.server.bodies('roster')[0].scope;
  assert.deepEqual(scope, { kind: 'room', slot_index: 1, room_code: 'M-A' });
  assert.equal(byRoom.$('examRostersDays').querySelector('[aria-selected="true"]').dataset.day, 'Sun');
  assert.equal(byRoom.$('examRostersPeriods').querySelector('[aria-checked="true"]').dataset.slot, '1');
  const unknown = await rosters(t, { url: `${PAGE_URL}&view=room&slot=0&room=NOPE` });
  assert.equal(unknown.server.requests('roster').length, 0);
  assert.equal(unknown.address().searchParams.has('room'), false);
});

// ── Find and the student lookup ─────────────────────────────

test('Find matches courses and rooms at once and students by one POST once typing settles; nothing reaches the address', async t => {
  const ui = await rosters(t);
  const find = ui.$('examRostersFind');
  assert.equal(find.getAttribute('role'), 'combobox');
  find.focus();
  // Keystrokes in a row: courses match at once, students wait for the pause.
  for (const typed of ['m', 'ma', 'mat', 'math']) ui.type(find, typed);
  const list = ui.$('examRostersFindList');
  assert.equal(list.hidden, false);
  assert.equal(find.getAttribute('aria-expanded'), 'true');
  assert.equal(list.getAttribute('role'), 'listbox');
  assert.deepEqual([...list.querySelectorAll('[role="option"]')].map(node => ui.text(node)), ['MATH101 · CALCULUS I · Sun 08:00-10:00']);
  assert.equal(ui.text(list.querySelector('.et-find-note')), say('Looking for students…', 'جارٍ البحث عن الطلاب…'));
  assert.equal(ui.server.requests('lookup').length, 0);
  await idle();
  assert.deepEqual(ui.server.bodies('lookup'), [{ query: 'math' }], 'a settled name search is one POST, not one per keystroke');
  assert.deepEqual([...list.querySelectorAll('.et-find-group')].map(node => ui.text(node)), [say('Courses', 'المقررات')]);
  ui.type(find, 'M-');
  await idle();
  const rooms = [...list.querySelectorAll('[role="option"]')].map(node => ui.text(node));
  const expected = fixture.index.rooms.filter(room => room.room_code.startsWith('M-')).length;
  assert.equal(rooms.length, Math.min(8, expected));
  assert.ok(rooms[0].startsWith('M-A · Sun 08:00-10:00 · '));
  // Digits: an ID prefix of four or more is a student search.
  ui.type(find, '44010');
  await idle();
  assert.deepEqual(ui.server.bodies('lookup').at(-1), { query: '44010' });
  const options = [...list.querySelectorAll('[role="option"]')];
  const matches = fixture.searches['44010'].matches;
  assert.equal(options.length, matches.length);
  assert.equal(ui.text(options[0]), say(
    `${matches[0].student_id} · ${matches[0].name} · CS · Exams: ${matches[0].exams}`,
    `${matches[0].student_id} · ${matches[0].name} · CS · عدد الاختبارات: ${matches[0].exams}`));
  ui.key(find, 'ArrowDown');
  ui.key(find, 'ArrowDown');
  assert.equal(find.getAttribute('aria-activedescendant'), options[1].id);
  assert.equal(options[1].getAttribute('aria-selected'), 'true');
  const historyBefore = ui.window.history.length;
  const addressBefore = ui.window.location.href;
  ui.key(find, 'Enter');
  await idle();
  const chosen = matches[1].student_id;
  assert.deepEqual(ui.server.bodies('lookup').at(-1), { student_id: String(chosen) });
  assert.equal(find.value, '', 'the ID leaves the field once chosen');
  assert.equal(ui.window.history.length, historyBefore, 'a lookup is not a history entry');
  assert.equal(ui.window.location.href, addressBefore);
  assert.ok(ui.history.every(entry => !JSON.stringify(entry).includes(String(chosen))));
  assertNoStudentInAddress(ui);
  assert.ok(ui.server.requests('lookup').every(call => call.options.method === 'POST' && !call.url.includes('?')));
});

test('a lookup lists the student\'s every exam with room and flags; Open goes to that list, and Back or Esc return to the room', async t => {
  const ui = await rosters(t);
  await chooseRoom(ui, 'F-A');
  const find = ui.$('examRostersFind');
  find.focus();
  ui.type(find, '4402');
  await idle();
  const option = [...ui.$('examRostersFindList').querySelectorAll('[role="option"]')][0];
  const id = fixture.searches['4402'].matches[0].student_id;
  option.click();
  await idle();
  const answer = fixture.lookups[String(id)];
  assert.equal(ui.$('examRostersRoster').hidden, true);
  assert.equal(ui.text('examRostersPaneTitle'), say(`Student ${id}`, `الطالب ${id}`));
  assert.equal(ui.document.activeElement, ui.$('examRostersPaneTitle'));
  assert.equal(ui.text('examRostersPaneMeta'), `${answer.rows[0].name} · ${answer.rows[0].program}`);
  const lookupRows = [...ui.$('examRostersLookup').querySelectorAll('tbody tr')];
  assert.equal(lookupRows.length, answer.rows.length);
  assert.equal(ui.text(ui.$('examRostersLookup').querySelector('.et-roster-count')), say(
    `Exams in timetable #${RUN}: ${answer.rows.length} · Clash ${answer.counts.clash} · Same day ${answer.counts.same_day}`,
    `الاختبارات في الجدول رقم ${RUN}: ${answer.rows.length} · تعارض: ${answer.counts.clash} · اليوم نفسه: ${answer.counts.same_day}`));
  const first = answer.rows[0];
  const exam = answer.exams[first.exam];
  const cells = [...lookupRows[0].children];
  assert.deepEqual([0, 1, 3, 4].map(i => ui.text(cells[i])), [exam.day, exam.period, first.section, first.room]);
  assert.deepEqual([ui.text(cells[2].querySelector(':scope > bdi')), ui.text(cells[2].querySelector('.et-lookup-name'))], [first.exam, exam.name]);
  // A narrow pane shows each exam as a card: when, section and room under the course.
  assert.equal(ui.text(cells[2].querySelector('.et-lookup-sub')), `${exam.day} ${exam.period} · ${first.section} · ${first.room}`);
  assert.deepEqual(cells.map(cell => cell.className), ['et-col-day', 'et-col-period', 'et-col-name', 'et-col-section', 'et-col-room', 'et-col-flags', 'et-col-open']);
  assert.ok(lookupRows.every(row => row.classList.contains('et-lookup-row') && !row.classList.contains('et-roster-row')));
  // One way back at a time: the pane's ‹ Rooms hides while the lookup's Back leads.
  assert.equal(ui.$('examRostersScreenBack').hidden, true);
  assert.equal(ui.text('examRostersLookupBackText'), say('Back to Room F-A', 'العودة إلى القاعة F-A'));
  // Esc returns to the room list, which never left.
  ui.key(ui.document.body, 'Escape');
  await idle();
  assert.equal(ui.$('examRostersScreenBack').hidden, false);
  assert.equal(ui.$('examRostersRoster').hidden, false);
  assert.equal(ui.$('examRostersLookup').hidden, true);
  assert.equal(ui.text('examRostersPaneTitle'), say('Room F-A', 'القاعة F-A'));
  assert.equal(ui.document.activeElement, ui.$('examRostersPaneTitle'));
  assert.equal(ui.server.requests('roster').length, 1, 'returning asks for nothing again');
  // Open from a lookup goes to that exam's list, as a history entry.
  find.focus();
  ui.type(find, '4402');
  await idle();
  ui.$('examRostersFindList').querySelector('[role="option"]').click();
  await idle();
  const open = ui.$('examRostersLookup').querySelector('[data-open-course]');
  assert.equal(open.getAttribute('aria-label'), say(`Open the list for ${first.exam}`, `فتح قائمة ${first.exam}`));
  open.click();
  await idle();
  assert.equal(ui.address().searchParams.get('course'), first.exam);
  assert.equal(ui.$('examRostersLookup').hidden, true);
  assertNoStudentInAddress(ui);
});

test('a lookup opened before any list goes straight to the exam Open names, and Back from the pane returns to the navigator', async t => {
  const ui = await rosters(t, { narrow: true });
  const find = ui.$('examRostersFind');
  find.focus();
  ui.type(find, '4401');
  await idle();
  ui.key(find, 'Enter');
  await idle();
  assert.equal(ui.$('examRostersLayout').dataset.screen, 'pane', 'a lookup is the pane on a phone');
  assert.equal(ui.text('examRostersLookupBackText'), say('Back', 'رجوع'));
  const open = ui.$('examRostersLookup').querySelector('[data-open-course]');
  const code = open.dataset.openCourse;
  open.click();
  await idle();
  assert.equal(ui.text('examRostersPaneTitle').split(' · ')[0], code);
  assert.ok(ui.rows().length > 0, 'the list arrives');
  assert.equal(ui.$('examRostersLookup').hidden, true);
  ui.window.history.back();
  await idle();
  assert.equal(ui.$('examRostersLayout').dataset.screen, 'nav');
  assert.equal(ui.$('examRostersPaneHead').hidden, true);
});

test('Esc in Find clears the field and closes the list; an ID the run lacks says so plainly', async t => {
  const ui = await rosters(t);
  const find = ui.$('examRostersFind');
  find.focus();
  ui.type(find, 'zzzz');
  await idle();
  assert.equal(ui.text(ui.$('examRostersFindList')), say('No course, room or student matches “zzzz”.', 'لا يوجد مقرر أو قاعة أو طالب يطابق «zzzz».'));
  ui.key(find, 'Escape');
  assert.equal(find.value, '');
  assert.equal(ui.$('examRostersFindList').hidden, true);
  assert.equal(find.getAttribute('aria-expanded'), 'false');
  // Too short to search students: the hint says what is needed.
  ui.type(find, '44');
  await idle();
  assert.ok(ui.text(ui.$('examRostersFindList')).endsWith(say(
    'Type at least 4 digits of a student ID, or 3 letters of a name (2 or more in each word).',
    'اكتب 4 أرقام على الأقل من الرقم الجامعي، أو 3 أحرف من الاسم (حرفان على الأقل في كل كلمة).')));
  assert.equal(ui.server.requests('lookup').length, 1, 'only the settled four-letter search went out');
});

test('/ focuses Find unless typing or switched off; the switch is remembered', async t => {
  const ui = await rosters(t);
  const find = ui.$('examRostersFind');
  ui.key(ui.document.body, '/');
  assert.equal(ui.document.activeElement, find);
  await chooseCourse(ui, 'MATH101');
  const filter = ui.$('examRostersRoster').querySelector('.et-roster-filter');
  filter.focus();
  assert.equal(ui.key(filter, '/'), true, 'typing a slash in a field is typing');
  assert.equal(ui.document.activeElement, filter);
  const shortcut = ui.$('examRostersShortcut');
  shortcut.click();
  assert.equal(shortcut.checked, false);
  assert.equal(ui.$('examRostersFindKey').hidden, true);
  ui.document.body.focus();
  ui.key(ui.document.body, '/');
  assert.notEqual(ui.document.activeElement, find);
  assert.equal(JSON.parse(ui.window.localStorage.getItem('exam-rosters')).shortcut, false);
});

// ── Export to Excel: the phase-1 dialog, preset ─────────────

const checkedScope = ui => ui.document.querySelector('input[name="examStudentScope"]:checked')?.value;

test('Export to Excel opens the phase-1 dialog on its heading, preset to the room on screen, and focus returns to it', async t => {
  const ui = await rosters(t);
  await chooseRoom(ui, 'M-C');
  const button = ui.$('examRostersExport');
  assert.equal(button.getAttribute('aria-haspopup'), 'dialog');
  button.click();
  await idle();
  const dialog = ui.$('examStudentExportDialog');
  assert.equal(dialog.open, true);
  assert.equal(ui.document.activeElement, ui.$('examStudentExportTitle'));
  assert.equal(checkedScope(ui), 'room');
  assert.equal(ui.$('examStudentRoomPeriod').value, '0');
  assert.equal(ui.$('examStudentRoom').value, 'M-C');
  assert.equal(ui.$('examStudentPeriod').value, '0', 'the two period pickers agree');
  // The page lends a partial run: no saved "about" figures, the preflight's instead.
  assert.equal(ui.document.querySelector('[data-scope-count="room"]').textContent, '');
  assert.equal(ui.text('examStudentExportSource'), say(
    `Timetable #${RUN} “${fixture.index.run.label}” · saved ${ui.text('examRostersRun').split(' · ')[2].replace('saved ', '')} · 1448 term 1`,
    `الجدول رقم ${RUN} «${fixture.index.run.label}» · حُفظ ${ui.text('examRostersRun').split(' · ')[2].replace('حُفظ في ', '')} · الفصل 1 من عام 1448`));
  const preflight = ui.server.bodies('preflight')[0];
  assert.deepEqual(preflight.scope, { kind: 'room', slot_index: 0, room_code: 'M-C' });
  assert.deepEqual(preflight.groups, ['M', 'F', 'U'].filter(group => fixture.index.rooms.some(room => room.gender === group) || fixture.index.not_assigned.some(item => item.gender === group)));
  ui.$('examStudentExportCancel').click();
  assert.equal(dialog.open, false);
  assert.equal(ui.document.activeElement, button);
});

test('Export presets both period pickers to the period of the room', async t => {
  const ui = await rosters(t);
  ui.$('examRostersPeriods').querySelector('[data-slot="1"]').click();
  await idle();
  await chooseRoom(ui, 'M-A');
  ui.$('examRostersExport').click();
  await idle();
  assert.equal(checkedScope(ui), 'room');
  assert.deepEqual([ui.$('examStudentRoomPeriod').value, ui.$('examStudentPeriod').value, ui.$('examStudentRoom').value], ['1', '1', 'M-A']);
});

test('Export presets the course, or the section tab chosen in it', async t => {
  const ui = await rosters(t);
  await chooseCourse(ui, 'MATH101');
  ui.$('examRostersExport').click();
  await idle();
  assert.equal(checkedScope(ui), 'course');
  assert.equal(ui.$('examStudentCourse').value, 'MATH101');
  assert.deepEqual(ui.server.bodies('preflight')[0].scope, { kind: 'course', exam: 'MATH101' });
  ui.$('examStudentExportCancel').click();
  ui.tabs()[2].click();
  await idle();
  ui.$('examRostersExport').click();
  await idle();
  assert.equal(checkedScope(ui), 'section');
  assert.equal(ui.$('examStudentSectionExam').value, 'MATH101');
  assert.deepEqual(JSON.parse(ui.$('examStudentSection').value), ['MATH101', 'term-section:2', 'F']);
  assert.deepEqual(ui.server.bodies('preflight').at(-1).scope, { kind: 'section', exam: 'MATH101', section_key: 'term-section:2', gender: 'F' });
});

test('with nothing chosen Export presets the period; before the navigator answers it waits', async t => {
  const held = hold();
  const server = rosterServer();
  server.queue('index', held.answer);
  const ui = await rosters(t, { server });
  assert.equal(ui.$('examRostersExport').getAttribute('aria-disabled'), 'true');
  ui.$('examRostersExport').click();
  await idle();
  assert.equal(ui.$('examStudentExportDialog').open, false);
  held.release(json(fixture.index));
  await idle();
  assert.equal(ui.$('examRostersExport').getAttribute('aria-disabled'), 'false');
  ui.$('examRostersExport').click();
  await idle();
  assert.equal(checkedScope(ui), 'period');
  assert.equal(ui.$('examStudentPeriod').value, '0');
});

// ── States ──────────────────────────────────────────────────

test('a failed audit shows no student, says why, and Try again asks again', async t => {
  const server = rosterServer();
  server.queue('roster', json({ ok: false, code: 'audit_unavailable', error: "Couldn't record this access, so the list wasn't shown. Try again." }, 503));
  const ui = await rosters(t, { server });
  await chooseRoom(ui, 'F-A');
  const alert = ui.$('examRostersRoster').querySelector('.et-roster-error');
  assert.equal(alert.getAttribute('role'), 'alert');
  assert.equal(ui.text(alert.querySelector('span')), say(
    "Couldn't record this access, so the list wasn't shown. Try again.", 'تعذّر تسجيل هذا الاطلاع، لذا لم تُعرض القائمة. حاول مرة أخرى.'));
  assert.equal(ui.rows().length, 0);
  assert.equal(ui.$('examRostersRoster').querySelector('.et-roster').hidden, true);
  alert.querySelector('button').click();
  await idle();
  assert.equal(ui.server.requests('roster').length, 2);
  assert.equal(ui.rows().length, 12);
  assert.equal(ui.$('examRostersRoster').querySelector('.et-roster-error'), null);
});

test('a list that fails after another was shown leaves none of the rows it had behind', async t => {
  const server = rosterServer();
  const ui = await rosters(t, { server });
  await chooseRoom(ui, 'F-A');
  assert.equal(ui.rows().length, 12);
  server.queue('roster', json({ ok: false, code: 'audit_unavailable', error: 'x' }, 503));
  await chooseRoom(ui, 'M-B');
  assert.ok(ui.$('examRostersRoster').querySelector('.et-roster-error'));
  assert.equal(ui.$('examRostersRoster').querySelectorAll('tr.et-roster-row, tr.et-roster-group-row').length, 0, 'not even hidden');
  const text = ui.$('examRostersRoster').textContent;
  for (const row of fixture.rosters['{"kind": "room", "room_code": "F-A", "slot_index": 0}'].rows) {
    assert.equal(text.includes(row.name), false, row.name);
  }
});

test('a busy check, an ended session and a lost connection each say so in place, never navigating away', async t => {
  const server = rosterServer();
  server.queue('roster',
    json({ ok: false, code: 'roster_busy', error: 'busy' }, 503),
    { ok: false, status: 200, redirected: true, url: 'http://exam.test/login/', headers: { get: () => 'text/html' }, json: async () => { throw new Error('html'); } },
    () => { throw new server.window.TypeError('Failed to fetch'); });
  const ui = await rosters(t, { server });
  const message = () => ui.text(ui.$('examRostersRoster').querySelector('.et-roster-error span'));
  await chooseRoom(ui, 'F-A');
  assert.equal(message(), say('The student lists are being checked. Try again in a moment.', 'تجري مطابقة قوائم الطلاب. أعد المحاولة بعد قليل.'));
  ui.$('examRostersRoster').querySelector('.et-roster-error button').click();
  await idle();
  assert.equal(message(), say('Your session expired. Sign in again in a new tab, then try again.', 'انتهت جلسة تسجيل الدخول. سجّل الدخول في تبويب جديد ثم أعد المحاولة.'));
  ui.$('examRostersRoster').querySelector('.et-roster-error button').click();
  await idle();
  assert.equal(message(), say("Couldn't reach the server. Check your connection, then try again.", 'تعذّر الوصول إلى الخادم. تحقق من الاتصال ثم أعد المحاولة.'));
  assert.equal(ui.window.location.pathname, '/exam-timetable/rosters/');
});

test('a gate on the run blocks the navigator and the export in plain words; another term\'s students are never shown', async t => {
  const server = rosterServer();
  server.queue('index', json({ ok: false, code: 'lists_term_mismatch', error: 'x', live_term: ['1448', '2'], saved_term: ['1448', '1'] }, 409));
  const ui = await rosters(t, { server });
  const words = say(
    'Student lists are unavailable: the imported student timetables are for 1448 term 2 and this exam timetable is for 1448 term 1.',
    'قوائم الطلاب غير متاحة: الجداول الدراسية المستوردة تخص الفصل 2 من عام 1448، وجدول الاختبارات هذا يخص الفصل 1 من عام 1448.');
  assert.equal(ui.text('examRostersNavState'), words);
  assert.equal(ui.text('examRostersPaneEmpty'), words);
  assert.equal(ui.$('examRostersNavState').querySelector('button'), null, 'a refusal has no Try again');
  assert.equal(ui.$('examRostersExport').getAttribute('aria-disabled'), 'true');
  assert.equal(ui.text('examRostersExportReason'), words);
  assert.equal(ui.rooms().length, 0);
  assert.equal(ui.server.requests('roster').length, 0);
});

test('while a list loads the previous one stays, dimmed and busy, and a first load shows skeleton rows then its count', async t => {
  const server = rosterServer();
  const first = hold();
  server.queue('roster', first.answer);
  const ui = await rosters(t, { server });
  room(ui, 'F-A').click();
  await idle();
  const status = ui.$('examRostersRoster').querySelector('.et-roster-status');
  assert.equal(status.querySelectorAll('.et-skeleton-bar').length, 8);
  assert.equal(status.querySelector('.et-roster-skeleton').getAttribute('aria-busy'), 'true');
  assert.equal(ui.text(status.querySelector('.et-roster-loading')), say('Loading 12 students…', 'جارٍ التحميل · عدد الطلاب: 12'));
  assert.equal(ui.text('examRostersPaneTitle'), say('Room F-A', 'القاعة F-A'), 'the header comes from the navigator at once');
  first.release(json(fixture.rosters['{"kind": "room", "room_code": "F-A", "slot_index": 0}']));
  await idle();
  assert.equal(ui.rows().length, 12);
  const second = hold();
  server.queue('roster', second.answer);
  room(ui, 'M-B').click();
  await idle();
  const roster = ui.$('examRostersRoster').querySelector('.et-roster');
  assert.ok(roster.classList.contains('is-stale'));
  assert.equal(roster.querySelector('table').getAttribute('aria-busy'), 'true');
  assert.equal(ui.rows().length, 12);
  second.release(json(fixture.rosters['{"kind": "room", "room_code": "M-B", "slot_index": 0}']));
  await idle();
  assert.equal(roster.classList.contains('is-stale'), false);
  assert.equal(roster.querySelector('table').hasAttribute('aria-busy'), false);
  assert.equal(ui.rows().length, 20);
});

test('a slow answer for a room left behind never replaces the room chosen since', async t => {
  const server = rosterServer();
  const slow = hold();
  server.queue('roster', slow.answer);
  const ui = await rosters(t, { server });
  room(ui, 'F-A').click();
  await idle();
  await chooseRoom(ui, 'M-B');
  slow.release(json(fixture.rosters['{"kind": "room", "room_code": "F-A", "slot_index": 0}']));
  await idle();
  assert.equal(ui.text('examRostersPaneTitle'), say('Room M-B', 'القاعة M-B'));
  assert.equal(ui.rows().length, 20);
});

test('a long list renders in batches and says so until every row is in', async t => {
  // The batches wait for the test, not for a clock: a busy machine never
  // runs two before the first is looked at.
  const batches = [];
  const ui = await rosters(t, {
    before: window => {
      window.djCsrfToken = 'test-csrf';
      window.__examRosterTiming = { batch: 10, loadingNotice: 0, filterDelay: 0, announceDelay: 0, schedule: callback => batches.push(callback) };
    },
  });
  ui.$('examRostersViews').querySelector('[data-view="course"]').click();
  await idle();
  ui.courses().find(item => item.dataset.course === 'MATH101').click();
  await idle();
  const count = () => ui.text(ui.$('examRostersRosterListCount'));
  assert.equal(ui.rows().length, 10);
  assert.equal(count(), say('Showing 10 of 43…', 'المعروض: 10 من 43…'));
  assert.equal(batches.length, 1, 'the next batch waits for idle time');
  while (batches.length) batches.shift()();
  assert.equal(ui.rows().length, 43);
  assert.equal(count(), say('Showing 43 of 43', 'المعروض: 43 من 43'));
});

test('Refresh checks the lists again (refresh=1) and reloads the list on screen once', async t => {
  const ui = await rosters(t);
  await chooseRoom(ui, 'F-A');
  ui.$('examRostersRefresh').click();
  await idle();
  assert.deepEqual(ui.server.requests('index').map(call => call.query), ['', '?refresh=1']);
  assert.equal(ui.server.requests('roster').length, 2);
  assert.deepEqual(ui.server.bodies('roster')[1], { scope: { kind: 'room', slot_index: 0, room_code: 'F-A' } });
  assert.match(ui.text('examRostersChecked'), say(/^Lists checked \d\d:\d\d:\d\d$/, /^وقت مطابقة القوائم: \d\d:\d\d:\d\d$/));
});

// ── Small screens ───────────────────────────────────────────

test('below 800px the navigator comes first; a room pushes the pane with focus on it, and ‹ Rooms goes back', async t => {
  const ui = await rosters(t, { narrow: true });
  const revealed = [];
  ui.window.HTMLElement.prototype.scrollIntoView = function () { revealed.push(this); };
  const layout = ui.$('examRostersLayout');
  assert.equal(layout.dataset.screen, 'nav');
  await chooseRoom(ui, 'F-A');
  assert.equal(layout.dataset.screen, 'pane');
  assert.deepEqual(revealed.map(node => node.id), ['examRostersPane', 'examRostersPane'], 'the pane is brought into view, and again once its list is in');
  assert.equal(ui.document.activeElement, ui.$('examRostersPaneTitle'));
  assert.equal(ui.text('examRostersScreenBackText'), say('Rooms', 'القاعات'));
  const written = ui.history.length;
  ui.$('examRostersScreenBack').focus();
  ui.$('examRostersScreenBack').click();
  await idle();
  assert.equal(ui.history.length, written, 'the room was a history entry: going back pops it, rewriting nothing');
  assert.equal(layout.dataset.screen, 'nav');
  // Focus returns to the room that opened the pane, brought into view.
  assert.equal(ui.document.activeElement, room(ui, 'F-A'));
  assert.equal(room(ui, 'F-A').tabIndex, 0);
  assert.equal(revealed.at(-1), room(ui, 'F-A'), 'scrolled into view');
  assert.equal(ui.address().searchParams.has('room'), false);
  assert.equal(ui.$('examRostersPaneHead').hidden, true);
});

test('a phone opened on a room by its address: ‹ Rooms stays on the page and puts focus on that room', async t => {
  const ui = await rosters(t, { narrow: true, url: `${PAGE_URL}&view=room&slot=0&room=M-B` });
  const layout = ui.$('examRostersLayout');
  assert.equal(layout.dataset.screen, 'pane');
  assert.equal(ui.text('examRostersPaneTitle'), say('Room M-B', 'القاعة M-B'));
  // Nothing of this page to go back to: Back would leave it.
  assert.equal(ui.window.history.state.pushed, false);
  const length = ui.window.history.length;
  const back = ui.$('examRostersScreenBack');
  back.focus();
  assert.equal(ui.document.activeElement, back);
  back.click();
  await idle();
  assert.equal(ui.window.history.length, length, 'no Back, no new entry');
  assert.equal(ui.window.location.pathname, '/exam-timetable/rosters/', 'still on Student lists');
  assert.equal(layout.dataset.screen, 'nav');
  assert.equal(ui.address().searchParams.has('room'), false);
  assert.equal(ui.$('examRostersPaneHead').hidden, true);
  // Focus goes to the room whose list was just left, never stays on a gone control.
  assert.equal(ui.document.activeElement, room(ui, 'M-B'));
  assert.equal(room(ui, 'M-B').tabIndex, 0);
});

test('the Arabic page is right to left with codes isolated left to right, and never guesses a direction', { skip: !AR }, async t => {
  const ui = await rosters(t);
  assert.equal(ui.document.documentElement.dir, 'rtl');
  assert.equal(ui.document.documentElement.lang, 'ar');
  await chooseCourse(ui, 'MATH101');
  assert.equal(ui.document.querySelectorAll('[dir="auto"]').length, 0);
  for (const node of ui.$('examRostersRoster').querySelectorAll('.et-col-id bdi, .et-col-program bdi, .et-col-room bdi')) assert.equal(node.dir, 'ltr');
  // Label-first counts, so plural agreement never breaks.
  assert.equal(ui.text(ui.chip('clash')), 'تعارض: 17');
  assert.equal(ui.text('examRostersPaneMeta').split(' · ')[1], 'عدد الطلاب: 43');
});

// ── Edge shapes (the real answers, reshaped as the contract allows) ─

test('after eight tabs the other sections wait in More; one chosen there takes the last tab and focus', async t => {
  const math = H.clone(MATH());
  const base = math.sections[0];
  const rows = math.rows.filter(row => row.section_key === base.section_key);
  math.sections = Array.from({ length: 10 }, (_, i) => ({ ...H.clone(base), section_key: `term-section:${100 + i}`, section: `M${10 + i}`, rows: 3, now: 3, saved: 3 }));
  math.rows = math.sections.flatMap((section, i) => rows.slice(i * 3, i * 3 + 3).map(row => ({ ...row, section_key: section.section_key, section: section.section })));
  const server = rosterServer();
  server.queue('roster', json(math));
  const ui = await rosters(t, { server });
  await chooseCourse(ui, 'MATH101');
  assert.deepEqual(ui.tabs().map(tab => tab.querySelector('.et-tab-label').textContent), [say('All', 'الكل'), 'M10', 'M11', 'M12', 'M13', 'M14', 'M15', 'M16']);
  const more = ui.$('examRostersRoster').querySelector('.et-section-more');
  assert.equal(more.hidden, false);
  assert.deepEqual([...more.options].map(option => option.textContent), [say('More (3)', 'المزيد (3)'), 'M17 · 3', 'M18 · 3', 'M19 · 3']);
  more.value = 'MATH101|term-section:109|M';
  more.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
  await idle();
  const tabs = ui.tabs();
  assert.equal(tabs.length, 8);
  assert.equal(tabs[7].querySelector('.et-tab-label').textContent, 'M19');
  assert.equal(tabs[7].getAttribute('aria-selected'), 'true');
  assert.equal(ui.document.activeElement, tabs[7]);
  assert.deepEqual([...more.options].slice(1).map(option => option.value), ['MATH101|term-section:106|M', 'MATH101|term-section:107|M', 'MATH101|term-section:108|M']);
  assert.equal(ui.rows().length, 3);
});

test('the unroomed section opens as its own list; an online room waits last under Online', async t => {
  const index = H.clone(fixture.index);
  index.rooms.find(item => item.slot_index === 0 && item.room_code === 'M-A').online = true;
  const server = rosterServer();
  server.queue('index', json(index));
  const ui = await rosters(t, { server });
  const headings = [...ui.$('examRostersRooms').querySelectorAll('.et-nav-heading')].map(node => ui.text(node));
  assert.deepEqual(headings, [say('Not assigned · 1 section', 'لم تُحدَّد لها قاعة · عدد الشعب: 1'), say('Online · 1 room', 'عن بُعد · عدد القاعات: 1')]);
  const items = ui.rooms();
  assert.equal(items.at(-1).dataset.room, 'M-A');
  assert.equal(items.filter(item => item.dataset.room === 'M-A').length, 1, 'an online room is listed once, under Online');
  assert.ok(ui.text(items.at(-1).querySelector('.et-nav-code')).endsWith(say('Online', 'عن بُعد')));
  const unroomed = items.find(item => item.dataset.sectionGroup);
  assert.equal(ui.text(unroomed.querySelector('.et-nav-detail')), say('No room', 'لم تُحدَّد قاعة'));
  unroomed.click();
  await idle();
  assert.deepEqual(ui.server.bodies('roster'), [{ scope: { kind: 'section', exam: 'MATH101', section_key: 'unmapped:U:missing', gender: 'U' } }]);
  assert.equal(ui.text('examRostersPaneTitle'), say('MATH101 · CALCULUS I · Section not recorded · Not recorded', 'MATH101 · CALCULUS I · الشعبة غير مسجلة · غير مسجل'));
  assert.equal(ui.rows().length, 1);
  assert.equal(ui.rooms().find(item => item.dataset.sectionGroup).getAttribute('aria-current'), 'true');
});

test('each Needs review heading names what it counts: sections not roomed, students without a seat, online rooms', async t => {
  // The changed lists, reshaped as the contract allows: three sections the
  // timetable could not room, one student without a seat, two online rooms.
  // Every count differs, so no heading can pass with another one's count.
  const index = H.clone(fixture.changed.index);
  const unroomed = index.not_assigned.find(item => item.slot_index === 0);
  index.not_assigned.push(
    { ...H.clone(unroomed), section_key: 'unmapped:U:ambiguous', section_status: 'ambiguous' },
    { ...H.clone(unroomed), exam: 'IS201' },
  );
  index.no_seat = index.no_seat.filter(item => item.section === 'M1');
  assert.deepEqual(index.no_seat.map(item => item.no_seat), [1]);
  index.rooms.filter(item => item.slot_index === 0 && ['M-A', 'M-B'].includes(item.room_code)).forEach(item => { item.online = true; });
  const server = rosterServer({ changed: true });
  server.queue('index', json(index));
  const ui = await rosters(t, { server });
  // The page was asked for in this language, never left to the default.
  assert.equal(ui.document.documentElement.lang, say('en', 'ar'));
  const headings = [...ui.$('examRostersRooms').querySelectorAll('.et-nav-heading')].map(node => ui.text(node));
  assert.deepEqual(headings, [
    say('Not assigned · 3 sections', 'لم تُحدَّد لها قاعة · عدد الشعب: 3'),
    say('No seat · 1 student', 'بلا مقعد · عدد الطلاب: 1'),
    say('Online · 2 rooms', 'عن بُعد · عدد القاعات: 2'),
  ]);
  // Each count is of the unit it names, under its own heading.
  assert.equal(ui.rooms().filter(item => item.dataset.from === 'not_assigned').length, 3);
  assert.equal(ui.rooms().filter(item => item.dataset.from === 'no_seat').length, 1);
  assert.deepEqual(ui.rooms().slice(-2).map(item => item.dataset.room), ['M-A', 'M-B']);
});

test('a section gone since the save still says so, with its saved count', async t => {
  const math = H.clone(MATH());
  math.sections.push({ ...H.clone(math.sections[0]), section_key: 'term-section:77', section: 'M77', membership: 'gone', saved: 9, now: 0, rows: 0, parts: [] });
  const server = rosterServer();
  server.queue('roster', json(math));
  const ui = await rosters(t, { server });
  await chooseCourse(ui, 'MATH101');
  const gone = ui.tabs().find(tab => tab.querySelector('.et-tab-label').textContent === 'M77');
  assert.equal(gone.querySelector('.visually-hidden').textContent, say(' gone', ' غير موجودة الآن'));
  assert.ok(ui.groupRows().includes(say('M77 M77 is gone since saving (9 students at save).', 'M77 M77 غير موجودة الآن (عدد الطلاب عند الحفظ: 9).')));
});

test('more than two flags show two and "+n", the rest said in full to a screen reader', async t => {
  const math = H.clone(MATH());
  const row = math.rows.find(item => item.clash && item.same_day);
  row.clash_with = ['IS201', 'CS101'];
  row.same_day_with = ['PHYS103 (1)'];
  row.room_basis = 'no_seat';
  row.room = null;
  const server = rosterServer();
  server.queue('roster', json(math));
  const ui = await rosters(t, { server });
  await chooseCourse(ui, 'MATH101');
  const cell = ui.rows().find(item => item.querySelector('.et-col-id').textContent === String(row.student_id)).querySelector('.et-col-flags');
  const badges = [...cell.children];
  assert.equal(badges.length, 3);
  assert.deepEqual(badges.slice(0, 2).map(badge => badge.dataset.exam), ['IS201', 'CS101']);
  const more = badges[2];
  assert.equal(more.querySelector('[aria-hidden="true"]').textContent, '+2');
  assert.ok(more.querySelector('.visually-hidden').textContent.startsWith(say('Same day · PHYS103 (1)', 'اليوم نفسه · PHYS103 (1)')));
  assert.ok(more.querySelector('.visually-hidden').textContent.includes(say('No seat', 'بلا مقعد')));
});

test('names fold as the server folds them; an Arabic name in the English page says its language', async t => {
  const ui = await rosters(t);
  const R = ui.window.ExamRoster;
  const alef = String.fromCodePoint(0x0623);
  const hamzaAlef = String.fromCodePoint(0x0625);
  const plainAlef = String.fromCodePoint(0x0627);
  const fatha = String.fromCodePoint(0x064E);
  const tatweel = String.fromCodePoint(0x0640);
  const indicFour = String.fromCodePoint(0x0664);
  assert.equal(R.fold(`  ${alef}${fatha}B${tatweel}C   ${hamzaAlef}D  `), `${plainAlef}bc ${plainAlef}d`);
  assert.equal(R.fold(`${indicFour}401`), '4401');
  const name = R.nameNode(`${plainAlef}B`);
  assert.equal(name.getAttribute('lang'), AR ? null : 'ar');
  assert.equal(name.hasAttribute('dir'), false);
  assert.equal(R.nameNode('STUDENT').hasAttribute('lang'), false);
});

test('the group last chosen is remembered for the next visit, never a student', async t => {
  const first = await rosters(t);
  first.$('examRostersGroups').querySelector('[data-group="F"]').click();
  await idle();
  const stored = first.window.localStorage.getItem('exam-rosters');
  assert.equal(JSON.parse(stored).group, 'F');
  const storage = {
    data: { 'exam-rosters': stored },
    getItem(key) { return this.data[key] ?? null; },
    setItem(key, value) { this.data[key] = String(value); },
  };
  const second = await rosters(t, { storage });
  assert.equal(second.$('examRostersGroups').querySelector('[aria-checked="true"]').dataset.group, 'F');
  // Blocked storage: the page still opens, on its defaults.
  const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  const third = await rosters(t, { storage: blocked });
  assert.equal(third.$('examRostersGroups').querySelector('[aria-checked="true"]').dataset.group, 'all');
  assert.ok(third.rooms().length > 0);
});

test('a list with one section shows no section tabs, and no Section not recorded chip when none is missing', async t => {
  const ui = await rosters(t);
  await chooseRoom(ui, 'F-A');
  const strip = ui.$('examRostersRoster').querySelector('.et-section-strip');
  assert.equal(strip.hidden, true);
  const panel = ui.$('examRostersRosterListPanel');
  assert.equal(panel.hasAttribute('role'), false, 'no tablist, so no tabpanel');
  // A room's list never holds a student without a seat: no "No seat 0" chip
  // to promise every student there has one.
  assert.deepEqual(ui.chips().map(chip => chip.dataset.flag), ['all', 'clash', 'same_day']);
  await chooseCourse(ui, 'MATH101');
  assert.ok(ui.chip('no_seat'), 'a course list keeps it');
});

test('the ID filter matches the start of an ID, never its middle; the program filter narrows the rows and the chips', async t => {
  const ui = await rosters(t);
  await chooseCourse(ui, 'MATH101');
  const filter = ui.$('examRostersRoster').querySelector('.et-roster-filter');
  ui.type(filter, '0200');
  await idle();
  assert.equal(ui.rows().length, 0, 'no ID starts with 0200, though several contain it');
  ui.key(filter, 'Escape');
  await idle();
  const program = ui.$('examRostersRoster').querySelector('.et-roster-program');
  assert.equal(program.hidden, false);
  assert.deepEqual([...program.options].map(option => option.value), ['', 'AI', 'CS', 'CS2', 'IS']);
  program.value = 'AI';
  program.dispatchEvent(new ui.window.Event('change', { bubbles: true }));
  await idle();
  const ai = MATH().rows.filter(row => row.program === 'AI');
  assert.equal(ui.rows().length, ai.length);
  assert.equal(ui.text(ui.chip('all')), say(`All ${ai.length}`, `الكل: ${ai.length}`));
  assert.equal(ui.text(ui.$('examRostersRosterListCount')), say(`Showing ${ai.length} of ${ai.length}`, `المعروض: ${ai.length} من ${ai.length}`));
});

test('a run deleted since the page opened is said plainly, with no Try again', async t => {
  const server = rosterServer();
  const page404 = { ok: false, status: 404, redirected: false, url: '', headers: { get: () => 'text/html' }, json: async () => { throw new Error('html'); } };
  server.queue('roster', json({ ok: false, code: 'not_found', error: 'Run not found' }, 404), page404);
  const ui = await rosters(t, { server });
  const deleted = say('This saved timetable was deleted. Choose another one.', 'حُذف هذا الجدول المحفوظ. اختر جدولاً آخر.');
  await chooseRoom(ui, 'F-A');
  const alert = () => ui.$('examRostersRoster').querySelector('.et-roster-error');
  assert.equal(ui.text(alert().querySelector('span')), deleted);
  assert.equal(alert().querySelector('button'), null);
  // A 404 page (not the endpoint's JSON) means the same, and is never shown raw.
  await chooseRoom(ui, 'M-B');
  assert.equal(ui.text(alert().querySelector('span')), deleted);
  assert.equal(alert().querySelector('button'), null);
});

test('a refusal that arrives with Refresh disables Export and says why', async t => {
  const server = rosterServer();
  const ui = await rosters(t, { server });
  assert.equal(ui.$('examRostersExport').getAttribute('aria-disabled'), 'false');
  server.queue('index', json({ ok: false, code: 'lists_unavailable', error: 'x' }, 409));
  ui.$('examRostersRefresh').click();
  await idle();
  const words = say('Student lists are unavailable: no student timetables have been imported for this term.',
    'قوائم الطلاب غير متاحة: لم تُستورد الجداول الدراسية للطلاب لهذا الفصل.');
  assert.equal(ui.$('examRostersExport').getAttribute('aria-disabled'), 'true');
  assert.equal(ui.text('examRostersExportReason'), words);
  assert.equal(ui.text('examRostersNavState'), words);
});

test('a room over its capacity says so in words; one nearly full is marked', async t => {
  const index = H.clone(fixture.index);
  const over = index.rooms.find(item => item.slot_index === 0 && item.room_code === 'F-A');
  over.seated_now = over.capacity + 2;
  const full = index.rooms.find(item => item.slot_index === 0 && item.room_code === 'M-B');
  full.seated_now = full.capacity;
  const server = rosterServer();
  server.queue('index', json(index));
  const ui = await rosters(t, { server });
  const figure = code => room(ui, code).querySelector('.et-nav-figure');
  assert.ok(figure('F-A').classList.contains('is-over'));
  assert.equal(ui.text(figure('F-A')), say(`${over.capacity + 2}/${over.capacity} over capacity`, `${over.capacity + 2}/${over.capacity} فوق السعة`));
  assert.ok(figure('M-B').classList.contains('is-full'));
  assert.equal(figure('M-B').classList.contains('is-over'), false);
  assert.equal(ui.text(figure('M-B')), `${full.capacity}/${full.capacity}`);
});

test('the Export dialog shows no saved "about" figures from a partial run while the check is on its way', async t => {
  const server = rosterServer();
  const held = hold();
  server.queue('preflight', held.answer);
  const ui = await rosters(t, { server });
  await chooseRoom(ui, 'F-A');
  ui.$('examRostersExport').click();
  await idle();
  for (const kind of ['room', 'period', 'day', 'all', 'course']) {
    assert.equal(ui.document.querySelector(`[data-scope-count="${kind}"]`).textContent, '', kind);
  }
  held.release(json(fixture.preflight));
  await idle();
});

test('a period with no exams is offered, muted, and says it has none', async t => {
  const index = H.clone(fixture.index);
  const empty = index.slots.find(item => item.slot_index === 3);
  empty.exams = [];
  empty.sittings = 0;
  index.rooms = index.rooms.filter(item => item.slot_index !== 3);
  const server = rosterServer();
  server.queue('index', json(index));
  const ui = await rosters(t, { server });
  ui.$('examRostersDays').querySelector('[data-day="Mon"]').click();
  await idle();
  const chip = ui.$('examRostersPeriods').querySelector('[data-slot="3"]');
  assert.ok(chip.classList.contains('is-empty'));
  assert.equal(chip.getAttribute('aria-label'), say('13:00-15:00, no exams', '13:00-15:00، لا توجد اختبارات'));
  chip.click();
  await idle();
  assert.equal(ui.text(ui.$('examRostersRooms')), say('No exams on Mon 13:00-15:00.', 'لا توجد اختبارات في Mon 13:00-15:00.'));
  assert.equal(ui.rooms().length, 0);
});

test('a student gone from the lists since the search says so; Esc with nothing chosen returns to Find', async t => {
  const server = rosterServer();
  const ui = await rosters(t, { server });
  const find = ui.$('examRostersFind');
  find.focus();
  ui.type(find, '4402');
  await idle();
  const id = fixture.searches['4402'].matches[0].student_id;
  server.queue('lookup', json({ ...fixture.lookups['1234567'] }));
  ui.key(find, 'Enter');
  await idle();
  assert.deepEqual(ui.server.bodies('lookup').at(-1), { student_id: String(id) });
  assert.equal(ui.text(ui.$('examRostersLookup')), say(
    `No student with ID ${id} sits an exam in this timetable.`, `لا يوجد اختبار للرقم الجامعي ${id} في هذا الجدول.`));
  ui.key(ui.document.body, 'Escape');
  await idle();
  assert.equal(ui.$('examRostersLookup').hidden, true);
  assert.equal(ui.$('examRostersPaneHead').hidden, true);
  assert.equal(ui.document.activeElement, find, 'with no list behind the lookup, focus goes back to Find');
});

// ── Review round: one build, focus, history, Find ──────────

test('a list from a newer build than the navigator names that one build in the header and brings the navigator up to it', async t => {
  const server = rosterServer();
  const changed = CHANGED_MATH();
  const ui = await rosters(t, { server });
  assert.equal(ui.$('examRostersCheck').querySelector('[data-check]').dataset.check, 'matches');
  // The navigator's 60 s entry expired; the next list was built after a student joined.
  const synced = hold();
  server.queue('roster', json(changed));
  server.queue('index', synced.answer);
  await chooseCourse(ui, 'MATH101');
  const clock = ui.window.ExamRoster.clock;
  assert.notEqual(changed.checked_at, fixture.index.checked_at);
  // The header names one build: that list's check and time, together - at
  // once, not only once the navigator has caught up.
  assert.deepEqual(ui.server.requests('index').map(call => call.query), ['', ''], 'the navigator is asked again (a plain GET)');
  assert.equal(ui.text(ui.$('examRostersCheck')), say('· ≠ Sections changed since saving: 2', '· ≠ الشعب المتغيرة بعد الحفظ: 2'));
  assert.equal(ui.text('examRostersChecked'), say(`Lists checked ${clock(changed.checked_at)}`, `وقت مطابقة القوائم: ${clock(changed.checked_at)}`));
  // The same cached build answers, and the navigator now agrees with the list.
  synced.release(json(fixture.changed.index));
  await idle();
  assert.equal(ui.text(ui.$('examRostersCheck')), say('· ≠ Sections changed since saving: 2', '· ≠ الشعب المتغيرة بعد الحفظ: 2'));
  const item = ui.courses().find(node => node.dataset.course === 'MATH101');
  assert.equal(ui.text(item.querySelector('.et-nav-figure')), String(changed.counts.students));
  assert.equal(ui.text('examRostersPaneMeta').split(' · ')[1], say(`Students: ${changed.counts.students}`, `عدد الطلاب: ${changed.counts.students}`));
  assert.equal(ui.document.activeElement, item, 'redrawing the navigator keeps the keyboard where it was');
  // An answer from the older build never takes the header back.
  server.queue('roster', json(MATH()));
  ui.courses().find(node => node.dataset.course === 'MATH101').click();
  await idle();
  assert.equal(ui.text('examRostersChecked'), say(`Lists checked ${clock(changed.checked_at)}`, `وقت مطابقة القوائم: ${clock(changed.checked_at)}`));
  assert.equal(ui.$('examRostersCheck').querySelector('[data-check]').dataset.check, 'changed');
  assert.equal(ui.server.requests('index').length, 2, 'an older answer asks nothing');
});

test('a room from a newer build takes its header facts from the navigator of that build once it arrives', async t => {
  const server = rosterServer();
  const scope = { kind: 'room', slot_index: 0, room_code: 'M-B' };
  const list = H.rosterOf(scope, { changed: true });
  assert.ok(Date.parse(list.checked_at) > Date.parse(fixture.index.checked_at), 'the list is from a later build');
  // The navigator of that same build: M-B holds a different count of its first section now.
  const later = H.clone(fixture.changed.index);
  assert.equal(later.checked_at, list.checked_at);
  const facts = later.rooms.find(item => item.slot_index === 0 && item.room_code === 'M-B');
  const [first] = facts.sections;
  const before = first.now;
  first.now = before + 1;
  const ui = await rosters(t, { server });
  const synced = hold();
  server.queue('roster', json(list));
  server.queue('index', synced.answer);
  await chooseRoom(ui, 'M-B');
  const inRoom = n => say(`In this room: ${first.exam} ${first.section} (${n})`, `في هذه القاعة: ${first.exam} ${first.section} (${n})`);
  const meta = () => ui.text('examRostersPaneMeta').split(' · ').at(-1);
  // Until the navigator catches up, the header says what the page knows.
  assert.deepEqual(ui.server.requests('index').map(call => call.query), ['', ''], 'the navigator is asked again');
  assert.equal(meta(), inRoom(before));
  synced.release(json(later));
  await idle();
  // The room's header now agrees with the navigator it came with.
  assert.equal(meta(), inRoom(before + 1));
  assert.equal(ui.text('examRostersPaneTitle'), say('Room M-B', 'القاعة M-B'));
  assert.equal(ui.server.requests('roster').length, 1, 'the list is not asked for again');
  assert.equal(ui.rows().length, list.rows.length);
});

test('Refresh keeps the list as it was left: section tab, flag chip, filter and sort', async t => {
  const ui = await rosters(t);
  await chooseCourse(ui, 'MATH101');
  ui.tabs()[1].click();
  await idle();
  ui.chip('same_day').click();
  await idle();
  const filter = ui.$('examRostersRoster').querySelector('.et-roster-filter');
  ui.type(filter, '44010');
  await idle();
  ui.$('examRostersRoster').querySelector('button[data-sort="id"]').click();
  await idle();
  ui.$('examRostersRoster').querySelector('button[data-sort="id"]').click();
  await idle();
  const ids = () => ui.rows().map(row => row.querySelector('.et-col-id').textContent);
  const before = ids();
  assert.ok(before.length > 1 && before.length < 30);
  ui.$('examRostersRefresh').click();
  await idle();
  assert.equal(ui.server.requests('roster').length, 2, 'the list was asked for again');
  assert.equal(ui.tabs()[1].getAttribute('aria-selected'), 'true');
  assert.equal(ui.chip('same_day').getAttribute('aria-checked'), 'true');
  assert.equal(filter.value, '44010');
  assert.equal(ui.$('examRostersRoster').querySelector('button[data-sort="id"]').closest('th').getAttribute('aria-sort'), 'descending');
  assert.deepEqual(ids(), before);
});

test('Refresh re-checks an open lookup, keeps its way back, and re-asks for the list behind it once it closes', async t => {
  const ui = await rosters(t);
  await chooseRoom(ui, 'M-B');
  const find = ui.$('examRostersFind');
  find.focus();
  ui.type(find, '4402');
  await idle();
  ui.$('examRostersFindList').querySelector('[role="option"]').click();
  await idle();
  const id = fixture.searches['4402'].matches[0].student_id;
  const asked = ui.server.requests('lookup').length;
  ui.$('examRostersRefresh').focus();
  ui.$('examRostersRefresh').click();
  await idle();
  assert.deepEqual(ui.server.requests('index').map(call => call.query), ['', '?refresh=1']);
  assert.equal(ui.server.requests('lookup').length, asked + 1);
  assert.deepEqual(ui.server.bodies('lookup').at(-1), { student_id: String(id) });
  assert.equal(ui.$('examRostersLookup').hidden, false);
  assert.equal(ui.text('examRostersPaneTitle'), say(`Student ${id}`, `الطالب ${id}`));
  assert.equal(ui.text('examRostersLookupBackText'), say('Back to Room M-B', 'العودة إلى القاعة M-B'));
  assert.equal(ui.server.requests('roster').length, 1, 'the list behind waits until it is shown');
  assert.equal(ui.document.activeElement, ui.$('examRostersRefresh'), 'the keyboard stays on Refresh');
  ui.$('examRostersLookupBack').click();
  await idle();
  assert.equal(ui.server.requests('roster').length, 2);
  assert.deepEqual(ui.server.bodies('roster').at(-1), { scope: { kind: 'room', slot_index: 0, room_code: 'M-B' } });
  assert.equal(ui.text('examRostersPaneTitle'), say('Room M-B', 'القاعة M-B'));
  assert.equal(ui.rows().length, 20);
});

test('a second lookup keeps the way back to the list the first was opened from', async t => {
  const ui = await rosters(t);
  await chooseRoom(ui, 'M-B');
  const find = ui.$('examRostersFind');
  const lookUp = async query => {
    find.focus();
    ui.type(find, query);
    await idle();
    ui.$('examRostersFindList').querySelector('[role="option"]').click();
    await idle();
  };
  await lookUp('4402');
  assert.equal(ui.text('examRostersLookupBackText'), say('Back to Room M-B', 'العودة إلى القاعة M-B'));
  await lookUp('44010');
  assert.equal(ui.text('examRostersPaneTitle'), say(`Student ${fixture.searches['44010'].matches[0].student_id}`, `الطالب ${fixture.searches['44010'].matches[0].student_id}`));
  assert.equal(ui.text('examRostersLookupBackText'), say('Back to Room M-B', 'العودة إلى القاعة M-B'), 'never "Back to Student …"');
  ui.$('examRostersLookupBack').click();
  await idle();
  assert.equal(ui.text('examRostersPaneTitle'), say('Room M-B', 'القاعة M-B'));
});

test('on a phone the unroomed section is a history entry: Back returns to the navigator, with focus on it', async t => {
  const ui = await rosters(t, { narrow: true });
  const layout = ui.$('examRostersLayout');
  const unroomed = () => ui.rooms().find(node => node.dataset.from === 'not_assigned');
  const before = ui.window.history.length;
  unroomed().click();
  await idle();
  assert.equal(ui.window.history.length, before + 1);
  assert.equal(layout.dataset.screen, 'pane');
  assert.equal(ui.document.activeElement, ui.$('examRostersPaneTitle'));
  ui.window.history.back();
  await idle();
  assert.equal(layout.dataset.screen, 'nav', 'Back stays on Student lists');
  assert.equal(ui.window.location.pathname, '/exam-timetable/rosters/');
  assert.equal(ui.$('examRostersPaneHead').hidden, true);
  assert.equal(ui.document.activeElement, unroomed());
  // Forward opens it again from the entry's state (never from the address).
  ui.window.history.forward();
  await idle();
  assert.equal(layout.dataset.screen, 'pane');
  assert.equal(ui.server.bodies('roster').at(-1).scope.kind, 'section');
  assert.equal(ui.address().search.includes('section'), false);
});

test('Enter straight after typing a student ID is kept: the student opens once the match arrives', async t => {
  const server = rosterServer();
  const held = hold();
  server.queue('lookup', held.answer);
  const ui = await rosters(t, { server });
  const find = ui.$('examRostersFind');
  find.focus();
  ui.type(find, '4402003');
  assert.equal(ui.key(find, 'Enter'), false, 'the Enter is taken, not dropped');
  await idle();
  assert.deepEqual(ui.server.bodies('lookup'), [{ query: '4402003' }], 'one search, asked at once');
  held.release(json(fixture.searches['4402003']));
  await idle();
  assert.deepEqual(ui.server.bodies('lookup').at(-1), { student_id: '4402003' });
  assert.equal(ui.text('examRostersPaneTitle'), say('Student 4402003', 'الطالب 4402003'));
  assert.equal(find.value, '');
  // Typing on after Enter drops it: the query it was for is gone, even when
  // it is typed again - the matches then wait to be chosen.
  const again = hold();
  server.queue('lookup', again.answer);
  find.focus();
  ui.type(find, '4402');
  ui.key(find, 'Enter');
  ui.type(find, '44010');
  await idle();
  ui.type(find, '4402');
  await idle();
  again.release(json(fixture.searches['4402']));
  await idle();
  assert.equal(ui.server.bodies('lookup').filter(body => 'student_id' in body).length, 1);
  assert.equal(ui.$('examRostersFindList').hidden, false);
  assert.ok(ui.$('examRostersFindList').querySelectorAll('[role="option"]').length > 0);
});

test('Find never searches (or audits) a student for a course or room code, or for letters that are no name', async t => {
  const ui = await rosters(t);
  const find = ui.$('examRostersFind');
  find.focus();
  for (const typed of ['MATH101', 'math 101', 'e e e', 's t e', '---', 'M-B1']) {
    ui.type(find, typed);
    await idle();
  }
  assert.equal(ui.server.requests('lookup').length, 0);
  ui.type(find, 'MATH101');
  await idle();
  assert.deepEqual([...ui.$('examRostersFindList').querySelectorAll('[role="option"]')].map(node => ui.text(node)), ['MATH101 · CALCULUS I · Sun 08:00-10:00']);
  assert.equal(ui.$('examRostersFindList').querySelector('.et-find-note'), null, 'no student search is under way');
  ui.type(find, 'testname');
  await idle();
  assert.deepEqual(ui.server.bodies('lookup'), [{ query: 'testname' }], 'a name is searched');
});

test('restored from the back/forward cache, the page drops every list and lookup and asks for the lists again', async t => {
  const ui = await rosters(t);
  await chooseRoom(ui, 'M-B');
  const find = ui.$('examRostersFind');
  find.focus();
  ui.type(find, '4402');
  await idle();
  ui.$('examRostersFindList').querySelector('[role="option"]').click();
  await idle();
  // An ordinary show (a first load) changes nothing.
  ui.window.dispatchEvent(new ui.window.PageTransitionEvent('pageshow', { persisted: false }));
  await idle();
  assert.equal(ui.$('examRostersLookup').hidden, false);
  ui.window.dispatchEvent(new ui.window.PageTransitionEvent('pageshow', { persisted: true }));
  await idle();
  assert.equal(ui.$('examRostersLookup').hidden, true);
  assert.equal(ui.$('examRostersLookup').children.length, 0);
  assert.equal(ui.$('examRostersPaneHead').hidden, true);
  assert.equal(ui.$('examRostersRoster').querySelectorAll('tr').length, 1, 'the column header only');
  assert.equal(ui.address().searchParams.has('room'), false);
  assert.equal(ui.server.requests('index').length, 2);
  const text = ui.document.body.textContent;
  for (const name of NAMES) assert.equal(text.includes(name), false, name);
});

test('a roomed section the lists do not name reads "Section not recorded · Male" everywhere, with its room, never "no room yet"', async t => {
  const data = fixture.unmapped;
  const server = rosterServer();
  server.queue('index', json(data.index));
  const ui = await rosters(t, { server });
  const unrecorded = say('Section not recorded · Male', 'الشعبة غير مسجلة · طلاب');
  const item = room(ui, data.room);
  assert.ok(ui.text(item.querySelector('.et-nav-detail')).includes(`MATH101 ${unrecorded}`), ui.text(item));
  server.queue('roster', json(data.roomList));
  item.click();
  await idle();
  assert.ok(ui.text('examRostersPaneMeta').includes(`MATH101 ${unrecorded} (1)`), ui.text('examRostersPaneMeta'));
  // The course list: its tab, its part group with the room, and why there is no section.
  server.queue('roster', json(data.course));
  await chooseCourse(ui, 'MATH101');
  assert.ok(ui.tabs().map(tab => ui.text(tab)).includes(`${unrecorded} 1`));
  const heading = ui.groupRows().find(text => text.startsWith(`${unrecorded} · ${data.room}`));
  assert.ok(heading, ui.groupRows().join(' | '));
  assert.ok(heading.endsWith(say("The imported timetables don't name a section for these students.", 'لا تحدد الجداول المستوردة شعبة لهؤلاء الطلاب.')), heading);
  // The student with no cohort is not roomed: for them it is still "no room yet".
  const unroomed = ui.groupRows().find(text => text.startsWith(say('Section not recorded · Not recorded', 'الشعبة غير مسجلة · غير مسجل')));
  assert.ok(unroomed.endsWith(say("The imported timetables don't name a section for these students, so they have no room yet.",
    'لا تحدد الجداول المستوردة شعبة لهؤلاء الطلاب، لذا لم تُحدَّد لهم قاعة بعد.')), unroomed);
  // The lookup names the same group, with its room.
  server.queue('lookup', json(data.search), json(data.lookup));
  const find = ui.$('examRostersFind');
  find.focus();
  ui.type(find, String(data.student));
  await idle();
  ui.key(find, 'Enter');
  await idle();
  const row = [...ui.$('examRostersLookup').querySelectorAll('tbody tr')].find(node => node.querySelector('.et-col-name > bdi').textContent === 'MATH101');
  assert.equal(ui.text(row.querySelector('.et-col-section')), unrecorded);
  assert.equal(ui.text(row.querySelector('.et-col-room')), data.room);
});
