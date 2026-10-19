const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const read = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const slice = (source, begin, end) => source.slice(source.indexOf(begin), source.indexOf(end, source.indexOf(begin)));
const mri = read('page-timetable-mri.js');
const graph = read('page-timetable-graph.js');
const split = read('page-timetable-workspace-split.js');

function mriHarness(quality) {
  const context = vm.createContext({
    state: { quality, conflicts: new Map(), capacities: new Map(), boardDetails: new Map() },
    toneFor: pressure => pressure >= 70 ? 'critical' : pressure >= 35 ? 'watch' : 'stable',
  });
  vm.runInContext(slice(mri, '  function boardStudents(', '  async function loadScenarios('), context);
  return context;
}

test('full MRI room audit scopes true room shortages by placement and does not turn unknown into zero', () => {
  const room = (id, ids, reasons) => ({ demand_id: id, placement_ids: ids, reasons });
  const context = mriHarness({ rooms: { assessed: true, details: [room('a', [11], ['missing']), room('b', [22], ['undersized']), room('c', [33], ['incompatible'])] } });
  context.state.boardDetails.set(1, { placements: [{ id: 11 }] });
  context.state.boardDetails.set(2, { placements: [{ id: 22 }, { id: 33 }] });
  const first = context.scanBoard({ id: 1 }), second = context.scanBoard({ id: 2 });
  assert.equal(first.rooms, 1);
  assert.equal(second.rooms, 2);
  assert.equal(first.roomClashes, 0);
  assert.equal(context.aggregateScan([first, second]).rooms, 3);
  context.state.quality = { rooms: { assessed: false, shortages: null } };
  context.state.conflicts.set(1, { room_clashes: [{}] });
  const unknown = context.scanBoard({ id: 1 });
  assert.equal(unknown.roomsAssessed, false);
  assert.equal(unknown.roomClashes, 1);
  assert.equal(context.boardRoomAudit(null, [{ id: 11 }]).shortages, null);
});

test('system MRI counts a physical meeting once when it is linked to multiple board placements', () => {
  const context = mriHarness({ rooms: { assessed: true, details: [{ demand_id: 'same', placement_ids: [11, 22], reasons: ['missing'] }] } });
  context.state.boardDetails.set(1, { placements: [{ id: 11 }] });
  context.state.boardDetails.set(2, { placements: [{ id: 22 }] });
  assert.equal(context.aggregateScan([context.scanBoard({ id: 1 }), context.scanBoard({ id: 2 })]).rooms, 1);
});

test('MRI pressure distinguishes assessed suitability from known room-sharing clashes and marks course-demand evidence', () => {
  const context = mriHarness(null);
  context.num = value => String(value || 0);
  context.plural = (count, label) => `${count} ${label}`;
  vm.runInContext(slice(mri, '  function pressureComponents(', '  function capacityCourseDiagnosis('), context);
  const unknown = context.scanBoard({ id: 1 });
  let rows = context.pressureComponents(unknown);
  assert.match(rows.find(row => row.label.startsWith('Room')).label, /audit unavailable/);
  assert.match(rows.find(row => row.label === 'Cross-board network').detail, /potential course-demand overlap/);
  context.state.quality = { rooms: { assessed: true, details: [] } };
  rows = context.pressureComponents(context.scanBoard({ id: 1 }));
  assert.equal(rows.find(row => row.label.startsWith('Room')).label, 'Room suitability');
});

// H15 severity is decided once on the server: a same-course overlap or one
// that leaves students with no clash-free pair is critical; one they can route
// round through sibling sections is a warning, and the MRI must not paint it red.
test('MRI reads each overlap severity: critical count, tones, pressure and the move summary', () => {
  const context = mriHarness({ rooms: { assessed: true, details: [] } });
  const elements = {};
  Object.assign(context, {
    $: id => (elements[id] ||= { textContent: '', innerHTML: '' }),
    esc: value => String(value ?? ''),
    num: value => String(value || 0),
    pct: (value, max) => Math.round((value / Math.max(1, max)) * 100),
    plural: (count, label) => `${count} ${label}`,
    capacityDiagnosis: () => ({ tone: 'stable', label: 'Seats cover demand', detail: '' }),
    capacityCourseDiagnosis: () => ({ tone: 'stable', label: '', detail: '' }),
    placementInstructor: () => '',
    placementLabel: p => `${p.course_code}-${p.section}`,
    crossBoardCounterparts: () => [],
  });
  for (const [begin, end] of [
    ['  function renderMetrics(', '  function capacityCourseDiagnosis('],
    ['  function renderLayers(', '  function renderHotspots('],
    ['  function placementEvidence(', '  function renderGeneInspector('],
    ['  function groupScans(', '  function expandedBoardIds('],
    ['  function selectedGraphGroup(', '  function buildPreviewTarget('],
    ['  function renderInterventions(', '  function buildRepairQueue('],
    ['  function currentConflictSummary(', '  function classifyMoveTargets('],
  ]) vm.runInContext(slice(mri, begin, end), context);
  const at = id => ({ id, board_id: 2, course_code: `AI${id}`, section: 'S1' });
  const overlap = (ids, severity) => ({ ids, sections: ids.map(id => `AI${id}-S1`), detail: 'SUN 09:00-10:15', severity, shared_students: 30 });
  const mixed = { id: 1, label: 'Mixed', placement_count: 2 }, warnOnly = { id: 2, label: 'Warn only', placement_count: 2 };
  context.state.boards = [mixed, warnOnly];
  context.state.conflicts.set(1, { overlaps: [overlap([10, 11], 'critical'), overlap([10, 12], 'warning')] });
  context.state.conflicts.set(2, { overlaps: [overlap([12, 13], 'warning')] });
  context.state.boardDetails.set(2, { placements: [at(12), at(13)] });
  const first = context.scanBoard(mixed), second = context.scanBoard(warnOnly);
  // 26 per critical overlap, 4 per non-blocking one (what a cross-board demand overlap weighs).
  assert.deepEqual([first.criticalOverlaps, first.warningOverlaps, first.pressure], [1, 1, 30]);
  assert.deepEqual([second.criticalOverlaps, second.warningOverlaps, second.pressure], [0, 1, 4]);
  const total = context.aggregateScan([first, second]);
  assert.deepEqual([total.overlaps, total.criticalOverlaps, total.warningOverlaps], [3, 1, 2]);
  assert.equal(context.pressureComponents(first).reduce((sum, row) => sum + row.score, 0), first.pressure);
  const metric = label => elements.twmMetrics.innerHTML.match(new RegExp(`<strong>(\\d+)</strong>\\s*<span>${label}</span>`))[1];
  context.renderMetrics(total);
  assert.equal(metric('Critical'), '1');
  context.renderMetrics(second);
  assert.equal(metric('Critical'), '0');
  context.renderLayers(second, [second]);
  assert.match(elements.twmLayers.innerHTML, /<div class="twm-layer watch">\s*<div>\s*<strong>Time layer<\/strong>/);
  const [hotspot] = context.collectHotspots([second]);
  assert.deepEqual([hotspot.tone, hotspot.kind], ['watch', 'Non-blocking time overlap']);
  assert.deepEqual(Array.from(context.collectHotspots([first]), row => row.tone), ['critical', 'watch']);
  assert.deepEqual(Array.from(context.placementEvidence(second, at(12)), row => [row.tone, row.title]), [['watch', 'Non-blocking time overlap']]);
  // Two sections only in a non-blocking overlap weigh 10 each, as a network hit does.
  const [group] = context.groupScans([second]);
  assert.deepEqual([group.localConflicts, group.pressure], [2, 20]);
  assert.deepEqual(Object.fromEntries(Array.from(context.buildSuggestions([second]), move => [move.id.split(':')[0], move.tone])), { reslot: 'watch', 'board-balance': 'watch' });
  context.renderInterventions([second]);
  assert.match(elements.twmInterventions.innerHTML, /<div class="twm-action watch">[\s\S]*Separate AI12-S1 \/ AI13-S1/);
  assert.deepEqual({ ...context.currentConflictSummary(at(12)) }, { critical: 0, warning: 1 });
});

test('graph cross-board detail labels course demand without mutating graph evidence or API property names', () => {
  const context = vm.createContext({});
  vm.runInContext(slice(graph, '  function demandEvidenceView(', '  function renderGraphDetail('), context);
  const input = { type: 'CLASHES_WITH', label: 'CLASHES_WITH', detail: 'cross_board_student_clash', shared_students: 9, meta: { Kind: 'cross_board_student_clash', 'Shared students': 9 } };
  const view = context.demandEvidenceView(input);
  assert.equal(view.shared_students, 9);
  assert.equal(view.type, 'CLASHES_WITH');
  assert.equal(view.label, 'Potential course-demand overlap');
  assert.match(view.detail, /Assignment to both sections has not been confirmed/);
  assert.equal(view.meta['Students with common course demand'], 9);
  assert.equal(input.meta['Shared students'], 9);
});

test('split aggregate cross-board KPI does not claim actual assigned student clashes', () => {
  const metric = { title: '' }, elements = {};
  for (const id of ['twsStStudents', 'twsStLinks', 'twsStPlaced', 'twsStCross']) elements[id] = { closest: () => metric };
  const context = vm.createContext({ S: { boards: [], scenarioSummary: { cross_board_affected_students: 12, cross_board_conflicts: 4, cross_board_student_conflict_incidences: 16 } }, $: id => elements[id] });
  vm.runInContext(slice(split, 'function updateAggregateMetrics()', '/* ── Slot bar'), context);
  context.updateAggregateMetrics();
  assert.equal(elements.twsStCross.textContent, '12 potential');
  assert.match(metric.title, /common course demand/);
  assert.match(metric.title, /Assignment to both sections is not confirmed/);
  assert.doesNotMatch(metric.title, /actual students affected/);
});

test('graph handoff links preserve the current scenario and an available selected board', () => {
  const elements = { tgWorkspaceLink: {}, tgMriLink: {} };
  const parentNodes = {
    section: { id: 'course', type: 'TTCourse' },
    course: { id: 'board:76', type: 'TTBoard' },
  };
  const context = vm.createContext({
    URLSearchParams,
    state: { selectedScenarioId: '42', graphData: { edges: [] } },
    $: id => elements[id],
    getPlanParent: id => parentNodes[id] || null,
  });
  vm.runInContext(slice(graph, '  function workspaceContextUrl(', '  function placementUrlFor('), context);
  context.updateWorkspaceContextLinks();
  assert.equal(elements.tgWorkspaceLink.href, '/timetable-workspace/?scenario=42');
  assert.equal(elements.tgMriLink.href, '/timetable-workspace/mri/?scenario=42');
  const boardId = context.graphBoardContext({ id: 'section', type: 'TTSection' });
  assert.equal(boardId, '76');
  assert.equal(context.workspaceContextUrl('/timetable-workspace/split/', boardId), '/timetable-workspace/split/?scenario=42&board=76');
  context.state.graphData.edges.push({ type: 'ON_BOARD', source: 'section', target: 'board:81' });
  assert.equal(context.graphBoardContext({ id: 'section', type: 'TTSection' }), '81');
  context.state.selectedScenarioId = '99';
  context.updateWorkspaceContextLinks();
  assert.equal(elements.tgWorkspaceLink.href, '/timetable-workspace/?scenario=99');
  context.state.selectedScenarioId = '';
  context.updateWorkspaceContextLinks();
  assert.equal(elements.tgMriLink.href, '/timetable-workspace/mri/');
});

test('split Arabic interface strings retain readable Unicode text', () => {
  assert.doesNotMatch(split, /[ØÙÃâ]/);
  assert.match(split, /IS_AR \? 'بحث في الجدول'/);
  assert.match(split, /IS_AR \? 'فشل تطبيق الإصلاح'/);
  assert.match(split, /IS_AR \? 'جودة الجدول'/);
});

function splitScenarioHarness(initial = '665', scenarios = {}) {
  const { JSDOM } = require('jsdom');
  const dom = new JSDOM('<select id="twsScenario"></select>');
  const calls = [], opened = [], warnings = [];
  const defaultScenario = { id: 662, name: 'Term 1 default', status: 'draft', academic_year: '1448', term: '1' };
  const context = vm.createContext({ DEFAULT_YEAR: 1448, DEFAULT_TERM: 1, INITIAL_SCENARIO: initial, IS_AR: false, URLSearchParams,
    window: { history: { state: null } },
    S: { scenarioId: null, scenarios: [] }, T: { selectScenario: 'Select scenario' }, document: dom.window.document,
    $: id => dom.window.document.getElementById(id), notify: { warning: message => warnings.push(message) },
    onScenarioChange: async () => opened.push(dom.window.document.getElementById('twsScenario').value),
    api: async url => {
      calls.push(url);
      if (url === '/ops/tw/scenarios/?year=1448&term=1') return { scenarios: [defaultScenario] };
      const id = url.match(/^\/ops\/tw\/scenarios\/(\d+)\/$/)?.[1];
      if (id) return scenarios[id] ? { scenario: scenarios[id] } : null;
      const scope = new URLSearchParams(url.split('?')[1]);
      return { scenarios: Object.values(scenarios).filter(row => row.academic_year === scope.get('year') && row.term === scope.get('term')) };
    },
  });
  vm.runInContext(slice(split, 'const COPY_ACTIVITY =', 'async function api('), context);
  vm.runInContext(slice(split, 'async function loadScenarios(', 'async function onScenarioChange('), context);
  return { context, calls, opened, warnings, dom };
}

test('explicit split scenario outside the default term resolves its actual scope instead of another scenario', async () => {
  const h = splitScenarioHarness('665', { 665: { id: 665, name: 'Fresh term 2', status: 'draft', academic_year: '1448', term: '2' } });
  await h.context.loadScenarios();
  assert.deepEqual(h.opened, ['665']);
  assert.ok(h.calls.includes('/ops/tw/scenarios/665/'));
  assert.ok(h.calls.includes('/ops/tw/scenarios/?year=1448&term=2'));
  assert.equal(h.context.S.scenarios.length, 1);
  assert.equal(h.context.S.scenarios[0].id, 665);
  h.dom.window.close();
});

test('a newly-created split scenario takes priority over current and initial scenarios', async () => {
  const h = splitScenarioHarness('665', { 666: { id: 666, name: 'New term 3', status: 'draft', academic_year: '1449', term: '3' } });
  h.context.S.scenarioId = 664;
  await h.context.loadScenarios('666');
  assert.deepEqual(h.opened, ['666']);
  assert.ok(h.calls.includes('/ops/tw/scenarios/?year=1449&term=3'));
  h.dom.window.close();
});

test('an unavailable explicit split scenario does not silently open the default scenario', async () => {
  const h = splitScenarioHarness('999');
  await h.context.loadScenarios();
  assert.deepEqual(h.opened, ['']);
  assert.equal(h.warnings.length, 1);
  h.dom.window.close();
});
