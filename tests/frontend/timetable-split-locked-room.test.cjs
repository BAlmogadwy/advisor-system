/* Split workspace: a locked section keeps its time but its room can change
 * from the selection panel without an override; the server's refusal is shown. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };
const wait = async condition => { for (let i = 0; i < 100; i++) { if (condition()) return; await settle(); } assert.fail('Split UI did not settle'); };
const ROOM = { room_code: 'R2', room_type: 'lecture', section: '', capacity: 40, available: true, fits_type: true, fits_gender: true, fits_capacity: true, department_fit: true, schedule_clean: true, policy_clean: true, slot_clean: true, reasons: [], occupied_by: [], validation: { critical_count: 0, warning_count: 0 } };

async function harness(t, intercept = () => undefined) {
  const html = read('core/templates/core/timetable_workspace_split.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://split.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [];
  let room = 'R1';
  const placement = () => ({ id: 10, board_id: 2, term_section_id: 110, course_code: 'AI10', course_key: 'AI10', course_name: 'Course', section: 'S1', day: 'SUN', start_time: '09:00', end_time: '10:15', room, is_locked: true, meetings: [] });
  const board = { id: 2, label: 'Board', nominal_term: 1, primary_count: 1, placement_count: 1 };
  const boardData = () => ({ board, placements: [placement()], slot_config: [{ start: '09:00', end: '10:15' }, { start: '10:30', end: '11:45' }] });
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
      if (url.includes('/room-candidates/')) body = { ok: true, target: { day: 'SUN', start: '09:00', end: '10:15', required_type: 'lecture', required_capacity: 30 }, candidates: [ROOM], summary: {} };
      else if (url.endsWith('/move/')) {
        room = JSON.parse(options.body).room;
        body = { ok: true, placement: placement(), placements: [placement()], validation: { critical_count: 0, warning_count: 0 }, instructor_clash_backstop: { checked: true, involving_this_section: [] } };
      }
      else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
      else if (url === '/ops/tw/boards/2/') body = boardData();
      else if (url === '/ops/tw/boards/3/') body = { board: { ...board, id: 3, label: 'Other' }, placements: [{ ...placement(), id: 11, board_id: 3 }], slot_config: boardData().slot_config };
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
      else if (url.endsWith('/conflicts/')) body = { overlaps: [], instructor_clashes: [], room_clashes: [] };
      else if (url.endsWith('/plan-lens/')) body = { plan_lens: { plans: [], courses: {}, sections: {} } };
      else body = { ok: true, candidates: [], instructors: [], summary: {} };
    }
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  w.eval(read('static/js/timetable-meeting-policy.js'));
  w.eval(read('static/js/page-timetable-workspace-split.js') + '\n;globalThis.__splitAudit = { S, RP };');
  await settle();
  const { S: state, RP: panel } = w.__splitAudit;
  state.scenarioId = 1; state.scenarioMeta = { id: 1, status: 'draft' }; state.boards = [board];
  state.cols = 2; state.rows = 1;
  state.panes[0].boardId = 2; state.panes[0].boardData = boardData();
  state.panes[1].boardId = 3; state.panes[1].boardData = { board: { ...board, id: 3, label: 'Other' }, placements: [], slot_config: boardData().slot_config };
  state.selectedPlacementId = 10; state.selectedPaneIdx = 0;
  panel.open = true; panel.tab = 'selection';
  w.renderPane(0); w.renderPane(1); w.renderRpanel();
  const moves = () => requests.filter(row => row.url.endsWith('/move/'));
  const apply = () => d.querySelector('.tws-room-apply[data-room-idx="0"]');
  return { w, d, state, notices, requests, moves, apply, room: () => room };
}

test('a locked section changes room at its current slot without override and stays locked', async t => {
  const h = await harness(t);
  await wait(() => h.apply());
  h.apply().click();
  await wait(() => h.notices.some(row => row.kind === 'success'));
  assert.equal(h.moves().length, 1);
  assert.deepEqual(JSON.parse(h.moves()[0].options.body), { day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'R2' });
  assert.deepEqual(h.notices.map(row => row.message), ['Assigned room R2']);
  assert.equal(h.state.undoStack.at(-1).old_room, 'R1');
  assert.equal(h.state.undoStack.at(-1).new_room, 'R2');
  assert.equal(h.state.panes[0].boardData.placements[0].is_locked, true);
});

test('a locked section refuses a room at a previewed new time before any request', async t => {
  const h = await harness(t);
  const result = await h.w.applyRoomCandidate(10, ROOM, { day: 'SUN', start: '10:30', end: '11:45' });
  assert.equal(result, null);
  assert.equal(h.moves().length, 0);
  assert.match(h.notices.at(-1).message, /Locked section: its room can change only at its current time/);
});

test('a server refusal is shown with its reason and records no undo', async t => {
  const h = await harness(t, url => url.endsWith('/move/')
    ? { httpError: 409, ok: false, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'This section is also placed by another scenario, which owns its time and room.' } }
    : undefined);
  await wait(() => h.apply());
  h.apply().click();
  await wait(() => h.notices.some(row => row.kind === 'error'));
  assert.deepEqual(h.notices.filter(row => row.kind === 'error').map(row => row.message),
    ['Room assignment failed: This section is also placed by another scenario, which owns its time and room.']);
  assert.equal(h.state.undoStack.length, 0);
  assert.equal(h.room(), 'R1');
});

test('every board holding a copy of the shared section is reloaded after the room change', async t => {
  const h = await harness(t, (url, options) => url.endsWith('/move/')
    ? { ok: true, placement: { id: 10, board_id: 2 }, placements: [{ id: 10, board_id: 2 }, { id: 11, board_id: 3 }], validation: { critical_count: 0 }, instructor_clash_backstop: {} }
    : undefined);
  await wait(() => h.apply());
  const before = h.requests.filter(row => row.url === '/ops/tw/boards/3/').length;
  h.apply().click();
  await wait(() => h.notices.some(row => row.kind === 'success'));
  assert.equal(h.requests.filter(row => row.url === '/ops/tw/boards/3/').length, before + 1);
});

test('bulk clean rooms reloads every board a shared class was roomed on', async t => {
  const slot = { day: 'SUN', start: '09:00', end: '10:15' };
  const assignment = { placement_id: 10, board_id: 2, board_label: 'Board', label: 'AI10-S1', ...slot, slot, old_room: 'UNASSIGNED', room: { room_code: 'R100' }, new_room: 'R100' };
  const h = await harness(t, url => {
    if (url.startsWith('/ops/tw/scenarios/1/clean-room-assignments/?limit=')) return { assignments: [assignment], total_unassigned: 2, ready_count: 1, skipped: [] };
    if (url === '/ops/tw/scenarios/1/clean-room-assignments/apply/') return { applied_count: 1, applied: [{ ...assignment, old_day: 'SUN', old_start: '09:00', old_end: '10:15', new_day: 'SUN', new_start: '09:00', new_end: '10:15', placement_ids: [10, 11], board_ids: [2, 3] }], skipped: [] };
    return undefined;
  });
  const reloads = () => h.requests.filter(row => row.url === '/ops/tw/boards/3/').length;
  const before = reloads();
  const run = h.w.bulkAssignCleanRooms();
  await wait(() => h.d.querySelector('#twsModalFooter button.primary'));
  h.d.querySelector('#twsModalFooter button.primary').click();
  await run;
  assert.equal(reloads(), before + 1);
});
