/* Owner ruling 2026-10-20: "For the manual move all the moves should be
 * allowed even with any type of clash, because all that will be seen and the
 * one doing the move sees the clash." A clash (occupied room, critical target,
 * filled cell, a repair that is not apply-ready) never disables or refuses a
 * manual edit in the split workspace; it is saved and the clash is reported.
 * A misfit room (type, gender, seats) is still refused. Full split script and
 * template; controlled HTTP responses; no server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };
const wait = async condition => { for (let i = 0; i < 200; i++) { if (condition()) return; await settle(); } assert.fail('UI did not settle'); };
const baseRoom = { room_type: 'lecture', section: '', capacity: 40, fits_type: true, fits_gender: true, fits_capacity: true, department_fit: true, reasons: [], occupied_by: [], validation: { critical_count: 0, warning_count: 0 } };
const OCCUPIED = { ...baseRoom, room_code: 'R9', available: false, tone: 'block', schedule_clean: false, policy_clean: false, slot_clean: false, reasons: ['room occupied at this time'], occupied_by: [{ board_label: 'Other', section: 'CS9-S1', start: '09:00', end: '10:15' }], validation: { critical_count: 0, warning_count: 1, room_clashes: [{ id: 99, room: 'R9', scope: 'cross_board' }] } };
const TOO_SMALL = { ...baseRoom, room_code: 'R3', capacity: 10, available: false, tone: 'block', fits_capacity: false, reasons: ['capacity 10 < 30'] };
const ROOM_CLASH = { critical_count: 0, warning_count: 1, room_clashes: [{ id: 99, room: 'R9', scope: 'cross_board' }] };

async function splitHarness(t, intercept = () => undefined) {
  const html = read('core/templates/core/timetable_workspace_split.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://split.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [];
  const placement = { id: 10, board_id: 2, term_section_id: 110, course_code: 'AI10', course_key: 'AI10', course_name: 'Course', section: 'S1', day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'R1', is_locked: false, meetings: [] };
  const board = { id: 2, label: 'Board', nominal_term: 1, primary_count: 1, placement_count: 1 };
  const boardData = () => ({ board, placements: [placement], slot_config: [{ start: '09:00', end: '10:15' }, { start: '10:30', end: '11:45' }] });
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  Object.assign(w, { LANGUAGE_CODE: 'en', djCsrfToken: 'audit', INITIAL_SCENARIO: '', INITIAL_BOARD: '', DEFAULT_YEAR: '1448', DEFAULT_TERM: '1' });
  w.esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  w.notify = Object.fromEntries(['success', 'warning', 'error', 'info'].map(kind => [kind, message => notices.push({ kind, message })]));
  w.console.error = () => {};
  w.matchMedia = () => ({ matches: false });
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    let body = await intercept(url, options);
    if (body === undefined) {
      if (url.includes('/room-candidates/')) body = { ok: true, target: { day: 'SUN', start: '09:00', end: '10:15', required_type: 'lecture', required_capacity: 30 }, candidates: [OCCUPIED, TOO_SMALL], summary: {} };
      else if (url.endsWith('/move/')) body = { ok: true, placement, placements: [placement], validation: ROOM_CLASH, instructor_clash_backstop: { checked: true, involving_this_section: [] } };
      else if (url.endsWith('/create-planned/')) body = { ok: true, placement: { id: 50, board_id: 2, term_section_id: 150 }, placements: [{ id: 50, board_id: 2 }], validation: { critical_count: 2, warning_count: 0, items: [] }, instructor_clash_backstop: { checked: true, involving_this_section: [] } };
      else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
      else if (url === '/ops/tw/boards/2/') body = boardData();
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
      else if (url.endsWith('/conflicts/')) body = { overlaps: [], instructor_clashes: [], room_clashes: [] };
      else if (url.endsWith('/plan-lens/')) body = { plan_lens: { plans: [], courses: {}, sections: {} } };
      else body = { ok: true, candidates: [], instructors: [], summary: {} };
    }
    return { ok: !body.httpError, status: body.httpError || 200, headers: { get: () => null }, json: async () => body };
  };
  // base.html loads shared-utils.js (esc, safeHtml, pluralAr) on every page, first.
  w.eval(read('static/js/shared-utils.js'));
  w.eval(read('static/js/timetable-meeting-policy.js'));
  // The page loads the shared slot-column module before its own script.
  w.eval(read('static/js/timetable-slot-config.js'));
  w.eval(read('static/js/timetable-section-teacher.js'));
  w.eval(read('static/js/timetable-programme-mark.js'));
  w.eval(read('static/js/page-timetable-workspace-split.js') + '\n;globalThis.__splitAudit = { S, RP };');
  await settle();
  const { S: state, RP: panel } = w.__splitAudit;
  state.scenarioId = 1; state.scenarioMeta = { id: 1, status: 'draft' }; state.boards = [board];
  state.cols = 1; state.rows = 1;
  state.panes[0].boardId = 2; state.panes[0].boardData = boardData();
  w.renderPane(0);
  const posts = suffix => requests.filter(row => row.url.endsWith(suffix) && row.options.method === 'POST');
  const cell = (day, start) => d.querySelector(`#twsPanes .block-grid .cell[data-day="${day}"][data-start="${start}"]`)
    || d.querySelector(`.tws-pane .cell[data-day="${day}"][data-start="${start}"]`);
  return { w, d, state, panel, requests, notices, posts, cell };
}

test('an occupied room that fits is assignable by hand; the clash is kept, shown and undoable', async t => {
  const h = await splitHarness(t);
  h.state.selectedPlacementId = 10; h.state.selectedPaneIdx = 0;
  h.panel.open = true; h.panel.tab = 'selection'; h.w.renderRpanel();
  await wait(() => h.d.querySelectorAll('.tws-room-card').length === 2);
  const [occupied, small] = h.d.querySelectorAll('.tws-room-card');
  assert.ok(occupied.classList.contains('block'), 'the occupied card keeps its clash tone');
  const apply = occupied.querySelector('.tws-room-apply');
  assert.equal(apply.disabled, false);
  assert.equal(apply.textContent, 'Apply');
  assert.match(apply.title, /occupied/);
  assert.match(occupied.textContent, /room occupied at this time/);
  assert.match(occupied.textContent, /Other CS9-S1/);
  // A room too small for the section is a misfit, not a clash: still refused.
  assert.equal(small.querySelector('.tws-room-apply').disabled, true);
  assert.equal(small.querySelector('.tws-room-apply').textContent, 'Blocked');
  apply.click();
  await wait(() => h.notices.some(row => row.kind === 'warning'));
  assert.equal(h.posts('/move/').length, 1);
  assert.equal(JSON.parse(h.posts('/move/')[0].options.body).room, 'R9');
  assert.deepEqual(h.notices.map(row => row.message), ['Assigned room R9 with 1 conflict(s)']);
  assert.equal(h.state.undoStack.at(-1).new_room, 'R9');
});

test('a misfit room is refused before any request, occupied or not', async t => {
  const h = await splitHarness(t);
  const slot = { day: 'SUN', start: '09:00', end: '10:15' };
  // Occupied as well as misfit: the misfit decides (wrong type, gender, seats).
  for (const misfit of [TOO_SMALL, { ...OCCUPIED, fits_capacity: false }, { ...OCCUPIED, fits_type: false }, { ...OCCUPIED, fits_gender: false }]) {
    assert.equal(await h.w.applyRoomCandidate(10, misfit, slot), null);
    assert.match(h.notices.at(-1).message, /does not meet the section requirements/);
  }
  assert.equal(h.posts('/move/').length, 0);
});

test('a manual move that keeps a room double-booking reports it as a conflict', async t => {
  const h = await splitHarness(t);
  await h.w.movePlacementToSlot({ placementId: 10, targetPaneIdx: 0, sourcePaneIdx: 0, day: 'MON', start: '09:00', end: '10:15' });
  assert.equal(h.posts('/move/').length, 1);
  assert.deepEqual(h.notices.map(row => row.message), ['Moved with 1 conflict(s)']);
});

test('a critical create-assist target stays clickable and places the section with its conflicts', async t => {
  const h = await splitHarness(t, url => (url.includes('/planned-slot-candidates/')
    ? { candidates: [{ kind: 'lect', day: 'MON', start: '09:00', end: '10:15', tone: 'avoid', critical_count: 2, evidence: [{ kind: 'instructor', tone: 'critical', title: 'Instructor clash' }] }] }
    : undefined));
  h.w.beginCreateSectionAssist({ course_code: 'AI20', course_key: 'AI20', course_name: 'AI20', section_label: 'S2' });
  await wait(() => h.d.querySelector('.tws-create-target'));
  const target = h.d.querySelector('.tws-create-target');
  assert.ok(target.classList.contains('avoid'), 'the target keeps its clash tone');
  assert.equal(target.disabled, false);
  target.click();
  await wait(() => h.posts('/create-planned/').length === 1);
  await wait(() => h.notices.some(row => /conflict/.test(row.message)));
  assert.deepEqual(JSON.parse(h.posts('/create-planned/')[0].options.body).meetings, [{ day: 'MON', start_time: '09:00', end_time: '10:15' }]);
  assert.ok(h.notices.some(row => row.kind === 'warning' && row.message === 'Placed with 2 conflict(s)'));
});

test('a section dropped on a filled cell is placed there with its clash', async t => {
  const h = await splitHarness(t);
  const filled = h.d.querySelector('.tws-pane .cell.filled[data-day="SUN"][data-start="09:00"]');
  assert.ok(filled, 'the existing section fills SUN 09:00');
  const data = await h.w.createPlannedPlacementFromCell(0, filled, { course_code: 'AI20', course_key: 'AI20', section_label: 'S2' });
  assert.ok(data);
  assert.equal(h.posts('/create-planned/').length, 1);
  assert.deepEqual(JSON.parse(h.posts('/create-planned/')[0].options.body).meetings, [{ day: 'SUN', start_time: '09:00', end_time: '10:15' }]);
  assert.ok(!h.notices.some(row => /empty slot/.test(row.message)));
  assert.ok(h.notices.some(row => row.message === 'Placed with 2 conflict(s)'));
});

test('a slot whose analysed repair is not apply-ready is still a plain two-click move', async t => {
  const h = await splitHarness(t);
  h.state.selectedPlacementId = 10; h.state.selectedPaneIdx = 0;
  h.w.beginSlotAssist(0, 10, { sticky: true });
  await settle();
  const key = h.w.slotAssistKey(h.state.slotAssist.kind, 'MON', '09:00');
  const notReady = { kind: h.state.slotAssist.kind, day: 'MON', start: '09:00', end: '10:15', deepRepair: true, deepRepairCandidateId: 'c1', status: 'feasible', solver_status: 'feasible', exact_repair: { existing_lost: 3, blocked_recovered: 0 } };
  h.state.slotAssist.candidates.set(key, notReady);
  h.state.slotAssist.deepRepairRun = { run: { id: 7 }, candidates: [{ candidate_id: 'c1', status: 'feasible', metrics: { exact_repair: { existing_lost: 3 } } }] };
  const target = h.d.querySelector('.tws-pane .cell[data-day="MON"][data-start="09:00"]');
  target.dataset.slotAssistKey = key;
  assert.equal(h.w.previewSelectedMoveToSlot(0, target), true);
  assert.equal(h.posts('/move/').length, 0, 'the first click previews');
  assert.equal(h.w.previewSelectedMoveToSlot(0, target), true);
  await wait(() => h.posts('/move/').length === 1);
  await wait(() => h.notices.some(row => row.message === 'Moved with 1 conflict(s)'));
  await wait(() => !h.state.slotAssist.active);
  for (let i = 0; i < 5; i++) await settle();
  assert.ok(!h.notices.some(row => /not an apply-ready repair/.test(row.message)));
  assert.equal(h.requests.filter(row => row.url.includes('/repair/runs/')).length, 0);
});

test("the classic page's Change room decides fit and use exactly as split view does", async t => {
  // TimetableRoomChoice copies split's rules until split calls it (design
  // record F3); the two read one case table (timetable-room-choice.test.cjs).
  const h = await splitHarness(t);
  h.w.eval(read('static/js/timetable-room-choice.js'));
  const choice = h.w.TimetableRoomChoice;
  const { cases } = JSON.parse(read('tests/fixtures/room_choice_cases.json'));
  for (const { name, room, ...expected } of cases) {
    assert.equal(choice.fits(room), h.w.roomFitsSection(room), name);
    assert.equal(choice.occupied(room), h.w.roomIsOccupied(room), name);
    assert.equal(choice.codeKey(room?.room_code), h.w.roomCodeKey(room?.room_code), name);
    // Split's answer is the table's, not only the module's.
    assert.equal(h.w.roomFitsSection(room), expected.fits, name);
    assert.equal(h.w.roomIsOccupied(room), expected.occupied, name);
    assert.equal(h.w.roomCodeKey(room?.room_code), expected.code_key, name);
  }
  // The table holds rooms each way, so agreeing says something.
  for (const rule of [h.w.roomFitsSection, h.w.roomIsOccupied]) {
    assert.ok(cases.some(c => rule(c.room)) && cases.some(c => !rule(c.room)), rule.name);
  }
  assert.ok(cases.some(c => !c.room?.available && h.w.roomFitsSection(c.room)), 'a held room that fits');
  // A held room that misses on one flag alone, for each flag: dropping any
  // one check from either copy is a held misfit made choosable.
  const flags = ['fits_type', 'fits_gender', 'fits_capacity'];
  for (const flag of flags) {
    assert.ok(cases.some(c => h.w.roomIsOccupied(c.room) && flags.every(f => (c.room[f] === false) === (f === flag))), `a held room that misses only ${flag}`);
  }
});

test('the classic page and split view differ only on answers that carry the audit; without it, which room can be chosen is split view’s answer', async t => {
  // 5c-audit: the classic dialog judges by the quality audit when the answer
  // carries it (?audit=1); split view keeps the legacy rule until F3.
  const h = await splitHarness(t);
  h.w.eval(read('static/js/timetable-room-choice.js'));
  const choice = h.w.TimetableRoomChoice;
  const table = JSON.parse(read('tests/fixtures/room_choice_cases.json'));
  const targetOf = c => {
    const target = { ...table.target, ...(c.target || {}) };
    if ('audit_target' in c) target.audit = { ...table.audit_target, ...c.audit_target };
    return target;
  };
  const carries = c => 'audit_target' in c && targetOf(c).audit.assessed === true && Boolean(c.room?.audit);
  const placement = c => ('placement' in c ? c.placement : table.placement);
  const offered = (c, room, target) => choice.verdict(room, placement(c), target).group !== 'misfit';
  const parted = { offered: [], refused: [] };
  for (const c of table.cases) {
    const target = targetOf(c);
    const split = h.w.roomFitsSection(c.room);
    // The same answer without the audit, on either side: split view's answer.
    const legacyTarget = { ...target };
    delete legacyTarget.audit;
    const legacyRoom = c.room && { ...c.room };
    if (legacyRoom) delete legacyRoom.audit;
    assert.equal(offered(c, c.room, legacyTarget), split, `${c.name}: no audit on the target`);
    assert.equal(offered(c, legacyRoom, target), split, `${c.name}: no audit on the room`);
    if (!carries(c)) assert.equal(offered(c, c.room, target), split, c.name);
    else if (offered(c, c.room, target) !== split) parted[split ? 'refused' : 'offered'].push(c.room.room_code);
  }
  // They do part, both ways: rooms the audit offers that split refuses (seats)
  // and rooms it refuses that split offers (a room with no gender, the
  // audit's type or gender need).
  assert.ok(parted.offered.length > 0 && parted.refused.length > 0, JSON.stringify(parted));
  assert.ok(parted.offered.includes('R25') && parted.refused.includes('R34'), JSON.stringify(parted));
});

test('split view keeps the legacy rule: its room reads never ask for the audit (design record F3)', async t => {
  const h = await splitHarness(t);
  h.state.selectedPlacementId = 10; h.state.selectedPaneIdx = 0;
  h.panel.open = true; h.panel.tab = 'selection'; h.w.renderRpanel();
  await wait(() => h.d.querySelectorAll('.tws-room-card').length === 2);
  const reads = h.requests.filter(row => row.url.includes('/room-candidates/'));
  assert.ok(reads.length > 0, 'split view read its rooms');
  assert.ok(reads.every(row => !/[?&]audit=/.test(row.url)), reads.map(row => row.url).join('\n'));
});
