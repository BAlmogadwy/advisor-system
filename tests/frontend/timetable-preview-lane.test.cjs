/* Heavy previews and the server's preview lane: a 503 SERVER_BUSY is retried
 * after Retry-After for the newest selection only, the panel keeps its loading
 * state meanwhile, superseded requests are aborted, and a drag that ends at
 * once sends no outcome preview. Full split script and template; controlled
 * HTTP responses; no server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };
const wait = async condition => { for (let i = 0; i < 200; i++) { if (condition()) return; await settle(); } assert.fail('UI did not settle'); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const room = code => ({ room_code: code, room_type: 'lecture', section: '', capacity: 40, available: true, fits_type: true, fits_gender: true, fits_capacity: true, department_fit: true, schedule_clean: true, policy_clean: true, slot_clean: true, reasons: [], occupied_by: [], validation: { critical_count: 0, warning_count: 0 } });
const BUSY = { httpError: 503, retryAfter: '3', code: 'SERVER_BUSY', ok: false, error: { code: 'SERVER_BUSY', message: 'The server is busy with other previews. Retrying shortly.' } };

async function splitHarness(t, intercept = () => undefined) {
  const html = read('core/templates/core/timetable_workspace_split.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://split.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [], delays = [];
  const placement = id => ({ id, board_id: 2, term_section_id: id + 100, course_code: `AI${id}`, course_key: `AI${id}`, course_name: 'Course', section: 'S1', day: 'SUN', start_time: id === 10 ? '09:00' : '10:30', end_time: id === 10 ? '10:15' : '11:45', room: 'R1', is_locked: false, meetings: [] });
  const board = { id: 2, label: 'Board', nominal_term: 1, primary_count: 1, placement_count: 2 };
  const boardData = () => ({ board, placements: [placement(10), placement(11)], slot_config: [{ start: '09:00', end: '10:15' }, { start: '10:30', end: '11:45' }] });
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  Object.assign(w, { LANGUAGE_CODE: 'en', djCsrfToken: 'audit', INITIAL_SCENARIO: '', INITIAL_BOARD: '', DEFAULT_YEAR: '1448', DEFAULT_TERM: '1' });
  w.esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  w.notify = Object.fromEntries(['success', 'warning', 'error', 'info'].map(kind => [kind, message => notices.push({ kind, message })]));
  w.console.error = () => {};
  w.matchMedia = () => ({ matches: false });
  w.HTMLElement.prototype.scrollIntoView = () => {};
  // Waits run at once; the delays asked for are recorded.
  const realSetTimeout = w.setTimeout.bind(w);
  w.setTimeout = (fn, ms = 0, ...args) => { delays.push(ms); return realSetTimeout(fn, 0, ...args); };
  w.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    let body = await intercept(url, options);
    if (body === undefined) {
      if (url.includes('/room-candidates/')) {
        const id = Number(url.match(/placements\/(\d+)/)[1]);
        body = { ok: true, target: { day: 'SUN', start: '09:00', end: '10:15', required_type: 'lecture', required_capacity: 30 }, candidates: [room(`ROOM${id}`)], summary: {} };
      }
      else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
      else if (url === '/ops/tw/boards/2/') body = boardData();
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
      else if (url.endsWith('/conflicts/')) body = { overlaps: [], instructor_clashes: [], room_clashes: [] };
      else if (url.endsWith('/plan-lens/')) body = { plan_lens: { plans: [], courses: {}, sections: {} } };
      else body = { ok: true, candidates: [], instructors: [], summary: {} };
    }
    const headers = { get: name => (name.toLowerCase() === 'retry-after' ? body.retryAfter || null : null) };
    return { ok: !body.httpError, status: body.httpError || 200, headers, json: async () => body };
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
  panel.open = true; panel.tab = 'selection';
  w.renderPane(0);
  const select = id => { state.selectedPlacementId = id; state.selectedPaneIdx = 0; w.renderRpanel(); };
  const roomBox = () => d.getElementById('twsRoomCandidates');
  const roomRequests = id => requests.filter(row => row.url.includes(`/placements/${id}/room-candidates/`));
  return { w, d, state, panel, requests, notices, delays, select, roomBox, roomRequests };
}

for (const nextBoardId of [2, 3]) {
  test(`split pane keeps the newest load when board 2 replies after board ${nextBoardId}`, async t => {
    const late = deferred();
    let holdFirst = true;
    let latestData;
    const h = await splitHarness(t, url => {
      if (url === '/ops/tw/boards/2/' && holdFirst) {
        holdFirst = false;
        return late.promise;
      }
      if (url === `/ops/tw/boards/${nextBoardId}/`) return latestData;
    });
    const previousData = h.state.panes[0].boardData;
    const board = { ...previousData.board, id: nextBoardId, label: 'Latest selection' };
    latestData = { ...previousData, board, placements: [] };
    h.state.boards = [board];
    const first = h.w.setPaneBoard(0, 2);
    await h.w.setPaneBoard(0, nextBoardId);
    late.resolve(previousData);
    await first;
    assert.equal(h.state.panes[0].boardData, latestData);
    const pane = h.d.querySelector('.tws-pane[data-idx="0"]');
    assert.match(pane.textContent, /Latest selection/);
    assert.doesNotMatch(pane.textContent, /AI10/);
  });
}

test('a superseded scenario detail cannot replace the selected scenario or its boards', async t => {
  const late = deferred();
  const lateBoard = deferred();
  let holdBoard = true;
  const h = await splitHarness(t, url => {
    if (url === '/ops/tw/boards/2/' && holdBoard) {
      holdBoard = false;
      return lateBoard.promise;
    }
    if (url === '/ops/tw/scenarios/1/') return late.promise;
    if (url === '/ops/tw/scenarios/2/') return { scenario: { id: 2, name: 'Latest', status: 'draft' } };
  });
  const picker = h.d.getElementById('twsScenario');
  picker.innerHTML = '<option value="1">First</option><option value="2">Latest</option>';
  const oldPane = h.w.loadSplitPane(0);
  h.state.panes[1].boardId = 99;
  picker.value = '1';
  const first = h.w.onScenarioChange();
  picker.value = '2';
  await h.w.onScenarioChange();
  late.resolve({ scenario: { id: 1, name: 'Previous', status: 'draft' } });
  lateBoard.resolve({ board: { id: 2 }, placements: [] });
  assert.equal(await oldPane, false);
  await first;
  assert.equal(h.state.scenarioId, 2);
  assert.equal(h.state.panes[0].boardData.placements.length, 2);
  assert.equal(h.state.panes[1].boardId, null);
  assert.equal(h.requests.filter(({ url }) => url === '/ops/tw/boards/?scenario_id=1').length, 0);
});

test('a late summary cannot replace another scenario’s board picker or metrics', async t => {
  const late = deferred();
  const board = { id: 22, label: 'Second board', nominal_term: 1 };
  const h = await splitHarness(t, url => {
    if (url === '/ops/tw/boards/?scenario_id=1') return late.promise;
    if (url === '/ops/tw/scenarios/2/') return { scenario: { id: 2, name: 'Second', status: 'draft' } };
    if (url === '/ops/tw/boards/?scenario_id=2') return { boards: [board], scenario_summary: { unique_students: 17 } };
    if (url === '/ops/tw/boards/22/') return { board, placements: [] };
  });
  const first = h.w.refreshBoardsSummary();
  h.d.getElementById('twsScenario').innerHTML = '<option value="2">Second</option>';
  assert.equal(await h.w.onScenarioChange(), true);
  const remainingRequests = h.requests.length;
  late.resolve({ boards: [{ id: 2, label: 'First board' }], scenario_summary: { unique_students: 50 } });
  assert.equal(await first, false);
  assert.equal(h.state.scenarioId, 2);
  assert.equal(h.state.boards[0].id, 22);
  assert.equal(h.state.panes[0].boardId, 22);
  assert.equal(h.d.querySelector('[data-role="board-select"]').value, '22');
  assert.equal(h.d.getElementById('twsStStudents').textContent, '17');
  assert.equal(h.requests.length, remainingRequests);
});

test('two summaries for the same scenario keep the most recently requested result', async t => {
  const late = deferred();
  let calls = 0;
  const h = await splitHarness(t, url => {
    if (url === '/ops/tw/boards/?scenario_id=1') {
      if (++calls === 1) return late.promise;
      return { boards: [{ id: 2, label: 'Current' }], scenario_summary: { unique_students: 23 } };
    }
  });
  const first = h.w.refreshBoardsSummary();
  assert.equal(await h.w.refreshBoardsSummary(), true);
  late.resolve({ boards: [{ id: 9, label: 'Outdated' }] });
  assert.equal(await first, false);
  assert.equal(h.d.querySelector('[data-role="board-select"]').value, '2');
  assert.equal(h.d.getElementById('twsStStudents').textContent, '23');
});

for (const failedRequest of ['detail', 'boards']) {
  test(`scenario switch clears the previous board and actions while ${failedRequest} loads and after failure`, async t => {
    const late = deferred();
    const h = await splitHarness(t, url => {
      if (url === '/ops/tw/scenarios/2/') {
        return failedRequest === 'detail' ? late.promise : { scenario: { id: 2, name: 'Second', status: 'draft' } };
      }
      if (url === '/ops/tw/boards/?scenario_id=2') return late.promise;
    });
    const picker = h.d.getElementById('twsScenario');
    picker.innerHTML = '<option value="">Select</option><option value="2">Second</option>';
    picker.value = '2';
    h.d.getElementById('twsPublish').disabled = false;
    const loading = h.w.onScenarioChange();
    const pendingUrl = failedRequest === 'detail' ? '/ops/tw/scenarios/2/' : '/ops/tw/boards/?scenario_id=2';
    await wait(() => h.requests.some(r => r.url === pendingUrl));
    const assertCleared = () => {
      assert.equal(h.state.scenarioId, null);
      assert.equal(h.state.panes[0].boardData, null);
      assert.equal(h.d.querySelector('[data-role="board-select"]'), null);
      assert.doesNotMatch(h.d.querySelector('.tws-pane').textContent, /AI10/);
      for (const id of ['twsPublish', 'twsOptimise', 'twsExport', 'twsNewBoard']) {
        assert.equal(h.d.getElementById(id).disabled, true, id);
      }
    };
    assertCleared();
    late.resolve({ httpError: 500, error: { message: 'Load failed' } });
    assert.equal(await loading, false);
    assertCleared();
    assert.equal(picker.value, '');
    assert.equal(picker.disabled, false);
  });
}

function secondScenario(url) {
  const board = { id: 22, label: 'Second board', nominal_term: 1 };
  if (url === '/ops/tw/scenarios/2/') return { scenario: { id: 2, name: 'Second', status: 'draft' } };
  if (url === '/ops/tw/boards/?scenario_id=2') return { boards: [board] };
  if (url === '/ops/tw/boards/22/') return { board, placements: [], slot_config: [{ start: '09:00', end: '10:15' }] };
}

for (const navigation of ['scenario', 'board', 'hide pane']) {
  test(`a section preview loses its placement targets after changing the ${navigation}`, async t => {
    const late = deferred();
    const h = await splitHarness(t, url => url.includes('/planned-slot-candidates/') ? late.promise : secondScenario(url));
    h.w.beginCreateSectionAssist({ course_code: 'OLD001', course_key: 'OLD001', section_label: 'S1' });
    await wait(() => h.requests.some(r => r.url.includes('/planned-slot-candidates/')));
    if (navigation === 'scenario') {
      h.d.getElementById('twsScenario').innerHTML = '<option value="2">Second</option>';
      await h.w.onScenarioChange();
    } else if (navigation === 'board') {
      await h.w.setPaneBoard(0, 22);
    } else {
      await h.w.togglePaneOff(0);
    }
    late.resolve({ candidates: [{ kind: 'lect', day: 'MON', start: '09:00', end: '10:15' }] });
    await settle();
    assert.equal(h.state.createAssist.active, false);
    assert.equal(h.state.createAssist.candidates.size, 0);
    assert.equal(h.d.querySelector('.tws-create-target, [data-create-assist]'), null);
    assert.equal(h.requests.find(r => r.url.includes('/planned-slot-candidates/')).options.signal.aborted, true);
    assert.equal(h.requests.some(r => r.url.endsWith('/create-planned/')), false);
  });
}

test('a move preview cannot highlight cells after its board changes', async t => {
  const late = deferred();
  const h = await splitHarness(t, url => url.includes('/student-outcome-candidates/') ? late.promise : secondScenario(url));
  h.w.beginSlotAssist(0, 10);
  await wait(() => h.requests.some(r => r.url.includes('/student-outcome-candidates/')));
  await h.w.setPaneBoard(0, 22);
  late.resolve({ candidates: [{ day: 'MON', start: '09:00', end: '10:15' }] });
  await settle();
  assert.equal(h.state.slotAssist.active, false);
  assert.equal(h.d.querySelector('[data-slot-assist-key]'), null);
  assert.equal(h.requests.find(r => r.url.includes('/student-outcome-candidates/')).options.signal.aborted, true);
});

for (const edit of ['blocked slot', 'planned section']) {
  test(`the scenario picker waits for a ${edit} save and keeps its result on the original scenario`, async t => {
    const late = deferred();
    const h = await splitHarness(t, url => (url.endsWith('/blocked-slots/') || url.endsWith('/create-planned/')) ? late.promise : secondScenario(url));
    const picker = h.d.getElementById('twsScenario');
    picker.innerHTML = '<option value="1">First</option><option value="2">Second</option>';
    const editing = edit === 'blocked slot'
      ? h.w.doToggleBlockedSlot('MON', '09:00')
      : h.w.createPlannedPlacementFromCell(0, h.d.querySelector('.cell[data-day="MON"][data-start="09:00"]'), { course_code: 'OLD001', section_label: 'S1' });
    assert.equal(picker.disabled, true);
    picker.value = '2';
    assert.equal(await h.w.onScenarioChange(), false);
    assert.equal(picker.value, '1');
    assert.equal(h.requests.some(r => r.url === '/ops/tw/scenarios/2/'), false);
    late.resolve(edit === 'blocked slot'
      ? { blocked: true, blocked_slots: [{ day: 'MON', start: '09:00' }] }
      : { placement: { id: 85, term_section_id: 185 }, validation: {} });
    await editing;
    assert.equal(picker.disabled, false);
    if (edit === 'blocked slot') assert.equal(h.state.scenarioMeta.blocked_slots[0].day, 'MON');
    else assert.equal(h.state.undoStack.at(-1).placement_id, 85);
    picker.value = '2';
    assert.equal(await h.w.onScenarioChange(), true);
    assert.equal(h.state.scenarioId, 2);
    assert.equal(h.state.scenarioMeta.blocked_slots, undefined);
    assert.equal(h.state.undoStack.length, 0);
    await h.w.doUndo();
    assert.equal(h.requests.some(r => r.url.endsWith('/remove/')), false);
  });
}

test('one builder-actions request supplies the Issues panel with publish readiness', async t => {
  const readiness = { ready: true, blockers: [], warnings: [] };
  const h = await splitHarness(t, url => {
    if (url.includes('/builder-actions/')) return { readiness, actions: [] };
  });
  h.panel.tab = 'issues';
  await h.w.refreshBuilderConsole(h.d.body);
  assert.equal(h.panel.builder.readiness, readiness);
  assert.equal(h.requests.filter(({ url }) => url.includes('/builder-actions/')).length, 1);
  assert.equal(h.requests.filter(({ url }) => url.endsWith('/readiness/')).length, 0);
});

test('a busy room preview keeps its loading state and is retried after Retry-After', async t => {
  let calls = 0;
  const h = await splitHarness(t, url => (url.includes('/room-candidates/') && calls++ === 0 ? BUSY : undefined));
  h.select(10);
  await wait(() => h.roomRequests(10).length === 1);
  assert.match(h.roomBox().textContent, /Finding rooms/);
  assert.doesNotMatch(h.roomBox().textContent, /No rooms found/);
  await wait(() => /ROOM10/.test(h.roomBox().textContent));
  assert.equal(h.roomRequests(10).length, 2);
  assert.ok(h.delays.includes(3000), `Retry-After 3 s should be honoured: ${h.delays}`);
  assert.deepEqual(h.notices.filter(row => row.kind === 'error'), []);
});

test('only the latest selection is retried; the superseded request is aborted and discarded', async t => {
  const first = deferred();
  const h = await splitHarness(t, url => (url.includes('/placements/10/room-candidates/') ? first.promise : undefined));
  h.select(10);
  await wait(() => h.roomRequests(10).length === 1);
  h.select(11);
  await wait(() => /ROOM11/.test(h.roomBox().textContent));
  assert.equal(h.roomRequests(10)[0].options.signal.aborted, true);
  // The old answer arrives late (and busy): it neither repaints nor retries.
  first.resolve(BUSY);
  await settle(); await settle();
  assert.equal(h.roomRequests(10).length, 1);
  assert.match(h.roomBox().textContent, /ROOM11/);
  assert.doesNotMatch(h.roomBox().textContent, /ROOM10/);
});

test('a busy answer for an older selection is never retried once a newer one exists', async t => {
  const h = await splitHarness(t, url => (url.includes('/placements/10/room-candidates/') ? BUSY : undefined));
  h.select(10);
  await wait(() => h.roomRequests(10).length >= 1);
  h.select(11);
  await wait(() => /ROOM11/.test(h.roomBox().textContent));
  await settle(); await settle();
  assert.ok(h.roomRequests(10).length <= 2, 'the superseded selection stops retrying');
  const settled = h.roomRequests(10).length;
  for (let i = 0; i < 10; i++) await settle();
  assert.equal(h.roomRequests(10).length, settled);
});

test('a drag that ends at once sends no outcome preview; one that lingers sends exactly one', async t => {
  const h = await splitHarness(t);
  const outcomes = () => h.requests.filter(row => row.url.includes('/student-outcome-candidates/'));
  const realSetTimeout = h.w.setTimeout;
  // Hold the debounce until the drag has ended.
  const held = [];
  h.w.setTimeout = (fn, ms, ...args) => { if (ms === 280) { held.push(() => fn(...args)); return 0; } return realSetTimeout(fn, ms, ...args); };
  h.w.beginSlotAssist(0, 10);
  await settle();
  h.w.clearSlotAssist();
  held.splice(0).forEach(fire => fire());
  await settle(); await settle();
  assert.equal(outcomes().length, 0);
  h.w.setTimeout = realSetTimeout;
  h.w.beginSlotAssist(0, 10);
  await wait(() => outcomes().length === 1);
  await settle(); await settle();
  assert.equal(outcomes().length, 1);
});

test('a busy outcome preview during a drag is retried and still painted', async t => {
  let calls = 0;
  const h = await splitHarness(t, url => (url.includes('/student-outcome-candidates/') && calls++ === 0 ? BUSY : undefined));
  h.w.beginSlotAssist(0, 10);
  await wait(() => h.requests.filter(row => row.url.includes('/student-outcome-candidates/')).length === 2);
  assert.equal(h.state.slotAssist.active, true);
  assert.ok(h.d.querySelectorAll('.tws-pane .cell[data-slot-assist-key]').length > 0, 'local candidates stay painted');
});

/* Classic workspace: a read refused as busy is asked again after Retry-After. */
test('classic workspace retries a busy read and never retries a write', async () => {
  const template = read('core/templates/core/timetable_workspace.html');
  const html = template.replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost/timetable-workspace/' });
  const w = dom.window, requests = [], notices = [], delays = [];
  w.LANGUAGE_CODE = 'en';
  w._injectCsrf = x => x;
  w.esc = x => String(x ?? '');
  w.debounce = fn => fn;
  w.notify = Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => notices.push({ type, text })]));
  w.dlg = { confirm: async () => false };
  const realSetTimeout = w.setTimeout.bind(w);
  w.setTimeout = (fn, ms = 0, ...args) => { delays.push(ms); return realSetTimeout(fn, 0, ...args); };
  let busyLeft = 1;
  w.fetch = async (url, opts = {}) => {
    requests.push({ url, opts });
    let answer;
    if (url.includes('/slot-candidates/') || url.endsWith('/move/')) answer = busyLeft-- > 0 || url.endsWith('/move/') ? BUSY : { candidates: [{ day: 'SUN' }] };
    else if (url.startsWith('/ops/tw/scenarios/?')) answer = { scenarios: [] };
    else answer = {};
    const headers = { get: name => (name.toLowerCase() === 'retry-after' ? answer.retryAfter || null : null) };
    return { ok: !answer.httpError, status: answer.httpError || 200, headers, json: async () => answer };
  };
  w.document.getElementById('twYear').value = '1448'; w.document.getElementById('twTerm').value = '1';
  w.eval(read('static/js/page-timetable-workspace.js'));
  const data = await w.twFetch('/ops/tw/placements/10/slot-candidates/');
  assert.deepEqual(data.candidates, [{ day: 'SUN' }]);
  assert.equal(requests.filter(row => row.url.includes('/slot-candidates/')).length, 2);
  assert.ok(delays.includes(3000));
  const moved = await w.twFetch('/ops/tw/placements/10/move/', { method: 'POST', body: '{}' });
  assert.equal(moved, null);
  assert.equal(requests.filter(row => row.url.endsWith('/move/')).length, 1);
  assert.match(notices.at(-1).text, /busy/);
  w.close();
});
