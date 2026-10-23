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
  // base.html's <html lang>, which the slot module reads.
  d.documentElement.lang = arabic ? 'ar' : 'en';
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
      // The classic page reads the lens with the quality (6d), never /plan-lens/.
      else if (url.includes('/quality/')) answer = quality;
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
  w.eval(fs.readFileSync(path.join(root, 'static/js/timetable-hotspots.js'), 'utf8'));
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
  fail = () => ({ httpError: 409, error: { code: 'VALIDATION_PLACEMENT', message: 'Refused by the server' } });
  assert.equal(await post(false), null);
  fail = () => { throw new TypeError('Failed to fetch'); };
  assert.equal(await post(false), null, 'a handler that throws on a lost answer still lets twFetch answer null');
  assert.deepEqual(told, [['VALIDATION_PLACEMENT', 409], [null, 0]], 'each failure is told once, never again as a network failure');
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'error', text: 'Refused by the server' }, { type: 'error', text: 'Failed to fetch' }],
    'the server’s message (or the network’s), never the handler’s exception');
  assert.deepEqual(slips, ['a handler bug', 'a handler bug'], 'the slip is not lost: it is logged');
  // A quiet request stays quiet when its handler throws.
  fail = () => ({ httpError: 409, error: { code: 'VALIDATION_PLACEMENT', message: 'Refused by the server' } });
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
  fail = () => ({ httpError: 409, error: { code: 'VALIDATION_PLACEMENT', message: 'Refused by the server' } });
  assert.equal(await h.w.twFetch(MOVE_URL, { method: 'POST', body: '{}', _onError: (body, status) => { told.push([body?.error?.code, status]); } }), null);
  assert.deepEqual(told, [['VALIDATION_PLACEMENT', 409]]);
  assert.deepEqual(h.notices.slice(toasts + 1), [{ type: 'error', text: 'Refused by the server' }]);
});

test('the Optimise result names the courses that left students without a section, with their students', async t => {
  const h = await harness(t);
  await h.select();
  let shown = '';
  h.w.dlg = { confirm: async ({ body }) => { shown = body; return true; } };
  h.w.runOptimiseRequest = async () => ({ ok: true, optimisation: {
    mode: 'current', elapsed_seconds: 1,
    baseline_score: [0, 15, 0, 2, 900, 0], final_score: [0, 15, 0, 2, 900, 0], best_score: [0, 15, 0, 2, 900, 0],
    hotspot_courses: ['CS372::DATABASE_SYSTEMS', 'FE2::FREE_ELECTIVE_COURSE_II'],
    hotspot_details: [
      { course_key: 'CS372::DATABASE_SYSTEMS', course_code: 'CS372', course_name: 'Database Systems', students: 14 },
      { course_key: 'FE2::FREE_ELECTIVE_COURSE_II', course_code: 'FE2', course_name: 'Free Elective Course II', students: 1 },
    ],
  } });
  await h.w.runOptimiseV2('current');
  const box = h.d.createElement('div');
  box.innerHTML = shown;
  assert.deepEqual([...box.querySelectorAll('.ux-hotspots-row')].map(row => row.textContent), ['CS372Database Systems14 students', 'FE2Free Elective Course II1 student']);
  assert.ok(!shown.includes('::'), 'no raw course key is shown');
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

test('an issue row takes the user to its sections, on another board too; a meeting off the grid is found in its strip', async t => {
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
  // Drawn in the strip under its table (6e), so its row points at it too (PK1).
  assert.equal(row('Outside the grid’s columns').tagName, 'BUTTON');
  row('Outside the grid’s columns').click();
  await settle();
  assert.deepEqual([...h.d.querySelectorAll('.tw-card.highlight')].map(c => c.dataset.placementId), ['12']);
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
  // The filter dims what does not match (6c): every card stays on the board.
  const dims = () => [10, 11, 12].map(id => card(h, id).dataset.dim || '');
  await find('no room');
  assert.deepEqual(dims(), ['', 'filter', 'filter'], 'the meeting with no room matches what its card says');
  await find('unassigned');
  assert.deepEqual(dims(), ['filter', 'filter', 'filter'], 'the placer’s marker is not something the user can see, nor find');
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

test('LN8 two courses sharing a code are told apart: colour by key, and named once the lens says so (one redraw; none for the same codes again)', async t => {
  const placements = [
    at(10, { course_code: 'CS112', course_key: 'CS112::PROGRAMMING_I', course_name: 'Programming I' }),
    at(11, { day: 'MON', course_code: 'CS112', course_key: 'CS112::PROGRAMMING_II', course_name: 'Programming II', term_section_id: 120 }),
  ];
  // The lens is where the page learns that two courses share a code: it comes
  // with the quality read, after the board is first drawn.
  let courses = { 'CS112::PROGRAMMING_I': {}, 'CS112::PROGRAMMING_II': {} };
  const h = await harness(t, url => url === '/ops/tw/boards/2/' ? { placements, slot_config: scenario.slot_config }
    : url.includes('/quality/') && url.endsWith('?lens=1') ? { ...noRooms, plan_lens: { plans: [], sections: {}, courses } }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms : undefined);
  const draws = [];
  const draw = h.w.renderGridWithSlots;
  h.w.renderGridWithSlots = (...args) => {
    draw(...args);
    draws.push([...h.d.querySelectorAll('#twGrid .tw-card-name')].map(name => name.textContent));
  };
  await h.select();
  assert.deepEqual(draws, [[], ['Programming I', 'Programming II']], 'drawn without names, then once more with them');
  const hue = id => card(h, id).style.getPropertyValue('--tw-card-hue');
  assert.notEqual(hue(10), hue(11), 'the card colours by course key, not by code');
  assert.equal(card(h, 10).querySelector('.tw-card-name').getAttribute('dir'), 'auto');
  // The same codes again: the board is not drawn again.
  await h.w.refreshWorkspaceQuality(true, { lens: true });
  assert.equal(draws.length, 2, 'the same codes: no redraw');
  // The codes no longer shared: drawn once more, without the names.
  courses = { 'CS112::PROGRAMMING_I': {} };
  await h.w.refreshWorkspaceQuality(true, { lens: true });
  assert.deepEqual(draws.slice(2), [[]]);
  assert.deepEqual(h.errors, []);
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
  // The filter dims what does not match (6c): every card stays on the board.
  const dims = () => [10, 11].map(id => card(h, id).dataset.dim || '');
  await find('dr rule');
  assert.deepEqual(dims(), ['', 'filter']);
  await find('stale name');
  assert.deepEqual(dims(), ['filter', 'filter'], 'the stale name is not on the card, so it finds nothing');
  await find('dr meeting');
  assert.deepEqual(dims(), ['filter', ''], 'a section the rule leaves to its meetings keeps that name');
});

/* ── The filter's view: folds and dims in place (design record §7.2 item 6, 6c) ──
   Every meeting stays drawn. A table the text matches nothing in folds to one
   row (its toggle, the table's one keyboard stop); the other cards the text
   misses are dimmed, by attributes only, so typing never draws the board
   again. Programme tokens (the lens, 6d) dim and never fold. */
const VIEW_LABS = [{ label: 'L1', start: '10:30', end: '12:10' }];
function viewHarness(t, { placements, labs = VIEW_LABS, lens = null, q = noRooms, arabic = false, other = null } = {}) {
  return harness(t, url => {
    if (url === '/ops/tw/boards/2/') return { placements, slot_config: scenario.slot_config, lab_slot_config: labs };
    if (other && url.startsWith('/ops/tw/boards/?')) return { boards: [board, { ...board, id: 3, label: 'Term 8', nominal_term: 8 }] };
    if (other && url === '/ops/tw/boards/3/') return { placements: other, slot_config: scenario.slot_config, lab_slot_config: labs };
    if (url.includes('/conflicts/')) return quiet;
    // The lens comes with the quality read that asks for it (6d).
    if (url.includes('/quality/')) return lens && url.endsWith('?lens=1') ? { ...q, plan_lens: lens } : q;
    if (url.includes('/readiness/')) return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    return undefined;
  }, {}, arabic);
}
// A programme or code as plain text isolates it (U+2066 … U+2069).
const iso = text => `${String.fromCharCode(0x2066)}${text}${String.fromCharCode(0x2069)}`;
// S1: two lectures; S2: a lecture and a lab. With labs on, each group has a lab table.
const viewRows = () => [
  at(10, { course_code: 'CS101' }),
  at(11, { course_code: 'MATH1', day: 'MON' }),
  at(12, { course_code: 'PHYS1', section: 'S2', term_section_id: 212 }),
  at(13, { course_code: 'CS102', section: 'S2', term_section_id: 213, start_time: '10:30', end_time: '12:10' }),
];
const findText = async (h, text) => {
  const search = h.d.getElementById('twSearch');
  search.value = text;
  search.dispatchEvent(new h.w.Event('input'));
  await settle();
};
function foldOf(h, group, kind) {
  const table = h.d.querySelector(`#twGrid [role="grid"][data-group="${group}"][data-kind="${kind}"]`);
  const fold = table.querySelector(':scope > .tw-fold-row');
  const body = table.querySelector(':scope > .tw-fold-body');
  const toggle = fold.querySelector('.tw-fold-toggle');
  const state = [fold.hidden ? 'row hidden' : 'row shown', toggle.getAttribute('aria-expanded'), body.hidden ? 'rows hidden' : 'rows shown'];
  return { table, fold, body, toggle, state, label: toggle.querySelector('.ux-disclosure-label').textContent };
}
const NO_FOLD = ['row hidden', 'true', 'rows shown'];
const FOLDED = ['row shown', 'false', 'rows hidden'];
const OPENED = ['row shown', 'true', 'rows shown'];
const DAYS5 = ['SUN', 'MON', 'TUE', 'WED', 'THU'];
const dimsOf = (h, ids) => ids.map(id => card(h, id).dataset.dim || '');
const boardNavOf = h => h.w.UXControls.roving(h.d.getElementById('twGrid'));

test('FL1 the text folds each table it matches nothing in and dims the rest of the open ones; every meeting stays drawn', async t => {
  const h = await viewHarness(t, { placements: viewRows() });
  await h.select();
  const tables = [['S1', 'lecture'], ['S1', 'lab'], ['S2', 'lecture'], ['S2', 'lab']];
  assert.deepEqual(tables.map(([g, k]) => foldOf(h, g, k).state), Array(4).fill(NO_FOLD), 'no text: nothing folds');
  assert.equal(h.d.querySelectorAll('#twGrid [data-dim]').length, 0);
  assert.equal(h.d.getElementById('twLegendDim').hidden, true);
  await findText(h, 'cs1');
  assert.deepEqual(tables.map(([g, k]) => foldOf(h, g, k).state), [NO_FOLD, FOLDED, FOLDED, NO_FOLD],
    'folded per table: a table with no match (an empty one too), never its whole group');
  assert.equal(foldOf(h, 'S1', 'lab').label, 'Labs · no match · 0 meetings');
  assert.equal(foldOf(h, 'S2', 'lecture').label, 'Lectures · no match · 1 meeting');
  assert.equal(foldOf(h, 'S2', 'lecture').toggle.getAttribute('aria-controls'), foldOf(h, 'S2', 'lecture').body.id);
  assert.equal(foldOf(h, 'S2', 'lecture').fold.firstElementChild.getAttribute('aria-colspan'), '2', 'the day column and the one slot');
  assert.deepEqual([...h.d.querySelectorAll('#twGrid .tw-card')].map(c => c.dataset.placementId).sort(), ['10', '11', '12', '13'], 'nothing removed');
  assert.deepEqual(dimsOf(h, [10, 11, 12, 13]), ['', 'filter', 'filter', '']);
  assert.equal(card(h, 11).getAttribute('aria-describedby'), 'twDimNoteFilter');
  assert.equal(card(h, 10).hasAttribute('aria-describedby'), false);
  assert.equal(h.d.getElementById('twDimNoteFilter').textContent, 'Outside the filter');
  assert.ok(!h.d.getElementById('twGrid').contains(h.d.getElementById('twDimNoteFilter')), 'the notes are outside the board');
  assert.equal(card(h, 11).title, 'Outside the filter', 'its title says why');
  assert.equal(h.d.getElementById('twLegendDim').hidden, false);
  assert.equal(h.d.getElementById('twLegendDim').textContent, 'Dimmed: outside the filter');
  assert.deepEqual([...h.d.querySelectorAll('#twGrid .tw-group-meta')].map(m => m.textContent), ['2 courses · 1 match', '2 courses · 1 match']);
  const input = h.d.getElementById('twSearch');
  assert.equal(h.d.getElementById('twSearchCount').textContent, '2 of 4');
  assert.equal(input.title, '2 of 4 meetings match; tables folded for no match: 2.');
  assert.equal(h.d.getElementById('twSearchNote').textContent, input.title);
  assert.equal(input.getAttribute('aria-describedby'), 'twSearchNote');
  // Drawn again with the text on (as after an edit): every meeting, the same view.
  h.w.renderGrid();
  assert.deepEqual([...h.d.querySelectorAll('#twGrid .tw-card')].map(c => c.dataset.placementId).sort(), ['10', '11', '12', '13'], 'a redraw removes nothing either');
  assert.deepEqual(tables.map(([g, k]) => foldOf(h, g, k).state), [NO_FOLD, FOLDED, FOLDED, NO_FOLD]);
  assert.deepEqual(dimsOf(h, [10, 11, 12, 13]), ['', 'filter', 'filter', '']);
  await findText(h, '');
  assert.deepEqual(tables.map(([g, k]) => foldOf(h, g, k).state), Array(4).fill(NO_FOLD), 'cleared: nothing folds');
  assert.equal(h.d.querySelectorAll('#twGrid [data-dim], #twGrid .tw-card[aria-describedby]').length, 0);
  assert.equal(h.d.getElementById('twLegendDim').hidden, true);
  assert.deepEqual([h.d.getElementById('twSearchCount').textContent, input.title, h.d.getElementById('twSearchNote').textContent], ['', '', '']);
  assert.deepEqual([...h.d.querySelectorAll('#twGrid .tw-group-meta')].map(m => m.textContent), ['2 courses', '2 courses']);
  assert.deepEqual(h.errors, []);
  // A board with nothing on it: its empty tables fold too.
  const empty = await viewHarness(t, { placements: [] });
  await empty.select();
  await findText(empty, 'cs1');
  assert.deepEqual([foldOf(empty, '-', 'lecture').state, foldOf(empty, '-', 'lab').state], [FOLDED, FOLDED]);
  empty.w.renderGrid();
  assert.deepEqual([foldOf(empty, '-', 'lecture').state, foldOf(empty, '-', 'lab').state], [FOLDED, FOLDED], 'and when drawn again');
  assert.equal(foldOf(empty, '-', 'lecture').label, 'Lectures · no match · 0 meetings');
  assert.equal(empty.d.getElementById('twSearch').title, '0 of 0 meetings match; tables folded for no match: 2.');
});

test('FL2 typing keeps the same card nodes and the keyboard on a focused card; the board is never drawn again', async t => {
  const h = await viewHarness(t, { placements: viewRows() });
  await findText(h, 'cs1');  // before any scenario is open: nothing to fold, nothing thrown
  assert.equal(h.d.getElementById('twSearchCount').textContent, '0 of 0');
  await findText(h, '');
  await h.select();
  const nodes = [...h.d.querySelectorAll('#twGrid .tw-card')];
  card(h, 11).focus();
  // Records are handed to the callback after each turn: keep them all.
  const records = [];
  const observer = new h.w.MutationObserver(list => records.push(...list));
  observer.observe(h.d.getElementById('twGrid'), { childList: true, subtree: true });
  for (const text of ['c', 'cs', 'cs1', 'math', 'zzz', '']) {
    await findText(h, text);
    assert.deepEqual([...h.d.querySelectorAll('#twGrid .tw-card')], nodes, `"${text}": the same cards`);
    assert.equal(h.d.activeElement, card(h, 11), `"${text}": the keyboard stays`);
  }
  records.push(...observer.takeRecords());
  assert.deepEqual(records, [], 'no node of the board added or removed');
});

test('FL3 a fold counts its toned meetings with the icon and the words; a later warning updates the count in place', async t => {
  const rows = [...viewRows(), at(14, { course_code: 'BIO1', section: 'S2', term_section_id: 214, day: 'TUE' })];
  rows[2] = { ...rows[2], course_key: 'PHYS1', room: 'UNASSIGNED', room_state: 'unassigned' };  // PHYS1: no room, critical
  const h = await viewHarness(t, { placements: rows });
  await h.select();
  await findText(h, 'cs1');
  assert.equal(card(h, 12).title, 'Blocks publishing: No room | PHYS1 | Outside the filter', 'the tone, the identity, then why it is dimmed');
  assert.ok(card(h, 12).classList.contains('critical'), 'a dimmed card keeps its tone');
  const fold = () => foldOf(h, 'S2', 'lecture');
  assert.deepEqual(fold().state, FOLDED);
  const tone = name => fold().fold.querySelector(`.tw-fold-tone[data-tone="${name}"]`);
  assert.deepEqual([tone('critical').hidden, tone('critical').querySelector('.tw-fold-n').textContent], [false, '1']);
  assert.ok(tone('critical').querySelector('.ux-tone-icon[data-tone="critical"][aria-hidden="true"]'));
  assert.equal(tone('warning').hidden, true, 'no warning in it');
  assert.equal(fold().toggle.textContent.replace(/\s+/g, ' ').trim(), 'Lectures · no match · 2 meetingsBlocks publishing: 1Lowers quality: 0');
  // A later answer (the quality's rooms) warns about BIO1: counted in place.
  const nav = boardNavOf(h);
  let refreshes = 0;
  const refresh = nav.refresh;
  nav.refresh = () => { refreshes++; refresh(); };
  t.after(() => { nav.refresh = refresh; });
  const observer = new h.w.MutationObserver(() => {});
  observer.observe(h.d.getElementById('twGrid'), { childList: true, subtree: true });
  // The count is a live region: the same words are not written again.
  const live = new h.w.MutationObserver(() => {});
  live.observe(h.d.getElementById('twSearchCount'), { childList: true, characterData: true, subtree: true });
  const row = fold().fold;
  h.w.TW.quality = { ...noRooms, rooms: { assessed: true, shortages: 1, details: [{ course_code: 'BIO1', placement_ids: [14], rooms: ['R1'], reasons: ['undersized'], required_capacity: 30 }] } };
  h.w.renderBoardIssueViews();
  assert.deepEqual([tone('warning').hidden, tone('warning').querySelector('.tw-fold-n').textContent], [false, '1']);
  assert.equal(tone('critical').querySelector('.tw-fold-n').textContent, '1');
  assert.equal(fold().fold, row, 'the same row');
  assert.deepEqual(observer.takeRecords(), [], 'counts written in place, no node added or removed');
  assert.equal(refreshes, 0, 'tones never make the board read its stops again');
  // A tone update builds the issue model once: applyGridView takes its byPlacement.
  const build = h.w.buildBoardIssues;
  let builds = 0;
  h.w.buildBoardIssues = () => { builds++; return build(); };
  h.w.applyCardTones();
  h.w.buildBoardIssues = build;
  assert.equal(builds, 1, 'one issue model per tone update');
  assert.equal(refreshes, 0);
  assert.equal(h.d.getElementById('twSearchCount').textContent, '2 of 5');
  assert.deepEqual(live.takeRecords(), [], 'the count was not read out again');
});

test('FL4 a fold the user opens stays open through tone redraws and a redraw; new text folds it again', async t => {
  const h = await viewHarness(t, { placements: viewRows() });
  await h.select();
  await findText(h, 'cs1');
  const fold4 = () => foldOf(h, 'S2', 'lecture');
  const title = () => h.d.getElementById('twSearch').title;
  assert.equal(title(), '2 of 4 meetings match; tables folded for no match: 2.');
  fold4().toggle.click();
  assert.deepEqual(fold4().state, OPENED);
  assert.deepEqual([...h.w.TW.foldOpen], ['S2|lecture']);
  assert.equal(title(), '2 of 4 meetings match; tables folded for no match: 1.', 'opened: no longer folded');
  h.w.applyCardTones();
  assert.deepEqual(fold4().state, OPENED, 'a tone redraw keeps it open');
  h.w.renderGrid();
  assert.deepEqual(fold4().state, OPENED, 'the same table and text, drawn again: still open');
  // The board's stop inside the opened table: new text that still matches
  // nothing there hides its rows (its fold row stays), and the stop moves.
  // (The redraw's own refresh runs first, as it does long before a key.)
  await settle();
  boardNavOf(h).active('placement:12');
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')], [card(h, 12)]);
  await findText(h, 'cs10');
  assert.deepEqual(fold4().state, FOLDED, 'new text: folded again');
  const zero = [...h.d.querySelectorAll('#twGrid [tabindex="0"]')];
  assert.equal(zero.length, 1, 'one stop');
  assert.equal(zero[0].closest('[hidden]'), null, 'a stop that is shown: Tab still reaches the board');
  await findText(h, 'cs1');
  assert.deepEqual(fold4().state, FOLDED, 'and the memory is gone with the old text');
  // Opened, then shut by the user: shut it stays.
  fold4().toggle.click();
  fold4().toggle.click();
  assert.deepEqual(fold4().state, FOLDED);
  h.w.applyCardTones();
  assert.deepEqual(fold4().state, FOLDED);
  assert.deepEqual([...h.w.TW.foldOpen], []);
  // The memory is the board's: another board with the same tables starts folded.
  const b = await viewHarness(t, { placements: viewRows(), other: viewRows() });
  await b.select();
  await findText(b, 'cs1');
  foldOf(b, 'S2', 'lecture').toggle.click();
  await b.w.selectBoard(3);
  assert.deepEqual(foldOf(b, 'S2', 'lecture').state, FOLDED, 'another board: folded');
  await b.w.selectBoard(2);
  assert.deepEqual(foldOf(b, 'S2', 'lecture').state, FOLDED, 'and back: the memory went with the board');
});

test('FL5 locating a meeting inside a fold opens it, pulses the card and gives the board’s stop to it', async t => {
  const rows = viewRows();
  rows[2] = { ...rows[2], room: 'UNASSIGNED', room_state: 'unassigned' };  // PHYS1: no room, critical
  const h = await viewHarness(t, { placements: rows });
  await h.select();
  await findText(h, 'cs1');
  assert.deepEqual(foldOf(h, 'S2', 'lecture').state, FOLDED);
  h.w.renderRightPanel('issues');
  const row = [...h.d.querySelectorAll('#twRightBody .tw-issue-row')].find(r => r.querySelector('.tw-issue-kind').textContent.startsWith('No room'));
  const events = [];
  h.d.addEventListener('ux-disclosure', event => events.push([event.target.dataset.foldKey, event.detail.open]));
  row.focus();
  row.click();
  await settle();
  assert.deepEqual(foldOf(h, 'S2', 'lecture').state, OPENED);
  assert.deepEqual(events, [['S2|lecture', true]], 'opened as a click on its toggle would: one event');
  assert.deepEqual([...h.w.TW.foldOpen], ['S2|lecture']);
  assert.ok(card(h, 12).classList.contains('highlight'));
  assert.equal(card(h, 12).tabIndex, 0, 'the board’s one stop');
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')], [card(h, 12)]);
  assert.equal(h.d.activeElement, row, 'the keyboard stays where it was');
  assert.equal(h.w.openFoldOf(card(h, 10)), false, 'a card outside a fold: nothing to open');
});

test('FL6 the text filter finds what cards show, never the lens’s own words', async t => {
  const lens = { plans: ['AI'], courses: {}, sections: { 110: { owner: 'UNALLOCATED', owner_label: 'Unallocated', role: 'core', filter_plans: ['AI'] } } };
  const h = await viewHarness(t, { lens, labs: [], placements: [
    at(10, { course_code: 'CS101', room: 'UNASSIGNED', room_state: 'unassigned', teacher: { name: 'Dr Rule', status: 'pinned' } }),
  ] });
  await h.select();
  for (const text of ['unallocated', 'core', 'Unallocated']) {
    await findText(h, text);
    assert.deepEqual(dimsOf(h, [10]), ['filter'], text);
    assert.deepEqual(foldOf(h, 'S1', 'lecture').state, FOLDED, text);
  }
  for (const text of ['no room', 'dr rule', 'cs101 sun']) {
    await findText(h, text);
    assert.deepEqual(dimsOf(h, [10]), [''], text);
    assert.deepEqual(foldOf(h, 'S1', 'lecture').state, NO_FOLD, text);
  }
});

test('FL7 the keyboard: ↓ or PageDown reaches a folded table’s toggle; Enter opens it; ↓ goes into it; a toggle is never a Tab stop of its own', async t => {
  const h = await viewHarness(t, { labs: [], placements: [
    at(10, { course_code: 'CS101' }),
    at(12, { course_code: 'PHYS1', section: 'S2', term_section_id: 212 }),
  ] });
  await h.select();
  await findText(h, 'cs1');
  const fold = () => foldOf(h, 'S2', 'lecture');
  const toggles = () => [...h.d.querySelectorAll('#twGrid .tw-fold-toggle')];
  assert.ok(toggles().every(toggle => toggle.tabIndex === -1), 'drawn out of the tab order');
  boardNavOf(h).active('placement:10', { focus: true });
  for (let i = 0; i < 4; i++) keyOn(h, 'ArrowDown');
  assert.equal(h.d.activeElement.dataset.focusKey, 'cell:S1:lecture|THU|09:00');
  keyOn(h, 'ArrowDown');
  assert.equal(h.d.activeElement, fold().toggle, '↓ from the last day reaches the next table’s fold');
  assert.deepEqual(toggles().map(toggle => toggle.tabIndex), [-1, 0], 'one stop: the active toggle; the hidden one is not a stop');
  keyOn(h, 'PageUp');
  assert.equal(h.d.activeElement, card(h, 10));
  keyOn(h, 'PageDown');
  assert.equal(h.d.activeElement, fold().toggle, 'PageDown too');
  const enter = keyOn(h, 'Enter');
  assert.equal(enter.defaultPrevented, false, 'Enter is the button’s own');
  fold().toggle.click();  // what the browser does with that Enter
  assert.deepEqual(fold().state, OPENED);
  // A cell's stop is its card, or the cell when it holds none.
  const stops = [...fold().body.querySelectorAll('.tw-cell')].map(cell => cell.querySelector('.tw-card') || cell);
  assert.equal(stops.length, 5);
  assert.ok(stops.every(stop => stop.hasAttribute('data-ux-roving') && stop.tabIndex === -1), 'its rows are stops now, read again at once');
  keyOn(h, 'ArrowDown');
  assert.equal(h.d.activeElement, card(h, 12), '↓ goes into the table');
  keyOn(h, 'ArrowUp');
  assert.equal(h.d.activeElement, fold().toggle, '↑ back to its fold, which can shut it again');
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')], [fold().toggle]);
  fold().toggle.click();
  assert.deepEqual(fold().state, FOLDED);
  assert.ok(stops.every(stop => !stop.hasAttribute('data-ux-roving')), 'shut: its rows are not stops');
  assert.deepEqual(toggles().map(toggle => toggle.tabIndex), [-1, 0]);
  // The text cleared while the stop is a fold toggle: the board's one stop
  // moves to something shown (Tab must still reach the board).
  h.d.getElementById('twSearch').focus();
  await findText(h, '');
  const zero = [...h.d.querySelectorAll('#twGrid [tabindex="0"]')];
  assert.equal(zero.length, 1);
  assert.equal(zero[0].closest('[hidden]'), null, 'a stop that is shown');
  assert.ok(toggles().every(toggle => toggle.tabIndex === -1 || toggle.closest('[hidden]')), 'no toggle is a Tab stop of its own');
});

test('FL8 a dimmed card is an ordinary card: it drags, Enter selects it, M and R act on it', async t => {
  const h = await viewHarness(t, { placements: viewRows() });
  await h.select();
  await findText(h, 'cs1');
  assert.equal(card(h, 11).dataset.dim, 'filter');
  assert.equal(card(h, 11).draggable, true);
  const acted = [];
  h.w.TW.keyActions.move = p => acted.push(['move', p.id]);
  h.w.TW.keyActions.room = p => acted.push(['room', p.id]);
  boardNavOf(h).active('placement:11', { focus: true });
  keyOn(h, 'Enter');
  assert.equal(h.w.TW.selectedPlacementId, 11);
  keyOn(h, 'm');
  keyOn(h, 'r');
  assert.deepEqual(acted, [['move', 11], ['room', 11]]);
  assert.equal(card(h, 11).classList.contains('is-selected'), true, 'selected and still dimmed');
  assert.equal(card(h, 11).dataset.dim, 'filter');
});

test('FL9 the count, its title and the folds count meetings by the language’s plural (1, 2, 3, 11)', async t => {
  const forms = {
    en: { 1: ['1 meeting matches', '1 meeting'], 2: ['2 meetings match', '2 meetings'], 3: ['3 meetings match', '3 meetings'], 11: ['11 meetings match', '11 meetings'] },
    ar: { 1: ['لقاء واحد', 'لقاء واحد'], 2: ['لقاءين', 'لقاءان'], 3: ['3 لقاءات', '3 لقاءات'], 11: ['11 لقاءً', '11 لقاءً'] },
  };
  for (const arabic of [false, true]) {
    for (const n of [1, 2, 3, 11]) {
      const rows = Array.from({ length: n }, (_, i) => at(10 + i, { course_code: `X${i}Y`, day: DAYS5[i % 5] }));
      const h = await viewHarness(t, { placements: rows, labs: [], arabic });
      await h.select();
      const [ofTotal, nominative] = forms[arabic ? 'ar' : 'en'][n];
      await findText(h, 'x0y');
      const input = h.d.getElementById('twSearch');
      assert.equal(h.d.getElementById('twSearchCount').textContent, arabic ? `1 من ${n}` : `1 of ${n}`);
      assert.equal(input.title, arabic ? `المطابق للتصفية: 1 من ${ofTotal}.` : `1 of ${n} ${ofTotal.replace(/^\d+ /, '')}.`);
      await findText(h, 'qqq');
      assert.equal(h.d.getElementById('twSearchCount').textContent, arabic ? `0 من ${n}` : `0 of ${n}`, 'nothing matches');
      assert.equal(foldOf(h, 'S1', 'lecture').label, arabic ? `المحاضرات · لا شيء مطابق · ${nominative}` : `Lectures · no match · ${nominative}`);
      assert.equal(input.title, arabic
        ? `المطابق للتصفية: 0 من ${ofTotal}؛ الجداول المطويّة لعدم وجود مطابق: 1.`
        : `0 of ${n} ${ofTotal.replace(/^\d+ /, '')}; tables folded for no match: 1.`);
      if (arabic) {
        assert.equal(h.d.getElementById('twDimNoteFilter').textContent, 'خارج التصفية');
        assert.equal(h.d.getElementById('twLegendDim').textContent, 'باهتة: خارج التصفية');
        assert.equal(card(h, 10).title, 'خارج التصفية');
      }
    }
  }
  // A group heading's matches, in Arabic.
  const matches = { 0: 'لا شيء مطابق', 1: 'مطابقة واحدة', 2: 'مطابقتان', 3: '3 مطابقات', 11: '11 مطابقةً' };
  const courses = { 1: 'مقرر واحد', 2: 'مقرران', 3: '3 مقررات', 11: '11 مقرراً' };
  for (const n of [1, 2, 3, 11]) {
    const rows = [...Array.from({ length: n }, (_, i) => at(10 + i, { course_code: `X${i}Y`, day: DAYS5[i % 5] })),
      at(40, { course_code: 'ZED', section: 'S2', term_section_id: 240 })];
    const h = await viewHarness(t, { placements: rows, labs: [], arabic: true });
    await h.select();
    await findText(h, 'x');
    assert.deepEqual([...h.d.querySelectorAll('#twGrid .tw-group-meta')].map(m => m.textContent), [`${courses[n]} · ${matches[n]}`, `${courses[1]} · ${matches[0]}`]);
  }
});

test('FL10 a programme token dims what its per-plan file leaves out, by the union of the programmes, and never folds; SHARED reads the shared roles', async t => {
  // The roles are the server's (programme_view, 6d): the page holds no copy of the rule.
  const lens = { plans: ['AI', 'AI2'], courses: {}, sections: {}, seating: { assessed: true, students: 9 }, programme_view: {
    programmes: ['AI', 'AI2'],
    roles: { AI: { main: [10] }, AI2: { main: [11, 12] } },
    shared_roles: { AI: {}, AI2: { main: [11] } },
    placement_ids: [10, 11, 12, 20],
    seating_assessed: true,
  } };
  for (const arabic of [false, true]) {
    const h = await viewHarness(t, { lens, labs: [], arabic, placements: [
      at(10, { course_code: 'CS101', term_section_id: 110 }),
      at(11, { course_code: 'MATH1', term_section_id: 111, day: 'MON' }),
      at(12, { course_code: 'PHYS1', term_section_id: 112, day: 'TUE' }),
      at(20, { course_code: 'BIO1', section: 'S2', term_section_id: 220 }),  // in no programme's file: its table is all dimmed
    ] });
    await h.select();
    await findText(h, 'ai2');
    assert.deepEqual(dimsOf(h, [10, 11, 12, 20]), ['lens', '', '', 'lens']);
    assert.deepEqual([foldOf(h, 'S1', 'lecture').state, foldOf(h, 'S2', 'lecture').state], [NO_FOLD, NO_FOLD], 'the lens never folds');
    assert.equal(card(h, 10).getAttribute('aria-describedby'), 'twDimNoteLens');
    const note = arabic ? `ليست في جدول ${iso('AI2')}` : `Not in ${iso('AI2')}’s timetable`;
    assert.equal(h.d.getElementById('twDimNoteLens').textContent, note);
    assert.equal(card(h, 10).title, note);
    assert.equal(card(h, 11).title, arabic ? `${iso('AI2')}: شعبة خاصة به` : `${iso('AI2')}: its own section`, 'a card in the file says its role');
    assert.equal(h.d.getElementById('twSearchCount').textContent, arabic ? '2 من 4' : '2 of 4');
    assert.equal(h.d.getElementById('twSearch').title, arabic
      ? `المطابق للتصفية: 2 من 4 لقاءات. ${iso('AI2')}: 2 من 4 لقاءات في جدوله.`
      : `2 of 4 meetings match. ${iso('AI2')}: 2 of 4 meetings in its timetable.`,
    'the count’s words say what the programme’s timetable holds');
    // A lens dim is a dim: a heading does not count it a match, and the
    // legend's programme line says what it is (the text's line stays hidden).
    assert.deepEqual([...h.d.querySelectorAll('#twGrid .tw-group-meta')].map(m => m.textContent), arabic
      ? ['3 مقررات · مطابقتان', 'مقرر واحد · لا شيء مطابق'] : ['3 courses · 2 matches', '1 course · 0 matches']);
    assert.deepEqual([h.d.getElementById('twLegendDim').hidden, h.d.getElementById('twLegendDimLens').hidden], [true, false]);
    assert.equal(h.d.getElementById('twLegendDimLens').textContent, arabic ? `باهتة: ${note}` : `Dimmed: not in ${iso('AI2')}’s timetable`);
    await findText(h, 'ai ai2');
    assert.deepEqual(dimsOf(h, [10, 11, 12, 20]), ['', '', '', 'lens'], 'several programmes: a union');
    const several = arabic ? `ليست في أيٍّ من جداول: ${iso('AI')}، ${iso('AI2')}` : `In none of these timetables: ${iso('AI')}, ${iso('AI2')}`;
    assert.equal(h.d.getElementById('twDimNoteLens').textContent, several);
    assert.equal(h.d.getElementById('twLegendDimLens').textContent, arabic ? `باهتة: ${several}` : `Dimmed: in none of these timetables: ${iso('AI')}, ${iso('AI2')}`,
      'the legend’s words follow the filter');
    await findText(h, 'shared ai2');
    assert.deepEqual(dimsOf(h, [10, 11, 12, 20]), ['lens', '', 'lens', 'lens'], 'SHARED: the programme’s shared roles');
    assert.equal(h.d.getElementById('twDimNoteLens').textContent, arabic
      ? `ليست شعبة مشتركة في جدول ${iso('AI2')}` : `Not a shared section in ${iso('AI2')}’s timetable`);
    await findText(h, 'shared');
    assert.deepEqual(dimsOf(h, [10, 11, 12, 20]), ['lens', '', 'lens', 'lens'], 'SHARED alone: every programme’s shared roles');
    assert.equal(h.d.getElementById('twDimNoteLens').textContent, arabic
      ? 'ليست شعبة مشتركة في جدول أي برنامج' : 'Not a shared section in any programme’s timetable');
    await findText(h, 'shared ai ai2');
    assert.equal(h.d.getElementById('twDimNoteLens').textContent, arabic
      ? `ليست شعبة مشتركة في هذه الجداول: ${iso('AI')}، ${iso('AI2')}` : `Not a shared section in these timetables: ${iso('AI')}, ${iso('AI2')}`);
    // A programme and text: the text folds, the programme only dims.
    await findText(h, 'ai2 math');
    assert.deepEqual(dimsOf(h, [10, 11, 12, 20]), ['filter lens', '', 'filter', 'filter lens']);
    assert.equal(card(h, 10).getAttribute('aria-describedby'), 'twDimNoteFilter twDimNoteLens');
    assert.deepEqual([foldOf(h, 'S1', 'lecture').state, foldOf(h, 'S2', 'lecture').state], [NO_FOLD, FOLDED]);
    assert.deepEqual([h.d.getElementById('twLegendDim').hidden, h.d.getElementById('twLegendDimLens').hidden], [false, false], 'both lines');
    assert.equal(h.d.getElementById('twSearch').title, arabic
      ? `المطابق للتصفية: 1 من 4 لقاءات؛ الجداول المطويّة لعدم وجود مطابق: 1. ${iso('AI2')}: 2 من 4 لقاءات في جدوله.`
      : `1 of 4 meetings match; tables folded for no match: 1. ${iso('AI2')}: 2 of 4 meetings in its timetable.`);
    // An opened fold is kept for the board and its TEXT: a programme token
    // removed or added, the text the same, keeps it open.
    foldOf(h, 'S2', 'lecture').toggle.click();
    assert.deepEqual(foldOf(h, 'S2', 'lecture').state, OPENED);
    await findText(h, 'math');
    assert.deepEqual(foldOf(h, 'S2', 'lecture').state, OPENED, 'a programme removed: still open');
    assert.deepEqual([h.d.getElementById('twLegendDim').hidden, h.d.getElementById('twLegendDimLens').hidden], [false, true], 'the text’s line alone');
    await findText(h, 'math ai');
    assert.deepEqual(foldOf(h, 'S2', 'lecture').state, OPENED, 'a programme added: still open');
    assert.deepEqual(h.errors, []);
  }
});

/* ── The parking strip (design record §7.2 item 6, 6e) ──
   A meeting no column of its kind has (its day is no teaching day, or no
   column starts at its time) is drawn in a strip, the last row of its own
   table, with its day and time; it is toned a warning, its Issues row points
   at it, and the keyboard reaches it in time order. */
const offGrid = (id, extra = {}) => at(id, { day: 'MON', start_time: '18:30', end_time: '19:45', ...extra });
const stripOf = (h, group, kind) => h.d.querySelector(`#twGrid [role="grid"][data-group="${group}"][data-kind="${kind}"] .tw-park-row`);
const stripIds = (h, group, kind) => [...(stripOf(h, group, kind)?.querySelectorAll('.tw-card') || [])].map(c => Number(c.dataset.placementId));

test('PK1 a meeting outside the columns is parked under its table with its day and time; its Issues row finds it', async t => {
  const h = await viewHarness(t, { placements: [at(10), offGrid(11)] });
  await h.select();
  h.w.Element.prototype.scrollIntoView = () => {};  // jsdom has none
  const parked = card(h, 11);
  assert.equal(parked.closest('.tw-cell'), null, 'in no cell');
  const parkRow = stripOf(h, 'S1', 'lecture');
  assert.ok(parkRow && parkRow.contains(parked), 'in its own table’s strip');
  assert.deepEqual([parkRow.getAttribute('role'), parkRow.classList.contains('ux-grid-row')], ['row', true]);
  assert.equal(foldOf(h, 'S1', 'lecture').body.lastElementChild, parkRow, 'the table’s last row, inside the rows the fold hides');
  const head = parkRow.querySelector(':scope > [role="rowheader"]');
  assert.equal(head.textContent, 'Off grid — outside the grid’s columns');
  assert.equal(head.querySelector('.visually-hidden').textContent, ' — outside the grid’s columns', 'only "Off grid" is shown');
  assert.ok(head.classList.contains('tw-park-head') && head.classList.contains('tw-grid-slot-label'));
  const cell = parked.parentElement;
  assert.ok(cell.matches('.tw-park > .tw-park-cell[role="gridcell"]'));
  assert.deepEqual([cell.dataset.uxCol, cell.dataset.focusKey], ['1110', 'park:S1:lecture|11']);
  assert.equal(parked.querySelector('.tw-card-when').innerHTML, 'Mon <bdi dir="ltr">18:30–19:45</bdi>');
  assert.equal(parked.querySelectorAll('.visually-hidden').length, 0, 'its day and time are said once, by the line');
  assert.ok(card(h, 10).querySelector('.visually-hidden'), 'a card in a cell keeps its words for assistive technology');
  assert.equal(card(h, 10).querySelector('.tw-card-when'), null);
  // A warning: ringed, and the strip explained by the legend.
  assert.equal(tone(h, 11), 'warning');
  assert.match(parked.title, /^Lowers quality: Outside the grid’s columns/);
  assert.equal(h.d.getElementById('twLegendPark').textContent, 'Under a table: its meetings outside the columns (M moves one onto the grid)');
  assert.equal(h.d.querySelector('#twLegendPark .ux-legend-swatch').dataset.swatch, 'park');
  assert.equal(parked.draggable, true, 'it drags onto a cell');
  // Its Issues row is a button that finds it: a pulse and the board's tab stop.
  h.w.renderRightPanel('issues');
  const row = [...h.d.querySelectorAll('#twRightBody .tw-issue-row')].find(r => r.querySelector('.tw-issue-kind').textContent === 'Outside the grid’s columns');
  assert.equal(row.tagName, 'BUTTON');
  assert.equal(row.dataset.ids, '["11"]');
  assert.equal(strip(row.querySelector('.tw-issue-detail').textContent), 'Mon 18:30–19:45 · under its table');
  row.focus();
  row.click();
  await settle();
  assert.ok(parked.classList.contains('highlight'));
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')], [parked], 'the board’s one stop');
  assert.equal(h.d.activeElement, row, 'the keyboard stays where it was');
  assert.match(strip(h.d.getElementById('twBoardStatus').textContent), /^Located: Mon 18:30–19:45, AI11 S1\./);
  // The filter: it counts and folds with the rest of its table, and a fold
  // the text shuts is opened by a locate.
  await findText(h, '19:45');
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, NO_FOLD, 'its time matches: its table stays open');
  assert.deepEqual(dimsOf(h, [10, 11]), ['filter', '']);
  await findText(h, 'zzz');
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, FOLDED);
  assert.equal(foldOf(h, 'S1', 'lecture').label, 'Lectures · no match · 2 meetings', 'the parked meeting is one of the table’s');
  assert.deepEqual([...foldOf(h, 'S1', 'lecture').fold.querySelectorAll('.tw-fold-tone:not([hidden]) .tw-fold-n')].map(n => n.textContent), ['1'], 'and its warning is counted');
  h.w.renderRightPanel('issues');
  [...h.d.querySelectorAll('#twRightBody .tw-issue-row')].find(r => r.querySelector('.tw-issue-kind').textContent === 'Outside the grid’s columns').click();
  await settle();
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, OPENED, 'found inside a fold: the fold opens');
  assert.deepEqual(h.errors, []);
});

test('PK2 each meeting is parked under its own table: a lab under its group’s labs, English on a weekend under English; nowhere else', async t => {
  const english = (id, extra) => at(id, { course_code: 'ENG101', course_key: 'ENG101', start_time: '09:00', end_time: '12:00', ...extra });
  const h = await viewHarness(t, { placements: [
    at(10),
    at(12, { course_code: 'PHYS1', section: 'S2', term_section_id: 212 }),
    at(13, { course_code: 'CS102', section: 'S2', term_section_id: 213, start_time: '16:00', end_time: '17:40' }),  // a lab at no lab column
    english(14, { day: 'SUN' }),
    english(15, { day: 'SAT', start_time: '14:00', end_time: '17:00' }),  // a weekend English meeting
  ] });
  await h.select();
  assert.deepEqual(stripIds(h, 'S2', 'lab'), [13], 'S2’s labs');
  assert.deepEqual(stripIds(h, 'S1', 'english'), [15], 'English, on a day the grid has no row for');
  assert.equal(h.d.querySelectorAll('#twGrid .tw-park-row').length, 2, 'no strip elsewhere');
  assert.equal(card(h, 14).closest('.tw-cell').dataset.focusKey, 'cell:S1:english|SUN|09:00', 'the weekday one in its cell');
  // English columns come from its meetings on the grid: the weekend one adds none.
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [data-kind="english"] .tw-slot-time')].map(e => e.textContent), ['09:00–12:00', '13:00–16:00']);
  assert.equal(card(h, 15).querySelector('.tw-card-when').innerHTML, 'Sat <bdi dir="ltr">14:00–17:00</bdi>');
  assert.match(issuesText(h), /Outside the grid’s columnsENG101 S1Sat 14:00–17:00 · under its table/);
  // A board with no lab column: its labs are still drawn, under a lab table of their own.
  const bare = await viewHarness(t, { labs: [], placements: [at(10), at(13, { start_time: '16:00', end_time: '17:40' })] });
  await bare.select();
  assert.deepEqual(stripIds(bare, 'S1', 'lab'), [13]);
  const table = bare.d.querySelector('#twGrid [role="grid"][data-kind="lab"]');
  assert.equal(table.style.getPropertyValue('--tw-slots'), '1', 'one column to lay the strip out in');
  assert.equal(table.querySelectorAll('.tw-cell').length, 0);
});

test('PK3 a board with nothing off the grid has no strip and no strip in its legend; one moved back loses it', async t => {
  const h = await viewHarness(t, { placements: [at(10), offGrid(11)] });
  await h.select();
  assert.equal(h.d.querySelectorAll('#twGrid .tw-park-row').length, 1);
  h.w.TW.placements = [at(10), at(11, { day: 'MON' })];
  h.w.renderGrid();
  assert.equal(h.d.querySelectorAll('#twGrid .tw-park-row, #twGrid .tw-park, #twGrid .tw-park-cell').length, 0);
  assert.equal(h.d.getElementById('twLegendPark'), null);
  assert.ok(card(h, 11).closest('.tw-cell'));
  const none = await viewHarness(t, { placements: [] });
  await none.select();
  assert.equal(none.d.querySelectorAll('#twGrid .tw-park-row').length, 0);
});

test('PK4 the keyboard: ↓ from the last day lands on the parked card nearest in time; ←/→ walk the strip; it is no drop target', async t => {
  const h = await viewHarness(t, { placements: [
    at(10),
    offGrid(11),                                                   // MON 18:30
    offGrid(12, { day: 'SAT', start_time: '09:00', end_time: '10:15' }),  // SAT 09:00
    offGrid(14, { day: 'SUN', start_time: '08:00', end_time: '09:15' }),  // SUN 08:00
  ] });
  await h.select();
  assert.deepEqual(stripIds(h, 'S1', 'lecture'), [14, 11, 12], 'by day, then start');
  assert.deepEqual([...stripOf(h, 'S1', 'lecture').querySelectorAll('.tw-park-cell')].map(c => c.dataset.uxCol), ['480', '1110', '540']);
  const nav = boardNavOf(h);
  nav.active('cell:S1:lecture|THU|09:00', { focus: true });
  keyOn(h, 'ArrowDown');
  assert.equal(h.d.activeElement, card(h, 12), 'THU 09:00 → the parked meeting at 09:00 (SAT), not the strip’s first');
  keyOn(h, 'ArrowLeft');
  assert.equal(h.d.activeElement, card(h, 11));
  keyOn(h, 'ArrowLeft');
  assert.equal(h.d.activeElement, card(h, 14));
  keyOn(h, 'ArrowLeft');
  assert.equal(h.d.activeElement, card(h, 14), 'the strip’s start');
  keyOn(h, 'ArrowRight');
  assert.equal(h.d.activeElement, card(h, 11));
  keyOn(h, 'ArrowUp');
  assert.equal(h.d.activeElement.dataset.focusKey, 'cell:S1:lecture|THU|09:00', '↑: back to the last day, nearest in time');
  keyOn(h, 'ArrowDown');
  keyOn(h, 'PageDown');
  assert.equal(h.d.activeElement.closest('[role="grid"]').dataset.kind, 'lab', 'PageDown: the next table');
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')].length, 1, 'still one stop');
  // M and R act on a parked card as on any other.
  const acted = [];
  h.w.TW.keyActions.move = p => acted.push(['move', p.id]);
  h.w.TW.keyActions.room = p => acted.push(['room', p.id]);
  nav.active('placement:12', { focus: true });
  keyOn(h, 'm');
  keyOn(h, 'r');
  assert.deepEqual(acted, [['move', 12], ['room', 12]]);
  // No drop target: a drag over the strip is the browser's (no drop), over a cell the page's.
  const over = target => { const event = new h.w.Event('dragover', { bubbles: true, cancelable: true }); target.dispatchEvent(event); return event.defaultPrevented; };
  const parkRow = stripOf(h, 'S1', 'lecture');
  assert.deepEqual([over(parkRow.querySelector('.tw-park-cell')), over(parkRow.querySelector('.tw-park')), over(parkRow.querySelector('.tw-park-head'))], [false, false, false]);
  assert.equal(over(h.d.querySelector('#twGrid .tw-cell')), true, 'a cell takes the drop');
});

test('PK5 the grid rule and the renderer agree with the shared cases: a meeting is in a cell exactly when it is on the grid, else in a strip', async t => {
  const table = JSON.parse(fs.readFileSync(path.join(root, 'tests/fixtures/grid_rule_cases.json'), 'utf8'));
  const rows = table.cases.map((c, i) => at(100 + i, { course_code: c.course, course_key: c.course, term_section_id: 300 + i, day: c.day, start_time: c.start, end_time: c.end }));
  const h = await harness(t, url => (url === '/ops/tw/boards/2/' ? { placements: rows }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms
    : url.includes('/readiness/') ? { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } } : undefined),
  { effective_slot_config: table.lecture_slots, effective_lab_slot_config: table.lab_slots });
  await h.select();
  const offGridRows = issuesText(h).split('Outside the grid’s columns').length - 1;
  table.cases.forEach((c, i) => {
    const drawn = card(h, 100 + i);
    const cell = drawn.closest('.tw-cell');
    assert.equal(h.w.isPlacementOnGrid(rows[i]), c.on_grid, `rule: ${c.name}`);
    assert.equal(Boolean(cell), c.on_grid, `in a cell: ${c.name}`);
    if (cell) assert.equal(h.w.cellKey(cell.dataset.day, cell.dataset.start), h.w.cellKey(c.day, c.start), `the right cell: ${c.name}`);
    else assert.ok(drawn.closest('.tw-park-cell'), `parked: ${c.name}`);
  });
  assert.equal(h.d.querySelectorAll('#twGrid .tw-card').length, rows.length, 'each drawn once');
  assert.equal(offGridRows, table.cases.filter(c => !c.on_grid).length, 'an Issues row for each parked one');
  // The key itself, as the server reads a time.
  assert.deepEqual(['9:00', '09:00:00', ' 9:05 ', '24:00', '9am', '', null].map(v => h.w.cellKey('sun', v)),
    ['SUN|09:00', 'SUN|09:00', 'SUN|09:05', null, null, null, null]);
});

test('PK7 the strip in Arabic: the time left to right, Friday and Saturday named, any other day isolated; the words', async t => {
  for (const arabic of [false, true]) {
    const h = await viewHarness(t, { arabic, placements: [
      at(10),
      offGrid(11),
      offGrid(12, { day: 'SAT', start_time: '09:00', end_time: '10:15' }),
      offGrid(13, { day: 'FRI', start_time: '09:00', end_time: '10:15' }),
      offGrid(14, { day: 'X<y', start_time: '09:00', end_time: '10:15' }),
    ] });
    await h.select();
    h.w.Element.prototype.scrollIntoView = () => {};
    const when = id => card(h, id).querySelector('.tw-card-when').innerHTML;
    assert.equal(when(11), arabic ? 'الاثنين <bdi dir="ltr">18:30–19:45</bdi>' : 'Mon <bdi dir="ltr">18:30–19:45</bdi>');
    assert.equal(when(12), arabic ? 'السبت <bdi dir="ltr">09:00–10:15</bdi>' : 'Sat <bdi dir="ltr">09:00–10:15</bdi>');
    assert.equal(when(13), arabic ? 'الجمعة <bdi dir="ltr">09:00–10:15</bdi>' : 'Fri <bdi dir="ltr">09:00–10:15</bdi>');
    assert.equal(when(14), '<bdi>X&lt;y</bdi> <bdi dir="ltr">09:00–10:15</bdi>', 'an unknown day: escaped and isolated');
    assert.deepEqual(stripIds(h, 'S1', 'lecture'), [11, 13, 12, 14], 'MON, FRI, SAT, then any other day');
    assert.equal(stripOf(h, 'S1', 'lecture').querySelector('.tw-park-head').textContent,
      arabic ? 'خارج الشبكة — خارج أعمدة الجدول' : 'Off grid — outside the grid’s columns');
    assert.equal(h.d.getElementById('twLegendPark').textContent, arabic
      ? 'أسفل الجدول: لقاءاته خارج الأعمدة (المفتاح M ينقل أحدها إلى الجدول)'
      : 'Under a table: its meetings outside the columns (M moves one onto the grid)');
    h.w.renderRightPanel('issues');
    const details = [...h.d.querySelectorAll('#twRightBody .tw-issue-row')]
      .filter(r => r.querySelector('.tw-issue-kind').textContent === (arabic ? 'خارج أعمدة الجدول' : 'Outside the grid’s columns'))
      .map(r => r.querySelector('.tw-issue-detail').innerHTML);
    assert.deepEqual(details, arabic ? [
      '<bdi>الاثنين <bdi dir="ltr">18:30–19:45</bdi> · أسفل جدوله</bdi>',
      '<bdi>السبت <bdi dir="ltr">09:00–10:15</bdi> · أسفل جدوله</bdi>',
      '<bdi>الجمعة <bdi dir="ltr">09:00–10:15</bdi> · أسفل جدوله</bdi>',
      '<bdi>\u2066X&lt;y\u2069 <bdi dir="ltr">09:00–10:15</bdi> · أسفل جدوله</bdi>',
    ] : [
      '<bdi>Mon <bdi dir="ltr">18:30–19:45</bdi> · under its table</bdi>',
      '<bdi>Sat <bdi dir="ltr">09:00–10:15</bdi> · under its table</bdi>',
      '<bdi>Fri <bdi dir="ltr">09:00–10:15</bdi> · under its table</bdi>',
      '<bdi>\u2066X&lt;y\u2069 <bdi dir="ltr">09:00–10:15</bdi> · under its table</bdi>',
    ]);
    // Where else the page names the day: Selection, the locate, Move.
    card(h, 12).click();
    const day = h.d.querySelector('#twRightBody .tw-sel-fields dd').innerHTML;
    assert.equal(day, arabic ? 'السبت' : 'Sat');
    const p = h.w.TW.placements.find(row => row.id === 12);
    assert.ok(h.w.moveSubHtml(p).startsWith(arabic ? 'السبت <bdi dir="ltr">09:00–10:15</bdi>. ' : 'Saturday <bdi dir="ltr">09:00–10:15</bdi>. '), 'Move names the day in full');
    assert.equal(h.w.moveSubHtml(h.w.TW.placements.find(row => row.id === 14)).startsWith('\u2066X&lt;y\u2069 '), true, 'Move isolates an unknown day');
    card(h, 14).click();
    assert.equal(h.d.querySelector('#twRightBody .tw-sel-fields dd').innerHTML, '<bdi>X&lt;y</bdi>');
    h.w.renderRightPanel('issues');
    [...h.d.querySelectorAll('#twRightBody .tw-issue-row')].find(r => r.dataset.ids === '["12"]').click();
    await settle();
    assert.match(h.d.getElementById('twBoardStatus').textContent, arabic
      ? /^عُثر عليها: السبت \u206609:00–10:15\u2069، /
      : /^Located: Sat \u206609:00–10:15\u2069, /);
  }
});

test('PK7b Selection’s meetings list names each day as the grid does; an empty day is a dash on the card, in Issues, Selection and Move', async t => {
  for (const arabic of [false, true]) {
    const meetings = ['SUN', 'SAT', 'X<y', ''].map(day => ({ day, start_time: '09:00', end_time: '10:15', room: 'R1' }));
    const h = await viewHarness(t, { arabic, placements: [at(10, { meetings }), offGrid(11, { day: '' })] });
    await h.select();
    card(h, 10).click();
    assert.deepEqual([...h.d.querySelectorAll('#twRightBody .tw-sel-meetings li > span:first-child')].map(s => s.innerHTML),
      [arabic ? 'الأحد' : 'Sun', arabic ? 'السبت' : 'Sat', '<bdi>X&lt;y</bdi>', '—']);
    // A meeting with no day is parked, and its day reads as a dash wherever it is said.
    assert.deepEqual(stripIds(h, 'S1', 'lecture'), [11]);
    assert.equal(card(h, 11).querySelector('.tw-card-when').innerHTML, '— <bdi dir="ltr">18:30–19:45</bdi>');
    card(h, 11).click();
    assert.equal(h.d.querySelector('#twRightBody .tw-sel-fields dd').innerHTML, '—');
    assert.ok(h.w.moveSubHtml(h.w.TW.placements.find(row => row.id === 11)).startsWith('— <bdi dir="ltr">18:30–19:45</bdi>.'), 'Move');
    h.w.renderRightPanel('issues');
    assert.equal([...h.d.querySelectorAll('#twRightBody .tw-issue-row')].find(r => r.dataset.ids === '["11"]').querySelector('.tw-issue-detail').innerHTML,
      `<bdi>— <bdi dir="ltr">18:30–19:45</bdi> · ${arabic ? 'أسفل جدوله' : 'under its table'}</bdi>`);
    assert.deepEqual(h.errors, []);
  }
});

test('PK4b the strip’s order: by day, then start (a start that is no time after every time), then course; any other day last', async t => {
  const h = await viewHarness(t, { placements: [
    at(10),
    offGrid(17, { day: 'ZZZ' }),
    offGrid(12, { day: 'SAT', start_time: '09:00', end_time: '10:15' }),
    offGrid(15, { start_time: '7pm' }),
    offGrid(18, { day: 'ABC' }),
    offGrid(21, { day: 'SAT', start_time: '09:00', end_time: '10:15', course_code: 'AA9' }),  // the same course and time: by id
    offGrid(16, { day: 'SAT', start_time: '09:00', end_time: '10:15', course_code: 'AA9' }),
    offGrid(11),
    offGrid(19, { course_code: 'ZZ1', start_time: '07:30', end_time: '08:45' }),  // a later course, earlier on the day: by start
  ] });
  await h.select();
  assert.deepEqual(stripIds(h, 'S1', 'lecture'), [19, 11, 15, 16, 21, 12, 18, 17]);
  assert.deepEqual([...stripOf(h, 'S1', 'lecture').querySelectorAll('.tw-park-cell')].map(c => c.dataset.uxCol),
    ['450', '1110', '1440', '540', '540', '540', '1110', '1110'], 'in minutes; no time sits after every time');
  assert.equal(card(h, 15).querySelector('.tw-card-when').innerHTML, 'Mon <bdi dir="ltr">7pm–19:45</bdi>', 'the stored time as it is');
});

test('PK5b two columns with one start (their ends differ, the server keeps both): a meeting there is drawn once, in the first', async t => {
  const h = await harness(t, url => (url === '/ops/tw/boards/2/' ? { placements: [at(10)] }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms
    : url.includes('/readiness/') ? { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } } : undefined),
  { effective_slot_config: [{ label: 'A', start: '09:00', end: '10:15' }, { label: 'B', start: '09:00', end: '10:40' }], effective_lab_slot_config: [] });
  await h.select();
  assert.equal(h.d.querySelectorAll('#twGrid .tw-card').length, 1, 'once (it used to be drawn in both)');
  assert.equal(card(h, 10).closest('.tw-cell').dataset.end, '10:15');
  assert.equal(h.d.querySelectorAll('#twGrid .tw-park-row').length, 0);
});

test('PK4c a parked card drags onto a cell as any card does: the drop moves it to that cell’s time, and the strip goes', async t => {
  const rows = [at(10), offGrid(11)];
  const posts = [];
  const h = await harness(t, (url, opts) => {
    if (url === '/ops/tw/placements/11/move/' && opts.method === 'POST') {
      posts.push(JSON.parse(opts.body));
      rows[1] = { ...rows[1], ...posts.at(-1) };
      return { ok: true, placement: rows[1] };
    }
    if (url === '/ops/tw/boards/2/') return { placements: rows.map(row => ({ ...row })) };
    if (url.includes('/conflicts/')) return quiet;
    if (url.includes('/quality/')) return noRooms;
    if (url.includes('/readiness/')) return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    return undefined;
  });
  await h.select();
  const data = {};
  const dragstart = new h.w.Event('dragstart', { bubbles: true, cancelable: true });
  dragstart.dataTransfer = { setData: (type, value) => { data[type] = value; }, effectAllowed: '' };
  card(h, 11).dispatchEvent(dragstart);
  assert.equal(dragstart.defaultPrevented, false, 'a parked card drags');
  assert.deepEqual(JSON.parse(data['text/plain']), { type: 'move', placement_id: 11 });
  await h.w.onDrop({ preventDefault() {}, currentTarget: h.d.querySelector('#twGrid .tw-cell[data-day="MON"][data-start="09:00"]'),
    dataTransfer: { getData: () => data['text/plain'] } });
  assert.deepEqual(posts, [{ day: 'MON', start_time: '09:00', end_time: '10:15' }], 'the cell’s time, by its time alone');
  assert.deepEqual([card(h, 11).closest('.tw-cell')?.dataset.focusKey, h.d.querySelectorAll('#twGrid .tw-park-row').length],
    ['cell:S1:lecture|MON|09:00', 0]);
});

/* ── Programme mark in Selection (PR 4b) ── */
const LENS = (sections, programme_view = null) => ({ plan_lens: { plans: ['AI', 'AI2'], courses: { CS112: { plans: { AI2: 4 } }, CS113: { plans: { AI: 9 } } },
  sections, programme_grouping: { students_regrouped: 3, student_gap_minutes_change: -20 }, seating: { assessed: true, students: 44 }, programme_view } });
const markRows = () => [
  at(10, { course_key: 'CS112', preferred_programme: '', preferred_programme_editable: true }),
  at(13, { day: 'WED', course_key: 'CS112', term_section_id: 110, preferred_programme: '', preferred_programme_editable: true }),  // its other meeting
  at(11, { day: 'MON', course_key: 'CS113', preferred_programme: 'CYS', preferred_programme_editable: false, term_section_id: 111 }),
  at(12, { day: 'TUE', course_key: 'CS114' }),  // the feature off for this one: no key
];
async function markHarness(t, { sections = () => ({ 110: { actual_total: 38, actual_plans: { AI2: 30, AI: 6 } } }), lens = null,
  answer = null, hold = {}, extra = {}, arabic = false, rows = markRows() } = {}) {
  // `lens` counts the quality reads that ask for the lens; `quality` every one.
  const seen = { posts: [], lens: 0, quality: 0, readiness: 0 };
  const h = await harness(t, async (url, opts) => {
    if (url === '/ops/tw/boards/2/') return { placements: rows, slot_config: scenario.slot_config };
    if (url.includes('/quality/') && url.endsWith('?lens=1')) {
      seen.quality++;
      const n = ++seen.lens;
      if (hold.lens) await hold.lens(n);
      const body = lens ? lens(n) : LENS(sections());
      return body.httpError ? body : { ...noRooms, ...body };
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
  busy.disabled = false; busy.click(); h.w.refreshWorkspaceQuality(true, { lens: true });  // a second ask joins the read in flight
  await settle();
  assert.equal(h.seen.lens, before + 1, 'one read');
  held.resolve();
  await wait(() => /40: AI2 40/.test(h.body().textContent));
  assert.doesNotMatch(h.body().textContent, /From before your last change/);
  assert.equal(h.d.getElementById('twInspectorStatus').textContent, 'Programme mix updated.');
  assert.ok(h.body().contains(h.d.activeElement), 'the keyboard stays in the panel');
});

test('LN12 a Refresh overtaken by a plain read (an edit made meanwhile) never says the mix was updated, but how old it is; by a read with the lens, it was; by a reset, none is held', async t => {
  for (const arabic of [false, true]) {
    const holds = {};
    // A programme view, so that AI2 typed is a programme.
    const view = { programmes: ['AI', 'AI2'], roles: { AI2: { main: [10, 13] } }, shared_roles: {}, placement_ids: [10, 11, 12, 13], seating_assessed: true };
    const h = await markHarness(t, { arabic, lens: () => LENS({ 110: { actual_total: 38, actual_plans: { AI2: 30, AI: 6 } } }, view),
      hold: { lens: n => holds[n]?.promise } });
    const status = h.d.getElementById('twInspectorStatus');
    card(h, 10).click();
    h.w.TW.dataVersion++;  // an edit
    h.w.renderRightPanel('selection');
    const [lens0, quality0] = [h.seen.lens, h.seen.quality];
    holds[lens0 + 1] = deferred();
    status.textContent = '';
    h.body().querySelector('[data-tw-lens-refresh]').click();
    await settle();
    assert.equal(h.seen.lens, lens0 + 1, 'the Refresh reads the lens');
    // Another edit while it reads, with no programme in the filter: the
    // board's read is plain and overtakes it.
    h.w.TW.dataVersion++;
    assert.equal(h.w.lensWanted(), false);
    await h.w.refreshWorkspaceQuality(false, { lens: h.w.lensWanted() });
    assert.deepEqual([h.seen.lens, h.seen.quality], [lens0 + 1, quality0 + 2], 'a plain read');
    holds[lens0 + 1].resolve();
    await wait(() => status.textContent !== '');
    assert.equal(status.textContent, arabic ? 'توزيع البرامج من قبل آخر تعديل.' : 'The programme mix is from before your last change.');
    assert.notEqual(h.w.TW.planLens.version, h.w.TW.dataVersion, 'the mix held is from before the edit');
    assert.match(h.body().textContent, arabic ? /من قبل آخر تعديل/ : /From before your last change/, 'as Selection says');
    assert.equal(h.body().querySelector('[data-tw-lens-refresh]')?.disabled, false, 'with its Refresh');
    // Overtaken by a read with the lens (a programme typed meanwhile), the
    // mix is of the data now: updated.
    holds[lens0 + 2] = deferred();
    status.textContent = '';
    h.body().querySelector('[data-tw-lens-refresh]').click();
    await settle();
    h.w.TW.dataVersion++;
    await findText(h, 'AI2');
    await wait(() => h.seen.lens === lens0 + 3 && h.w.TW.planLens.version === h.w.TW.dataVersion);
    assert.equal(status.textContent, '', 'nothing said before the Refresh ends');
    holds[lens0 + 2].resolve();
    await wait(() => status.textContent !== '');
    assert.equal(status.textContent, arabic ? 'حُدِّث توزيع البرامج.' : 'Programme mix updated.');
    // Overtaken by a reset (a reload after a planner job forgets the lens and
    // the read in flight): no mix is held, as Selection says.
    h.w.TW.dataVersion++;
    h.w.renderRightPanel('selection');
    holds[lens0 + 4] = deferred();
    status.textContent = '';
    h.body().querySelector('[data-tw-lens-refresh]').click();
    await settle();
    assert.equal(h.seen.lens, lens0 + 4);
    h.w.resetPlanLens();
    holds[lens0 + 4].resolve();
    await wait(() => status.textContent !== '');
    const none = arabic ? 'توزيع البرامج غير متاح.' : 'The programme mix is not available.';
    assert.equal(status.textContent, none);
    h.w.renderRightPanel('selection');
    assert.ok(h.body().textContent.includes(none), h.body().textContent);
    assert.deepEqual(h.errors, []);
  }
});

test('an older lens answer never replaces a newer one', async t => {
  const first = deferred();
  const h = await markHarness(t, { lens: n => LENS({ 110: { actual_total: n, actual_plans: { AI2: n } } }),
    hold: { lens: n => (n === 2 ? first.promise : null) } });
  const older = h.w.refreshWorkspaceQuality(true, { lens: true });  // the second read, held
  h.w.TW.dataVersion++;
  await h.w.refreshWorkspaceQuality(true, { lens: true });  // the third, about newer data, answers first
  first.resolve();
  await older;
  assert.equal(h.w.TW.planLens.version, h.w.TW.dataVersion);
  assert.equal(h.w.TW.planLens.sections['110'].actual_total, 3, 'the newer read stands');
  assert.equal(h.w.TW.planLens.loading, false);
});

test('LN7 saving posts once, keeps the keyboard, reads the quality once, with the lens, and the publish check', async t => {
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
  for (let i = 0; i < 10; i++) await settle();
  assert.equal(h.seen.quality, quality0 + 1, 'one quality read: the one with the lens (LN7)');
  assert.equal(h.requests.filter(r => r.url.includes('/plan-lens/')).length, 0, 'never /plan-lens/');
  assert.equal(h.body().querySelector('[data-tw-lens-refresh]'), null, 'fresh: no Refresh');
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

test('a programme in the filter dims anew, in place, when the lens is read again', async t => {
  let ai2 = [];
  const view = () => ({ programmes: ['AI', 'AI2'], roles: { AI: { main: [10] }, AI2: { main: ai2 } }, shared_roles: {},
    placement_ids: [10, 11, 12, 13], seating_assessed: true });
  const h = await markHarness(t, { lens: () => LENS({ 110: { actual_total: 1, actual_plans: { AI: 1 } } }, view()) });
  const lens0 = h.seen.lens;
  await findText(h, 'AI2');
  const node = card(h, 10);
  assert.equal(node.dataset.dim, 'lens', 'in no AI2 file yet: dimmed by the lens, still on the board');
  ai2 = [10];
  h.w.TW.dataVersion++;
  await h.w.refreshPlanLens();
  assert.equal(h.seen.lens, lens0 + 1);
  assert.equal(card(h, 10), node, 'a new lens dims anew in place: the same card');
  assert.equal(node.dataset.dim, undefined, 'the new roles put it in AI2');
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
  const first = h.w.refreshWorkspaceQuality(true, { lens: true });  // read 2, held
  assert.equal(h.w.TW.planLens.loading, true);
  const joined = h.w.refreshWorkspaceQuality(true, { lens: true });
  const plain = h.w.refreshWorkspaceQuality(true, { lens: false });  // a read with no lens needed joins one with it
  assert.equal(h.seen.lens, 2, 'the same data: one read');
  assert.equal(h.seen.quality, 2, 'and no plain read beside it');
  h.w.TW.scenarioId = 2;  // another scenario, the same data version
  await h.w.refreshWorkspaceQuality(true, { lens: true });
  assert.equal(h.seen.lens, 3, 'a read for another scenario is its own');
  held.resolve();
  await Promise.all([first, joined, plain]);
});

test('a reset (the scenario reloaded after a planner job) forgets the read in flight', async t => {
  const held = deferred();
  const h = await markHarness(t, { lens: n => LENS({ 110: { actual_total: n, actual_plans: { AI: n } } }), hold: { lens: n => (n === 2 ? held.promise : null) } });
  const old = h.w.refreshWorkspaceQuality(true, { lens: true });  // read 2, held; the data version does not move on a reload
  h.w.resetPlanLens();
  const fresh = h.w.refreshWorkspaceQuality(true, { lens: true });
  assert.equal(h.seen.lens, 3, 'a new read, never the old one');
  await fresh;
  held.resolve();
  await old;
  assert.equal(h.w.TW.planLens.sections['110'].actual_total, 3, 'the forgotten read does not land');
  // A read forgotten before any other starts lands nowhere either: no lens,
  // no quality (the reload reads both again).
  const late = deferred();
  const r = await markHarness(t, { lens: n => LENS({ 110: { actual_total: n, actual_plans: { AI: n } } }), hold: { lens: n => (n === 2 ? late.promise : null) } });
  r.w.TW.quality = null;
  const forgotten = r.w.refreshWorkspaceQuality(true, { lens: true });
  r.w.resetPlanLens();
  late.resolve();
  await forgotten;
  assert.deepEqual([r.w.TW.planLens.loaded, r.w.TW.planLens.failed, r.w.TW.quality, r.w.TW.qualityLoading], [false, undefined, null, false]);
});

test('an older read finishing never clears the newer read’s loading, and its caller waits for the newer one', async t => {
  const older = deferred(), newer = deferred();
  const h = await markHarness(t, { hold: { lens: n => (n === 2 ? older.promise : n === 3 ? newer.promise : null) } });
  let done = false;
  const a = h.w.refreshWorkspaceQuality(true, { lens: true }).then(() => { done = true; });
  h.w.TW.dataVersion++;
  const b = h.w.refreshWorkspaceQuality(true, { lens: true });
  older.resolve();
  for (let i = 0; i < 10; i++) await settle();
  assert.equal(h.w.TW.planLens.loading, true, 'the newer read is still loading');
  assert.equal(h.w.TW.qualityLoading, true);
  assert.equal(done, false, 'the older read’s caller waits for the read that overtook it');
  newer.resolve();
  await Promise.all([a, b]);
  assert.equal(done, true);
  assert.deepEqual([h.w.TW.planLens.loading, h.w.TW.qualityLoading], [false, false]);
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

/* ── The lens from the quality read (design record §7.2 item 6, 6d) ──
   The plan lens comes with the quality read (`quality/?lens=1`) while it is
   wanted, never from /plan-lens/; a programme in the filter dims what its
   per-plan file leaves out, by the server's roles (`programme_view`), and the
   page holds no copy of that rule. */
const lensRows = () => [
  at(10, { course_code: 'CS101', preferred_programme: '', preferred_programme_editable: true }),
  at(11, { course_code: 'MATH1', day: 'MON' }),
  at(12, { course_code: 'PHYS1', day: 'TUE' }),
  at(13, { course_code: 'BIO1', day: 'WED' }),
  at(14, { course_code: 'ENG1', day: 'THU' }),
];
// AI2's file: CS101 its own, MATH1 another programme's section it seats two
// in, BIO1 mixed, ENG1 outside its plan; PHYS1 is in DS's file alone.
const LENS_VIEW = () => ({
  programmes: ['AI2', 'DS'],
  roles: { AI2: { main: [10], mixed: [13], other: [11], outside: [14] }, DS: { main: [11, 12] } },
  shared_roles: { AI2: { main: [11] }, DS: { main: [11] } },
  placement_ids: [10, 11, 12, 13, 14],
  seating_assessed: true,
});
// PHYS1 is allocated to AI2 yet in no AI2 file; BIO1 is a shared section that
// the SHARED classification gives AI2 no role in: the roles decide, never the
// allocation or the shared flag.
const LENS_SECTIONS = () => ({
  110: { actual_plans: { AI2: 20, DS: 0 }, actual_total: 20, filter_plans: ['AI2'] },
  111: { actual_plans: { AI2: 2, DS: 28 }, actual_total: 30, filter_plans: ['DS'], shared: true },
  112: { actual_plans: { AI2: 0, DS: 25 }, actual_total: 25, filter_plans: ['AI2'] },
  113: { actual_plans: { AI2: 5, DS: 7 }, actual_total: 12, filter_plans: ['AI2'], shared: true },
  114: { actual_plans: { AI2: 0, DS: 0 }, actual_total: 0, filter_plans: ['AI2'] },
});
const lensAnswer = ({ view = LENS_VIEW(), sections = LENS_SECTIONS(), courses = {}, plans = ['AI2', 'DS'], assessed = true } = {}) => ({
  plans, courses, sections,
  seating: assessed ? { assessed: true, students: 57 } : { assessed: false, students: 0, reason: 'assessment_failed' },
  programme_view: view && { ...view, seating_assessed: assessed },
});
const ALL = [10, 11, 12, 13, 14];
/* `reads` names each quality read: 'lens' (with `?lens=1`) or 'plain'.
   `hold(n, lens)` may hold the n-th; `answer(n)` is the n-th lens (null:
   `plan_lens: null`; an httpError: the read fails). */
async function lensHarness(t, { rows = lensRows(), answer = () => lensAnswer(), hold = null, arabic = false, boards = [board], budget = [], select = true } = {}) {
  const reads = [];
  const h = await harness(t, async url => {
    if (url === '/ops/tw/boards/2/') return { placements: rows.map(row => ({ ...row })), slot_config: scenario.slot_config };
    if (url.startsWith('/ops/tw/boards/?')) return { boards };
    if (url.includes('/budget/')) return { budget };
    if (url.includes('/conflicts/')) return quiet;
    if (url.includes('/readiness/')) return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    if (!url.includes('/quality/')) return undefined;
    const lens = url.endsWith('/quality/?lens=1');
    reads.push(lens ? 'lens' : 'plain');
    const n = reads.length;
    if (hold) await hold(n, lens);
    if (!lens) return noRooms;
    const body = answer(n);
    return body?.httpError ? body : { ...noRooms, plan_lens: body };
  }, {}, arabic);
  if (select) await h.select();
  return Object.assign(h, { reads, rows });
}
// An edit's end: the data moved on and the board is read again.
const edit = async h => { h.w.TW.dataVersion++; await h.w.selectBoard(2); };
const planLensReads = h => h.requests.filter(r => r.url.includes('/plan-lens/')).length;

test('LN1 the lens comes with the quality read: on open, while the filter names a programme, never on a read of its own', async t => {
  const h = await lensHarness(t);
  assert.deepEqual(h.requests.filter(r => r.url.includes('/quality/')).map(r => r.url), ['/ops/tw/scenarios/1/quality/?lens=1'],
    'open: the first quality read brings the lens');
  await edit(h);
  assert.deepEqual(h.reads, ['lens', 'plain'], 'an edit with no programme in the filter: a plain read');
  await findText(h, 'cs1');
  await settle();
  assert.equal(h.reads.length, 2, 'text alone reads nothing');
  await findText(h, 'ai2');
  await wait(() => h.reads.length === 3 && !h.w.TW.planLens.loading);
  assert.equal(h.reads[2], 'lens', 'a programme typed while the lens is from before an edit: one read, with the lens');
  await findText(h, 'ds');
  await findText(h, 'ai2 ds shared');
  await settle();
  assert.equal(h.reads.length, 3, 'another programme, the roles fresh: nothing read');
  await edit(h);
  await edit(h);
  assert.deepEqual(h.reads.slice(3), ['lens', 'lens'], 'with a programme in the filter, every quality read has the lens');
  // A board switch reuses the quality and the lens.
  await h.w.selectBoard(2);
  assert.equal(h.reads.length, 5);
  await findText(h, 'math');
  await edit(h);
  assert.deepEqual(h.reads.slice(5), ['plain'], 'the programme gone: plain again');
  // The quality is fresh but the lens is not: asking for the lens reads it; asked again, nothing.
  await h.w.refreshWorkspaceQuality(false, { lens: true });
  await h.w.refreshWorkspaceQuality(false, { lens: true });
  assert.deepEqual(h.reads.slice(6), ['lens']);
  assert.equal('plan_lens' in h.w.TW.quality, false, 'the lens is never part of the quality');
  assert.equal(h.w.TW.planLens.version, h.w.TW.dataVersion);
  assert.equal(planLensReads(h), 0, 'never /plan-lens/');
  assert.deepEqual(h.notices.filter(n => n.type === 'error'), []);
  assert.deepEqual(h.errors, []);
});

test('LN1b a scenario generated in place never shows the one before it: its board is drawn with no lens, and its first quality read brings its own', async t => {
  const generated = { ...scenario, id: 7, name: 'Generated' };
  const courses = { 'CS112::PROGRAMMING_I': {}, 'CS112::PROGRAMMING_II': {} };
  const reads = [];
  const h = await harness(t, url => {
    if (url === '/ops/tw/generate-workspace/') return { scenario: generated, boards: [board], build_summary: {}, seed: 1 };
    if (url === '/ops/tw/boards/2/') return { placements: [at(10, { course_code: 'CS112', course_key: 'CS112::PROGRAMMING_I', course_name: 'Programming I' })], slot_config: scenario.slot_config };
    if (url.includes('/quality/')) {
      reads.push(url);
      return url.endsWith('?lens=1') ? { ...noRooms, plan_lens: { plans: [], sections: {}, courses: url.includes('/scenarios/1/') ? courses : {} } } : noRooms;
    }
    if (url.includes('/conflicts/')) return quiet;
    return undefined;
  });
  await h.select();
  assert.equal(card(h, 10).querySelector('.tw-card-name')?.textContent, 'Programming I', 'scenario 1’s lens names it');
  const names = [];
  const draw = h.w.renderGridWithSlots;
  h.w.renderGridWithSlots = (...args) => { draw(...args); names.push(card(h, 10)?.querySelector('.tw-card-name')?.textContent || ''); };
  await h.w.runGenerate('classic', GENERATE);
  assert.deepEqual(names, [''], 'the new board is drawn without the old scenario’s lens');
  assert.ok(reads.includes('/ops/tw/scenarios/7/quality/?lens=1'), JSON.stringify(reads));
  assert.deepEqual([h.w.TW.planLens.loaded, Object.keys(h.w.TW.planLens.courses)], [true, []], 'its own lens');
});

test('LN2 a programme dims what its per-plan file leaves out, by the server’s roles, never by the allocation; each card’s title says its role', async t => {
  for (const arabic of [false, true]) {
    const h = await lensHarness(t, { arabic });
    await findText(h, 'ai2');
    assert.deepEqual(dimsOf(h, ALL), ['', '', 'lens', '', ''],
      'PHYS1 (allocated to AI2, in no AI2 file) is dimmed; MATH1 (two AI2 seated, mainly DS’s: "other") is not');
    const P = iso('AI2');
    const titles = arabic ? [
      `${P}: شعبة خاصة به`,
      `${P}: فيها 2 من 30؛ ومعظمها لبرنامج آخر`,
      `ليست في جدول ${P}`,
      `${P}: شعبة مختلطة (5 من 12)`,
      `${P}: خارج خطته؛ يذكر ملفه المقرر بين المقررات المطروحة`,
    ] : [
      `${P}: its own section`,
      `${P}: seats 2 of 30; mainly another programme’s`,
      `Not in ${P}’s timetable`,
      `${P}: mixed section (5 of 12)`,
      `${P}: outside its plan; its file lists the course as placed`,
    ];
    assert.deepEqual(ALL.map(id => card(h, id).title), titles);
    assert.equal(card(h, 12).getAttribute('aria-describedby'), 'twDimNoteLens');
    assert.equal(h.d.getElementById('twDimNoteLens').textContent, titles[2]);
    assert.equal(h.d.getElementById('twSearchCount').textContent, arabic ? '4 من 5' : '4 of 5');
    assert.equal(h.d.getElementById('twSearch').title, arabic
      ? `المطابق للتصفية: 4 من 5 لقاءات. ${P}: 4 من 5 لقاءات في جدوله.`
      : `4 of 5 meetings match. ${P}: 4 of 5 meetings in its timetable.`);
    assert.equal(h.d.getElementById('twSearchNote').textContent, h.d.getElementById('twSearch').title);
    assert.deepEqual([h.d.getElementById('twLegendDim').hidden, h.d.getElementById('twLegendDimLens').hidden], [true, false]);
    assert.equal(h.d.getElementById('twLegendDimLens').textContent, arabic ? `باهتة: ليست في جدول ${P}` : `Dimmed: not in ${P}’s timetable`);
    assert.equal(h.d.getElementById('twExportScope').textContent, arabic ? `برنامج ${P} فقط` : `Programme ${P} only`);
    await findText(h, 'ds');
    assert.deepEqual(dimsOf(h, ALL), ['lens', '', '', 'lens', 'lens']);
    assert.equal(card(h, 11).title, arabic ? `${iso('DS')}: شعبة خاصة به` : `${iso('DS')}: its own section`);
    assert.deepEqual(h.errors, []);
  }
});

test('LN3 several programmes are a union; SHARED reads the shared roles, never the shared flag; text folds, programmes only dim; switching programmes reads nothing', async t => {
  const h = await lensHarness(t);
  const reads = h.reads.length;
  await findText(h, 'ai2 ds');
  assert.deepEqual(dimsOf(h, ALL), ['', '', '', '', '']);
  assert.equal(card(h, 11).title, `${iso('AI2')}: seats 2 of 30; mainly another programme’s | ${iso('DS')}: its own section`, 'each programme’s role');
  assert.equal(h.d.getElementById('twSearch').title,
    `5 of 5 meetings match. ${iso('AI2')}: 4 of 5 meetings in its timetable; ${iso('DS')}: 2 of 5 meetings in its timetable.`);
  assert.equal(h.d.getElementById('twExportScope').textContent, `Programmes ${iso('AI2')}, ${iso('DS')} only`);
  await findText(h, 'shared ai2');
  assert.deepEqual(dimsOf(h, ALL), ['lens', '', 'lens', 'lens', 'lens'],
    'BIO1, a shared section with an AI2 role but no AI2 shared role, is dimmed');
  assert.equal(card(h, 11).title, `${iso('AI2')}: its own section`, 'its role under SHARED');
  assert.equal(h.d.getElementById('twSearch').title,
    `1 of 5 meetings match. ${iso('AI2')} (shared sections only): 1 of 5 meetings in its timetable.`);
  assert.equal(h.d.getElementById('twExportScope').textContent, `Programme ${iso('AI2')} only · Shared sections only`);
  await findText(h, 'shared');
  assert.deepEqual(dimsOf(h, ALL), ['lens', '', 'lens', 'lens', 'lens'], 'SHARED alone: every programme');
  assert.equal(h.d.getElementById('twSearch').title, '1 of 5 meetings match. Shared sections: 1 of 5 meetings in the programmes’ timetables.');
  assert.equal(h.d.getElementById('twExportScope').textContent, 'Shared sections only');
  await findText(h, 'ds');
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, NO_FOLD, 'three dimmed by the lens: nothing folds');
  await findText(h, 'ds cs1');
  assert.deepEqual(dimsOf(h, ALL), ['lens', 'filter', 'filter', 'filter lens', 'filter lens']);
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, NO_FOLD, 'the text matches CS101, which the lens dims: a match all the same');
  await findText(h, 'ai2 zzz');
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, FOLDED, 'the text folds');
  await findText(h, 'ai2 cs1');
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, NO_FOLD);
  assert.deepEqual(dimsOf(h, ALL), ['', 'filter', 'filter lens', 'filter', 'filter']);
  assert.equal(h.d.getElementById('twExportScope').textContent, `Programme ${iso('AI2')} only · Only what matches "CS1"`);
  assert.equal(h.reads.length, reads, 'switching programmes with fresh roles reads nothing');
});

test('LN4 roles arriving keep the board: the same cards and nodes, the keyboard on its card; before them, nothing is dimmed for the programme', async t => {
  const held = deferred();
  const h = await lensHarness(t, { hold: n => (n === 3 ? held.promise : null) });
  await edit(h);  // read 2, plain: the lens is now from before the change
  const nodes = [...h.d.querySelectorAll('#twGrid .tw-card')];
  card(h, 12).focus();
  // Records are handed to the callback after each turn: keep them all.
  const records = [];
  const observer = new h.w.MutationObserver(list => records.push(...list));
  observer.observe(h.d.getElementById('twGrid'), { childList: true, subtree: true });
  await findText(h, 'ai2');  // read 3, held
  assert.deepEqual(h.reads, ['lens', 'plain', 'lens']);
  assert.deepEqual(dimsOf(h, ALL), ['', '', '', '', ''], 'a programme typed while its roles are read: no lens dims');
  assert.equal(h.d.getElementById('twSearch').title, `5 of 5 meetings match. ${iso('AI2')}: checking…`);
  assert.deepEqual(ALL.map(id => card(h, id).title), Array(5).fill('(checking)'));
  assert.equal(h.d.getElementById('twLegendDimLens').hidden, true);
  held.resolve();
  await wait(() => card(h, 12).dataset.dim === 'lens');
  assert.deepEqual([...h.d.querySelectorAll('#twGrid .tw-card')], nodes, 'the same cards');
  assert.equal(h.d.activeElement, card(h, 12), 'the keyboard stays');
  assert.equal(h.d.getElementById('twLegendDimLens').hidden, false);
  assert.match(h.d.getElementById('twLegendDimLens').textContent, /^Dimmed: not in /);
  records.push(...observer.takeRecords());
  assert.deepEqual(records, [], 'no node of the board added or removed');
});

test('LN5 a lens that cannot be read: nothing is dimmed for the programme and the count says why; typing does not ask again, Selection’s Refresh does', async t => {
  for (const failure of ['http', 'null', 'view']) {
    let fail = true;
    // With no programme view, the boards say which tokens are programmes.
    const h = await lensHarness(t, { boards: [{ ...board, program: 'AI2,DS' }], answer: () => (!fail ? lensAnswer()
      : failure === 'http' ? { httpError: 500, error: { message: 'down' } }
        : failure === 'null' ? null : lensAnswer({ view: null })) });
    assert.equal(h.w.TW.planLens.loading, false, failure);
    await findText(h, 'ai2');
    await settle();
    assert.equal(h.reads.length, 1, `${failure}: not asked again by typing`);
    assert.deepEqual(dimsOf(h, ALL), ['', '', '', '', ''], failure);
    assert.equal(h.d.getElementById('twSearch').title,
      `5 of 5 meetings match. The programme view could not be read; nothing is dimmed for ${iso('AI2')}.`, failure);
    assert.equal(card(h, 12).title, '', `${failure}: no "(checking)" for a lens that could not be read`);
    await findText(h, 'ai2 shared');
    assert.equal(h.d.getElementById('twSearch').title,
      `5 of 5 meetings match. The programme view could not be read; nothing is dimmed for ${iso('AI2')}, shared sections.`, failure);
    if (failure !== 'http') assert.equal(h.d.getElementById('twQualityRooms').textContent, '0', `${failure}: the quality is shown`);
    card(h, 10).click();
    const body = h.d.getElementById('twRightBody');
    if (failure === 'view') {
      // The seating read: Selection's mix is current; the dims wait for the next read.
      assert.match(body.textContent, /20: AI2 20/);
      assert.equal(body.querySelector('[data-tw-lens-refresh]'), null);
      fail = false;
      await edit(h);
    } else {
      assert.match(body.textContent, /The programme mix is not available/, failure);
      fail = false;
      body.querySelector('[data-tw-lens-refresh]').click();
      await wait(() => !h.w.TW.planLens.loading && /20: AI2 20/.test(body.textContent));
      assert.equal(h.d.getElementById('twInspectorStatus').textContent, 'Programme mix updated.');
    }
    await wait(() => card(h, 12).dataset.dim === 'lens');
    assert.deepEqual(h.reads, ['lens', 'lens'], failure);
    assert.equal(planLensReads(h), 0);
  }
});

test('LN5b a seating that could not be evaluated: dims by the roles and the count says why; Selection reads "—" and the reason, never "nobody"', async t => {
  // Unassessed, the files follow the allocation and the marks: main and outside only.
  const view = { ...LENS_VIEW(), roles: { AI2: { main: [10, 11, 13], outside: [14] }, DS: { main: [11, 12] } } };
  const sections = { 110: { actual_plans: { AI2: 0, DS: 0 }, actual_total: 0, filter_plans: ['AI2'] } };
  for (const arabic of [false, true]) {
    const h = await lensHarness(t, { arabic, answer: () => lensAnswer({ view, sections, assessed: false }) });
    const [AI2, DS] = [iso('AI2'), iso('DS')];
    await findText(h, 'ai2');
    assert.deepEqual(dimsOf(h, ALL), ['', '', 'lens', '', '']);
    assert.equal(h.d.getElementById('twSearch').title, arabic
      ? `المطابق للتصفية: 4 من 5 لقاءات. ${AI2}: 4 من 5 لقاءات في جدوله. تعذّر تقييم توزيع الطلاب على الشعب: تتبع شعب ${AI2} توزيع الخطة، كما في ملفه لكل برنامج.`
      : `4 of 5 meetings match. ${AI2}: 4 of 5 meetings in its timetable. The seating could not be evaluated: ${AI2}’s sections follow the plan’s allocation, as its per-plan file does.`);
    await findText(h, 'ai2 ds');
    assert.match(h.d.getElementById('twSearch').title, arabic
      ? new RegExp(`تعذّر تقييم توزيع الطلاب على الشعب: تتبع شعب ${AI2}، ${DS} توزيع خططها، كما في ملفاتها لكل برنامج\\.$`)
      : new RegExp(`The seating could not be evaluated: the sections of ${AI2}, ${DS} follow their plans’ allocation, as their per-plan files do\\.$`));
    await findText(h, 'shared');
    const none = arabic
      ? 'تعذّر تقييم توزيع الطلاب على الشعب: تتبع الشعب توزيع الخطة، كما في الملفات لكل برنامج.'
      : 'The seating could not be evaluated: sections follow the plan’s allocation, as the per-plan files do.';
    assert.ok(h.d.getElementById('twSearch').title.endsWith(` ${none}`), h.d.getElementById('twSearch').title);
    card(h, 10).click();
    const seated = [...h.d.querySelectorAll('#twRightBody .tw-sel-field')].find(f => f.querySelector('dt').textContent === (arabic ? 'المقاعد' : 'Seated')).querySelector('dd');
    assert.match(seated.textContent, /^—/);
    assert.ok(seated.textContent.includes(none), seated.textContent);
    assert.doesNotMatch(seated.textContent, /No student|لا يُقعَد/);
  }
});

test('LN5c a lens read again that fails keeps the one before it and says so in Selection, how old it is too; while it reads, the count says checking', async t => {
  for (const arabic of [false, true]) {
    let fail = false;
    const held = deferred();
    const h = await lensHarness(t, { arabic, answer: () => (fail ? { httpError: 500, error: { message: 'down' } } : lensAnswer()),
      hold: n => (n === 2 ? held.promise : null) });
    await findText(h, 'ai2');
    card(h, 10).click();
    const body = h.d.getElementById('twRightBody');
    fail = true;
    const again = h.w.refreshPlanLens();  // read again, for the same data
    await wait(() => h.reads.length === 2);
    assert.ok(h.d.getElementById('twSearch').title.endsWith(arabic ? `${iso('AI2')}: 4 من 5 لقاءات في جدوله.` : `${iso('AI2')}: 4 of 5 meetings in its timetable.`),
      'the roles are still those of this data');
    held.resolve();
    await again;
    assert.match(body.textContent, /20: AI2 20/, 'the mix it had');
    assert.ok(body.textContent.includes(arabic ? 'تعذّرت قراءة توزيع البرامج مجدداً.' : 'The programme mix could not be read again.'), body.textContent);
    assert.ok(!body.textContent.includes(arabic ? 'من قبل آخر تعديل.' : 'From before your last change.'), 'of this data still');
    assert.ok(body.querySelector('[data-tw-lens-refresh]'), 'Refresh again');
    assert.equal(h.d.getElementById('twInspectorStatus').textContent, arabic ? 'توزيع البرامج غير متاح.' : 'The programme mix is not available.');
    assert.deepEqual(dimsOf(h, ALL), ['', '', '', '', ''], 'nothing dimmed by a lens that could not be read again');
    assert.equal(h.d.getElementById('twSearch').title, arabic
      ? `المطابق للتصفية: 5 من 5 لقاءات. تعذّرت قراءة عرض البرامج؛ لم يُبهَت شيء للبرنامج ${iso('AI2')}.`
      : `5 of 5 meetings match. The programme view could not be read; nothing is dimmed for ${iso('AI2')}.`);
    await findText(h, 'ai2 ds');
    await settle();
    assert.equal(h.reads.length, 2, 'not asked again by typing');
    // After an edit (its read fails too), the mix says how old it is.
    await edit(h);
    assert.deepEqual(h.reads, ['lens', 'lens', 'lens']);
    assert.ok(body.textContent.includes(arabic ? 'تعذّرت قراءة توزيع البرامج مجدداً. من قبل آخر تعديل.' : 'The programme mix could not be read again. From before your last change.'), body.textContent);
    // Refresh: the count says the programmes are being checked while it reads.
    const next = deferred();
    fail = false;
    const refresh = body.querySelector('[data-tw-lens-refresh]');
    h.w.fetch = (old => async (url, opts) => { if (url.includes('/quality/')) await next.promise; return old(url, opts); })(h.w.fetch);
    refresh.click();
    await settle();
    assert.ok(h.d.getElementById('twSearch').title.endsWith(arabic ? `${iso('AI2')}، ${iso('DS')}: جارٍ التحقق…` : `${iso('AI2')}, ${iso('DS')}: checking…`),
      h.d.getElementById('twSearch').title);
    next.resolve();
    await wait(() => !h.w.TW.planLens.loading && card(h, 10).title !== '(checking)');
    assert.deepEqual(dimsOf(h, ALL), ['', '', '', '', '']);
    assert.ok(h.d.getElementById('twSearch').title.endsWith(arabic ? 'في جدوله.' : 'in its timetable.'));
  }
});

test('LN5d a lens that could not be read is not asked for by every read after it', async t => {
  const h = await lensHarness(t, { answer: () => null });
  await edit(h);
  assert.deepEqual(h.reads, ['lens', 'plain']);
});

test('LN6 a meeting the roles never saw is not dimmed and says "(checking)"; an edit keeps the old roles, "updating…", until the new ones arrive', async t => {
  // The roles of the first read never saw ENG1.
  const first = { ...LENS_VIEW(), roles: { AI2: { main: [10], mixed: [13], other: [11] }, DS: { main: [11, 12] } }, placement_ids: [10, 11, 12, 13] };
  const held = deferred();
  const h = await lensHarness(t, { hold: n => (n === 2 ? held.promise : null),
    answer: n => lensAnswer({ view: n === 1 ? first : { ...LENS_VIEW(), placement_ids: [...ALL, 15] } }) });
  await findText(h, 'ai2');
  assert.deepEqual(dimsOf(h, ALL), ['', '', 'lens', '', ''], 'ENG1 unknown: not dimmed');
  assert.equal(card(h, 14).title, '(checking)');
  // An edit: a new meeting, the board read again, its quality read held.
  h.rows.push(at(15, { course_code: 'CHEM1', day: 'THU' }));
  h.w.TW.dataVersion++;
  const loading = h.w.selectBoard(2);
  await wait(() => h.reads.length === 2);
  assert.equal(h.reads[1], 'lens');
  assert.deepEqual(dimsOf(h, [...ALL, 15]), ['', '', 'lens', '', '', ''], 'the old roles stay; the new meeting is unknown');
  assert.equal(card(h, 15).title, '(checking)');
  assert.equal(h.d.getElementById('twSearch').title, `5 of 6 meetings match. ${iso('AI2')}: updating…`);
  // Another programme while the roles are read: none were shown for it.
  await findText(h, 'ds');
  assert.equal(h.reads.length, 2, 'joined');
  assert.deepEqual(dimsOf(h, [...ALL, 15]), ['', '', '', '', '', '']);
  assert.equal(h.d.getElementById('twSearch').title, `6 of 6 meetings match. ${iso('DS')}: checking…`);
  await findText(h, 'ai2');
  held.resolve();
  await loading;
  assert.deepEqual(dimsOf(h, [...ALL, 15]), ['', '', 'lens', '', '', 'lens'], 'the new roles: CHEM1 is in no AI2 file');
  assert.equal(card(h, 14).title, `${iso('AI2')}: outside its plan; its file lists the course as placed`);
  assert.equal(h.d.getElementById('twSearch').title, `4 of 6 meetings match. ${iso('AI2')}: 4 of 6 meetings in its timetable.`);
});

test('LN9 a read without the lens out when a programme is typed: a second read, with it, starts; one with the lens out is joined; the board’s load holds until it lands', async t => {
  const plain = deferred(), withLens = deferred();
  const h = await lensHarness(t, { hold: n => (n === 2 ? plain.promise : n === 3 ? withLens.promise : null) });
  h.w.TW.dataVersion++;
  const loading = h.w.selectBoard(2);
  await wait(() => h.reads.length === 2);
  assert.equal(h.reads[1], 'plain');
  await findText(h, 'ai2');
  await wait(() => h.reads.length === 3);
  assert.equal(h.reads[2], 'lens', 'the read out has no lens: a new one, with it');
  await findText(h, 'ai2 ds');
  h.w.refreshWorkspaceQuality(false, { lens: true });
  h.w.refreshWorkspaceQuality(true, { lens: false });
  await settle();
  assert.equal(h.reads.length, 3, 'a read with the lens out is joined, with or without the lens');
  plain.resolve();
  await settle(); await settle();
  assert.equal(h.w.workspaceBusy(), true, 'the board’s load waits for the read that overtook its own');
  withLens.resolve();
  await loading;
  assert.equal(h.w.workspaceBusy(), false);
  await findText(h, 'ai2');
  assert.equal(card(h, 12).dataset.dim, 'lens');
  assert.equal(h.w.TW.qualityVersion, h.w.TW.dataVersion);
});

test('LN10 Arabic: the programmes isolated, the counts by the plural, the scope line, the checking and failure words', async t => {
  const rowsOf = n => Array.from({ length: n }, (_, i) => at(10 + i, { course_code: `CS10${i}`, day: DAYS5[i % 5] }));
  const viewOf = n => ({ programmes: ['AI2', 'DS'], roles: { AI2: { main: rowsOf(n).map(p => p.id) }, DS: {} }, shared_roles: {},
    placement_ids: rowsOf(n).map(p => p.id), seating_assessed: true });
  const counts = { 1: 'لقاء واحد', 2: 'لقاءين', 3: '3 لقاءات', 11: '11 لقاءً' };
  for (const [n, of] of Object.entries(counts)) {
    const h = await lensHarness(t, { arabic: true, rows: rowsOf(Number(n)), answer: () => lensAnswer({ view: viewOf(Number(n)) }) });
    await findText(h, 'ai2');
    assert.ok(h.d.getElementById('twSearch').title.endsWith(` ${iso('AI2')}: ${n} من ${of} في جدوله.`), h.d.getElementById('twSearch').title);
  }
  const h = await lensHarness(t, { arabic: true });
  await findText(h, 'ai2 ds');
  assert.equal(h.d.getElementById('twExportScope').textContent, `البرامج ${iso('AI2')}، ${iso('DS')} فقط`);
  assert.equal(h.d.getElementById('twSearch').title,
    `المطابق للتصفية: 5 من 5 لقاءات. ${iso('AI2')}: 4 من 5 لقاءات في جدوله؛ ${iso('DS')}: 2 من 5 لقاءات في جدوله.`);
  await findText(h, 'shared ai2 cs1');
  assert.equal(h.d.getElementById('twExportScope').textContent, `برنامج ${iso('AI2')} فقط · الشعب المشتركة فقط · ما يطابق «CS1» فقط`);
  assert.ok(h.d.getElementById('twSearch').title.endsWith(` ${iso('AI2')} (الشعب المشتركة فقط): 1 من 5 لقاءات في جدوله.`), h.d.getElementById('twSearch').title);
  await findText(h, 'shared');
  assert.ok(h.d.getElementById('twSearch').title.endsWith(' الشعب المشتركة: 1 من 5 لقاءات في جداول البرامج.'), h.d.getElementById('twSearch').title);
  // Checking, updating, and a lens that could not be read, in Arabic.
  const held = deferred();
  let fail = false;
  const a = await lensHarness(t, { arabic: true, answer: () => (fail ? null : lensAnswer()), hold: n => (n === 2 ? held.promise : null) });
  await findText(a, 'ai2');
  a.w.TW.dataVersion++;
  const loading = a.w.selectBoard(2);
  await wait(() => a.reads.length === 2);
  assert.ok(a.d.getElementById('twSearch').title.endsWith(` ${iso('AI2')}: جارٍ التحديث…`), a.d.getElementById('twSearch').title);
  await findText(a, 'ds');
  assert.ok(a.d.getElementById('twSearch').title.endsWith(` ${iso('DS')}: جارٍ التحقق…`), a.d.getElementById('twSearch').title);
  assert.equal(card(a, 10).title, '(جارٍ التحقق)');
  fail = true;
  held.resolve();
  await loading;
  assert.ok(a.d.getElementById('twSearch').title.endsWith(` تعذّرت قراءة عرض البرامج؛ لم يُبهَت شيء للبرنامج ${iso('DS')}.`), a.d.getElementById('twSearch').title);
  await findText(a, 'ds ai2');
  assert.ok(a.d.getElementById('twSearch').title.endsWith(` تعذّرت قراءة عرض البرامج؛ لم يُبهَت شيء لتصفية البرامج: ${iso('DS')}، ${iso('AI2')}.`), a.d.getElementById('twSearch').title);
  await findText(a, 'shared');
  assert.ok(a.d.getElementById('twSearch').title.endsWith(' تعذّرت قراءة عرض البرامج؛ لم يُبهَت شيء لتصفية البرامج: الشعب المشتركة.'), a.d.getElementById('twSearch').title);
  assert.equal(a.d.getElementById('twExportScope').textContent, 'الشعب المشتركة فقط');
});

test('LN11 a programme is known by the boards before the view is read; the left list asks whether it needs the course, anew when the lens arrives', async t => {
  const held = deferred();
  const budget = [
    { course_code: 'CS101', course_key: 'CS101', programme_term: 7, remaining_sections: 1, used_sections: 0, planned_sections: 1, total_demand: 20 },
    { course_code: 'PHYS1', course_key: 'PHYS1', programme_term: 7, remaining_sections: 1, used_sections: 0, planned_sections: 1, total_demand: 25 },
  ];
  const courses = { CS101: { plans: { AI2: 20 } }, PHYS1: { plans: { DS: 25 } } };
  const h = await lensHarness(t, { select: false, budget, boards: [{ ...board, program: 'ai2, DS' }],
    answer: n => lensAnswer({ courses, view: n === 1 ? LENS_VIEW() : null, plans: ['AI2'] }), hold: n => (n === 1 ? held.promise : null) });
  const opening = h.select();
  await wait(() => h.reads.length === 1);
  const rows = () => [...h.d.querySelectorAll('#twSectionList [data-focus-key^="place:"]')].map(row => row.dataset.focusKey.split(':')[1].split('|')[0]);
  assert.deepEqual(rows(), ['CS101', 'PHYS1']);
  await findText(h, 'ai2');
  assert.equal(h.reads.length, 1, 'the open read, with the lens, is joined');
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, NO_FOLD, 'a board programme: a programme, never text, before the view');
  assert.equal(h.d.getElementById('twExportScope').textContent, `Programme ${iso('AI2')} only`);
  assert.deepEqual(rows(), [], 'no course known to need it yet');
  held.resolve();
  await opening;
  assert.deepEqual(rows(), ['CS101'], 'the lens arrives: the list asks again');
  // A lens with no view: the board programmes it knows (DS has no demand here).
  await edit(h);
  await findText(h, 'ds');
  await wait(() => !h.w.TW.planLens.loading);
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, FOLDED, 'DS is text now: it matches nothing');
  await findText(h, 'ai2');
  assert.deepEqual(foldOf(h, 'S1', 'lecture').state, NO_FOLD);
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
  // Its first row is its fold (6c), hidden with no filter; the header and the
  // day rows are in the rowgroup the fold controls.
  const fold = grids[0].firstElementChild;
  assert.deepEqual([fold.className, fold.getAttribute('role'), fold.hidden], ['ux-grid-row tw-fold-row', 'row', true]);
  const body = grids[0].querySelector(':scope > [role="rowgroup"]');
  assert.equal(fold.querySelector('.tw-fold-toggle').getAttribute('aria-controls'), body.id);
  const header = body.querySelector('[role="row"]');
  const columns = [...header.querySelectorAll('[role="columnheader"]')];
  assert.equal(columns[0].textContent, 'Day');
  assert.equal(strip(columns[1].getAttribute('aria-label')), 'Slot 1, 09:00–10:15');
  assert.deepEqual([...grids[0].querySelectorAll('[role="rowheader"]')].map(r => r.getAttribute('aria-label')), ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday']);
  const cells = [...body.querySelectorAll('[role="gridcell"]')];
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
  const firstCells = [...two.d.querySelectorAll('#twGrid [role="grid"]')].map(g => g.querySelector('.tw-cell[role="gridcell"]').dataset.focusKey);
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
  assert.equal(english.querySelector('button'), null, 'no nested button: the row is the control, its Place… the pointer’s (5f)');
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

test('J5 any other refusal stays an error with the server’s words', async t => {
  const h = await dropHarness(t, { refuse: 'Pattern days are not on this board.', refuseCode: 'VALIDATION_PLANNED_PATTERN' });
  await h.select();
  await dropOn(h, plannedPayload({}));
  assert.deepEqual(h.notices.map(n => [n.type, n.text]), [['error', 'Pattern days are not on this board.']]);
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
  // 5c-audit: the rooms are asked with the quality audit's answer (C6).
  assert.deepEqual(h.reads, ['/ops/tw/placements/10/room-candidates/?audit=1'], 'the server’s own time: no slot parameters');
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
  // The room can change on a time-locked meeting; only Move… (5e) is held,
  // and the note says why.
  assert.deepEqual([button.disabled, live.d.getElementById('twSelActionNote').hidden], [false, false]);
  assert.equal(live.d.getElementById('twSelActionNote').textContent, "This section's time is locked. Unlock its time to move it; its room can still change.");
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

/* ── Change room by the audit (5c-audit client): the legacy snapshot ──
   An answer without `audit` (a server before 5c-audit) is drawn and saved
   exactly as before the audit client: the dialog's title, sub-line, body
   (markup, byte for byte), note and the report of each save, in English and
   Arabic, recorded in tests/fixtures/room_dialog_legacy.json at 5892a12,
   before any client change. ROOM_DIALOG_LEGACY_RECORD=1 writes the file
   instead of comparing (only ever on that commit). */
const LEGACY_DIALOG = path.join(root, 'tests/fixtures/room_dialog_legacy.json');
const timedRoom = room => ({ ...room, validation: { ...room.validation, critical_count: 2 } });
const copyChanged = body => ({ ok: true, placement: roomRow({ room: body.room }), placements: [roomRow({ room: body.room }), roomRow({ id: 31, board_id: 3, room: body.room })] });
const LEGACY_DIALOG_CASES = [
  { name: 'every group, the time keeping 2 clashes', rooms: () => ROOM_SET.map(timedRoom), saves: ['R1', 'R7', 'R9', 'R8'] },
  { name: 'every group, in Arabic', arabic: true, rooms: () => ROOM_SET, saves: ['R2', 'R7', 'R9', 'R8'] },
  { name: 'in use and outside the programme rooms, a copy changed with it', move: copyChanged,
    rooms: () => [rc('R1'), rc('LAB25'), rc('R12', { available: false, tone: 'block', department_fit: false, occupied_by: [heldOn(3)] }),
      rc('R13', { available: false, tone: 'block', department_fit: false, occupied_by: [heldOn(2)] })], saves: ['R12', 'R13', 'R1'] },
  { name: 'in Arabic, in use and outside, a copy changed with it', arabic: true, move: copyChanged,
    rooms: () => [rc('R1'), rc('LAB25'), rc('R12', { available: false, tone: 'block', department_fit: false, occupied_by: [heldOn(3)] })], saves: ['R12', 'R1'] },
  { name: 'only misfits, a cut list', rooms: () => ROOM_SET.filter(r => ['R3', 'R4', 'R5'].includes(r.room_code)), summary: { total: 75 }, saves: [] },
  { name: 'no room yet, in Arabic, seats counted and no need', arabic: true, row: { room: 'UNASSIGNED', room_state: 'unassigned' }, target: { required_capacity: null },
    rooms: () => [rc('R1', { capacity: 3, validation: { critical_count: 1 } }), rc('R2', { capacity: 11 }), rc('R6', { capacity: 1 })], saves: ['R2'] },
  { name: 'the current room outside the programme rooms', row: { room: 'R7' }, rooms: () => ROOM_SET, saves: ['R1'] },
  { name: 'the current room a misfit', row: { room: 'R5' }, rooms: () => ROOM_SET, saves: ['R2'] },
];
// One case drawn (and, given a code, that room saved): what the dialog says.
async function legacyDialog(t, c, { save = '', wrap = answer => answer, quality: q = null } = {}) {
  const h = await roomHarness(t, {
    arabic: Boolean(c.arabic), move: c.move || null, rows: () => [roomRow(c.row || {})],
    rooms: () => wrap(roomsAnswer(c.rooms(), { target: c.target || {}, summary: c.summary || {} })),
    more: url => (q && url.includes('/quality/') ? q : undefined),
  });
  await h.select();
  roomByKey(h);
  await wait(() => !h.d.querySelector('#twRoomBody [aria-busy]'));
  const drawn = { title: h.d.getElementById('twRoomTitle').innerHTML, sub: h.d.getElementById('twRoomSub').innerHTML,
    body: h.d.getElementById('twRoomBody').innerHTML, note: roomNote(h) };
  if (!save) return { h, drawn };
  roomRadio(h, save).click();
  h.d.getElementById('twRoomAssign').click();
  await wait(() => h.notices.length > 0 && !roomDialogEl(h).hasAttribute('open'));
  return { h, drawn, report: h.notices.at(-1) };
}
// `saves` says which cases also save each of their rooms (every one by default).
async function legacyDialogs(t, { wrap, quality: q, saves = () => true } = {}) {
  const out = {};
  for (const c of LEGACY_DIALOG_CASES) {
    const { drawn } = await legacyDialog(t, c, { wrap, quality: q });
    const reports = {};
    if (saves(c)) for (const code of c.saves) reports[code] = (await legacyDialog(t, c, { save: code, wrap, quality: q })).report;
    out[c.name] = { ...drawn, reports };
  }
  return out;
}

test('RC22 an answer without the audit is drawn and saved byte for byte as before the audit client', async t => {
  const produced = plainOf(await legacyDialogs(t));
  if (process.env.ROOM_DIALOG_LEGACY_RECORD === '1') {
    // Isolates written as escapes, so the file shows them.
    const json = JSON.stringify(produced, null, 1).replace(/[\u2066\u2069]/g, c => `\\u${c.charCodeAt(0).toString(16)}`);
    fs.writeFileSync(LEGACY_DIALOG, `${json}\n`);
    return;
  }
  const recorded = JSON.parse(fs.readFileSync(LEGACY_DIALOG, 'utf8'));
  assert.deepEqual(Object.keys(produced), Object.keys(recorded));
  for (const name of Object.keys(recorded)) assert.deepEqual(produced[name], recorded[name], name);
});

/* ── Change room judged by the quality audit (5c-audit client) ──
   The rooms are read with ?audit=1. An answer that carries the audit's
   answer for the meeting is judged by it (D20c as amended): before a save
   the dialog says the rows Issues will list after it; a room short of the
   audit's seats is a choosable warning, the wrong gender or type refused
   (the owner's answer to Q1). CS323 S1 needs 26 seats by the audit (30 by
   the legacy rule); it sits in LAB25, 25 seats. */
const AUDIT_TARGET = { assessed: true, save: 'room_only', required_type: 'lecture', required_capacity: 26, capacity_source: 'budget', required_gender: 'M',
  boards: [{ board_id: 2, board_label: 'Term 7', programmes: ['AI'] }], placement_ids: [10], moving_placement_ids: [10], kept: [], current: null };
const judged = (reasons = [], qualityReasons = [], extra = {}) => ({
  compatible: !reasons.some(reason => ['type', 'gender', 'programme'].includes(reason)), undersized: reasons.includes('capacity'),
  reasons, programme_board_ids: [], quality_reasons: qualityReasons, ...extra });
const SHORT = { available: false, tone: 'block', fits_capacity: false };
// In the server's order, with the legacy fields as the server sends them.
const AUDIT_SET = [
  rc('R1', { audit: judged() }),
  rc('LAB25', { ...SHORT, capacity: 25, audit: judged(['capacity'], ['undersized']) }),
  rc('R2', { ...SHORT, capacity: 28, building: 'North', audit: judged() }),
  rc('R9', { available: false, tone: 'block', occupied_by: [heldOn(2)], audit: judged() }),
  rc('R8', { ...SHORT, capacity: 24, occupied_by: [heldOn(3)], audit: judged(['capacity'], ['undersized']) }),
  rc('R7', { tone: 'warn', department_fit: false, audit: judged(['programme'], ['incompatible'], { programme_board_ids: [2] }) }),
  rc('R5', { ...SHORT, capacity: 20, department_fit: false, audit: judged(['programme', 'capacity'], ['undersized', 'incompatible'], { programme_board_ids: [2] }) }),
  rc('R3', { available: false, tone: 'block', fits_type: false, room_type: 'lab', audit: judged(['type'], ['incompatible']) }),
  rc('R4', { available: false, tone: 'block', fits_gender: false, section: 'F', audit: judged(['gender'], ['incompatible']) }),
  rc('R6', { section: '', audit: judged(['gender'], ['incompatible']) }),
];
const auditAnswer = (candidates = AUDIT_SET, audit = {}, extra = {}) => roomsAnswer(candidates, { target: { audit: { ...AUDIT_TARGET, ...audit } }, ...extra });
const radioWords = (h, code, index = 0) => h.d.getElementById(roomRadio(h, code).getAttribute('aria-describedby').split(' ')[index]).textContent;
const roomGroupsDrawn = h => [...h.d.querySelectorAll('#twRoomChoices fieldset')].map(set => set.dataset.group);
const LAB25_SHORT = judged(['capacity'], ['undersized']);

test('RC23 judged by the audit: free, lowering quality (a choosable warning, too small or unsuitable), in use, or refused for gender or type; worded as Issues will list them', async t => {
  const words = {
    en: {
      legends: ['Free and fits (2)', 'Free, but lowers quality (3)', 'Fits, but in use at this time (2)'],
      hint: 'Choosing one saves the warning on its row, which Issues then shows.',
      verdicts: {
        R1: 'Free and fits', R2: 'Free and fits',
        LAB25: 'Lowers quality: Room too small · needs 26 seats',
        R7: "Lowers quality: Room unsuitable · outside this board's programme rooms",
        R5: "Lowers quality: Room too small, Room unsuitable · needs 26 seats · outside this board's programme rooms",
        R9: 'Blocks publishing: Room double-booked',
        R8: 'Lowers quality: Room too small, Room also booked on another board · needs 26 seats',
      },
      facts: { R2: /^Lecture room · 28 seats · 2 spare/, LAB25: /^Lecture room · 25 seats · 1 short/ },
      misfits: [['R3', 'This meeting needs a lecture room'], ['R4', 'This meeting needs a Male (M) room'], ['R6', 'This meeting needs a Male (M) room']],
      current: 'Current room: LAB25 · Lowers quality: Room too small · needs 26 seats',
      note: 'This is the current room.',
    },
    ar: {
      legends: ['متاحة ومناسبة (2)', 'متاحة لكنها تُضعف الجودة (3)', 'مناسبة لكنها مستخدمة في هذا الوقت (2)'],
      hint: 'اختيار إحداها يحفظ التحذير المذكور في سطرها، ويظهر بعدها في «المشاكل».',
      verdicts: {
        R1: 'متاحة ومناسبة', R2: 'متاحة ومناسبة',
        LAB25: 'يُضعف الجودة: القاعة أصغر من المطلوب · يحتاج 26 مقعداً',
        R7: 'يُضعف الجودة: القاعة غير مناسبة · خارج قاعات برامج هذه اللوحة',
        R5: 'يُضعف الجودة: القاعة أصغر من المطلوب, القاعة غير مناسبة · يحتاج 26 مقعداً · خارج قاعات برامج هذه اللوحة',
        R9: 'يمنع النشر: القاعة محجوزة مرتين',
        R8: 'يُضعف الجودة: القاعة أصغر من المطلوب, القاعة محجوزة في لوحة أخرى · يحتاج 26 مقعداً',
      },
      facts: { R2: /^قاعة محاضرات · 28 مقعداً · مقعدان فائضان/, LAB25: /^قاعة محاضرات · 25 مقعداً · ينقصها مقعد واحد/ },
      misfits: [['R3', 'هذا اللقاء يحتاج إلى قاعة محاضرات'], ['R4', 'هذا اللقاء يحتاج إلى قاعة طلاب (M)'], ['R6', 'هذا اللقاء يحتاج إلى قاعة طلاب (M)']],
      current: 'القاعة الحالية: LAB25 · يُضعف الجودة: القاعة أصغر من المطلوب · يحتاج 26 مقعداً',
      note: 'هذه هي القاعة الحالية.',
    },
  };
  for (const [language, W] of Object.entries(words)) {
    const h = await roomHarness(t, { arabic: language === 'ar', rooms: () => auditAnswer(AUDIT_SET, { current: LAB25_SHORT }) });
    await h.select();
    roomByKey(h);
    await settled(h);
    // Rooms the legacy seats refuse (R2, LAB25, R8, R5) can be chosen; a room
    // with no gender (R6), which they offer, cannot. No "outside" group.
    assert.deepEqual(roomRadios(h).map(r => [r.value, r.closest('.ux-choice').dataset.tone, r.closest('fieldset').dataset.group]), [
      ['R1', 'ok', 'free'], ['R2', 'ok', 'free'], ['LAB25', 'warning', 'flagged'], ['R7', 'warning', 'flagged'], ['R5', 'warning', 'flagged'],
      ['R9', 'critical', 'inUse'], ['R8', 'warning', 'inUse']], language);
    assert.deepEqual(roomRadios(h).map(r => r.closest('.ux-choice').querySelector('.ux-tone-icon').dataset.tone),
      ['ok', 'ok', 'warning', 'warning', 'warning', 'critical', 'warning'], language);
    assert.deepEqual([...h.d.querySelectorAll('#twRoomChoices legend')].map(l => l.textContent), W.legends, language);
    assert.deepEqual([...h.d.querySelectorAll('#twRoomChoices fieldset')].map(set => set.getAttribute('aria-describedby')), [null, 'twRoomHint-flagged', 'twRoomHint-inUse']);
    assert.equal(h.d.getElementById('twRoomHint-flagged').textContent, W.hint, language);
    assert.ok(h.d.querySelector('fieldset[data-group="flagged"]').matches('.ux-choice-list[data-ux-submit]'), 'its rows submit on Enter, as the other groups do');
    for (const [code, said] of Object.entries(W.verdicts)) assert.equal(radioWords(h, code), said, `${language} ${code}`);
    // Seats against the audit's need, not the legacy one.
    for (const [code, facts] of Object.entries(W.facts)) assert.match(radioWords(h, code, 1), facts, `${language} ${code}`);
    assert.equal(strip(radioWords(h, 'R8', 2)), language === 'ar' ? 'تستخدمها CS103-S1 (Term 3) في 09:00–10:15' : 'Used by CS103-S1 (Term 3) at 09:00–10:15');
    // Refused: rows with their reasons, never radios.
    const misfits = [...h.d.querySelectorAll('#twRoomMisfits .ux-choice-static')];
    assert.deepEqual(misfits.map(row => [row.querySelector('bdi').textContent, row.querySelector('.ux-choice-desc').textContent]), W.misfits, language);
    assert.equal(h.d.querySelectorAll('#twRoomMisfits input').length, 0);
    // The current room, short of the audit's seats, is chosen at first (C9),
    // and its line says what Issues say of it now.
    assert.equal(roomRadio(h, 'LAB25').checked, true, language);
    assert.deepEqual(roomNote(h), [true, W.note], language);
    assert.equal(h.d.querySelector('#twRoomBody .tw-room-current').textContent, W.current, language);
    assert.equal(h.d.querySelector('#twRoomBody .tw-room-current .tw-room-verdict').dataset.tone, 'warning');
    assert.deepEqual(h.errors, []);
  }
});

test('RC24 judged by the audit, a saved room is reported by the rows Issues will list: a warning naming them, after any clash; none, a success', async t => {
  const withCopy = body => ({ ok: true, placement: roomRow({ room: body.room }), placements: [roomRow({ room: body.room }), roomRow({ id: 31, board_id: 3, room: body.room })] });
  const saves = {
    en: [
      ['R7', null, { type: 'warning', text: 'Room \u2066R7\u2069 assigned; Issues will show “Room unsuitable”.' }],
      ['R5', null, { type: 'warning', text: 'Room \u2066R5\u2069 assigned; Issues will show “Room too small”, “Room unsuitable”.' }],
      ['R8', null, { type: 'warning', text: 'Room \u2066R8\u2069 assigned; \u2066CS103-S1\u2069 also uses it at this time. Issues will also show “Room too small”.' }],
      ['R9', null, { type: 'warning', text: 'Room \u2066R9\u2069 assigned; \u2066CS102-S1\u2069 also uses it at this time.' }],
      ['R2', null, { type: 'success', text: 'Room \u2066R2\u2069 assigned.' }],
      ['R7', withCopy, { type: 'warning', text: 'Room \u2066R7\u2069 assigned; Issues will show “Room unsuitable”. It also changed on 1 other board.' }],
    ],
    ar: [
      ['R5', null, { type: 'warning', text: 'عُيّنت القاعة \u2066R5\u2069، وستعرض «المشاكل»: «القاعة أصغر من المطلوب»، «القاعة غير مناسبة».' }],
      ['R8', null, { type: 'warning', text: 'عُيّنت القاعة \u2066R8\u2069، ويستخدمها في هذا الوقت أيضاً: \u2066CS103-S1\u2069. وستعرض «المشاكل» أيضاً: «القاعة أصغر من المطلوب».' }],
      ['R1', null, { type: 'success', text: 'عُيّنت القاعة \u2066R1\u2069.' }],
    ],
  };
  for (const [language, rows] of Object.entries(saves)) {
    for (const [code, move, report] of rows) {
      const h = await roomHarness(t, { arabic: language === 'ar', move, rooms: () => auditAnswer() });
      await h.select();
      roomByKey(h);
      await settled(h);
      roomRadio(h, code).click();
      assert.deepEqual(roomNote(h), [false, ''], `${language} ${code}`);
      h.d.getElementById('twRoomAssign').click();
      await wait(() => h.notices.length > 0 && !roomDialogEl(h).hasAttribute('open'));
      // A room the legacy seats refused (R2, R5, R8) is sent as any other.
      assert.deepEqual(h.posts, [{ room: code }], `${language} ${code}`);
      assert.deepEqual(h.notices.at(-1), report, `${language} ${code}`);
    }
  }
  // A save the user left (Escape, then R again, D3b) is reported by what its
  // row was judged against, whatever the list open by then is judged by.
  let room = 'LAB25';
  const landing = deferred();
  const h = await roomHarness(t, {
    rows: () => [roomRow({ room })],
    rooms: () => auditAnswer(),
    move: async body => {
      await landing.promise;
      room = body.room;
      return { ok: true, placement: roomRow({ room }), placements: [roomRow({ room })] };
    },
  });
  await h.select();
  roomByKey(h);
  await settled(h);
  roomRadio(h, 'R5').click();
  h.d.getElementById('twRoomAssign').click();
  await settle();
  keyOn(h, 'Escape');
  h.w.TW.quality = { ...quality, rooms: { assessed: false, shortages: 0, details: [] } };
  roomByKey(h);
  await settled(h);
  assert.deepEqual([h.reads.length, roomGroupsDrawn(h)], [1, ['free', 'outside', 'inUse']], 'the reopened list, judged by the legacy rule');
  const alert = h.d.getElementById('twRoomAlert');
  landing.resolve();
  await wait(() => !alert.hidden);
  assert.deepEqual([alert.textContent, alert.dataset.tone], ['Room \u2066R5\u2069 assigned; Issues will show “Room too small”, “Room unsuitable”.', 'warning']);
});

test('RC25 judged by the audit, a row says what Issues’ card says once the room is saved: the same lead, kinds and order, and the seats Issues’ row says', async t => {
  const target = { ...ROOM_TARGET, audit: { ...AUDIT_TARGET } };
  const cases = [];
  for (const reasons of [[], ['undersized'], ['incompatible'], ['undersized', 'incompatible']]) {
    for (const use of ['', 'here', 'elsewhere']) cases.push({ reasons, use });
  }
  for (const arabic of [false, true]) {
    for (const { reasons, use } of cases) {
      const name = `${arabic ? 'ar' : 'en'} ${reasons.join('+') || 'none'} ${use || 'free'}`;
      // Issues after the save: the quality check's reasons ("clash" when the
      // room is in use at that time), and the board's clash when it is this
      // board's. Placement 11 holds R1 at the same time on this board.
      const h = await modelHarness(t, {
        arabic,
        placements: use === 'here' ? [at(10), at(11)] : [at(10), at(11, { day: 'MON' })],
        conflicts: use === 'here' ? { ...quiet, summary: { critical: 1, warning: 0 }, room_clashes: [{ ids: [10, 11], room: 'R1', sections: ['AI10-S1', 'AI11-S1'] }] } : quiet,
        q: { ...quality, rooms: { assessed: true, shortages: 1, details: [{ course_code: 'AI10', placement_ids: [10], rooms: ['R1'], reasons: use ? [...reasons, 'clash'] : reasons, required_capacity: 26 }] } },
      });
      await h.select();
      const mark = card(h, 10).querySelector('.tw-card-tone');
      const room = rc('R1', { occupied_by: use === 'here' ? [heldOn(2)] : use === 'elsewhere' ? [heldOn(3)] : [], audit: judged([], reasons) });
      const verdict = h.w.TimetableRoomChoice.verdict(room, { id: 10, board_id: 2 }, target);
      const [head, ...details] = verdict.words.split(' · ');
      if (!reasons.length && !use) {
        assert.deepEqual([mark.hidden, verdict.group, verdict.tone], [true, 'free', 'ok'], name);
        continue;
      }
      assert.equal(head, mark.getAttribute('aria-label'), name);
      assert.equal(verdict.tone, tone(h, 10), name);
      // "Room too small": the seats it needs, as Issues' row says them.
      if (reasons.includes('undersized')) {
        h.w.renderRightPanel('issues');
        const small = [...h.d.querySelectorAll('#twRightBody .tw-issue-row')]
          .find(row => row.querySelector('.tw-issue-kind').textContent === h.w.TimetableRoomChoice.words(arabic).kinds.roomSmall);
        assert.equal(details[0], small.querySelector('.tw-issue-detail').textContent, name);
      } else {
        assert.deepEqual(details, [], name);
      }
    }
  }
});

test('RC26 when the quality check cannot judge the rooms, or the audit did not judge the meeting: the legacy rule as before, with a note that says so; the judgement is the read’s', async t => {
  // Every room carries an audit answer that would re-sort it.
  const withAudit = audit => answer => ({ ...answer, target: { ...answer.target, audit },
    candidates: answer.candidates.map(room => ({ ...room, audit: judged(['gender', 'capacity'], ['undersized', 'incompatible']) })) });
  const unjudged = { ...quality, rooms: { assessed: false, shortages: 0, details: [] } };
  const recorded = JSON.parse(fs.readFileSync(LEGACY_DIALOG, 'utf8'));
  const notes = {
    en: '<p class="tw-room-note" data-tone="info">The quality check cannot judge rooms for this meeting; rooms are grouped by type, gender and seats only.</p>',
    ar: '<p class="tw-room-note" data-tone="info">لا يستطيع فحص الجودة الحكم على قاعات هذا اللقاء؛ صُنّفت القاعات حسب النوع والجنس والمقاعد فقط.</p>',
  };
  // The reports of every room of the first case (each kind of report, each
  // judged by the legacy rule); the drawing of every case.
  const first = c => c === LEGACY_DIALOG_CASES[0];
  for (const [how, wrap, q, saves] of [
    ['the quality check cannot judge the scenario’s rooms', withAudit({ ...AUDIT_TARGET, current: judged(['gender'], ['incompatible']) }), unjudged, first],
    ['the audit did not judge the meeting', withAudit({ assessed: false, reason: 'duplicate_room_codes' }), null, first],
  ]) {
    const produced = plainOf(await legacyDialogs(t, { wrap, quality: q, saves }));
    for (const c of LEGACY_DIALOG_CASES) {
      const { body, reports, ...rest } = produced[c.name];
      const want = recorded[c.name];
      const note = notes[c.arabic ? 'ar' : 'en'];
      assert.equal(body.split(note).length, 2, `${how}: ${c.name}: the note, once`);
      assert.equal(body.replace(note, ''), want.body, `${how}: ${c.name}`);
      assert.ok(body.indexOf(note) < body.indexOf('id="twRoomFilter"'), `${how}: ${c.name}: above the rooms`);
      assert.deepEqual(rest, { title: want.title, sub: want.sub, note: want.note }, `${how}: ${c.name}`);
      if (saves(c)) assert.deepEqual(reports, want.reports, `${how}: ${c.name}`);
    }
  }
  // The judgement is fixed when the rooms arrive: a quality check answering
  // later re-sorts nothing in the open list; the next open judges again.
  const h = await roomHarness(t, { rooms: () => auditAnswer() });
  await h.select();
  roomByKey(h);
  await settled(h);
  assert.deepEqual(roomGroupsDrawn(h), ['free', 'flagged', 'inUse']);
  roomRadio(h, 'R2').click();
  h.w.TW.quality = unjudged;
  h.w.drawRoomDialog();
  assert.deepEqual([roomGroupsDrawn(h), roomRadio(h, 'R2').checked, roomNote(h)], [['free', 'flagged', 'inUse'], true, [false, '']], 'the open list is the one the room was chosen from');
  keyOn(h, 'Escape');
  roomByKey(h);
  await settled(h);
  assert.equal(h.reads.length, 1, 'the kept answer');
  assert.deepEqual(roomGroupsDrawn(h), ['free', 'outside', 'inUse'], 'judged again: the legacy rule');
  assert.ok(h.d.getElementById('twRoomBody').innerHTML.includes(notes.en));
  assert.equal(h.d.querySelector('#twRoomBody .tw-room-current').textContent, 'Current room: LAB25', 'no audit words on the current room');
});

test('RC27 the audit’s notes about the meeting: a copy staying in its room, a board with no programme, seats assumed; each code isolated and every label escaped', async t => {
  const label = '<img src=x onerror="alert(1)">\'"';
  const kept = [{ placement_id: 31, board_id: 3, board_label: label, room: 'R50' }];
  const boards = [{ board_id: 2, board_label: 'Term 7', programmes: ['AI'] }, { board_id: 3, board_label: label, programmes: [] }];
  const rooms = [rc('R1', { audit: judged(['programme', 'split'], ['incompatible'], { programme_board_ids: [3] }) }), rc('R50', { audit: judged(['programme'], ['incompatible'], { programme_board_ids: [3] }) }), rc('LAB25', { audit: judged() })];
  const h = await roomHarness(t, { rooms: () => auditAnswer(rooms, { kept, boards, capacity_source: 'default', required_capacity: 40,
    current: judged(['split'], ['incompatible']) }) });
  await h.select();
  roomByKey(h);
  await settled(h);
  const notes = [...h.d.querySelectorAll('#twRoomBody .tw-room-note')];
  assert.deepEqual(notes.map(note => [note.dataset.tone, note.textContent]), [
    ['warning', `The same class stays in \u2066R50\u2069 (\u2066${label}\u2069): any other room leaves it split, which Issues shows as “Room unsuitable”.`],
    ['warning', `\u2066${label}\u2069 has no programme, so Issues shows every room as “Room unsuitable”.`],
    ['info', 'This course has no budget row, so the quality check assumes 40 seats.'],
  ]);
  // Before the filter, after the time's own clashes; the rows say it too.
  const body = h.d.getElementById('twRoomBody');
  assert.ok(body.innerHTML.indexOf('tw-room-note') < body.innerHTML.indexOf('id="twRoomFilter"'));
  assert.equal(radioWords(h, 'R1'), `Lowers quality: Room unsuitable · \u2066${label}\u2069 has no programme · the same class stays in \u2066R50\u2069 (\u2066${label}\u2069)`);
  assert.equal(radioWords(h, 'R50'), `Lowers quality: Room unsuitable · \u2066${label}\u2069 has no programme`);
  assert.equal(h.d.querySelector('#twRoomBody .tw-room-current').textContent,
    `Current room: LAB25 · Lowers quality: Room unsuitable · the same class stays in \u2066R50\u2069 (\u2066${label}\u2069)`);
  // Markup from the answer is text, in every place it is drawn.
  assert.equal(h.d.querySelectorAll('#twRoomDialog img').length, 0);
  assert.equal(roomDialogEl(h).querySelectorAll('[onerror]').length, 0);
  // Notes only where the audit has something to say of the meeting.
  const plain = await roomHarness(t, { rooms: () => auditAnswer() });
  await plain.select();
  roomByKey(plain);
  await settled(plain);
  assert.equal(plain.d.querySelectorAll('#twRoomBody .tw-room-note').length, 0);
  // Arabic, with the time's own clash first.
  const ar = await roomHarness(t, { arabic: true, rooms: () => auditAnswer(rooms.map(timedRoom), {
    kept: [{ ...kept[0], board_label: 'DS T3' }], boards: [boards[0], { ...boards[1], board_label: 'DS T3', programmes: ['DS'] }], capacity_source: 'default', required_capacity: 11 }) });
  await ar.select();
  roomByKey(ar);
  await settled(ar);
  assert.deepEqual([...ar.d.querySelectorAll('#twRoomBody .tw-room-note')].map(note => note.textContent), [
    'أيّاً كانت القاعة، يبقى في وقته تعارضان.',
    'تبقى الشعبة نفسها في \u2066R50\u2069 (\u2066DS T3\u2069)؛ وأي قاعة أخرى تتركها مقسّمة، فتعرضها «المشاكل» «القاعة غير مناسبة».',
    'لا يوجد صف ميزانية لهذا المقرر، لذا يفترض فحص الجودة 11 مقعداً.',
  ]);
  // No rooms on record: that alone, no note about the meeting.
  const empty = await roomHarness(t, { rooms: () => auditAnswer([], { kept, boards, capacity_source: 'default', required_capacity: 40 }) });
  await empty.select();
  roomByKey(empty);
  await wait(() => !empty.d.querySelector('#twRoomBody [aria-busy]'));
  assert.match(empty.d.getElementById('twRoomBody').textContent, /No rooms are on record\./);
  assert.equal(empty.d.querySelectorAll('#twRoomBody .tw-room-note').length, 0);
  // A list cut by the server counts the rows it lists.
  const cut = await roomHarness(t, { rooms: () => auditAnswer(Array.from({ length: 62 }, (_, i) => rc(`X${i}`, { audit: judged() })), {}, { summary: { total: 70 } }) });
  await cut.select();
  roomByKey(cut);
  await settled(cut);
  assert.match(cut.d.getElementById('twRoomBody').textContent, /Showing the best 62 of 70 rooms\./);
});

test('RC28 a current room the audit refuses is not chosen; its line says why Issues list it; and a meeting with no room has no audit words', async t => {
  // While the rooms load, the line is the room alone, as before the audit.
  const slow = deferred();
  const loading = await roomHarness(t, { rooms: () => slow.promise });
  await loading.select();
  roomByKey(loading);
  await settle();
  assert.equal(loading.d.querySelector('#twRoomBody .tw-room-current').outerHTML, '<p class="tw-room-current">Current room: <bdi>LAB25</bdi></p>');
  slow.resolve(auditAnswer(AUDIT_SET, { current: LAB25_SHORT }));
  await settled(loading);
  const rows = [roomRow({ room: 'R3' })];
  const h = await roomHarness(t, { rows: () => rows, rooms: () => auditAnswer(AUDIT_SET, { current: judged(['type'], ['incompatible']) }) });
  await h.select();
  roomByKey(h);
  await settled(h);
  assert.equal(roomRadios(h).some(radio => radio.checked), false);
  assert.deepEqual(roomNote(h), [true, 'Choose a room.']);
  assert.equal(h.d.querySelector('#twRoomBody .tw-room-current').textContent, 'Current room: R3 · Lowers quality: Room unsuitable · This meeting needs a lecture room');
  // In use on this board now: critical, the clash first.
  const held = await roomHarness(t, { rows: () => [roomRow({ room: 'R9' })], rooms: () => auditAnswer(AUDIT_SET, { current: judged() }) });
  await held.select();
  roomByKey(held);
  await settled(held);
  assert.equal(roomRadio(held, 'R9').checked, true, 'a current room in use can be chosen, so it is');
  assert.equal(held.d.querySelector('#twRoomBody .tw-room-current').textContent, 'Current room: R9 · Blocks publishing: Room double-booked');
  assert.equal(held.d.querySelector('#twRoomBody .tw-room-current .tw-room-verdict').dataset.tone, 'critical');
  // Free and fitting: the line is the room alone.
  const fine = await roomHarness(t, { rows: () => [roomRow({ room: 'R1' })], rooms: () => auditAnswer(AUDIT_SET, { current: judged() }) });
  await fine.select();
  roomByKey(fine);
  await settled(fine);
  assert.equal(fine.d.querySelector('#twRoomBody .tw-room-current').innerHTML, 'Current room: <bdi>R1</bdi>');
  // Rooms that lower quality can be chosen: a list of only those and misfits
  // is not "none fit", and its misfits stay closed.
  const only = await roomHarness(t, { rows: () => [roomRow({ room: 'R3' })], rooms: () => auditAnswer(AUDIT_SET.filter(room => ['R7', 'R3'].includes(room.room_code))) });
  await only.select();
  roomByKey(only);
  await settled(only);
  assert.deepEqual(roomRadios(only).map(radio => radio.value), ['R7']);
  assert.doesNotMatch(only.d.getElementById('twRoomBody').textContent, /No room fits this meeting\./);
  assert.equal(only.d.getElementById('twRoomMisfits').hidden, true);
  // No room yet: the audit has no current room.
  const none = await roomHarness(t, { rows: () => [roomRow({ room: 'UNASSIGNED', room_state: 'unassigned' })], rooms: () => auditAnswer() });
  await none.select();
  roomByKey(none);
  await settled(none);
  assert.equal(none.d.querySelector('#twRoomBody .tw-room-current').textContent, 'Current room: No room');
});

/* ── Move: M, Selection's Move… and the dialog (design record §7.3, 5e) ──
   The page reads the times the server offers a manual move with its verdict
   (what the publish check will say once the move is saved), lists them in
   the verdict's order, and sends {day, start_time, end_time} alone, through
   the one commitPlacementMove a drop uses too. CS323 S1 meets on Sunday
   09:00 in LAB25; the server's array order (the legacy one) differs from
   the verdict's on purpose. */
const MOVE_COLUMNS = [{ label: '09:00', start: '09:00', end: '10:15' }, { label: '10:30', start: '10:30', end: '11:45' }, { label: '13:00', start: '13:00', end: '14:15' }];
const vItem = (issue_kind, severity, extra = {}) => ({ issue_kind, severity, source: issue_kind, meeting: null, other: null, room: null,
  teacher: null, students: null, blocked_students: null, ...extra });
const otherAt = (code, section, day, start, end, extra = {}) => ({ course_code: code, section, board_id: 2, board_label: 'Term 7', day, start, end, ...extra });
const verdictOf = (items = [], extra = {}) => {
  const counts = { critical: 0, warning: 0, info: 0 };
  items.forEach(item => { counts[item.severity]++; });
  return { severity: ['critical', 'warning', 'info'].find(s => counts[s]) || 'ok', counts, students: 0,
    items: items.slice(0, 8), more: Math.max(0, items.length - 8), own_section_meeting: false, ...extra };
};
const slotAt = (day, start, end, rank, verdict = verdictOf(), extra = {}) => ({ kind: 'lect', day, start, end, score: rank * 10, tone: 'clean',
  critical_count: 0, warning_count: 0, rank, verdict, verdict_rank: rank, ...extra });
const ROOM_AT_MON = vItem('room', 'critical', { other: otherAt('AI222', 'S1', 'MON', '13:00', '14:15'), room: 'LAB25' });
const SOFT_AT_WED = vItem('softOverlap', 'warning', { source: 'students', other: otherAt('AI555', 'S1', 'WED', '10:30', '11:45'), students: 12, blocked_students: 0 });
// In the server's (legacy) order: a room clash, a time it calls "avoid" that
// the verdict finds clean, an overlap students can route round, and a time
// where the section already meets.
const MOVE_SET = [
  slotAt('MON', '13:00', '14:15', 3, verdictOf([ROOM_AT_MON]), { tone: 'risky' }),
  slotAt('TUE', '09:00', '10:15', 1, verdictOf(), { tone: 'avoid', critical_count: 1 }),
  slotAt('WED', '10:30', '11:45', 2, verdictOf([SOFT_AT_WED])),
  slotAt('SUN', '13:00', '14:15', 4, verdictOf([], { own_section_meeting: true })),
];
const movesAnswer = (candidates = MOVE_SET, extra = {}) => ({ ok: true,
  placement: { id: 10, course_code: 'CS323', section: 'S1', board_id: 2, board_label: 'Term 7', day: 'SUN', start: '09:00', end: '10:15' },
  current_verdict: verdictOf(), hard_student_threshold: 20, candidates, ...extra });
const moveRow = extra => at(10, { course_code: 'CS323', room: 'LAB25', board_id: 2, ...extra });
const timesOf = body => ({ day: body.day, start_time: body.start_time, end_time: body.end_time });
// Unless a test says otherwise, the server answers for the meeting where it
// is now (a saved move included), as the real preview does.
function moveHarness(t, { rows = () => [moveRow()], moves = null, move = null, conflicts = () => quiet, more = () => undefined, arabic = false, extra = {} } = {}) {
  const posts = [], reads = [];
  const saved = new Map();
  const where = id => {
    const row = { ...rows().find(r => r.id === id), ...(saved.get(id) || {}) };
    return { id, course_code: row.course_code, section: row.section, board_id: 2, board_label: 'Term 7', day: row.day, start: row.start_time, end: row.end_time };
  };
  return harness(t, async (url, opts) => {
    const own = await more(url, opts);
    if (own !== undefined) return own;
    if (url.includes('/slot-candidates/')) {
      reads.push(url);
      return moves ? moves(reads.length, url) : movesAnswer(MOVE_SET, { placement: where(Number(/placements\/(\d+)\//.exec(url)[1])) });
    }
    const moving = /^\/ops\/tw\/placements\/(\d+)\/move\/$/.exec(url);
    if (moving && opts.method === 'POST') {
      const body = JSON.parse(opts.body);
      posts.push(body);
      if (move) return move(body, posts.length, saved);
      saved.set(Number(moving[1]), timesOf(body));
      const row = { ...rows().find(r => r.id === Number(moving[1])), ...timesOf(body) };
      return { ok: true, placement: row, placements: [row], validation: { critical_count: 0 } };
    }
    if (url === '/ops/tw/boards/2/') return { placements: rows().map(row => (saved.has(row.id) ? { ...row, ...saved.get(row.id) } : row)), slot_config: MOVE_COLUMNS };
    if (url.includes('/conflicts/')) return conflicts();
    if (url.includes('/quality/')) return noRooms;
    if (url.includes('/readiness/')) return { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
    return undefined;
  }, extra, arabic).then(h => Object.assign(h, { posts, reads, saved }));
}
const moveDialogEl = h => h.d.getElementById('twMoveDialog');
const moveRadios = h => [...h.d.querySelectorAll('#twMoveBody input[name="twMoveChoice"]')];
const moveRadio = (h, key) => moveRadios(h).find(radio => radio.value === key);
const moveNote = h => [h.d.getElementById('twMoveCommit').disabled, h.d.getElementById('twMoveNote').textContent];
const moveStatus = h => h.d.getElementById('twMoveStatus').textContent;
const moveAlert = h => (h.d.getElementById('twMoveAlert').hidden ? '' : h.d.getElementById('twMoveAlert').textContent);
const moveSaid = (h, radio, index) => h.d.getElementById(radio.getAttribute('aria-describedby').split(' ')[index]).textContent;
const moveName = (h, radio) => h.d.getElementById(radio.getAttribute('aria-labelledby')).textContent;
// M on the card (a letter as the keyboard gives it: lower case).
const moveByKey = (h, id = 10) => { card(h, id).focus(); return keyOn(h, 'm'); };
const movesSettled = h => wait(() => moveRadios(h).length > 0);
const atRest = h => h.d.querySelector('.tw-workspace').dataset.busy === 'false';
const LOCKED_WORDS = "This section's time is locked. Unlock its time to move it; its room can still change.";
const EXTERNAL_WORDS = "Another scenario owns this section's time and room; change it there.";
const BUSY_WORDS = 'Wait for the current operation to finish.';

test('M1 one read with the verdict; the times in the verdict’s order and words (never the legacy tone); the best time chosen, the keyboard on it', async t => {
  const h = await moveHarness(t);
  await h.select();
  assert.equal(moveByKey(h).defaultPrevented, true);
  await movesSettled(h);
  assert.deepEqual(h.reads, ['/ops/tw/placements/10/slot-candidates/?verdict=1&offer=manual']);
  assert.equal(h.d.getElementById('twMoveTitle').textContent, 'Move CS323 S1');
  assert.equal(h.d.getElementById('twMoveSub').textContent, 'Sunday 09:00–10:15. Its room stays LAB25.');
  assert.deepEqual(moveRadios(h).map(radio => [radio.value, radio.closest('.ux-choice').dataset.tone, radio.dataset.focusKey]),
    [['TUE|09:00', 'ok', 'move-choice:TUE|09:00'], ['WED|10:30', 'warning', 'move-choice:WED|10:30'], ['MON|13:00', 'critical', 'move-choice:MON|13:00']]);
  assert.deepEqual(moveRadios(h).map(radio => moveName(h, radio)), ['Tuesday 09:00–10:15', 'Wednesday 10:30–11:45', 'Monday 13:00–14:15']);
  assert.deepEqual(moveRadios(h).map(radio => moveSaid(h, radio, 0)),
    ['No clash found', 'Lowers quality: Overlap students can route round', 'Blocks publishing: Room double-booked']);
  assert.deepEqual(moveRadios(h).map(radio => radio.closest('.ux-choice').querySelector('.ux-tone-icon').dataset.tone), ['ok', 'warning', 'critical']);
  assert.equal(moveSaid(h, moveRadio(h, 'MON|13:00'), 1), 'Room double-booked: LAB25 — AI222 S1 · Mon 13:00–14:15');
  assert.equal(moveSaid(h, moveRadio(h, 'WED|10:30'), 1), 'Overlap students can route round: AI555 S1 · Wed 10:30–11:45 · 12 students need both');
  // A time the section already meets at: last, a row with its reason, never a radio.
  const rows = [...h.d.querySelectorAll('#twMoveChoices > .ux-choice')];
  assert.equal(rows.length, 4);
  assert.deepEqual([rows[3].matches('.ux-choice-static'), rows[3].querySelector('input'), rows[3].textContent],
    [true, null, 'Sunday 13:00–14:15Another meeting of this section is at this time.']);
  // Now: the meeting's own time, by the current verdict, and the legend.
  const now = h.d.querySelector('#twMoveBody .tw-move-now');
  assert.deepEqual([now.matches('.ux-choice-static'), now.dataset.tone, now.textContent], [true, 'ok', 'NowSunday 09:00–10:15No clash found']);
  assert.equal(h.d.querySelector('#twMoveBody fieldset > legend').textContent, 'Other times on the grid, best first');
  assert.ok(h.d.getElementById('twMoveChoices').matches('.ux-choice-list[data-ux-submit]'));
  // The best time that can be chosen is chosen, the keyboard goes to it,
  // and the note says what it brings.
  assert.equal(moveRadio(h, 'TUE|09:00').checked, true);
  assert.equal(h.d.activeElement, moveRadio(h, 'TUE|09:00'));
  assert.deepEqual(moveNote(h), [false, 'Chosen: No clash found']);
  // The commit's words, in both languages (the template's).
  assert.match(template, /id="twMoveCommit"[^>]*>{% if LANGUAGE_CODE == 'ar' %}نقل إلى هذا الوقت{% else %}Move to this time{% endif %}<\/button>/);
  assert.equal(moveStatus(h), '');
  moveRadio(h, 'MON|13:00').click();
  assert.deepEqual(moveNote(h), [false, 'Chosen: Blocks publishing: Room double-booked']);
  // Three lines at most, then how many more; context kinds only when nothing
  // worse is there.
  const many = [1, 2, 3, 4].map(n => vItem('time', 'critical', { source: 'students', other: otherAt(`AI${n}`, 'S1', 'TUE', '09:00', '10:15'), students: 20 + n, blocked_students: 20 + n }));
  const busy = await moveHarness(t, { moves: () => movesAnswer([
    slotAt('TUE', '09:00', '10:15', 1, verdictOf([...many, vItem('crossBoardMinor', 'info', { other: otherAt('AI9', 'S2', 'TUE', '09:00', '10:15', { board_id: 3, board_label: 'Term 4' }), students: 4 })], { more: 2 })),
    slotAt('WED', '10:30', '11:45', 2, verdictOf([vItem('crossBoardMinor', 'info', { other: otherAt('AI9', 'S2', 'WED', '10:30', '11:45', { board_id: 3, board_label: 'Term 4' }), students: 4 })])),
  ]) });
  await busy.select();
  moveByKey(busy);
  await movesSettled(busy);
  const first = moveRadio(busy, 'TUE|09:00');
  assert.deepEqual(first.getAttribute('aria-describedby').split(' ').map(id => busy.d.getElementById(id).textContent), [
    'Blocks publishing: Time clash',
    'Time clash: AI1 S1 · Tue 09:00–10:15 · 21 students need both',
    'Time clash: AI2 S1 · Tue 09:00–10:15 · 22 students need both',
    'Time clash: AI3 S1 · Tue 09:00–10:15 · 23 students need both',
    'and 4 more',
  ]);
  const minor = moveRadio(busy, 'WED|10:30');
  assert.deepEqual([minor.closest('.ux-choice').dataset.tone, moveSaid(busy, minor, 0), moveSaid(busy, minor, 1)],
    ['info', 'For context: Possible overlap with another board', 'Possible overlap with another board: AI9 S2 (Term 4) · Wed 10:30–11:45 · 4 students need both']);
  // The first time that can be chosen is chosen, whatever the rank of a
  // time where the section already meets.
  const own = await moveHarness(t, { moves: () => movesAnswer([slotAt('TUE', '09:00', '10:15', 2), slotAt('SUN', '13:00', '14:15', 1, verdictOf([], { own_section_meeting: true }))]) });
  await own.select();
  moveByKey(own);
  await movesSettled(own);
  assert.deepEqual([moveRadios(own).map(radio => [radio.value, radio.checked]), moveNote(own)], [[['TUE|09:00', true]], [false, 'Chosen: No clash found']]);
});

test('M2 the move sends {day, start_time, end_time} alone and is undone as a drop is; a drop goes through the same commitPlacementMove', async t => {
  const h = await moveHarness(t);
  await h.select();
  moveByKey(h);
  await movesSettled(h);
  moveRadio(h, 'WED|10:30').click();
  h.w.TW.redoStack.push({ type: 'room', placement_id: 10, old_room: 'X', new_room: 'Y' });
  h.d.getElementById('twMoveCommit').click();
  await wait(() => h.notices.some(n => n.type === 'success') && atRest(h));
  assert.deepEqual(h.posts, [{ day: 'WED', start_time: '10:30', end_time: '11:45' }], 'the time alone: no room, no override');
  const entry = plainOf(h.w.TW.undoStack.at(-1));
  assert.deepEqual(entry, { type: 'move', placement_id: 10, old_day: 'SUN', old_start: '09:00', old_end: '10:15', new_day: 'WED', new_start: '10:30', new_end: '11:45' });
  assert.equal(h.w.TW.redoStack.length, 0);
  assert.equal(moveDialogEl(h).hasAttribute('open'), false);
  assert.deepEqual(h.notices.at(-1), { type: 'success', text: 'Moved to Wednesday \u206610:30–11:45\u2069.' });
  assert.equal(h.reads.length, 1);
  // Undo sends the old time back as the board had it, Redo the new one.
  h.d.getElementById('twUndo').click();
  await wait(() => h.posts.length === 2 && !h.d.getElementById('twRedo').disabled);
  h.d.getElementById('twRedo').click();
  await wait(() => h.posts.length === 3 && !h.d.getElementById('twUndo').disabled);
  assert.deepEqual(h.posts.slice(1), [{ day: 'SUN', start_time: '09:00', end_time: '10:15' }, { day: 'WED', start_time: '10:30', end_time: '11:45' }]);
  // The dialog's own choice, moved to with Enter: the write that lands
  // reads nothing more while the dialog closes.
  const a = await moveHarness(t);
  await a.select();
  moveByKey(a);
  await movesSettled(a);
  keyOn(a, 'Enter');
  await wait(() => a.notices.some(n => n.type === 'success') && atRest(a));
  assert.deepEqual([a.posts, a.reads.length], [[{ day: 'TUE', start_time: '09:00', end_time: '10:15' }], 1], 'nothing read again while moving');
  // A drop: the same function, the same body, the same undo entry, the same report.
  const d = await moveHarness(t);
  await d.select();
  const calls = [];
  const shared = d.w.commitPlacementMove;
  d.w.commitPlacementMove = (...args) => { calls.push(plainOf(args)); return shared(...args); };
  const cell = d.d.querySelector('#twGrid [role="gridcell"][data-day="WED"][data-start="10:30"]');
  await d.w.onDrop({ preventDefault() {}, currentTarget: cell, dataTransfer: { getData: () => JSON.stringify({ type: 'move', placement_id: 10 }) } });
  assert.deepEqual(calls, [[10, { day: 'WED', start_time: '10:30', end_time: '11:45' }]]);
  assert.deepEqual(d.posts, [{ day: 'WED', start_time: '10:30', end_time: '11:45' }]);
  assert.deepEqual(plainOf(d.w.TW.undoStack.at(-1)), entry);
  assert.deepEqual(d.notices.at(-1), { type: 'success', text: 'Moved to Wednesday \u206610:30–11:45\u2069.' });
  // A refused drop is twFetch's toast, as it always was: nothing undoable.
  const r = await moveHarness(t, { move: () => ({ httpError: 409, error: { code: 'PLACEMENT_DUPLICATE', message: 'Duplicate placement at new position' } }) });
  await r.select();
  await r.w.onDrop({ preventDefault() {}, currentTarget: r.d.querySelector('#twGrid [role="gridcell"][data-day="WED"][data-start="10:30"]'), dataTransfer: { getData: () => JSON.stringify({ type: 'move', placement_id: 10 }) } });
  assert.deepEqual([r.notices.at(-1), r.w.TW.undoStack.length], [{ type: 'error', text: 'Duplicate placement at new position' }, 0]);
});

test('M3 a time-locked meeting is refused before anything is read and Move… is held with the words through any sync; another scenario’s section says so first', async t => {
  for (const [row, words] of [[{ is_locked: true }, LOCKED_WORDS], [{ shared_with_other_scenario: true }, EXTERNAL_WORDS], [{ is_locked: true, shared_with_other_scenario: true }, EXTERNAL_WORDS]]) {
    const h = await moveHarness(t, { rows: () => [moveRow(row)] });
    await h.select();
    card(h, 10).click();
    const button = h.d.querySelector('#twRightBody [data-tw-action="move"]');
    const note = h.d.getElementById('twSelActionNote');
    const state = () => [button.disabled, button.hasAttribute('data-tw-hold'), button.getAttribute('aria-describedby'), note.textContent, note.hidden];
    assert.deepEqual([button.textContent, button.getAttribute('aria-haspopup')], ['Move…', 'dialog']);
    assert.deepEqual(state(), [true, true, 'twSelActionNote', words, false], JSON.stringify(row));
    h.w.syncWorkspaceControls();
    assert.deepEqual(state(), [true, true, 'twSelActionNote', words, false], `${JSON.stringify(row)}: held through a sync`);
    const toasts = h.notices.length;
    assert.equal(moveByKey(h).defaultPrevented, true);
    assert.deepEqual(h.notices.slice(toasts), [{ type: 'warning', text: words }], JSON.stringify(row));
    assert.deepEqual([h.reads, moveDialogEl(h).hasAttribute('open')], [[], false], JSON.stringify(row));
    // The seam itself refuses too (Selection's button is off; a call is not).
    assert.equal(h.w.TW.keyActions.move(h.w.TW.placements[0], { origin: 'sel:move:10' }), false);
    assert.deepEqual(h.reads, []);
  }
  // Free to move: Selection's Move… sits before Lock, and opens the dialog.
  const h = await moveHarness(t);
  await h.select();
  card(h, 10).click();
  const keys = [...h.d.querySelectorAll('#twRightBody .tw-sel-actions button')].map(button => button.dataset.focusKey);
  assert.deepEqual(keys, ['move', 'lock', 'remove']);
  const button = h.d.querySelector('#twRightBody [data-tw-action="move"]');
  assert.deepEqual([button.disabled, button.hasAttribute('data-tw-hold'), button.hasAttribute('aria-describedby'), h.d.getElementById('twSelActionNote').hidden], [false, false, false, true]);
  button.click();
  await movesSettled(h);
  assert.deepEqual(h.reads, ['/ops/tw/placements/10/slot-candidates/?verdict=1&offer=manual']);
});

test('M4 an English section goes to its pattern chooser, after the gate: no Move… and no move dialog; after a switch the board’s stop is on the new row', async t => {
  const eng = (id, day) => at(id, { course_code: 'ENG101', course_key: 'ENG101', section: 'S1', term_section_id: 300, day, start_time: '09:00', end_time: '12:00', room: '', needs_room: false });
  const english = [eng(30, 'SUN'), eng(31, 'MON'), eng(32, 'WED'), eng(33, 'THU')];
  const englishHarness = (extra = {}) => {
    let rows = english;
    const seen = { choose: 0, switched: [] };
    return moveHarness(t, {
      extra,
      rows: () => rows,
      more: (url, opts) => {
        if (url === '/ops/tw/placements/30/slot-candidates/') return { section_policy: { current_pattern_id: 'ENG_INTENSIVE_V1_AM' }, candidates: [] };
        if (url === '/ops/tw/placements/30/switch-pattern/' && opts.method === 'POST') {
          seen.switched.push(JSON.parse(opts.body));
          rows = english.map((row, i) => ({ ...row, id: 40 + i, start_time: '13:00', end_time: '16:00' }));
          return { ok: true, placements: [{ ...rows[0], board_id: 9, id: 49 }, ...rows], before_placements: english };
        }
        return undefined;
      },
    }).then(h => {
      h.w.TimetableMeetingPolicy.choose = async () => { seen.choose++; return h.w.TimetableMeetingPolicy.patterns[1]; };
      return Object.assign(h, { seen });
    });
  };
  const h = await englishHarness();
  await h.select();
  card(h, 30).click();
  assert.equal(h.d.querySelector('#twRightBody [data-tw-action="move"]'), null, 'no Move…: the section has its pattern');
  assert.ok(h.d.querySelector('#twRightBody [data-focus-key="pattern"]'));
  assert.equal(moveByKey(h, 30).defaultPrevented, true);
  assert.equal(h.d.getElementById('twBoardStatus').textContent, 'Finding patterns…', 'said before the read');
  await wait(() => h.seen.switched.length === 1 && atRest(h) && card(h, 40));
  assert.deepEqual([h.seen.choose, h.reads, moveDialogEl(h).hasAttribute('open')], [1, [], false], 'the chooser, never the move dialog');
  assert.deepEqual(h.seen.switched, [{ pattern_id: 'ENG_INTENSIVE_V1_PM' }]);
  // The new row of the meeting's day on this board (not a copy elsewhere)
  // holds the board's stop, and the keyboard with it.
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')].map(element => element.dataset.focusKey), ['placement:40']);
  assert.equal(h.d.activeElement, card(h, 40));
  // Published: the gate speaks first, from the key and from the seam itself;
  // the chooser is never asked.
  const p = await englishHarness({ status: 'published' });
  await p.select();
  const toasts = p.notices.length;
  moveByKey(p, 30);
  assert.equal(p.w.TW.keyActions.move(p.w.TW.placements[0], { origin: 'placement:30' }), false);
  await settle();
  const published = { type: 'warning', text: 'This scenario is published and read-only.' };
  assert.deepEqual([p.notices.slice(toasts), p.seen.choose, p.d.getElementById('twBoardStatus').textContent], [[published, published], 0, '']);
  // Busy: the chooser saves as soon as a pattern is chosen, so it waits, and says why.
  const b = await englishHarness();
  await b.select();
  const write = deferred();
  b.w.fetch = (fetch => async (url, opts) => (url === '/ops/tw/x/' ? { ok: true, status: 200, json: async () => write.promise } : fetch(url, opts)))(b.w.fetch);
  const writing = b.w.twFetch('/ops/tw/x/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  const before = b.notices.length;
  moveByKey(b, 30);
  assert.deepEqual([b.notices.slice(before), b.seen.choose, b.d.getElementById('twBoardStatus').textContent], [[{ type: 'warning', text: BUSY_WORDS }], 0, '']);
  write.resolve({ ok: true });
  await writing;
  // The chooser gives the keyboard back to the node it was opened from; a
  // card drawn again meanwhile is gone, and the board's stop takes it.
  const c = await englishHarness();
  await c.select();
  c.w.TimetableMeetingPolicy.choose = async () => {
    const away = c.d.createElement('button');
    c.d.body.append(away);
    const previous = c.d.activeElement;
    away.focus();
    c.w.renderGrid();
    away.remove();
    previous.focus();
    return null;
  };
  moveByKey(c, 30);
  await wait(() => !c.w.TW.englishPicker);
  assert.equal(c.d.activeElement, card(c, 30), 'the card drawn now');
});

test('M5 a write in flight never holds the dialog: it opens, Commit is held with the busy note, and comes back on by itself', async t => {
  const write = deferred();
  const h = await moveHarness(t, { more: url => (url === '/ops/tw/x/' ? write.promise : undefined) });
  await h.select();
  const writing = h.w.twFetch('/ops/tw/x/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  await settle();
  assert.equal(moveByKey(h).defaultPrevented, true);
  await movesSettled(h);
  assert.equal(moveDialogEl(h).hasAttribute('open'), true);
  assert.deepEqual(moveNote(h), [true, BUSY_WORDS]);
  assert.equal(h.d.getElementById('twMoveCommit').hasAttribute('data-tw-hold'), false, 'held by the page, not by the dialog');
  write.resolve({ ok: true });
  await writing;
  // The write moved the data: the times are read again, and Commit is back.
  await wait(() => h.reads.length === 2 && moveRadios(h).length > 0);
  assert.deepEqual(moveNote(h), [false, 'Chosen: No clash found']);
});

test('M6 the keyboard goes back to whatever opened the dialog: an unselected card while another is selected, the selected card, Selection’s Move…', async t => {
  const h = await moveHarness(t, { rows: () => [moveRow(), at(11, { day: 'MON' })] });
  await h.select();
  card(h, 11).click();
  moveByKey(h, 10);
  await movesSettled(h);
  keyOn(h, 'Escape');
  assert.equal(moveDialogEl(h).hasAttribute('open'), false);
  assert.equal(h.d.activeElement, card(h, 10), 'the card M was pressed on');
  moveByKey(h, 11);
  await movesSettled(h);
  keyOn(h, 'Escape');
  assert.equal(h.d.activeElement, card(h, 11));
  const button = h.d.querySelector('#twRightBody [data-tw-action="move"]');
  button.focus();
  button.click();
  await movesSettled(h);
  h.w.renderRightPanel('selection');
  moveDialogEl(h).querySelector('.ux-dialog-foot [data-dialog-close]').click();
  assert.equal(h.d.activeElement, h.d.querySelector('#twRightBody [data-tw-action="move"][data-placement-id="11"]'));
  // Moved from Selection while the board's tab stop is on another card: the
  // keyboard goes back to Move…, and the board's stop to the moved card.
  const s = await moveHarness(t, { rows: () => [moveRow(), at(11, { day: 'MON' })] });
  await s.select();
  card(s, 11).focus();
  card(s, 10).click();
  const move = s.d.querySelector('#twRightBody [data-tw-action="move"]');
  move.focus();
  move.click();
  await movesSettled(s);
  s.d.getElementById('twMoveCommit').click();
  await wait(() => s.notices.some(n => n.type === 'success') && atRest(s));
  assert.deepEqual([...s.d.querySelectorAll('#twGrid [tabindex="0"]')].map(element => element.dataset.focusKey), ['placement:10']);
  assert.equal(s.d.activeElement, s.d.querySelector('#twRightBody [data-tw-action="move"][data-placement-id="10"]'));
});

test('M7 committing: the keyboard is on the chosen row before Commit turns off; the list is busy, the status line is not', async t => {
  const answer = deferred();
  let seen = null;
  const h = await moveHarness(t, { move: () => answer.promise });
  await h.select();
  moveByKey(h);
  await movesSettled(h);
  moveRadio(h, 'WED|10:30').click();
  const commit = h.d.getElementById('twMoveCommit');
  commit.focus();
  const fetch = h.w.fetch;
  h.w.fetch = async (url, opts) => {
    if (opts?.method === 'POST') seen = [h.d.activeElement, commit.disabled];
    return fetch(url, opts);
  };
  // Where the keyboard is at the very moment Commit turns off.
  let atOff = null;
  const original = Object.getOwnPropertyDescriptor(h.w.HTMLButtonElement.prototype, 'disabled');
  Object.defineProperty(commit, 'disabled', {
    configurable: true,
    get() { return original.get.call(this); },
    set(value) { if (value && !atOff) atOff = h.d.activeElement; original.set.call(this, value); },
  });
  commit.click();
  await settle();
  assert.equal(atOff, moveRadio(h, 'WED|10:30'), 'the keyboard is on the chosen row when Commit turns off');
  assert.deepEqual(seen, [moveRadio(h, 'WED|10:30'), true], 'the keyboard on the chosen row as the request leaves');
  assert.equal(h.d.getElementById('twMoveChoices').getAttribute('aria-busy'), 'true');
  assert.deepEqual([moveStatus(h), h.d.getElementById('twMoveStatus').closest('[aria-busy="true"]')], ['Moving…', null]);
  assert.deepEqual(moveNote(h), [true, '']);
  answer.resolve({ ok: true, placement: moveRow(), placements: [moveRow()] });
  await wait(() => !moveDialogEl(h).hasAttribute('open') && atRest(h));
});

test('M8 a refused move is said in the dialog, never as a toast; what asking again cannot change holds Commit; Escape during a commit closes it and the result is a toast', async t => {
  const cases = [
    ['PLACEMENT_LOCKED', 400, LOCKED_WORDS, true],
    ['PLACEMENT_EXTERNALLY_OWNED', 409, EXTERNAL_WORDS, true],
    ['PLACEMENT_DUPLICATE', 409, 'Another meeting of this section is at this time.', false],
    ['SCENARIO_PUBLISHED', 400, 'This scenario is published and read-only.', true],
    ['SCENARIO_BUSY', 409, BUSY_WORDS, false],
    ['NOT_FOUND', 404, 'This meeting is no longer on the board.', true],
  ];
  for (const [code, status, words, held] of cases) {
    const h = await moveHarness(t, { move: () => ({ httpError: status, error: { code, message: 'Refused' } }) });
    await h.select();
    moveByKey(h);
    await movesSettled(h);
    const toasts = h.notices.length;
    const boards = h.requests.filter(r => r.url === '/ops/tw/boards/2/').length;
    h.d.getElementById('twMoveCommit').click();
    await wait(() => moveAlert(h) && atRest(h));
    await settle();
    assert.deepEqual([moveAlert(h), h.d.getElementById('twMoveAlert').dataset.tone], [words, 'warning'], code);
    assert.equal(h.d.getElementById('twMoveCommit').disabled, held, code);
    assert.deepEqual([h.notices.slice(toasts), h.w.TW.undoStack.length, moveDialogEl(h).hasAttribute('open')], [[], 0, true], code);
    assert.equal(h.requests.filter(r => r.url === '/ops/tw/boards/2/').length, boards, `${code}: a known refusal reads nothing`);
  }
  // Escape while the write is out closes the dialog; its refusal, then a
  // success, arrive as toasts.
  let answer = deferred();
  const h = await moveHarness(t, { move: () => answer.promise });
  await h.select();
  moveByKey(h);
  await movesSettled(h);
  const toasts = h.notices.length;
  h.d.getElementById('twMoveCommit').click();
  await settle();
  keyOn(h, 'Escape');
  assert.equal(moveDialogEl(h).hasAttribute('open'), false, 'Escape is never blocked by a commit');
  assert.equal(h.d.activeElement, card(h, 10));
  answer.resolve({ httpError: 400, error: { code: 'PLACEMENT_LOCKED', message: 'Locked' } });
  await wait(() => h.notices.length > toasts);
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'warning', text: LOCKED_WORDS }]);
  answer = deferred();
  moveByKey(h);
  await movesSettled(h);
  h.d.getElementById('twMoveCommit').click();
  await settle();
  keyOn(h, 'Escape');
  answer.resolve({ ok: true, placement: moveRow({ day: 'TUE' }), placements: [moveRow({ day: 'TUE' })] });
  await wait(() => h.notices.some(n => n.type === 'success') && atRest(h));
  assert.deepEqual(h.notices.at(-1), { type: 'success', text: 'Moved to Tuesday \u206609:00–10:15\u2069.' });
  assert.equal(h.w.TW.undoStack.length, 1);
});

test('M9 a meeting that changed under the list is not moved: each field of its snapshot stops the commit; a lost answer reads the board, which then stops a second move', async t => {
  const h = await moveHarness(t);
  await h.select();
  moveByKey(h);
  await movesSettled(h);
  const p = h.w.TW.placements[0];
  const changes = [
    ['day', () => { p.day = 'MON'; }, () => { p.day = 'SUN'; }],
    ['start', () => { p.start_time = '09:30'; }, () => { p.start_time = '09:00'; }],
    ['end', () => { p.end_time = '10:45'; }, () => { p.end_time = '10:15'; }],
    ['a lock since', () => { p.is_locked = true; }, () => { p.is_locked = false; }],
    ['another scenario’s claim since', () => { p.shared_with_other_scenario = true; }, () => { p.shared_with_other_scenario = false; }],
    ['gone', () => { h.w.TW.placements = []; }, () => { h.w.TW.placements = [p]; }],
    ['another board', () => { h.w.TW.boardId = 3; }, () => { h.w.TW.boardId = 2; }],
    ['another scenario', () => { h.w.TW.scenarioId = 9; }, () => { h.w.TW.scenarioId = 1; }],
  ];
  const alert = h.d.getElementById('twMoveAlert');
  for (const [name, change, undo] of changes) {
    change();
    h.d.getElementById('twMoveForm').requestSubmit();
    await settle();
    assert.deepEqual([alert.textContent, alert.hidden, h.posts.length], ['This section changed while the list was open. Nothing was moved.', false, 0], name);
    undo();
    alert.textContent = '';
    alert.hidden = true;
    h.w.drawMoveDialog();
    // As read again (a meeting off the board is read again once back).
    await wait(() => moveNote(h)[0] === false);
  }
  // As it was read: the move goes.
  await wait(() => moveNote(h)[0] === false);
  assert.deepEqual(moveNote(h), [false, 'Chosen: No clash found']);
  h.d.getElementById('twMoveForm').requestSubmit();
  await wait(() => h.posts.length === 1);
  // The list shows it too: the times are stale, Check again reads the
  // board, then the times, and the choice starts again.
  let day = 'SUN';
  const s = await moveHarness(t, { rows: () => [moveRow({ day })] });
  await s.select();
  moveByKey(s);
  await movesSettled(s);
  moveRadio(s, 'MON|13:00').click();
  day = 'MON';
  await s.w.selectBoard(2);
  assert.equal(moveStatus(s), 'The timetable changed since these times were read.');
  assert.deepEqual([moveRadios(s).length, moveNote(s)], [0, [true, '']]);
  s.d.getElementById('twMoveForm').requestSubmit();
  await settle();
  assert.equal(s.posts.length, 0);
  const boards = s.requests.filter(r => r.url === '/ops/tw/boards/2/').length;
  s.d.querySelector('#twMoveBody [data-tw-move-recheck]').click();
  await wait(() => s.reads.length === 2 && moveRadios(s).length > 0);
  assert.equal(s.requests.filter(r => r.url === '/ops/tw/boards/2/').length, boards + 1, 'the board, read again');
  assert.equal(s.reads.at(-1), '/ops/tw/placements/10/slot-candidates/?verdict=1&offer=manual');
  // A lost answer: "not saved", and the board is read; this one did move,
  // so a second move from the old list is stopped.
  const n = await moveHarness(t, { move: (body, count, saved) => { saved.set(10, timesOf(body)); throw new TypeError('Failed to fetch'); } });
  await n.select();
  moveByKey(n);
  await movesSettled(n);
  const before = n.requests.length;
  n.d.getElementById('twMoveCommit').click();
  await wait(() => moveAlert(n) && atRest(n));
  assert.equal(moveAlert(n), 'The move was not saved.');
  const after = n.requests.slice(before).map(r => `${r.opts.method || 'GET'} ${r.url}`);
  assert.ok(after.indexOf('GET /ops/tw/boards/2/') > after.indexOf('POST /ops/tw/placements/10/move/'), after.join('\n'));
  assert.equal(moveStatus(n), 'The timetable changed since these times were read.');
  n.d.getElementById('twMoveForm').requestSubmit();
  await settle();
  assert.deepEqual([n.posts.length, moveAlert(n)], [1, 'This section changed while the list was open. Nothing was moved.']);
  assert.equal(n.w.TW.undoStack.length, 0, 'an answer that never came is not undoable');
  // Any other refusal says the same and reads the board; nothing moved, so
  // the user may try again.
  const v = await moveHarness(t, { move: () => ({ httpError: 400, error: { code: 'VALIDATION_ERROR', message: 'Bad time' } }) });
  await v.select();
  moveByKey(v);
  await movesSettled(v);
  const read = v.requests.filter(r => r.url === '/ops/tw/boards/2/').length;
  v.d.getElementById('twMoveCommit').click();
  await wait(() => moveAlert(v) && atRest(v) && v.requests.filter(r => r.url === '/ops/tw/boards/2/').length === read + 1);
  assert.deepEqual([moveAlert(v), moveNote(v)], ['The move was not saved.', [false, 'Chosen: No clash found']]);
});

test('M10 in Arabic: times isolated left to right, counts in their forms, course codes as text', async t => {
  const board4 = { board_id: 4, board_label: 'المستوى 4' };
  const effect = vItem('time', 'critical', { source: 'h15_effect', other: otherAt('AI7', 'S2', 'TUE', '09:00', '10:15', board4), students: 20, blocked_students: 20 });
  const teacher = vItem('instructorElsewhere', 'warning', { source: 'cross_board_instructor', other: otherAt('AI8', 'S1', 'TUE', '09:00', '10:15', board4), teacher: 'د. أحمد' });
  const crossing = vItem('crossBoard', 'warning', { source: 'cross_board_students', other: otherAt('AI9', 'S1', 'TUE', '09:00', '10:15', board4), students: 1 });
  // In the server's order: severity, then kind.
  const items = [
    vItem('time', 'critical', { source: 'students', other: otherAt('<img src=x>', 'S1', 'TUE', '09:00', '10:15'), students: 3, blocked_students: 3 }),
    effect, vItem('blocked', 'critical', { source: 'blocked_slot' }), teacher, crossing,
  ];
  const h = await moveHarness(t, {
    arabic: true,
    rows: () => [moveRow({ meetings: [{ day: 'SUN', start_time: '09:00', end_time: '10:15' }, { day: 'TUE', start_time: '09:00', end_time: '10:15' }] })],
    moves: () => movesAnswer([slotAt('TUE', '09:00', '10:15', 1, verdictOf(items)), slotAt('WED', '10:30', '11:45', 2, verdictOf([teacher, crossing])), MOVE_SET[3]]),
  });
  await h.select();
  moveByKey(h);
  await movesSettled(h);
  const dialog = moveDialogEl(h);
  const lines = key => moveRadio(h, key).getAttribute('aria-describedby').split(' ').map(id => h.d.getElementById(id).textContent);
  assert.equal(h.d.getElementById('twMoveTitle').textContent, 'نقل CS323 S1');
  assert.equal(h.d.getElementById('twMoveSub').innerHTML,
    'هذا اللقاء فقط: الأحد <bdi dir="ltr">09:00–10:15</bdi>. تبقى لقاءات الشعبة الأخرى في أماكنها. وتبقى قاعته <bdi>LAB25</bdi>.');
  const times = [...dialog.querySelectorAll('bdi[dir="ltr"]')];
  assert.ok(times.length >= 6, String(times.length));
  assert.deepEqual(times.filter(bdi => !/^\d\d:\d\d–\d\d:\d\d$/.test(bdi.textContent)).map(bdi => bdi.textContent), []);
  // Every row's name holds its time in one.
  assert.deepEqual(moveRadios(h).map(radio => h.d.getElementById(radio.getAttribute('aria-labelledby')).innerHTML),
    ['الثلاثاء <bdi dir="ltr">09:00–10:15</bdi>', 'الأربعاء <bdi dir="ltr">10:30–11:45</bdi>']);
  assert.deepEqual(lines('TUE|09:00'), [
    'يمنع النشر: تعارض زمني, في وقت محجوب, المدرّس يدرّس في لوحة أخرى في الوقت نفسه, تداخل مع لوحة أخرى',
    'تعارض زمني: <img src=x> S1 · الثلاثاء 09:00–10:15 · 3 طلاب يحتاجون كليهما',
    'تعارض زمني: AI7 S2 (المستوى 4) · الثلاثاء 09:00–10:15 · لا يجد 20 طالباً شعبتين بلا تعارض',
    'في وقت محجوب',
    'وبندان آخران',
  ]);
  assert.deepEqual(lines('WED|10:30'), [
    'يُضعف الجودة: المدرّس يدرّس في لوحة أخرى في الوقت نفسه, تداخل مع لوحة أخرى',
    'المدرّس يدرّس في لوحة أخرى في الوقت نفسه: د. أحمد — AI8 S1 (المستوى 4) · الثلاثاء 09:00–10:15',
    'تداخل مع لوحة أخرى: AI9 S1 (المستوى 4) · الثلاثاء 09:00–10:15 · طالب واحد يحتاج كليهما',
  ]);
  assert.equal(dialog.querySelector('img'), null, 'a course code is text');
  assert.deepEqual([...h.d.querySelectorAll('#twMoveBody .tw-move-now > .ux-choice-title > span:not(.ux-tone-icon)')].map(span => span.textContent),
    ['الآن', 'الأحد 09:00–10:15', 'لا تعارض']);
  assert.equal(h.d.querySelector('#twMoveBody fieldset > legend').textContent, 'أوقات أخرى في الجدول، الأفضل أولاً');
  assert.match(h.d.querySelector('#twMoveChoices .ux-choice-static').textContent, /^الأحد 13:00–14:15لقاء آخر لهذه الشعبة في هذا الوقت\.$/);
  assert.equal(h.d.getElementById('twMoveNote').textContent, 'المختار: يمنع النشر: تعارض زمني, في وقت محجوب, المدرّس يدرّس في لوحة أخرى في الوقت نفسه, تداخل مع لوحة أخرى');
  // Every count form, as drawn: a time whose first item leaves n students
  // with no clash-free pair, with n more items past the three shown.
  const counts = async (arabic, forms) => {
    const c = await moveHarness(t, { arabic, moves: () => movesAnswer(forms.map((n, i) => slotAt(['SUN', 'MON', 'TUE', 'WED', 'THU'][i], '10:30', '11:45', i + 1,
      verdictOf([{ ...effect, blocked_students: n }, items[2], items[2]], { more: n })))) });
    await c.select();
    moveByKey(c);
    await movesSettled(c);
    return moveRadios(c).map(radio => {
      const said = radio.getAttribute('aria-describedby').split(' ').map(id => c.d.getElementById(id).textContent);
      return [said[1].split(' · ').at(-1), said.at(-1)];
    });
  };
  assert.deepEqual(await counts(true, [1, 2, 3, 11, 100]), [
    ['لا يجد طالب واحد شعبتين بلا تعارض', 'وبند آخر'],
    ['لا يجد طالبان شعبتين بلا تعارض', 'وبندان آخران'],
    ['لا يجد 3 طلاب شعبتين بلا تعارض', 'و3 بنود أخرى'],
    ['لا يجد 11 طالباً شعبتين بلا تعارض', 'و11 بنداً آخر'],
    ['لا يجد 100 طالب شعبتين بلا تعارض', 'و100 بند آخر'],
  ]);
  assert.deepEqual(await counts(false, [1, 2]), [
    ['leaves 1 student with no clash-free pair of sections', 'and 1 more'],
    ['leaves 2 students with no clash-free pair of sections', 'and 2 more'],
  ]);
  // The report and the "changed" words, in Arabic.
  assert.equal(h.w.movedWords('MON', '13:00', '14:15'), 'نُقلت إلى الاثنين \u206613:00–14:15\u2069.');
  h.w.TW.placements[0].start_time = '09:30';
  h.d.getElementById('twMoveForm').requestSubmit();
  await settle();
  assert.equal(moveAlert(h), 'تغيّرت هذه الشعبة أثناء فتح القائمة. لم يُنقل شيء.');
  assert.equal(moveStatus(h), 'تغيّر الجدول منذ قراءة هذه الأوقات.');
  // English: one student, one more, and an effect on this board named without one.
  const e = await moveHarness(t, { moves: () => movesAnswer([slotAt('TUE', '09:00', '10:15', 1, verdictOf([
    { ...effect, other: { ...effect.other, board_id: 2, board_label: 'Term 7' }, blocked_students: 1, students: 1 }, teacher, crossing]))]) });
  await e.select();
  moveByKey(e);
  await movesSettled(e);
  assert.deepEqual(moveRadio(e, 'TUE|09:00').getAttribute('aria-describedby').split(' ').slice(1).map(id => e.d.getElementById(id).textContent), [
    'Time clash: AI7 S2 · Tue 09:00–10:15 · leaves 1 student with no clash-free pair of sections',
    'Teacher also teaching on another board: د. أحمد — AI8 S1 (المستوى 4) · Tue 09:00–10:15',
    'Overlap with another board: AI9 S1 (المستوى 4) · Tue 09:00–10:15 · 1 student needs both',
  ]);
});

test('M11 the report reads Issues once the board is read again: what the moved meeting has now, with R’s hint where its room is the clash; a drop says the same', async t => {
  const clash = { ...quiet, room_clashes: [{ ids: [10, 12], room: 'LAB25', sections: ['CS323 S1', 'AI222 S1'] }] };
  const rows = () => [moveRow(), at(12, { course_code: 'AI222', day: 'MON', start_time: '13:00', end_time: '14:15', room: 'LAB25' })];
  const movedHarness = (options = {}) => {
    let moved = false;
    return moveHarness(t, {
      rows,
      conflicts: () => (moved ? clash : quiet),
      move: (body, n, saved) => {
        moved = true;
        saved.set(10, timesOf(body));
        const row = moveRow(timesOf(body));
        return { ok: true, placement: row, placements: [row], ...options };
      },
    });
  };
  const h = await movedHarness();
  await h.select();
  moveByKey(h);
  await movesSettled(h);
  moveRadio(h, 'MON|13:00').click();
  moveRadio(h, 'MON|13:00').focus();
  const before = h.notices.length;
  keyOn(h, 'Enter');
  await wait(() => h.notices.length > before && atRest(h));
  assert.deepEqual(h.notices.slice(before), [{ type: 'warning',
    text: 'Moved to Monday \u206613:00–14:15\u2069. · Blocks publishing: Room double-booked · Press R on the card to change its room.' }]);
  assert.equal(card(h, 10).closest('[role="gridcell"]').dataset.day, 'MON');
  assert.equal(tone(h, 10), 'critical');
  assert.equal(h.d.activeElement, card(h, 10), 'the keyboard on the moved card, at its new time');
  // A drop: the same report, without the hint (it read no verdict), and its
  // teacher backstop as its own toast, as before.
  const d = await movedHarness({ instructor_clash_backstop: { checked: true, involving_this_section: [{ id: 1 }, { id: 2 }] } });
  await d.select();
  const dropped = d.notices.length;
  await d.w.onDrop({ preventDefault() {}, currentTarget: d.d.querySelector('#twGrid [role="gridcell"][data-day="MON"][data-start="13:00"]'),
    dataTransfer: { getData: () => JSON.stringify({ type: 'move', placement_id: 10 }) } });
  assert.deepEqual(d.notices.slice(dropped), [
    { type: 'warning', text: 'Moved to Monday \u206613:00–14:15\u2069. · Blocks publishing: Room double-booked' },
    { type: 'warning', text: 'Instructor double-booked — 2 overlapping session(s) across boards' },
  ]);
  // Only fresh rows about the moved meeting count: a check older than the
  // edit is not, nor context; a fresh warning is.
  Object.assign(h.w.TW, {
    conflicts: { ...quiet, cross_board_conflicts: [{ board_a_id: 2, board_b_id: 3, placement_a_id: 10, placement_b_id: 99, section_a: 'CS323', section_b: 'AI9', overlap_count: 3 }] },
    quality: { ...noRooms, rooms: { assessed: true, shortages: 1, details: [{ course_code: 'CS323', placement_ids: [10], rooms: ['LAB25'], reasons: ['undersized'], required_capacity: 50 }] } },
    qualityVersion: h.w.TW.dataVersion - 1,
  });
  assert.deepEqual(plainOf(h.w.editReport('Moved.', [10])), { text: 'Moved.', tone: 'success' });
  h.w.TW.qualityVersion = h.w.TW.dataVersion;
  assert.deepEqual(plainOf(h.w.editReport('Moved.', [10])), { text: 'Moved. · Lowers quality: Room too small', tone: 'warning' });
  assert.deepEqual(plainOf(h.w.editReport('Moved.', [12])), { text: 'Moved.', tone: 'success' }, 'another meeting’s rows are not this move’s');
  // R's hint only beside a room problem the report names: a room row, yes;
  // none (the clash the verdict saw has gone), or another kind only, no.
  const hint = 'Press R on the card to change its room.';
  assert.equal(plainOf(h.w.editReport('Moved.', [10], hint)).text, `Moved. · Lowers quality: Room too small · ${hint}`);
  h.w.TW.quality = { ...noRooms, rooms: { assessed: true, shortages: 0, details: [] } };
  assert.deepEqual(plainOf(h.w.editReport('Moved.', [10], hint)), { text: 'Moved.', tone: 'success' });
  h.w.TW.conflicts = { ...quiet, cross_board_conflicts: [{ board_a_id: 2, board_b_id: 3, placement_a_id: 10, placement_b_id: 99, section_a: 'CS323', section_b: 'AI9', overlap_count: 30 }] };
  assert.doesNotMatch(plainOf(h.w.editReport('Moved.', [10], hint)).text, /Press R/);
});

test('M12 a move the user left (Escape, then R or M again) ends where the user is: in the dialog open by then, named when that dialog is about another meeting; a refusal of the same meeting holds Commit there', async t => {
  const rows = () => [moveRow(), at(11, { day: 'MON' })];
  const rooms = url => (url.includes('/room-candidates/') ? roomsAnswer(ROOM_SET, { target: { day: 'MON' } }) : undefined);
  // Saved while Change room is open on another meeting: said there, named,
  // with the teacher backstop in the same line; no toast behind it.
  for (const arabic of [false, true]) {
    const landed = deferred();
    const h = await moveHarness(t, {
      arabic, rows, more: rooms,
      move: async (body, n, saved) => {
        await landed.promise;
        saved.set(10, timesOf(body));
        const row = moveRow(timesOf(body));
        return { ok: true, placement: row, placements: [row], instructor_clash_backstop: { checked: true, involving_this_section: [{ id: 1 }] } };
      },
    });
    await h.select();
    moveByKey(h);
    await movesSettled(h);
    moveRadio(h, 'WED|10:30').click();
    h.d.getElementById('twMoveCommit').click();
    await settle();
    keyOn(h, 'Escape');
    roomByKey(h, 11);
    await settled(h);
    const toasts = h.notices.length;
    const line = h.d.getElementById('twRoomAlert');
    landed.resolve();
    await wait(() => !line.hidden && atRest(h));
    assert.deepEqual([line.textContent, line.dataset.tone], arabic
      ? ['\u2066CS323\u2069 \u2066S1\u2069: نُقلت إلى الأربعاء \u206610:30–11:45\u2069. · تعارض للمحاضر: 1 حصة متداخلة عبر اللوحات', 'warning']
      : ['\u2066CS323\u2069 \u2066S1\u2069: Moved to Wednesday \u206610:30–11:45\u2069. · Instructor double-booked — 1 overlapping session(s) across boards', 'warning']);
    assert.deepEqual(h.notices.slice(toasts), [], 'no toast behind the dialog');
    assert.equal(h.w.TW.undoStack.length, 1);
  }
  // Saved while Move is open again on the same meeting: said there as it is,
  // and that dialog reads its times again (the data moved, nothing chosen).
  const landed = deferred();
  const s = await moveHarness(t, { move: async (body, n, saved) => { await landed.promise; saved.set(10, timesOf(body)); return { ok: true, placement: moveRow(timesOf(body)) }; } });
  await s.select();
  moveByKey(s);
  await movesSettled(s);
  moveRadio(s, 'WED|10:30').click();
  s.d.getElementById('twMoveCommit').click();
  await settle();
  keyOn(s, 'Escape');
  moveByKey(s);
  await movesSettled(s);
  assert.deepEqual([s.reads.length, moveNote(s)], [1, [true, BUSY_WORDS]], 'the reopen takes the answer kept for this data; the page is busy');
  landed.resolve();
  await wait(() => moveAlert(s) && atRest(s) && s.reads.length === 2 && moveRadios(s).length > 0);
  assert.deepEqual([moveAlert(s), s.d.getElementById('twMoveAlert').dataset.tone], ['Moved to Wednesday \u206610:30–11:45\u2069.', 'success']);
  assert.equal(moveDialogEl(s).hasAttribute('open'), true, 'the dialog the user opened stays open');
  assert.equal(s.d.getElementById('twMoveSub').textContent, 'Wednesday 10:30–11:45. Its room stays LAB25.', 'about the meeting where it is now');
  // Refused instead, with Move open again on the same meeting: said there,
  // and Commit holds whatever is chosen.
  const refused = deferred();
  const r = await moveHarness(t, { move: () => refused.promise });
  await r.select();
  moveByKey(r);
  await movesSettled(r);
  r.d.getElementById('twMoveCommit').click();
  await settle();
  keyOn(r, 'Escape');
  moveByKey(r);
  await movesSettled(r);
  const quietly = r.notices.length;
  refused.resolve({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  await wait(() => moveAlert(r) && atRest(r));
  await settle();
  assert.deepEqual([moveAlert(r), r.d.getElementById('twMoveAlert').dataset.tone, r.notices.slice(quietly)], [EXTERNAL_WORDS, 'warning', []]);
  moveRadio(r, 'WED|10:30').click();
  assert.equal(r.d.getElementById('twMoveCommit').disabled, true, 'asking again would change nothing');
  // Open on another meeting meanwhile: named, and its own Commit is not held.
  const elsewhere = deferred();
  const m = await moveHarness(t, { rows, move: () => elsewhere.promise });
  await m.select();
  moveByKey(m);
  await movesSettled(m);
  m.d.getElementById('twMoveCommit').click();
  await settle();
  keyOn(m, 'Escape');
  moveByKey(m, 11);
  await movesSettled(m);
  elsewhere.resolve({ httpError: 409, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'Owned elsewhere' } });
  await wait(() => moveAlert(m) && atRest(m));
  await settle();
  assert.equal(m.d.getElementById('twMoveTitle').textContent, 'Move AI11 S1');
  assert.equal(moveAlert(m), `\u2066CS323\u2069 \u2066S1\u2069: ${EXTERNAL_WORDS}`);
  assert.equal(m.d.getElementById('twMoveCommit').disabled, false, 'another meeting: asking is still the user’s');
});

test('M13 data that moves under an open dialog: nothing chosen reads again by itself; a chosen time waits for Check again; an overtaken read is never drawn; a lock since refuses inside', async t => {
  const rows = () => [moveRow(), at(11, { day: 'MON' })];
  const lockOk11 = url => (url.endsWith('/lock/') ? { placement_id: 11, is_locked: true } : undefined);
  // Nothing chosen by the user: read again, the keyboard with the dialog's choice.
  const h = await moveHarness(t, { rows, more: lockOk11 });
  await h.select();
  moveByKey(h);
  await movesSettled(h);
  assert.equal(h.d.activeElement, moveRadio(h, 'TUE|09:00'));
  await h.w.toggleLock(11);
  await wait(() => h.reads.length === 2 && moveRadios(h).length > 0);
  assert.deepEqual(moveNote(h), [false, 'Chosen: No clash found']);
  assert.equal(h.d.activeElement, moveRadio(h, 'TUE|09:00'));
  assert.equal(h.reads[1], '/ops/tw/placements/10/slot-candidates/?verdict=1&offer=manual');
  // A time the user chose waits: stale, Commit held, nothing sent; Check
  // again reads the board, then the times, and the choice starts again.
  const c = await moveHarness(t, { rows, more: lockOk11 });
  await c.select();
  moveByKey(c);
  await movesSettled(c);
  moveRadio(c, 'WED|10:30').click();
  await c.w.toggleLock(11);
  assert.deepEqual([moveStatus(c), moveRadios(c).length, moveNote(c), c.reads.length],
    ['The timetable changed since these times were read.', 0, [true, ''], 1]);
  c.d.getElementById('twMoveForm').requestSubmit();
  await settle();
  assert.deepEqual([c.posts.length, moveAlert(c)], [0, ''], 'the meeting itself did not change: nothing to say, nothing sent');
  const boards = c.requests.filter(r => r.url === '/ops/tw/boards/2/').length;
  c.d.querySelector('#twMoveBody [data-tw-move-recheck]').click();
  await wait(() => c.reads.length === 2 && moveRadios(c).length > 0);
  assert.equal(c.requests.filter(r => r.url === '/ops/tw/boards/2/').length, boards + 1);
  assert.deepEqual([moveNote(c), moveStatus(c)], [[false, 'Chosen: No clash found'], '']);
  // Check again finds the meeting locked since: refused inside; once the
  // board shows it free again, its times are read by themselves.
  let lockedNow = false;
  const r = await moveHarness(t, { rows: () => [moveRow({ is_locked: lockedNow }), at(11, { day: 'MON' })], more: lockOk11 });
  await r.select();
  moveByKey(r);
  await movesSettled(r);
  moveRadio(r, 'WED|10:30').click();
  await r.w.toggleLock(11);
  lockedNow = true;
  r.d.querySelector('#twMoveBody [data-tw-move-recheck]').click();
  await wait(() => moveStatus(r) === LOCKED_WORDS && atRest(r));
  assert.equal(r.reads.length, 1, 'nothing read for a meeting that cannot move');
  lockedNow = false;
  await r.w.selectBoard(2);
  await wait(() => r.reads.length === 2 && moveRadios(r).length > 0);
  assert.deepEqual(moveNote(r), [false, 'Chosen: No clash found']);
  // A write out as the dialog opens: its read is overtaken as the write
  // lands, read again at once, and the first answer is never drawn.
  const lock = deferred();
  const first = deferred();
  const o = await moveHarness(t, {
    rows,
    moves: n => (n === 1 ? first.promise : movesAnswer([MOVE_SET[0]])),
    more: url => (url.endsWith('/lock/') ? lock.promise : undefined),
  });
  await o.select();
  const locking = o.w.toggleLock(11);
  await settle();
  moveByKey(o);
  await settle();
  assert.equal(o.reads.length, 1);
  lock.resolve({ placement_id: 11, is_locked: true });
  await locking;
  await wait(() => moveRadios(o).length > 0);
  assert.equal(o.reads.length, 2, 'read again without waiting for the first answer');
  first.resolve(movesAnswer());
  for (let i = 0; i < 5; i++) await settle();
  assert.deepEqual(moveRadios(o).map(radio => radio.value), ['MON|13:00'], 'the overtaken answer is not drawn');
  // Locked since it opened (the board read again says so): refused inside,
  // nothing to choose, and a commit is stopped by the snapshot.
  let locked = false;
  const k = await moveHarness(t, { rows: () => [moveRow({ is_locked: locked })], more: url => (url === '/ops/tw/x/' ? { ok: true } : undefined) });
  await k.select();
  moveByKey(k);
  await movesSettled(k);
  locked = true;
  await k.w.selectBoard(2);
  assert.deepEqual([moveStatus(k), moveRadios(k).length, moveNote(k)], [LOCKED_WORDS, 0, [true, '']]);
  k.d.getElementById('twMoveForm').requestSubmit();
  await settle();
  assert.deepEqual([k.posts.length, moveAlert(k)], [0, 'This section changed while the list was open. Nothing was moved.']);
  // A write elsewhere then reads nothing for a meeting that cannot move.
  await k.w.twFetch('/ops/tw/x/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  for (let i = 0; i < 5; i++) await settle();
  assert.deepEqual([k.reads.length, moveStatus(k)], [1, LOCKED_WORDS]);
  // The scenario read again (a planner job ended): the meeting leaves the
  // board and comes back, and its times are read again once it is back.
  let back = null;
  const g = await moveHarness(t, { more: url => (back && url === '/ops/tw/boards/2/' ? back.promise : undefined) });
  await g.select();
  moveByKey(g);
  await movesSettled(g);
  back = deferred();
  const reloading = g.w.onScenarioChange();
  await wait(() => moveStatus(g) === 'This meeting is no longer on the board.');
  for (let i = 0; i < 5; i++) await settle();
  assert.equal(g.reads.length, 1, 'nothing is read for a meeting not on the board');
  back.resolve({ placements: [moveRow()], slot_config: MOVE_COLUMNS });
  back = null;
  await reloading;
  await wait(() => g.reads.length === 2 && moveRadios(g).length > 0);
  assert.equal(moveDialogEl(g).hasAttribute('open'), true);
});

test('M14 reads: a failure says so with Try again and is not kept, a meeting the server no longer has says so without, no other time is said, and an answer never takes the keyboard', async t => {
  const failing = await moveHarness(t, { moves: n => (n === 1 ? { httpError: 500, error: { code: 'BOOM', message: 'Broken' } } : movesAnswer()) });
  await failing.select();
  moveByKey(failing);
  await wait(() => failing.d.querySelector('#twMoveBody [data-tw-move-retry]'));
  assert.deepEqual([moveStatus(failing), moveNote(failing), failing.notices.filter(n => n.type === 'error')], ['The times could not be loaded.', [true, ''], []]);
  assert.equal(failing.d.querySelector('#twMoveBody [data-tw-move-retry]').textContent, 'Try again');
  // Not kept: Escape and M again asks again.
  keyOn(failing, 'Escape');
  moveByKey(failing);
  await movesSettled(failing);
  assert.equal(failing.reads.length, 2);
  // Retry reads again (fresh, past a kept answer).
  const retry = await moveHarness(t, { moves: n => (n === 1 ? { httpError: 500, error: { code: 'BOOM', message: 'Broken' } } : movesAnswer()) });
  await retry.select();
  moveByKey(retry);
  await wait(() => retry.d.querySelector('#twMoveBody [data-tw-move-retry]'));
  retry.d.querySelector('#twMoveBody [data-tw-move-retry]').click();
  await movesSettled(retry);
  assert.deepEqual([retry.reads.length, moveStatus(retry)], [2, '']);
  // A server that cannot answer now.
  const busy = await moveHarness(t, { moves: () => ({ httpError: 503, error: { code: 'MAINTENANCE', message: 'Down' } }) });
  await busy.select();
  moveByKey(busy);
  await wait(() => busy.d.querySelector('#twMoveBody [data-tw-move-retry]'));
  assert.equal(moveStatus(busy), 'The server is busy; try again in a moment.');
  // The server no longer has the meeting: no Try again.
  const gone = await moveHarness(t, { moves: () => ({ httpError: 404, error: { code: 'NOT_FOUND', message: 'Gone' } }) });
  await gone.select();
  moveByKey(gone);
  await wait(() => moveStatus(gone) === 'This meeting is no longer on the board.');
  assert.deepEqual([gone.d.querySelector('#twMoveBody [data-tw-move-retry]'), gone.d.getElementById('twMoveCommit').disabled], [null, true]);
  // No other time: said, with the meeting's own time as it stands.
  const empty = await moveHarness(t, { moves: () => movesAnswer([]) });
  await empty.select();
  moveByKey(empty);
  await wait(() => moveStatus(empty) === 'No other time of this kind is on the grid.');
  assert.deepEqual([empty.d.querySelectorAll('#twMoveBody .tw-move-now').length, empty.d.querySelector('#twMoveBody fieldset'), moveNote(empty)], [1, null, [true, '']]);
  // Loading: said, the list busy, the keyboard on the body; moved by the
  // user meanwhile, it stays where they put it.
  const reply = deferred();
  const slow = await moveHarness(t, { moves: () => reply.promise });
  await slow.select();
  moveByKey(slow);
  await settle();
  assert.deepEqual([moveStatus(slow), slow.d.getElementById('twMoveChoices').getAttribute('aria-busy'), moveNote(slow), slow.d.activeElement],
    ['Finding times…', 'true', [true, ''], slow.d.getElementById('twMoveBody')]);
  const close = moveDialogEl(slow).querySelector('.ux-dialog-head [data-dialog-close]');
  close.focus();
  reply.resolve(movesAnswer());
  await movesSettled(slow);
  assert.equal(slow.d.activeElement, close);
  // One read per meeting and data version: a reopen joins it, in flight or answered.
  const first = deferred();
  const j = await moveHarness(t, { moves: n => (n === 1 ? first.promise : movesAnswer()) });
  await j.select();
  moveByKey(j);
  keyOn(j, 'Escape');
  moveByKey(j);
  for (let i = 0; i < 5; i++) await settle();
  assert.equal(j.reads.length, 1, 'a reopen joins the read in flight');
  first.resolve(movesAnswer());
  await movesSettled(j);
  keyOn(j, 'Escape');
  moveByKey(j);
  await movesSettled(j);
  assert.equal(j.reads.length, 1, 'open, Esc, open: the answer is kept');
  // The sub-line says what happens to the room.
  const sub = row => { const div = j.d.createElement('div'); div.innerHTML = j.w.moveSubHtml(at(10, { room: 'LAB25', ...row })); return div.textContent; };
  assert.deepEqual([
    sub({}), sub({ is_online: true, needs_room: false }), sub({ room: 'UNASSIGNED', room_state: 'unassigned' }), sub({ room: '', room_state: 'unset' }), sub({ needs_room: false }),
    sub({ meetings: [{}, {}] }),
  ], [
    'Sunday 09:00–10:15. Its room stays LAB25.', 'Sunday 09:00–10:15. It is online and needs no room.', 'Sunday 09:00–10:15. It has no room yet.',
    'Sunday 09:00–10:15. It has no room yet.', 'Sunday 09:00–10:15.',
    "This meeting only: Sunday 09:00–10:15. The section's other meetings stay where they are. Its room stays LAB25.",
  ]);
});

test('M15 the gate’s first condition wins for Move; the note gives the first hold (published, a planner job, busy) before what the chosen time brings; the keys line names M; the seam waits for an open dialog', async t => {
  const h = await moveHarness(t);
  await h.select();
  // Every condition at once, then each lifted in turn: the next one speaks.
  const p = { ...h.w.TW.placements[0], shared_with_other_scenario: true, is_locked: true };
  Object.assign(h.w.TW, { loadedBoardId: 99, plannerJob: { job_id: 'job-1', status: 'running' } });
  h.w.TW.scenario.status = 'published';
  const ladder = [
    ['published', () => { h.w.TW.scenario.status = 'draft'; }],
    ['loading', () => { h.w.TW.loadedBoardId = h.w.TW.boardId; }],
    ['job', () => { h.w.TW.plannerJob = null; }],
    ['external', () => { p.shared_with_other_scenario = false; }],
    ['locked', () => { p.is_locked = false; }],
  ];
  const words = {
    published: 'This scenario is published and read-only.', loading: 'The board is still loading.',
    job: 'A planner job is running; edits wait until it ends.', external: EXTERNAL_WORDS, locked: LOCKED_WORDS,
  };
  for (const [code, lift] of ladder) {
    assert.deepEqual(plainOf(h.w.cardActionRefusal('move', p)), { code, text: words[code] }, code);
    lift();
  }
  assert.equal(h.w.cardActionRefusal('move', p), '');
  h.w.syncWorkspaceControls();
  // The seam opens nothing while any dialog is open.
  h.w.TW.englishPicker = true;
  assert.equal(h.w.TW.keyActions.move(h.w.TW.placements[0], { origin: 'placement:10' }), false);
  assert.equal(moveByKey(h).defaultPrevented, false, 'a dialog is open: the key is not the board’s');
  assert.deepEqual([moveDialogEl(h).hasAttribute('open'), h.reads.length], [false, 0]);
  h.w.TW.englishPicker = false;
  // The note: the first hold, then the choice.
  let refresh = null;
  const n = await moveHarness(t, { more: url => (refresh && url === '/ops/tw/boards/2/' ? refresh.promise : undefined) });
  await n.select();
  moveByKey(n);
  await movesSettled(n);
  assert.deepEqual(moveNote(n), [false, 'Chosen: No clash found']);
  refresh = deferred();
  const refreshing = n.w.selectBoard(2);
  await settle();
  assert.deepEqual(moveNote(n), [true, BUSY_WORDS]);
  n.w.dispatchEvent(new n.w.CustomEvent('tw:planner-job', { detail: { scenarioId: 1, job: { job_id: 'job-3', status: 'running' }, source: 'adapter' } }));
  assert.deepEqual(moveNote(n), [true, 'A planner job is running; edits wait until it ends.']);
  n.w.TW.scenario.status = 'published';
  n.w.syncWorkspaceControls();
  assert.deepEqual(moveNote(n), [true, 'This scenario is published and read-only.']);
  // A commit asked for all the same is answered inside, and its words go
  // when that hold ends.
  const toasts = n.notices.length;
  n.d.getElementById('twMoveForm').requestSubmit();
  await settle();
  assert.deepEqual([moveAlert(n), n.posts.length, n.notices.slice(toasts)], ['This scenario is published and read-only.', 0, []]);
  n.w.TW.scenario.status = 'draft';
  n.w.TW.plannerJob = null;
  refresh.resolve({ placements: [moveRow()], slot_config: MOVE_COLUMNS });
  refresh = null;
  await refreshing;
  assert.deepEqual([moveAlert(n), moveNote(n)], ['', [false, 'Chosen: No clash found']]);
  // The keys line says what M does, in both languages, before R.
  const keys = /<p id="twGridKeys"[^>]*>{% if LANGUAGE_CODE == 'ar' %}([^{]*){% else %}([^{]*){% endif %}<\/p>/.exec(template);
  assert.ok(keys, 'the keys line, with its two languages');
  assert.match(keys[1], / · Esc: إلغاء التحديد · M: النقل · R: تغيير القاعة$/);
  assert.match(keys[2], / · Esc: clear the selection · M: move · R: change the room$/);
});

test('M16 the pattern chooser gives the keyboard back where it was opened: a list row drawn again meanwhile is the list’s, never the board’s', async t => {
  const h = await harness(t, url => (url.includes('/budget/') ? { budget: budgetRows }
    : url.includes('/planned-slot-candidates/') ? { candidates: [] }
    : url === '/ops/tw/boards/2/' ? { placements: [at(10)], slot_config: scenario.slot_config }
    : url.includes('/conflicts/') ? quiet : url.includes('/quality/') ? noRooms : undefined));
  await h.select();
  await wait(() => h.d.querySelector('#twSectionList [data-focus-key="place:ENG101|S1"]'));
  let asked = 0;
  h.w.TimetableMeetingPolicy.choose = async () => {
    asked++;
    const away = h.d.createElement('button');
    h.d.body.append(away);
    const previous = h.d.activeElement;
    away.focus();
    h.w.renderBudgetSidebar();
    away.remove();
    previous.focus();
    return null;
  };
  // The board had a stop the keyboard was not on.
  card(h, 10).focus();
  h.d.querySelector('#twSectionList [data-focus-key="place:ENG101|S1"]').focus();
  keyOn(h, 'Enter');
  await wait(() => asked === 1 && !h.w.TW.englishPicker);
  await settle();
  assert.equal(h.d.activeElement?.dataset.focusKey, 'place:ENG101|S1', 'back on the list row drawn now');
});

/* ── Place… (design record §7.3, 5f) ──
   A planned section placed with its whole weekly pattern from a dialog: the
   patterns the server offers with their verdict (one read for all of them),
   in the verdict's order, filtered on the page by a day and a time they must
   include; the body names the course and sends every meeting; one Undo
   removes them all. AI222 (Search) meets twice a week: S1 is placed, S2 and
   S3 are not. The server's array order (the legacy one) differs from the
   verdict's on purpose, and one pattern lists its Thursday before its
   Tuesday (a verdict item's `meeting` is an index into that order). 5f
   replaces 5b's refusal of a multi-meeting drop (J2, J3, J6 then). */
const AI222 = { course_code: 'AI222', course_key: 'AI222::SEARCH', course_name: 'Search', department: 'AI', credit_hours: 3, programme_term: 7,
  planned_sections: 3, used_sections: 1, used_section_labels: ['S1'], remaining_sections: 2, max_per_section: 35, total_demand: 70,
  required_meetings_per_section: 2 };
// The budget as the server answers it once these placements are made: a label
// placed anywhere in the scenario is used.
const placeBudget = (made = [], row = AI222) => {
  const labels = [...new Set([...row.used_section_labels, ...made.filter(r => r.course_key === row.course_key).map(r => r.section)])];
  return [{ ...row, used_sections: labels.length, used_section_labels: labels, remaining_sections: Math.max(0, row.planned_sections - labels.length) }];
};
const placeSlot = (day, start, end, kind = 'lect') => ({ kind, day, start, end, slot_idx: 0 });
const pattern = (meetings, rank, verdict = verdictOf(), extra = {}) => ({ candidate_id: `pattern-${rank}`, kind: meetings[0].kind, is_pattern: true,
  requires_full_section_pattern: true, required_meetings: meetings.length, day: meetings[0].day, start: meetings[0].start, end: meetings[0].end,
  meetings, score: rank * 10, tone: 'clean', critical_count: 0, rank, verdict, verdict_rank: rank, ...extra });
const PL_SOFT = vItem('softOverlap', 'warning', { source: 'students', meeting: 1, other: otherAt('AI555', 'S1', 'WED', '10:30', '11:45'), students: 12, blocked_students: 0 });
const PL_TIME = vItem('time', 'critical', { source: 'students', meeting: 0, other: otherAt('AI777', 'S1', 'THU', '13:00', '14:15'), students: 25, blocked_students: 25 });
const PATTERNS = [
  pattern([placeSlot('MON', '10:30', '11:45'), placeSlot('WED', '10:30', '11:45')], 2, verdictOf([PL_SOFT])),
  pattern([placeSlot('SUN', '09:00', '10:15'), placeSlot('TUE', '09:00', '10:15')], 1),
  pattern([placeSlot('THU', '13:00', '14:15'), placeSlot('TUE', '13:00', '14:15')], 3, verdictOf([PL_TIME])),
  pattern([placeSlot('SUN', '13:00', '14:15'), placeSlot('TUE', '13:00', '14:15')], 4),
];
const BEST = 'SUN@09:00-10:15|TUE@09:00-10:15';
const SOFT = 'MON@10:30-11:45|WED@10:30-11:45';
const CLASH = 'TUE@13:00-14:15|THU@13:00-14:15';
const LAST = 'SUN@13:00-14:15|TUE@13:00-14:15';
const patternsAnswer = (candidates = PATTERNS, request = {}) => ({ ok: true, board: { id: 2, label: 'Term 7', nominal_term: 7 },
  request: { course_code: 'AI222', course_key: 'AI222::SEARCH', section_label: 'S2', required_meetings_per_section: 2, requires_full_section_pattern: true, ...request },
  status: 'ready', summary: { total: candidates.length }, candidates });
const ROOMS_NOTE = 'Rooms are chosen afterwards (R on a card). To change one meeting later, use Move….';
const READY = { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] };
// Unless a test says otherwise, a create makes one placement per meeting (ids
// from 91), on the board from then on, and the budget counts its label.
function placeHarness(t, { budget = made => placeBudget(made), rows = () => [at(10)], patterns = null, create = null, remove = null,
  more = () => undefined, arabic = false, extra = {}, boards = [board] } = {}) {
  const posts = [], reads = [], removes = [], seen = { budget: 0 };
  let made = [];
  let nextId = 90;
  const making = (body, boardId = body.board_id) => body.meetings.map(m => at(++nextId, { course_code: body.course_code, course_key: body.course_key,
    section: body.section_label, term_section_id: 500, board_id: boardId, day: m.day, start_time: m.start_time, end_time: m.end_time,
    room: '', room_state: 'unset', needs_room: true }));
  return harness(t, async (url, opts) => {
    const own = await more(url, opts);
    if (own !== undefined) return own;
    if (url.includes('/planned-slot-candidates/')) { reads.push(url); return patterns ? patterns(reads.length, url) : patternsAnswer(); }
    if (url === '/ops/tw/placements/create-planned/' && opts.method === 'POST') {
      const body = JSON.parse(opts.body);
      posts.push(body);
      const tools = { make: (b, boardId) => { const rowsMade = making(b, boardId); made.push(...rowsMade); return rowsMade; } };
      if (create) { const answer = await create(body, posts.length, tools); if (answer !== undefined) return answer; }
      const rowsMade = tools.make(body);
      return { ok: true, placement: rowsMade[0], placements: rowsMade, validation: { critical_count: 0 }, required_meetings: rowsMade.length };
    }
    const removing = /^\/ops\/tw\/placements\/(\d+)\/remove\/$/.exec(url);
    if (removing && opts.method === 'POST') {
      const id = Number(removing[1]);
      removes.push(id);
      if (remove) { const answer = await remove(id, removes.length); if (answer !== undefined) return answer; }
      made = made.filter(row => row.id !== id);
      return { ok: true, removed: id };
    }
    if (url.includes('/budget/')) { seen.budget++; return { budget: budget(made) }; }
    if (url === '/ops/tw/boards/2/') return { placements: [...rows(), ...made.filter(row => row.board_id === 2)], slot_config: MOVE_COLUMNS };
    if (url.startsWith('/ops/tw/boards/?')) return { boards };
    if (url.includes('/conflicts/')) return quiet;
    if (url.includes('/quality/')) return noRooms;
    if (url.includes('/readiness/')) return { readiness: READY };
    return undefined;
  }, extra, arabic).then(h => {
    h.w.Element.prototype.scrollIntoView = () => {};  // jsdom has none (a located card is scrolled to)
    return Object.assign(h, { posts, reads, removes, seen, made: () => made });
  });
}
const placeDialogEl = h => h.d.getElementById('twPlaceDialog');
const placeRow = (h, label = 'S2', key = 'AI222::SEARCH') => h.d.querySelector(`#twSectionList [data-focus-key="place:${key}|${label}"]`);
const placeRadios = h => [...h.d.querySelectorAll('#twPlaceBody input[name="twPlaceChoice"]')];
const placeRadio = (h, sig) => placeRadios(h).find(radio => radio.value === sig);
const placeShown = h => placeRadios(h).filter(radio => !radio.closest('[hidden]')).map(radio => radio.value);
const placeChecked = h => placeRadios(h).find(radio => radio.checked)?.value || '';
const placeNote = h => [h.d.getElementById('twPlaceCommit').disabled, h.d.getElementById('twPlaceNote').textContent];
const placeStatus = h => h.d.getElementById('twPlaceStatus').textContent;
const placeAlert = h => (h.d.getElementById('twPlaceAlert').hidden ? '' : h.d.getElementById('twPlaceAlert').textContent);
const placeSaid = (h, radio, index) => h.d.getElementById(radio.getAttribute('aria-describedby').split(' ')[index]).textContent;
const placeName = (h, radio) => h.d.getElementById(radio.getAttribute('aria-labelledby')).textContent;
const placesSettled = h => wait(() => placeRadios(h).length > 0);
const boardReads = h => h.requests.filter(r => r.url === '/ops/tw/boards/2/').length;
// Enter on a list row, as the keyboard gives it.
const placeByKey = async (h, label = 'S2', key = 'AI222::SEARCH') => {
  await wait(() => placeRow(h, label, key));
  placeRow(h, label, key).focus();
  return keyOn(h, 'Enter');
};
// What a row hands a drag (its dragstart's payload).
const dragPayloadOf = (h, label = 'S2', key = 'AI222::SEARCH') => {
  const data = {};
  const dragstart = new h.w.Event('dragstart', { bubbles: true });
  dragstart.dataTransfer = { setData: (type, value) => { data[type] = value; }, effectAllowed: '' };
  placeRow(h, label, key).dispatchEvent(dragstart);
  return JSON.parse(data['text/plain']);
};
const dropAt = (h, payload, day, start) => h.w.onDrop({ preventDefault() {},
  currentTarget: h.d.querySelector(`#twGrid [role="gridcell"][data-day="${day}"][data-start="${start}"]`),
  dataTransfer: { getData: () => JSON.stringify(payload) } });
const PLACE_REQUEST = { course_code: 'AI222', course_key: 'AI222::SEARCH', course_name: 'Search', section_label: 'S2', capacity: 35, required: 2, programme_term: 7 };
const hhmm = minutes => `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
// n distinct two-meeting patterns, the first `clean` with no clash.
const manyPatterns = (n, clean = 0) => Array.from({ length: n }, (_, i) => pattern(
  [placeSlot('SUN', hhmm(420 + i), hhmm(495 + i)), placeSlot('TUE', hhmm(420 + i), hhmm(495 + i))], i + 1,
  i < clean ? verdictOf() : verdictOf([PL_TIME])));

test('PL1 Enter on a row: one read of every pattern with its verdict, best first; the chosen pattern is sent whole, the course named, no credit hours', async t => {
  const h = await placeHarness(t);
  await h.select();
  assert.equal((await placeByKey(h)).defaultPrevented, true);
  await placesSettled(h);
  assert.equal(h.reads.length, 1);
  const read = new URL(h.reads[0], 'http://localhost');
  assert.equal(read.pathname, '/ops/tw/boards/2/planned-slot-candidates/');
  assert.deepEqual(Object.fromEntries(read.searchParams), { course_code: 'AI222', course_key: 'AI222::SEARCH', section_label: 'S2', limit: '1000', verdict: '1' });
  assert.equal(h.d.getElementById('twPlaceTitle').textContent, 'Place AI222 S2');
  assert.equal(h.d.getElementById('twPlaceSub').textContent, 'Term 7 · 2 meetings a week · the whole weekly pattern');
  // In the verdict's order, each a whole pattern by day, worded and toned by the verdict.
  assert.deepEqual(placeRadios(h).map(radio => [radio.value, radio.closest('.ux-choice').dataset.tone, radio.dataset.focusKey]), [
    [BEST, 'ok', `place-choice:${BEST}`], [SOFT, 'warning', `place-choice:${SOFT}`], [CLASH, 'critical', `place-choice:${CLASH}`], [LAST, 'ok', `place-choice:${LAST}`],
  ]);
  assert.deepEqual(placeRadios(h).map(radio => placeName(h, radio)), [
    'Sunday 09:00–10:15, Tuesday 09:00–10:15', 'Monday 10:30–11:45, Wednesday 10:30–11:45',
    'Tuesday 13:00–14:15, Thursday 13:00–14:15', 'Sunday 13:00–14:15, Tuesday 13:00–14:15',
  ]);
  assert.deepEqual(placeRadios(h).map(radio => placeSaid(h, radio, 0)),
    ['No clash found', 'Lowers quality: Overlap students can route round', 'Blocks publishing: Time clash', 'No clash found']);
  // An item is led by the meeting it belongs to: its index in the server's list.
  assert.equal(placeSaid(h, placeRadio(h, SOFT), 1), 'Wed 10:30–11:45: Overlap students can route round: AI555 S1 · Wed 10:30–11:45 · 12 students need both');
  assert.equal(placeSaid(h, placeRadio(h, CLASH), 1), 'Thu 13:00–14:15: Time clash: AI777 S1 · Thu 13:00–14:15 · 25 students need both');
  assert.equal(h.d.querySelector('#twPlaceBody fieldset:last-of-type > legend').textContent, 'Complete weekly patterns, best first');
  assert.ok(h.d.getElementById('twPlaceChoices').matches('.ux-choice-list[data-ux-submit]'));
  assert.equal(placeStatus(h), '4 patterns, best first · 2 with no clash');
  // The best pattern is chosen, the keyboard goes to it, and the note says
  // what it brings and what comes afterwards.
  assert.equal(placeChecked(h), BEST);
  assert.equal(h.d.activeElement, placeRadio(h, BEST));
  assert.deepEqual(placeNote(h), [false, `Chosen: No clash found · ${ROOMS_NOTE}`]);
  assert.match(template, /id="twPlaceCommit"[^>]*>{% if LANGUAGE_CODE == 'ar' %}وضع الشعبة{% else %}Place section{% endif %}<\/button>/);
  // The second best, chosen, is sent whole: every meeting, the course's key and name.
  placeRadio(h, SOFT).click();
  assert.deepEqual(placeNote(h), [false, `Chosen: Lowers quality: Overlap students can route round · ${ROOMS_NOTE}`]);
  h.d.getElementById('twPlaceCommit').click();
  await wait(() => h.posts.length === 1 && atRest(h) && !placeDialogEl(h).hasAttribute('open'));
  assert.deepEqual(h.posts, [{ board_id: 2, course_code: 'AI222', course_key: 'AI222::SEARCH', course_name: 'Search', section_label: 'S2', capacity: 35,
    meetings: [{ day: 'MON', start_time: '10:30', end_time: '11:45' }, { day: 'WED', start_time: '10:30', end_time: '11:45' }] }]);
  assert.equal(h.reads.length, 1, 'nothing read again while placing');
});

test('PL2 a drop of a course that meets twice a week sends nothing and opens Place on the dropped time; a time no pattern includes clears the filter and says so', async t => {
  const h = await placeHarness(t);
  await h.select();
  await wait(() => placeRow(h));
  const payload = dragPayloadOf(h);
  assert.deepEqual([payload.required_meetings_per_section, payload.course_key, payload.programme_term], [2, 'AI222::SEARCH', 7]);
  await dropAt(h, payload, 'THU', '13:00');
  await placesSettled(h);
  assert.deepEqual([h.posts.length, placeDialogEl(h).hasAttribute('open'), h.notices.filter(n => n.type !== 'success')], [0, true, []]);
  // The filter is the dropped time; only the patterns that include it show.
  const day = h.d.getElementById('twPlaceDay'), time = h.d.getElementById('twPlaceTime');
  assert.deepEqual([day.value, time.value], ['THU', '13:00-14:15']);
  assert.deepEqual(placeShown(h), [CLASH]);
  assert.equal(placeChecked(h), CLASH);
  assert.equal(placeStatus(h), '1 of 4 include Thursday \u206613:00–14:15\u2069');
  // Its words: the legend, the labels, "any" first, the days in week order, the times by start.
  assert.equal(h.d.querySelector('#twPlaceBody fieldset > legend').textContent, 'Must include');
  assert.deepEqual([...h.d.querySelectorAll('#twPlaceBody .fb-lbl')].map(label => [label.textContent, label.htmlFor]), [['Day', 'twPlaceDay'], ['Time', 'twPlaceTime']]);
  assert.deepEqual([...day.options].map(o => [o.value, o.textContent]), [['', 'Any day'], ['SUN', 'Sunday'], ['MON', 'Monday'], ['TUE', 'Tuesday'], ['WED', 'Wednesday'], ['THU', 'Thursday']]);
  assert.deepEqual([...time.options].map(o => [o.value, o.textContent]), [['', 'Any time'], ['09:00-10:15', '\u206609:00–10:15\u2069'], ['10:30-11:45', '\u206610:30–11:45\u2069'], ['13:00-14:15', '\u206613:00–14:15\u2069']]);
  // Another day at that time: the chosen pattern hidden, the first shown is
  // chosen (by the dialog); a day and time no pattern has together: none.
  day.value = 'SUN';
  day.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  assert.deepEqual([placeShown(h), placeChecked(h), placeStatus(h)], [[LAST], LAST, '1 of 4 include Sunday \u206613:00–14:15\u2069']);
  day.value = 'WED';
  day.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  assert.deepEqual([placeShown(h), placeChecked(h), placeStatus(h)], [[], '', '0 of 4 include Wednesday \u206613:00–14:15\u2069']);
  assert.deepEqual(placeNote(h), [true, '']);
  // A day alone: the patterns with a meeting that day.
  time.value = '';
  time.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  assert.deepEqual([placeShown(h), placeChecked(h), placeStatus(h)], [[SOFT], SOFT, '1 of 4 include Wednesday']);
  day.value = 'SUN';
  day.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  assert.deepEqual([placeShown(h), placeChecked(h), placeStatus(h)], [[BEST, LAST], BEST, '2 of 4 include Sunday']);
  assert.deepEqual(placeNote(h), [false, `Chosen: No clash found · ${ROOMS_NOTE}`]);
  // Escape: nothing sent; the keyboard back on the row.
  keyOn(h, 'Escape');
  assert.deepEqual([placeDialogEl(h).hasAttribute('open'), h.posts.length, h.d.activeElement], [false, 0, placeRow(h)]);
  // Dropped where no pattern meets: every pattern, the filter cleared, and the fallback said.
  await dropAt(h, payload, 'MON', '09:00');
  await placesSettled(h);
  assert.deepEqual([h.d.getElementById('twPlaceDay').value, h.d.getElementById('twPlaceTime').value], ['', '']);
  assert.deepEqual(placeShown(h), [BEST, SOFT, CLASH, LAST]);
  assert.equal(placeChecked(h), BEST);
  assert.equal(placeStatus(h), 'No pattern includes Monday \u206609:00–10:15\u2069; showing every pattern.');
  assert.equal(h.reads.length, 1, 'the second open takes the answer kept for this data');
  // A filter chosen afterwards ends the fallback.
  h.d.getElementById('twPlaceDay').value = 'MON';
  h.d.getElementById('twPlaceDay').dispatchEvent(new h.w.Event('change', { bubbles: true }));
  assert.deepEqual([placeShown(h), placeStatus(h)], [[SOFT], '1 of 4 include Monday']);
  // A one-meeting drop the server answers "complete pattern" (an older budget
  // row): the same, the budget read again, no error toast.
  const o = await placeHarness(t, { create: () => ({ httpError: 400, error: { code: 'FULL_SECTION_PATTERN_REQUIRED', message: 'AI222 needs a complete 2-meeting section pattern.' } }) });
  await o.select();
  await wait(() => placeRow(o));
  const budgets = o.seen.budget;
  await dropAt(o, { ...dragPayloadOf(o), required_meetings_per_section: 1 }, 'THU', '13:00');
  await placesSettled(o);
  assert.deepEqual(o.posts.map(body => body.meetings), [[{ day: 'THU', start_time: '13:00', end_time: '14:15' }]], 'the drop’s one meeting, once');
  assert.deepEqual([placeDialogEl(o).hasAttribute('open'), o.d.getElementById('twPlaceDay').value, o.d.getElementById('twPlaceTime').value], [true, 'THU', '13:00-14:15']);
  assert.equal(o.seen.budget, budgets + 1, 'the budget is read again');
  assert.deepEqual(o.notices.filter(n => n.type === 'error'), []);
  assert.equal(o.d.getElementById('twPlaceSub').textContent, 'Term 7 · 2 meetings a week · the whole weekly pattern', 'the count the patterns were read for');
  // The day and the time are one meeting's: Monday 09:00 and Wednesday 13:00
  // do not include Monday 13:00.
  const mixed = pattern([placeSlot('MON', '09:00', '10:15'), placeSlot('WED', '13:00', '14:15')], 1);
  const m = await placeHarness(t, { patterns: () => patternsAnswer([mixed]) });
  await m.select();
  await wait(() => placeRow(m));
  await dropAt(m, dragPayloadOf(m), 'MON', '13:00');
  await placesSettled(m);
  assert.deepEqual([placeShown(m), placeStatus(m)], [['MON@09:00-10:15|WED@13:00-14:15'], 'No pattern includes Monday \u206613:00–14:15\u2069; showing every pattern.']);
});

test('PL3 a pattern that blocks publishing is chosen like any other: no row is held, nothing asks to confirm, and the note states its verdict', async t => {
  let asked = 0;
  const h = await placeHarness(t);
  h.w.dlg.confirm = async () => { asked++; return true; };
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  assert.deepEqual([placeRadios(h).filter(radio => radio.disabled).length, h.d.querySelectorAll('#twPlaceChoices .ux-choice-static').length], [0, 0]);
  placeRadio(h, CLASH).click();
  assert.deepEqual(placeNote(h), [false, `Chosen: Blocks publishing: Time clash · ${ROOMS_NOTE}`]);
  h.d.getElementById('twPlaceForm').requestSubmit();
  await wait(() => h.posts.length === 1 && atRest(h) && !placeDialogEl(h).hasAttribute('open'));
  assert.deepEqual(h.posts[0].meetings, [{ day: 'TUE', start_time: '13:00', end_time: '14:15' }, { day: 'THU', start_time: '13:00', end_time: '14:15' }]);
  assert.equal(asked, 0, 'never confirmed');
});

test('PL4 in Arabic: every time isolated in rows and options, counts in their forms; server words are text in text and in attributes', async t => {
  const hostile = 'MON@08:00"><b>x</b>-09:15|WED@08:00-09:15';
  const odd = pattern([placeSlot('MON', '08:00"><b>x</b>', '09:15'), placeSlot('WED', '08:00', '09:15')], 5,
    verdictOf([vItem('instructorElsewhere', 'warning', { meeting: 0, other: otherAt('<img src=x>', 'S"1', 'MON', '08:00', '09:15', { board_id: 4, board_label: '"<i>T4</i>' }), teacher: 'Dr "Q"' })]));
  const h = await placeHarness(t, { arabic: true, patterns: () => patternsAnswer([...PATTERNS, odd]), boards: [{ ...board, label: '"<u>المستوى 7</u>' }] });
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  const dialog = placeDialogEl(h);
  assert.equal(h.d.getElementById('twPlaceTitle').textContent, 'وضع AI222 S2');
  assert.equal(h.d.getElementById('twPlaceSub').textContent, '"<u>المستوى 7</u> · لقاءان أسبوعياً · النمط الأسبوعي كاملاً');
  assert.deepEqual([dialog.querySelector('img, b, i, u'), placeRadio(h, hostile)?.value], [null, hostile], 'server words never become markup');
  assert.deepEqual(placeRadios(h).slice(0, 2).map(radio => h.d.getElementById(radio.getAttribute('aria-labelledby')).innerHTML), [
    'الأحد <bdi dir="ltr">09:00–10:15</bdi>، الثلاثاء <bdi dir="ltr">09:00–10:15</bdi>',
    'الاثنين <bdi dir="ltr">10:30–11:45</bdi>، الأربعاء <bdi dir="ltr">10:30–11:45</bdi>',
  ]);
  assert.equal(placeSaid(h, placeRadio(h, SOFT), 1), 'الأربعاء 10:30–11:45: تداخل يمكن تجاوزه: AI555 S1 · الأربعاء 10:30–11:45 · 12 طالباً يحتاجون كليهما');
  assert.equal(placeSaid(h, placeRadio(h, hostile), 1), 'الاثنين 08:00"><b>x</b>–09:15: المدرّس يدرّس في لوحة أخرى في الوقت نفسه: Dr "Q" — <img src=x> S"1 ("<i>T4</i>) · الاثنين 08:00–09:15');
  const leads = [...dialog.querySelectorAll('#twPlaceChoices .ux-choice-desc bdi[dir="ltr"]')].map(bdi => bdi.textContent);
  assert.ok(leads.includes('10:30–11:45'), 'a line’s lead isolates its time');
  assert.equal(h.d.querySelector('#twPlaceBody fieldset > legend').textContent, 'يشمل');
  assert.deepEqual([...dialog.querySelectorAll('.fb-lbl')].map(label => label.textContent), ['اليوم', 'الوقت']);
  assert.deepEqual([...h.d.getElementById('twPlaceDay').options].map(o => o.textContent), ['أي يوم', 'الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس']);
  assert.deepEqual([...h.d.getElementById('twPlaceTime').options].slice(0, 4).map(o => [o.value, o.textContent]),
    [['', 'أي وقت'], ['08:00"><b>x</b>-09:15', '\u206608:00"><b>x</b>–09:15\u2069'], ['08:00-09:15', '\u206608:00–09:15\u2069'], ['09:00-10:15', '\u206609:00–10:15\u2069']]);
  assert.equal(h.d.querySelector('#twPlaceBody fieldset:last-of-type > legend').textContent, 'الأنماط الأسبوعية الكاملة، الأفضل أولاً');
  assert.equal(placeStatus(h), '5 أنماط، الأفضل أولاً · نمطان بلا تعارض');
  assert.equal(h.d.getElementById('twPlaceNote').textContent,
    'المختار: لا تعارض · تُختار القاعات بعد الوضع (R على البطاقة). ولتغيير لقاء واحد لاحقاً استخدم «نقل…».');
  h.d.getElementById('twPlaceDay').value = 'THU';
  h.d.getElementById('twPlaceDay').dispatchEvent(new h.w.Event('change', { bubbles: true }));
  assert.equal(placeStatus(h), '1 من 5 تشمل الخميس');
  // The list row: its meetings a week, in its form.
  assert.match(placeRow(h).textContent, /لقاءان أسبوعياً/);
  // Every count form, as the status line says it (CLDR: 190 is "many", as 11; 100 is "other").
  const counts = async (n, clean) => {
    const c = await placeHarness(t, { arabic: true, patterns: () => patternsAnswer(manyPatterns(n, clean)) });
    await c.select();
    await placeByKey(c);
    await placesSettled(c);
    return placeStatus(c);
  };
  assert.deepEqual([await counts(3, 0), await counts(11, 1), await counts(100, 11), await counts(190, 100), await counts(2, 3)], [
    '3 أنماط، الأفضل أولاً · لا نمط بلا تعارض', '11 نمطاً، الأفضل أولاً · نمط واحد بلا تعارض',
    '100 نمط، الأفضل أولاً · 11 نمطاً بلا تعارض', '190 نمطاً، الأفضل أولاً · 100 نمط بلا تعارض', 'نمطان، الأفضل أولاً · نمطان بلا تعارض',
  ]);
  const three = await placeHarness(t, { arabic: true, budget: () => [{ ...AI222, required_meetings_per_section: 3 }], patterns: () => patternsAnswer(PATTERNS, { required_meetings_per_section: 3 }) });
  await three.select();
  await wait(() => placeRow(three));
  assert.match(placeRow(three).textContent, /3 لقاءات أسبوعياً/);
  await placeByKey(three);
  await placesSettled(three);
  assert.equal(three.d.getElementById('twPlaceSub').textContent, 'Term 7 · 3 لقاءات أسبوعياً · النمط الأسبوعي كاملاً');
  // In English, the row says it too.
  const en = await placeHarness(t);
  await en.select();
  await wait(() => placeRow(en));
  assert.deepEqual([...placeRow(en).querySelectorAll('.ux-meta')[0].children].map(part => part.textContent), ['AI', '3 cr', 'cap 35', '2 meetings a week']);
});

test('PL5 placed: the dialog closes, the keyboard is on the row now where the section was, the board’s stop on its first card, the new cards pulse, the report reads Issues', async t => {
  const h = await placeHarness(t);
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  h.w.TW.redoStack.push({ type: 'room', placement_id: 10, old_room: 'X', new_room: 'Y' });
  const toasts = h.notices.length;
  keyOn(h, 'Enter');  // on the chosen pattern
  await wait(() => h.posts.length === 1 && atRest(h) && h.notices.length > toasts);
  assert.equal(placeDialogEl(h).hasAttribute('open'), false);
  assert.equal(placeRow(h, 'S2'), null, 'its row is gone');
  assert.equal(h.d.activeElement?.dataset.focusKey, 'place:AI222::SEARCH|S3', 'the row now at that place has the keyboard');
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')].map(element => element.dataset.focusKey), ['placement:91']);
  assert.deepEqual([card(h, 91), card(h, 92)].map(c => c?.classList.contains('highlight')), [true, true], 'the new cards pulse');
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'warning',
    text: 'Placed \u2066AI222\u2069 \u2066S2\u2069. · Lowers quality: Room not assigned yet · Press R on the card to change its room.' }]);
  // One entry undoes the section: the ids, the body sent and the meetings as the server stored them.
  assert.deepEqual(plainOf(h.w.TW.undoStack), [{ type: 'section_create', placement_ids: [91, 92], board_id: 2, payload: h.posts[0], meetings: [
    { id: 91, board_id: 2, term_section_id: 500, day: 'SUN', start_time: '09:00', end_time: '10:15', room: '', is_locked: false },
    { id: 92, board_id: 2, term_section_id: 500, day: 'TUE', start_time: '09:00', end_time: '10:15', room: '', is_locked: false },
  ] }]);
  assert.equal(h.w.TW.redoStack.length, 0);
  assert.equal(h.d.getElementById('twUndo').disabled, false);
  // The course's last section: the row now at that place is the course's own
  // placed line (5a keeps a complete course reachable).
  const last = await placeHarness(t, { budget: made => [...placeBudget(made, { ...AI222, planned_sections: 2, remaining_sections: 1 }),
    { course_code: 'CS323', course_key: 'CS323', programme_term: 7, planned_sections: 1, used_sections: 0, remaining_sections: 1, total_demand: 30 }] });
  await last.select();
  await placeByKey(last);
  await placesSettled(last);
  keyOn(last, 'Enter');
  await wait(() => last.posts.length === 1 && atRest(last) && !placeDialogEl(last).hasAttribute('open'));
  assert.equal(last.d.activeElement?.dataset.focusKey, 'place:AI222::SEARCH|complete');
  // A one-meeting drop places through the same commit, undo entry and report (no hint: it read no verdict).
  const one = await placeHarness(t, { budget: made => placeBudget(made, { ...AI222, required_meetings_per_section: 1 }) });
  await one.select();
  await wait(() => placeRow(one));
  const calls = [];
  const shared = one.w.commitPlannedCreate;
  one.w.commitPlannedCreate = (...args) => { calls.push(plainOf(args)); return shared(...args); };
  const before = one.notices.length;
  await dropAt(one, dragPayloadOf(one), 'MON', '13:00');
  assert.deepEqual(calls, [[{ board_id: 2, course_code: 'AI222', course_key: 'AI222::SEARCH', course_name: 'Search', section_label: 'S2', capacity: 35,
    meetings: [{ day: 'MON', start_time: '13:00', end_time: '14:15' }] }]]);
  assert.deepEqual(plainOf(one.w.TW.undoStack.at(-1)).placement_ids, [91]);
  assert.deepEqual(one.notices.slice(before), [{ type: 'warning', text: 'Placed \u2066AI222\u2069 \u2066S2\u2069. · Lowers quality: Room not assigned yet' }]);
});

test('PL6 Undo removes every meeting, last first; a refused removal keeps what is left on the stack and reads the board; Redo creates them all again and remaps the ids', async t => {
  let refuse = null;
  const h = await placeHarness(t, { remove: (id, n) => (refuse ? refuse(id, n) : undefined) });
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  keyOn(h, 'Enter');
  await wait(() => h.posts.length === 1 && atRest(h) && !placeDialogEl(h).hasAttribute('open'));
  await h.w.runHistoryAction(true);
  assert.deepEqual(h.removes, [92, 91], 'every meeting, the last made first');
  assert.deepEqual([h.w.TW.undoStack.length, h.w.TW.redoStack.length], [0, 1]);
  assert.deepEqual([card(h, 91), card(h, 92)], [null, null]);
  await h.w.runHistoryAction(false);
  assert.equal(h.posts.length, 2);
  assert.deepEqual(h.posts[1], h.posts[0], 'Redo creates it again, every meeting, by the same endpoint');
  const redone = plainOf(h.w.TW.undoStack.at(-1));
  assert.deepEqual([redone.type, redone.placement_ids, redone.meetings.map(m => m.id), h.w.TW.redoStack.length], ['section_create', [93, 94], [93, 94], 0]);
  await h.w.runHistoryAction(true);
  assert.deepEqual(h.removes.slice(2), [94, 93], 'the new ids');
  // A refused removal in the middle: the entry stays with what is left, and
  // the board shows what went.
  h.w.TW.undoStack = [{ type: 'section_create', placement_ids: [95, 96, 97], board_id: 2, payload: h.posts[0], meetings: [] }];
  h.w.TW.redoStack = [];
  refuse = id => (id === 96 ? { httpError: 409, error: { code: 'PLACEMENT_LOCKED', message: 'Locked' } } : undefined);
  const reads = boardReads(h);
  await h.w.runHistoryAction(true);
  assert.deepEqual(h.removes.slice(4), [97, 96]);
  assert.deepEqual(plainOf(h.w.TW.undoStack), [{ type: 'section_create', placement_ids: [95, 96], board_id: 2, payload: h.posts[0], meetings: [] }]);
  assert.equal(h.w.TW.redoStack.length, 0);
  assert.equal(boardReads(h), reads + 1, 'the board is read again');
  assert.deepEqual(h.notices.at(-1), { type: 'error', text: 'Locked' });
  // Nothing removed: nothing to read.
  const none = boardReads(h);
  refuse = id => (id === 96 ? { httpError: 409, error: { code: 'PLACEMENT_LOCKED', message: 'Locked' } } : undefined);
  await h.w.runHistoryAction(true);
  assert.deepEqual([h.removes.slice(6), boardReads(h)], [[96], none]);
  // Then it goes, and a meeting a removal took with it (a bundle) is not asked for again.
  refuse = id => (id === 96 ? { ok: true, removed: 96, removed_placements: [{ id: 96 }, { id: 95 }] } : undefined);
  await h.w.runHistoryAction(true);
  assert.deepEqual(h.removes.slice(7), [96]);
  assert.deepEqual([h.w.TW.undoStack.length, h.w.TW.redoStack.length], [0, 1]);
});

test('PL7 a refused create is said in the dialog: a duplicate takes the next free label and keeps the choice, "complete pattern" reads again, what asking again cannot change holds Commit', async t => {
  const cases = [
    ['SCENARIO_PUBLISHED', 400, 'This scenario is published and read-only.', true, false],
    ['SCENARIO_BUSY', 409, BUSY_WORDS, false, false],
    ['NOT_FOUND', 404, 'The board changed; nothing was placed.', true, false],
    ['VALIDATION_PLANNED_PATTERN', 400, 'Nothing was placed.', false, false],
    ['SERVER_ERROR', 500, 'Nothing was placed.', false, true],
  ];
  for (const [code, status, words, held, reads] of cases) {
    const h = await placeHarness(t, { create: () => ({ httpError: status, error: { code, message: 'Refused' } }) });
    await h.select();
    await placeByKey(h);
    await placesSettled(h);
    const toasts = h.notices.length;
    const boards = boardReads(h);
    h.d.getElementById('twPlaceCommit').click();
    await wait(() => placeAlert(h) && atRest(h));
    await settle();
    assert.deepEqual([placeAlert(h), h.d.getElementById('twPlaceAlert').dataset.tone], [words, 'warning'], code);
    assert.equal(h.d.getElementById('twPlaceCommit').disabled, held, code);
    assert.deepEqual([h.notices.slice(toasts), h.w.TW.undoStack.length, placeDialogEl(h).hasAttribute('open')], [[], 0, true], code);
    assert.equal(boardReads(h), boards + (reads ? 1 : 0), `${code}: ${reads ? 'an unknown outcome reads the board' : 'a known refusal reads nothing'}`);
  }
  // A duplicate: the next free label, said; the chosen pattern kept and the
  // keyboard on it; its patterns read for that label; placed under it.
  let duplicate = true;
  const d = await placeHarness(t, { create: (body, n, tools) => {
    if (!duplicate) return undefined;
    duplicate = false;
    tools.make(body, 3);  // S2 placed meanwhile, on another board
    return { httpError: 409, error: { code: 'PLACEMENT_DUPLICATE', message: 'Duplicate placement for this section/day/time' } };
  } });
  await d.select();
  await placeByKey(d);
  await placesSettled(d);
  placeRadio(d, SOFT).click();
  d.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(d) && atRest(d) && d.reads.length === 2 && placeRadios(d).length > 0);
  assert.deepEqual([placeAlert(d), d.d.getElementById('twPlaceTitle').textContent], ['Section \u2066S2\u2069 was placed meanwhile; this is now \u2066S3\u2069.', 'Place AI222 S3']);
  assert.equal(new URL(d.reads[1], 'http://localhost').searchParams.get('section_label'), 'S3');
  assert.deepEqual([placeChecked(d), d.d.activeElement, placeDialogEl(d).hasAttribute('open')], [SOFT, placeRadio(d, SOFT), true]);
  assert.deepEqual(placeNote(d), [false, `Chosen: Lowers quality: Overlap students can route round · ${ROOMS_NOTE}`]);
  d.d.getElementById('twPlaceCommit').click();
  await wait(() => d.posts.length === 2 && atRest(d) && !placeDialogEl(d).hasAttribute('open'));
  assert.deepEqual([d.posts[1].section_label, d.posts[1].meetings[0]], ['S3', { day: 'MON', start_time: '10:30', end_time: '11:45' }]);
  // A duplicate the budget does not show yet: the next label all the same.
  const lag = await placeHarness(t, { create: (body, n) => (n === 1 ? { httpError: 409, error: { code: 'PLACEMENT_DUPLICATE', message: 'Duplicate' } } : undefined) });
  await lag.select();
  await placeByKey(lag);
  await placesSettled(lag);
  lag.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(lag) && atRest(lag) && placeRadios(lag).length > 0);
  assert.deepEqual([placeAlert(lag), lag.d.getElementById('twPlaceTitle').textContent], ['Section \u2066S2\u2069 was placed meanwhile; this is now \u2066S3\u2069.', 'Place AI222 S3']);
  // The board read again meanwhile (its budget names S2) moves the dialog on
  // by itself; the duplicate's own budget read, landing later, does not move
  // it a second time.
  const four = { ...AI222, planned_sections: 4, remaining_sections: 3 };
  let held = null;
  let holdNext = false;
  const twice = await placeHarness(t, {
    budget: made => placeBudget(made, four),
    create: (body, n, tools) => {
      if (n > 1) return undefined;
      tools.make(body, 3);
      holdNext = true;
      held = deferred();
      return { httpError: 409, error: { code: 'PLACEMENT_DUPLICATE', message: 'Duplicate' } };
    },
    more: url => {
      if (!holdNext || !url.includes('/budget/')) return undefined;
      holdNext = false;
      return held.promise;
    },
  });
  await twice.select();
  await placeByKey(twice);
  await placesSettled(twice);
  twice.d.getElementById('twPlaceCommit').click();
  await wait(() => held && !holdNext);
  await twice.w.selectBoard(2);
  await wait(() => twice.reads.length === 2 && placeRadios(twice).length > 0);
  assert.equal(twice.d.getElementById('twPlaceTitle').textContent, 'Place AI222 S3');
  held.resolve({ budget: placeBudget(twice.made(), four) });
  await wait(() => atRest(twice));
  for (let i = 0; i < 5; i++) await settle();
  assert.deepEqual([twice.d.getElementById('twPlaceTitle').textContent, twice.reads.length], ['Place AI222 S3', 2]);
  // Every other label taken meanwhile too: the budget, read again, says so.
  const both = await placeHarness(t, { create: (body, n, tools) => {
    tools.make(body, 3);
    tools.make({ ...body, section_label: 'S3' }, 3);
    return { httpError: 409, error: { code: 'PLACEMENT_DUPLICATE', message: 'Duplicate' } };
  } });
  await both.select();
  await placeByKey(both);
  await placesSettled(both);
  both.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(both) && atRest(both));
  await settle();
  assert.deepEqual([placeAlert(both), placeNote(both)], ['Every section of \u2066AI222\u2069 is placed.', [true, '']]);
  // No label left: every section is placed, and Commit holds.
  const full = await placeHarness(t, {
    budget: made => placeBudget(made, { ...AI222, planned_sections: 2, remaining_sections: 1 }),
    create: (body, n, tools) => { tools.make(body, 3); return { httpError: 409, error: { code: 'PLACEMENT_DUPLICATE', message: 'Duplicate' } }; },
  });
  await full.select();
  await placeByKey(full);
  await placesSettled(full);
  full.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(full) && atRest(full));
  await settle();
  assert.deepEqual([placeAlert(full), placeRadios(full).length, placeNote(full), placeStatus(full)], ['Every section of \u2066AI222\u2069 is placed.', 0, [true, ''], '']);
  full.d.getElementById('twPlaceForm').requestSubmit();
  await settle();
  assert.equal(full.posts.length, 1);
  // "Complete pattern": the course now meets another number of times a week;
  // the patterns are read again (a fresh read) and the words say so.
  const c = await placeHarness(t, {
    create: () => ({ httpError: 400, error: { code: 'FULL_SECTION_PATTERN_REQUIRED', message: 'AI222 needs a complete 3-meeting section pattern.' } }),
    patterns: n => (n === 1 ? patternsAnswer() : patternsAnswer(manyPatterns(2, 2).map(p => ({ ...p, meetings: [...p.meetings, placeSlot('THU', p.start, p.end)] })), { required_meetings_per_section: 3 })),
  });
  await c.select();
  await placeByKey(c);
  await placesSettled(c);
  const budgets = c.seen.budget;
  c.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(c) && atRest(c) && placeRadios(c).length > 0);
  assert.deepEqual([c.reads.length, placeAlert(c)], [2, 'This course now needs 3 meetings a week; the patterns were ranked again.']);
  assert.equal(c.d.getElementById('twPlaceSub').textContent, 'Term 7 · 3 meetings a week · the whole weekly pattern');
  assert.ok(c.seen.budget > budgets, 'the list’s rows are read again');
  assert.equal(c.d.getElementById('twPlaceCommit').disabled, false);
  // A lost answer: "Nothing was placed.", and the board is read; this one did
  // place, so the budget names S2 and the dialog moves on to S3.
  const lost = await placeHarness(t, { create: (body, n, tools) => { if (n === 1) { tools.make(body); throw new TypeError('Failed to fetch'); } return undefined; } });
  await lost.select();
  await placeByKey(lost);
  await placesSettled(lost);
  lost.d.getElementById('twPlaceCommit').click();
  await wait(() => /S3/.test(placeAlert(lost)) && atRest(lost) && placeRadios(lost).length > 0);
  assert.deepEqual([placeAlert(lost), lost.w.TW.undoStack.length], ['Section \u2066S2\u2069 was placed meanwhile; this is now \u2066S3\u2069.', 0]);
  const said = await placeHarness(t, { create: () => { throw new TypeError('Failed to fetch'); } });
  await said.select();
  await placeByKey(said);
  await placesSettled(said);
  said.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(said) && atRest(said));
  assert.deepEqual([placeAlert(said), said.d.getElementById('twPlaceTitle').textContent, said.d.getElementById('twPlaceCommit').disabled], ['Nothing was placed.', 'Place AI222 S2', false]);
});

test('PL8 an English row has no nested button: Enter, or its Place…, opens its pattern chooser, never Place', async t => {
  const english = { course_code: 'ENG101', course_key: 'ENG101', course_name: 'English', programme_term: 7, planned_sections: 1, used_sections: 0,
    remaining_sections: 1, total_demand: 30, required_meetings_per_section: 4 };
  const h = await placeHarness(t, { budget: made => [...placeBudget(made), english] });
  await h.select();
  await wait(() => placeRow(h, 'S1', 'ENG101'));
  const row = placeRow(h, 'S1', 'ENG101');
  assert.deepEqual([row.querySelector('button'), row.getAttribute('aria-haspopup'), row.querySelector('.tw-place-hint')?.textContent], [null, 'dialog', 'Place…']);
  const calls = [];
  h.w.chooseEnglishWorkspacePattern = item => { calls.push([item.course_code, item.type, item.section_label]); return Promise.resolve(null); };
  await placeByKey(h, 'S1', 'ENG101');
  placeRow(h, 'S1', 'ENG101').querySelector('.tw-place-hint').click();
  await settle();
  assert.deepEqual(calls, [['ENG101', 'create_planned', 'S1'], ['ENG101', 'create_planned', 'S1']]);
  assert.deepEqual([placeDialogEl(h).hasAttribute('open'), h.reads.length], [false, 0]);
});

test('PL9 one read per section and data version: a reopen joins the read in flight or takes its answer; a write reads again', async t => {
  const first = deferred();
  const h = await placeHarness(t, { patterns: n => (n === 1 ? first.promise : patternsAnswer()), more: url => (url.endsWith('/lock/') ? { placement_id: 10, is_locked: true } : undefined) });
  await h.select();
  await placeByKey(h);
  keyOn(h, 'Escape');
  await placeByKey(h);
  for (let i = 0; i < 5; i++) await settle();
  assert.equal(h.reads.length, 1, 'a reopen joins the read in flight');
  first.resolve(patternsAnswer());
  await placesSettled(h);
  keyOn(h, 'Escape');
  await placeByKey(h);
  await placesSettled(h);
  assert.equal(h.reads.length, 1, 'open, Esc, open: the answer is kept');
  keyOn(h, 'Escape');
  await h.w.toggleLock(10);
  await placeByKey(h);
  await placesSettled(h);
  assert.equal(h.reads.length, 2, 'the data moved: read again');
});

test('PL10 the gate: an open dialog, no board, published, loading, a planner job — the first wins, said as a toast; the rows, their hint and the keys line', async t => {
  const h = await placeHarness(t);
  await h.select();
  await wait(() => placeRow(h));
  const boardId = h.w.TW.boardId;
  Object.assign(h.w.TW, { englishPicker: true, boardId: null, loadedBoardId: 99, plannerJob: { job_id: 'job-1', status: 'running' } });
  h.w.TW.scenario.status = 'published';
  const ladder = [
    ['open', null, () => { h.w.TW.englishPicker = false; }],
    ['no board', 'Create a board first.', () => { h.w.TW.boardId = boardId; }],
    ['published', 'This scenario is published and read-only.', () => { h.w.TW.scenario.status = 'draft'; }],
    ['loading', 'The board is still loading.', () => { h.w.TW.loadedBoardId = boardId; }],
    ['job', 'A planner job is running; edits wait until it ends.', () => { h.w.TW.plannerJob = null; }],
  ];
  for (const [name, words, lift] of ladder) {
    const toasts = h.notices.length;
    assert.equal(h.w.TW.keyActions.place({ ...PLACE_REQUEST }, { origin: 'place:AI222::SEARCH|S2' }), false, name);
    assert.deepEqual(h.notices.slice(toasts), words ? [{ type: 'warning', text: words }] : [], name);
    assert.deepEqual([placeDialogEl(h).hasAttribute('open'), h.reads.length], [false, 0], name);
    lift();
  }
  assert.equal(h.w.TW.keyActions.place({ ...PLACE_REQUEST }, { origin: 'place:AI222::SEARCH|S2' }), true);
  await placesSettled(h);
  keyOn(h, 'Escape');
  // A row that places: the row is the control; its hint is the pointer's
  // (hidden from assistive technology), and opens Place too.
  const row = placeRow(h);
  assert.deepEqual([row.getAttribute('role'), row.getAttribute('aria-haspopup'), row.querySelector('button')], ['gridcell', 'dialog', null]);
  const hint = row.querySelector('.tw-section-head > .tw-place-hint');
  assert.deepEqual([hint.textContent, hint.getAttribute('aria-hidden')], ['Place…', 'true']);
  assert.equal(row.querySelector('.tw-section-head > .code').innerHTML, 'AI222 <bdi>S2</bdi>');
  hint.click();
  assert.equal(placeDialogEl(h).hasAttribute('open'), true, 'its Place… opens the dialog');
  await placesSettled(h);
  keyOn(h, 'Escape');
  // A placed course says so; a row of the unplaced list says how to place it.
  const done = await placeHarness(t, { budget: () => [{ ...AI222, used_sections: 3, used_section_labels: ['S1', 'S2', 'S3'], remaining_sections: 0 }] });
  await done.select();
  await wait(() => done.d.querySelector('#twSectionList [data-focus-key="place:AI222::SEARCH|complete"]'));
  done.d.querySelector('#twSectionList [data-focus-key="place:AI222::SEARCH|complete"]').focus();
  keyOn(done, 'Enter');
  assert.equal(done.d.getElementById('twBoardStatus').textContent, 'Every section of \u2066AI222\u2069 is placed.');
  assert.equal(done.d.querySelector('#twSectionList .tw-place-hint'), null);
  const loose = await placeHarness(t, { budget: () => [], more: url => (url.includes('/unplaced/') ? { unplaced: [{ term_section_id: 77, course_code: 'CS9', section: 'S1', meetings: [] }] } : undefined) });
  await loose.select();
  await wait(() => loose.d.querySelector('#twSectionList [data-focus-key="ts:77"]'));
  loose.d.querySelector('#twSectionList [data-focus-key="ts:77"]').focus();
  keyOn(loose, 'Enter');
  assert.equal(loose.d.getElementById('twBoardStatus').textContent, 'Drag this section onto the board to place it; keyboard placement needs a section budget.');
  assert.deepEqual([placeDialogEl(loose).hasAttribute('open'), loose.d.querySelector('#twSectionList .tw-place-hint')], [false, null]);
  // The keys line says what Enter does, in both languages.
  const keys = /<p id="twListKeys"[^>]*>{% if LANGUAGE_CODE == 'ar' %}([^{]*){% else %}([^{]*){% endif %}<\/p>/.exec(template);
  assert.deepEqual([keys?.[1], keys?.[2]], ['الأسهم: الشعب · Page Up/Down: المستوى التالي · Enter: وضع الشعبة (تُفتح نافذة)',
    'Arrows: sections · Page Up/Down: next term · Enter: place the section (opens a dialog)']);
  const ar = await placeHarness(t, { arabic: true, budget: () => [], more: url => (url.includes('/unplaced/') ? { unplaced: [{ term_section_id: 77, course_code: 'CS9', section: 'S1', meetings: [] }] } : undefined) });
  await ar.select();
  await wait(() => ar.d.querySelector('#twSectionList [data-focus-key="ts:77"]'));
  ar.d.querySelector('#twSectionList [data-focus-key="ts:77"]').focus();
  keyOn(ar, 'Enter');
  assert.equal(ar.d.getElementById('twBoardStatus').textContent, 'اسحب هذه الشعبة إلى الجدول لوضعها؛ الوضع بلوحة المفاتيح يحتاج إلى ميزانية شعب.');
  const nob = await placeHarness(t, { arabic: true });
  await nob.select();
  nob.w.TW.boardId = null;
  nob.w.TW.keyActions.place({ ...PLACE_REQUEST });
  assert.deepEqual(nob.notices.at(-1), { type: 'warning', text: 'أنشئ لوحة أولاً.' });
});

test('PL11 data that moves under an open dialog: nothing chosen reads again by itself; a chosen pattern waits for Check again; an overtaken read is never drawn; a label taken meanwhile moves on', async t => {
  const lockOk = url => (url.endsWith('/lock/') ? { placement_id: 10, is_locked: true } : undefined);
  // Nothing chosen by the user: read again, the keyboard with the dialog's choice.
  const h = await placeHarness(t, { more: lockOk });
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  assert.equal(h.d.activeElement, placeRadio(h, BEST));
  await h.w.toggleLock(10);
  await wait(() => h.reads.length === 2 && placeRadios(h).length > 0);
  assert.deepEqual([placeNote(h)[0], h.d.activeElement, placeStatus(h)], [false, placeRadio(h, BEST), '4 patterns, best first · 2 with no clash']);
  // A pattern the user chose waits: stale, Commit held, nothing sent; Check
  // again reads the board, then the patterns, and the choice starts again.
  const c = await placeHarness(t, { more: lockOk });
  await c.select();
  await placeByKey(c);
  await placesSettled(c);
  placeRadio(c, SOFT).click();
  await c.w.toggleLock(10);
  assert.deepEqual([placeStatus(c), placeRadios(c).length, placeNote(c), c.reads.length],
    ['The timetable changed since these patterns were read.', 0, [true, ''], 1]);
  c.d.getElementById('twPlaceForm').requestSubmit();
  await settle();
  assert.deepEqual([c.posts.length, placeAlert(c)], [0, '']);
  const boards = boardReads(c);
  c.d.querySelector('#twPlaceBody [data-tw-place-recheck]').click();
  await wait(() => c.reads.length === 2 && placeRadios(c).length > 0);
  assert.equal(boardReads(c), boards + 1, 'the board, read again');
  assert.deepEqual([placeChecked(c), placeNote(c)[0]], [BEST, false]);
  // Check again drops the choice at once: a board gone from view by the time
  // it is read is read again by itself once back.
  let away = false;
  const k = await placeHarness(t, { more: (url, opts) => {
    if (url.endsWith('/lock/')) return lockOk(url);
    if (away && url === '/ops/tw/boards/2/') { k.w.TW.scenarioId = 9; return undefined; }
    return undefined;
  } });
  await k.select();
  await placeByKey(k);
  await placesSettled(k);
  placeRadio(k, SOFT).click();
  await k.w.toggleLock(10);
  away = true;
  k.d.querySelector('#twPlaceBody [data-tw-place-recheck]').click();
  await wait(() => placeStatus(k) === 'The board changed; nothing was placed.' && atRest(k));
  away = false;
  k.w.TW.scenarioId = 1;
  k.w.syncWorkspaceControls();
  await wait(() => k.reads.length === 2 && placeRadios(k).length > 0);
  assert.equal(placeChecked(k), BEST);
  // A write out as the dialog opens: its read is overtaken as the write
  // lands, read again at once, and the first answer is never drawn.
  const lock = deferred();
  const first = deferred();
  const o = await placeHarness(t, { patterns: n => (n === 1 ? first.promise : patternsAnswer([PATTERNS[0]])), more: url => (url.endsWith('/lock/') ? lock.promise : undefined) });
  await o.select();
  const locking = o.w.toggleLock(10);
  await settle();
  await placeByKey(o);
  await settle();
  assert.equal(o.reads.length, 1);
  lock.resolve({ placement_id: 10, is_locked: true });
  await locking;
  await wait(() => placeRadios(o).length > 0);
  assert.equal(o.reads.length, 2, 'read again without waiting for the first answer');
  first.resolve(patternsAnswer());
  for (let i = 0; i < 5; i++) await settle();
  assert.deepEqual(placeRadios(o).map(radio => radio.value), [SOFT], 'the overtaken answer is not drawn');
  // Another section took S2 meanwhile (the budget, read again, names it): the
  // dialog moves to S3 by itself, and says so.
  let taken = false;
  const l = await placeHarness(t, { budget: made => placeBudget(made, taken ? { ...AI222, used_section_labels: ['S1', 'S2'] } : AI222) });
  await l.select();
  await placeByKey(l);
  await placesSettled(l);
  taken = true;
  await l.w.selectBoard(2);
  await wait(() => l.reads.length === 2 && placeRadios(l).length > 0);
  assert.deepEqual([placeAlert(l), l.d.getElementById('twPlaceTitle').textContent, l.d.getElementById('twPlaceDialog').dataset.twAbout],
    ['Section \u2066S2\u2069 was placed meanwhile; this is now \u2066S3\u2069.', 'Place AI222 S3', 'place:AI222::SEARCH|S3']);
  // The scenario read again (a planner job ended): the board leaves and
  // comes back; the patterns are read again once it is back.
  let back = null;
  const g = await placeHarness(t, { more: url => (back && url === '/ops/tw/boards/2/' ? back.promise : undefined) });
  await g.select();
  await placeByKey(g);
  await placesSettled(g);
  back = deferred();
  const reloading = g.w.onScenarioChange();
  await wait(() => placeStatus(g) === 'The board is still loading.');
  assert.deepEqual([g.reads.length, placeNote(g), placeRadios(g).length], [1, [true, BUSY_WORDS], 0]);
  back.resolve({ placements: [at(10)], slot_config: MOVE_COLUMNS });
  back = null;
  await reloading;
  await wait(() => g.reads.length === 2 && placeRadios(g).length > 0);
  assert.equal(placeDialogEl(g).hasAttribute('open'), true);
});

test('PL19 after a place the board’s stop is the new section’s first card, not merely the board’s first', async t => {
  const h = await placeHarness(t);
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  placeRadio(h, SOFT).click();
  placeRadio(h, SOFT).focus();
  keyOn(h, 'Enter');
  await wait(() => h.posts.length === 1 && atRest(h));
  assert.equal(h.posts[0].meetings[0].day, 'MON');
  assert.deepEqual([...h.d.querySelectorAll('#twGrid [tabindex="0"]')].map(element => element.dataset.focusKey), ['placement:91'],
    'the Monday card, though the Sunday card comes first on the board');
});

test('PL18 a course code from the budget is text in the dialog title, never markup', async t => {
  const code = 'AI<b>2"2';
  const h = await placeHarness(t, { budget: made => placeBudget(made, { ...AI222, course_code: code }) });
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  const title = h.d.getElementById('twPlaceTitle');
  assert.equal(title.querySelector('b'), null);
  assert.equal(title.textContent, `Place ${code} S2`);
});

test('PL12 a create the user left (Escape) ends where the user is: a toast with no dialog open; in Move’s dialog, named, when one is open by then; a refusal too', async t => {
  const moves = url => (url.includes('/slot-candidates/') ? movesAnswer() : undefined);
  for (const arabic of [false, true]) {
    const landed = deferred();
    const h = await placeHarness(t, { arabic, more: moves, create: async () => { await landed.promise; return undefined; } });
    await h.select();
    await placeByKey(h);
    await placesSettled(h);
    h.d.getElementById('twPlaceCommit').click();
    await settle();
    keyOn(h, 'Escape');
    assert.deepEqual([placeDialogEl(h).hasAttribute('open'), h.d.activeElement], [false, placeRow(h)], 'Escape is never blocked by a commit');
    moveByKey(h, 10);
    await movesSettled(h);
    const toasts = h.notices.length;
    landed.resolve();
    const line = h.d.getElementById('twMoveAlert');
    await wait(() => !line.hidden && atRest(h));
    assert.deepEqual([line.textContent, line.dataset.tone], arabic
      ? ['\u2066AI222\u2069 \u2066S2\u2069: وُضعت \u2066AI222\u2069 \u2066S2\u2069. · يُضعف الجودة: لم تُحدَّد القاعة بعد · اضغط R على البطاقة لتغيير قاعتها.', 'warning']
      : ['\u2066AI222\u2069 \u2066S2\u2069: Placed \u2066AI222\u2069 \u2066S2\u2069. · Lowers quality: Room not assigned yet · Press R on the card to change its room.', 'warning']);
    assert.deepEqual(h.notices.slice(toasts), [], 'no toast behind the dialog');
    assert.equal(h.w.TW.undoStack.at(-1).type, 'section_create');
  }
  // No dialog open by then: a toast.
  const landed = deferred();
  const n = await placeHarness(t, { create: async () => { await landed.promise; return undefined; } });
  await n.select();
  await placeByKey(n);
  await placesSettled(n);
  n.d.getElementById('twPlaceCommit').click();
  await settle();
  keyOn(n, 'Escape');
  const before = n.notices.length;
  landed.resolve();
  await wait(() => n.notices.length > before && atRest(n));
  assert.deepEqual(n.notices.slice(before), [{ type: 'warning', text: 'Placed \u2066AI222\u2069 \u2066S2\u2069. · Lowers quality: Room not assigned yet · Press R on the card to change its room.' }]);
  // A refusal of a create the user left: a toast, as it would be anywhere.
  const refused = deferred();
  const r = await placeHarness(t, { create: () => refused.promise });
  await r.select();
  await placeByKey(r);
  await placesSettled(r);
  r.d.getElementById('twPlaceCommit').click();
  await settle();
  keyOn(r, 'Escape');
  const quietly = r.notices.length;
  refused.resolve({ httpError: 400, error: { code: 'SCENARIO_PUBLISHED', message: 'Published' } });
  await wait(() => r.notices.length > quietly && atRest(r));
  assert.deepEqual(r.notices.slice(quietly), [{ type: 'warning', text: 'This scenario is published and read-only.' }]);
  assert.equal(r.w.TW.undoStack.length, 0);
  // The two refusals Place answers by itself while open (a pattern of another
  // length; a label placed meanwhile) say so where the user is once left,
  // and read nothing for the closed dialog: no toast there, none missing.
  for (const [code, words] of [
    ['FULL_SECTION_PATTERN_REQUIRED', 'Nothing was placed.'],
    ['PLACEMENT_DUPLICATE', 'The board changed; nothing was placed.'],
  ]) {
    // No dialog open by then: a toast.
    const answer = deferred();
    const g = await placeHarness(t, { create: () => answer.promise });
    await g.select();
    await placeByKey(g);
    await placesSettled(g);
    g.d.getElementById('twPlaceCommit').click();
    await settle();
    keyOn(g, 'Escape');
    const said = g.notices.length;
    const reads = g.requests.filter(row => row.url.includes('/planned-slot-candidates/')).length;
    answer.resolve({ httpError: 409, error: { code, message: 'refused' } });
    await wait(() => g.notices.length > said && atRest(g));
    assert.deepEqual(g.notices.slice(said), [{ type: 'warning', text: words }], code);
    assert.equal(g.requests.filter(row => row.url.includes('/planned-slot-candidates/')).length, reads, `${code}: nothing read for the closed dialog`);
    assert.equal(g.w.TW.undoStack.length, 0);
    // Move's dialog open by then: said there, named.
    const late = deferred();
    const m = await placeHarness(t, { more: moves, create: () => late.promise });
    await m.select();
    await placeByKey(m);
    await placesSettled(m);
    m.d.getElementById('twPlaceCommit').click();
    await settle();
    keyOn(m, 'Escape');
    moveByKey(m, 10);
    await movesSettled(m);
    const toasts = m.notices.length;
    late.resolve({ httpError: 409, error: { code, message: 'refused' } });
    const line = m.d.getElementById('twMoveAlert');
    await wait(() => !line.hidden && atRest(m));
    assert.equal(line.textContent, `\u2066AI222\u2069 \u2066S2\u2069: ${words}`, code);
    assert.deepEqual(m.notices.slice(toasts), [], `${code}: no toast behind Move's dialog`);
  }
  // Place open again by then: on another section, said there, named, and its
  // own Commit not held; on the same section, held.
  for (const [label, words, held] of [
    ['S3', '\u2066AI222\u2069 \u2066S2\u2069: This scenario is published and read-only.', false],
    ['S2', 'This scenario is published and read-only.', true],
  ]) {
    const later = deferred();
    const a = await placeHarness(t, { create: (body, n) => (n === 1 ? later.promise : undefined) });
    await a.select();
    await placeByKey(a);
    await placesSettled(a);
    a.d.getElementById('twPlaceCommit').click();
    await settle();
    keyOn(a, 'Escape');
    await placeByKey(a, label);
    await placesSettled(a);
    const toasts = a.notices.length;
    later.resolve({ httpError: 400, error: { code: 'SCENARIO_PUBLISHED', message: 'Published' } });
    await wait(() => placeAlert(a) && atRest(a));
    await settle();
    assert.deepEqual([placeAlert(a), a.d.getElementById('twPlaceTitle').textContent, a.notices.slice(toasts)], [words, `Place AI222 ${label}`, []], label);
    assert.equal(a.d.getElementById('twPlaceCommit').disabled, held, label);
  }
});

test('PL13 reads: ranking is said with the list busy and the keyboard on the body; a failure with Try again is not kept; a busy server; the board gone; no pattern; a course that meets once reads as times', async t => {
  const reply = deferred();
  const slow = await placeHarness(t, { patterns: () => reply.promise });
  await slow.select();
  await placeByKey(slow);
  await settle();
  assert.deepEqual([placeStatus(slow), slow.d.getElementById('twPlaceChoices').getAttribute('aria-busy'), placeNote(slow), slow.d.activeElement],
    ['Ranking the patterns…', 'true', [true, ''], slow.d.getElementById('twPlaceBody')]);
  const close = placeDialogEl(slow).querySelector('.ux-dialog-head [data-dialog-close]');
  close.focus();
  reply.resolve(patternsAnswer());
  await placesSettled(slow);
  assert.equal(slow.d.activeElement, close, 'an answer never takes the keyboard from where the user put it');
  // A failure: said, Try again, Commit held, no toast; not kept.
  const failing = await placeHarness(t, { patterns: n => (n === 1 ? { httpError: 500, error: { code: 'BOOM', message: 'Broken' } } : patternsAnswer()) });
  await failing.select();
  await placeByKey(failing);
  await wait(() => failing.d.querySelector('#twPlaceBody [data-tw-place-retry]'));
  assert.deepEqual([placeStatus(failing), placeNote(failing), failing.notices.filter(n => n.type === 'error')], ['The patterns could not be ranked.', [true, ''], []]);
  assert.equal(failing.d.querySelector('#twPlaceBody [data-tw-place-retry]').textContent, 'Try again');
  keyOn(failing, 'Escape');
  await placeByKey(failing);
  await placesSettled(failing);
  assert.equal(failing.reads.length, 2, 'a failed read is not kept');
  const retry = await placeHarness(t, { patterns: n => (n === 1 ? { httpError: 500, error: { code: 'BOOM', message: 'Broken' } } : patternsAnswer()) });
  await retry.select();
  await placeByKey(retry);
  await wait(() => retry.d.querySelector('#twPlaceBody [data-tw-place-retry]'));
  retry.d.querySelector('#twPlaceBody [data-tw-place-retry]').click();
  await placesSettled(retry);
  assert.deepEqual([retry.reads.length, placeStatus(retry)], [2, '4 patterns, best first · 2 with no clash']);
  const busy = await placeHarness(t, { patterns: () => ({ httpError: 503, error: { code: 'MAINTENANCE', message: 'Down' } }) });
  await busy.select();
  await placeByKey(busy);
  await wait(() => busy.d.querySelector('#twPlaceBody [data-tw-place-retry]'));
  assert.equal(placeStatus(busy), 'The server is busy; try again in a moment.');
  // The board is gone on the server: said, no Try again, Commit held.
  const gone = await placeHarness(t, { patterns: () => ({ httpError: 404, error: { code: 'NOT_FOUND', message: 'Board not found' } }) });
  await gone.select();
  await placeByKey(gone);
  await wait(() => placeStatus(gone) === 'The board changed; nothing was placed.');
  assert.deepEqual([gone.d.querySelector('#twPlaceBody [data-tw-place-retry]'), placeNote(gone)], [null, [true, '']]);
  // No pattern fits.
  const empty = await placeHarness(t, { patterns: () => patternsAnswer([]) });
  await empty.select();
  await placeByKey(empty);
  await wait(() => placeStatus(empty).startsWith('No complete'));
  assert.deepEqual([placeStatus(empty), empty.d.querySelector('#twPlaceBody fieldset'), placeNote(empty)],
    ["No complete pattern fits this board's teaching columns. Check Edit time slots… and blocked times.", null, [true, '']]);
  // A course that meets once a week: its times, labs marked, in the list and the filter.
  const once = { ...AI222, required_meetings_per_section: 1 };
  const times = [
    { kind: 'lect', day: 'MON', start: '09:00', end: '10:15', verdict: verdictOf(), verdict_rank: 2, score: 1 },
    { kind: 'lab', day: 'TUE', start: '13:00', end: '15:30', verdict: verdictOf(), verdict_rank: 1, score: 2 },
    { kind: 'lect', day: 'WED', start: '09:00', end: '10:15', verdict: verdictOf([vItem('time', 'critical', { other: otherAt('AI9', 'S1', 'WED', '09:00', '10:15'), students: 30, blocked_students: 30 })]), verdict_rank: 3, score: 3 },
  ];
  const o = await placeHarness(t, { budget: made => placeBudget(made, once), patterns: () => patternsAnswer(times, { required_meetings_per_section: 1, requires_full_section_pattern: false }) });
  await o.select();
  await placeByKey(o);
  await placesSettled(o);
  assert.equal(o.d.getElementById('twPlaceSub').textContent, 'Term 7 · one meeting a week');
  assert.deepEqual(placeRadios(o).map(radio => [radio.value, placeName(o, radio)]),
    [['TUE@13:00-15:30', 'Tuesday 13:00–15:30 · Lab'], ['MON@09:00-10:15', 'Monday 09:00–10:15'], ['WED@09:00-10:15', 'Wednesday 09:00–10:15']]);
  assert.equal(placeSaid(o, placeRadio(o, 'WED@09:00-10:15'), 1), 'Time clash: AI9 S1 · Wed 09:00–10:15 · 30 students need both', 'no meeting index, no lead');
  assert.equal(o.d.querySelector('#twPlaceBody fieldset:last-of-type > legend').textContent, 'Times, best first');
  assert.equal(placeStatus(o), '3 times, best first · 2 with no clash');
  assert.deepEqual([...o.d.getElementById('twPlaceTime').options].map(option => option.textContent), ['Any time', '\u206609:00–10:15\u2069', '\u206613:00–15:30\u2069 · Lab']);
  o.d.getElementById('twPlaceCommit').click();
  await wait(() => o.posts.length === 1 && atRest(o));
  assert.deepEqual(o.posts[0].meetings, [{ day: 'TUE', start_time: '13:00', end_time: '15:30' }]);
  const oar = await placeHarness(t, { arabic: true, budget: made => placeBudget(made, once), patterns: () => patternsAnswer(times, { required_meetings_per_section: 1 }) });
  await oar.select();
  await placeByKey(oar);
  await placesSettled(oar);
  assert.deepEqual([oar.d.getElementById('twPlaceSub').textContent, placeStatus(oar), oar.d.querySelector('#twPlaceBody fieldset:last-of-type > legend').textContent],
    ['Term 7 · لقاء واحد أسبوعياً', '3 أوقات، الأفضل أولاً · وقتان بلا تعارض', 'الأوقات، الأفضل أولاً']);
  assert.equal(placeName(oar, placeRadio(oar, 'TUE@13:00-15:30')), 'الثلاثاء 13:00–15:30 · معمل');
  const timesReply = deferred();
  const ranking = await placeHarness(t, { budget: made => placeBudget(made, once), patterns: () => timesReply.promise });
  await ranking.select();
  await placeByKey(ranking);
  await settle();
  assert.equal(placeStatus(ranking), 'Ranking the times…');
  timesReply.resolve({ httpError: 500, error: { code: 'BOOM', message: 'Broken' } });
  await wait(() => placeStatus(ranking) === 'The times could not be ranked.');
});

test('PL14 nothing is sent for a board the patterns were not read for: the scenario, the board and its load each stop the commit', async t => {
  const h = await placeHarness(t);
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  const changes = [
    ['another scenario', () => { h.w.TW.scenarioId = 9; }, () => { h.w.TW.scenarioId = 1; }],
    ['another board', () => { h.w.TW.boardId = 3; }, () => { h.w.TW.boardId = 2; }],
    ['the board loading', () => { h.w.TW.loadedBoardId = 3; }, () => { h.w.TW.loadedBoardId = 2; }],
  ];
  const alert = h.d.getElementById('twPlaceAlert');
  for (const [name, change, undo] of changes) {
    change();
    h.d.getElementById('twPlaceForm').requestSubmit();
    await settle();
    assert.deepEqual([alert.textContent, alert.hidden, h.posts.length], ['The board changed; nothing was placed.', false, 0], name);
    undo();
    alert.textContent = '';
    alert.hidden = true;
    h.w.drawPlaceDialog();
    await wait(() => placeNote(h)[0] === false);
  }
  h.d.getElementById('twPlaceForm').requestSubmit();
  await wait(() => h.posts.length === 1);
});

test('PL15 committing: the keyboard is on the chosen pattern before Commit turns off; the list is busy, the status line says so, the note is quiet', async t => {
  const answer = deferred();
  let seen = null;
  const h = await placeHarness(t, { create: () => answer.promise });
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  placeRadio(h, SOFT).click();
  const commit = h.d.getElementById('twPlaceCommit');
  commit.focus();
  const fetch = h.w.fetch;
  h.w.fetch = async (url, opts) => {
    if (opts?.method === 'POST') seen = [h.d.activeElement, commit.disabled];
    return fetch(url, opts);
  };
  let atOff = null;
  const original = Object.getOwnPropertyDescriptor(h.w.HTMLButtonElement.prototype, 'disabled');
  Object.defineProperty(commit, 'disabled', {
    configurable: true,
    get() { return original.get.call(this); },
    set(value) { if (value && !atOff) atOff = h.d.activeElement; original.set.call(this, value); },
  });
  commit.click();
  await settle();
  assert.equal(atOff, placeRadio(h, SOFT), 'the keyboard is on the chosen pattern when Commit turns off');
  assert.deepEqual(seen, [placeRadio(h, SOFT), true]);
  assert.equal(h.d.getElementById('twPlaceChoices').getAttribute('aria-busy'), 'true');
  assert.deepEqual([placeStatus(h), h.d.getElementById('twPlaceStatus').closest('[aria-busy="true"]')], ['Placing the section…', null]);
  assert.deepEqual(placeNote(h), [true, '']);
  answer.resolve(undefined);
  await wait(() => !placeDialogEl(h).hasAttribute('open') && atRest(h));
});

test('PL16 the course’s term when it is not the board’s is a note; a course online for this board says no room is booked; the note gives the first hold before the choice', async t => {
  const h = await placeHarness(t, { budget: made => placeBudget(made, { ...AI222, programme_term: 5 }) });
  await h.select();
  await placeByKey(h);
  await placesSettled(h);
  const note = h.d.querySelector('#twPlaceBody > .tw-place-note');
  assert.deepEqual([note?.getAttribute('role'), note?.querySelector('.ux-tone-icon')?.dataset.tone, note?.textContent],
    ['note', 'warning', 'This course is in term 5; this board is term 7.']);
  const same = await placeHarness(t);
  await same.select();
  await placeByKey(same);
  await placesSettled(same);
  assert.equal(same.d.querySelector('#twPlaceBody .tw-place-note'), null, 'the board’s own term: no note');
  const ar = await placeHarness(t, { arabic: true, budget: made => placeBudget(made, { ...AI222, programme_term: 5 }) });
  await ar.select();
  await placeByKey(ar);
  await placesSettled(ar);
  assert.equal(ar.d.querySelector('#twPlaceBody .tw-place-note').textContent, 'هذا المقرر في المستوى 5، وهذه اللوحة للمستوى 7.');
  // Online for this board (its S1 here says so): no room is booked.
  const online = await placeHarness(t, { rows: () => [at(10, { course_code: 'AI222', course_key: 'AI222::SEARCH', section: 'S1', is_online: true, needs_room: false })] });
  await online.select();
  await placeByKey(online);
  await placesSettled(online);
  assert.deepEqual(placeNote(online), [false, 'Chosen: No clash found · Online for this board: no room is booked.']);
  const oar = await placeHarness(t, { arabic: true, rows: () => [at(10, { course_code: 'AI222', course_key: 'AI222::SEARCH', section: 'S1', is_online: true, needs_room: false })] });
  await oar.select();
  await placeByKey(oar);
  await placesSettled(oar);
  assert.equal(placeNote(oar)[1], 'المختار: لا تعارض · عن بُعد في هذه اللوحة: لا تُحجز قاعة.');
  const other = await placeHarness(t, { rows: () => [at(10, { course_code: 'GS101', course_key: 'GS101', is_online: true, needs_room: false })] });
  await other.select();
  await placeByKey(other);
  await placesSettled(other);
  assert.deepEqual(placeNote(other), [false, `Chosen: No clash found · ${ROOMS_NOTE}`], 'another course online here says nothing of this one');
  // The first hold, then the choice: busy, a planner job, published.
  let refresh = null;
  const n = await placeHarness(t, { more: url => (refresh && url === '/ops/tw/boards/2/' ? refresh.promise : undefined) });
  await n.select();
  await placeByKey(n);
  await placesSettled(n);
  refresh = deferred();
  const refreshing = n.w.selectBoard(2);
  await settle();
  assert.deepEqual(placeNote(n), [true, BUSY_WORDS]);
  n.w.dispatchEvent(new n.w.CustomEvent('tw:planner-job', { detail: { scenarioId: 1, job: { job_id: 'job-3', status: 'running' }, source: 'adapter' } }));
  assert.deepEqual(placeNote(n), [true, 'A planner job is running; edits wait until it ends.']);
  n.w.TW.scenario.status = 'published';
  n.w.syncWorkspaceControls();
  assert.deepEqual(placeNote(n), [true, 'This scenario is published and read-only.']);
  // A commit asked for all the same is answered inside, and its words go
  // when that hold ends.
  const toasts = n.notices.length;
  n.d.getElementById('twPlaceForm').requestSubmit();
  await settle();
  assert.deepEqual([placeAlert(n), n.posts.length, n.notices.slice(toasts)], ['This scenario is published and read-only.', 0, []]);
  n.w.TW.scenario.status = 'draft';
  n.w.TW.plannerJob = null;
  refresh.resolve({ placements: [at(10)], slot_config: MOVE_COLUMNS });
  refresh = null;
  await refreshing;
  assert.deepEqual([placeAlert(n), placeNote(n)], ['', [false, `Chosen: No clash found · ${ROOMS_NOTE}`]]);
});

test('PL17 the dialog’s other words in Arabic: ranking, failures, the fallback, placing, a label taken, every section placed, a new count, the board changed', async t => {
  const ar = options => placeHarness(t, { arabic: true, ...options });
  const open = async (h, label = 'S2') => { await h.select(); await placeByKey(h, label); };
  // The row's hint, and ranking.
  const reply = deferred();
  const slow = await ar({ patterns: () => reply.promise, more: url => (url === '/ops/tw/x/' ? { ok: true } : undefined) });
  await slow.select();
  await wait(() => placeRow(slow));
  assert.equal(placeRow(slow).querySelector('.tw-place-hint').textContent, 'وضع…');
  await placeByKey(slow);
  await settle();
  assert.equal(placeStatus(slow), 'جارٍ ترتيب الأنماط…');
  reply.resolve(patternsAnswer());
  await placesSettled(slow);
  // A write while a pattern is chosen: the list is out of date.
  placeRadio(slow, SOFT).click();
  await slow.w.twFetch('/ops/tw/x/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  assert.equal(placeStatus(slow), 'تغيّر الجدول منذ قراءة هذه الأنماط.');
  assert.equal(slow.d.querySelector('#twPlaceBody [data-tw-place-recheck]').textContent, 'إعادة التحقق');
  // A failure, and a course that meets once: its times.
  const failing = await ar({ patterns: () => ({ httpError: 500, error: { code: 'BOOM', message: 'Broken' } }) });
  await open(failing);
  await wait(() => failing.d.querySelector('#twPlaceBody [data-tw-place-retry]'));
  assert.deepEqual([placeStatus(failing), failing.d.querySelector('#twPlaceBody [data-tw-place-retry]').textContent], ['تعذّر ترتيب الأنماط.', 'إعادة المحاولة']);
  const once = { ...AI222, required_meetings_per_section: 1 };
  const timesSlow = deferred();
  const t1 = await ar({ budget: made => placeBudget(made, once), patterns: () => timesSlow.promise });
  await open(t1);
  await settle();
  assert.equal(placeStatus(t1), 'جارٍ ترتيب الأوقات…');
  timesSlow.resolve({ httpError: 500, error: { code: 'BOOM', message: 'Broken' } });
  await wait(() => placeStatus(t1) === 'تعذّر ترتيب الأوقات.');
  // No pattern; the board gone on the server.
  const empty = await ar({ patterns: () => patternsAnswer([]) });
  await open(empty);
  await wait(() => placeStatus(empty).startsWith('لا يوجد'));
  assert.equal(placeStatus(empty), 'لا يوجد نمط كامل يناسب أعمدة التدريس في هذه اللوحة. راجع «تعديل الفترات الزمنية…» والأوقات المحجوبة.');
  const gone = await ar({ patterns: () => ({ httpError: 404, error: { code: 'NOT_FOUND', message: 'Board not found' } }) });
  await open(gone);
  await wait(() => placeStatus(gone) === 'تغيّرت اللوحة؛ لم يُوضع شيء.');
  // A drop where no pattern meets.
  const drop = await ar();
  await drop.select();
  await wait(() => placeRow(drop));
  await dropAt(drop, dragPayloadOf(drop), 'MON', '09:00');
  await placesSettled(drop);
  assert.equal(placeStatus(drop), 'لا يوجد نمط يشمل الاثنين \u206609:00–10:15\u2069؛ تُعرض كل الأنماط.');
  // Placing, then a refusal that says nothing was placed.
  const answer = deferred();
  const placing = await ar({ create: () => answer.promise });
  await open(placing);
  await placesSettled(placing);
  placing.d.getElementById('twPlaceCommit').click();
  await settle();
  assert.equal(placeStatus(placing), 'جارٍ وضع الشعبة…');
  answer.resolve({ httpError: 400, error: { code: 'VALIDATION_PLANNED_PATTERN', message: 'Bad' } });
  await wait(() => placeAlert(placing) && atRest(placing));
  assert.equal(placeAlert(placing), 'لم يُوضع شيء.');
  // A label taken meanwhile; then every section placed.
  const dup = await ar({ create: (body, n, tools) => { tools.make(body, 3); return { httpError: 409, error: { code: 'PLACEMENT_DUPLICATE', message: 'Duplicate' } }; } });
  await open(dup);
  await placesSettled(dup);
  dup.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(dup) && atRest(dup) && placeRadios(dup).length > 0);
  assert.deepEqual([placeAlert(dup), dup.d.getElementById('twPlaceTitle').textContent], ['وُضعت الشعبة \u2066S2\u2069 في هذه الأثناء؛ هذه الآن \u2066S3\u2069.', 'وضع AI222 S3']);
  dup.d.getElementById('twPlaceCommit').click();
  await wait(() => /كل شعب/.test(placeAlert(dup)) && atRest(dup));
  assert.equal(placeAlert(dup), 'وُضعت كل شعب \u2066AI222\u2069.');
  // A new count, in its genitive form.
  const count = await ar({
    create: () => ({ httpError: 400, error: { code: 'FULL_SECTION_PATTERN_REQUIRED', message: 'AI222 needs a complete 3-meeting section pattern.' } }),
    patterns: n => (n === 1 ? patternsAnswer() : patternsAnswer(PATTERNS, { required_meetings_per_section: n === 2 ? 3 : 2 })),
  });
  await open(count);
  await placesSettled(count);
  count.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(count) && atRest(count) && placeRadios(count).length > 0);
  assert.equal(placeAlert(count), 'يحتاج هذا المقرر الآن إلى 3 لقاءات أسبوعياً؛ أُعيد ترتيب الأنماط.');
  count.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(count).includes('لقاءين') && atRest(count));
  assert.equal(placeAlert(count), 'يحتاج هذا المقرر الآن إلى لقاءين أسبوعياً؛ أُعيد ترتيب الأنماط.');
  // The board changed under the dialog.
  const moved = await ar();
  await open(moved);
  await placesSettled(moved);
  moved.w.TW.boardId = 3;
  moved.d.getElementById('twPlaceForm').requestSubmit();
  await settle();
  assert.equal(placeAlert(moved), 'تغيّرت اللوحة؛ لم يُوضع شيء.');
});

/* ── Edit time slots (design record §7.2 item 6, 6f) ──
   The slot dialog: the scenario's effective columns as rows; a save on PR 1's
   contract (a refused field marked on the row sent at its index, the server's
   list reviewed inside the dialog and confirmed by its fingerprint, flags
   never sent); edits never lost. These replace the textarea editor's tests
   (a spec change). */
const SLOT_LRI = String.fromCharCode(0x2066);
const SLOT_PDI = String.fromCharCode(0x2069);
const slotIso = text => `${SLOT_LRI}${text}${SLOT_PDI}`;
const SLOT_COLUMNS = { slot_config: [], lab_slot_config: [],
  effective_slot_config: [{ label: '09:00-10:15', start: '09:00', end: '10:15' }, { label: 'Late', start: '16:00', end: '17:15' }],
  effective_lab_slot_config: [{ label: 'Lab 1', start: '09:00', end: '10:40' }, { label: 'Bundle', start: '13:00', end: '16:00', policy_only: true },
    { label: 'Online 1', start: '15:50', end: '17:30', online_only: true }] };
const SLOT_HIDDEN = { hidden: [{ placement_id: 10, board_id: 2, board_label: 'Term 7', course_code: 'CS323', section: 'S1', day: 'MON',
  start_time: '16:00', end_time: '17:15', is_locked: true }], hidden_count: 1, dropped_flags: [], fingerprint: 'abc123' };
const slotNeedsConfirm = (details = SLOT_HIDDEN) => ({ httpError: 409, error: { code: 'SLOT_EDIT_NEEDS_CONFIRMATION', message: 'x', details } });
const slotInvalid = errors => ({ httpError: 400, error: { code: 'VALIDATION_SLOTS', message: 'x', details: { errors } } });
const slotSaved = (body, extra = {}) => ({ ok: true, hidden_count: 0, ...extra, scenario: { ...scenario, ...SLOT_COLUMNS, updated_at: 'saved now',
  slot_config: body.slot_config, lab_slot_config: body.lab_slot_config, effective_slot_config: body.slot_config, effective_lab_slot_config: body.lab_slot_config } });
// The board answers with the scenario's columns as the server has them (a save changes them).
function slotHarness(t, { answer = body => slotSaved(body), arabic = false, more = () => undefined } = {}) {
  const posts = [];
  const stored = { ...SLOT_COLUMNS };
  return harness(t, async (url, opts) => {
    const own = await more(url, opts);
    if (own !== undefined) return own;
    if (url === '/ops/tw/boards/2/') return { placements: [placement], ...stored };
    if (url !== '/ops/tw/scenarios/1/slots/update/') return undefined;
    const body = JSON.parse(opts.body);
    posts.push({ method: opts.method, body });
    const reply = await answer(body, posts.length);
    if (reply?.ok) ['slot_config', 'lab_slot_config', 'effective_slot_config', 'effective_lab_slot_config'].forEach(key => { stored[key] = reply.scenario[key]; });
    return reply;
  }, SLOT_COLUMNS, arabic).then(h => Object.assign(h, { posts }));
}
const SLOT_TABLE = { slot_config: 'twSlotLectures', lab_slot_config: 'twSlotLabs' };
const slotDialogEl = h => h.d.getElementById('twSlotDialog');
const slotRowsOf = (h, list = 'slot_config') => [...h.d.querySelectorAll(`#${SLOT_TABLE[list]} tbody tr[data-row-key]`)];
const slotInput = (h, list, index, field) => slotRowsOf(h, list)[index].querySelector(`input[data-field="${field}"]`);
const slotButton = (h, list, index, kind) => slotRowsOf(h, list)[index].querySelector(`[data-row-action="${kind}"]`);
const slotValues = (h, list) => slotRowsOf(h, list).map(tr => ['label', 'start', 'end'].map(field => tr.querySelector(`input[data-field="${field}"]`).value));
const slotLengths = (h, list) => slotRowsOf(h, list).map(tr => tr.querySelector('.ux-rows-length').textContent);
const slotAlert = h => (h.d.getElementById('twSlotAlert').hidden ? '' : h.d.getElementById('twSlotAlert').textContent);
const slotSaveButton = h => h.d.getElementById('twSlotSave');
const slotShown = (h, id) => !h.d.getElementById(id).hidden;
const slotType = (h, input, value) => {
  input.focus();
  input.value = value;
  input.dispatchEvent(new h.w.Event('input', { bubbles: true }));
};
// Opened as the user does: the ⋯ menu's item, once the page is at rest.
const slotOpen = async h => {
  await wait(() => atRest(h) && !h.d.getElementById('twSlotEdit').disabled);
  h.d.getElementById('twSlotEdit').click();
  assert.equal(slotDialogEl(h).hasAttribute('open'), true, 'the dialog opens');
};
const slotSubmit = h => h.d.getElementById('twSlotForm').requestSubmit();
const slotOut = h => h.d.getElementById('twSlotEditor').hasAttribute('inert');

test('SD1 one row per effective column, its flags as pills, the keyboard on the first label; a second open is refused', async t => {
  const h = await slotHarness(t);
  await h.select();
  await slotOpen(h);
  assert.deepEqual(slotValues(h, 'slot_config'), [['09:00-10:15', '09:00', '10:15'], ['Late', '16:00', '17:15']]);
  assert.deepEqual(slotValues(h, 'lab_slot_config'), [['Lab 1', '09:00', '10:40'], ['Bundle', '13:00', '16:00'], ['Online 1', '15:50', '17:30']]);
  const pills = list => slotRowsOf(h, list).map(tr => [...tr.querySelectorAll('.pill-status')].map(pill => pill.textContent));
  assert.deepEqual([pills('slot_config'), pills('lab_slot_config')], [[[], []], [[], ['Policy column'], ['Online only']]]);
  assert.deepEqual([slotLengths(h, 'slot_config'), slotLengths(h, 'lab_slot_config')], [['75 min', '75 min'], ['100 min', '180 min', '100 min']]);
  assert.equal(h.d.activeElement, slotInput(h, 'slot_config', 0, 'label'));
  // Each field is named by its row header and its column header.
  const nameOf = element => element.getAttribute('aria-labelledby').split(' ').map(id => h.d.getElementById(id).textContent).join(' ');
  assert.deepEqual(['label', 'start', 'end'].map(field => nameOf(slotInput(h, 'lab_slot_config', 1, field))),
    ['Lab column 2 Label', 'Lab column 2 Start', 'Lab column 2 End']);
  assert.equal(nameOf(slotInput(h, 'slot_config', 1, 'end')), 'Lecture column 2 End');
  assert.deepEqual(slotRowsOf(h).map(tr => tr.querySelector('th[scope="row"]').textContent), ['Lecture column 1', 'Lecture column 2']);
  const start = slotInput(h, 'slot_config', 0, 'start');
  assert.deepEqual(['type', 'inputmode', 'dir', 'autocomplete', 'spellcheck', 'class'].map(name => start.getAttribute(name)),
    ['text', 'numeric', 'ltr', 'off', 'false', 'ux-rows-time']);
  assert.deepEqual(['type', 'dir'].map(name => slotInput(h, 'slot_config', 0, 'label').getAttribute(name)), ['text', 'auto']);
  assert.deepEqual([...h.d.querySelectorAll('#twSlotLabs thead th')].map(th => th.textContent), ['Label', 'Start', 'End', 'Length', 'Rule']);
  assert.equal(h.d.getElementById('twSlotScenario').textContent, 'Real scenario copy');
  assert.equal(h.d.getElementById('twSlotFlags').textContent, 'The online-only columns (15:50) stay online-only while they keep the same start and end.');
  assert.deepEqual(['twSlotDraft', 'twSlotOneRow', 'twSlotReview', 'twSlotBack', 'twSlotAlert'].map(id => slotShown(h, id)), [false, false, false, false, false]);
  assert.equal(slotSaveButton(h).textContent, slotSaveButton(h).dataset.saveLabel);
  assert.equal(slotDialogEl(h).dataset.twAbout, 'slots:1');
  // A second open while it is open: refused, the rows as the user left them.
  slotType(h, slotInput(h, 'slot_config', 1, 'label'), 'Evening');
  const field = slotInput(h, 'slot_config', 1, 'label');
  assert.equal(h.w.openSlotDialog(), false);
  assert.equal(slotInput(h, 'slot_config', 1, 'label'), field, 'nothing drawn again');
  assert.equal(field.value, 'Evening');
  assert.deepEqual([h.posts, h.errors], [[], []]);
});

test('SD2 a save sends the rows in their order, trimmed, an empty value as empty, never a flag; no time is checked here', async t => {
  const h = await slotHarness(t, { answer: () => slotInvalid([]) });
  await h.select();
  await slotOpen(h);
  slotType(h, slotInput(h, 'slot_config', 1, 'label'), '');
  slotType(h, slotInput(h, 'slot_config', 1, 'start'), ' 4pm ');
  slotType(h, slotInput(h, 'lab_slot_config', 2, 'label'), ' Online one ');
  slotType(h, slotInput(h, 'lab_slot_config', 2, 'end'), '');
  slotSubmit(h);
  await wait(() => h.posts.length === 1 && slotAlert(h) && atRest(h));
  assert.deepEqual(h.posts, [{ method: 'POST', body: {
    slot_config: [{ label: '09:00-10:15', start: '09:00', end: '10:15' }, { label: '', start: '4pm', end: '17:15' }],
    lab_slot_config: [{ label: 'Lab 1', start: '09:00', end: '10:40' }, { label: 'Bundle', start: '13:00', end: '16:00' }, { label: 'Online one', start: '15:50', end: '' }],
  } }]);
  // The rows keep what was typed.
  assert.deepEqual(slotValues(h, 'slot_config')[1], ['', ' 4pm ', '17:15']);
});

test('SD3 a refused field is marked on the row sent at its index, though the rows moved before Save; the keyboard on it; the alert says it all; no toast', async t => {
  const errors = [
    { list: 'lab_slot_config', index: 1, field: 'end', message: 'must be after the start' },
    { list: 'lab_slot_config', index: 1, field: 'start', message: 'must be a time as HH:MM' },
    { list: 'slot_config', index: -1, field: 'list', message: 'has more than 40 columns' },
  ];
  const later = deferred();
  const h = await slotHarness(t, { answer: (body, n) => (n === 1 ? slotInvalid(errors) : later.promise) });
  await h.select();
  await slotOpen(h);
  // Lab 1 goes down: it is sent second, where Bundle was when the dialog opened.
  slotButton(h, 'lab_slot_config', 0, 'down').click();
  assert.deepEqual(slotValues(h, 'lab_slot_config').map(row => row[0]), ['Bundle', 'Lab 1', 'Online 1']);
  const toasts = h.notices.length;
  slotSubmit(h);
  await wait(() => slotAlert(h) && atRest(h) && !slotOut(h));
  const marked = () => [...h.d.querySelectorAll('#twSlotDialog [aria-invalid="true"]')];
  assert.deepEqual(marked(), [slotInput(h, 'lab_slot_config', 1, 'start'), slotInput(h, 'lab_slot_config', 1, 'end')]);
  assert.equal(h.d.activeElement, slotInput(h, 'lab_slot_config', 1, 'start'), 'the first marked field has the keyboard');
  const said = h.d.getElementById(slotInput(h, 'lab_slot_config', 1, 'end').getAttribute('aria-describedby'));
  assert.equal(h.d.getElementById(slotInput(h, 'lab_slot_config', 1, 'label').getAttribute('aria-describedby')), said, 'every field of the row is described by its error');
  assert.deepEqual([said.textContent, said.closest('tr').hidden, said.closest('tr').dataset.errorFor], ['End: must be after the start; Start: must be a time as HH:MM', false, slotRowsOf(h, 'lab_slot_config')[1].dataset.rowKey]);
  const others = [...h.d.querySelectorAll('#twSlotDialog tr[data-error-for]')].filter(row => row !== said.closest('tr'));
  assert.ok(others.length === 4 && others.every(row => row.hidden && row.textContent === ''), 'no other row says anything');
  assert.equal(slotAlert(h), 'The columns were not saved: Labs, line 2: end must be after the start; Labs, line 2: start must be a time as HH:MM; Lectures: has more than 40 columns');
  assert.equal(h.d.getElementById('twSlotAlert').dataset.tone, 'warning');
  assert.deepEqual(h.notices.slice(toasts), [], 'nothing toasted behind the dialog');
  assert.equal(slotDialogEl(h).hasAttribute('open'), true);
  // A marked row keeps its mark when it moves.
  slotButton(h, 'lab_slot_config', 1, 'up').click();
  assert.deepEqual(marked(), [slotInput(h, 'lab_slot_config', 0, 'start'), slotInput(h, 'lab_slot_config', 0, 'end')]);
  assert.equal(slotRowsOf(h, 'lab_slot_config')[0].nextElementSibling.hidden, false);
  // The next save clears every mark, and the alert, as it starts.
  slotSubmit(h);
  await settle();
  const errorRows = [...h.d.querySelectorAll('#twSlotDialog tr[data-error-for]')];
  assert.deepEqual([marked(), slotAlert(h), errorRows.length, errorRows.every(row => row.hidden && row.textContent === '')], [[], '', 5, true]);
  assert.equal(h.d.getElementById(slotInput(h, 'lab_slot_config', 0, 'end').getAttribute('aria-describedby')).textContent, '');
  later.resolve(slotSaved(h.posts[1].body));
  await wait(() => !slotDialogEl(h).hasAttribute('open') && atRest(h));
});

test('SD4 the server’s list is reviewed inside the dialog: Back sends nothing and keeps the edits; Save anyway confirms by the fingerprint; Escape settles the save', async t => {
  const confirmed = deferred();
  const h = await slotHarness(t, { answer: body => (body.confirm === 'abc123' ? confirmed.promise : slotNeedsConfirm()) });
  await h.select();
  let stacked = 0;
  h.w.dlg = { confirm: async () => { stacked++; return true; } };
  await slotOpen(h);
  // The 16:00 column goes.
  slotButton(h, 'slot_config', 1, 'remove').click();
  const save = slotSaveButton(h);
  slotSubmit(h);
  await wait(() => slotShown(h, 'twSlotReview') && atRest(h));
  assert.deepEqual([slotShown(h, 'twSlotEditor'), slotShown(h, 'twSlotBack'), save.textContent, save.disabled, slotOut(h)],
    [false, true, save.getAttribute('data-review-label'), false, false]);
  assert.equal(h.d.activeElement, h.d.getElementById('twSlotReviewTitle'), 'the keyboard on the review’s heading');
  const review = h.d.getElementById('twSlotReviewBody').innerHTML;
  assert.match(review, /^<p class="tw-readiness-lead">This change leaves 1 meeting outside the grid's columns\. They stay in the timetable and are still published; each is shown under its table/);
  assert.match(review, /<bdi>Term 7<\/bdi> · <bdi>CS323 S1<\/bdi> · Mon <bdi dir="ltr">16:00–17:15<\/bdi> · time locked/);
  assert.deepEqual([stacked, h.posts.map(post => post.body.confirm)], [0, [undefined]], 'no dialog over the dialog');
  // Back: nothing sent, the rows as they were (the removal kept), Save under the keyboard.
  h.d.getElementById('twSlotBack').click();
  await wait(() => slotShown(h, 'twSlotEditor'));
  await settle();
  assert.deepEqual([h.posts.length, slotShown(h, 'twSlotReview'), slotShown(h, 'twSlotBack'), save.textContent, slotAlert(h)],
    [1, false, false, save.dataset.saveLabel, '']);
  assert.deepEqual(slotValues(h, 'slot_config'), [['09:00-10:15', '09:00', '10:15']]);
  assert.equal(h.d.activeElement, save);
  // Save, then Save anyway: the same body again, with the fingerprint; the
  // review stays in view while it is out, with no way back.
  slotSubmit(h);
  await wait(() => slotShown(h, 'twSlotReview') && atRest(h));
  slotSubmit(h);
  await settle();
  assert.deepEqual([slotShown(h, 'twSlotReview'), slotShown(h, 'twSlotEditor'), slotShown(h, 'twSlotBack'), save.textContent, save.disabled,
    h.d.getElementById('twSlotStatus').textContent, h.d.activeElement.id], [true, false, false, save.getAttribute('data-review-label'), true, 'Saving the columns…', 'twSlotBody']);
  slotSubmit(h);
  await settle();
  assert.equal(h.posts.length, 3, 'one Save anyway');
  confirmed.resolve(slotSaved(h.posts[2].body, { hidden_count: 1 }));
  await wait(() => !slotDialogEl(h).hasAttribute('open') && atRest(h));
  assert.deepEqual(h.posts.map(post => post.body.confirm), [undefined, undefined, 'abc123']);
  assert.deepEqual(h.posts[2].body.slot_config, h.posts[1].body.slot_config);
  assert.equal(stacked, 0);
  // Escape while Save anyway is out: the next open (before it ends) is the
  // rows, never the review.
  const late = deferred();
  const g = await slotHarness(t, { answer: body => (body.confirm ? late.promise : slotNeedsConfirm()) });
  await g.select();
  await slotOpen(g);
  slotSubmit(g);
  await wait(() => slotShown(g, 'twSlotReview') && atRest(g));
  slotSubmit(g);
  await settle();
  keyOn(g, 'Escape');
  assert.equal(slotDialogEl(g).hasAttribute('open'), false);
  g.w.openSlotDialog();
  assert.deepEqual([slotShown(g, 'twSlotEditor'), slotShown(g, 'twSlotReview'), slotSaveButton(g).textContent], [true, false, slotSaveButton(g).dataset.saveLabel]);
  late.resolve(slotSaved(g.posts[1].body));
  await wait(() => atRest(g) && slotAlert(g));
  assert.equal(slotAlert(g), 'Columns saved: 2 lecture, 3 lab.');
  // Escape during the review: the save settles, nothing more is sent, and a
  // review the user closed is not reported.
  const e = await slotHarness(t, { answer: () => slotNeedsConfirm() });
  await e.select();
  await slotOpen(e);
  const toasts = e.notices.length;
  const saving = e.w.saveSlotDialog();
  await wait(() => slotShown(e, 'twSlotReview'));
  keyOn(e, 'Escape');
  const settled = await Promise.race([saving.then(() => 'settled'), new Promise(resolve => setTimeout(() => resolve('hanging'), 1000))]);
  assert.equal(settled, 'settled');
  await settle();
  assert.deepEqual([slotDialogEl(e).hasAttribute('open'), e.posts.length, e.notices.slice(toasts)], [false, 1, []]);
  // Opened again: the rows, never the review.
  await slotOpen(e);
  assert.deepEqual([slotShown(e, 'twSlotEditor'), slotShown(e, 'twSlotReview'), slotShown(e, 'twSlotBack'), slotSaveButton(e).textContent],
    [true, false, false, slotSaveButton(e).dataset.saveLabel]);
  assert.equal(e.d.activeElement, slotInput(e, 'slot_config', 0, 'label'));
});

test('SD4b the list changed while it was reviewed: the new list is reviewed, confirmed by its own fingerprint; one that keeps changing is said after four answers, the last never reviewed', async t => {
  const other = { ...SLOT_HIDDEN, hidden: [{ ...SLOT_HIDDEN.hidden[0], placement_id: 11, course_code: 'AI777' }], fingerprint: 'def456' };
  const h = await slotHarness(t, { answer: body => (body.confirm === 'def456' ? slotSaved(body) : body.confirm === 'abc123' ? slotNeedsConfirm(other) : slotNeedsConfirm()) });
  await h.select();
  await slotOpen(h);
  slotSubmit(h);
  await wait(() => slotShown(h, 'twSlotReview') && atRest(h));
  assert.match(h.d.getElementById('twSlotReviewBody').innerHTML, /CS323 S1/);
  slotSubmit(h);
  // The second answer is another list: it is the one reviewed now.
  await wait(() => /AI777 S1/.test(h.d.getElementById('twSlotReviewBody').innerHTML) && atRest(h));
  assert.doesNotMatch(h.d.getElementById('twSlotReviewBody').innerHTML, /CS323/);
  assert.deepEqual([slotShown(h, 'twSlotBack'), slotSaveButton(h).disabled, h.d.activeElement.id, slotOut(h)], [true, false, 'twSlotReviewTitle', false]);
  slotSubmit(h);
  await wait(() => !slotDialogEl(h).hasAttribute('open') && atRest(h));
  assert.deepEqual(h.posts.map(post => post.body.confirm), [undefined, 'abc123', 'def456']);
  // A list that changes at every answer: three reviews; the fourth answer is
  // never shown (nothing would be sent after a Save anyway on it): the
  // module's words, the rows back.
  let n = 0;
  const k = await slotHarness(t, { answer: () => slotNeedsConfirm({ ...SLOT_HIDDEN, fingerprint: `fp${++n}` }) });
  await k.select();
  await slotOpen(k);
  slotSubmit(k);
  for (let i = 0; i < 3; i++) {
    await wait(() => slotShown(k, 'twSlotReview') && atRest(k) && k.posts.length === i + 1);
    slotSubmit(k);
  }
  await wait(() => slotAlert(k) && atRest(k) && k.posts.length === 4);
  assert.deepEqual([k.posts.map(post => post.body.confirm), slotAlert(k), slotShown(k, 'twSlotEditor'), slotShown(k, 'twSlotReview'), k.d.activeElement],
    [[undefined, 'fp1', 'fp2', 'fp3'], 'The timetable kept changing while you reviewed it; nothing was saved. Try again.', true, false, slotSaveButton(k)]);
});

test('SD5 saved: the dialog closes, the keyboard goes back to the ⋯ menu, the board and the quality are read again, the draft goes, and how many meetings now sit under their tables is a warning', async t => {
  const h = await slotHarness(t, { answer: body => slotSaved(body, { hidden_count: 2 }) });
  await h.select();
  await slotOpen(h);
  slotType(h, slotInput(h, 'slot_config', 1, 'label'), 'Evening');
  const before = h.requests.length;
  const toasts = h.notices.length;
  slotSubmit(h);
  await wait(() => !slotDialogEl(h).hasAttribute('open') && atRest(h) && h.notices.length > toasts);
  const urls = h.requests.slice(before).map(request => request.url);
  const saved = urls.indexOf('/ops/tw/scenarios/1/slots/update/');
  assert.ok(saved >= 0 && urls.indexOf('/ops/tw/boards/2/', saved) > saved, 'the board, read after the save');
  assert.ok(urls.some((url, i) => i > saved && url.includes('/quality/')), 'the quality, read again');
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'warning', text: 'Columns saved: 2 lecture, 3 lab; now under their tables, outside the columns: 2 meetings.' }]);
  assert.equal(h.d.activeElement, h.d.getElementById('twMoreMenu'));
  assert.equal(h.w.TW.scenario.effective_slot_config[1].label, 'Evening');
  assert.equal(h.w.TW.scenario.updated_at, 'saved now', 'the scenario as the save answered it');
  // The draft went with the save.
  await slotOpen(h);
  assert.deepEqual([slotShown(h, 'twSlotDraft'), slotInput(h, 'slot_config', 1, 'label').value], [false, 'Evening']);
  // Nothing outside the columns: a success; one meeting: its count.
  for (const [count, tone, text] of [[0, 'success', 'Columns saved: 2 lecture, 3 lab.'],
    [1, 'warning', 'Columns saved: 2 lecture, 3 lab; now under their tables, outside the columns: 1 meeting.']]) {
    const s = await slotHarness(t, { answer: body => slotSaved(body, { hidden_count: count }) });
    await s.select();
    await slotOpen(s);
    const shown = s.notices.length;
    slotSubmit(s);
    await wait(() => !slotDialogEl(s).hasAttribute('open') && atRest(s) && s.notices.length > shown);
    assert.deepEqual(s.notices.slice(shown), [{ type: tone, text }]);
  }
});

test('SD6 Add, Remove, Up and Down keep the keyboard where the rule says; a list’s only row cannot be removed, and says why', async t => {
  const h = await slotHarness(t);
  await h.select();
  await slotOpen(h);
  const keys = list => slotRowsOf(h, list).map(tr => tr.dataset.rowKey);
  const names = (list, i) => ['up', 'down', 'remove'].map(kind => [slotButton(h, list, i, kind).getAttribute('aria-label'), slotButton(h, list, i, kind).title]);
  const late = slotIso('Late');
  assert.deepEqual(names('slot_config', 1), [[`Move ${late} up`, `Move ${late} up`], [`Move ${late} down`, `Move ${late} down`], [`Remove ${late}`, `Remove ${late}`]]);
  // Up on the first row and Down on the last are off (focusable, aria-disabled).
  const off = (list, i) => ['up', 'down', 'remove'].map(kind => slotButton(h, list, i, kind).getAttribute('aria-disabled'));
  assert.deepEqual([off('slot_config', 0), off('slot_config', 1), off('lab_slot_config', 1)], [['true', null, null], [null, 'true', null], [null, null, null]]);
  assert.ok([...h.d.querySelectorAll('#twSlotDialog [data-row-action]')].every(button => !button.disabled && button.type === 'button'));
  // Add: a row at the end, the keyboard on its label, named by its list and number.
  h.d.querySelector('[data-slot-add="slot_config"]').click();
  assert.deepEqual([slotRowsOf(h).length, slotValues(h, 'slot_config')[2], slotLengths(h, 'slot_config')[2]], [3, ['', '', ''], '—']);
  assert.equal(h.d.activeElement, slotInput(h, 'slot_config', 2, 'label'));
  assert.equal(slotButton(h, 'slot_config', 2, 'remove').getAttribute('aria-label'), `Remove ${slotIso('Lecture column 3')}`);
  assert.deepEqual([off('slot_config', 1), off('slot_config', 2)], [[null, null, null], [null, 'true', null]], 'the old last row moves down again');
  // Typing names the row's buttons and gives its length.
  slotType(h, slotInput(h, 'slot_config', 2, 'label'), 'Night');
  slotType(h, slotInput(h, 'slot_config', 2, 'start'), '18:00');
  assert.equal(slotLengths(h, 'slot_config')[2], '—');
  slotType(h, slotInput(h, 'slot_config', 2, 'end'), '19:15');
  assert.deepEqual([names('slot_config', 2)[0], slotLengths(h, 'slot_config')[2]], [[`Move ${slotIso('Night')} up`, `Move ${slotIso('Night')} up`], '75 min']);
  // Up: the rows swap, the keyboard stays on the same button; at the top it is off and moves nothing.
  const night = keys('slot_config')[2];
  slotButton(h, 'slot_config', 2, 'up').click();
  assert.deepEqual([keys('slot_config')[1], h.d.activeElement], [night, slotButton(h, 'slot_config', 1, 'up')]);
  slotButton(h, 'slot_config', 1, 'up').click();
  assert.deepEqual([keys('slot_config')[0], h.d.activeElement, h.d.activeElement.getAttribute('aria-disabled')], [night, slotButton(h, 'slot_config', 0, 'up'), 'true']);
  h.d.activeElement.click();
  assert.deepEqual([keys('slot_config')[0], h.d.activeElement], [night, slotButton(h, 'slot_config', 0, 'up')]);
  // Down: the same, the other way; at the bottom it is off.
  slotButton(h, 'slot_config', 0, 'down').click();
  assert.deepEqual([keys('slot_config')[1], h.d.activeElement], [night, slotButton(h, 'slot_config', 1, 'down')]);
  slotButton(h, 'slot_config', 1, 'down').click();
  assert.deepEqual([keys('slot_config')[2], h.d.activeElement.getAttribute('aria-disabled')], [night, 'true']);
  h.d.activeElement.click();
  assert.equal(keys('slot_config')[2], night);
  // Remove: the keyboard on the next row's label, or on Add after the last row.
  const [first, second] = keys('slot_config');
  slotButton(h, 'slot_config', 0, 'remove').click();
  assert.deepEqual([keys('slot_config'), h.d.activeElement], [[second, night], slotInput(h, 'slot_config', 0, 'label')]);
  assert.ok(!keys('slot_config').includes(first));
  slotButton(h, 'slot_config', 1, 'remove').click();
  assert.equal(h.d.activeElement, h.d.querySelector('[data-slot-add="slot_config"]'));
  // One row left: Remove is off with its reason, which shows, and removes nothing.
  const only = slotButton(h, 'slot_config', 0, 'remove');
  assert.deepEqual([only.getAttribute('aria-disabled'), only.getAttribute('aria-describedby'), slotShown(h, 'twSlotOneRow')], ['true', 'twSlotOneRow', true]);
  assert.match(h.d.getElementById('twSlotOneRow').textContent, /A list keeps at least one column: an empty list would mean the default columns\./);
  only.click();
  assert.equal(slotRowsOf(h).length, 1, 'never an empty list');
  // A second row: the reason hides; back to one, it shows.
  h.d.querySelector('[data-slot-add="slot_config"]').click();
  assert.deepEqual([slotShown(h, 'twSlotOneRow'), slotButton(h, 'slot_config', 0, 'remove').getAttribute('aria-disabled')], [false, null]);
  slotButton(h, 'slot_config', 1, 'remove').click();
  assert.equal(slotShown(h, 'twSlotOneRow'), true);
  assert.ok(slotRowsOf(h, 'lab_slot_config').every((tr, i) => slotButton(h, 'lab_slot_config', i, 'remove').getAttribute('aria-disabled') === null),
    'the labs keep their own');
  slotSubmit(h);
  await wait(() => h.posts.length === 1);
  assert.deepEqual(h.posts[0].body.slot_config, [{ label: 'Late', start: '16:00', end: '17:15' }]);
  await wait(() => !slotDialogEl(h).hasAttribute('open') && atRest(h));
  assert.deepEqual(h.errors, []);
  // A list that arrives with no row (the server's never do) is never sent:
  // an empty list would be saved as the defaults.
  const none = await slotHarness(t);
  await none.select();
  none.w.TW.scenario.effective_slot_config = [];
  await slotOpen(none);
  assert.deepEqual([slotRowsOf(none).length, slotShown(none, 'twSlotOneRow')], [0, true]);
  slotSubmit(none);
  await settle();
  assert.deepEqual([none.posts.length, slotAlert(none).includes('A list keeps at least one column: an empty list would mean the default columns.')], [0, true]);
  none.d.querySelector('[data-slot-add="slot_config"]').click();
  slotType(none, slotInput(none, 'slot_config', 0, 'start'), '09:00');
  slotType(none, slotInput(none, 'slot_config', 0, 'end'), '10:15');
  slotSubmit(none);
  await wait(() => none.posts.length === 1 && !slotDialogEl(none).hasAttribute('open') && atRest(none));
  assert.deepEqual(none.posts[0].body.slot_config, [{ label: '', start: '09:00', end: '10:15' }]);
});

test('SD7 edits are never lost: Cancel, Escape and a failure keep the scenario’s draft, the next open offers it, and “Start from the saved columns” sets it aside', async t => {
  const h = await slotHarness(t, { answer: () => ({ httpError: 503, error: { code: 'SERVER_BUSY', message: 'Busy' } }) });
  await h.select();
  await slotOpen(h);
  slotType(h, slotInput(h, 'slot_config', 1, 'label'), 'Evening');
  slotButton(h, 'lab_slot_config', 0, 'down').click();
  // Cancel: the next open has the edits, and says so.
  h.d.querySelector('#twSlotDialog .ux-dialog-foot [data-dialog-close]').click();
  assert.equal(slotDialogEl(h).hasAttribute('open'), false);
  await slotOpen(h);
  assert.equal(slotShown(h, 'twSlotDraft'), true);
  assert.match(h.d.getElementById('twSlotDraft').textContent, /Your unsaved columns are back\..*Start from the saved columns/);
  assert.equal(slotShown(h, 'twSlotDraftChanged'), false, 'the saved columns are the ones it started from');
  assert.deepEqual([slotInput(h, 'slot_config', 1, 'label').value, slotValues(h, 'lab_slot_config').map(row => row[0])], ['Evening', ['Bundle', 'Lab 1', 'Online 1']]);
  assert.equal(h.d.activeElement, slotInput(h, 'slot_config', 0, 'label'));
  // Escape: the same.
  slotType(h, slotInput(h, 'slot_config', 0, 'label'), 'Morning');
  keyOn(h, 'Escape');
  assert.equal(slotDialogEl(h).hasAttribute('open'), false);
  await slotOpen(h);
  assert.deepEqual(slotValues(h, 'slot_config').map(row => row[0]), ['Morning', 'Evening']);
  // A failure: said in the dialog; the edits stay, Save under the keyboard again; the draft kept.
  const toasts = h.notices.length;
  slotSubmit(h);
  await wait(() => slotAlert(h) && atRest(h) && !slotOut(h));
  assert.deepEqual([slotAlert(h), h.d.activeElement, slotValues(h, 'slot_config').map(row => row[0]), h.notices.slice(toasts)],
    ['The columns were not saved.', slotSaveButton(h), ['Morning', 'Evening'], []]);
  keyOn(h, 'Escape');
  await slotOpen(h);
  assert.deepEqual([slotShown(h, 'twSlotDraft'), slotAlert(h)], [true, ''], 'offered again, the old alert gone');
  assert.equal(h.d.getElementById('twSlotAlert').hidden, true, 'no empty warning strip');
  // "Start from the saved columns": the saved rows, the hint gone, the keyboard on the first label, the draft gone.
  slotSubmit(h);
  await wait(() => slotAlert(h) && atRest(h) && !slotOut(h));
  h.d.getElementById('twSlotFromSaved').click();
  assert.deepEqual([slotValues(h, 'slot_config').map(row => row[0]), slotValues(h, 'lab_slot_config').map(row => row[0])],
    [['09:00-10:15', 'Late'], ['Lab 1', 'Bundle', 'Online 1']]);
  assert.deepEqual([slotShown(h, 'twSlotDraft'), h.d.activeElement, slotAlert(h)], [false, slotInput(h, 'slot_config', 0, 'label'), '']);
  keyOn(h, 'Escape');
  await slotOpen(h);
  assert.equal(slotShown(h, 'twSlotDraft'), false);
  // Edits taken back to what is saved leave no draft.
  slotType(h, slotInput(h, 'slot_config', 1, 'label'), 'X');
  slotType(h, slotInput(h, 'slot_config', 1, 'label'), 'Late');
  slotButton(h, 'slot_config', 0, 'down').click();
  slotButton(h, 'slot_config', 1, 'up').click();
  keyOn(h, 'Escape');
  await slotOpen(h);
  assert.equal(slotShown(h, 'twSlotDraft'), false);
  // A draft is its scenario's: another scenario opens on its own columns.
  slotType(h, slotInput(h, 'slot_config', 1, 'label'), 'Mine');
  keyOn(h, 'Escape');
  Object.assign(h.w.TW, { scenarioId: 2, scenario: { ...h.w.TW.scenario, id: 2, name: 'Another' } });
  assert.equal(h.w.openSlotDialog(), true);
  assert.deepEqual([slotShown(h, 'twSlotDraft'), slotInput(h, 'slot_config', 1, 'label').value, slotDialogEl(h).dataset.twAbout], [false, 'Late', 'slots:2']);
});

test('SD8 Save waits for the page: published, a planner job or a write in flight hold it with the first hold’s words; while its save is out the rows are inert', async t => {
  const answer = deferred();
  const lock = deferred();
  let n = 0;
  const h = await slotHarness(t, {
    answer: body => (++n === 1 ? answer.promise : slotSaved(body)),
    more: url => (url.endsWith('/lock/') ? lock.promise : undefined),
  });
  await h.select();
  // At the open: no scenario, or a published one, is a toast; no dialog.
  h.w.TW.scenario.status = 'published';
  let toasts = h.notices.length;
  assert.equal(h.w.openSlotDialog(), false);
  assert.deepEqual([h.notices.slice(toasts), slotDialogEl(h).hasAttribute('open')], [[{ type: 'warning', text: 'This scenario is published and read-only.' }], false]);
  h.w.TW.scenario.status = 'draft';
  const id = h.w.TW.scenarioId;
  h.w.TW.scenarioId = null;
  toasts = h.notices.length;
  assert.equal(h.w.openSlotDialog(), false);
  assert.deepEqual([h.notices.slice(toasts), slotDialogEl(h).hasAttribute('open')], [[{ type: 'warning', text: 'Select a scenario first' }], false]);
  h.w.TW.scenarioId = id;
  await slotOpen(h);
  const save = slotSaveButton(h);
  const note = () => [save.disabled, h.d.getElementById('twSlotNote').textContent];
  assert.deepEqual(note(), [false, '']);
  h.w.TW.plannerJob = { job_id: 'j', status: 'running' };
  h.w.syncWorkspaceControls();
  assert.deepEqual(note(), [true, 'A planner job is running; edits wait until it ends.']);
  h.w.TW.scenario.status = 'published';
  h.w.syncWorkspaceControls();
  assert.deepEqual(note(), [true, 'This scenario is published and read-only.'], 'the first hold');
  // Submitted anyway: the hold is said in the alert, nothing is sent, and its words go with it.
  slotSubmit(h);
  await wait(() => slotAlert(h));
  assert.deepEqual([slotAlert(h), h.posts.length, slotOut(h)], ['This scenario is published and read-only.', 0, false]);
  h.w.TW.scenario.status = 'draft';
  h.w.syncWorkspaceControls();
  assert.deepEqual([slotAlert(h), note()], ['', [true, 'A planner job is running; edits wait until it ends.']]);
  h.w.TW.plannerJob = null;
  h.w.syncWorkspaceControls();
  assert.deepEqual(note(), [false, '']);
  // Another write out: busy.
  const locking = h.w.toggleLock(10);
  await settle();
  assert.deepEqual(note(), [true, 'Wait for the current operation to finish.']);
  lock.resolve({ placement_id: 10, is_locked: true });
  await locking;
  await wait(() => atRest(h) && !save.disabled);
  assert.equal(h.d.getElementById('twSlotNote').textContent, '');
  // The save out: the rows inert, the status says so, the keyboard waits on the body, Save off and its note quiet.
  slotType(h, slotInput(h, 'slot_config', 0, 'label'), 'Early');
  save.focus();
  slotSubmit(h);
  await settle();
  assert.deepEqual([slotOut(h), h.d.getElementById('twSlotStatus').textContent, save.disabled, save.hasAttribute('data-tw-hold'), h.d.getElementById('twSlotNote').textContent, h.d.activeElement.id],
    [true, 'Saving the columns…', true, true, '', 'twSlotBody']);
  slotSubmit(h);
  await settle();
  assert.deepEqual([h.posts.length, slotAlert(h), h.d.getElementById('twSlotStatus').textContent, slotOut(h)], [1, '', 'Saving the columns…', true],
    'one save at a time; the one out is left alone');
  answer.resolve({ httpError: 503, error: { code: 'SERVER_BUSY', message: 'Busy' } });
  await wait(() => slotAlert(h) && atRest(h) && !slotOut(h));
  assert.deepEqual([h.d.getElementById('twSlotStatus').textContent, save.disabled, save.hasAttribute('data-tw-hold'), h.d.activeElement],
    ['', false, false, save]);
  // A refusal the server gives: the gate's words.
  slotSubmit(h);
  await wait(() => !slotDialogEl(h).hasAttribute('open') && atRest(h));
  const refused = await slotHarness(t, { answer: () => ({ httpError: 400, error: { code: 'SCENARIO_PUBLISHED', message: 'Published' } }) });
  await refused.select();
  await slotOpen(refused);
  slotSubmit(refused);
  await wait(() => slotAlert(refused) && atRest(refused));
  assert.equal(slotAlert(refused), 'This scenario is published and read-only.');
});

test('SD9 in Arabic: the times left to right, the label in its own direction, the words and lengths in Arabic, the review’s times isolated, the toast in Arabic', async t => {
  const h = await slotHarness(t, { arabic: true, answer: body => (body.confirm === 'abc123' ? slotSaved(body, { hidden_count: 2 }) : slotNeedsConfirm()) });
  await h.select();
  await slotOpen(h);
  assert.deepEqual(['start', 'end', 'label'].map(field => slotInput(h, 'slot_config', 0, field).getAttribute('dir')), ['ltr', 'ltr', 'auto']);
  assert.deepEqual([slotLengths(h, 'slot_config'), slotLengths(h, 'lab_slot_config')], [['75 دقيقةً', '75 دقيقةً'], ['100 دقيقة', '180 دقيقةً', '100 دقيقة']]);
  assert.deepEqual([...h.d.querySelectorAll('#twSlotLectures thead th')].map(th => th.textContent), ['الاسم', 'البداية', 'النهاية', 'المدة', 'القيد']);
  assert.deepEqual(slotRowsOf(h, 'lab_slot_config').map(tr => tr.querySelector('th[scope="row"]').textContent), ['عمود معامل 1', 'عمود معامل 2', 'عمود معامل 3']);
  assert.deepEqual(slotRowsOf(h, 'lab_slot_config').map(tr => [...tr.querySelectorAll('.pill-status')].map(pill => pill.textContent)), [[], ['عمود سياسة'], ['عن بُعد فقط']]);
  const late = slotIso('Late');
  assert.deepEqual(['up', 'down', 'remove'].map(kind => slotButton(h, 'slot_config', 1, kind).getAttribute('aria-label')),
    [`نقل ${late} إلى الأعلى`, `نقل ${late} إلى الأسفل`, `حذف ${late}`]);
  h.d.querySelector('[data-slot-add="slot_config"]').click();
  assert.equal(slotButton(h, 'slot_config', 2, 'remove').getAttribute('aria-label'), `حذف ${slotIso('عمود محاضرات 3')}`);
  slotButton(h, 'slot_config', 2, 'remove').click();
  assert.equal(h.d.getElementById('twSlotFlags').textContent, 'أعمدة «عن بُعد فقط» (15:50) تبقى كذلك ما دامت بالبداية والنهاية نفسيهما.');
  // The review: Arabic, its time isolated left to right; Save anyway; the toast.
  slotButton(h, 'slot_config', 1, 'remove').click();
  slotSubmit(h);
  await wait(() => slotShown(h, 'twSlotReview') && atRest(h));
  const review = h.d.getElementById('twSlotReviewBody').innerHTML;
  assert.match(review, /هذا التغيير يترك لقاءً واحداً خارج أعمدة الجدول\. تبقى في الجدول وتُنشر، ويظهر كلٌّ منها أسفل جدوله/);
  assert.match(review, /الاثنين <bdi dir="ltr">16:00–17:15<\/bdi> · الوقت مقفل/);
  const toasts = h.notices.length;
  slotSubmit(h);
  await wait(() => !slotDialogEl(h).hasAttribute('open') && atRest(h) && h.notices.length > toasts);
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'warning', text: 'حُفظت الأعمدة: المحاضرات 1، المعامل 3؛ أسفل جداولها الآن خارج الأعمدة: لقاءان.' }]);
  // Saving, a refused field and a failure, in Arabic.
  const out = deferred();
  const errors = [{ list: 'slot_config', index: 0, field: 'end', message: 'must be after the start' }];
  let n = 0;
  const w = await slotHarness(t, { arabic: true, answer: () => (++n === 1 ? out.promise : slotInvalid(errors)) });
  await w.select();
  await slotOpen(w);
  slotSubmit(w);
  await settle();
  assert.equal(w.d.getElementById('twSlotStatus').textContent, 'جارٍ حفظ الأعمدة…');
  out.resolve({ httpError: 503, error: { code: 'SERVER_BUSY', message: 'Busy' } });
  await wait(() => slotAlert(w) && atRest(w) && !slotOut(w));
  assert.equal(slotAlert(w), 'لم تُحفظ الأعمدة.');
  slotSubmit(w);
  await wait(() => /المحاضرات، السطر 1/.test(slotAlert(w)) && atRest(w) && !slotOut(w));
  assert.equal(slotAlert(w), 'لم تُحفظ الأعمدة: المحاضرات، السطر 1: النهاية يجب أن تكون بعد البداية');
  assert.equal(w.d.getElementById(slotInput(w, 'slot_config', 0, 'end').getAttribute('aria-describedby')).textContent, 'النهاية: يجب أن تكون بعد البداية');
  // The template's words, in both arms.
  const arms = (english, arabic) => assert.ok(template.includes(`{% if LANGUAGE_CODE == 'ar' %}${arabic}{% else %}${english}{% endif %}`), english);
  arms('Edit time slots', 'تعديل الفترات الزمنية');
  arms('The grid\'s columns, for every board of this scenario', 'أعمدة الجدول لكل لوحات هذا السيناريو');
  arms('Lectures (75 min)', 'المحاضرات (75 دقيقة)');
  arms('Labs (100 min)', 'المعامل (100 دقيقة)');
  arms('Add a lecture column', 'إضافة عمود محاضرات');
  arms('Add a lab column', 'إضافة عمود معامل');
  arms('A list keeps at least one column: an empty list would mean the default columns.', 'تبقى في القائمة فترة واحدة على الأقل: القائمة الفارغة تعني الأعمدة الافتراضية.');
  arms('Your unsaved columns are back.', 'عادت أعمدتك غير المحفوظة.');
  arms('Start from the saved columns', 'البدء من الأعمدة المحفوظة');
  arms('Check before saving', 'راجِع قبل الحفظ');
  arms('Back to editing', 'العودة إلى التعديل');
  arms('Save anyway', 'حفظ على أي حال');
  arms('Save', 'حفظ');
  arms('Cancel', 'إلغاء');
  assert.match(template, /data-review-label="{% if LANGUAGE_CODE == 'ar' %}حفظ على أي حال{% else %}Save anyway{% endif %}">{% if LANGUAGE_CODE == 'ar' %}حفظ{% else %}Save{% endif %}<\/button>/);
});

test('SD10 a save the user left (Escape while it was out) ends where the user is; a confirmation it then needs is not given; where the edits are is said when no slot dialog shows them', async t => {
  const leave = async (h, reply) => {
    await h.select();
    await slotOpen(h);
    slotType(h, slotInput(h, 'slot_config', 1, 'label'), 'Evening');
    slotSubmit(h);
    await settle();
    keyOn(h, 'Escape');
    assert.equal(slotDialogEl(h).hasAttribute('open'), false, 'Escape is never blocked by a save');
    assert.equal(h.d.activeElement, h.d.getElementById('twMoreMenu'));
    const toasts = h.notices.length;
    reply();
    return toasts;
  };
  const kept = 'Your edits come back when you open “Edit time slots” again.';
  // Saved: a toast; the draft gone.
  let out = deferred();
  const s = await slotHarness(t, { answer: () => out.promise });
  let toasts = await leave(s, () => out.resolve(slotSaved({ slot_config: [...SLOT_COLUMNS.effective_slot_config], lab_slot_config: [...SLOT_COLUMNS.effective_lab_slot_config] })));
  await wait(() => s.notices.length > toasts && atRest(s));
  assert.deepEqual(s.notices.slice(toasts), [{ type: 'success', text: 'Columns saved: 2 lecture, 3 lab.' }]);
  await slotOpen(s);
  assert.equal(slotShown(s, 'twSlotDraft'), false);
  // A failure: a toast that says where the edits are.
  out = deferred();
  const f = await slotHarness(t, { answer: () => out.promise });
  toasts = await leave(f, () => out.resolve({ httpError: 503, error: { code: 'SERVER_BUSY', message: 'Busy' } }));
  await wait(() => f.notices.length > toasts && atRest(f));
  assert.deepEqual(f.notices.slice(toasts), [{ type: 'warning', text: `The columns were not saved. ${kept}` }]);
  // A confirmation needed: not given, nothing more sent; said the same way.
  out = deferred();
  const c = await slotHarness(t, { answer: (body, n) => (n === 1 ? out.promise : slotSaved(body)) });
  toasts = await leave(c, () => out.resolve(slotNeedsConfirm()));
  await wait(() => c.notices.length > toasts && atRest(c));
  await settle();
  assert.deepEqual([c.notices.slice(toasts), c.posts.length, slotShown(c, 'twSlotReview')], [[{ type: 'warning', text: `The columns were not saved. ${kept}` }], 1, false]);
  await slotOpen(c);
  assert.deepEqual([slotShown(c, 'twSlotDraft'), slotInput(c, 'slot_config', 1, 'label').value], [true, 'Evening'], 'the edits came back');
  // A refused field: the server's sentence, nothing marked.
  out = deferred();
  const v = await slotHarness(t, { answer: () => out.promise });
  toasts = await leave(v, () => out.resolve(slotInvalid([{ list: 'slot_config', index: 1, field: 'end', message: 'must be after the start' }])));
  await wait(() => v.notices.length > toasts && atRest(v));
  assert.deepEqual(v.notices.slice(toasts), [{ type: 'warning', text: `The columns were not saved: Lectures, line 2: end must be after the start. ${kept}` }]);
  await slotOpen(v);
  assert.equal(v.d.querySelectorAll('#twSlotDialog [aria-invalid="true"]').length, 0);
  // Nothing changed and nothing kept: no word about edits.
  out = deferred();
  const u = await slotHarness(t, { answer: () => out.promise });
  await u.select();
  await slotOpen(u);
  slotSubmit(u);
  await settle();
  keyOn(u, 'Escape');
  toasts = u.notices.length;
  out.resolve({ httpError: 503, error: { code: 'SERVER_BUSY', message: 'Busy' } });
  await wait(() => u.notices.length > toasts && atRest(u));
  assert.deepEqual(u.notices.slice(toasts), [{ type: 'warning', text: 'The columns were not saved.' }]);
  // Move's dialog open by then: its alert line, no toast.
  out = deferred();
  const m = await slotHarness(t, { answer: () => out.promise, more: url => (url.includes('/slot-candidates/') ? movesAnswer() : undefined) });
  await leave(m, () => {});
  moveByKey(m, 10);
  await movesSettled(m);
  toasts = m.notices.length;
  out.resolve({ httpError: 503, error: { code: 'SERVER_BUSY', message: 'Busy' } });
  await wait(() => moveAlert(m) && atRest(m));
  assert.deepEqual([moveAlert(m), m.d.getElementById('twMoveAlert').dataset.tone, m.notices.slice(toasts)], [`The columns were not saved. ${kept}`, 'warning', []]);
  // The slot dialog open again by then: its own alert, the edits in front of the user.
  out = deferred();
  const r = await slotHarness(t, { answer: (body, n) => (n === 1 ? out.promise : slotSaved(body)) });
  await leave(r, () => {});
  r.w.openSlotDialog();
  assert.equal(slotDialogEl(r).hasAttribute('open'), true);
  assert.deepEqual([slotShown(r, 'twSlotDraft'), slotSaveButton(r).disabled, r.d.getElementById('twSlotNote').textContent], [true, true, 'Wait for the current operation to finish.']);
  toasts = r.notices.length;
  out.resolve({ httpError: 503, error: { code: 'SERVER_BUSY', message: 'Busy' } });
  await wait(() => slotAlert(r) && atRest(r));
  assert.deepEqual([slotAlert(r), r.notices.slice(toasts), slotOut(r), slotSaveButton(r).disabled], ['The columns were not saved.', [], false, false]);
  // Reopened, then refused: the sentence in its alert, and no mark on the
  // rows it shows (they are not the rows that were sent, even when drawn again).
  out = deferred();
  const x = await slotHarness(t, { answer: (body, n) => (n === 1 ? out.promise : slotSaved(body)) });
  await leave(x, () => {});
  x.w.openSlotDialog();
  out.resolve(slotInvalid([{ list: 'slot_config', index: 1, field: 'end', message: 'must be after the start' }]));
  await wait(() => slotAlert(x) && atRest(x));
  x.d.querySelector('[data-slot-add="slot_config"]').click();
  assert.deepEqual([slotAlert(x), x.d.querySelectorAll('#twSlotDialog [aria-invalid="true"]').length,
    [...x.d.querySelectorAll('#twSlotDialog tr[data-error-for]')].every(row => row.hidden)],
  ['The columns were not saved: Lectures, line 2: end must be after the start', 0, true]);
});

test('SD11b a saved column whose label, start or end alone changed elsewhere makes a draft older too', async t => {
  for (const [field, value] of [['label', 'Morning'], ['start', '09:05'], ['end', '10:20']]) {
    let stored = { ...SLOT_COLUMNS };
    const h = await slotHarness(t, { more: url => (url === '/ops/tw/scenarios/1/' ? { scenario: { ...scenario, ...stored }, active_job: null }
      : url === '/ops/tw/boards/2/' ? { placements: [placement], ...stored } : undefined) });
    await h.select();
    await slotOpen(h);
    slotType(h, slotInput(h, 'slot_config', 1, 'label'), 'Evening');
    keyOn(h, 'Escape');
    // Saved elsewhere: only this field of the first column.
    stored = { ...SLOT_COLUMNS, effective_slot_config: SLOT_COLUMNS.effective_slot_config.map((slot, i) => (i ? slot : { ...slot, [field]: value })) };
    await h.w.selectBoard(2);
    await slotOpen(h);
    assert.deepEqual([slotShown(h, 'twSlotDraft'), slotShown(h, 'twSlotDraftChanged')], [true, true], field);
    keyOn(h, 'Escape');
  }
});

test('SD11 a draft older than the saved columns (saved elsewhere, e.g. split view) says so and keeps “Start from the saved columns”; one that now equals them is none', async t => {
  // The scenario's columns as the server has them now: the scenario and the board answer with them.
  let stored = { ...SLOT_COLUMNS };
  const h = await slotHarness(t, { more: url => (url === '/ops/tw/scenarios/1/' ? { scenario: { ...scenario, ...stored }, active_job: null }
    : url === '/ops/tw/boards/2/' ? { placements: [placement], ...stored } : undefined) });
  await h.select();
  await slotOpen(h);
  slotType(h, slotInput(h, 'slot_config', 1, 'label'), 'Evening');
  keyOn(h, 'Escape');
  // Read again with nothing changed: the draft, offered as current.
  await h.w.selectBoard(2);
  await slotOpen(h);
  assert.deepEqual([slotShown(h, 'twSlotDraft'), slotShown(h, 'twSlotDraftChanged'), slotInput(h, 'slot_config', 1, 'label').value], [true, false, 'Evening']);
  keyOn(h, 'Escape');
  // Saved elsewhere: a lecture column more.
  const night = { label: 'Night', start: '18:00', end: '19:15' };
  stored = { ...SLOT_COLUMNS, effective_slot_config: [...SLOT_COLUMNS.effective_slot_config, night] };
  await h.w.selectBoard(2);
  await slotOpen(h);
  assert.deepEqual([slotShown(h, 'twSlotDraft'), slotShown(h, 'twSlotDraftChanged'), slotValues(h, 'slot_config').map(row => row[0])],
    [true, true, ['09:00-10:15', 'Evening']], 'the draft, said to be older than the saved columns');
  assert.match(h.d.getElementById('twSlotDraft').textContent, /Your unsaved columns are back\..*The saved columns changed since this draft\..*Start from the saved columns/);
  // Edited further, it is still that draft: said again on the next open.
  slotType(h, slotInput(h, 'slot_config', 0, 'label'), 'Morning');
  h.d.querySelector('#twSlotDialog .ux-dialog-foot [data-dialog-close]').click();
  await slotOpen(h);
  assert.deepEqual([slotShown(h, 'twSlotDraftChanged'), slotValues(h, 'slot_config').map(row => row[0])], [true, ['Morning', 'Evening']]);
  // Start from the saved columns: the new ones; a draft from them is current.
  h.d.getElementById('twSlotFromSaved').click();
  assert.deepEqual([slotShown(h, 'twSlotDraft'), slotValues(h, 'slot_config').map(row => row[0])], [false, ['09:00-10:15', 'Late', 'Night']]);
  slotType(h, slotInput(h, 'slot_config', 2, 'end'), '19:30');
  keyOn(h, 'Escape');
  await slotOpen(h);
  assert.deepEqual([slotShown(h, 'twSlotDraft'), slotShown(h, 'twSlotDraftChanged'), slotInput(h, 'slot_config', 2, 'end').value], [true, false, '19:30']);
  keyOn(h, 'Escape');
  // A rule the saved columns lost is a change too.
  stored = { ...stored, effective_lab_slot_config: SLOT_COLUMNS.effective_lab_slot_config.map(slot => ({ ...slot, online_only: false })) };
  await h.w.selectBoard(2);
  await slotOpen(h);
  assert.deepEqual([slotShown(h, 'twSlotDraft'), slotShown(h, 'twSlotDraftChanged')], [true, true]);
  keyOn(h, 'Escape');
  // Saved elsewhere as exactly the draft: nothing to offer, and the draft is gone.
  stored = { ...stored, effective_slot_config: [...SLOT_COLUMNS.effective_slot_config, { ...night, end: '19:30' }] };
  await h.w.selectBoard(2);
  await slotOpen(h);
  assert.deepEqual([slotShown(h, 'twSlotDraft'), slotInput(h, 'slot_config', 2, 'end').value], [false, '19:30']);
  keyOn(h, 'Escape');
  await slotOpen(h);
  assert.equal(slotShown(h, 'twSlotDraft'), false);
  assert.deepEqual([h.posts, h.errors], [[], []]);
  // Its words, in both arms of the template.
  assert.ok(template.includes(`<span class="tw-slot-draft-changed" id="twSlotDraftChanged" hidden>{% if LANGUAGE_CODE == 'ar' %}تغيّرت الأعمدة المحفوظة منذ هذه المسودة.{% else %}The saved columns changed since this draft.{% endif %}</span>`));
});

test('SD12 a time typed in Arabic digits shows its length and is saved as 0–9, in English and in Arabic', async t => {
  // Built from their code points: Arabic-Indic U+0660–0669, Eastern Arabic-Indic U+06F0–06F9.
  const arabic = text => text.replace(/[0-9]/g, d => String.fromCharCode(0x0660 + Number(d)));
  const eastern = text => text.replace(/[0-9]/g, d => String.fromCharCode(0x06F0 + Number(d)));
  for (const [lang, start, end, length] of [['en', eastern, eastern, '80 min'], ['ar', arabic, eastern, '80 دقيقةً']]) {
    const h = await slotHarness(t, { arabic: lang === 'ar' });
    await h.select();
    await slotOpen(h);
    slotType(h, slotInput(h, 'slot_config', 0, 'start'), start('09:00'));
    slotType(h, slotInput(h, 'slot_config', 0, 'end'), end('10:20'));
    assert.equal(slotLengths(h, 'slot_config')[0], length, lang);
    assert.deepEqual(slotValues(h, 'slot_config')[0], ['09:00-10:15', start('09:00'), end('10:20')], `${lang}: the field keeps what was typed`);
    slotSubmit(h);
    await wait(() => !slotDialogEl(h).hasAttribute('open') && atRest(h));
    assert.deepEqual(h.posts.map(post => post.body.slot_config[0]), [{ label: '09:00-10:15', start: '09:00', end: '10:20' }], lang);
  }
});
