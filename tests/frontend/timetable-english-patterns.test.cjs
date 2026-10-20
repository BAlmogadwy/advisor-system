const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const read = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const main = read('page-timetable-workspace.js'), split = read('page-timetable-workspace-split.js');
const slice = (source, begin, end) => source.slice(source.indexOf(begin), source.indexOf(end, source.indexOf(begin)));
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
function harness() {
  const dom = new JSDOM('<button id="anchor">Section</button>', { runScripts: 'outside-only' });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  dom.window.eval(read('timetable-meeting-policy.js'));
  return { dom, policy: dom.window.TimetableMeetingPolicy };
}

test('English has exactly two complete four-day lecture patterns and keeps offering identity rules', () => {
  const h = harness();
  assert.equal(h.policy.patterns.length, 2);
  for (const pattern of h.policy.patterns) {
    assert.equal(pattern.meetings.length, 4);
    assert.equal(pattern.meetings.map(row => row.day).join(','), 'SUN,MON,WED,THU');
    assert.equal(h.policy.identify(pattern.meetings), pattern.pattern_id);
  }
  assert.equal(h.policy.isEnglish({ course_code: 'ENG', course_number: '101' }), true);
  assert.equal(h.policy.isEnglish({ course_code: 'ENG101', course_key: 'ENG102::offering-A' }), true);
  assert.equal(h.policy.isEnglish('ENG103'), false);
  assert.equal(h.policy.isEnglish('AI101'), false);
  assert.equal(h.policy.roomLabel({ course_code: 'ENG101', room: 'Old saved room' }), 'No room required');
  assert.equal(h.policy.roomLabel({ course_code: 'ENG102' }, true), 'لا يحتاج إلى قاعة');
  assert.equal(h.policy.roomLabel({ course_code: 'AI101', room: 'R1' }), 'R1');
  assert.equal(h.policy.identify(h.policy.patterns[0].meetings.slice(0, 3)), null);
  h.dom.window.close();
});

test('pattern chooser has exactly two native options, cancellation and focus restoration', async () => {
  const h = harness(), doc = h.dom.window.document;
  const anchor = doc.getElementById('anchor'); anchor.focus();
  const choice = h.policy.choose('ENG101');
  const buttons = [...doc.querySelectorAll('[data-pattern-id]')];
  assert.equal(buttons.length, 2);
  assert.equal(buttons[0].tagName, 'BUTTON');
  buttons[1].click();
  assert.equal((await choice).pattern_id, 'ENG_INTENSIVE_V1_PM');
  assert.equal(doc.activeElement, anchor);
  const cancelled = h.policy.choose('ENG102', { arabic: true });
  doc.querySelector('.tw-pattern-cancel').click();
  assert.equal(await cancelled, null);
  h.dom.window.close();
});

test('split pattern chooser preserves board choice and disables a backend-unavailable pattern', async () => {
  const h = harness(), doc = h.dom.window.document;
  const choice = h.policy.choose('ENG101', { boards: [
    { id: 1, label: 'Board A', candidates: [{ pattern_id: 'ENG_INTENSIVE_V1_AM' }] },
    { id: 2, label: 'Board B', candidates: [{ pattern_id: 'ENG_INTENSIVE_V1_PM' }] },
  ] });
  const buttons = [...doc.querySelectorAll('[data-pattern-id]')];
  assert.equal(buttons.length, 2);
  assert.equal(buttons[1].disabled, true);
  const select = doc.querySelector('select'); select.value = '2'; select.dispatchEvent(new h.dom.window.Event('change'));
  assert.equal(buttons[0].disabled, true); assert.equal(buttons[1].disabled, false);
  buttons[1].click();
  assert.equal((await choice).boardId, 2);
  h.dom.window.close();
});

test('split renders all English meetings in continuous three-hour lecture cells', () => {
  const h = harness();
  const context = vm.createContext({ TimetableMeetingPolicy: h.policy, S: { scenarioMeta: {} }, IS_AR: false,
    DAYS: ['SUN', 'MON', 'TUE', 'WED', 'THU'], DAY_LABELS: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'], esc,
    placementIssuesFromMap: () => [], courseColor: () => '#eee', courseColorBorder: () => '#bbb', planLensBadgesHtml: () => '',
  });
  vm.runInContext(slice(split, 'function renderGridHTML(', 'function bindPaneControls('), context);
  const placements = h.policy.patterns[0].meetings.map((row, index) => ({ ...row, id: index + 1, course_code: 'ENG101', section: 'S1' }));
  const html = context.renderGridHTML([{ start: '09:00', end: '10:15' }], placements, new Map(), 'lect');
  const doc = new JSDOM(html).window.document;
  assert.equal(doc.querySelectorAll('.english-grid .filled').length, 4);
  assert.equal(doc.querySelectorAll('.english-grid .filled[data-start="09:00"][data-end="12:00"]').length, 4);
  assert.equal(doc.querySelectorAll('.lab-grid .filled').length, 0);
  assert.equal(doc.querySelectorAll('.english-grid [data-day="TUE"].filled').length, 0);
  assert.match(html, /No room required/);
  h.dom.window.close();
});

test('English room picker reports no room required without a shortage message and retains meeting times', async () => {
  const h = harness();
  const body = h.dom.window.document.createElement('div');
  body.innerHTML = '<div id="twsRoomCandidates"></div><div id="twsStudentEvidence"></div>';
  const context = vm.createContext({ TimetableMeetingPolicy: h.policy, S: { selectedPlacementId: 1 }, IS_AR: false, esc,
    RP: { open: true, tab: 'selection', builder: { token: 0, roomCache: {}, studentCache: {} } }, URLSearchParams,
    selectionRoomSlot: () => ({ day: 'SUN', start: '09:00', end: '12:00', label: 'Morning pattern' }),
    roomCacheKey: () => '1:SUN:09:00', studentEvidenceHtml: () => '',
    api: async url => url.includes('/room-candidates/') ? { summary: { room_required: false }, candidates: [] } : {},
  });
  vm.runInContext(slice(split, 'async function refreshSelectionBuilderPanels(', 'function renderRpanelSelection('), context);
  await context.refreshSelectionBuilderPanels(body, { id: 1, course_code: 'ENG101', room: 'Old room' }, null);
  assert.match(body.querySelector('#twsRoomCandidates').textContent, /No room required/);
  assert.match(body.querySelector('#twsRoomCandidates').textContent, /SUN 09:00-12:00/);
  assert.match(body.querySelector('#twsRoomCandidates').textContent, /instructor checks still apply/);
  assert.doesNotMatch(body.querySelector('#twsRoomCandidates').textContent, /No rooms found|dedicated/i);
  h.dom.window.close();
});

test('main whole-pattern undo/redo sends one atomic switch with all room and lock snapshots', async () => {
  const calls = [];
  const context = vm.createContext({ twFetch: async (url, options) => { calls.push({ url, body: JSON.parse(options.body) }); return { ok: true }; } });
  vm.runInContext(slice(main, 'async function applyHistoryAction(', 'async function runHistoryAction('), context);
  const action = { type: 'pattern', placement_id: 4, old_pattern_id: 'ENG_INTENSIVE_V1_AM', new_pattern_id: 'ENG_INTENSIVE_V1_PM',
    before_placements: [{ id: 4, room: 'R1', is_locked: false }, { id: 5, room: 'R2', is_locked: true }],
    placements: [{ id: 4, room: 'R3', is_locked: false }, { id: 5, room: 'R4', is_locked: true }] };
  await context.applyHistoryAction(action, true, {}); await context.applyHistoryAction(action, false, {});
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, '/ops/tw/placements/4/switch-pattern/');
  assert.equal(calls[0].body.pattern_id, 'ENG_INTENSIVE_V1_AM');
  assert.equal(calls[0].body.placements[1].room, 'R2');
  assert.equal(calls[1].body.placements[1].is_locked, true);
});

test('recreating a whole section remaps earlier pattern history and every per-day snapshot', () => {
  const h = harness();
  const before = h.policy.patterns[0].meetings.map((row, index) => ({ ...row, id: index + 1, board_id: 10, room: `R${index}` }));
  const after = before.map(row => ({ ...row, id: row.id + 100 }));
  const history = [{ type: 'pattern', placement_id: 1, before_placements: h.policy.snapshot(before), placements: h.policy.snapshot(before) },
    { type: 'bundle_create', placement_ids: [1, 2, 3, 4], meetings: h.policy.snapshot(before) }];
  h.policy.remapHistory(history, before, after);
  assert.equal(history[0].placement_id, 101);
  assert.equal(history[0].before_placements[3].id, 104);
  assert.equal(history[0].before_placements[3].room, 'R3');
  assert.deepEqual(history[1].placement_ids, [101, 102, 103, 104]);
  h.dom.window.close();
});

test('create then switch then remove can undo and redo every action using recreated section IDs', async () => {
  const h = harness(), calls = [];
  const morning = h.policy.patterns[0].meetings.map((row, index) => ({ ...row, id: index + 1, board_id: 10, term_section_id: 7, room: `R${index}`, is_locked: false }));
  const afternoon = morning.map(row => ({ ...row, start_time: '13:00', end_time: '16:00' }));
  const create = { type: 'create', placement_id: 1, board_id: 10, term_section_id: 7, day: 'SUN', start_time: '09:00', end_time: '12:00', meetings: h.policy.snapshot(morning) };
  const pattern = { type: 'pattern', placement_id: 1, old_pattern_id: 'ENG_INTENSIVE_V1_AM', new_pattern_id: 'ENG_INTENSIVE_V1_PM', before_placements: h.policy.snapshot(morning), placements: h.policy.snapshot(afternoon) };
  const remove = { ...create, type: 'remove', start_time: '13:00', end_time: '16:00', meetings: h.policy.snapshot(afternoon) };
  let rows = [], nextId = 101;
  const state = { scenarioId: 1, boardId: 10, undoStack: [create, pattern, remove], redoStack: [] };
  const context = vm.createContext({ TW: state, TimetableMeetingPolicy: h.policy, workspaceCanEdit: () => true,
    beginWorkspaceOperation: () => ({ scenarioId: 1 }), finishWorkspaceOperation: () => {}, selectBoard: async () => {},
    twFetch: async (url, options) => {
      const body = JSON.parse(options.body); calls.push({ url, body });
      if (url.endsWith('/create/')) { rows = body.meetings.map(row => ({ ...row, id: nextId++ })); return { ok: true, placement: rows[0], placements: rows }; }
      const id = Number(url.match(/placements\/(\d+)\//)[1]);
      assert.ok(rows.some(row => row.id === id), `History refers to active ID ${id}`);
      if (url.endsWith('/remove/')) { const placement = rows.find(row => row.id === id); rows = []; return { ok: true, removed: placement }; }
      const selected = h.policy.patterns.find(row => row.pattern_id === body.pattern_id);
      rows = rows.map(row => ({ ...row, ...body.placements.find(item => item.id === row.id), start_time: selected.start, end_time: selected.end }));
      return { ok: true, placement: rows.find(row => row.id === id), placements: rows };
    },
  });
  vm.runInContext(slice(main, 'async function applyHistoryAction(', "$('twUndo').addEventListener"), context);
  await context.runHistoryAction(true); // restore deleted afternoon bundle with IDs 101–104
  assert.equal(rows.length, 4); assert.equal(rows[0].id, 101);
  await context.runHistoryAction(true); // restore morning using recreated IDs and original rooms
  assert.equal(rows[0].start_time, '09:00'); assert.equal(rows[3].room, 'R3');
  await context.runHistoryAction(true); // undo original create using ID 101
  assert.equal(rows.length, 0); assert.equal(state.undoStack.length, 0);
  await context.runHistoryAction(false); // redo create: IDs 105–108
  await context.runHistoryAction(false); // redo whole afternoon switch
  assert.equal(rows[0].id, 105); assert.equal(rows[0].start_time, '13:00');
  await context.runHistoryAction(false); // redo remove using ID 105
  assert.equal(rows.length, 0); assert.equal(state.redoStack.length, 0);
  assert.equal(calls.filter(call => call.url.endsWith('/switch-pattern/')).length, 2);
  h.dom.window.close();
});

test('split bundle removal uses removed IDs so one atomic deletion does not delete companions twice', async () => {
  const calls = [];
  const context = vm.createContext({ doRemoveApi: async id => { calls.push(id); return { removed_ids: [1, 2, 3, 4] }; } });
  vm.runInContext(slice(split, 'async function removePlacementList(', 'async function doUndo('), context);
  assert.equal((await context.removePlacementList([1, 2, 3, 4])).ok, true);
  assert.deepEqual(calls, [4]);
});

test('split whole-section create, switch and removal history survives undo/redo with new IDs', async () => {
  const h = harness(), calls = [];
  const morning = h.policy.patterns[0].meetings.map((row, index) => ({ ...row, id: index + 1, board_id: 10, term_section_id: 7, course_code: 'ENG101', room: `R${index}`, is_locked: false }));
  const afternoon = morning.map(row => ({ ...row, start_time: '13:00', end_time: '16:00' }));
  const create = { type: 'bundle_create', english_pattern: true, placement_ids: [1, 2, 3, 4], board_id: 10,
    payload: { course_code: 'ENG101', section_label: 'S1' }, meetings: h.policy.snapshot(morning) };
  const pattern = { type: 'pattern', placement_id: 1, old_pattern_id: 'ENG_INTENSIVE_V1_AM', new_pattern_id: 'ENG_INTENSIVE_V1_PM', before_placements: h.policy.snapshot(morning), placements: h.policy.snapshot(afternoon) };
  const remove = { type: 'remove', placement_id: 1, board_id: 10, term_section_id: 7,
    day: 'SUN', start_time: '13:00', end_time: '16:00', meetings: h.policy.snapshot(afternoon) };
  let rows = [], nextId = 101;
  const state = { scenarioId: 1, scenarioMeta: { status: 'draft' }, panes: [{ boardId: 10, boardData: { placements: [] } }], undoStack: [create, pattern, remove], redoStack: [] };
  const refresh = async () => { state.panes[0].boardData.placements = rows; };
  const context = vm.createContext({ S: state, IS_AR: false, TimetableMeetingPolicy: h.policy, notify: { success: () => {}, warning: () => {}, error: () => {} },
    findPlacement: id => { const placement = rows.find(row => row.id === id); return placement ? { placement, paneIdx: 0 } : null; },
    placementProtectionReasons: () => [], updateUndoRedoButtons: () => {}, paneCount: () => 1, loadAndRenderPane: refresh,
    refreshBoardsSummary: async () => {}, loadSidebarBudget: async () => {}, refreshEnglishSplit: refresh,
    clearSlotAssist: () => {}, notifyInstructorClashBackstop: () => {},
    api: async (url, options) => {
      // The pattern read is a preview (a GET with only the `_preview` option).
      if (!options?.body) return { section_policy: { current_pattern_id: h.policy.identify(rows) }, candidates: h.policy.patterns };
      const body = JSON.parse(options.body); calls.push({ url, body });
      if (url.endsWith('/create/') || url.endsWith('/create-planned/')) {
        rows = body.meetings.map(row => ({ ...row, id: nextId++, course_code: 'ENG101' })); await refresh();
        return { placement: rows[0], placements: rows };
      }
      const id = Number(url.match(/placements\/(\d+)\//)[1]);
      assert.ok(rows.some(row => row.id === id), `Split history refers to active ID ${id}`);
      if (url.endsWith('/remove/')) { const removed_ids = rows.map(row => row.id); rows = []; await refresh(); return { removed_ids }; }
      const selected = h.policy.patterns.find(row => row.pattern_id === body.pattern_id);
      rows = rows.map(row => ({ ...row, ...body.placements.find(item => item.id === row.id), start_time: selected.start, end_time: selected.end }));
      await refresh(); return { placement: rows.find(row => row.id === id), placements: rows };
    },
  });
  vm.runInContext(slice(split, 'async function switchEnglishSplitPattern(', 'async function chooseEnglishSplitCreate('), context);
  vm.runInContext(slice(split, 'async function apiCreateWithBackstop(', '/* ── Top-bar actions'), context);
  await context.doUndo(); await context.doUndo();
  assert.equal(rows[0].id, 101); assert.equal(rows[0].start_time, '09:00'); assert.equal(rows[3].room, 'R3');
  await context.doUndo(); assert.equal(rows.length, 0);
  await context.doRedo(); await context.doRedo();
  assert.equal(rows[0].id, 105); assert.equal(rows[0].start_time, '13:00');
  await context.doRedo(); assert.equal(rows.length, 0);
  assert.equal(calls.filter(call => call.url.endsWith('/switch-pattern/')).length, 2);
  assert.equal(calls.filter(call => call.url.endsWith('/remove/')).length, 2);
  h.dom.window.close();
});
