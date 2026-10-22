const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');

assert.ok(process.env.PR8_TEST_HTML, 'Run through pytest tests/test_pr8_frontend_interactions.py');
const template = fs.readFileSync(process.env.PR8_TEST_HTML, 'utf8');
const adapter = fs.readFileSync(path.join(__dirname, '../../core/static/core/js/pr8_async_job_adapter.js'), 'utf8');
// The real shared dialog, loaded the way a page loads it: a classic script whose
// top-level `const dlg` is a global binding and NOT a property of window.
const dialogSource = fs.readFileSync(path.join(__dirname, '../../static/js/dialog.js'), 'utf8');
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const answer = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

async function page(t, { scenarioId = 1, initial = null, snapshot = null, fetch = () => { throw new Error('Unexpected request'); }, standalone = false, config = {}, sharedDialog = false } = {}) {
  const errors = [];
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(error.message));
  const selector = standalone ? '' : `<select id="twScenario"><option value="">Choose</option><option value="1">AI</option><option value="2">DS</option></select>`;
  const dom = new JSDOM(selector + template, { url: 'http://planner.test/', runScripts: 'outside-only', virtualConsole: console });
  const { window } = dom;
  t.after(() => { window.close(); assert.deepEqual(errors, []); });
  window.document.getElementById('pr8-initial-job').textContent = JSON.stringify(initial);
  if (Object.keys(config).length) {
    const node = window.document.querySelector('[data-pr8-config]');
    node.textContent = JSON.stringify({ ...JSON.parse(node.textContent), ...config });
  }
  if (sharedDialog) {
    // jsdom paints nothing; the dialog only waits a frame to move focus.
    window.requestAnimationFrame = callback => { Promise.resolve().then(() => callback(0)); return 0; };
    vm.runInContext("function esc(value) { return String(value ?? '').replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';'); }", dom.getInternalVMContext());
    vm.runInContext(dialogSource, dom.getInternalVMContext(), { filename: 'dialog.js' });
    assert.equal(window.dlg, undefined, 'like a real page: dlg is a global binding, not window.dlg');
  }
  if (standalone) {
    const config = window.document.querySelector('[data-pr8-config]');
    config.textContent = JSON.stringify({ ...JSON.parse(config.textContent), scenarioId });
  } else {
    window.TW = { scenarioId, plannerJob: snapshot };
    window.document.getElementById('twScenario').value = scenarioId == null ? '' : String(scenarioId);
  }
  const requests = [];
  window.fetch = async (url, options = {}) => {
    const request = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null, options };
    requests.push(request);
    return fetch(request);
  };
  const timers = new Map();
  let next = 0;
  window.setTimeout = (callback, delay) => { const id = ++next; timers.set(id, { callback, delay }); return id; };
  window.clearTimeout = id => timers.delete(id);
  const opened = [];
  window.open = (...args) => { opened.push(args); return null; };
  vm.runInContext(adapter, dom.getInternalVMContext(), { filename: 'pr8_async_job_adapter.js' });
  await settle();
  const card = window.document.querySelector('[data-pr8-card]');
  const button = action => card.querySelector(`[data-pr8-action="${action}"]`);
  const field = key => card.querySelector(`[data-pr8-field="${key}"]`).textContent;
  const emit = (name, detail) => window.dispatchEvent(new window.CustomEvent(name, { detail }));
  const job = (status, id = 'job-1', scenario = 1, extra = {}) => emit('tw:planner-job', { scenarioId: scenario, jobId: id, job: { job_id: id, status, ...extra } });
  const tick = async () => {
    assert.equal(timers.size, 1, 'Exactly one serial poll is scheduled');
    const [id, timer] = timers.entries().next().value;
    assert.equal(timer.delay, 2000);
    timers.delete(id);
    timer.callback();
    await settle();
  };
  return { window, card, button, field, emit, job, tick, timers, requests, opened, message: () => card.querySelector('[data-pr8-message]').textContent };
}

test('Generate job events drive the card and terminal results remain locked during workspace refresh', async t => {
  const p = await page(t, { fetch: () => answer({ job_id: 'generated', status: 'running', last_stage_seen: 'rooming' }) });
  p.emit('tw:planner-busy', { busy: true });
  p.job('queued', 'generated');
  assert.equal(p.button('submit').disabled, true);
  assert.equal(p.button('cancel').hidden, false);
  assert.equal(p.field('scenario'), 'AI (#1)');
  await p.tick();
  assert.equal(p.field('last_stage_seen'), 'rooming');
  p.job('succeeded', 'generated');
  assert.equal(p.timers.size, 0);
  assert.equal(p.button('view-result').hidden, false);
  assert.equal(p.button('submit').disabled, true);
  p.emit('tw:planner-busy', { busy: false });
  assert.equal(p.button('submit').disabled, false);
  p.button('view-result').click();
  assert.deepEqual(p.opened, [['/planner-jobs/generated/result/', '_blank', 'noopener']]);
  p.job('running', 'generated');
  assert.equal(p.button('view-result').hidden, false, 'Late active events cannot undo a terminal state');
});

test('submit locks immediately, targets selected scenario and emits request/job coordination', async t => {
  const post = deferred();
  const p = await page(t, { fetch: () => post.promise });
  const events = [];
  p.window.addEventListener('tw:planner-request', event => events.push(['request', event.detail]));
  p.window.addEventListener('tw:planner-job', event => events.push(['job', event.detail]));
  p.emit('tw:scenario-selected', { scenarioId: 2 });
  p.button('submit').click();
  p.button('submit').click();
  assert.equal(p.requests.length, 1);
  assert.deepEqual(p.requests[0].body, { scenario_id: 2, mode: 'full_rebuild' });
  assert.equal(p.requests[0].options.headers['X-CSRFToken'], 'test-csrf');
  assert.equal(events[0][1].pending, true);
  assert.equal(p.button('submit').disabled, true);
  post.resolve(answer({ job_id: 'ds-job', status: 'queued' }, 201));
  await settle();
  assert.equal(events[1][0], 'job');
  assert.equal(events[1][1].scenarioId, 2);
  assert.equal(events[1][1].source, 'adapter');
  assert.equal(events[2][1].pending, false);
  assert.equal(p.button('submit').disabled, true);
});

test('POST errors display backend details and restore retry controls without inventing success', async t => {
  const p = await page(t, { fetch: () => answer({ error: { message: 'Scenario is published' } }, 409) });
  p.button('submit').click();
  await settle();
  assert.equal(p.message(), 'Scenario is published');
  assert.equal(p.button('submit').disabled, false);
  assert.equal(p.button('cancel').hidden, true);
  assert.equal(p.button('view-result').hidden, true);
});

test('cancellation acknowledgement preserves activity until a confirmed terminal poll', async t => {
  const p = await page(t, { fetch: request => request.method === 'POST'
    ? answer({ job_id: 'job-1', cancel_requested: true }, 202)
    : answer({ job_id: 'job-1', status: 'running', cancel_requested: false }) });
  p.job('running');
  p.emit('tw:planner-busy', { busy: true });
  p.button('cancel').click();
  p.button('cancel').click();
  await settle();
  assert.equal(p.requests.filter(request => request.method === 'POST').length, 1);
  assert.equal(p.button('submit').disabled, true);
  assert.equal(p.button('cancel').disabled, true);
  assert.match(p.button('cancel').textContent, /requested/);
  assert.equal(p.button('view-result').hidden, true);
  await p.tick();
  assert.equal(p.button('cancel').disabled, true, 'Earlier polls cannot undo cancellation acknowledgement');
  p.job('cancelled');
  assert.equal(p.timers.size, 0);
  assert.equal(p.button('cancel').hidden, true);
  assert.equal(p.button('rerun').hidden, false);
  assert.equal(p.button('rerun').disabled, true);
  p.emit('tw:planner-busy', { busy: false });
  assert.equal(p.button('rerun').disabled, false);
});

test('failed cancellation shows the failure and allows another cancellation attempt', async t => {
  const p = await page(t, { fetch: () => answer({ detail: 'Cannot cancel now' }, 503) });
  p.job('running');
  p.button('cancel').click();
  await settle();
  assert.equal(p.message(), 'Cannot cancel now');
  assert.equal(p.button('cancel').disabled, false);
  assert.equal(p.button('submit').disabled, true);
});

test('poll blips and unknown status retain busy state and retry serially', async t => {
  let polls = 0;
  const pending = deferred();
  const p = await page(t, { fetch: () => ++polls === 1 ? answer({ detail: 'Worker restarting' }, 503) : pending.promise });
  p.job('unknown', 'job-1', 1, { error_message: 'Connection lost' });
  assert.equal(p.button('cancel').hidden, false);
  await p.tick();
  assert.match(p.message(), /may still be active/);
  assert.equal(p.button('submit').disabled, true);
  await p.tick();
  assert.equal(p.timers.size, 0, 'A slow poll cannot overlap with another tick');
  p.job('running');
  assert.equal(p.timers.size, 0);
  pending.resolve(answer({ job_id: 'job-1', status: 'failed', error_message: 'Solver failed' }));
  await settle();
  assert.equal(p.timers.size, 0);
  assert.equal(p.field('error_message'), 'Solver failed');
  assert.equal(p.card.querySelector('[data-pr8-error-row]').hidden, false);
  assert.equal(p.button('rerun').hidden, false);
  assert.equal(p.button('submit').disabled, false);
});

test('switching scenarios clears stale fields and ignores an in-flight former scenario poll', async t => {
  const pending = deferred();
  const p = await page(t, { fetch: () => pending.promise });
  p.job('running', 'ai-job', 1, { submitted_at: 'yesterday', last_stage_seen: 'greedy' });
  await p.tick();
  p.emit('tw:scenario-selected', { scenarioId: 2, job: null });
  assert.equal(p.field('scenario'), 'DS (#2)');
  assert.equal(p.field('submitted_at'), '-');
  assert.equal(p.field('last_stage_seen'), '-');
  assert.equal(p.button('cancel').hidden, true);
  assert.equal(p.button('submit').disabled, false);
  pending.resolve(answer({ job_id: 'ai-job', status: 'succeeded' }));
  await settle();
  assert.equal(p.button('view-result').hidden, true);
  assert.equal(p.field('scenario'), 'DS (#2)');
  p.job('running', 'ai-job', 1);
  assert.equal(p.button('cancel').hidden, true, 'Other scenario events cannot repaint the selected card');
  p.emit('tw:scenario-selected', { scenarioId: 1 });
  assert.equal(p.button('cancel').hidden, false, 'Returning to a known active run resumes its status');
});

test('initial server job and shared workspace snapshot survive initialization', async t => {
  const p = await page(t, { initial: { job_id: 'initial', status: 'running', submitted_at: '2026-10-10' }, standalone: true });
  assert.equal(p.card.querySelector('[data-pr8-status]').textContent, 'Running');
  assert.equal(p.field('submitted_at'), '2026-10-10');
  assert.equal(p.button('cancel').hidden, false);
  assert.equal(p.timers.size, 1);
  const q = await page(t, { snapshot: { scenarioId: 1, jobId: 'shared', job: { status: 'succeeded' } } });
  assert.equal(q.button('view-result').hidden, false);
  q.button('view-result').click();
  assert.equal(q.opened[0][0], '/planner-jobs/shared/result/');
});

test('external POST and non-planner busy locks prevent parallel runs; no selection is disabled', async t => {
  const p = await page(t, { scenarioId: null });
  assert.equal(p.button('submit').disabled, true);
  const select = p.window.document.getElementById('twScenario');
  select.value = '2';
  select.dispatchEvent(new p.window.Event('change', { bubbles: true }));
  assert.equal(p.button('submit').disabled, false);
  p.emit('tw:planner-request', { scenarioId: 2, pending: true });
  assert.equal(p.button('submit').disabled, true);
  p.emit('tw:planner-request', { scenarioId: 2, pending: false });
  p.emit('tw:planner-busy', { busy: true });
  assert.equal(p.button('submit').disabled, true);
  p.job('failed', 'ds-job', 2);
  assert.equal(p.button('rerun').disabled, true);
  assert.equal(p.requests.length, 0);
  p.emit('tw:planner-busy', { busy: false });
  assert.equal(p.button('rerun').disabled, false);
});

test('rerun submits once and preserves previous failure if the new POST is rejected', async t => {
  const pending = deferred();
  const p = await page(t, { fetch: () => pending.promise });
  p.job('failed', 'old-job', 1, { error_message: 'Previous solver failure' });
  p.button('rerun').click();
  p.button('rerun').click();
  assert.equal(p.requests.length, 1);
  assert.equal(p.button('rerun').disabled, true);
  pending.resolve(answer({ detail: 'Not submitted' }, 400));
  await settle();
  assert.equal(p.field('error_message'), 'Previous solver failure');
  assert.equal(p.message(), 'Not submitted');
  assert.equal(p.button('rerun').disabled, false);
});

test('published scenarios cannot start a background run but an existing run can be cancelled', async t => {
  const p = await page(t);
  p.window.TW.scenario = { status: 'published' };
  p.emit('tw:planner-busy', { busy: false });
  assert.equal(p.button('submit').disabled, true);
  p.button('submit').disabled = false;
  p.button('submit').click();
  assert.equal(p.requests.length, 0, 'The action guard also protects a programmatic click');
  p.job('running');
  assert.equal(p.button('cancel').disabled, false);
});

test('a late event from an older job cannot replace a newer active run', async t => {
  const p = await page(t);
  p.job('succeeded', 'old-job');
  p.job('queued', 'new-job');
  p.job('succeeded', 'old-job');
  assert.equal(p.button('cancel').hidden, false);
  assert.equal(p.button('view-result').hidden, true);
  assert.equal(p.card.dataset.jobId, 'new-job');
  assert.equal(p.timers.size, 1, 'The newer run must remain monitored');
});

test('authoritative selected scenario snapshot can replace a different cached active job', async t => {
  const p = await page(t);
  p.job('unknown', 'old-job');
  p.emit('tw:scenario-selected', { scenarioId: 1, job: { job_id: 'authoritative-job', status: 'running' } });
  assert.equal(p.card.dataset.jobId, 'authoritative-job');
  assert.equal(p.button('cancel').hidden, false);
  assert.equal(p.timers.size, 1);
});

test('server busy response adopts the existing run from another browser and retains cancellation', async t => {
  const p = await page(t, { fetch: () => answer({ error: { code: 'SCENARIO_BUSY', message: 'A planner is already running',
    details: { scenario_id: 1, active_job: { job_id: 'other-browser', scenario_id: 1, status: 'running' } } } }, 409) });
  const jobs = [];
  p.window.addEventListener('tw:planner-job', event => jobs.push(event.detail));
  p.button('submit').click(); await settle();
  assert.equal(p.card.dataset.jobId, 'other-browser');
  assert.equal(p.button('submit').disabled, true);
  assert.equal(p.button('cancel').hidden, false);
  assert.equal(p.button('cancel').disabled, false);
  assert.equal(p.message(), 'A planner is already running');
  assert.equal(p.timers.size, 1);
  assert.equal(jobs[0].jobId, 'other-browser');
});

test('the rebuild asks first through the real shared dialog, and sends nothing until confirmed', async t => {
  const pending = deferred();
  const p = await page(t, { fetch: () => pending.promise, sharedDialog: true });
  p.button('submit').click(); await settle();
  const dialog = p.window.document.querySelector('.dlg-backdrop');
  assert.ok(dialog, 'the shared dialog opened');
  assert.match(dialog.textContent, /Every unlocked placement/);
  assert.equal(p.requests.length, 0, 'nothing is sent before the user agrees');
  dialog.querySelector('.btn-confirm').click(); await settle();
  assert.equal(p.requests.length, 1);
  assert.deepEqual(p.requests[0].body, { scenario_id: 1, mode: 'full_rebuild' });
  pending.resolve(answer({ job_id: 'confirmed', status: 'queued' }, 201)); await settle();
});

test('declining the shared dialog sends nothing', async t => {
  const p = await page(t, { sharedDialog: true });
  p.button('submit').click(); await settle();
  p.window.document.querySelector('.dlg-backdrop .btn-cancel').click(); await settle();
  assert.equal(p.requests.length, 0);
});

test('a monitor-only card never offers the legacy rebuild, not even after a failure', async t => {
  const p = await page(t, { config: { offerRebuild: false } });
  assert.equal(p.button('submit').hidden, true);
  p.job('failed', 'failed-job', 1, { error_message: 'Solver failed' });
  assert.equal(p.button('rerun').hidden, true);
  assert.equal(p.button('submit').hidden, true);
});

test('the card stays out of the way while idle, shows a run, and closes when dismissed', async t => {
  const p = await page(t);
  assert.equal(p.card.hidden, true, 'idle: hidden');
  p.job('running', 'run-1');
  assert.equal(p.card.hidden, false, 'running: shown');
  const dismiss = p.card.querySelector('[data-pr8-dismiss]');
  assert.equal(dismiss.hidden, true, 'a running job cannot be dismissed');
  assert.equal(p.button('cancel').hidden, false, 'its Cancel is there');
  p.job('succeeded', 'run-1');
  assert.equal(p.card.hidden, false, 'finished: still shown');
  assert.equal(dismiss.hidden, false);
  dismiss.click();
  assert.equal(p.card.hidden, true, 'dismissed: hidden');
  p.job('running', 'run-2');
  assert.equal(p.card.hidden, false, 'the next run shows again');
});
