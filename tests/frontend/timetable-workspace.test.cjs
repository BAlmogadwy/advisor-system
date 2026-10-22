const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/page-timetable-workspace.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'core/templates/core/timetable_workspace.html'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const wait = async condition => { for (let i = 0; i < 80; i++) { if (condition()) return; await settle(); } assert.fail('UI did not settle'); };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const scenario = { id: 1, name: 'Real scenario copy', status: 'draft', slot_config: [{ label: '09:00', start: '09:00', end: '10:15' }] };
const placement = { id: 10, course_code: 'CS323', section: 'S1', term_section_id: 5, day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'LAB25', meetings: [], available_capacity: 24 };
const board = { id: 2, label: 'Term 7', nominal_term: 7, primary_count: 1 };
// What the Generate dialog hands runGenerate (it has its own year and term).
const GENERATE = { year: '1448', term: '1', programs: ['AI'], section: 'M' };
const quality = { ok: true, scenario_id: 1, instructor: { assessed: true, working_days: 18, idle_minutes: 780 }, soft_seats: { assessed: true, unresolved: 18 }, rooms: { assessed: true, shortages: 3, details: [{ course_code: 'CS323', placement_ids: [10], rooms: ['LAB25'], reasons: ['undersized'], required_capacity: 26 }] }, assigned_student_clashes: { assessed: true, count: 0 } };
async function harness(t, intercept = () => undefined, extra = {}, arabic = false) {
  const html = template.replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost/timetable-workspace/' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [];
  w.addEventListener('error', e => errors.push(e.error));
  // A const in an indirect eval stays local to that eval; the page reads a global.
  // base.html loads shared-utils (esc, pluralAr) on every page.
  w.eval(fs.readFileSync(path.join(root, 'static/js/shared-utils.js'), 'utf8'));
  w.LANGUAGE_CODE = arabic ? 'ar' : 'en';
  w._injectCsrf = x => x;
  w.esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  w.debounce = fn => fn;
  w.notify = Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => notices.push({ type, text })]));
  w.dlg = { confirm: async () => false };
  w.fetch = async (url, opts = {}) => {
    requests.push({ url, opts });
    let answer = await intercept(url, opts, w);
    if (answer === undefined) {
      if (url.startsWith('/ops/tw/scenarios/?')) answer = { scenarios: [scenario] };
      else if (url === '/ops/tw/scenarios/1/') answer = { scenario: { ...scenario, ...extra }, active_job: extra.active_job || null };
      else if (url.includes('/quality/')) answer = quality;
      else if (url.includes('/plan-lens/')) answer = { plans: [], courses: {}, sections: {} };
      else if (url.startsWith('/ops/tw/boards/?')) answer = { boards: [board] };
      else if (/\/boards\/\d+\/$/.test(url)) answer = { placements: [placement], slot_config: scenario.slot_config };
      else if (url.includes('/conflicts/')) answer = { summary: { critical: 0 }, cross_board_conflicts: [{ board_a_id: 2, board_b_id: 3, section_a: 'CS323', section_b: 'FE2', overlap_count: 14 }] };
      else if (url.includes('/capacity/')) answer = { courses: [], totals: {} };
      else if (url.includes('/budget/')) answer = { budget: [] };
      else if (url.includes('/unplaced/')) answer = { unplaced: [] };
      else assert.fail(`Unexpected request ${url}`);
    }
    return { ok: !answer.httpError, status: answer.httpError || 200, json: async () => answer };
  };
  d.getElementById('twYear').value = '1448'; d.getElementById('twTerm').value = '1';
  // The page loads the meeting policy and the shared slot-column module before its own script.
  w.eval(fs.readFileSync(path.join(root, 'static/js/timetable-meeting-policy.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(root, 'static/js/timetable-slot-config.js'), 'utf8'));
  w.eval(source);
  await wait(() => d.getElementById('twScenario').options.length === 2);
  const select = async () => { d.getElementById('twScenario').value = '1'; await w.onScenarioChange(); };
  return { w, d, select, requests, notices, errors };
}

test('saved scenario shows evaluated scope and genuine buffered room shortages in Issues and MRI', async t => {
  const h = await harness(t); await h.select();
  assert.equal(h.d.getElementById('twQualityInstructorDays').textContent, '18');
  assert.equal(h.d.getElementById('twQualityInstructorIdle').textContent, '780');
  assert.equal(h.d.getElementById('twQualityRooms').textContent, '3');
  // In words, never the raw reason code.
  assert.match(h.d.getElementById('twRightBody').textContent, /Room too small.*CS323 · LAB25.*needs 26 seats/);
  assert.doesNotMatch(h.d.getElementById('twRightBody').textContent, /undersized/);
  h.w.renderRightPanel('mri');
  assert.match(h.d.getElementById('twRightBody').textContent, /1 meetings lack a suitable room/);
  assert.doesNotMatch(h.d.getElementById('twRightBody').textContent, /verified shared-student/);
  assert.equal(h.d.getElementById('twNewBoard').classList.contains('d-none'), false);
  assert.equal(h.d.getElementById('twSlotEdit').classList.contains('d-none'), false);
  assert.ok([...h.d.querySelectorAll('.tw-section-item:not([data-tw-draggable])')].every(el => !el.draggable));
  assert.deepEqual(h.errors, []);
});

test('Generate takes ownership before POST; incompatible actions and duplicate engine clicks are guarded', async t => {
  const pending = deferred();
  const h = await harness(t, (url) => url === '/ops/tw/generate-workspace/' ? pending.promise : undefined);
  await h.select();
  const generation = h.w.runGenerate('classic', GENERATE);
  for (const id of ['twGenerate', 'twGenerateRun', 'twEmptyGenerate', 'twNewScenario', 'twOptimise', 'twOptimiseMenu', 'twPublish', 'twScenario', 'twSlotEdit']) assert.equal(h.d.getElementById(id).disabled, true, id);
  assert.equal(h.d.getElementById('twExport').disabled, false);
  h.w.TW.selectedPlacementId = 10; h.w.renderRightPanel('selection');
  const mutations = [...h.d.querySelectorAll('#twRightBody [data-tw-mutation]')];
  assert.ok(mutations.length && mutations.every(el => el.disabled));
  assert.equal(h.d.querySelector('.tw-card').draggable, false);
  await h.w.runOptimiseV2('current'); await h.w.runGenerate('scheduler'); await h.w.toggleLock(10);
  assert.equal(h.requests.filter(r => r.opts.method === 'POST').length, 1);
  pending.resolve({ httpError: 500, error: { message: 'Build submission failed' } }); await generation;
  for (const id of ['twGenerate', 'twGenerateRun', 'twOptimise', 'twPublish', 'twScenario']) assert.equal(h.d.getElementById(id).disabled, false, id);
  assert.equal(h.d.querySelector('.tw-card').draggable, true);
});

test('unexpected Generate render failure releases all controls', async t => {
  const h = await harness(t, url => url === '/ops/tw/generate-workspace/' ? { ok: true } : undefined);
  await h.select();
  await h.w.runGenerate('classic', GENERATE);
  assert.equal(h.d.getElementById('twGenerate').disabled, false);
  assert.equal(h.d.getElementById('twScenario').disabled, false);
});

test('completed deferred Generate refreshes its captured scenario and keeps saved scenario choices', async t => {
  const h = await harness(t, url => url === '/ops/tw/generate-workspace/' ? { scenario, boards: [board], build_job_id: 'build1' }
    : url === '/planner-jobs/build1/' ? { job_id: 'build1', status: 'succeeded' }
    : url === '/planner-jobs/build1/result/' ? { result: {} } : undefined);
  await h.select();
  await h.w.runGenerate('classic', GENERATE);
  assert.equal(h.d.getElementById('twGenerate').disabled, false);
  assert.equal(h.d.getElementById('twOptimise').disabled, false);
  assert.equal(h.w.TW.boardId, 2);
  assert.equal(h.w.TW.plannerJob.status, 'succeeded');
  assert.ok(h.d.getElementById('twScenario').options.length >= 2);
  assert.ok(h.notices.some(n => n.text === 'Generated + built'));
});

test('Optimise submission failure restores controls and retains published protection', async t => {
  const h = await harness(t, url => url.includes('/optimise-v2/') ? { httpError: 500, error: { message: 'Planner unavailable' } } : undefined);
  await h.select(); await h.w.runOptimiseV2('current');
  assert.equal(h.d.getElementById('twOptimise').disabled, false);
  assert.equal(h.d.getElementById('twPublish').disabled, false);
  assert.equal(h.d.getElementById('twScenario').disabled, false);
});

test('reload discovers an active job; confirmed external completion refreshes and restores edits', async t => {
  const h = await harness(t, undefined, { active_job: { job_id: 'job1', status: 'running', scenario_id: 1 } });
  await h.select(); assert.equal(h.d.getElementById('twOptimise').disabled, true);
  // Authoritative detail after completion has no active job.
  h.w.fetch = async url => ({ ok: true, json: async () => url.endsWith('/1/') ? { scenario, active_job: null } : url.includes('/quality/') ? quality : url.includes('/plan-lens/') ? {} : url.includes('boards/?') ? { boards: [board] } : /boards\/2\/$/.test(url) ? { placements: [placement], slot_config: scenario.slot_config } : {} });
  h.w.dispatchEvent(new h.w.CustomEvent('tw:planner-job', { detail: { scenarioId: 1, job: { job_id: 'job1', status: 'succeeded' }, source: 'adapter' } }));
  await wait(() => !h.d.getElementById('twOptimise').disabled);
  assert.equal(h.d.querySelector('.tw-card').draggable, true);
  h.w.dispatchEvent(new h.w.CustomEvent('tw:planner-job', { detail: { scenarioId: 1, job: { job_id: 'job1', status: 'running' }, source: 'adapter' } }));
  assert.equal(h.d.getElementById('twOptimise').disabled, false, 'late active poll cannot relock a terminal job');
});

test('unconfirmed polling keeps ownership protection without inventing a failed run or cancelling it', async t => {
  const h = await harness(t, url => url.startsWith('/planner-jobs/') ? { httpError: 503 } : undefined);
  await h.select();
  const job = await h.w.pollPlannerJob('lost', null, { maxConsecutiveErrors: 1 });
  assert.equal(job.status, 'unknown');
  assert.equal(h.d.getElementById('twPublish').disabled, true);
  assert.equal(h.requests.some(r => r.url.includes('/cancel/')), false);
});

test('published scenario stays read only while all read views and exports remain accessible', async t => {
  const h = await harness(t, undefined, { status: 'published' }); await h.select();
  for (const id of ['twOptimise', 'twOptimiseMenu', 'twPublish', 'twNewBoard']) assert.equal(h.d.getElementById(id).disabled, true, id);
  assert.equal(h.d.getElementById('twGenerate').disabled, false);
  assert.equal(h.d.getElementById('twExport').disabled, false);
  await h.w.toggleLock(10); await h.w.runOptimiseV2('current');
  assert.equal(h.requests.some(r => r.opts.method === 'POST'), false);
});

test('failed Undo and Redo retain original history and never create an inverse action', async t => {
  const h = await harness(t, (url, opts) => opts.method === 'POST' ? { httpError: 409, error: { message: 'Scenario busy' } } : undefined);
  await h.select();
  h.w.TW.undoStack.push({ type: 'move', placement_id: 10, old_day: 'SUN', old_start: '09:00', old_end: '10:15' });
  h.w.updateUndoRedoButtons(); h.d.getElementById('twUndo').click(); await wait(() => h.notices.length);
  assert.equal(h.w.TW.undoStack.length, 1); assert.equal(h.w.TW.redoStack.length, 0);
  h.w.TW.redoStack.push({ type: 'remove', placement_id: 10 });
  h.w.updateUndoRedoButtons(); h.d.getElementById('twRedo').click(); await wait(() => h.notices.length === 2);
  assert.equal(h.w.TW.redoStack.length, 1); assert.equal(h.w.TW.undoStack.length, 1);
});

test('older board responses cannot replace the currently selected board', async t => {
  const old = deferred(); let delay = false;
  const h = await harness(t, url => delay && url === '/ops/tw/boards/2/' ? old.promise : undefined);
  await h.select(); delay = true;
  const a = h.w.selectBoard(2); await h.w.selectBoard(3);
  old.resolve({ placements: [{ ...placement, id: 99 }], slot_config: scenario.slot_config }); await a;
  assert.equal(h.w.TW.boardId, 3); assert.equal(h.w.TW.placements[0].id, 10);
});

for (const type of ['move', 'create', 'remove']) {
  test(`successful ${type} Undo/Redo/Undo retains times, metadata, and newly created row IDs`, async t => {
    let id = 100;
    const writes = [];
    const h = await harness(t, (url, opts) => {
      if (opts.method === 'POST') {
        writes.push({ url, body: JSON.parse(opts.body) });
        return { ok: true, placement: url.endsWith('/create/') ? { id: ++id } : undefined };
      }
    });
    await h.select();
    h.w.TW.undoStack.push({ type, placement_id: 10, board_id: 2, term_section_id: 5,
      day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'LAB25',
      old_day: 'SUN', old_start: '09:00', old_end: '10:15', new_day: 'MON', new_start: '13:00', new_end: '14:15' });
    await h.w.runHistoryAction(true); await h.w.runHistoryAction(false); await h.w.runHistoryAction(true);
    assert.equal(writes.length, 3);
    if (type === 'move') assert.deepEqual(writes.map(r => r.body.day), ['SUN', 'MON', 'SUN']);
    else {
      const created = writes.filter(r => r.url.endsWith('/create/'));
      assert.ok(created.every(r => r.body.board_id === 2 && r.body.term_section_id === 5 && r.body.room === 'LAB25'));
      assert.match(writes[type === 'create' ? 2 : 1].url, /101\/remove\/$/);
    }
    assert.equal(h.w.TW.undoStack.length, 0); assert.equal(h.w.TW.redoStack.length, 1);
  });
}

test('manual board exposes eligible existing sections and keeps their drag placement path', async t => {
  const h = await harness(t, url => url.includes('/unplaced/') ? { unplaced: [{ term_section_id: 7, course_code: 'AI113', section: 'M', course_name: 'AI basics', meetings: [] }] } : undefined);
  await h.select();
  const card = h.d.querySelector('#twSectionList .tw-section-item');
  assert.match(card.textContent, /AI113/); assert.equal(card.draggable, true);
});

test('manual sections beyond the initial page remain reachable and searchable', async t => {
  const sections = Array.from({ length: 125 }, (_, i) => ({ term_section_id: i + 100,
    course_code: `AI${i + 100}`, section: 'M', course_name: 'AI basics', meetings: [] }));
  const h = await harness(t, url => url.includes('/unplaced/') ? { unplaced: sections } : undefined);
  await h.select();
  assert.equal(h.d.querySelectorAll('#twSectionList .tw-section-item').length, 100);
  const more = [...h.d.querySelectorAll('#twSectionList button')].find(el => /Show more/.test(el.textContent));
  assert.ok(more); more.click();
  assert.equal(h.d.querySelectorAll('#twSectionList .tw-section-item').length, 125);
  h.d.getElementById('twSectionSearch').value = 'AI224';
  h.d.getElementById('twSectionSearch').dispatchEvent(new h.w.Event('input'));
  assert.equal(h.d.querySelectorAll('#twSectionList .tw-section-item').length, 1);
  assert.match(h.d.getElementById('twSectionList').textContent, /AI224/);
});

test('MRI, Graph and Split return URLs restore the selected saved scenario and board', async t => {
  const h = await harness(t, url => url.startsWith('/ops/tw/boards/?') ? { boards: [board, { ...board, id: 3, label: 'Second board' }] } : undefined);
  h.w.history.replaceState({}, '', '?scenario=1&board=3');
  await h.w.restoreWorkspaceContext();
  assert.equal(h.w.TW.scenarioId, 1); assert.equal(h.w.TW.boardId, 3);
  assert.equal(new URL(h.w.location.href).searchParams.get('board'), '3');
  assert.equal(h.d.getElementById('twScenario').value, '1');
  assert.equal(h.d.getElementById('twGraphPage').getAttribute('href'), '/timetable-workspace/graph/?scenario=1');
});

const LOCK_HELP = {
  en: "Keeps this section's days and times; its room can still change.",
  ar: 'يثبّت أيام وأوقات هذه الشعبة؛ ويمكن تغيير القاعة.',
};
for (const arabic of [false, true]) test(`the inspector's Lock button says a lock keeps days and times, not the room (${arabic ? 'ar' : 'en'})`, async t => {
  const h = await harness(t, undefined, {}, arabic); await h.select();
  const help = LOCK_HELP[arabic ? 'ar' : 'en'];
  h.w.TW.selectedPlacementId = 10; h.w.renderRightPanel('selection');
  const inspectorButtons = [...h.d.querySelectorAll('#twRightBody button')];
  const inspectorLock = inspectorButtons.find(el => /toggleLock\(10\)/.test(el.getAttribute('onclick') || ''));
  assert.ok(inspectorLock);
  assert.equal(inspectorLock.textContent, arabic ? 'قفل الوقت' : 'Lock time');
  assert.equal(inspectorLock.getAttribute('aria-pressed'), 'false');
  assert.equal(inspectorLock.getAttribute('title'), help);
  assert.equal(inspectorLock.hasAttribute('dir'), false);
  const inspectorRemove = inspectorButtons.find(el => /removePlacement\(10\)/.test(el.getAttribute('onclick') || ''));
  assert.ok(inspectorRemove);
  assert.equal(inspectorRemove.hasAttribute('title'), false);
  // The sentence is also spelled out under the buttons, once, in the page's language.
  assert.equal(h.d.querySelector('#twRightBody .tw-sel-help').textContent, help);
  assert.equal(h.d.getElementById('twDrawer'), null);
  assert.equal(h.d.body.innerHTML.includes(LOCK_HELP[arabic ? 'en' : 'ar']), false);
  assert.deepEqual(h.errors, []);
});

// H15 severity is decided once on the server: a same-course overlap or one
// that leaves students with no clash-free pair is critical; one they can route
// round through sibling sections is a warning, and must not read as red here.
// The Issues tab groups by what each does (blocks publishing / lowers quality),
// and each row names its kind.
const OVERLAP_LABELS = {
  en: { critical: 'Blocks publishing', warning: 'Lowers quality', criticalKind: 'Time clash', warningKind: 'Overlap students can route round' },
  ar: { critical: 'يمنع النشر', warning: 'يُضعف الجودة', criticalKind: 'تعارض زمني', warningKind: 'تداخل يمكن تجاوزه' },
};
for (const arabic of [false, true]) test(`a non-blocking overlap stays a warning on the board button, cards, Issues and MRI (${arabic ? 'ar' : 'en'})`, async t => {
  const at = (id, day) => ({ ...placement, id, term_section_id: id + 100, course_code: `AI${id}`, day, room: '' });
  const overlap = (ids, severity) => ({ ids, sections: ids.map(id => `AI${id}-S1`), detail: `${ids[0]} vs ${ids[1]}`,
    severity, same_course: false, shared_students: 30, blocked_students: severity === 'critical' ? 30 : 0 });
  const conflicts = { summary: { critical: 1, warning: 1 }, overlaps: [overlap([10, 11], 'critical'), overlap([12, 13], 'warning')],
    instructor_clashes: [], room_clashes: [], cross_board_conflicts: [] };
  const h = await harness(t, url => url === '/ops/tw/boards/2/' ? { placements: [at(10, 'SUN'), at(11, 'SUN'), at(12, 'MON'), at(13, 'MON')], slot_config: scenario.slot_config }
    : url.includes('/conflicts/') ? conflicts : undefined, {}, arabic);
  await h.select();
  const label = OVERLAP_LABELS[arabic ? 'ar' : 'en'];
  // The board's button counts only what blocks (the critical overlap), with the octagon.
  const flag = h.d.querySelector('#twBoardTabs .ux-seg-item[aria-current="true"] .ux-seg-flag');
  assert.deepEqual([flag.dataset.tone, flag.textContent], ['critical', '1']);
  const card = id => h.d.querySelector(`.tw-card[data-placement-id="${id}"]`);
  assert.ok(card(10).classList.contains('critical'));
  assert.ok(card(12).classList.contains('warning'));
  assert.equal(card(12).classList.contains('critical'), false);
  assert.ok(card(12).title.startsWith(`${label.warning}: ${label.warningKind}`), card(12).title);
  h.w.renderRightPanel('issues');
  const issue = text => {
    const head = [...h.d.querySelectorAll('#twRightBody .tw-issue-head')].find(node => node.children[1]?.textContent === text);
    assert.ok(head, text);
    const row = head.nextElementSibling;
    return [head.dataset.tone, head.querySelector('.tw-issue-count').textContent, row.dataset.tone, row.querySelector('.tw-issue-kind').textContent, row.querySelector('.tw-issue-subject').textContent];
  };
  assert.deepEqual(issue(label.critical), ['critical', '1', 'critical', label.criticalKind, 'AI10-S1 / AI11-S1']);
  // Lowers quality: the overlap, and the quality fixture's room too small for AI10.
  assert.deepEqual(issue(label.warning), ['warning', '2', 'warning', label.warningKind, 'AI12-S1 / AI13-S1']);
  // With only the warning left, the MRI panel counts no critical and paints it amber.
  h.w.TW.conflicts = { ...conflicts, summary: { critical: 0, warning: 1 }, overlaps: [conflicts.overlaps[1]] };
  h.w.renderRightPanel('mri');
  const mri = h.d.getElementById('twRightBody');
  // Warnings: the overlap and the quality fixture's one meeting without a suitable room.
  const [criticalCount, warningCount] = [...mri.querySelectorAll('.tw-mri-metrics > div > span')].map(node => node.textContent);
  assert.deepEqual([criticalCount, warningCount], ['0', '2']);
  assert.ok(mri.querySelector('[data-mri-layer="time-layer"]').classList.contains('watch'));
  const hotspot = [...mri.querySelectorAll('.tw-mri-hotspot')].find(node => node.querySelector('strong').textContent === 'AI12-S1 / AI13-S1');
  assert.deepEqual([hotspot.classList.contains('watch'), hotspot.querySelector('span').textContent], [true, 'Same-board overlap (non-blocking)']);
  // Pressure: 4 for the non-blocking overlap (a critical one weighs 28) + 12 for that room.
  assert.equal(mri.querySelector('.tw-mri-score span').textContent, '16');
  assert.deepEqual(h.errors, []);
});

// The server's publish blockers on a board are shown where the user looks for
// problems: listed first in Issues, counted on its badge and on the board button.
test('the Issues tab lists the board’s publish blockers first and counts them', async t => {
  const readiness = { ready: false, blockers: ['x'], warnings: [], blocker_items: [
    { code: 'missing_required_section', board_id: null, board_label: null, count: 1,
      params: { course_code: 'AI222', course_key: 'AI222', programme_term: 7, planned_sections: 2, used_sections: 1 } },
  ], warning_items: [] };
  const h = await harness(t, url => url.includes('/readiness/') ? { readiness } : undefined);
  await h.select();
  // No hand-drawn panel: the readiness answer itself must redraw the open Issues tab.
  await h.w.refreshPublishReadiness(true);
  const head = h.d.querySelector('#twRightBody .tw-issue-head');
  assert.equal(head.dataset.tone, 'critical');
  assert.equal(head.children[1].textContent, 'Blocks publishing');
  assert.match(head.nextElementSibling.textContent, /AI222: 1 of 2 sections not placed \(term 7\)/);
  assert.equal(h.d.getElementById('twIssuesCount').dataset.tone, 'critical');
  const flag = h.d.querySelector('#twBoardTabs .ux-seg-item[aria-current="true"] .ux-seg-flag');
  assert.equal(flag.textContent, '1');
  assert.match(h.d.getElementById('twPublishLabel').textContent, /Publish · 1 blocker/);
  // The section is placed: the next answer clears the group from the open tab.
  readiness.ready = true; readiness.blockers = []; readiness.blocker_items = [];
  h.w.TW.dataVersion++;
  await h.w.refreshPublishReadiness(true);
  assert.doesNotMatch(h.d.getElementById('twRightBody').textContent, /Blocks publishing/);
  assert.equal(h.d.querySelector('#twBoardTabs .ux-seg-item[aria-current="true"] .ux-seg-flag'), null);
});

test('an older readiness answer never replaces a newer one', async t => {
  const first = deferred(), second = deferred();
  let calls = 0;
  const h = await harness(t, url => url.includes('/readiness/') ? (++calls === 1 ? first.promise : second.promise) : undefined);
  await h.select();
  const older = h.w.refreshPublishReadiness(true);
  h.w.TW.dataVersion++;
  const newer = h.w.refreshPublishReadiness(true);
  second.resolve({ readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } });
  await newer;
  first.resolve({ readiness: { ready: false, blockers: ['stale'], warnings: [], blocker_items: [{ code: 'no_boards' }], warning_items: [] } });
  await older;
  assert.equal(h.w.TW.readinessVersion, h.w.TW.dataVersion);
  assert.equal(h.w.TW.readiness.ready, true);
  assert.ok(h.d.getElementById('twPublish').classList.contains('btn-primary'));
});

test('re-rendering the board buttons keeps the keyboard on the same board', async t => {
  const h = await harness(t, url => url.startsWith('/ops/tw/boards/?') ? { boards: [board, { ...board, id: 3, label: 'Second board' }] } : undefined);
  await h.select();
  h.d.querySelector('#twBoardTabs [data-board-id="3"]').focus();
  h.w.renderBoardTabs();
  assert.equal(h.d.activeElement.dataset.boardId, '3');
  assert.ok(h.d.getElementById('twBoardTabs').contains(h.d.activeElement));
});

test('a newly generated scenario never shows the previous scenario’s publish blockers', async t => {
  const readiness = { ready: false, blockers: ['x'], warnings: [], blocker_items: [{ code: 'no_boards' }], warning_items: [] };
  const h = await harness(t, url => url.includes('/readiness/') ? { readiness }
    : url === '/ops/tw/generate-workspace/' ? { scenario: { ...scenario, id: 9, name: 'Generated' }, boards: [board], build_job_id: null } : undefined);
  await h.select();
  await h.w.refreshPublishReadiness(true);
  assert.match(h.d.getElementById('twPublishLabel').textContent, /1 blocker/);
  const generated = h.w.runGenerate('classic', GENERATE);
  // As soon as the new scenario is open, the old answer is gone.
  await wait(() => h.w.TW.scenarioId === 9);
  assert.equal(h.w.TW.readiness, null);
  assert.doesNotMatch(h.d.getElementById('twPublishLabel').textContent, /blocker/);
  await generated;
});

// A card click opens Selection; the Issues badge and the collapsed inspector's
// rail must still follow the board, the conflicts and each readiness answer.
test('the Issues count follows the data while another inspector tab is open', async t => {
  const second = { ...board, id: 3, label: 'Term 8', nominal_term: 8 };
  const readiness = { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] };
  const h = await harness(t, url => {
    if (url.includes('/readiness/')) return { readiness };
    if (url.startsWith('/ops/tw/boards/?')) return { boards: [board, second] };
    if (url === '/ops/tw/boards/3/') return { placements: [{ ...placement, id: 11 }], slot_config: scenario.slot_config };
    if (url === '/ops/tw/boards/3/conflicts/') return { summary: { critical: 0 } };
    return undefined;
  });
  await h.select();
  const counts = () => [h.d.getElementById('twIssuesCount').textContent, h.d.getElementById('twRightRailCount').textContent];
  // Board 2: its undersized room (its cross-board pair is context, never counted).
  assert.deepEqual(counts(), ['1', '1']);
  h.d.getElementById('twTabSelection').click();
  assert.ok(h.d.getElementById('twTabSelection').classList.contains('active'));
  await h.w.selectBoard(3);
  assert.deepEqual(counts(), ['', ''], 'board 3 has nothing wrong');
  // A publish blocker lands on board 3 (a section of its term placed nowhere).
  readiness.ready = false; readiness.blockers = ['x'];
  readiness.blocker_items = [{ code: 'missing_required_section', board_id: null, count: 1,
    params: { course_code: 'AI222', course_key: 'AI222', programme_term: 8, planned_sections: 2, used_sections: 1 } }];
  h.w.TW.dataVersion++;
  await h.w.refreshPublishReadiness(true);
  assert.deepEqual(counts(), ['1', '1']);
  assert.equal(h.d.getElementById('twIssuesCount').dataset.tone, 'critical');
  await h.w.selectBoard(2);
  assert.deepEqual(counts(), ['1', '1']);
  assert.equal(h.d.getElementById('twIssuesCount').dataset.tone, 'warning');
  assert.ok(h.d.getElementById('twTabSelection').classList.contains('active'), 'Selection stayed open throughout');
  assert.deepEqual(h.errors, []);
});

// Between a board switch and that board's own conflicts, the previous board's
// clashes are neither shown nor counted, and the tab never says "No issues".
test('a board switch never counts the previous board’s clashes while its own load', async t => {
  const second = { ...board, id: 3, label: 'Term 8', nominal_term: 8 };
  const pending = deferred();
  const h = await harness(t, url => {
    if (url.includes('/readiness/')) return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    if (url.startsWith('/ops/tw/boards/?')) return { boards: [board, second] };
    if (url === '/ops/tw/boards/2/conflicts/') return { summary: { critical: 1 }, cross_board_conflicts: [{ board_a_id: 2, board_b_id: 3, section_a: 'CS323', section_b: 'FE2', overlap_count: 14 }] };
    if (url === '/ops/tw/boards/3/') return { placements: [{ ...placement, id: 11 }], slot_config: scenario.slot_config };
    if (url === '/ops/tw/boards/3/conflicts/') return pending.promise;
    return undefined;
  });
  await h.select();
  const counts = () => [h.d.getElementById('twIssuesCount').textContent, h.d.getElementById('twRightRailCount').textContent];
  // Board 2's undersized room (its cross-board pair is context, listed but never counted).
  assert.deepEqual(counts(), ['1', '1']);
  const switching = h.w.selectBoard(3);
  await wait(() => h.w.TW.placements[0]?.id === 11);
  // Something redraws inside the window: a readiness answer lands.
  h.w.renderReadinessViews();
  assert.deepEqual(counts(), ['', ''], 'board 2’s clash is not board 3’s');
  const body = h.d.getElementById('twRightBody').textContent;
  assert.match(body, /This board’s clashes are not loaded/);
  assert.doesNotMatch(body, /No issues/);
  assert.equal(h.d.querySelector('#twBoardTabs [data-board-id="3"] .ux-seg-flag'), null, 'nor board 2’s critical count on its button');
  pending.resolve({ summary: { critical: 0 } });
  await switching;
  assert.match(h.d.getElementById('twRightBody').textContent, /No issues/);
  assert.doesNotMatch(h.d.getElementById('twRightBody').textContent, /not loaded/);
  assert.deepEqual(h.errors, []);
});

test('a board whose conflicts fail to load never reads as having no issues', async t => {
  const second = { ...board, id: 3, label: 'Term 8', nominal_term: 8 };
  const h = await harness(t, url => {
    if (url.startsWith('/ops/tw/boards/?')) return { boards: [board, second] };
    if (url === '/ops/tw/boards/3/') return { placements: [{ ...placement, id: 11 }], slot_config: scenario.slot_config };
    if (url === '/ops/tw/boards/3/conflicts/') return { httpError: 500, error: { message: 'Conflicts failed' } };
    return undefined;
  });
  await h.select();
  await h.w.selectBoard(3);
  const body = h.d.getElementById('twRightBody').textContent;
  assert.match(body, /This board’s clashes are not loaded/);
  assert.doesNotMatch(body, /No issues/);
  assert.ok(h.notices.some(n => n.type === 'error'), 'the failure itself is reported');
});

// The server can refuse a Publish the last check allowed (the data changed in
// between): its blockers then show everywhere the earlier answer did.
test('a refused Publish redraws the board flags, the count and the open Issues tab', async t => {
  const h = await harness(t, (url, opts) => {
    if (url.includes('/readiness/')) return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    if (url.endsWith('/publish/') && opts.method === 'POST') return { httpError: 409, error: { code: 'PUBLISH_BLOCKED', message: 'Blocked', details: {
      blockers: ['x'], warnings: [], warning_items: [], blocker_items: [{ code: 'missing_required_section', board_id: null, count: 1,
        params: { course_code: 'AI222', course_key: 'AI222', programme_term: 7, planned_sections: 2, used_sections: 1 } }] } } };
    return undefined;
  });
  await h.select();
  h.w.dlg = { confirm: async () => true };
  // Before: one thing lowers quality (the undersized room).
  assert.deepEqual([h.d.getElementById('twIssuesCount').textContent, h.d.getElementById('twIssuesCount').dataset.tone], ['1', 'warning']);
  await h.w.publishScenario();
  // After: the count says what blocks publishing — the refused section.
  assert.deepEqual([h.d.getElementById('twIssuesCount').textContent, h.d.getElementById('twIssuesCount').dataset.tone], ['1', 'critical']);
  assert.equal(h.d.querySelector('#twBoardTabs [aria-current="true"] .ux-seg-flag').textContent, '1');
  assert.match(h.d.getElementById('twRightBody').textContent, /Blocks publishing/);
  assert.match(h.d.getElementById('twPublishLabel').textContent, /1 blocker/);
});

// After an edit the board's conflicts are reloaded; if that fails, the earlier
// clashes are not shown as current (the edit may have changed them).
test('a failed conflicts refresh on the same board shows them as not loaded, not as current', async t => {
  let fail = false;
  const h = await harness(t, url => {
    if (url.includes('/readiness/')) return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    if (fail && url === '/ops/tw/boards/2/conflicts/') return { httpError: 500, error: { message: 'Conflicts failed' } };
    return undefined;
  });
  await h.select();
  assert.equal(h.d.getElementById('twIssuesCount').textContent, '1', 'its undersized room');
  assert.match(h.d.getElementById('twRightBody').textContent, /CS323 ↔ FE2/, 'the cross-board pair is listed while known');
  fail = true;
  await h.w.selectBoard(2);
  const body = h.d.getElementById('twRightBody').textContent;
  assert.match(body, /This board’s clashes are not loaded/);
  assert.doesNotMatch(body, /CS323 ↔ FE2/, 'the earlier clash is not listed as current');
  assert.equal(h.d.getElementById('twIssuesCount').textContent, '1', 'the room, from the quality check, still counts');
});

// A scenario with no boards: its blockers ("no boards") are the scenario's, on
// Publish; the Issues tab has no board to speak for and says so.
test('a scenario with no boards keeps its blockers on Publish, not in a board’s Issues', async t => {
  const h = await harness(t, url => {
    if (url.includes('/readiness/')) return { readiness: { ready: false, blockers: ['x'], warnings: [], warning_items: [],
      blocker_items: [{ code: 'no_boards', board_id: null, board_label: null, count: 0, params: {} }] } };
    if (url.startsWith('/ops/tw/boards/?')) return { boards: [] };
    return undefined;
  });
  await h.select();
  assert.equal(h.w.TW.boardId, null);
  await h.w.refreshPublishReadiness(true);
  const body = h.d.getElementById('twRightBody').textContent;
  assert.match(body, /Open a board to see its issues/);
  assert.doesNotMatch(body, /not loaded|Blocks publishing/);
  assert.equal(h.d.getElementById('twIssuesCount').textContent, '');
  assert.match(h.d.getElementById('twPublishLabel').textContent, /1 blocker/);
  assert.deepEqual(h.errors, []);
});

/* ── Slot columns: the page draws and edits the server's effective columns ── */
const ONLINE_LABS = [
  { label: 'Lab 1', start: '09:00', end: '10:40' },
  { label: 'Online 1', start: '15:50', end: '17:30', online_only: true },
];
const columns = { slot_config: [], lab_slot_config: [], effective_slot_config: scenario.slot_config, effective_lab_slot_config: ONLINE_LABS };

test('the grid draws the server’s effective columns and never writes defaults into the scenario', async t => {
  const online = { ...placement, id: 12, term_section_id: 6, course_code: 'GS101', start_time: '15:50', end_time: '17:30', room: '' };
  const h = await harness(t, url => url === '/ops/tw/boards/2/' ? { placements: [placement, online], ...columns } : undefined, columns);
  await h.select();
  const labTimes = [...h.d.querySelectorAll('.tw-lab-grid .tw-slot-time')].map(n => n.textContent);
  assert.deepEqual(labTimes, ['09:00–10:40', '15:50–17:30']);
  assert.ok(h.d.querySelector('.tw-lab-grid .tw-card[data-placement-id="12"]'), 'the online meeting is drawn in its window');
  assert.deepEqual([h.w.TW.scenario.slot_config, h.w.TW.scenario.lab_slot_config], [[], []], 'the stored lists are untouched');
  assert.deepEqual(h.errors, []);
});

test('a board answer without columns keeps the scenario’s effective columns', async t => {
  // The default board answer carries only `slot_config`.
  const h = await harness(t, undefined, columns);
  await h.select();
  assert.deepEqual(h.w.TW.scenario.effective_lab_slot_config.map(slot => slot.start), ['09:00', '15:50']);
  assert.equal(h.d.querySelectorAll('.tw-lab-grid .tw-slot-time').length, 2);
});

function slotHarness(t, answer) {
  const posts = [];
  return harness(t, (url, opts) => {
    if (url === '/ops/tw/boards/2/') return { placements: [placement], ...columns };
    if (!url.endsWith('/slots/update/')) return undefined;
    const body = JSON.parse(opts.body);
    posts.push(body);
    return answer(body);
  }, columns).then(h => Object.assign(h, { posts }));
}
const dialogs = (h, ...answers) => {
  const shown = [];
  h.w.dlg = { confirm: async ({ title, body }) => {
    shown.push({ title, body });
    // The real dialog puts its body in the page (one at a time): the editor
    // reads its textareas. An answer may first type into it.
    h.d.querySelectorAll('.stub-dialog').forEach(node => node.remove());
    h.d.body.insertAdjacentHTML('beforeend', `<div class="stub-dialog">${body}</div>`);
    const answer = answers.shift();
    return typeof answer === 'function' ? answer(h.d) : answer;
  } };
  return shown;
};
const HIDDEN = { hidden: [{ placement_id: 10, board_id: 2, board_label: 'Term 7', course_code: 'CS323', section: 'S1', day: 'SUN', start_time: '09:00', end_time: '10:15', is_locked: true }], hidden_count: 1, fingerprint: 'abc123' };

test('a slot save that would hide meetings names them, and repeats only with that confirmation', async t => {
  const h = await slotHarness(t, body => body.confirm === 'abc123'
    ? { ok: true, scenario: { ...scenario, ...columns, slot_config: body.slot_config }, hidden_count: 1 }
    : { httpError: 409, error: { code: 'SLOT_EDIT_NEEDS_CONFIRMATION', message: 'x', details: HIDDEN } });
  await h.select();
  const shown = dialogs(h, true, true);
  await h.w.openSlotEditor();
  // The editor lists the effective columns, the online window included, and says it stays online-only.
  assert.match(shown[0].body, /Online 1\t15:50\t17:30/);
  assert.match(shown[0].body, /The online-only columns \(15:50\) stay online-only/);
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[0].confirm, undefined);
  assert.deepEqual(h.posts[0].lab_slot_config.map(row => row.start), ['09:00', '15:50']);
  assert.match(shown[1].body, /leaves 1 meeting outside the grid/);
  assert.match(shown[1].body, /Term 7<\/bdi> · <bdi>CS323 S1<\/bdi> · Sun <bdi dir="ltr">09:00–10:15<\/bdi> · time locked/);
  assert.equal(h.posts[1].confirm, 'abc123');
  assert.ok(h.notices.some(n => n.type === 'success'));
  assert.deepEqual(h.errors, []);
});

test('a declined confirmation reopens the editor with the typed text, and sends nothing more', async t => {
  const h = await slotHarness(t, () => ({ httpError: 409, error: { code: 'SLOT_EDIT_NEEDS_CONFIRMATION', message: 'x', details: HIDDEN } }));
  await h.select();
  const edited = '09:00-10:15\t09:00\t10:15\nLate\t16:00\t17:15';
  const shown = dialogs(h, d => { d.getElementById('twSlotInput').value = edited; return true; }, false, false);
  await h.w.openSlotEditor();
  assert.equal(h.posts.length, 1, 'declined: no second request');
  assert.equal(shown.length, 3, 'editor, confirmation, editor again');
  assert.match(shown[2].body, /Late\t16:00\t17:15/, 'the user’s edit is still there');
  assert.ok(!h.notices.some(n => n.type === 'success'));
});

test('a save the server cannot take now (busy) gives the typed text back', async t => {
  const h = await slotHarness(t, () => ({ httpError: 503, error: { code: 'SERVER_BUSY', message: 'Busy' } }));
  await h.select();
  const shown = dialogs(h, d => { d.getElementById('twSlotInput').value = 'Kept	09:00	10:15'; return true; }, false);
  await h.w.openSlotEditor();
  assert.equal(h.posts.length, 1);
  assert.equal(shown.length, 2, 'the editor came back');
  assert.match(shown[1].body, /Kept	09:00	10:15/);
});

test('a line the editor cannot read saves nothing and is named; a refused column is named', async t => {
  let invalid = false;
  const h = await slotHarness(t, () => invalid
    ? { httpError: 400, error: { code: 'VALIDATION_SLOTS', message: 'x', details: { errors: [{ list: 'slot_config', index: 0, field: 'end', message: 'must be after the start' }] } } }
    : { ok: true });
  await h.select();
  const shown = dialogs(h, d => { d.getElementById('twLabSlotInput').value = 'Lab 1\t09:00\t10:40\nevening labs'; return true; }, false);
  await h.w.openSlotEditor();
  assert.equal(h.posts.length, 0, 'nothing is sent while a line is unread');
  assert.ok(h.notices.some(n => n.type === 'error' && /Labs: line 2/.test(n.text)), JSON.stringify(h.notices));
  assert.match(shown[1].body, /evening labs/, 'the editor reopens with the text');
  invalid = true;
  dialogs(h, true, false);
  await h.w.openSlotEditor();
  assert.ok(h.notices.some(n => n.type === 'error' && n.text === 'The columns were not saved: Lectures, line 1: end must be after the start'), JSON.stringify(h.notices));
});

/* ── One issue model: cards, Issues, counts and board buttons agree with the server ── */
const at = (id, extra = {}) => ({ ...placement, id, term_section_id: id + 100, course_code: `AI${id}`, room: 'R1',
  needs_room: true, room_state: 'assigned', is_online: false, shared_with_other_scenario: false, ...extra });
const quiet = { summary: { critical: 0, warning: 0 }, overlaps: [], instructor_clashes: [], room_clashes: [], cross_board_conflicts: [], hard_overlap_threshold: 20 };
const noRooms = { ...quality, rooms: { assessed: true, shortages: 0, details: [] } };
function modelHarness(t, { placements, conflicts = quiet, q = noRooms, boards = [board], extra = {}, readiness = null }) {
  return harness(t, url => {
    if (url === '/ops/tw/boards/2/') return { placements, slot_config: scenario.slot_config };
    if (url.includes('/conflicts/')) return conflicts;
    if (url.includes('/quality/')) return q;
    if (url.startsWith('/ops/tw/boards/?')) return { boards };
    if (url.includes('/readiness/')) return { readiness: readiness || { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    return undefined;
  }, extra);
}
const card = (h, id) => h.d.querySelector(`.tw-card[data-placement-id="${id}"]`);
const tone = (h, id) => ['critical', 'warning'].find(name => card(h, id).classList.contains(name)) || '';
const issuesText = h => { h.w.renderRightPanel('issues'); return h.d.getElementById('twRightBody').textContent; };

test('a room double-booked and a meeting with no room block publishing everywhere', async t => {
  const h = await modelHarness(t, {
    placements: [at(10, { day: 'SUN' }), at(11, { day: 'MON' }), at(12, { day: 'TUE', room: 'UNASSIGNED', room_state: 'unassigned' })],
    conflicts: { ...quiet, summary: { critical: 0, warning: 1 }, room_clashes: [{ ids: [10, 11], room: 'R1', sections: ['AI10-S1', 'AI11-S1'] }] },
  });
  await h.select();
  assert.deepEqual([tone(h, 10), tone(h, 11), tone(h, 12)], ['critical', 'critical', 'critical']);
  assert.match(card(h, 10).title, /^Blocks publishing: Room double-booked/);
  assert.equal(h.d.querySelector('#twBoardTabs [aria-current="true"] .ux-seg-flag').textContent, '2');
  assert.deepEqual([h.d.getElementById('twIssuesCount').textContent, h.d.getElementById('twIssuesCount').dataset.tone], ['2', 'critical']);
  const text = issuesText(h);
  assert.match(text, /Blocks publishing2Room double-booked/);
  assert.match(text, /No roomAI12 S1/);
  // The placer's marker is never shown: the card says "No room".
  assert.doesNotMatch(h.d.getElementById('twGrid').textContent + text, /UNASSIGNED/);
  assert.match(card(h, 12).textContent, /No room/);
  // MRI's resource layer says the same: a double-booked room is critical there too.
  h.w.renderRightPanel('mri');
  assert.ok(h.d.querySelector('[data-mri-layer="resource-layer"]').classList.contains('critical'));
  assert.deepEqual(h.errors, []);
});

test('a blank room lowers quality, an online meeting needs none, and Arabic never shows the marker', async t => {
  for (const arabic of [false, true]) {
    const h = await harness(t, url => url === '/ops/tw/boards/2/'
      ? { placements: [at(10, { room: '', room_state: 'unset' }), at(11, { day: 'MON', room: '', room_state: 'unset', is_online: true, needs_room: false }), at(12, { day: 'TUE', room: 'UNASSIGNED', room_state: 'unassigned', needs_room: false })], slot_config: scenario.slot_config }
      : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms : undefined, {}, arabic);
    await h.select();
    assert.deepEqual([tone(h, 10), tone(h, 11), tone(h, 12)], ['warning', '', ''], 'a room only where one is needed');
    assert.match(card(h, 11).textContent, arabic ? /عن بُعد/ : /Online/);
    assert.doesNotMatch(h.d.getElementById('twGrid').textContent, /UNASSIGNED/);
    assert.match(issuesText(h), arabic ? /لم تُحدَّد القاعة بعد/ : /Room not assigned yet/);
  }
});

test('sections sharing a time are not red by themselves, and unknown clashes tone nothing', async t => {
  const h = await harness(t, url => url === '/ops/tw/boards/2/' ? { placements: [at(10), at(11)], slot_config: scenario.slot_config }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms : undefined);
  await h.select();
  assert.equal(card(h, 10).parentElement, card(h, 11).parentElement, 'both in one cell');
  assert.deepEqual([tone(h, 10), tone(h, 11)], ['', ''], 'no server clash: no red');
  // Clashes held that are not this board's (a reload in flight, or a failed one): nothing is toned from them.
  h.w.TW.conflicts = { ...quiet, room_clashes: [{ ids: [10, 11], room: 'R1', sections: [] }] };
  h.w.TW.conflictsBoardId = null;
  h.w.renderWorkspaceQuality();
  assert.deepEqual([tone(h, 10), tone(h, 11)], ['', '']);
  assert.match(issuesText(h), /This board’s clashes are not loaded/);
});

test('a grid drawn again is toned at once, before any other redraw', async t => {
  const h = await modelHarness(t, { placements: [at(10)], q: quality });
  await h.select();
  h.w.renderGrid();  // a filter keystroke, a move: new cards
  assert.equal(tone(h, 10), 'warning');
});

test('a check older than the last edit lists its rows as being checked and tones no card', async t => {
  const h = await modelHarness(t, { placements: [at(10)], q: quality });
  await h.select();
  assert.equal(tone(h, 10), 'warning', 'the fresh room check tones the card');
  const before = card(h, 10);
  h.w.TW.dataVersion++;  // an edit: the quality answer is now about older data
  h.w.renderWorkspaceQuality();
  assert.equal(card(h, 10), before, 'tones change in place: the grid is not rebuilt');
  assert.equal(tone(h, 10), '', 'a stale check tones nothing');
  assert.match(issuesText(h), /Room too small · checking/, 'a readiness check is scheduled after the load');
  // No check scheduled or running (it finished, or failed): it says how old the row is.
  h.w.TW.readinessLoading = false;
  h.w.TW.qualityLoading = false;
  let text = issuesText(h);
  assert.match(text, /Room too small · from before your last change/);
  assert.match(text, /have not been checked again/);
  // A check in flight: "checking".
  h.w.TW.qualityLoading = true;
  text = issuesText(h);
  assert.match(text, /Room too small · checking/);
  assert.match(text, /they are being checked/);
});

test('a cross-board pair at the publish line lowers quality; below it, it is context and never counted', async t => {
  const pair = n => ({ board_a_id: 2, board_b_id: 3, placement_a_id: 10, placement_b_id: 99, section_a: 'AI10', section_b: 'FE2', overlap_count: n, board_a_label: 'Term 7', board_b_label: 'Term 8' });
  for (const [n, expected] of [[20, ['1', 'warning', 'warning']], [19, ['', '', '']]]) {
    const h = await modelHarness(t, { placements: [at(10)], conflicts: { ...quiet, cross_board_conflicts: [pair(n)] } });
    await h.select();
    assert.deepEqual([h.d.getElementById('twIssuesCount').textContent, h.d.getElementById('twIssuesCount').dataset.tone || '', tone(h, 10)], expected, `overlap ${n}`);
    assert.match(issuesText(h), n >= 20 ? /Lowers quality1Overlap with another board/ : /For context1Possible overlap with another board/);
  }
});

test('a blocked time and a meeting outside the grid are listed and toned', async t => {
  const h = await modelHarness(t, {
    placements: [at(10), at(11, { day: 'MON', start_time: '18:30', end_time: '19:45' })],
    extra: { blocked_slots: [{ day: 'SUN', start: '09:00' }] },
  });
  await h.select();
  const text = issuesText(h);
  assert.match(text, /On a blocked timeAI10 S1Sun 09:00–10:15/);
  assert.match(text, /Outside the grid’s columnsAI11 S1Mon 18:30–19:45/);
  assert.equal(tone(h, 10), 'critical');
});

test('a section another scenario owns does not drag, even after the controls are re-synced', async t => {
  const h = await modelHarness(t, { placements: [at(10), at(11, { day: 'MON', shared_with_other_scenario: true })] });
  await h.select();
  assert.deepEqual([card(h, 10).draggable, card(h, 11).draggable], [true, false]);
  h.w.syncWorkspaceControls();
  assert.deepEqual([card(h, 10).draggable, card(h, 11).draggable], [true, false]);
});

test('another board’s flag counts its publish-check items, not a stale list figure', async t => {
  const other = { ...board, id: 3, label: 'Term 8', nominal_term: 8, critical: 9 };
  const readiness = { ready: false, blockers: ['a', 'b'], warnings: [], warning_items: [], blocker_items: [
    { code: 'board_critical_conflicts', board_id: 3, count: 2, params: {} },
    { code: 'missing_required_section', board_id: null, count: 1, params: { course_code: 'AI8', programme_term: 8, planned_sections: 1, used_sections: 0 } },
  ] };
  const h = await modelHarness(t, { placements: [at(10)], boards: [board, other], readiness });
  await h.select();
  const flag = () => h.d.querySelector('#twBoardTabs [data-board-id="3"] .ux-seg-flag')?.textContent;
  assert.equal(flag(), '9', 'before any check: the board list’s own count');
  await h.w.refreshPublishReadiness(true);
  assert.equal(flag(), '3', 'two blocking clashes and one section placed nowhere');
});

test('the grid rule matches the shared case table (also run against the server’s rule)', async t => {
  const table = JSON.parse(fs.readFileSync(path.join(root, 'tests/fixtures/grid_rule_cases.json'), 'utf8'));
  const h = await harness(t, undefined, { effective_slot_config: table.lecture_slots, effective_lab_slot_config: table.lab_slots });
  await h.select();
  for (const c of table.cases) {
    const drawn = h.w.isPlacementOnGrid({ course_code: c.course, day: c.day, start_time: c.start, end_time: c.end });
    assert.equal(drawn, c.on_grid, c.name);
  }
});

test('Arabic issue rows keep every time range left to right, and count in the right form', async t => {
  const h = await harness(t, url => url === '/ops/tw/boards/2/'
    ? { placements: [at(10), at(11, { day: 'MON' })], slot_config: scenario.slot_config }
    : url.includes('/conflicts/') ? { ...quiet, overlaps: [{ ids: [10, 11], sections: ['AI10-S1', 'AI11-S1'], severity: 'critical', shared_students: 2 }] }
    : url.includes('/quality/') ? noRooms : undefined, {}, true);
  await h.select();
  h.w.renderRightPanel('issues');
  const html = h.d.getElementById('twRightBody').innerHTML;
  assert.match(html, /الأحد <bdi dir="ltr">09:00–10:15<\/bdi>/);
  assert.match(html, /طالبان يحتاجان كليهما/);
  assert.match(h.d.querySelector('#twBoardTabs [aria-current="true"]').getAttribute('aria-label'), /موانع النشر: 1/);
});

test('a board switch never counts the previous board’s meetings as the new board’s', async t => {
  const other = { ...board, id: 3, label: 'Term 8', nominal_term: 8 };
  const held = deferred();
  const h = await harness(t, url => {
    if (url.startsWith('/ops/tw/boards/?')) return { boards: [board, other] };
    if (url === '/ops/tw/boards/2/') return { placements: [at(10, { room: 'UNASSIGNED', room_state: 'unassigned' })], slot_config: scenario.slot_config };
    if (url === '/ops/tw/boards/3/') return held.promise;
    if (url.includes('/conflicts/')) return quiet;
    if (url.includes('/quality/')) return noRooms;
    return undefined;
  });
  await h.select();
  assert.equal(h.d.getElementById('twIssuesCount').textContent, '1', 'board 2: its meeting with no room');
  const switching = h.w.selectBoard(3);
  h.w.renderReadinessViews();  // something redraws before board 3 answers
  assert.equal(h.d.getElementById('twIssuesCount').textContent, '', 'board 2’s meeting is not board 3’s');
  assert.equal(h.d.querySelector('#twBoardTabs [data-board-id="3"] .ux-seg-flag'), null);
  held.resolve({ placements: [at(30)], slot_config: scenario.slot_config });
  await switching;
  assert.equal(h.d.getElementById('twIssuesCount').textContent, '');
});

test('the open board’s flag keeps the list’s clash count while its own clashes are unknown', async t => {
  const h = await modelHarness(t, { placements: [at(10)], boards: [{ ...board, critical: 4 }], conflicts: { httpError: 500, error: { message: 'down' } } });
  await h.select();
  assert.equal(h.d.querySelector('#twBoardTabs [aria-current="true"] .ux-seg-flag').textContent, '4');
});

test('an issue row takes the user to its sections, on another board too; an unplaceable one is read only', async t => {
  const other = { ...board, id: 3, label: 'Term 8', nominal_term: 8 };
  const pair = { board_a_id: 2, board_b_id: 3, placement_a_id: 10, placement_b_id: 99, section_a: 'AI10', section_b: 'FE2', overlap_count: 25, board_a_label: 'Term 7', board_b_label: 'Term 8' };
  const h = await harness(t, url => {
    if (url.startsWith('/ops/tw/boards/?')) return { boards: [board, other] };
    if (url === '/ops/tw/boards/2/') return { placements: [at(10), at(11), at(12, { day: 'MON', start_time: '18:30', end_time: '19:45' })], slot_config: scenario.slot_config };
    if (url === '/ops/tw/boards/3/') return { placements: [at(99)], slot_config: scenario.slot_config };
    if (url === '/ops/tw/boards/2/conflicts/') return { ...quiet, hard_overlap_threshold: 20, room_clashes: [{ ids: [10, 11], room: 'R1', sections: ['AI10-S1', 'AI11-S1'] }], cross_board_conflicts: [pair] };
    if (url.includes('/conflicts/')) return quiet;
    if (url.includes('/quality/')) return noRooms;
    return undefined;
  });
  await h.select();
  h.w.renderRightPanel('issues');
  const rows = [...h.d.querySelectorAll('#twRightBody .tw-issue-row')];
  const row = kind => rows.find(r => r.querySelector('.tw-issue-kind').textContent.startsWith(kind));
  assert.equal(row('Outside the grid’s columns').tagName, 'DIV', 'not drawn yet: nothing to point at');
  row('Room double-booked').click();
  await settle();
  assert.deepEqual([...h.d.querySelectorAll('.tw-card.highlight')].map(c => c.dataset.placementId).sort(), ['10', '11']);
  row('Overlap with another board').click();
  await wait(() => h.w.TW.loadedBoardId === 3);
  await wait(() => h.d.querySelector('.tw-card.highlight[data-placement-id="99"]'));
});

test('rooms are words everywhere, and those are the words the filter finds', async t => {
  const h = await modelHarness(t, { placements: [
    at(10, { room: 'UNASSIGNED', room_state: 'unassigned', meetings: [{ day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'UNASSIGNED' }] }),
    at(11, { day: 'MON', room: 'UNASSIGNED', room_state: 'unassigned', needs_room: false }),
    at(12, { day: 'TUE', room: '', room_state: 'unset', is_online: true, needs_room: false, meetings: [{ day: 'TUE', start_time: '09:00', end_time: '10:15', room: 'UNASSIGNED' }] }),
  ] });
  await h.select();
  assert.doesNotMatch(card(h, 11).textContent, /No room/, 'a section that needs no room is not missing one');
  card(h, 12).click();
  assert.match(h.d.querySelector('#twRightBody .tw-sel-meetings').textContent, /Online/, 'its meeting is online too');
  assert.doesNotMatch(h.d.getElementById('twRightBody').textContent, /No room|UNASSIGNED/);
  const search = h.d.getElementById('twSearch');
  const find = async text => { search.value = text; search.dispatchEvent(new h.w.Event('input')); await settle(); };
  await find('no room');
  assert.ok(card(h, 10), 'the meeting with no room matches what its card says');
  await find('unassigned');
  assert.equal(card(h, 10), null, 'the placer’s marker is not something the user can see, nor find');
});

test('while a switched board loads, the publish check’s room and blocked-time items stand in for it', async t => {
  const other = { ...board, id: 3, label: 'Term 8', nominal_term: 8 };
  const held = deferred();
  const readiness = { ready: false, blockers: ['x'], warnings: [], warning_items: [], blocker_items: [
    { code: 'board_sections_without_rooms', board_id: 3, count: 2, params: {} },
  ] };
  const h = await harness(t, url => {
    if (url.startsWith('/ops/tw/boards/?')) return { boards: [board, other] };
    if (url === '/ops/tw/boards/2/') return { placements: [at(10)], slot_config: scenario.slot_config };
    if (url === '/ops/tw/boards/3/') return held.promise;
    if (url.includes('/readiness/')) return { readiness };
    if (url.includes('/conflicts/')) return quiet;
    if (url.includes('/quality/')) return noRooms;
    return undefined;
  });
  await h.select();
  await h.w.refreshPublishReadiness(true);
  const flag = () => h.d.querySelector('#twBoardTabs [data-board-id="3"] .ux-seg-flag')?.textContent;
  assert.equal(flag(), '2');
  const switching = h.w.selectBoard(3);
  h.w.renderReadinessViews();
  assert.equal(flag(), '2', 'not dropped while board 3 loads');
  held.resolve({ placements: [at(30, { room: 'UNASSIGNED', room_state: 'unassigned' }), at(31, { day: 'MON', room: 'UNASSIGNED', room_state: 'unassigned' })], slot_config: scenario.slot_config });
  await switching;
  assert.equal(flag(), '2', 'and now read from the board itself');
});
