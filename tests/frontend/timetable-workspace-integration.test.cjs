/* Actual main script, planner adapter and accessibility layer in one DOM.
 * Requests are scripted; this suite never connects to a server or database. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const strip = html => html.replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
const source = read('static/js/page-timetable-workspace.js');
const adapter = read('core/static/core/js/pr8_async_job_adapter.js');
const accessibility = read('static/js/ux-controls.js');
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const wait = async condition => { for (let i = 0; i < 100; i++) { if (condition()) return; await settle(); } assert.fail('UI did not settle'); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const scenario = id => ({ id, name: `Scenario ${id}`, status: 'draft', slot_config: [{ label: '09:00', start: '09:00', end: '10:15' }] });
const board = { id: 2, label: 'Term 7', nominal_term: 7, primary_count: 1 };
const placement = { id: 10, course_code: 'CS323', section: 'S1', term_section_id: 5, day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'LAB25', meetings: [], available_capacity: 24 };
async function harness(t, intercept = () => undefined) {
  const main = strip(read('core/templates/core/timetable_workspace.html').replace(/<script[\s\S]*?<\/script>/g, ''));
  const partial = strip(read('core/templates/core/partials/pr8_async_job_card.html')).replace(/<script src=[\s\S]*?<\/script>/g, '');
  const dom = new JSDOM(main + partial, { runScripts: 'outside-only', url: 'http://workspace.test/' });
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [];
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  d.querySelector('[data-pr8-config]').textContent = JSON.stringify({ submitUrl: '/planner-jobs/', pollUrl: '/planner-jobs/{job_id}/',
    resultUrl: '/planner-jobs/{job_id}/result/', cancelUrl: '/planner-jobs/{job_id}/cancel/', pollIntervalMs: 2000, csrfToken: 'test', mode: 'full_rebuild' });
  w.eval("const LANGUAGE_CODE = 'en';");
  w._injectCsrf = value => value;
  w.esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  w.debounce = callback => callback;
  w.notify = Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => notices.push({ type, text })]));
  w.dlg = { confirm: async () => false };
  w.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    let body = await intercept(url, options, w);
    if (body === undefined) {
      if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [scenario(1), scenario(2)] };
      else if (/\/ops\/tw\/scenarios\/[12]\/$/.test(url)) body = { scenario: scenario(Number(url.match(/\/(\d+)\/$/)[1])), active_job: null };
      else if (url.includes('/quality/')) body = { ok: true };
      else if (url.includes('/plan-lens/')) body = { plans: [], courses: {}, sections: {} };
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
      else if (/\/boards\/\d+\/$/.test(url)) body = { placements: [placement], slot_config: scenario(1).slot_config };
      else if (url.includes('/conflicts/')) body = { summary: { critical: 0 } };
      else if (url.includes('/capacity/')) body = { courses: [], totals: {} };
      else if (url.includes('/budget/')) body = { budget: [] };
      else if (url.includes('/unplaced/')) body = { unplaced: [{ term_section_id: 42, course_code: 'AI111', section: 'S2', meetings: [] }] };
      else assert.fail(`Unexpected request ${url}`);
    }
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  d.getElementById('twYear').value = '1448'; d.getElementById('twTerm').value = '1';
  w.eval(source); w.eval(accessibility); w.eval(adapter);
  await wait(() => d.getElementById('twScenario').options.length === 3);
  const select = async id => { d.getElementById('twScenario').value = String(id); await w.onScenarioChange(); };
  const button = action => d.querySelector(`[data-pr8-action="${action}"]`);
  const emit = (name, detail) => w.dispatchEvent(new w.CustomEvent(name, { detail }));
  return { w, d, requests, notices, select, button, emit };
}

test('adapter submission owns all workspace mutations before the job ID arrives', async t => {
  const pending = deferred();
  const h = await harness(t, url => url === '/planner-jobs/' ? pending.promise : undefined);
  await h.select(1);
  // "Run in background" replaces unlocked placements, so the card asks first.
  const asked = [];
  h.w.dlg.confirm = async options => { asked.push(options.title); return true; };
  h.button('submit').click(); h.button('submit').click(); await settle();
  assert.equal(asked.length, 2);
  assert.equal(h.requests.filter(request => request.url === '/planner-jobs/').length, 1);
  for (const id of ['twGenerate', 'twOptimise', 'twPublish', 'twScenario']) assert.equal(h.d.getElementById(id).disabled, true, id);
  pending.resolve({ job_id: 'card-job', status: 'queued' }); await settle();
  assert.equal(h.w.TW.plannerJob.job_id, 'card-job');
  assert.equal(h.button('cancel').hidden, false);
  assert.equal(h.d.getElementById('twGenerate').disabled, true);
});

test('adapter terminal event keeps both controls locked through board refresh and restores after load', async t => {
  const detail = deferred(); let refreshing = false;
  const h = await harness(t, url => refreshing && url === '/ops/tw/scenarios/1/' ? detail.promise : undefined);
  await h.select(1);
  h.emit('tw:planner-job', { scenarioId: 1, source: 'workspace', job: { job_id: 'job-1', status: 'running' } });
  refreshing = true;
  h.emit('tw:planner-job', { scenarioId: 1, source: 'adapter', job: { job_id: 'job-1', status: 'cancelled' } });
  assert.equal(h.button('rerun').hidden, false);
  assert.equal(h.button('rerun').disabled, true);
  assert.equal(h.d.getElementById('twOptimise').disabled, true);
  detail.resolve({ scenario: scenario(1), active_job: null });
  await wait(() => !h.d.getElementById('twOptimise').disabled);
  assert.equal(h.button('rerun').disabled, false);
  assert.equal(h.d.querySelector('.tw-card').draggable, true);
});

test('loading a different scenario must guard both old-board edits and adapter submission', async t => {
  const detail = deferred();
  const h = await harness(t, url => url === '/ops/tw/scenarios/2/' ? detail.promise : undefined);
  await h.select(1);
  const selector = h.d.getElementById('twScenario');
  selector.value = '2';
  selector.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  await settle();
  assert.equal(h.d.getElementById('twOptimise').disabled, true, 'Previous scenario must not stay editable during selection');
  assert.equal(h.button('submit').disabled, true, 'Card must wait for authoritative selected scenario');
  detail.resolve({ scenario: scenario(2), active_job: null });
  await wait(() => h.w.TW.scenarioId === 2 && !h.d.getElementById('twOptimise').disabled);
  assert.equal(h.button('submit').disabled, false);
});

test('declining the rebuild confirmation submits nothing', async t => {
  const h = await harness(t); await h.select(1);
  h.w.dlg.confirm = async () => false;
  h.button('submit').click(); await settle();
  assert.equal(h.requests.filter(request => request.url === '/planner-jobs/').length, 0);
  assert.equal(h.d.getElementById('twGenerate').disabled, false);
});

test('menus, inspector tabs and read-only selection remain usable while a planner owns the board', async t => {
  const h = await harness(t); await h.select(1);
  h.emit('tw:planner-job', { scenarioId: 1, source: 'workspace', job: { job_id: 'job-1', status: 'running' } });
  // The course-demand table (formerly the bottom panel) is an inspector tab.
  h.d.getElementById('twTabDemand').click();
  assert.equal(h.d.getElementById('twTabDemand').getAttribute('aria-selected'), 'true');
  assert.equal(h.d.getElementById('twRightBody').getAttribute('aria-labelledby'), 'twTabDemand');
  h.d.getElementById('twTabIssues').click();
  const menu = h.d.getElementById('twExportMenu');
  menu.dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
  assert.equal(menu.getAttribute('aria-expanded'), 'true');
  assert.equal(h.d.activeElement.getAttribute('role'), 'menuitem');
  h.d.activeElement.dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(menu.getAttribute('aria-expanded'), 'false');
  h.d.querySelector('.tw-card').click();
  assert.equal(h.w.TW.selectedPlacementId, 10);
  assert.equal(h.d.querySelector('[data-tw-mutation]').disabled, true);
});

test('successful create and remove history retains recreation data through repeated cycles', async t => {
  let id = 20;
  const h = await harness(t, (url, options) => options.method === 'POST'
    ? url === '/ops/tw/placements/create/' ? { ok: true, placement: { id: ++id } } : { ok: true }
    : undefined);
  await h.select(1);
  const writes = () => h.requests.filter(request => request.options.method === 'POST');
  const action = { type: 'create', placement_id: 10, board_id: 2, term_section_id: 5, day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'LAB25' };
  h.w.TW.undoStack.push(action);
  await h.w.runHistoryAction(true); await h.w.runHistoryAction(false); await h.w.runHistoryAction(true);
  assert.match(writes()[0].url, /placements\/10\/remove/);
  assert.equal(writes()[1].url, '/ops/tw/placements/create/');
  assert.deepEqual(JSON.parse(writes()[1].options.body), { board_id: 2, term_section_id: 5, day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'LAB25' });
  assert.match(writes()[2].url, /placements\/21\/remove/);
  h.w.TW.undoStack = [{ ...action, type: 'remove' }]; h.w.TW.redoStack = [];
  await h.w.runHistoryAction(true); await h.w.runHistoryAction(false); await h.w.runHistoryAction(true);
  assert.equal(writes()[3].url, '/ops/tw/placements/create/');
  assert.match(writes()[4].url, /placements\/22\/remove/);
  assert.equal(writes()[5].url, '/ops/tw/placements/create/');
  assert.equal(JSON.parse(writes()[5].options.body).term_section_id, 5);
});

test('successful move Undo and Redo use original and destination times respectively', async t => {
  const h = await harness(t, (_url, options) => options.method === 'POST' ? { ok: true } : undefined);
  await h.select(1);
  h.w.TW.undoStack.push({ type: 'move', placement_id: 10, old_day: 'SUN', old_start: '09:00', old_end: '10:15', new_day: 'MON', new_start: '13:00', new_end: '14:15' });
  await h.w.runHistoryAction(true); await h.w.runHistoryAction(false);
  const bodies = h.requests.filter(request => request.options.method === 'POST').map(request => JSON.parse(request.options.body));
  assert.deepEqual(bodies, [{ day: 'SUN', start_time: '09:00', end_time: '10:15' }, { day: 'MON', start_time: '13:00', end_time: '14:15' }]);
});

test('board loading guards old grid edits and restores previous board on failed selection', async t => {
  const pending = deferred();
  const h = await harness(t, url => url === '/ops/tw/boards/3/' ? pending.promise : undefined);
  await h.select(1);
  const loading = h.w.selectBoard(3);
  assert.equal(h.d.querySelector('.tw-card').draggable, false);
  assert.equal(h.button('submit').disabled, true);
  pending.resolve({ httpError: 503, error: { message: 'Board unavailable' } });
  await loading;
  assert.equal(h.w.TW.boardId, 2);
  assert.equal(h.d.querySelector('.tw-card').draggable, true);
  assert.equal(h.button('submit').disabled, false);
});

test('saved manual boards show and search eligible existing sections with guarded drag actions', async t => {
  const h = await harness(t); await h.select(1);
  assert.match(h.d.getElementById('twSectionList').textContent, /AI111.*S2/);
  assert.equal(h.d.querySelector('.tw-section-item').draggable, true);
  const search = h.d.getElementById('twSectionSearch');
  search.value = 'NOCOURSE'; search.dispatchEvent(new h.w.Event('input', { bubbles: true }));
  assert.equal(h.d.querySelectorAll('.tw-section-item').length, 0);
  search.value = 'AI111'; search.dispatchEvent(new h.w.Event('input', { bubbles: true }));
  assert.equal(h.d.querySelector('.tw-section-item').draggable, true);
  h.emit('tw:planner-job', { scenarioId: 1, source: 'workspace', job: { job_id: 'job-1', status: 'running' } });
  assert.equal(h.d.querySelector('.tw-section-item').draggable, false);
});

test('late old-job events cannot split root and card ownership of a newer active job', async t => {
  const h = await harness(t); await h.select(1);
  const publish = (id, status) => h.emit('tw:planner-job', { scenarioId: 1, source: 'workspace', job: { job_id: id, status } });
  publish('old-job', 'succeeded'); publish('new-job', 'queued'); publish('old-job', 'succeeded');
  assert.equal(h.w.TW.plannerJob.job_id, 'new-job');
  assert.equal(h.d.querySelector('[data-pr8-card]').dataset.jobId, 'new-job');
  assert.equal(h.button('cancel').hidden, false);
  assert.equal(h.button('view-result').hidden, true);
  assert.equal(h.d.getElementById('twOptimise').disabled, true);
});

test('server busy response from another browser restores shared monitoring without retrying the write', async t => {
  const h = await harness(t, url => url === '/planner-jobs/' ? { httpError: 409, error: { code: 'SCENARIO_BUSY',
    message: 'A planner is already running', details: { scenario_id: 1, active_job: { job_id: 'remote-job', scenario_id: 1, status: 'running' } } } } : undefined);
  h.w.dlg.confirm = async () => true;
  await h.select(1); h.button('submit').click(); await settle();
  assert.equal(h.w.TW.plannerJob.job_id, 'remote-job');
  assert.equal(h.d.querySelector('[data-pr8-card]').dataset.jobId, 'remote-job');
  assert.equal(h.button('cancel').hidden, false);
  assert.equal(h.button('cancel').disabled, false);
  assert.equal(h.d.getElementById('twGenerate').disabled, true);
  assert.equal(h.requests.filter(request => request.url === '/planner-jobs/').length, 1);
});
