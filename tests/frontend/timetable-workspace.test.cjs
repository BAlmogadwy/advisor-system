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
    // A real Response, for a body read only once or one that is not JSON.
    if (answer instanceof Response) return answer;
    return { ok: !answer.httpError, status: answer.httpError || 200, json: async () => answer };
  };
  d.getElementById('twYear').value = '1448'; d.getElementById('twTerm').value = '1';
  // The page loads the meeting policy and the shared slot-column module before its own script.
  w.eval(fs.readFileSync(path.join(root, 'static/js/timetable-meeting-policy.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(root, 'static/js/timetable-slot-config.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(root, 'static/js/timetable-section-teacher.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(root, 'static/js/timetable-programme-mark.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(root, 'static/js/timetable-room-choice.js'), 'utf8'));
  // base.html loads the design system's controls (menus, tabs, disclosures) on every page.
  w.eval(fs.readFileSync(path.join(root, 'static/js/ux-controls.js'), 'utf8'));
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

/* ── Failed requests reach the caller (design record §7.3.2 C, 5c) ──
   A dialog keeps its messages inside itself (a toast behind a modal is never
   seen), so twFetch hands every failure the server or the network gives to
   the caller's handler, and still announces a planner job that holds the
   scenario whoever takes the refusal. */
const MOVE_URL = '/ops/tw/placements/10/move/';
const ROOMS_URL = '/ops/tw/placements/10/room-candidates/';

test('F1 a busy refusal the caller takes still announces the planner job, and edits wait for it', async t => {
  const job = { job_id: 'other-tab', scenario_id: 1, status: 'running' };
  const h = await harness(t, (url, opts) => (url === MOVE_URL && opts.method === 'POST'
    ? { httpError: 409, error: { code: 'SCENARIO_BUSY', message: 'Busy', details: { scenario_id: 1, active_job: job } } } : undefined));
  await h.select();
  assert.equal(h.d.getElementById('twOptimise').disabled, false);
  const heard = [], told = [], toasts = h.notices.length;
  h.w.addEventListener('tw:planner-job', event => heard.push([event.detail.jobId, event.detail.scenarioId]));
  // As a dialog commits: quiet, and its handler takes the refusal.
  const answer = await h.w.twFetch(MOVE_URL, {
    method: 'POST', body: '{"room":"R2"}', _quiet: true,
    _onError: (body, status) => (told.push([body?.error?.code, status]), true),
  });
  assert.equal(answer, null);
  assert.deepEqual(told, [['SCENARIO_BUSY', 409]], 'a quiet request still hands its refusal to the handler');
  assert.deepEqual(heard, [['other-tab', 1]]);
  assert.equal(h.w.TW.plannerJob?.job_id, 'other-tab');
  assert.equal(h.d.getElementById('twOptimise').disabled, true, 'the page holds its edits while the job runs');
  assert.equal(h.notices.length, toasts);
});

test('F2 an answer that is not JSON, or no answer, reaches the handler with no body; otherwise the toast reads as before', async t => {
  let fail = null;
  const h = await harness(t, url => (url === MOVE_URL && fail ? fail() : undefined));
  await h.select();
  const proxyPage = () => new Response('<html><body><h1>502 Bad Gateway</h1></body></html>', { status: 502, headers: { 'Content-Type': 'text/html' } });
  const offline = () => { throw new TypeError('Failed to fetch'); };
  const told = [], toasts = h.notices.length;
  const post = taken => h.w.twFetch(MOVE_URL, { method: 'POST', body: '{}', _onError: (body, status) => (told.push([body, status]), taken) });
  fail = proxyPage;
  assert.equal(await post(true), null);
  fail = offline;
  assert.equal(await post(true), null);
  assert.deepEqual(told, [[null, 502], [null, 0]]);
  assert.equal(h.notices.length, toasts, 'the handler took both: no toast');
  // A handler that does not take them (Publish's, for anything but its own
  // refusal) leaves the toasts as they were.
  fail = proxyPage;
  assert.equal(await post(false), null);
  fail = offline;
  assert.equal(await post(false), null);
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'error', text: '502' }, { type: 'error', text: 'Failed to fetch' }]);
  assert.equal(h.d.getElementById('twOptimise').disabled, false, 'every failed write lets go of the page');
});

test('F3 a handler that throws has not taken the failure: it is asked once, and the usual toast follows', async t => {
  let fail = null;
  const h = await harness(t, (url, opts) => (url === MOVE_URL && opts.method === 'POST' && fail ? fail() : undefined));
  await h.select();
  const slips = [], told = [], toasts = h.notices.length;
  h.w.console.error = (...args) => slips.push(args.find(arg => arg instanceof Error)?.message);
  const post = quiet => h.w.twFetch(MOVE_URL, {
    method: 'POST', body: '{"room":"R2"}', _quiet: quiet,
    _onError: (body, status) => { told.push([body?.error?.code ?? null, status]); throw new Error('a handler bug'); },
  });
  fail = () => ({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  assert.equal(await post(false), null);
  fail = () => { throw new TypeError('Failed to fetch'); };
  assert.equal(await post(false), null, 'a handler that throws on a lost answer still lets twFetch answer null');
  assert.deepEqual(told, [['PLACEMENT_EXTERNALLY_OWNED', 409], [null, 0]], 'each failure is told once, never again as a network failure');
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'error', text: 'Owned elsewhere' }, { type: 'error', text: 'Failed to fetch' }],
    'the server’s message (or the network’s), never the handler’s exception');
  assert.deepEqual(slips, ['a handler bug', 'a handler bug'], 'the slip is not lost: it is logged');
  // A quiet request stays quiet when its handler throws.
  fail = () => ({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  assert.equal(await post(true), null);
  assert.equal(told.length, 3);
  assert.equal(h.notices.length, toasts + 2);
  assert.equal(h.d.getElementById('twOptimise').disabled, false, 'every failed write lets go of the page');
});

test('F4 a read refused with 503 that is not the busy lane hands the handler the body the retry loop already read', async t => {
  const h = await harness(t, url => (url === ROOMS_URL
    ? new Response(JSON.stringify({ error: { code: 'MAINTENANCE', message: 'Down for maintenance' } }), { status: 503, headers: { 'Content-Type': 'application/json' } })
    : undefined));
  await h.select();
  const told = [];
  const answer = await h.w.twFetch(ROOMS_URL, { _quiet: true, _onError: (body, status) => (told.push([body?.error?.code, status]), true) });
  assert.equal(answer, null);
  assert.deepEqual(told, [['MAINTENANCE', 503]]);
  assert.equal(h.requests.filter(row => row.url === ROOMS_URL).length, 1, 'not the busy lane: asked once');
});

test('F5 a quiet request fails without a toast, whether refused or unanswered', async t => {
  let fail = null;
  const h = await harness(t, (url, opts) => (url === MOVE_URL && opts.method === 'POST' && fail ? fail() : undefined));
  await h.select();
  const toasts = h.notices.length;
  const post = extra => h.w.twFetch(MOVE_URL, { method: 'POST', body: '{"room":"R2"}', _quiet: true, ...extra });
  const refusal = () => ({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  const offline = () => { throw new TypeError('Failed to fetch'); };
  for (const answer of [refusal, offline]) {
    fail = answer;
    assert.equal(await post({}), null, `${answer.name}: no handler`);
    assert.equal(await post({ _onError: () => false }), null, `${answer.name}: a handler that declines`);
  }
  assert.deepEqual(h.notices.slice(toasts), [], 'no toast for a quiet request');
  assert.equal(h.d.getElementById('twOptimise').disabled, false, 'every failed write lets go of the page');
});

test('F6 a write held by the busy guard never reaches the handler; a handler that answers nothing has not taken the failure', async t => {
  const hang = deferred();
  let fail = null;
  const h = await harness(t, (url, opts) => {
    if (url === '/ops/tw/placements/11/move/' && opts.method === 'POST') return hang.promise;
    return url === MOVE_URL && opts.method === 'POST' && fail ? fail() : undefined;
  });
  await h.select();
  const told = [], toasts = h.notices.length;
  // Another write is in flight: the workspace is busy.
  const first = h.w.twFetch('/ops/tw/placements/11/move/', { method: 'POST', body: '{}' });
  const held = await h.w.twFetch(MOVE_URL, { method: 'POST', body: '{"room":"R2"}', _quiet: true, _onError: (body, status) => (told.push([body, status]), true) });
  assert.equal(held, null);
  assert.deepEqual(told, [], 'the busy guard is the page’s own: the handler is not asked');
  assert.equal(h.requests.filter(row => row.url === MOVE_URL).length, 0, 'nothing was sent');
  assert.deepEqual(h.notices.slice(toasts).map(n => n.type), ['warning'], 'the guard says why (a toast)');
  hang.resolve({ placement: { id: 11 } });
  await first;
  // A handler that answers nothing (undefined) has not taken the failure.
  fail = () => ({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  assert.equal(await h.w.twFetch(MOVE_URL, { method: 'POST', body: '{}', _onError: (body, status) => { told.push([body?.error?.code, status]); } }), null);
  assert.deepEqual(told, [['PLACEMENT_EXTERNALLY_OWNED', 409]]);
  assert.deepEqual(h.notices.slice(toasts + 1), [{ type: 'error', text: 'Owned elsewhere' }]);
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
  // Its label says what it does; aria-pressed would say the state twice.
  assert.equal(inspectorLock.hasAttribute('aria-pressed'), false);
  assert.equal(inspectorLock.getAttribute('title'), help);
  assert.equal(inspectorLock.hasAttribute('dir'), false);
  const inspectorRemove = inspectorButtons.find(el => /removePlacement\(10\)/.test(el.getAttribute('onclick') || ''));
  assert.ok(inspectorRemove);
  assert.equal(inspectorRemove.hasAttribute('title'), false);
  // The sentence is also spelled out under the buttons, once, in the page's language.
  assert.equal(h.d.querySelector('#twRightBody .tw-sel-actions + .tw-sel-help').textContent, help);
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
    // The heading is the group's disclosure button; its rows are in the group's region.
    const row = h.d.getElementById(head.getAttribute('aria-controls')).querySelector('.tw-issue-row');
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
  assert.match(h.d.getElementById(head.getAttribute('aria-controls')).textContent, /AI222: 1 of 2 sections not placed \(term 7\)/);
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
function modelHarness(t, { placements, conflicts = quiet, q = noRooms, boards = [board], extra = {}, readiness = null, arabic = false }) {
  return harness(t, url => {
    if (url === '/ops/tw/boards/2/') return { placements, slot_config: scenario.slot_config };
    if (url.includes('/conflicts/')) return conflicts;
    if (url.includes('/quality/')) return q;
    if (url.startsWith('/ops/tw/boards/?')) return { boards };
    if (url.includes('/readiness/')) return { readiness: readiness || { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    return undefined;
  }, extra, arabic);
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

/* ── Card anatomy (design record §7.2, PR 3) ── */
test('a card says code and section, its room in words, and its marks — never capacity', async t => {
  const h = await modelHarness(t, { placements: [
    at(10, { course_code: 'MATH101', available_capacity: 40, is_locked: true, room: 'UNASSIGNED', room_state: 'unassigned' }),
    at(11, { day: 'MON', shared_with_other_scenario: true, teacher: { name: 'Dr Sara Ali', status: 'assigned' } }),
    at(12, { day: 'TUE', teacher: { name: 'Stale name', status: 'tbd' }, meetings: [{ day: 'TUE', start_time: '09:00', end_time: '10:15', instructor: 'Stale name' }] }),
  ] });
  await h.select();
  const c10 = card(h, 10), c11 = card(h, 11), c12 = card(h, 12);
  assert.equal(c10.querySelector('.tw-card-code').textContent, 'MATH101');
  assert.equal(c10.querySelector('.tw-card-room').dataset.roomState, 'unassigned');
  assert.doesNotMatch(c10.textContent, /cap|40/, 'capacity is in Selection, not on the card');
  assert.deepEqual([...c10.querySelectorAll('.tw-card-marks .ux-icon')].map(i => i.dataset.icon), ['lock']);
  assert.match(c10.querySelector('[data-icon="lock"]').getAttribute('aria-label'), /Time locked/);
  assert.deepEqual([...c11.querySelectorAll('.tw-card-marks .ux-icon')].map(i => i.dataset.icon), ['shared']);
  assert.equal(c11.querySelector('.tw-card-teacher').textContent, 'Dr Sara Ali');
  assert.equal(c12.querySelector('.tw-card-teacher'), null, 'a teacher still to be decided shows no stale name');
  // The tone mark is an element on the meta line, set from the model in place.
  const mark = c10.querySelector('.tw-card-tone');
  assert.deepEqual([mark.hidden, mark.dataset.tone], [false, 'critical']);
  assert.equal(c12.querySelector('.tw-card-tone').hidden, true);
});

test('selecting a card marks it for assistive technology and echoes its course elsewhere', async t => {
  const h = await modelHarness(t, { placements: [
    at(10, { course_key: 'CS163' }), at(11, { day: 'TUE', course_key: 'CS163', term_section_id: 110 }), at(12, { day: 'MON', course_key: 'CS999' }),
  ] });
  await h.select();
  card(h, 10).click();
  assert.equal(card(h, 10).getAttribute('aria-current'), 'true');
  assert.ok(card(h, 11).classList.contains('is-same-course'));
  assert.equal(card(h, 12).classList.contains('is-same-course'), false);
  h.w.renderGrid();  // a redraw keeps both
  assert.equal(card(h, 10).getAttribute('aria-current'), 'true');
  assert.ok(card(h, 11).classList.contains('is-same-course'));
  card(h, 12).click();
  assert.equal(card(h, 10).hasAttribute('aria-current'), false);
  assert.equal(card(h, 11).classList.contains('is-same-course'), false);
});

test('a course has one colour whatever was drawn first, and two courses sharing a code differ', async t => {
  // Two fresh pages that meet the courses in different orders.
  const one = await harness(t);
  const two = await harness(t);
  const first = one.w.courseColor('CS112::PROGRAMMING_I');
  two.w.courseColor('AI10'); two.w.courseColor('ZZ999'); two.w.courseColor('MATH101');
  assert.equal(two.w.courseColor('CS112::PROGRAMMING_I'), first);
  assert.notEqual(one.w.courseColor('CS112::PROGRAMMING_II'), first);
});

test('a located card pulses and then lets go', async t => {
  const h = await modelHarness(t, { placements: [at(10), at(11, { day: 'MON' })] });
  await h.select();
  h.w.HTMLElement.prototype.scrollIntoView = () => {};  // jsdom has none
  h.w.highlightWorkspacePlacements([10, 11]);
  assert.equal(h.d.querySelectorAll('.tw-card.highlight').length, 2);
  card(h, 10).dispatchEvent(new h.w.Event('animationend'));
  assert.equal(card(h, 10).classList.contains('highlight'), false);
  assert.ok(card(h, 11).classList.contains('highlight'), 'each card lets go on its own animation');
});

test('the legend says what the marks mean, in both languages, under a board that has cards', async t => {
  for (const arabic of [false, true]) {
    const h = await harness(t, url => url.includes('/conflicts/') ? quiet : undefined, {}, arabic);
    await h.select();
    const legend = h.d.getElementById('twLegend');
    assert.ok(legend, 'drawn after the tables');
    assert.equal(legend.getAttribute('role'), 'note');
    assert.match(legend.textContent, arabic ? /يمنع النشر.*يُضعف الجودة.*الوقت مقفل/ : /Blocks publishing.*Lowers quality.*Time locked/);
    assert.ok(legend.querySelector('.ux-icon[data-icon="lock"]') && legend.querySelector('.ux-icon[data-icon="shared"]'));
  }
});

test('two courses sharing a code are told apart on the board: colour by key, name read in its own direction', async t => {
  const placements = [
    at(10, { course_code: 'CS112', course_key: 'CS112::PROGRAMMING_I', course_name: 'Programming I' }),
    at(11, { day: 'MON', course_code: 'CS112', course_key: 'CS112::PROGRAMMING_II', course_name: 'Programming II', term_section_id: 120 }),
  ];
  // The plan lens is where the page learns that two courses share a code.
  const h = await harness(t, url => url === '/ops/tw/boards/2/' ? { placements, slot_config: scenario.slot_config }
    : url.includes('/plan-lens/') ? { plan_lens: { plans: [], sections: {}, courses: { 'CS112::PROGRAMMING_I': {}, 'CS112::PROGRAMMING_II': {} } } }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms : undefined);
  await h.select();
  const hue = id => card(h, id).style.getPropertyValue('--tw-card-hue');
  assert.notEqual(hue(10), hue(11), 'the card colours by course key, not by code');
  assert.equal(card(h, 10).querySelector('.tw-card-name').getAttribute('dir'), 'auto');
});

test('the tone mark is announced, with the reason', async t => {
  const h = await modelHarness(t, { placements: [at(10, { room: 'UNASSIGNED', room_state: 'unassigned' })] });
  await h.select();
  const mark = card(h, 10).querySelector('.tw-card-tone');
  assert.equal(mark.getAttribute('role'), 'img');
  assert.equal(mark.getAttribute('aria-label'), 'Blocks publishing: No room');
});

test('a located card lets go without an animation too, and a second locate is never cut short', async t => {
  const h = await modelHarness(t, { placements: [at(10)] });
  await h.select();
  h.w.HTMLElement.prototype.scrollIntoView = () => {};
  // Fake timers for the locate only.
  const timers = new Map(); let next = 1;
  const realSet = h.w.setTimeout, realClear = h.w.clearTimeout;
  h.w.setTimeout = (fn, ms) => { const id = next++; timers.set(id, fn); return id; };
  h.w.clearTimeout = id => timers.delete(id);
  h.w.highlightWorkspacePlacements([10]);
  h.w.highlightWorkspacePlacements([10]);  // again, before the first ends
  h.w.setTimeout = realSet; h.w.clearTimeout = realClear;
  assert.equal(timers.size, 1, 'one timer per card: the first was replaced');
  assert.ok(card(h, 10).classList.contains('highlight'));
  [...timers.values()].forEach(fn => fn());  // no animationend (reduced motion)
  assert.equal(card(h, 10).classList.contains('highlight'), false);
});

/* ── Inspector (design record §7.2, PR 4a) ── */
test('Issues groups open and close, context starts closed, and a choice survives every redraw', async t => {
  const pair = n => ({ board_a_id: 2, board_b_id: 3, placement_a_id: 10, placement_b_id: 99, section_a: 'AI10', section_b: 'FE2', overlap_count: n, board_a_label: 'Term 7', board_b_label: 'Term 8' });
  const h = await modelHarness(t, { placements: [at(10, { room: 'UNASSIGNED', room_state: 'unassigned' })],
    conflicts: { ...quiet, cross_board_conflicts: [pair(5)] } });
  await h.select();
  h.w.sessionStorage.clear();
  h.w.renderRightPanel('issues');
  const toggle = tone => h.d.querySelector(`#twRightBody .tw-issue-head[data-tone="${tone}"]`);
  const region = tone => h.d.getElementById(toggle(tone).getAttribute('aria-controls'));
  assert.deepEqual([toggle('critical').getAttribute('aria-expanded'), region('critical').hidden], ['true', false]);
  assert.deepEqual([toggle('info').getAttribute('aria-expanded'), region('info').hidden], ['false', true], 'context starts closed');
  assert.equal(toggle('info').querySelector('.tw-issue-count').textContent, '1', 'its count shows while closed');
  toggle('critical').click();
  assert.deepEqual([toggle('critical').getAttribute('aria-expanded'), region('critical').hidden], ['false', true]);
  h.w.renderReadinessViews();  // a redraw: the user's choice holds
  assert.deepEqual([toggle('critical').getAttribute('aria-expanded'), region('critical').hidden], ['false', true]);
  toggle('info').click();
  h.w.renderRightPanel('issues');
  assert.equal(region('info').hidden, false);
});

test('Selection lists the section’s own issues, its teacher by the rule, and tells 0 from unknown', async t => {
  const tbd = { name: '', status: 'tbd', reason: 'clash' };
  const h = await modelHarness(t, { placements: [
    at(10, { room: 'UNASSIGNED', room_state: 'unassigned', available_capacity: 0, registered_count: null, teacher: tbd,
      meetings: [{ day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'UNASSIGNED', instructor: 'Stale Name' }] }),
    at(11, { day: 'MON', teacher: { name: '', status: 'frozen_unknown' },
      meetings: [{ day: 'MON', start_time: '09:00', end_time: '10:15', room: 'R1', instructor: 'Dr Meeting' }] }),
  ], conflicts: { ...quiet, cross_board_conflicts: [{ board_a_id: 2, board_b_id: 3, placement_a_id: 10, placement_b_id: 99,
    section_a: 'AI10', section_b: 'FE2', overlap_count: 5, board_a_label: 'Term 7', board_b_label: 'Term 8' }] } });
  await h.select();
  const selection = id => { card(h, id).click(); return h.d.getElementById('twRightBody'); };
  const body = selection(10);
  assert.match(body.querySelector('.tw-sel-issues').textContent, /No room/);
  assert.equal(body.querySelector('.tw-sel-issues .ux-tone-icon').getAttribute('aria-label'), 'Blocks publishing');
  const fields = Object.fromEntries([...body.querySelectorAll('.tw-sel-field')].map(f => [f.querySelector('dt').textContent, f.querySelector('dd').textContent]));
  assert.match(fields.Teacher, /Teacher to be decided.*Every eligible teacher is teaching at this time/);
  assert.equal(fields.Capacity, '0', 'a capacity of 0 is a capacity');
  assert.equal(fields.Registered, '—', 'unknown is not 0');
  assert.doesNotMatch(body.querySelector('.tw-sel-meetings').textContent, /Stale Name/, 'no stale names in the meetings list');
  assert.match(body.querySelector('.tw-sel-issues').textContent, /AI10 S1/, 'each issue names what it is about');
  assert.doesNotMatch(body.querySelector('.tw-sel-issues').textContent, /Possible overlap/, 'context stays in the Issues tab');
  const other = selection(11);
  assert.equal(other.querySelector('.tw-sel-issues'), null, 'nothing wrong: no list');
  const teacher = [...other.querySelectorAll('.tw-sel-field')].find(f => f.querySelector('dt').textContent === 'Teacher');
  assert.equal(teacher.querySelector('dd').textContent, 'Dr Meeting', 'a section the map does not know keeps its meeting name');
});

test('the keyboard stays on an Issues group or row through every redraw', async t => {
  const h = await modelHarness(t, { placements: [at(10, { room: 'UNASSIGNED', room_state: 'unassigned' }), at(11, { day: 'MON' })], q: quality });
  await h.select();
  h.w.renderRightPanel('issues');
  const head = () => h.d.querySelector('#twRightBody .tw-issue-head[data-tone="critical"]');
  head().focus();
  const node = head();
  h.w.renderReadinessViews();  // the same issues: nothing is drawn again
  assert.equal(head(), node, 'unchanged content keeps its nodes');
  h.w.TW.dataVersion++;  // an edit: rows turn "checking", the panel is drawn anew
  h.w.renderReadinessViews();
  assert.notEqual(head(), node);
  assert.equal(h.d.activeElement, head(), 'focus is back on the same group');
  const row = () => h.d.querySelector('#twRightBody .tw-issue-row[data-tone="warning"]');
  h.w.TW.readinessLoading = false; h.w.TW.qualityLoading = false;
  h.w.renderRightPanel('issues');
  row().focus();
  const rowNode = row();
  h.w.TW.qualityLoading = true;  // its caveat turns to "checking"
  h.w.renderRightPanel('issues');
  assert.notEqual(row(), rowNode, 'drawn anew');
  assert.equal(h.d.activeElement, row(), 'and on the same row');
  // A row still takes the user to its section after a redraw (one listener).
  row().click();
  await settle();
  assert.ok(card(h, 11).classList.contains('highlight') || card(h, 10).classList.contains('highlight'));
  assert.deepEqual(h.errors, []);
});

test('Selection marks an item from before the last edit, names a pair, and says None in both languages', async t => {
  for (const arabic of [false, true]) {
    const h = await modelHarness(t, { arabic, q: quality, placements: [
      at(10, { teacher: { name: '', status: 'pinned' } }),
      at(11, { day: 'MON' }),
    ], conflicts: { ...quiet, summary: { critical: 0, warning: 1 }, room_clashes: [{ ids: [10, 11], room: 'R1', sections: ['AI10-S1', 'AI11-S1'] }] } });
    await h.select();
    card(h, 10).click();
    const body = h.d.getElementById('twRightBody');
    const issues = () => [...body.querySelectorAll('.tw-sel-issues li')].map(li => li.textContent);
    assert.ok(issues().some(text => /AI10-S1/.test(text) && /AI11-S1/.test(text)), `a pair names both sections: ${issues()}`);
    const teacher = [...body.querySelectorAll('.tw-sel-field')][3].querySelector('dd').textContent;
    assert.equal(teacher, arabic ? 'لا يوجد' : 'None', 'the drawer’s word for no teacher');
    assert.ok(!issues().some(text => /checking|from before|يجري التحقق|من قبل آخر تعديل/.test(text)), 'fresh rows carry no caveat');
    h.w.TW.dataVersion++;  // an edit: the room check is about older data
    h.w.TW.readinessLoading = false; h.w.TW.qualityLoading = false;
    h.w.renderRightPanel('selection');
    const room = issues().find(text => /Room too small|القاعة أصغر/.test(text));
    assert.ok(room, issues().join(' | '));
    assert.match(room, arabic ? /من قبل آخر تعديل/ : /from before your last change/);
    assert.deepEqual(h.errors, []);
  }
});

test('search finds a section by the teacher its card names, never by a stale meeting name', async t => {
  const h = await modelHarness(t, { placements: [
    at(10, { teacher: { name: 'Dr Rule', status: 'pinned' }, meetings: [{ day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'R1', instructor: 'Stale Name' }] }),
    at(11, { day: 'MON', meetings: [{ day: 'MON', start_time: '09:00', end_time: '10:15', room: 'R1', instructor: 'Dr Meeting' }] }),
  ] });
  await h.select();
  const search = h.d.getElementById('twSearch');
  const find = async text => { search.value = text; search.dispatchEvent(new h.w.Event('input')); await settle(); };
  await find('dr rule');
  assert.ok(card(h, 10));
  await find('stale name');
  assert.equal(card(h, 10), null, 'the stale name is not on the card, so it finds nothing');
  await find('dr meeting');
  assert.ok(card(h, 11), 'a section the rule leaves to its meetings keeps that name');
});

/* ── Programme mark in Selection (PR 4b) ── */
const LENS = sections => ({ plan_lens: { plans: ['AI', 'AI2'], courses: { CS112: { plans: { AI2: 4 } }, CS113: { plans: { AI: 9 } } },
  sections, programme_grouping: { students_regrouped: 3, student_gap_minutes_change: -20 } } });
const markRows = () => [
  at(10, { course_key: 'CS112', preferred_programme: '', preferred_programme_editable: true }),
  at(13, { day: 'WED', course_key: 'CS112', term_section_id: 110, preferred_programme: '', preferred_programme_editable: true }),  // its other meeting
  at(11, { day: 'MON', course_key: 'CS113', preferred_programme: 'CYS', preferred_programme_editable: false, term_section_id: 111 }),
  at(12, { day: 'TUE', course_key: 'CS114' }),  // the feature off for this one: no key
];
async function markHarness(t, { sections = () => ({ 110: { actual_total: 38, actual_plans: { AI2: 30, AI: 6 } } }), lens = null,
  answer = null, hold = {}, extra = {}, arabic = false, rows = markRows() } = {}) {
  const seen = { posts: [], lens: 0, quality: 0, readiness: 0 };
  const h = await harness(t, async (url, opts) => {
    if (url === '/ops/tw/boards/2/') return { placements: rows, slot_config: scenario.slot_config };
    if (url.includes('/plan-lens/')) {
      const n = ++seen.lens;
      if (hold.lens) await hold.lens(n);
      return lens ? lens(n) : LENS(sections());
    }
    if (url.endsWith('/programme-preference/') && opts.method === 'POST') {
      const body = JSON.parse(opts.body);
      seen.posts.push(body);
      if (hold.post) await hold.post();
      return answer ? answer(body) : { term_section_id: 110, preferred_programme: body.preferred_programme, changed: true };
    }
    if (url.includes('/conflicts/')) return quiet;
    if (url.includes('/quality/')) { seen.quality++; return noRooms; }
    if (url.includes('/readiness/')) { seen.readiness++; return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } }; }
    return undefined;
  }, extra, arabic);
  await h.select();
  const body = () => h.d.getElementById('twRightBody');
  const select = () => h.d.getElementById('twSelProgramme');
  const choose = (value, { keyed = false } = {}) => {
    const s = select();
    if (keyed) s.dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    s.value = value;
    s.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  };
  return Object.assign(h, { seen, body, select, choose });
}

test('Selection carries the programme mark where the feature does, with the seated mix and its trade-off', async t => {
  for (const arabic of [false, true]) {
    const h = await markHarness(t, { arabic });
    card(h, 12).click();
    assert.equal(h.select(), null, 'no key, no mark');
    card(h, 10).click();
    assert.deepEqual([...h.select().options].map(o => o.value), ['', 'AI', 'AI2']);
    assert.equal(h.select().disabled, false);
    assert.equal(h.body().querySelector('label[for="twSelProgramme"]')?.textContent, arabic ? 'مخصّصة أساساً لبرنامج' : 'Mainly for programme');
    // The figure in one LTR isolate (an Arabic panel would paint «38: … :38» otherwise).
    assert.equal(h.body().querySelector('.tw-sel-field bdi[dir="ltr"]')?.textContent, '38: AI2 30 · AI 6');
    assert.match(h.body().textContent, arabic ? /نقل التجميع 3 طالباً/ : /Grouping moved 3 students/);
    assert.equal(h.body().querySelector('[data-tw-lens-refresh]'), null, 'a fresh lens offers no Refresh');
    // A definition list holds only its groups.
    assert.deepEqual([...h.body().querySelectorAll('dl')].flatMap(dl => [...dl.children].filter(c => c.tagName !== 'DIV').map(c => c.tagName)), []);
    card(h, 11).click();
    assert.equal(h.select().disabled, true, 'another scenario owns it');
    const note = h.d.getElementById(h.select().getAttribute('aria-describedby'));
    assert.match(note.textContent, arabic ? /لا يحتاج أي طالب من CYS/ : /No CYS student needs this course/, 'the warning describes the select');
    assert.deepEqual(h.errors, []);
  }
});

test('the mark is off while the scenario cannot be edited, and a held mark stays off when editing resumes', async t => {
  const published = await markHarness(t, { extra: { status: 'published' } });
  card(published, 10).click();
  assert.equal(published.select().disabled, true, 'a published scenario');
  const h = await markHarness(t);
  card(h, 10).click();
  h.w.TW.plannerJob = { job_id: 'j', status: 'running' };
  h.w.syncWorkspaceControls();
  assert.equal(h.select().disabled, true, 'a job is running');
  h.w.TW.plannerJob = null;
  h.w.syncWorkspaceControls();
  assert.equal(h.select().disabled, false);
  card(h, 11).click();
  h.w.syncWorkspaceControls();
  assert.equal(h.select().disabled, true, 'another scenario’s section stays off');
});

test('a section the lens does not hold is unknown; one it holds with nobody seated says so', async t => {
  const h = await markHarness(t, { sections: () => ({ 111: { actual_total: 0, actual_plans: {} } }) });
  card(h, 10).click();
  const seated = () => [...h.body().querySelectorAll('.tw-sel-field')].find(f => /Seated/.test(f.querySelector('dt').textContent)).querySelector('dd');
  assert.match(seated().textContent, /^—/);
  assert.doesNotMatch(seated().textContent, /No student/);
  card(h, 11).click();
  assert.match(seated().textContent, /No student of this scenario sits here/);
});

test('after an edit the mix and its trade-off say how old they are; Refresh reads the lens once and says so', async t => {
  let total = 38;
  const held = deferred();
  let holding = false;
  const h = await markHarness(t, { sections: () => ({ 110: { actual_total: total, actual_plans: { AI2: total } } }), hold: { lens: () => (holding ? held.promise : null) } });
  const before = h.seen.lens;
  card(h, 10).click();
  h.w.TW.dataVersion++;  // an edit
  h.w.renderRightPanel('selection');
  assert.equal(h.seen.lens, before, 'an edit never re-reads the lens');
  assert.match(h.body().textContent, /From before your last change/);
  total = 40; holding = true;
  const refresh = h.body().querySelector('[data-tw-lens-refresh]');
  refresh.focus();
  refresh.click();
  await settle();
  assert.match(h.body().textContent, /Loading programme mix/);
  const busy = h.body().querySelector('[data-tw-lens-refresh]');
  assert.equal(busy.disabled, true, 'Refresh waits while it reads');
  busy.disabled = false; busy.click(); h.w.loadPlanLens();  // a second ask joins the read in flight
  await settle();
  assert.equal(h.seen.lens, before + 1, 'one read');
  held.resolve();
  await wait(() => /40: AI2 40/.test(h.body().textContent));
  assert.doesNotMatch(h.body().textContent, /From before your last change/);
  assert.equal(h.d.getElementById('twInspectorStatus').textContent, 'Programme mix updated.');
  assert.ok(h.body().contains(h.d.activeElement), 'the keyboard stays in the panel');
});

test('an older lens answer never replaces a newer one', async t => {
  const first = deferred();
  const h = await markHarness(t, { lens: n => LENS({ 110: { actual_total: n, actual_plans: { AI2: n } } }),
    hold: { lens: n => (n === 2 ? first.promise : null) } });
  const older = h.w.loadPlanLens();  // the second read, held
  h.w.TW.dataVersion++;
  await h.w.loadPlanLens();  // the third, about newer data, answers first
  first.resolve();
  await older;
  assert.equal(h.w.TW.planLens.version, h.w.TW.dataVersion);
  assert.equal(h.w.TW.planLens.sections['110'].actual_total, 3, 'the newer read stands');
  assert.equal(h.w.TW.planLens.loading, false);
});

test('saving posts once, keeps the keyboard, reads the lens, the quality and the publish check once each', async t => {
  const post = deferred(), lensHeld = deferred();
  let gateLens = false;
  const h = await markHarness(t, { sections: () => ({ 110: { actual_total: 10, actual_plans: { AI2: 10 } } }),
    hold: { post: () => post.promise, lens: () => (gateLens ? lensHeld.promise : null) } });
  card(h, 10).click();
  const [lens0, quality0] = [h.seen.lens, h.seen.quality];
  h.select().focus();
  h.choose('AI2');
  await settle();
  h.w.saveWorkspaceProgrammeMark(10, 'AI');  // a second save of the section waits its turn
  await settle();
  assert.equal(h.seen.posts.length, 1, 'one save at a time');
  assert.equal(h.select().disabled, true, 'off while it saves');
  assert.equal(h.select().getAttribute('aria-busy'), 'true');
  assert.equal(h.d.activeElement, h.body(), 'the panel holds the keyboard meanwhile');
  gateLens = true;
  post.resolve();
  await wait(() => h.seen.lens === lens0 + 1);
  assert.equal(h.d.activeElement, h.select(), 'the select has the keyboard back before the lens answers');
  assert.match(h.body().textContent, /Loading programme mix/);
  assert.doesNotMatch(h.body().textContent, /From before your last change/, 'never stale while it reloads');
  lensHeld.resolve();
  await wait(() => h.seen.quality === quality0 + 1 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
  assert.deepEqual(h.seen.posts.map(b => b.preferred_programme), ['AI2']);
  assert.deepEqual(h.w.TW.placements.filter(p => p.term_section_id === 110).map(p => p.preferred_programme), ['AI2', 'AI2'], 'every meeting of the section');
  assert.ok(h.notices.some(n => n.type === 'success'));
  assert.equal(h.seen.lens, lens0 + 1, 'the lens once');
  assert.equal(h.w.TW.planLens.version, h.w.TW.dataVersion);
  assert.equal(h.w.TW.readinessLoading, true, 'the publish check is scheduled');
  assert.equal(h.d.activeElement, h.select());
  assert.deepEqual(h.errors, []);
});

test('a refused save shows the server’s mark and gives the keyboard back; reading nothing again', async t => {
  const h = await markHarness(t, { answer: () => ({ httpError: 409, error: { code: 'SCENARIO_BUSY', message: 'Busy' } }) });
  card(h, 10).click();
  const [lens0, quality0, version0] = [h.seen.lens, h.seen.quality, h.w.TW.dataVersion];
  h.select().focus();
  h.choose('AI2');
  await wait(() => !h.select().disabled);
  assert.equal(h.select().value, '', 'the mark the server holds');
  assert.equal(h.d.activeElement, h.select());
  assert.deepEqual([h.seen.lens, h.seen.quality, h.w.TW.dataVersion], [lens0, quality0, version0]);
  assert.ok(h.notices.some(n => n.type === 'error' && n.text === 'Busy'));
});

test('the same mark again changes nothing: every answer stays current', async t => {
  const h = await markHarness(t, { answer: body => ({ term_section_id: 110, preferred_programme: body.preferred_programme, changed: false }) });
  card(h, 10).click();
  const [lens0, quality0, version0] = [h.seen.lens, h.seen.quality, h.w.TW.dataVersion];
  h.choose('');
  await wait(() => h.seen.posts.length === 1 && !h.select().disabled);
  assert.equal(h.w.TW.dataVersion, version0);
  assert.deepEqual([h.seen.lens, h.seen.quality], [lens0, quality0]);
  assert.doesNotMatch(h.body().textContent, /From before your last change/);
  h.w.renderRightPanel('issues');
  assert.doesNotMatch(h.body().textContent, /from before your last change/);
});

test('a user who moved on while the lens reloads keeps their place', async t => {
  const held = deferred();
  let gate = false;
  const h = await markHarness(t, { hold: { lens: () => (gate ? held.promise : null) } });
  card(h, 10).click();
  const lens0 = h.seen.lens;
  const quality0 = h.seen.quality;
  h.select().focus();
  gate = true;
  h.choose('AI2');
  await wait(() => h.seen.lens === lens0 + 1);
  const lock = () => h.body().querySelector('[data-focus-key="lock"]');
  lock().focus();
  held.resolve();
  await wait(() => h.seen.quality === quality0 + 1 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
  assert.equal(h.d.activeElement, lock());
});

test('a keyboard choice saves once the arrow keys rest, or at once on Enter or on leaving', async t => {
  const h = await markHarness(t);
  h.w.TimetableProgrammeMark.settleMs = 5;
  card(h, 10).click();
  const quality0 = h.seen.quality;
  h.select().focus();
  h.choose('AI', { keyed: true });
  h.choose('AI2', { keyed: true });
  await settle();
  assert.equal(h.seen.posts.length, 0, 'nothing while the keys move');
  await new Promise(resolve => setTimeout(resolve, 20));
  await wait(() => h.seen.posts.length === 1 && !h.select().disabled);
  assert.deepEqual(h.seen.posts.map(b => b.preferred_programme), ['AI2'], 'only where the keys stopped');
  h.w.TimetableProgrammeMark.settleMs = 60000;
  h.choose('AI', { keyed: true });
  h.select().dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await wait(() => h.seen.posts.length === 2 && !h.select().disabled);
  h.choose('', { keyed: true });
  h.select().dispatchEvent(new h.w.FocusEvent('focusout', { bubbles: true }));
  await wait(() => h.seen.posts.length === 3);
  assert.deepEqual(h.seen.posts.map(b => b.preferred_programme), ['AI2', 'AI', '']);
  // Each save reads the lens and the quality again: let them finish.
  await wait(() => h.seen.quality === quality0 + 3 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
});

test('a programme search filters anew when the lens is read again', async t => {
  let plans = ['AI'];
  const h = await markHarness(t, { lens: () => ({ plan_lens: { plans: ['AI', 'AI2'], courses: {}, sections: { 110: { actual_total: 1, actual_plans: { AI: 1 }, filter_plans: plans } } } }) });
  const search = h.d.getElementById('twSearch');
  search.value = 'AI2';
  search.dispatchEvent(new h.w.Event('input'));
  await settle();
  assert.equal(card(h, 10), null, 'not an AI2 section yet');
  plans = ['AI', 'AI2'];
  h.w.TW.dataVersion++;
  await h.w.refreshPlanLens();
  assert.ok(card(h, 10), 'the new lens puts it in AI2');
});

test('a lens that cannot be read offers Refresh and is not left loading', async t => {
  let fail = true;
  const h = await markHarness(t, { lens: () => (fail ? { httpError: 500, error: { message: 'down' } } : LENS({ 110: { actual_total: 2, actual_plans: { AI: 2 } } })) });
  card(h, 10).click();
  assert.equal(h.w.TW.planLens.loading, false);
  assert.match(h.body().textContent, /The programme mix is not available/);
  fail = false;
  h.body().querySelector('[data-tw-lens-refresh]').click();
  await wait(() => /2: AI 2/.test(h.body().textContent));
});

test('a save that ends while another inspector tab is open leaves that tab alone', async t => {
  const post = deferred(), lensHeld = deferred();
  let gate = false;
  const h = await markHarness(t, { hold: { post: () => post.promise, lens: () => (gate ? lensHeld.promise : null) } });
  card(h, 10).click();
  const [lens0, quality0] = [h.seen.lens, h.seen.quality];
  h.choose('AI2');
  await settle();
  h.d.getElementById('twTabIssues').click();
  gate = true;
  post.resolve();
  await wait(() => h.seen.lens === lens0 + 1);
  assert.equal(h.select(), null, 'Issues is still what the panel shows while the lens reloads');
  lensHeld.resolve();
  await wait(() => h.seen.quality === quality0 + 1 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
  assert.equal(h.select(), null);
  assert.ok(h.body().querySelector('.tw-empty, .tw-issue-group'), h.body().innerHTML.slice(0, 200));
});

// For saves behind the keyboard's settle timer: waits in time, not in turns.
const waitTimed = async (condition, ms = 3000) => {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (condition()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
  assert.fail('timed out');
};

test('a keyboard choice the page turns busy for is not dropped silently: the select shows the saved mark and says why', async t => {
  const h = await markHarness(t);
  h.w.TimetableProgrammeMark.settleMs = 5;
  card(h, 10).click();
  h.select().focus();
  h.choose('AI2', { keyed: true });
  h.w.TW.plannerJob = { job_id: 'j', status: 'running' };  // busy before the keys rest
  await new Promise(resolve => setTimeout(resolve, 20));
  await settle();
  assert.equal(h.seen.posts.length, 0);
  assert.equal(h.select().value, '', 'the saved mark again');
  assert.ok(h.notices.some(n => n.type === 'warning' && /not saved/.test(n.text)), JSON.stringify(h.notices));
});

test('a redraw while a keyboard choice settles keeps the choice, and the select shows what is being saved', async t => {
  const post = deferred();
  const h = await markHarness(t, { hold: { post: () => post.promise } });
  h.w.TimetableProgrammeMark.settleMs = 30;
  card(h, 10).click();
  const quality0 = h.seen.quality;
  h.select().focus();
  h.choose('AI2', { keyed: true });
  h.w.TW.dataVersion++;  // an answer arrives: the panel is drawn anew (the mix turns stale)
  h.w.renderRightPanel('selection');
  assert.match(h.body().textContent, /From before your last change/, 'it was drawn anew');
  assert.equal(h.select().value, 'AI2', 'the choice is still shown');
  await waitTimed(() => h.seen.posts.length === 1);
  await settle();
  assert.deepEqual([h.select().value, h.select().disabled], ['AI2', true], 'saving shows the mark being saved');
  h.w.TW.qualityLoading = !h.w.TW.qualityLoading;  // another answer while it saves (the select no longer has the keyboard)
  h.w.TW.dataVersion++;
  h.w.renderRightPanel('selection');
  assert.deepEqual([h.select().value, h.select().disabled], ['AI2', true], 'still the mark being saved');
  post.resolve();
  await wait(() => h.seen.quality === quality0 + 1 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
  assert.equal(h.seen.posts[0].preferred_programme, 'AI2');
});

test('each arrow key restarts the wait; a pointer choice saves at once', async t => {
  const h = await markHarness(t);
  h.w.TimetableProgrammeMark.settleMs = 60;
  card(h, 10).click();
  const quality0 = h.seen.quality;
  h.select().focus();
  h.choose('AI', { keyed: true });
  await new Promise(resolve => setTimeout(resolve, 40));
  h.choose('AI2', { keyed: true });  // before the first wait ends
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.equal(h.seen.posts.length, 0, 'the first key’s wait was restarted');
  await waitTimed(() => h.seen.posts.length === 1 && !h.select().disabled);
  await wait(() => h.seen.quality === quality0 + 1 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
  h.w.TimetableProgrammeMark.settleMs = 60000;
  h.choose('AI', { keyed: true });
  h.select().dispatchEvent(new h.w.Event('pointerdown', { bubbles: true }));  // then the mouse
  h.choose('');
  await wait(() => h.seen.posts.length === 2);
  assert.deepEqual(h.seen.posts.map(b => b.preferred_programme), ['AI2', '']);
  await wait(() => h.seen.quality === quality0 + 2 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
});

test('a lens read joins only a read about the same scenario and data', async t => {
  const held = deferred();
  const h = await markHarness(t, { lens: n => LENS({ 110: { actual_total: n, actual_plans: { AI: n } } }), hold: { lens: n => (n === 2 ? held.promise : null) } });
  const first = h.w.loadPlanLens();  // read 2, held
  assert.equal(h.w.TW.planLens.loading, true);
  const joined = h.w.loadPlanLens();
  assert.equal(h.seen.lens, 2, 'the same data: one read');
  h.w.TW.scenarioId = 2;  // another scenario, the same data version
  await h.w.loadPlanLens();
  assert.equal(h.seen.lens, 3, 'a read for another scenario is its own');
  held.resolve();
  await Promise.all([first, joined]);
});

test('a reset (the scenario reloaded after a planner job) forgets the read in flight', async t => {
  const held = deferred();
  const h = await markHarness(t, { lens: n => LENS({ 110: { actual_total: n, actual_plans: { AI: n } } }), hold: { lens: n => (n === 2 ? held.promise : null) } });
  const old = h.w.loadPlanLens();  // read 2, held; the data version does not move on a reload
  h.w.resetPlanLens();
  const fresh = h.w.loadPlanLens();
  assert.equal(h.seen.lens, 3, 'a new read, never the old one');
  await fresh;
  held.resolve();
  await old;
  assert.equal(h.w.TW.planLens.sections['110'].actual_total, 3, 'the forgotten read does not land');
});

test('an older read finishing never clears the newer read’s loading', async t => {
  const older = deferred(), newer = deferred();
  const h = await markHarness(t, { hold: { lens: n => (n === 2 ? older.promise : n === 3 ? newer.promise : null) } });
  const a = h.w.loadPlanLens();
  h.w.TW.dataVersion++;
  const b = h.w.loadPlanLens();
  older.resolve();
  await a;
  assert.equal(h.w.TW.planLens.loading, true, 'the newer read is still loading');
  newer.resolve();
  await b;
  assert.equal(h.w.TW.planLens.loading, false);
});

test('a select drawn again while the keys move carries on the same wait: one save, the last choice', async t => {
  const h = await markHarness(t);
  h.w.TimetableProgrammeMark.settleMs = 50;
  card(h, 10).click();
  const quality0 = h.seen.quality;
  h.select().focus();
  h.choose('AI', { keyed: true });
  h.w.TW.dataVersion++;  // an answer: the panel is drawn anew, a new select
  h.w.renderRightPanel('selection');
  h.choose('AI2', { keyed: true });  // the keys go on, on the new select
  await new Promise(resolve => setTimeout(resolve, 120));
  await waitTimed(() => h.seen.posts.length >= 1 && !h.select().disabled);
  await waitTimed(() => h.seen.quality === quality0 + 1 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
  assert.deepEqual(h.seen.posts.map(b => b.preferred_programme), ['AI2'], 'no intermediate save');
});

test('a select taken off the page as the browser blurs it is not "leaving": its wait still ends in one save', async t => {
  const h = await markHarness(t);
  h.w.TimetableProgrammeMark.settleMs = 40;
  card(h, 10).click();
  const quality0 = h.seen.quality;
  h.select().focus();
  h.choose('AI2', { keyed: true });
  // What Chromium does when a redraw removes the focused select: focusout as it goes.
  const old = h.select();
  old.dispatchEvent(new h.w.FocusEvent('focusout', { bubbles: true }));
  old.remove();
  await settle();
  assert.equal(h.seen.posts.length, 0, 'not saved in the middle of a redraw');
  await waitTimed(() => h.seen.posts.length === 1);
  assert.equal(h.seen.posts[0].preferred_programme, 'AI2');
  h.w.renderRightPanel('selection');
  await waitTimed(() => h.seen.quality === quality0 + 1 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
});

test('a settled choice is saved once: leaving after it, or a pointer choice over a waiting key, saves nothing more', async t => {
  const h = await markHarness(t);
  h.w.TimetableProgrammeMark.settleMs = 20;
  card(h, 10).click();
  const quality0 = h.seen.quality;
  h.select().focus();
  h.choose('AI2', { keyed: true });
  await waitTimed(() => h.seen.posts.length === 1 && !h.select().disabled);
  await waitTimed(() => h.seen.quality === quality0 + 1 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
  h.select().dispatchEvent(new h.w.FocusEvent('focusout', { bubbles: true }));  // leaving after the save
  await new Promise(resolve => setTimeout(resolve, 40));
  assert.equal(h.seen.posts.length, 1, 'leaving after a settled save saves nothing again');
  h.choose('AI', { keyed: true });  // a key waits ...
  h.select().dispatchEvent(new h.w.Event('pointerdown', { bubbles: true }));
  h.choose('');  // ... and the mouse chooses before it rests
  await waitTimed(() => h.seen.posts.length === 2 && !h.select().disabled);
  await new Promise(resolve => setTimeout(resolve, 60));
  assert.deepEqual(h.seen.posts.map(b => b.preferred_programme), ['AI2', ''], 'the waiting key never lands');
  await waitTimed(() => h.seen.quality === quality0 + 2 && h.w.TW.qualityVersion === h.w.TW.dataVersion);
});

/* ── Keyboard: grid and list semantics, roving focus (design record §7.3, 5a) ── */
const keyOn = (h, key, init = {}) => {
  const event = new h.w.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init });
  h.d.activeElement.dispatchEvent(event);
  return event;
};
const strip = text => String(text || '').replace(/[\u2066\u2069]/g, '');

test('W1 the board is a grid: named tables, a header row, day row headers, cells and cards, one tab stop', async t => {
  const h = await modelHarness(t, { placements: [at(10)] });
  await h.select();
  const grids = [...h.d.querySelectorAll('#twGrid [role="grid"]')];
  assert.deepEqual(grids.map(g => g.getAttribute('aria-label')), ['Lectures']);
  assert.equal(grids[0].getAttribute('aria-describedby'), 'twGridKeys');
  const header = grids[0].querySelector('[role="row"]');
  const columns = [...header.querySelectorAll('[role="columnheader"]')];
  assert.equal(columns[0].textContent, 'Day');
  assert.equal(strip(columns[1].getAttribute('aria-label')), 'Slot 1, 09:00–10:15');
  assert.deepEqual([...grids[0].querySelectorAll('[role="rowheader"]')].map(r => r.getAttribute('aria-label')), ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday']);
  const cells = [...grids[0].querySelectorAll('[role="gridcell"]')];
  assert.equal(cells.length, 5);
  assert.ok(cells.every(cell => cell.dataset.uxCol === '540'));
  const c = card(h, 10);
  assert.deepEqual([c.getAttribute('role'), c.dataset.focusKey], ['button', 'placement:10']);
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')], [c], 'one tab stop: the card');
  // Two section groups: only the first table points at the keys line.
  const two = await modelHarness(t, { placements: [at(10), at(11, { day: 'MON', section: 'S2', term_section_id: 211 })] });
  await two.select();
  assert.deepEqual([...two.d.querySelectorAll('#twGrid [role="grid"]')].map(g => [g.getAttribute('aria-label'), g.getAttribute('aria-describedby')]),
    [['Section S1 · Lectures', 'twGridKeys'], ['Section S2 · Lectures', null]]);
  two.w.renderGrid();  // drawn again: still only the first
  assert.deepEqual([...two.d.querySelectorAll('#twGrid [role="grid"]')].map(g => g.getAttribute('aria-describedby')), ['twGridKeys', null]);
  const firstCells = [...two.d.querySelectorAll('#twGrid [role="grid"]')].map(g => g.querySelector('[role="gridcell"]').dataset.focusKey);
  assert.deepEqual(firstCells, ['cell:S1:lecture|SUN|09:00', 'cell:S2:lecture|SUN|09:00'], 'a cell key names its group');
  // A card names its own day and time.
  assert.match(c.querySelector('.visually-hidden').textContent, /^Sunday \u206609:00–10:15\u2069$/);
});

test('W2 in Arabic the tables, slots and corner are named in Arabic with the times isolated', async t => {
  const h = await harness(t, url => url === '/ops/tw/boards/2/' ? { placements: [at(10)], slot_config: scenario.slot_config }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms : undefined, {}, true);
  await h.select();
  const grid = h.d.querySelector('#twGrid [role="grid"]');
  assert.equal(grid.getAttribute('aria-label'), 'المحاضرات');
  const columns = [...grid.querySelectorAll('[role="columnheader"]')];
  assert.equal(columns[0].textContent, 'اليوم');
  assert.equal(columns[1].getAttribute('aria-label'), 'الفترة 1، \u206609:00–10:15\u2069');
});

test('W3 the keyboard stays on its card through tones and a redraw that moves it; a stack says where each card is', async t => {
  const h = await modelHarness(t, { placements: [at(10)] });
  await h.select();
  card(h, 10).focus();
  const node = card(h, 10);
  h.w.applyCardTones();
  assert.equal(card(h, 10), node, 'tones change in place');
  assert.equal(h.d.activeElement, node);
  h.w.TW.placements = [at(10, { day: 'MON' }), at(11), at(12)];
  h.w.renderGrid();
  assert.equal(h.d.activeElement.dataset.focusKey, 'placement:10', 'the same section has the keyboard');
  assert.equal(h.d.activeElement.closest('[role="gridcell"]').dataset.day, 'MON');
  assert.deepEqual([card(h, 11), card(h, 12)].map(c => c.textContent.match(/\((\d) of (\d) at this time\)/)?.slice(1)), [['1', '2'], ['2', '2']]);
});

test('W4 Enter selects the card and says so outside the inspector; Escape clears, then lets the key pass', async t => {
  const h = await modelHarness(t, { placements: [at(10)] });
  await h.select();
  h.d.getElementById('twWorkspace')?.setAttribute('data-right', 'collapsed');
  card(h, 10).focus();
  keyOn(h, 'Enter');
  assert.equal(h.w.TW.selectedPlacementId, 10);
  assert.ok(h.d.getElementById('twTabSelection').classList.contains('active'));
  assert.equal(h.d.activeElement, card(h, 10), 'the keyboard stays on the card');
  assert.match(strip(h.d.getElementById('twBoardStatus').textContent), /AI10 S1 selected/);
  assert.equal(h.d.getElementById('twInspectorStatus')?.textContent || '', '', 'not said inside the inspector');
  assert.equal(keyOn(h, 'Escape').defaultPrevented, true);
  assert.equal(h.w.TW.selectedPlacementId, null);
  assert.equal(h.d.getElementById('twBoardStatus').textContent, 'Selection cleared.');
  assert.equal(h.d.getElementById('twRightBody').textContent.trim(), 'Select a section on the grid to inspect it.');
  assert.equal(keyOn(h, 'Escape').defaultPrevented, false, 'nothing to clear: the key passes');
});

const budgetRows = [
  { course_code: 'CS101', course_key: 'CS101', programme_term: 3, remaining_sections: 0, used_sections: 2, planned_sections: 2, total_demand: 60 },
  { course_code: 'CS323', course_key: 'CS323', programme_term: 3, remaining_sections: 2, used_sections: 0, planned_sections: 2, total_demand: 70 },
  { course_code: 'ENG101', course_key: 'ENG101', programme_term: 4, remaining_sections: 1, used_sections: 0, planned_sections: 1, total_demand: 30 },
];
function listHarness(t, { budget = () => budgetRows, unplaced = () => [] } = {}) {
  return harness(t, url => url.includes('/budget/') ? { budget: budget() }
    : url.includes('/unplaced/') ? { unplaced: unplaced() }
    : url === '/ops/tw/boards/2/' ? { placements: [at(10)], slot_config: scenario.slot_config }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms : undefined);
}

test('W5 the list is one grid per term with one tab stop; a placed course is reachable; Enter on English opens its pattern', async t => {
  const h = await listHarness(t);
  await h.select();
  await wait(() => h.d.querySelectorAll('#twSectionList [role="grid"]').length === 2);
  const grids = [...h.d.querySelectorAll('#twSectionList [role="grid"]')];
  assert.deepEqual(grids.map(g => g.getAttribute('aria-labelledby')), ['twTerm-3', 'twTerm-4']);
  assert.equal(grids[0].getAttribute('aria-describedby'), 'twListKeys');
  assert.equal(h.d.querySelectorAll('#twSectionList [tabindex="0"]').length, 1);
  const done = h.d.querySelector('#twSectionList [data-focus-key="place:CS101|complete"]');
  assert.equal(done.getAttribute('aria-disabled'), 'true');
  done.focus();
  keyOn(h, 'ArrowDown');
  assert.equal(h.d.activeElement.dataset.focusKey, 'place:CS323|S1');
  const calls = [];
  h.w.chooseEnglishWorkspacePattern = item => calls.push(item.course_code);
  const english = h.d.querySelector('#twSectionList [data-focus-key="place:ENG101|S1"]');
  assert.equal(english.querySelector('.tw-pattern-action').tabIndex, -1, 'the button is the pointer’s; the row is the keyboard’s');
  english.focus();
  keyOn(h, 'Enter');
  await settle();
  assert.deepEqual(calls, ['ENG101']);
  const u = await listHarness(t, { budget: () => [], unplaced: () => [{ term_section_id: 77, course_code: 'ENG101', section: 'S1', meetings: [] }] });
  await u.select();
  await wait(() => u.d.querySelector('#twSectionList [data-focus-key="ts:77"]'));
  assert.equal(u.d.querySelector('#twSectionList [data-focus-key="ts:77"] .tw-pattern-action').tabIndex, -1);
});

test('W6 the list keeps its place when a row goes, and Show more goes on at the first new row', async t => {
  let used = 0;
  const h = await listHarness(t, { budget: () => budgetRows.map(r => r.course_code === 'CS323' ? { ...r, used_sections: used, remaining_sections: 2 - used } : r) });
  await h.select();
  await wait(() => h.d.querySelector('#twSectionList [data-focus-key="place:CS323|S1"]'));
  h.d.querySelector('#twSectionList [data-focus-key="place:CS323|S1"]').focus();
  used = 1;
  await h.w.loadBudget();
  assert.equal(h.d.activeElement.dataset.focusKey, 'place:CS323|S2', 'the row now at that place');
  const many = Array.from({ length: 125 }, (_, i) => ({ term_section_id: 1000 + i, course_code: `X${i}`, section: 'S1', meetings: [] }));
  const u = await listHarness(t, { budget: () => [], unplaced: () => many });
  await u.select();
  await wait(() => u.d.querySelector('.tw-show-more'));
  const more = u.d.querySelector('.tw-show-more');
  more.focus();
  more.click();
  assert.equal(u.d.activeElement.dataset.focusKey, 'ts:1100');
});

test('W7 an Issues row locates without moving the keyboard: the board’s tab stop moves and the place is said', async t => {
  const h = await modelHarness(t, { placements: [at(10), at(11, { day: 'MON', room: 'UNASSIGNED', room_state: 'unassigned' })] });
  await h.select();
  h.w.Element.prototype.scrollIntoView = () => {};  // jsdom has none
  h.w.renderRightPanel('issues');
  const row = h.d.querySelector('#twRightBody .tw-issue-row[data-ids]');
  row.focus();
  row.click();
  await settle();
  assert.equal(h.d.activeElement, row, 'the keyboard stays on the row');
  assert.equal(card(h, 11).getAttribute('tabindex'), '0', 'Tab into the board reaches it');
  assert.match(strip(h.d.getElementById('twBoardStatus').textContent), /^Located: Mon 09:00–10:15, AI11 S1\./);
  const ar = await harness(t, url => url === '/ops/tw/boards/2/' ? { placements: [at(10), at(11, { day: 'MON', room: 'UNASSIGNED', room_state: 'unassigned' })], slot_config: scenario.slot_config }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms : undefined, {}, true);
  await ar.select();
  ar.w.Element.prototype.scrollIntoView = () => {};
  ar.w.renderRightPanel('issues');
  ar.d.querySelector('#twRightBody .tw-issue-row[data-ids]').click();
  await settle();
  assert.match(ar.d.getElementById('twBoardStatus').textContent, /^عُثر عليها: الاثنين \u206609:00–10:15\u2069، \u2066AI11 S1\u2069\./);
});

test('W8 busy is a progress cursor, never aria-busy on the workspace', async t => {
  const held = deferred();
  const h = await harness(t, url => url.endsWith('/lock/') ? held.promise.then(() => ({ placement_id: 10, is_locked: true })) : undefined);
  await h.select();
  const workspace = h.d.querySelector('.tw-workspace');
  const locking = h.w.toggleLock(10);
  await settle();
  assert.equal(workspace.dataset.busy, 'true');
  assert.equal(workspace.hasAttribute('aria-busy'), false);
  held.resolve();
  await locking;
  assert.equal(workspace.hasAttribute('aria-busy'), false);
});

test('W9 another board: the keyboard starts at its first stop, not where it was on the last board', async t => {
  const other = { ...board, id: 3, label: 'Term 8', nominal_term: 8 };
  const h = await harness(t, url => url.startsWith('/ops/tw/boards/?') ? { boards: [board, other] }
    : url === '/ops/tw/boards/2/' ? { placements: [at(10), at(11, { day: 'THU' })], slot_config: scenario.slot_config }
    : url === '/ops/tw/boards/3/' ? { placements: [at(20, { day: 'SUN' }), at(21, { day: 'THU' })], slot_config: scenario.slot_config }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms : undefined);
  await h.select();
  card(h, 11).focus();  // Thursday on the first board
  await h.w.selectBoard(3);
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')].map(e => e.dataset.focusKey), ['placement:20'], 'the new board’s first stop');
});

test('W10 Enter on an English row the page cannot act on says why; after the chooser the keyboard is back on the list', async t => {
  const h = await listHarness(t, { budget: () => budgetRows });
  await h.select();
  await wait(() => h.d.querySelector('#twSectionList [data-focus-key="place:ENG101|S1"]'));
  let resolve;
  h.w.chooseEnglishWorkspacePattern = () => new Promise(r => { resolve = r; });
  const row = () => h.d.querySelector('#twSectionList [data-focus-key="place:ENG101|S1"]');
  row().focus();
  keyOn(h, 'Enter');
  h.d.activeElement.blur();  // the chooser had the keyboard; the list was drawn again
  resolve(null);
  await settle(); await settle();
  assert.equal(h.d.activeElement.dataset.focusKey, 'place:ENG101|S1', 'the keyboard is back on the row');
  h.w.TW.scenario = { ...h.w.TW.scenario, status: 'published' };
  row().focus();
  keyOn(h, 'Enter');
  assert.equal(h.d.getElementById('twBoardStatus').textContent, 'This scenario is published and read-only.');
});

test('W11 Enter on an English row of the unplaced list opens its pattern; the keyboard stays where the user put it', async t => {
  const u = await listHarness(t, { budget: () => [], unplaced: () => [{ term_section_id: 77, course_code: 'ENG101', section: 'S1', meetings: [] }] });
  await u.select();
  await wait(() => u.d.querySelector('#twSectionList [data-focus-key="ts:77"]'));
  let resolve;
  const calls = [];
  u.w.chooseEnglishWorkspacePattern = item => { calls.push(item.term_section_id); return new Promise(r => { resolve = r; }); };
  u.d.querySelector('#twSectionList [data-focus-key="ts:77"]').focus();
  keyOn(u, 'Enter');
  assert.deepEqual(calls, [77]);
  u.d.getElementById('twSearch').focus();  // the user moved on meanwhile
  resolve(null);
  await settle(); await settle();
  assert.equal(u.d.activeElement.id, 'twSearch', 'not pulled back to the list');
});

/* ── Placing a planned section from the list (design record §7.3, 5b) ── */
function dropHarness(t, { arabic = false, refuse = null, refuseCode = 'FULL_SECTION_PATTERN_REQUIRED', budget = () => [] } = {}) {
  const posts = [];
  const seen = { budget: 0 };
  return harness(t, (url, opts) => {
    if (url === '/ops/tw/placements/create-planned/' && opts.method === 'POST') {
      posts.push(JSON.parse(opts.body));
      if (refuse) return { httpError: 400, error: { code: refuseCode, message: refuse } };
      return { placement: { id: 99, term_section_id: 199 }, validation: {} };
    }
    if (url.includes('/budget/')) { seen.budget++; return { budget: budget() }; }
    if (url === '/ops/tw/boards/2/') return { placements: [at(10)], slot_config: scenario.slot_config };
    if (url.includes('/conflicts/')) return quiet;
    if (url.includes('/quality/')) return noRooms;
    if (url.includes('/readiness/')) return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    return undefined;
  }, {}, arabic).then(h => Object.assign(h, { posts, seen }));
}
const dropOn = async (h, payload) => {
  const cell = h.d.querySelector('#twGrid [role="gridcell"][data-day="MON"]');
  await h.w.onDrop({ preventDefault() {}, currentTarget: cell, dataTransfer: { getData: () => JSON.stringify(payload) } });
};
const plannedPayload = extra => ({ type: 'create_planned', course_code: 'CS323', course_key: 'CS323::k', course_name: 'Compilers', section_label: 'S2', max_per_section: 40, ...extra });

test('J1 a one-meeting drop names the course: its key and its name go with it', async t => {
  const h = await dropHarness(t);
  await h.select();
  await dropOn(h, plannedPayload({ required_meetings_per_section: 1 }));
  assert.equal(h.posts.length, 1);
  assert.deepEqual([h.posts[0].course_key, h.posts[0].course_name], ['CS323::k', 'Compilers']);
});

test('J2 a drop of a course that meets several times a week sends nothing and says why, in both languages', async t => {
  for (const arabic of [false, true]) {
    const h = await dropHarness(t, { arabic });
    await h.select();
    await dropOn(h, plannedPayload({ required_meetings_per_section: 2 }));
    assert.equal(h.posts.length, 0, 'no request');
    const warning = h.notices.find(n => n.type === 'warning');
    assert.equal(warning?.text, arabic
      ? 'يُدرَّس \u2066CS323\u2069 مرتين أسبوعياً، وتُوضع لقاءاته معاً، والإفلات يضع لقاءً واحداً. لم يُوضع شيء. «عرض مقسّم» يضع نمطاً كاملاً.'
      : '\u2066CS323\u2069 meets 2 times a week, and its meetings are placed together; a drop places one. Nothing was placed. Split view places a whole pattern.');
  }
  // Arabic counts: few and many.
  const ar = await dropHarness(t, { arabic: true });
  await ar.select();
  await dropOn(ar, plannedPayload({ required_meetings_per_section: 3 }));
  await dropOn(ar, plannedPayload({ required_meetings_per_section: 11 }));
  const words = ar.notices.filter(n => n.type === 'warning').map(n => n.text);
  assert.match(words[0], /CS323\u2069 3 مرات أسبوعياً،/);
  assert.match(words[1], /CS323\u2069 11 مرةً أسبوعياً،/);
});

test('J3 the server’s "complete pattern" refusal reads the same, as a warning, never an error', async t => {
  const h = await dropHarness(t, { refuse: 'CS323 needs a complete 3-meeting section pattern.' });
  await h.select();
  await dropOn(h, plannedPayload({}));  // an older budget row: no meeting count
  assert.equal(h.posts.length, 1);
  assert.equal(h.notices.filter(n => n.type === 'error').length, 0);
  assert.match(h.notices.find(n => n.type === 'warning')?.text || '', /meets 3 times a week/);
  // Another "complete pattern" message, with no count: none is invented; the
  // budget is read again (the row came from an older one).
  const other = await dropHarness(t, { refuse: 'A complete planned section pattern must use one meeting per day.' });
  await other.select();
  const budgets = other.seen.budget;
  await dropOn(other, plannedPayload({}));
  await settle();
  const said = other.notices.find(n => n.type === 'warning')?.text || '';
  assert.match(said, /is placed with its whole weekly pattern/);
  assert.doesNotMatch(said, /times a week/);
  assert.equal(other.seen.budget, budgets + 1, 'the budget is read again');
  const ar = await dropHarness(t, { arabic: true, refuse: 'A complete planned section pattern must use one meeting per day.' });
  await ar.select();
  await dropOn(ar, plannedPayload({}));
  assert.equal(ar.notices.find(n => n.type === 'warning')?.text,
    'تُوضع \u2066CS323\u2069 بنمطها الأسبوعي كاملاً، والإفلات يضع لقاءً واحداً. لم يُوضع شيء. «عرض مقسّم» يضع نمطاً كاملاً.');
});

test('J5 any other refusal stays an error with the server’s words', async t => {
  const h = await dropHarness(t, { refuse: 'Pattern days are not on this board.', refuseCode: 'VALIDATION_PLANNED_PATTERN' });
  await h.select();
  await dropOn(h, plannedPayload({}));
  assert.deepEqual(h.notices.map(n => [n.type, n.text]), [['error', 'Pattern days are not on this board.']]);
});

test('J6 a row dragged from the list carries its meeting count: a multi-meeting drop sends nothing', async t => {
  const row = { course_code: 'CS323', course_key: 'CS323::K', programme_term: 3, planned_sections: 1, used_sections: 0, remaining_sections: 1, total_demand: 70, required_meetings_per_section: 3, used_section_labels: [] };
  const h = await dropHarness(t, { budget: () => [row] });
  await h.select();
  await wait(() => h.d.querySelector('#twSectionList [data-focus-key="place:CS323::K|S1"]'));
  const data = {};
  const dragstart = new h.w.Event('dragstart', { bubbles: true });
  dragstart.dataTransfer = { setData: (type, value) => { data[type] = value; }, effectAllowed: '' };
  h.d.querySelector('#twSectionList [data-focus-key="place:CS323::K|S1"]').dispatchEvent(dragstart);
  const payload = JSON.parse(data['text/plain']);
  assert.deepEqual([payload.required_meetings_per_section, payload.course_key], [3, 'CS323::K']);
  await dropOn(h, payload);
  assert.equal(h.posts.length, 0);
  // Enter on that row says the same, not "drag it".
  h.d.querySelector('#twSectionList [data-focus-key="place:CS323::K|S1"]').focus();
  keyOn(h, 'Enter');
  assert.equal(h.d.getElementById('twBoardStatus').textContent,
    '\u2066CS323\u2069 meets 3 times a week, and its meetings are placed together; a drop places one. Split view places a whole pattern.',
    'names the course; nothing was tried, so nothing "was placed"');
  // Two meetings a week is already more than a drop places.
  const two = await dropHarness(t, { budget: () => [{ ...row, required_meetings_per_section: 2 }] });
  await two.select();
  await wait(() => two.d.querySelector('#twSectionList [data-focus-key="place:CS323::K|S1"]'));
  two.d.querySelector('#twSectionList [data-focus-key="place:CS323::K|S1"]').focus();
  keyOn(two, 'Enter');
  assert.match(two.d.getElementById('twBoardStatus').textContent, /meets 2 times a week/);
});

test('J4 the list proposes the free section labels: with S1 and S3 placed, S2', async t => {
  const row = { course_code: 'CS323', course_key: 'CS323', programme_term: 3, planned_sections: 3, used_sections: 2, remaining_sections: 1, total_demand: 70 };
  const h = await dropHarness(t, { budget: () => [{ ...row, used_section_labels: ['S1', 'S3'] }] });
  await h.select();
  await wait(() => h.d.querySelector('#twSectionList [data-focus-key^="place:CS323|"]'));
  assert.deepEqual([...h.d.querySelectorAll('#twSectionList [data-focus-key^="place:CS323|"]')].map(e => e.dataset.focusKey), ['place:CS323|S2']);
  assert.match(h.d.querySelector('#twSectionList [data-focus-key="place:CS323|S2"]').textContent, /CS323 S2/);
  // Labels as the server may send them: spaced, lower case, another scheme.
  const spaced = await dropHarness(t, { budget: () => [{ ...row, remaining_sections: 2, used_section_labels: [' s2 ', 'M1'] }] });
  await spaced.select();
  await wait(() => spaced.d.querySelector('#twSectionList [data-focus-key^="place:CS323|"]'));
  assert.deepEqual([...spaced.d.querySelectorAll('#twSectionList [data-focus-key^="place:CS323|"]')].map(e => e.dataset.focusKey), ['place:CS323|S1', 'place:CS323|S3']);
  // An older server without the list: the first used labels count as used.
  const old = await dropHarness(t, { budget: () => [row] });
  await old.select();
  await wait(() => old.d.querySelector('#twSectionList [data-focus-key^="place:CS323|"]'));
  assert.deepEqual([...old.d.querySelectorAll('#twSectionList [data-focus-key^="place:CS323|"]')].map(e => e.dataset.focusKey), ['place:CS323|S3']);
});

/* ── Change room: R, Selection's button and the dialog (design record §7.3, 5c) ──
   The page reads the rooms at the meeting's own time and sends the room
   alone, so a time lock holds. A room that fits can be chosen even while
   another section holds it (the clash is saved, and shown); one that does not
   fit is a row with its reason. The keyboard goes back to whatever answers to
   the opener's key when the dialog closes. */
const ROOM_TARGET = { day: 'SUN', start: '09:00', end: '10:15', required_capacity: 30, required_type: 'lecture', required_gender: 'M', is_online: false };
const rc = (code, extra = {}) => ({ room_code: code, room_type: 'lecture', capacity: 40, section: 'M', building: 'B1', wing: 'W1', floor: 1,
  available: true, tone: 'clean', fits_type: true, fits_gender: true, fits_capacity: true, department_fit: true, occupied_by: [],
  validation: { critical_count: 0, warning_count: 0, room_clashes: [] }, ...extra });
const heldOn = (board, extra = {}) => ({ placement_id: 60 + board, board_id: board, board_label: `Term ${board}`, term_section_id: 600 + board,
  section: `CS10${board}-S1`, day: 'SUN', start: '09:00', end: '10:15', ...extra });
// In the server's order. R9 is held on this board, R8 on three others (two
// classes: CS103 is on boards 3 and 5); R7 is free but outside the board's
// programme rooms; R3, R4 and R5 miss on type, gender and seats.
const ROOM_SET = [
  rc('R1'), rc('LAB25'), rc('R2', { building: 'North' }),
  rc('R9', { available: false, tone: 'block', occupied_by: [heldOn(2)] }),
  rc('R8', { available: false, tone: 'block', occupied_by: [heldOn(3), heldOn(5, { term_section_id: 603, section: 'CS103-S1' }), heldOn(4, { term_section_id: 700 })] }),
  rc('R7', { tone: 'warn', department_fit: false }),
  rc('R3', { available: false, tone: 'block', fits_type: false, room_type: 'lab' }),
  rc('R4', { available: false, tone: 'block', fits_gender: false, section: 'F' }),
  rc('R5', { available: false, tone: 'block', fits_capacity: false, capacity: 10 }),
];
const roomsAnswer = (candidates = ROOM_SET, { target = {}, summary = {} } = {}) => ({ ok: true, placement: { id: 10, board_id: 2, current_room: 'LAB25' },
  target: { ...ROOM_TARGET, ...target }, candidates, summary: { total: candidates.length, ...summary } });
// CS323 S1 on Sunday 09:00, its time locked, in LAB25.
const roomRow = extra => at(10, { course_code: 'CS323', room: 'LAB25', board_id: 2, is_locked: true, ...extra });
function roomHarness(t, { rows = () => [roomRow()], rooms = () => roomsAnswer(), move = null, more = () => undefined, arabic = false, extra = {} } = {}) {
  const posts = [], reads = [];
  const saved = new Map();
  return harness(t, (url, opts) => {
    const own = more(url, opts);
    if (own !== undefined) return own;
    if (url.includes('/room-candidates/')) { reads.push(url); return rooms(reads.length); }
    const moving = /^\/ops\/tw\/placements\/(\d+)\/move\/$/.exec(url);
    if (moving && opts.method === 'POST') {
      const body = JSON.parse(opts.body);
      posts.push(body);
      if (move) return move(body, posts.length);
      saved.set(Number(moving[1]), body.room);
      const row = { ...rows().find(r => r.id === Number(moving[1])), room: body.room };
      return { ok: true, placement: row, placements: [row], validation: { critical_count: 0 } };
    }
    if (url === '/ops/tw/boards/2/') return { placements: rows().map(row => (saved.has(row.id) ? { ...row, room: saved.get(row.id) } : row)), slot_config: scenario.slot_config };
    if (url.includes('/conflicts/')) return quiet;
    if (url.includes('/quality/')) return noRooms;
    if (url.includes('/readiness/')) return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    return undefined;
  }, extra, arabic).then(h => Object.assign(h, { posts, reads }));
}
const roomDialogEl = h => h.d.getElementById('twRoomDialog');
const roomRadios = h => [...h.d.querySelectorAll('#twRoomBody input[name="twRoomChoice"]')];
const roomRadio = (h, code) => roomRadios(h).find(radio => radio.value === code);
const roomNote = h => [h.d.getElementById('twRoomAssign').disabled, h.d.getElementById('twRoomNote').textContent];
// The page's objects are of another realm: compare their content.
const plainOf = value => JSON.parse(JSON.stringify(value));
// R on the card (a letter as the keyboard gives it: lower case).
const roomByKey = (h, id = 10) => { card(h, id).focus(); return keyOn(h, 'r'); };
const settled = h => wait(() => roomRadios(h).length > 0);

test('RC1 a locked meeting changes room by its room alone: no slot is read, {room} is sent, and the change is undoable', async t => {
  const meetings = [{ day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'LAB25' }, { day: 'TUE', start_time: '09:00', end_time: '10:15', room: 'LAB25' }];
  const h = await roomHarness(t, { rows: () => [roomRow({ meetings })] });
  await h.select();
  assert.equal(roomByKey(h).defaultPrevented, true);
  await settled(h);
  assert.deepEqual(h.reads, ['/ops/tw/placements/10/room-candidates/'], 'the server’s own time: no slot parameters');
  assert.equal(h.d.getElementById('twRoomTitle').textContent, 'Change room · CS323 S1');
  assert.equal(h.d.getElementById('twRoomSub').textContent,
    "Sunday 09:00–10:15 · this meeting only; the section's other meetings keep their rooms. · Time locked; its room can change");
  assert.equal(roomRadio(h, 'LAB25').checked, true, 'the current room, chosen at first');
  roomRadio(h, 'R2').click();
  h.w.TW.redoStack.push({ type: 'move', placement_id: 10 });
  const before = h.requests.length;
  h.d.getElementById('twRoomAssign').click();
  await wait(() => h.notices.some(n => n.type === 'success'));
  assert.deepEqual(h.posts, [{ room: 'R2' }], 'the room alone: no time, no override');
  assert.deepEqual(plainOf(h.w.TW.undoStack.at(-1)), { type: 'room', placement_id: 10, old_room: 'LAB25', new_room: 'R2' });
  assert.equal(h.w.TW.redoStack.length, 0);
  const after = h.requests.slice(before).map(r => `${r.opts.method || 'GET'} ${r.url}`);
  assert.ok(after.lastIndexOf('GET /ops/tw/boards/2/') > after.indexOf('POST /ops/tw/placements/10/move/'), after.join('\n'));
  assert.equal(roomDialogEl(h).hasAttribute('open'), false);
  assert.deepEqual(h.notices.at(-1), { type: 'success', text: 'Room \u2066R2\u2069 assigned.' });
  assert.equal(card(h, 10).querySelector('.tw-card-room').textContent, 'R2', 'the board was read again');
  assert.deepEqual(h.errors, []);
  // The same class on another board changed with it (an older server listed
  // that copy as an occupant): said once, and never as a class using the room.
  const copy = { ...heldOn(3), placement_id: 31, term_section_id: 110, section: 'CS323-S1' };
  const copies = await roomHarness(t, {
    rooms: () => roomsAnswer([rc('R6', { available: false, occupied_by: [copy, heldOn(4, { term_section_id: 700 })] })]),
    move: body => ({ ok: true, placement: roomRow({ room: body.room }), placements: [roomRow({ room: body.room }), roomRow({ id: 31, board_id: 3, room: body.room })] }),
  });
  await copies.select();
  roomByKey(copies);
  await settled(copies);
  roomRadio(copies, 'R6').click();
  copies.d.getElementById('twRoomAssign').click();
  await wait(() => copies.notices.some(n => n.type === 'warning'));
  assert.equal(copies.notices.at(-1).text,
    'Room \u2066R6\u2069 assigned; \u2066CS104-S1\u2069 also uses it at this time. It also changed on 1 other board.');
});

test('RC2 rooms are grouped by what choosing them does: free, outside the programme rooms (warning), held here (critical) or elsewhere (warning); a misfit is a row with its reason', async t => {
  // The time's own clashes (the same for every room) come with each candidate.
  const timed = room => ({ ...room, validation: { ...room.validation, critical_count: 2 } });
  const h = await roomHarness(t, { rooms: () => roomsAnswer(ROOM_SET.filter(r => !['LAB25', 'R2'].includes(r.room_code)).map(timed)) });
  await h.select();
  roomByKey(h);
  await settled(h);
  assert.equal(h.d.querySelector('#twRoomBody .tw-room-note').textContent, 'Whatever the room, its time keeps 2 clashes.');
  assert.deepEqual(roomRadios(h).map(r => [r.value, r.closest('.ux-choice').dataset.tone, r.closest('fieldset').dataset.group]),
    [['R1', 'ok', 'free'], ['R7', 'warning', 'outside'], ['R9', 'critical', 'inUse'], ['R8', 'warning', 'inUse']]);
  // Each row's tone mark is its verdict's.
  assert.deepEqual(roomRadios(h).map(r => r.closest('.ux-choice').querySelector('.ux-tone-icon').dataset.tone), ['ok', 'warning', 'critical', 'warning']);
  const said = (code, index) => h.d.getElementById(roomRadio(h, code).getAttribute('aria-describedby').split(' ')[index]).textContent;
  assert.deepEqual(['R1', 'R7', 'R9', 'R8'].map(code => said(code, 0)),
    ['Free and fits', "Lowers quality: Room unsuitable · outside this board's programme rooms", 'Blocks publishing: Room double-booked', 'Lowers quality: Room also booked on another board']);
  assert.equal(h.d.getElementById(roomRadio(h, 'R9').getAttribute('aria-labelledby')).textContent, 'R9');
  // A room outside the programme rooms is a row like the others: its code,
  // its verdict and its facts (no occupants).
  assert.equal(h.d.getElementById(roomRadio(h, 'R7').getAttribute('aria-labelledby')).textContent, 'R7');
  assert.equal(roomRadio(h, 'R7').getAttribute('aria-describedby').split(' ').length, 2);
  assert.match(said('R7', 1), /^Lecture room · 40 seats · 10 spare/);
  assert.equal(strip(said('R8', 2)), 'Used by CS103-S1 (Term 3) at 09:00–10:15; and 1 more');
  assert.equal(strip(said('R9', 2)), 'Used by CS102-S1 (Term 2) at 09:00–10:15');
  assert.deepEqual([...h.d.querySelectorAll('#twRoomChoices legend')].map(l => l.textContent),
    ['Free and fits (1)', "Free, but outside this board's programme rooms (1)", 'Fits, but in use at this time (2)']);
  // The groups that save an issue say which; free and fits saves none.
  assert.deepEqual([...h.d.querySelectorAll('#twRoomChoices fieldset')].map(set => set.getAttribute('aria-describedby')), [null, 'twRoomHint-outside', 'twRoomHint-inUse']);
  assert.equal(h.d.getElementById('twRoomHint-outside').textContent, 'Choosing one saves a “Room unsuitable” warning, which Issues then shows.');
  assert.equal(h.d.getElementById('twRoomHint-inUse').textContent, 'Choosing one saves a room clash, which Issues then shows.');
  // The misfits: rows with their reasons, never radios; closed while rooms fit.
  const misfits = [...h.d.querySelectorAll('#twRoomMisfits .ux-choice-static')];
  assert.deepEqual(misfits.map(row => row.querySelector('bdi').textContent), ['R3', 'R4', 'R5']);
  assert.match(misfits[0].textContent, /This meeting needs a lecture room/);
  assert.match(misfits[1].textContent, /This meeting needs a Male \(M\) room/);
  assert.match(misfits[2].textContent, /10 seats · 20 short/);
  assert.equal(h.d.querySelectorAll('#twRoomMisfits input').length, 0);
  assert.equal(h.d.getElementById('twRoomMisfits').hidden, true);
  assert.match(h.d.querySelector('[data-ux-disclosure="tw-room:misfits"]').textContent, /Rooms that do not fit \(3\)/);
  assert.equal(h.d.querySelector('[data-ux-disclosure="tw-room:misfits"]').getAttribute('aria-expanded'), 'false');
});

test('RC3 Assign is held until another room is chosen, and off with the reason while the page is busy, then on by itself', async t => {
  // The board read again with nothing written: busy while it is out, and
  // nothing the rooms were read from moved (a write is RC13's).
  let refresh = null;
  const h = await roomHarness(t, { more: url => (refresh && url === '/ops/tw/boards/2/' ? refresh.promise : undefined) });
  await h.select();
  roomByKey(h);
  await settled(h);
  assert.deepEqual(roomNote(h), [true, 'This is the current room.']);
  assert.equal(h.d.getElementById('twRoomAssign').hasAttribute('data-tw-hold'), true);
  roomRadio(h, 'R1').click();
  assert.deepEqual(roomNote(h), [false, '']);
  // The user's keyboard on another row (Ctrl+arrow moves it without choosing).
  roomRadio(h, 'LAB25').focus();
  // The page busy: off, saying why; on again by itself.
  refresh = deferred();
  const refreshing = h.w.selectBoard(2);
  await settle();
  assert.deepEqual(roomNote(h), [true, 'Wait for the current operation to finish.']);
  // A commit asked for all the same is answered inside the dialog: the
  // page's own busy guard would toast behind it, where nobody sees it.
  const toasts = h.notices.length;
  const alert = h.d.getElementById('twRoomAlert');
  h.d.getElementById('twRoomForm').requestSubmit();
  await settle();
  assert.equal(h.posts.length, 0);
  assert.deepEqual([alert.textContent, alert.hidden, alert.dataset.tone], ['Wait for the current operation to finish.', false, 'warning']);
  assert.deepEqual(h.notices.slice(toasts), []);
  refresh.resolve({ placements: [roomRow()], slot_config: scenario.slot_config });
  refresh = null;
  await refreshing;
  assert.deepEqual(roomNote(h), [false, '']);
  assert.deepEqual([alert.textContent, alert.hidden], ['', true], 'the hold’s words went with it');
  assert.equal(roomRadio(h, 'R1').checked, true, 'the choice survives the board read again');
  assert.equal(h.d.activeElement, roomRadio(h, 'LAB25'), 'the keyboard stays where the user put it');
  assert.equal(h.reads.length, 1, 'nothing was written: the rooms stand');
  // A hold that turns into another (busy, then published): its words go,
  // and the note says the one holding now.
  refresh = deferred();
  const again = h.w.selectBoard(2);
  await settle();
  h.d.getElementById('twRoomForm').requestSubmit();
  await settle();
  assert.deepEqual([alert.textContent, alert.hidden], ['Wait for the current operation to finish.', false]);
  h.w.TW.scenario.status = 'published';
  h.w.syncWorkspaceControls();
  assert.deepEqual([alert.hidden, roomNote(h)], [true, [true, 'This scenario is published and read-only.']]);
  refresh.resolve({ placements: [roomRow()], slot_config: scenario.slot_config });
  refresh = null;
  await again;
  // A room the list does not hold: nothing is chosen.
  const none = await roomHarness(t, { rows: () => [roomRow({ room: 'OLD9' })] });
  await none.select();
  roomByKey(none);
  await settled(none);
  assert.equal(roomRadios(none).some(r => r.checked), false);
  assert.deepEqual(roomNote(none), [true, 'Choose a room.']);
});

test('RC4 the keyboard goes back to whatever answers to the opener’s key at close: Selection’s button or a card, drawn again meanwhile', async t => {
  const h = await roomHarness(t);
  await h.select();
  card(h, 10).click();
  const button = h.d.querySelector('#twRightBody [data-tw-action="room"]');
  assert.deepEqual([button.textContent, button.getAttribute('aria-label'), button.getAttribute('aria-haspopup'), button.disabled], ['Change…', 'Change room', 'dialog', false]);
  button.focus();
  button.click();
  await settled(h);
  const rows = roomRadios(h);
  h.w.renderGrid();
  h.w.TW.placements[0].registered_count = 31;
  h.w.renderRightPanel('selection');
  const redrawn = h.d.querySelector('#twRightBody [data-tw-action="room"]');
  assert.notEqual(redrawn, button, 'Selection drew a new button');
  assert.deepEqual(roomRadios(h), rows, 'the dialog’s rows are the same nodes');
  assert.equal(roomDialogEl(h).hasAttribute('open'), true);
  roomDialogEl(h).querySelector('.ux-dialog-foot [data-dialog-close]').click();
  assert.equal(roomDialogEl(h).hasAttribute('open'), false);
  assert.equal(h.d.activeElement, redrawn, 'on the button drawn now');
  // Selection no longer shows the section: the inspector takes the keyboard.
  redrawn.click();
  await settled(h);
  h.w.TW.selectedPlacementId = null;
  h.w.renderRightPanel('selection');
  roomDialogEl(h).querySelector('.ux-dialog-head [data-dialog-close]').click();
  assert.equal(h.d.activeElement, h.d.getElementById('twRightBody'));
  // From a card drawn again while the dialog is open.
  roomByKey(h);
  await settled(h);
  const old = card(h, 10);
  h.w.renderGrid();
  assert.notEqual(card(h, 10), old);
  keyOn(h, 'Escape');
  assert.equal(roomDialogEl(h).hasAttribute('open'), false);
  assert.equal(h.d.activeElement, card(h, 10), 'on the card drawn now');
});

test('RC5 after an assignment the board’s tab stop is on the card: from a card the keyboard is on it again, from Selection on the button', async t => {
  const rows = () => [roomRow(), at(11, { day: 'MON', board_id: 2 })];
  const h = await roomHarness(t, { rows });
  await h.select();
  roomByKey(h);
  await settled(h);
  const old = card(h, 10);
  roomRadio(h, 'R2').click();
  roomRadio(h, 'R2').focus();
  keyOn(h, 'Enter');
  await wait(() => h.notices.some(n => n.type === 'success'));
  assert.notEqual(card(h, 10), old, 'the card was drawn again');
  assert.equal(h.d.activeElement, card(h, 10));
  // From Selection, while the board's tab stop is on another card.
  const s = await roomHarness(t, { rows });
  await s.select();
  card(s, 11).focus();
  card(s, 10).click();
  const button = s.d.querySelector('#twRightBody [data-tw-action="room"]');
  button.focus();
  button.click();
  await settled(s);
  roomRadio(s, 'R2').click();
  s.d.getElementById('twRoomAssign').click();
  await wait(() => s.notices.some(n => n.type === 'success'));
  assert.deepEqual([...s.d.querySelectorAll('#twGrid [tabindex="0"]')].map(e => e.dataset.focusKey), ['placement:10']);
  assert.equal(s.d.activeElement, s.d.querySelector('#twRightBody [data-tw-action="room"]'));
});

test('RC6 the gate: published, loading, a planner job, another scenario’s section, online, no room needed; a stale shared flag is said inside', async t => {
  const cases = [
    { name: 'published', extra: { status: 'published' }, button: true, words: 'This scenario is published and read-only.' },
    { name: 'loading', setup: h => { h.w.TW.loadedBoardId = 99; }, button: true, words: 'The board is still loading.' },
    { name: 'job', extra: { active_job: { job_id: 'job-1', status: 'running', scenario_id: 1 } }, button: true, words: 'A planner job is running; edits wait until it ends.' },
    { name: 'shared', row: { shared_with_other_scenario: true }, button: true, words: "Another scenario owns this section's time and room; change it there." },
    { name: 'online', row: { is_online: true, needs_room: false }, button: false, words: 'This section is online; it needs no room.' },
    { name: 'no room', row: { needs_room: false }, button: false, words: 'This section needs no room.' },
  ];
  for (const c of cases) {
    const h = await roomHarness(t, { rows: () => [roomRow(c.row || {})], extra: c.extra || {} });
    await h.select();
    c.setup?.(h);
    card(h, 10).click();
    const button = h.d.querySelector('#twRightBody [data-tw-action="room"]');
    if (!c.button) assert.equal(button, null, `${c.name}: not this section’s action`);
    else {
      assert.deepEqual([button.disabled, button.hasAttribute('data-tw-hold'), button.getAttribute('aria-describedby')], [true, true, 'twSelActionNote'], c.name);
      assert.deepEqual([h.d.getElementById('twSelActionNote').textContent, h.d.getElementById('twSelActionNote').hidden], [c.words, false], c.name);
    }
    const toasts = h.notices.length;
    assert.equal(roomByKey(h).defaultPrevented, true, c.name);
    assert.deepEqual(h.notices.slice(toasts), [{ type: 'warning', text: c.words }], c.name);
    assert.deepEqual(h.reads, [], `${c.name}: nothing read`);
    assert.equal(roomDialogEl(h).hasAttribute('open'), false, c.name);
  }
  // Selection's button follows the gate in place (a job starting), and R
  // waits while any dialog is open.
  const live = await roomHarness(t);
  await live.select();
  card(live, 10).click();
  const button = live.d.querySelector('#twRightBody [data-tw-action="room"]');
  assert.deepEqual([button.disabled, live.d.getElementById('twSelActionNote').hidden], [false, true]);
  live.w.TW.englishPicker = true;
  assert.equal(roomByKey(live).defaultPrevented, false, 'a dialog is open: the key is not the board’s');
  live.w.TW.englishPicker = false;
  live.w.dispatchEvent(new live.w.CustomEvent('tw:planner-job', { detail: { scenarioId: 1, job: { job_id: 'job-2', status: 'running' }, source: 'adapter' } }));
  assert.equal(button.disabled, true, 'the same button, held in place');
  assert.deepEqual([live.d.getElementById('twSelActionNote').textContent, live.d.getElementById('twSelActionNote').hidden],
    ['A planner job is running; edits wait until it ends.', false]);
  assert.deepEqual(live.reads, []);
  // The board did not know yet that another scenario owns it: the server's
  // refusal is said in the dialog, Assign stays held, nothing is undoable.
  let answer = deferred();
  const h = await roomHarness(t, { move: () => answer.promise });
  await h.select();
  roomByKey(h);
  await settled(h);
  roomRadio(h, 'R2').click();
  const toasts = h.notices.length;
  h.d.getElementById('twRoomAssign').focus();
  h.d.getElementById('twRoomAssign').click();
  await settle();
  // While the write is out: the keyboard on the chosen row (Assign turns
  // off), the lists busy, and Assign held saying what it is doing.
  assert.equal(h.d.activeElement, roomRadio(h, 'R2'));
  assert.deepEqual([...h.d.querySelectorAll('#twRoomChoices fieldset')].map(f => f.getAttribute('aria-busy')), ['true', 'true', 'true'], 'every group, outside the programme rooms too');
  assert.deepEqual([roomNote(h)[0], strip(roomNote(h)[1])], [true, 'Assigning room R2…']);
  assert.equal(strip(h.d.getElementById('twRoomStatus').textContent), 'Assigning room R2…');
  answer.resolve({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  await wait(() => !h.d.getElementById('twRoomAlert').hidden);
  await settle();
  assert.equal(h.d.getElementById('twRoomAlert').textContent, "Another scenario owns this section's time and room; change it there.");
  assert.equal(roomDialogEl(h).hasAttribute('open'), true);
  assert.equal(h.d.getElementById('twRoomAssign').disabled, true, 'held: asking again would change nothing');
  assert.deepEqual([...h.d.querySelectorAll('#twRoomChoices fieldset')].map(f => f.hasAttribute('aria-busy')), [false, false, false]);
  assert.equal(h.w.TW.undoStack.length, 0);
  assert.deepEqual(h.notices.slice(toasts), []);
  // Escape while the write is out closes the dialog; the refusal then comes as a toast.
  keyOn(h, 'Escape');
  roomByKey(h);
  await settled(h);
  assert.equal(h.d.getElementById('twRoomAlert').hidden, true, 'a new open starts clean');
  roomRadio(h, 'R2').click();
  answer = deferred();
  h.d.getElementById('twRoomAssign').click();
  await settle();
  keyOn(h, 'Escape');
  assert.equal(roomDialogEl(h).hasAttribute('open'), false);
  answer.resolve({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  await wait(() => h.notices.length > toasts);
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'warning', text: "Another scenario owns this section's time and room; change it there." }]);
  assert.equal(h.w.TW.undoStack.length, 0);
});

test('RC7 loading, a failed read and Retry, and a busy server: each says so, and Assign is held', async t => {
  const body = h => h.d.getElementById('twRoomBody').textContent;
  const reply = deferred();
  const slow = await roomHarness(t, { rooms: () => reply.promise });
  await slow.select();
  roomByKey(slow);
  await settle();
  assert.match(body(slow), /Finding rooms for this meeting…/);
  assert.equal(slow.d.getElementById('twRoomChoices').getAttribute('aria-busy'), 'true');
  assert.equal(slow.d.getElementById('twRoomStatus').textContent, 'Finding rooms for this meeting…');
  assert.equal(slow.d.getElementById('twRoomAssign').disabled, true);
  assert.equal(slow.d.activeElement, slow.d.getElementById('twRoomBody'));
  reply.resolve(roomsAnswer());
  await settled(slow);
  assert.equal(slow.d.activeElement, roomRadio(slow, 'LAB25'), 'the keyboard goes on to the chosen row');
  // A failure, then Retry: read again.
  const failing = await roomHarness(t, { rooms: n => (n === 1 ? { httpError: 500, error: { code: 'BOOM', message: 'Broken' } } : roomsAnswer()) });
  await failing.select();
  roomByKey(failing);
  await wait(() => failing.d.querySelector('#twRoomBody [data-tw-room-retry]'));
  assert.match(body(failing), /The rooms could not be read\./);
  assert.equal(failing.d.getElementById('twRoomAssign').disabled, true);
  assert.deepEqual(failing.notices.filter(n => n.type === 'error'), [], 'said in the dialog, not as a toast');
  failing.d.querySelector('#twRoomBody [data-tw-room-retry]').click();
  await settled(failing);
  assert.equal(failing.reads.length, 2);
  // A server that cannot answer now.
  const busy = await roomHarness(t, { rooms: () => ({ httpError: 503, error: { code: 'MAINTENANCE', message: 'Down' } }) });
  await busy.select();
  roomByKey(busy);
  await wait(() => busy.d.querySelector('#twRoomBody [data-tw-room-retry]'));
  assert.match(body(busy), /The server is busy; try again in a moment\./);
});

test('RC7 no rooms, none that fit, a cut list, a meeting online or with no room after all, and one that moved: each says so', async t => {
  const body = h => h.d.getElementById('twRoomBody').textContent;
  const opened = async rooms => {
    const h = await roomHarness(t, { rooms });
    await h.select();
    roomByKey(h);
    await wait(() => !h.d.querySelector('#twRoomBody [aria-busy]'));
    return h;
  };
  const empty = await opened(() => roomsAnswer([]));
  assert.match(body(empty), /No rooms are on record\./);
  assert.equal(empty.d.getElementById('twRoomFilter'), null);
  const misfitsOnly = await opened(() => roomsAnswer(ROOM_SET.filter(r => ['R3', 'R4', 'R5'].includes(r.room_code))));
  assert.match(body(misfitsOnly), /No room fits this meeting\./);
  assert.equal(misfitsOnly.d.getElementById('twRoomMisfits').hidden, false, 'nothing fits: the reasons are open');
  assert.equal(misfitsOnly.d.querySelector('[data-ux-disclosure="tw-room:misfits"]').getAttribute('aria-expanded'), 'true');
  assert.deepEqual(roomNote(misfitsOnly), [true, 'Choose a room.']);
  const cut = await opened(() => roomsAnswer(Array.from({ length: 60 }, (_, i) => rc(`X${i}`)), { summary: { total: 75 } }));
  assert.match(body(cut), /Showing the best 60 of 75 rooms\./);
  const whole = await opened(() => roomsAnswer());
  assert.doesNotMatch(body(whole), /Showing the best/);
  const online = await opened(() => roomsAnswer([], { target: { is_online: true, required_type: 'online' } }));
  assert.match(body(online), /This section is online; it needs no room\./);
  assert.equal(online.d.getElementById('twRoomAssign').disabled, true);
  const noRoom = await opened(() => roomsAnswer([], { target: { no_room_required: true } }));
  assert.match(body(noRoom), /This section needs no room\./);
  // The meeting moved since its rooms were read: Check again reads the board, then its rooms.
  const moved = await opened(n => roomsAnswer(ROOM_SET, { target: n === 1 ? { day: 'MON' } : {} }));
  assert.match(body(moved), /This meeting changed since its rooms were read\./);
  assert.equal(roomRadios(moved).length, 0);
  assert.equal(moved.d.getElementById('twRoomAssign').disabled, true);
  moved.d.getElementById('twRoomForm').requestSubmit();
  await settle();
  assert.equal(moved.posts.length, 0, 'nothing is sent for a meeting that moved');
  const boards = moved.requests.filter(r => r.url === '/ops/tw/boards/2/').length;
  moved.d.querySelector('#twRoomBody [data-tw-room-recheck]').click();
  await settled(moved);
  assert.equal(moved.requests.filter(r => r.url === '/ops/tw/boards/2/').length, boards + 1, 'the board, read again');
  assert.equal(moved.reads.length, 2);
  // Its start or its end alone moved: stale too.
  for (const target of [{ start: '09:30' }, { end: '10:45' }]) {
    const shifted = await opened(() => roomsAnswer(ROOM_SET, { target }));
    assert.match(body(shifted), /This meeting changed since its rooms were read\./, JSON.stringify(target));
    assert.deepEqual([roomRadios(shifted).length, shifted.d.getElementById('twRoomAssign').disabled], [0, true]);
  }
  // The meeting moves while the dialog is open, a room already chosen: the
  // board's answer makes the list stale, and nothing is sent.
  let day = 'SUN';
  const shifting = await roomHarness(t, { rows: () => [roomRow({ day })] });
  await shifting.select();
  roomByKey(shifting);
  await settled(shifting);
  roomRadio(shifting, 'R1').click();
  day = 'MON';
  await shifting.w.selectBoard(2);
  assert.match(body(shifting), /This meeting changed since its rooms were read\./);
  assert.equal(shifting.d.getElementById('twRoomAssign').disabled, true);
  shifting.d.getElementById('twRoomForm').requestSubmit();
  await settle();
  assert.equal(shifting.posts.length, 0);
});

test('RC8 Undo and Redo send the room alone, back and forth', async t => {
  const h = await roomHarness(t);
  await h.select();
  roomByKey(h);
  await settled(h);
  roomRadio(h, 'R2').click();
  h.d.getElementById('twRoomAssign').click();
  await wait(() => !h.d.getElementById('twUndo').disabled);
  h.d.getElementById('twUndo').click();
  await wait(() => h.w.TW.redoStack.length === 1 && !h.d.getElementById('twRedo').disabled);
  h.d.getElementById('twRedo').click();
  await wait(() => h.w.TW.undoStack.length === 1 && !h.d.getElementById('twUndo').disabled);
  assert.deepEqual(h.posts, [{ room: 'R2' }, { room: 'LAB25' }, { room: 'R2' }]);
  // A meeting with no room yet: Undo sends the placer's marker back as it
  // was, never the words a page shows for it ("No room", "بلا قاعة").
  for (const arabic of [false, true]) {
    const u = await roomHarness(t, { arabic, rows: () => [roomRow({ room: 'UNASSIGNED', room_state: 'unassigned' })] });
    await u.select();
    roomByKey(u);
    await settled(u);
    roomRadio(u, 'R2').click();
    u.d.getElementById('twRoomAssign').click();
    await wait(() => !u.d.getElementById('twUndo').disabled);
    assert.equal(u.w.TW.undoStack.at(-1).old_room, 'UNASSIGNED');
    u.d.getElementById('twUndo').click();
    await wait(() => u.posts.length === 2 && !u.d.getElementById('twRedo').disabled);
    assert.deepEqual(u.posts, [{ room: 'R2' }, { room: 'UNASSIGNED' }], arabic ? 'ar' : 'en');
  }
});

test('RC9 in Arabic: the day with its time isolated, seats counted in the right form, and the placer’s marker never shown', async t => {
  const h = await roomHarness(t, {
    arabic: true,
    rows: () => [roomRow({ room: 'UNASSIGNED', room_state: 'unassigned' })],
    rooms: () => roomsAnswer([rc('R1', { capacity: 3, validation: { critical_count: 1 } }), rc('R2', { capacity: 11 }), rc('R6', { capacity: 1 })], { target: { required_capacity: null } }),
  });
  await h.select();
  card(h, 10).click();
  const button = h.d.querySelector('#twRightBody [data-tw-action="room"]');
  assert.deepEqual([button.textContent, button.getAttribute('aria-label')], ['اختيار…', 'اختيار قاعة'], 'no room yet: choose one');
  roomByKey(h);
  await settled(h);
  assert.match(h.d.getElementById('twRoomSub').innerHTML, /^الأحد <bdi dir="ltr">09:00–10:15<\/bdi> · الوقت مقفل؛ القاعة قابلة للتغيير$/);
  assert.equal(h.d.getElementById('twRoomTitle').textContent, 'تغيير القاعة · CS323 S1');
  const facts = code => h.d.getElementById(roomRadio(h, code).getAttribute('aria-describedby').split(' ')[1]).textContent;
  assert.match(facts('R1'), /3 مقاعد/);
  assert.match(facts('R2'), /11 مقعداً/);
  assert.match(facts('R6'), /مقعد واحد/);
  assert.equal(h.d.querySelector('#twRoomBody .tw-room-current').textContent, 'القاعة الحالية: بلا قاعة');
  assert.doesNotMatch(roomDialogEl(h).textContent, /UNASSIGNED/);
  assert.equal(h.d.querySelector('#twRoomBody .tw-room-note').textContent, 'أيّاً كانت القاعة، يبقى في وقته تعارض واحد.');
  assert.deepEqual([...h.d.querySelectorAll('#twRoomChoices legend')].map(l => l.textContent), ['متاحة ومناسبة (3)']);
});

test('RC10 the filter hides rows in place; Enter takes the one row left, Escape clears it, and a hidden choice is no choice', async t => {
  const h = await roomHarness(t);
  await h.select();
  roomByKey(h);
  await settled(h);
  const field = h.d.getElementById('twRoomFilter');
  const nodes = roomRadios(h);
  const type = text => { field.focus(); field.value = text; field.dispatchEvent(new h.w.Event('input', { bubbles: true })); };
  const shown = () => roomRadios(h).filter(radio => !radio.closest('[hidden]')).map(radio => radio.value);
  roomRadio(h, 'R1').click();
  type('r8');
  assert.deepEqual(shown(), ['R8']);
  assert.deepEqual(roomRadios(h), nodes, 'hidden in place: nothing drawn again');
  assert.equal(roomRadio(h, 'R1').checked, false, 'a hidden choice is no choice');
  assert.deepEqual(roomNote(h), [true, 'Choose a room.']);
  const enter = keyOn(h, 'Enter');
  assert.equal(enter.defaultPrevented, true);
  assert.equal(roomRadio(h, 'R8').checked, true);
  assert.equal(h.d.activeElement, roomRadio(h, 'R8'));
  assert.deepEqual(roomNote(h), [false, '']);
  assert.equal(h.posts.length, 0, 'Enter in the filter never submits');
  type('north');
  assert.deepEqual(shown(), ['R2'], 'by building too');
  type('zzz');
  assert.equal(h.d.getElementById('twRoomNoMatch').hidden, false);
  const escape = keyOn(h, 'Escape');
  assert.equal(escape.defaultPrevented, true);
  assert.equal(field.value, '');
  assert.equal(roomDialogEl(h).hasAttribute('open'), true, 'the dialog stays open');
  assert.deepEqual(shown(), ['R1', 'LAB25', 'R2', 'R7', 'R9', 'R8']);
  assert.equal(h.d.getElementById('twRoomNoMatch').hidden, true);
  // Enter on a row commits.
  roomRadio(h, 'R8').click();
  roomRadio(h, 'R8').focus();
  keyOn(h, 'Enter');
  await wait(() => h.posts.length === 1);
  assert.deepEqual(h.posts, [{ room: 'R8' }]);
  await wait(() => h.notices.some(n => n.type === 'warning' && /also use it/.test(n.text)));
  assert.equal(h.notices.at(-1).text, 'Room \u2066R8\u2069 assigned; \u2066CS103-S1\u2069, \u2066CS104-S1\u2069 also use it at this time.');
});

test('RC11 one read per meeting and data version: a reopen joins it, in flight or answered; a write reads again', async t => {
  const first = deferred();
  const h = await roomHarness(t, { rooms: n => (n === 1 ? first.promise : roomsAnswer()), more: url => (url.endsWith('/lock/') ? { placement_id: 10, is_locked: false } : undefined) });
  await h.select();
  roomByKey(h);
  keyOn(h, 'Escape');
  assert.equal(roomDialogEl(h).hasAttribute('open'), false);
  roomByKey(h);
  assert.equal(h.reads.length, 1, 'a reopen joins the read in flight');
  first.resolve(roomsAnswer());
  await settled(h);
  keyOn(h, 'Escape');
  roomByKey(h);
  await settled(h);
  assert.equal(h.reads.length, 1, 'open, Esc, open: the answer is kept');
  keyOn(h, 'Escape');
  await h.w.toggleLock(10);
  roomByKey(h);
  await settled(h);
  assert.equal(h.reads.length, 2, 'after a write: read again');
  // The board read again with no write from this page (a planner job ended):
  // the kept answer goes.
  keyOn(h, 'Escape');
  await h.w.selectBoard(2);
  roomByKey(h);
  await settled(h);
  assert.equal(h.reads.length, 3, 'a board answer drops the kept one');
  // A read still out when a write lands is not the answer for the new data.
  const out = deferred();
  const w = await roomHarness(t, { rooms: n => (n === 1 ? out.promise : roomsAnswer()), more: url => (url.endsWith('/lock/') ? { placement_id: 10, is_locked: false } : undefined) });
  await w.select();
  roomByKey(w);
  keyOn(w, 'Escape');
  await w.w.toggleLock(10);
  roomByKey(w);
  assert.equal(w.reads.length, 2, 'the data changed while it was out: read again');
  out.resolve(roomsAnswer());
  await settled(w);
});

/* ── Change room, review round 1: what the first tests left unpinned ── */
const lockOk = url => (url.endsWith('/lock/') ? { placement_id: 10, is_locked: true } : undefined);
// The second read and on hold a room the first did not: R6.
const roomsThenR6 = n => roomsAnswer(n === 1 ? ROOM_SET : [...ROOM_SET, rc('R6')]);

test('RC12 a save the user left (Escape, then R again) ends inside the dialog open by then, never as a toast behind it, naming its meeting where that dialog is about another, and that dialog reads its rooms again', async t => {
  // The reviewer's steps: R, a row, Enter with the move held; Escape; R
  // again while it is out (D3b); then the move lands.
  let room = 'LAB25';
  const move = deferred();
  const h = await roomHarness(t, {
    rows: () => [roomRow({ room })],
    rooms: roomsThenR6,
    move: async body => {
      await move.promise;
      room = body.room;
      return { ok: true, placement: roomRow({ room }), placements: [roomRow({ room })] };
    },
  });
  await h.select();
  roomByKey(h);
  await settled(h);
  roomRadio(h, 'R1').click();
  roomRadio(h, 'R1').focus();
  keyOn(h, 'Enter');
  await settle();
  assert.deepEqual(h.posts, [{ room: 'R1' }]);
  keyOn(h, 'Escape');
  assert.equal(roomDialogEl(h).hasAttribute('open'), false);
  roomByKey(h);
  await settled(h);
  assert.equal(h.reads.length, 1, 'the reopen takes the answer kept for this data');
  assert.deepEqual(roomNote(h), [true, 'Wait for the current operation to finish.']);
  const toasts = h.notices.length;
  const alert = h.d.getElementById('twRoomAlert');
  move.resolve();
  await wait(() => !alert.hidden);
  assert.deepEqual(h.notices.slice(toasts), [], 'no toast behind the dialog');
  assert.deepEqual([alert.textContent, alert.dataset.tone], ['Room \u2066R1\u2069 assigned.', 'success']);
  assert.equal(roomDialogEl(h).hasAttribute('open'), true);
  assert.deepEqual(plainOf(h.w.TW.undoStack.at(-1)), { type: 'room', placement_id: 10, old_room: 'LAB25', new_room: 'R1' });
  // The open dialog does not keep the list it read before the save: read
  // again, the saved room is the current one, chosen, and Assign holds on it.
  assert.equal(h.reads.length, 2);
  assert.ok(roomRadio(h, 'R6'), 'the rooms as read after the save');
  assert.equal(h.d.querySelector('#twRoomBody .tw-room-current').textContent, 'Current room: R1');
  assert.deepEqual(roomRadios(h).filter(radio => radio.checked).map(radio => radio.value), ['R1']);
  assert.deepEqual(roomNote(h), [true, 'This is the current room.']);
  assert.equal(h.d.activeElement, roomRadio(h, 'R1'), 'the keyboard goes with the dialog’s own choice');
  // Refused instead, while the dialog about the same meeting is open: said
  // there, and Assign holds there too, whatever is chosen.
  const refused = deferred();
  const r = await roomHarness(t, { move: () => refused.promise });
  await r.select();
  roomByKey(r);
  await settled(r);
  roomRadio(r, 'R1').click();
  r.d.getElementById('twRoomAssign').click();
  await settle();
  keyOn(r, 'Escape');
  roomByKey(r);
  await settled(r);
  roomRadio(r, 'R2').click();
  const before = r.notices.length;
  refused.resolve({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  const line = r.d.getElementById('twRoomAlert');
  await wait(() => !line.hidden);
  await settle();
  assert.deepEqual([line.textContent, line.dataset.tone], ["Another scenario owns this section's time and room; change it there.", 'warning']);
  assert.deepEqual(r.notices.slice(before), []);
  assert.equal(r.d.getElementById('twRoomAssign').disabled, true, 'asking again would change nothing');
  assert.equal(r.w.TW.undoStack.length, 0);
  // Opened on another meeting meanwhile (the same dialog, its title naming
  // that one): the words name the meeting they are about, so they never
  // read as its own, and its own Assign is not held by them.
  const elsewhere = deferred();
  const m = await roomHarness(t, { rows: () => [roomRow(), at(11, { board_id: 2 })], move: () => elsewhere.promise });
  await m.select();
  roomByKey(m);
  await settled(m);
  roomRadio(m, 'R2').click();
  m.d.getElementById('twRoomAssign').click();
  await settle();
  keyOn(m, 'Escape');
  roomByKey(m, 11);
  await settled(m);
  roomRadio(m, 'R2').click();
  elsewhere.resolve({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  await wait(() => !m.d.getElementById('twRoomAlert').hidden);
  await settle();
  assert.equal(m.d.getElementById('twRoomTitle').textContent, 'Change room · AI11 S1');
  assert.equal(m.d.getElementById('twRoomAlert').textContent, "\u2066CS323\u2069 \u2066S1\u2069: Another scenario owns this section's time and room; change it there.");
  assert.equal(m.d.getElementById('twRoomAssign').disabled, false, 'another meeting: asking is still the user’s');
  // Saved instead (the reviewer's steps, in either language): said there,
  // naming its meeting, with no toast; that dialog reads its rooms again.
  for (const arabic of [false, true]) {
    let moved = 'LAB25';
    const landed = deferred();
    const s = await roomHarness(t, {
      arabic,
      rooms: roomsThenR6,
      rows: () => [roomRow({ room: moved }), at(11, { board_id: 2 })],
      move: async body => {
        await landed.promise;
        moved = body.room;
        return { ok: true, placement: roomRow({ room: moved }), placements: [roomRow({ room: moved })] };
      },
    });
    await s.select();
    roomByKey(s);
    await settled(s);
    roomRadio(s, 'R1').click();
    s.d.getElementById('twRoomAssign').click();
    await settle();
    keyOn(s, 'Escape');
    roomByKey(s, 11);
    await settled(s);
    assert.equal(s.reads.length, 2);
    const toasts = s.notices.length;
    const said = s.d.getElementById('twRoomAlert');
    landed.resolve();
    await wait(() => !said.hidden);
    assert.equal(s.d.getElementById('twRoomTitle').textContent, arabic ? 'تغيير القاعة · AI11 S1' : 'Change room · AI11 S1');
    assert.deepEqual([said.textContent, said.dataset.tone],
      [arabic ? '\u2066CS323\u2069 \u2066S1\u2069: عُيّنت القاعة \u2066R1\u2069.' : '\u2066CS323\u2069 \u2066S1\u2069: Room \u2066R1\u2069 assigned.', 'success']);
    assert.deepEqual(s.notices.slice(toasts), [], 'no toast behind the dialog');
    await wait(() => s.reads.length === 3);
  }
  // Whichever helper dialog is open by then takes the words (Move and Place
  // join in 5e and 5f), naming the meeting unless that dialog is about it;
  // clearing a closed dialog's line leaves another's be; a toast is the
  // toast of any save.
  roomDialogEl(r).querySelector('.ux-dialog-foot [data-dialog-close]').click();
  const other = r.d.createElement('dialog');
  other.innerHTML = '<p role="alert" hidden></p><button type="button">OK</button>';
  r.d.body.append(other);
  assert.equal(r.w.UXControls.dialog.open(other), true);
  const quiet = r.notices.length;
  const otherLine = other.querySelector('[role="alert"]');
  const about = { key: 'placement:10', name: '\u2066CS323\u2069 \u2066S1\u2069' };
  r.w.dialogSay(roomDialogEl(r), 'Room \u2066R2\u2069 assigned.', 'success', about);
  assert.deepEqual([otherLine.textContent, otherLine.hidden, otherLine.dataset.tone], ['\u2066CS323\u2069 \u2066S1\u2069: Room \u2066R2\u2069 assigned.', false, 'success']);
  other.dataset.twAbout = 'placement:10';
  r.w.dialogSay(roomDialogEl(r), 'Room \u2066R2\u2069 assigned.', 'success', about);
  assert.equal(otherLine.textContent, 'Room \u2066R2\u2069 assigned.', 'a dialog about the same meeting: the words as they are');
  r.w.dialogSay(roomDialogEl(r), '');
  assert.equal(otherLine.hidden, false, 'clearing is the closed dialog’s own business');
  r.w.UXControls.dialog.close(other);
  r.w.dialogSay(roomDialogEl(r), 'Room \u2066R2\u2069 assigned.', 'success', about);
  assert.deepEqual(r.notices.slice(quiet), [{ type: 'success', text: 'Room \u2066R2\u2069 assigned.' }], 'no dialog open: a toast');
});

test('RC13 data that moves under an open dialog: a chosen room waits for Check again; nothing chosen reads again by itself; a read the write overtook is never drawn; a meeting back after a reload is read again', async t => {
  // A write with no board read after it (as a programme mark's save): the
  // data version alone makes the chosen list stale.
  const h = await roomHarness(t, { rooms: roomsThenR6, more: lockOk });
  await h.select();
  roomByKey(h);
  await settled(h);
  roomRadio(h, 'R1').click();
  await h.w.twFetch('/ops/tw/placements/10/lock/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  assert.match(h.d.getElementById('twRoomBody').textContent, /This meeting changed since its rooms were read\./);
  assert.deepEqual([roomRadios(h).length, h.d.getElementById('twRoomAssign').disabled, h.reads.length], [0, true, 1]);
  h.d.getElementById('twRoomForm').requestSubmit();
  await settle();
  assert.equal(h.posts.length, 0, 'nothing is sent from a list read before the data moved');
  h.d.querySelector('#twRoomBody [data-tw-room-recheck]').click();
  await wait(() => roomRadio(h, 'R6'));
  assert.equal(h.reads.length, 2);
  assert.deepEqual(roomNote(h), [true, 'This is the current room.'], 'chosen afresh: the current room');
  // Nothing chosen: read again by itself, and the keyboard in the filter
  // stays there (an answer never takes it).
  const f = await roomHarness(t, { rooms: roomsThenR6, more: lockOk });
  await f.select();
  roomByKey(f);
  await settled(f);
  const field = f.d.getElementById('twRoomFilter');
  field.focus();
  field.value = 'b1';
  field.dispatchEvent(new f.w.Event('input', { bubbles: true }));
  await f.w.toggleLock(10);
  await wait(() => roomRadio(f, 'R6'));
  assert.equal(f.reads.length, 2);
  assert.equal(f.d.activeElement, f.d.getElementById('twRoomFilter'), 'the keyboard stays in the filter');
  assert.equal(f.d.getElementById('twRoomFilter').value, 'b1');
  assert.deepEqual(roomNote(f), [true, 'This is the current room.']);
  // A choice the filter hid is no choice: nothing is chosen, so the rooms
  // are read again by themselves too, never held for Check again.
  const hid = await roomHarness(t, { rooms: roomsThenR6, more: lockOk });
  await hid.select();
  roomByKey(hid);
  await settled(hid);
  roomRadio(hid, 'R1').click();
  const hiding = hid.d.getElementById('twRoomFilter');
  hiding.focus();
  hiding.value = 'r8';
  hiding.dispatchEvent(new hid.w.Event('input', { bubbles: true }));
  assert.deepEqual(roomNote(hid), [true, 'Choose a room.']);
  await hid.w.toggleLock(10);
  await wait(() => roomRadio(hid, 'R6'));
  assert.equal(hid.reads.length, 2);
  assert.doesNotMatch(hid.d.getElementById('twRoomBody').textContent, /changed since its rooms were read/);
  assert.deepEqual(roomNote(hid), [true, 'Choose a room.'], 'the current room, hidden by the filter, is not chosen either');
  // A write out as the dialog opens (D3b): its read is overtaken as the
  // write lands, read again at once, and the first answer is never drawn.
  const lock = deferred();
  const first = deferred();
  const o = await roomHarness(t, {
    rooms: n => (n === 1 ? first.promise : roomsAnswer([...ROOM_SET, rc('R6')])),
    more: url => (url.endsWith('/lock/') ? lock.promise : undefined),
  });
  await o.select();
  const locking = o.w.toggleLock(10);
  await settle();
  roomByKey(o);
  await settle();
  assert.equal(o.reads.length, 1);
  lock.resolve({ placement_id: 10, is_locked: true });
  await locking;
  await wait(() => roomRadio(o, 'R6'));
  assert.equal(o.reads.length, 2, 'read again without waiting for the first answer');
  first.resolve(roomsAnswer(ROOM_SET.filter(row => row.room_code !== 'R1')));
  for (let i = 0; i < 5; i++) await settle();
  assert.ok(roomRadio(o, 'R1') && roomRadio(o, 'R6'), 'the overtaken answer is not drawn');
  // The scenario read again (a planner job ended) with the dialog open: the
  // meeting leaves the board and comes back, and its rooms are read again,
  // once it is back (nothing is read for a meeting not on the board).
  let back = null;
  const g = await roomHarness(t, { rooms: roomsThenR6, more: url => (back && url === '/ops/tw/boards/2/' ? back.promise : undefined) });
  await g.select();
  roomByKey(g);
  await settled(g);
  back = deferred();
  const reloading = g.w.onScenarioChange();
  await wait(() => /This meeting is no longer on the board\./.test(g.d.getElementById('twRoomBody').textContent));
  for (let i = 0; i < 5; i++) await settle();
  assert.equal(g.reads.length, 1, 'not while it is off the board');
  back.resolve({ placements: [roomRow()], slot_config: scenario.slot_config });
  back = null;
  await reloading;
  await wait(() => roomRadio(g, 'R6'));
  assert.deepEqual([g.reads.length, roomDialogEl(g).hasAttribute('open')], [2, true]);
  // A write that lands while the meeting is off the board reads nothing
  // for it.
  let present = true;
  const held = deferred();
  const v = await roomHarness(t, { rows: () => (present ? [roomRow()] : []), rooms: roomsThenR6, more: url => (url.endsWith('/lock/') ? held.promise : undefined) });
  await v.select();
  roomByKey(v);
  await settled(v);
  const locked = v.w.toggleLock(10);
  await settle();
  present = false;
  await v.w.selectBoard(2);
  assert.match(v.d.getElementById('twRoomBody').textContent, /This meeting is no longer on the board\./);
  held.resolve({ placement_id: 10, is_locked: true });
  await locked;
  for (let i = 0; i < 5; i++) await settle();
  assert.equal(v.reads.length, 1, 'nothing is read for a meeting not on the board');
  // Check again finds the meeting off the board: once it is back, its rooms
  // are read (the choice made from the old ones went with them).
  let shown = true;
  const k = await roomHarness(t, { rows: () => (shown ? [roomRow()] : []), rooms: roomsThenR6, more: lockOk });
  await k.select();
  roomByKey(k);
  await settled(k);
  roomRadio(k, 'R1').click();
  await k.w.twFetch('/ops/tw/placements/10/lock/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  shown = false;
  k.d.querySelector('#twRoomBody [data-tw-room-recheck]').click();
  await wait(() => /This meeting is no longer on the board\./.test(k.d.getElementById('twRoomBody').textContent)
    && k.d.querySelector('.tw-workspace').dataset.busy === 'false');
  shown = true;
  await k.w.selectBoard(2);
  await wait(() => roomRadio(k, 'R6'));
  assert.deepEqual([k.reads.length, roomNote(k)], [2, [true, 'This is the current room.']]);
  // Check again reads the board, then the rooms, once: a write landing while
  // it reads the board is not a reason to read them twice.
  let board = null;
  const write = deferred();
  const c = await roomHarness(t, {
    rooms: n => roomsAnswer(ROOM_SET, { target: n === 1 ? { day: 'MON' } : {} }),
    more: url => (url === '/ops/tw/x/' ? write.promise : board && url === '/ops/tw/boards/2/' ? board.promise : undefined),
  });
  await c.select();
  roomByKey(c);
  await wait(() => c.d.querySelector('#twRoomBody [data-tw-room-recheck]'));
  const writing = c.w.twFetch('/ops/tw/x/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  board = deferred();
  c.d.querySelector('#twRoomBody [data-tw-room-recheck]').click();
  await settle();
  write.resolve({ ok: true });
  await writing;
  board.resolve({ placements: [roomRow()], slot_config: scenario.slot_config });
  board = null;
  await settled(c);
  for (let i = 0; i < 5; i++) await settle();
  assert.equal(c.reads.length, 2);
});

test('RC14 the gate’s first condition wins, the seam itself waits for an open dialog, Selection’s button follows the gate after any draw, and the keys line names R', async t => {
  const h = await roomHarness(t);
  await h.select();
  // Every condition at once, then each lifted in turn: the next one speaks.
  const p = { ...h.w.TW.placements[0], shared_with_other_scenario: true, is_locked: true, is_online: true, needs_room: false };
  Object.assign(h.w.TW, { loadedBoardId: 99, plannerJob: { job_id: 'job-1', status: 'running' } });
  h.w.TW.scenario.status = 'published';
  const ladder = [
    ['published', () => { h.w.TW.scenario.status = 'draft'; }],
    ['loading', () => { h.w.TW.loadedBoardId = h.w.TW.boardId; }],
    ['job', () => { h.w.TW.plannerJob = null; }],
    ['external', () => { p.shared_with_other_scenario = false; }],
    ['online', () => { p.is_online = false; }],
    ['noRoom', () => { p.needs_room = true; }],
  ];
  for (const [code, lift] of ladder) {
    assert.equal(h.w.cardActionRefusal('room', p).code, code);
    lift();
  }
  assert.equal(h.w.cardActionRefusal('room', p), '', 'a time lock holds Move only');
  assert.equal(h.w.cardActionRefusal('move', { ...p, shared_with_other_scenario: true }).code, 'external');
  assert.equal(h.w.cardActionRefusal('move', p).code, 'locked');
  h.w.syncWorkspaceControls();
  // The seam (Move and Place join it) opens nothing while a dialog is open.
  h.w.TW.englishPicker = true;
  assert.equal(h.w.TW.keyActions.room(h.w.TW.placements[0], { origin: 'placement:10' }), false);
  assert.deepEqual([roomDialogEl(h).hasAttribute('open'), h.reads.length], [false, 0]);
  h.w.TW.englishPicker = false;
  // Selection drawn again with the same markup keeps its nodes: the gate is
  // put on them all the same (it moved since they were drawn).
  card(h, 10).click();
  const button = () => h.d.querySelector('#twRightBody [data-tw-action="room"]');
  h.w.TW.plannerJob = { job_id: 'job-2', status: 'running' };
  h.w.renderRightPanel('selection');
  assert.equal(button().disabled, true);
  h.w.TW.plannerJob = null;
  h.w.syncWorkspaceControls();
  const shown = button();
  assert.equal(shown.disabled, false);
  h.w.TW.plannerJob = { job_id: 'job-2', status: 'running' };
  h.w.renderRightPanel('selection');
  assert.equal(button(), shown, 'the same node');
  assert.deepEqual([shown.disabled, h.d.getElementById('twSelActionNote').textContent], [true, 'A planner job is running; edits wait until it ends.']);
  // The keys line says what R does, in both languages.
  const keys = /<p id="twGridKeys"[^>]*>{% if LANGUAGE_CODE == 'ar' %}([^{]*){% else %}([^{]*){% endif %}<\/p>/.exec(template);
  assert.ok(keys, 'the keys line, with its two languages');
  assert.match(keys[1], / · R: تغيير القاعة$/);
  assert.match(keys[2], / · R: change the room$/);
});

test('RC15 the filter finds a wing too, hides a group it empties, and leaves Escape to the dialog when it is empty', async t => {
  const h = await roomHarness(t, { rooms: () => roomsAnswer([rc('R1'), rc('LAB25'), rc('R2', { wing: 'East' }), ...ROOM_SET.slice(3)]) });
  await h.select();
  roomByKey(h);
  await settled(h);
  const field = h.d.getElementById('twRoomFilter');
  const type = text => { field.focus(); field.value = text; field.dispatchEvent(new h.w.Event('input', { bubbles: true })); };
  const shown = () => roomRadios(h).filter(radio => !radio.closest('[hidden]')).map(radio => radio.value);
  const groups = () => [...h.d.querySelectorAll('#twRoomChoices fieldset')].map(set => [set.dataset.group, set.hidden]);
  type('east');
  assert.deepEqual(shown(), ['R2'], 'by wing');
  assert.deepEqual(groups(), [['free', false], ['outside', true], ['inUse', true]], 'a group the filter empties is hidden, legend and all');
  type('r8');
  assert.deepEqual(groups(), [['free', true], ['outside', true], ['inUse', false]]);
  type('r7');
  assert.deepEqual(groups(), [['free', true], ['outside', false], ['inUse', true]]);
  assert.deepEqual(shown(), ['R7']);
  type('');
  assert.deepEqual(groups(), [['free', false], ['outside', false], ['inUse', false]]);
  // Escape in the empty filter closes the dialog, as it does anywhere in it.
  const closes = [];
  roomDialogEl(h).addEventListener('ux-dialog-close', event => closes.push(event.detail.reason));
  assert.equal(h.d.activeElement, field);
  keyOn(h, 'Escape');
  assert.deepEqual([roomDialogEl(h).hasAttribute('open'), closes], [false, ['cancel']]);
});

test('RC16 reads: a failed one is not kept, a meeting gone is said so, and an answer never takes the keyboard from where the user put it', async t => {
  // A failure, then Escape and R again: asked again, not the failure kept.
  const failing = await roomHarness(t, { rooms: n => (n === 1 ? { httpError: 500, error: { code: 'BOOM', message: 'Broken' } } : roomsAnswer()) });
  await failing.select();
  roomByKey(failing);
  await wait(() => failing.d.querySelector('#twRoomBody [data-tw-room-retry]'));
  keyOn(failing, 'Escape');
  roomByKey(failing);
  await settled(failing);
  assert.equal(failing.reads.length, 2);
  // A failure shown when a write lands: read again by itself (the data moved).
  const down = await roomHarness(t, { rooms: n => (n === 1 ? { httpError: 500, error: { code: 'BOOM', message: 'Broken' } } : roomsAnswer()), more: lockOk });
  await down.select();
  roomByKey(down);
  await wait(() => down.d.querySelector('#twRoomBody [data-tw-room-retry]'));
  await down.w.toggleLock(10);
  await settled(down);
  assert.equal(down.reads.length, 2);
  // The server no longer has the meeting: said so, with no Retry.
  const gone = await roomHarness(t, { rooms: () => ({ httpError: 404, error: { code: 'NOT_FOUND', message: 'Gone' } }) });
  await gone.select();
  roomByKey(gone);
  await wait(() => !gone.d.querySelector('#twRoomBody [aria-busy]'));
  assert.match(gone.d.getElementById('twRoomBody').textContent, /This meeting is no longer on the board\./);
  assert.equal(gone.d.querySelector('#twRoomBody [data-tw-room-retry]'), null);
  assert.equal(gone.d.getElementById('twRoomStatus').textContent, 'This meeting is no longer on the board.');
  // The keyboard moved to Close while the rooms loaded: it stays there.
  const reply = deferred();
  const slow = await roomHarness(t, { rooms: () => reply.promise });
  await slow.select();
  roomByKey(slow);
  await settle();
  const close = roomDialogEl(slow).querySelector('.ux-dialog-head [data-dialog-close]');
  close.focus();
  reply.resolve(roomsAnswer());
  await settled(slow);
  assert.equal(slow.d.activeElement, close);
});

test('RC17 a refused save: published and gone hold Assign as another scenario’s section does; a busy server and any other refusal leave it to try again', async t => {
  const cases = [
    ['SCENARIO_PUBLISHED', 'This scenario is published and read-only.', true],
    ['NOT_FOUND', 'This meeting is no longer on the board.', true],
    ['SCENARIO_BUSY', 'Wait for the current operation to finish.', false],
    ['ROOM_INVALID', 'The room was not changed.', false],
  ];
  for (const [code, words, held] of cases) {
    const h = await roomHarness(t, { move: () => ({ httpError: 409, error: { code, message: 'Refused' } }) });
    await h.select();
    roomByKey(h);
    await settled(h);
    roomRadio(h, 'R1').click();
    const toasts = h.notices.length;
    h.d.getElementById('twRoomAssign').click();
    const alert = h.d.getElementById('twRoomAlert');
    await wait(() => !alert.hidden && h.d.querySelector('.tw-workspace').dataset.busy === 'false');
    assert.deepEqual([alert.textContent, alert.dataset.tone], [words, 'warning'], code);
    assert.equal(h.d.getElementById('twRoomAssign').disabled, held, code);
    assert.deepEqual(h.notices.slice(toasts), [], code);
  }
});

test('RC18 rows: a code the server lists twice is one row, the first; the placer’s marker is no room; a current room that does not fit is not chosen', async t => {
  const notMale = { section: 'F', fits_gender: false, available: false, tone: 'block' };
  const h = await roomHarness(t, { rooms: () => roomsAnswer([rc('R1'), rc(' r1', notMale), rc('R4', notMale), rc('r4'), rc('UNASSIGNED'), rc('LAB25')]) });
  await h.select();
  roomByKey(h);
  await settled(h);
  assert.deepEqual(roomRadios(h).map(radio => radio.value), ['R1', 'LAB25']);
  assert.deepEqual([...h.d.querySelectorAll('#twRoomMisfits .ux-choice-static .ux-choice-title bdi')].map(code => code.textContent), ['R4']);
  assert.doesNotMatch(roomDialogEl(h).textContent, /UNASSIGNED/);
  // The meeting sits in R5, a room too small for it: listed with its reason,
  // never chosen, and the note asks for a room.
  const small = await roomHarness(t, { rows: () => [roomRow({ room: 'R5' })] });
  await small.select();
  roomByKey(small);
  await settled(small);
  assert.equal(roomRadios(small).some(radio => radio.checked), false);
  assert.deepEqual(roomNote(small), [true, 'Choose a room.']);
});

test('RC19 the note gives the first reason: published, a planner job, busy, then "Choose a room." and "This is the current room."', async t => {
  for (const [current, words] of [['OLD9', 'Choose a room.'], ['LAB25', 'This is the current room.']]) {
    let refresh = null;
    const h = await roomHarness(t, { rows: () => [roomRow({ room: current })], more: url => (refresh && url === '/ops/tw/boards/2/' ? refresh.promise : undefined) });
    await h.select();
    roomByKey(h);
    await settled(h);
    assert.deepEqual(roomNote(h), [true, words], current);
    refresh = deferred();
    const refreshing = h.w.selectBoard(2);
    await settle();
    assert.deepEqual(roomNote(h), [true, 'Wait for the current operation to finish.'], current);
    h.w.dispatchEvent(new h.w.CustomEvent('tw:planner-job', { detail: { scenarioId: 1, job: { job_id: 'job-3', status: 'running' }, source: 'adapter' } }));
    assert.deepEqual(roomNote(h), [true, 'A planner job is running; edits wait until it ends.'], current);
    h.w.TW.scenario.status = 'published';
    h.w.syncWorkspaceControls();
    assert.deepEqual(roomNote(h), [true, 'This scenario is published and read-only.'], current);
    refresh.resolve({ placements: [roomRow({ room: current })], slot_config: scenario.slot_config });
    refresh = null;
    await refreshing;
  }
});

/* ── Change room, live QA: a room outside the board's programme rooms ──
   Saved, it is Issues' "Room unsuitable", a warning that rings the card
   (the quality check holds a room to the meeting's programme pool), so the
   dialog says so before the save and the report says so after it. */
test('RC20 a free room outside the programme rooms is its own group, a warning as Issues word it; saved, the report is a warning that says Issues will list it', async t => {
  for (const arabic of [false, true]) {
    const h = await roomHarness(t, { arabic });
    await h.select();
    roomByKey(h);
    await settled(h);
    const set = h.d.querySelector('#twRoomChoices fieldset[data-group="outside"]');
    assert.equal(set.querySelector('legend').textContent, arabic ? 'متاحة لكنها خارج قاعات برامج هذه اللوحة (1)' : "Free, but outside this board's programme rooms (1)");
    assert.equal(h.d.getElementById('twRoomHint-outside').textContent, arabic
      ? 'اختيار إحداها يحفظ تحذير «القاعة غير مناسبة» الذي يظهر بعدها في «المشاكل».'
      : 'Choosing one saves a “Room unsuitable” warning, which Issues then shows.');
    assert.ok(set.matches('.ux-choice-list[data-ux-submit]'), 'its rows submit on Enter, as the other groups do');
    assert.deepEqual([...set.querySelectorAll('input[name="twRoomChoice"]')].map(radio => radio.value), ['R7']);
    assert.equal(h.d.getElementById(roomRadio(h, 'R7').getAttribute('aria-describedby').split(' ')[0]).textContent, arabic
      ? 'يُضعف الجودة: القاعة غير مناسبة · خارج قاعات برامج هذه اللوحة'
      : "Lowers quality: Room unsuitable · outside this board's programme rooms");
    roomRadio(h, 'R7').click();
    assert.deepEqual(roomNote(h), [false, '']);
    h.d.getElementById('twRoomAssign').click();
    await wait(() => h.notices.length > 0 && roomDialogEl(h).hasAttribute('open') === false);
    assert.deepEqual(h.posts, [{ room: 'R7' }]);
    assert.deepEqual(h.notices.at(-1), { type: 'warning', text: arabic
      ? 'عُيّنت القاعة \u2066R7\u2069، وستعرضها «المشاكل» غير مناسبة لبرامج هذه اللوحة.'
      : "Room \u2066R7\u2069 assigned; Issues will show it as unsuitable for this board's programmes." });
  }
});

test('RC21 a room in use and outside the programme rooms keeps its clash, says both, and so does its report; the current room outside them is chosen at first', async t => {
  const R12 = rc('R12', { available: false, tone: 'block', department_fit: false, occupied_by: [heldOn(3)] });
  const R13 = rc('R13', { available: false, tone: 'block', department_fit: false, occupied_by: [heldOn(2)] });
  const withCopy = body => ({ ok: true, placement: roomRow({ room: body.room }), placements: [roomRow({ room: body.room }), roomRow({ id: 31, board_id: 3, room: body.room })] });
  for (const arabic of [false, true]) {
    const h = await roomHarness(t, { arabic, rooms: () => roomsAnswer([rc('R1'), rc('LAB25'), R12, R13]), move: withCopy });
    await h.select();
    roomByKey(h);
    await settled(h);
    const said = code => h.d.getElementById(roomRadio(h, code).getAttribute('aria-describedby').split(' ')[0]).textContent;
    assert.deepEqual([...h.d.querySelectorAll('#twRoomChoices fieldset')].map(set => set.dataset.group), ['free', 'inUse'], 'no outside group: both are in use');
    assert.deepEqual(['R12', 'R13'].map(code => roomRadio(h, code).closest('.ux-choice').dataset.tone), ['warning', 'critical']);
    assert.deepEqual(['R12', 'R13'].map(said), arabic
      ? ['يُضعف الجودة: القاعة محجوزة في لوحة أخرى · خارج قاعات برامج هذه اللوحة', 'يمنع النشر: القاعة محجوزة مرتين · خارج قاعات برامج هذه اللوحة']
      : ["Lowers quality: Room also booked on another board · outside this board's programme rooms", "Blocks publishing: Room double-booked · outside this board's programme rooms"]);
    roomRadio(h, 'R12').click();
    h.d.getElementById('twRoomAssign').click();
    await wait(() => h.notices.length > 0 && roomDialogEl(h).hasAttribute('open') === false);
    assert.deepEqual(h.notices.at(-1), { type: 'warning', text: arabic
      ? 'عُيّنت القاعة \u2066R12\u2069، ويستخدمها في هذا الوقت أيضاً: \u2066CS103-S1\u2069. وستعرضها «المشاكل» أيضاً غير مناسبة لبرامج هذه اللوحة. وتغيّرت أيضاً في لوحة أخرى.'
      : "Room \u2066R12\u2069 assigned; \u2066CS103-S1\u2069 also uses it at this time. Issues will also show it as unsuitable for this board's programmes. It also changed on 1 other board." });
  }
  // A meeting already in a room outside the programme rooms: that room is
  // chosen at first and holds Assign, as a current room in any group does.
  const h = await roomHarness(t, { rows: () => [roomRow({ room: 'R7' })] });
  await h.select();
  roomByKey(h);
  await settled(h);
  assert.equal(roomRadio(h, 'R7').checked, true);
  assert.deepEqual(roomNote(h), [true, 'This is the current room.']);
  // Free and outside, with the class's copy on another board changed with
  // it: the warning first, then the other board.
  const shared = await roomHarness(t, { move: withCopy });
  await shared.select();
  roomByKey(shared);
  await settled(shared);
  roomRadio(shared, 'R7').click();
  shared.d.getElementById('twRoomAssign').click();
  await wait(() => shared.notices.length > 0);
  assert.deepEqual(shared.notices.at(-1), { type: 'warning',
    text: "Room \u2066R7\u2069 assigned; Issues will show it as unsuitable for this board's programmes. It also changed on 1 other board." });
  // Rooms outside the programme rooms can be chosen: a list of only those
  // and misfits is not "none fit", and its misfits stay closed.
  const only = await roomHarness(t, { rooms: () => roomsAnswer(ROOM_SET.filter(room => ['R7', 'R3'].includes(room.room_code))) });
  await only.select();
  roomByKey(only);
  await settled(only);
  assert.deepEqual(roomRadios(only).map(radio => radio.value), ['R7']);
  assert.doesNotMatch(only.d.getElementById('twRoomBody').textContent, /No room fits this meeting\./);
  assert.equal(only.d.getElementById('twRoomMisfits').hidden, true);
});
