/* "Mainly for programme" in the split workspace. The plan lens is a full
 * seating evaluation on the server, so it rides the preview lane: a busy answer
 * is retried after Retry-After, a superseded or failed answer never clears a
 * lens that is already loaded, and neither a scenario load nor a write waits
 * for it. Full split script and template; controlled HTTP; no server. */
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
const BUSY = { httpError: 503, retryAfter: '3', code: 'SERVER_BUSY', ok: false, error: { code: 'SERVER_BUSY', message: 'busy' } };

const LENS = (extra = {}) => ({
  plan_lens: {
    plans: ['AI2', 'DS'],
    courses: { AI236: { course_key: 'AI236', plans: { AI2: 6, DS: 6 } } },
    sections: {
      110: { term_section_id: 110, course_key: 'AI236', section: 'S1', owner: 'SHARED', actual_plans: { AI2: 0, DS: 6 }, actual_total: 6 },
      111: { term_section_id: 111, course_key: 'AI236', section: 'S2', owner: 'SHARED', actual_plans: { AI2: 6, DS: 0 }, actual_total: 6 },
    },
    ...extra,
  },
});

async function harness(t, intercept = () => undefined, { arabic = false, placementExtra = {} } = {}) {
  const html = read('core/templates/core/timetable_workspace_split.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://split.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [], delays = [];
  const placement = (id, section) => ({
    id, board_id: 2, term_section_id: id + 100, course_code: 'AI236', course_key: 'AI236', course_name: 'Course',
    section, day: id === 10 ? 'SUN' : 'MON', start_time: '09:00', end_time: '10:15', room: 'R1', is_locked: false,
    meetings: [], teacher: null, ...placementExtra,
  });
  const board = { id: 2, label: 'Term 2', nominal_term: 2, primary_count: 12, placement_count: 2 };
  const boardData = () => ({ board, placements: [placement(10, 'S1'), placement(11, 'S2')], slot_config: [{ start: '09:00', end: '10:15' }] });
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  Object.assign(w, { LANGUAGE_CODE: arabic ? 'ar' : 'en', djCsrfToken: 'audit', INITIAL_SCENARIO: '', INITIAL_BOARD: '', DEFAULT_YEAR: '1448', DEFAULT_TERM: '2' });
  w.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
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
      if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
      else if (url === '/ops/tw/scenarios/1/') body = { ok: true, scenario: { id: 1, name: 'Scn', status: 'draft' } };
      else if (url === '/ops/tw/boards/2/') body = boardData();
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
      else if (url.endsWith('/conflicts/')) body = { overlaps: [], instructor_clashes: [], room_clashes: [] };
      else if (url.endsWith('/plan-lens/')) body = LENS();
      else body = { ok: true, candidates: [], instructors: [], summary: {} };
    }
    const headers = { get: name => (name.toLowerCase() === 'retry-after' ? body.retryAfter || null : null) };
    return { ok: !body.httpError, status: body.httpError || 200, headers, json: async () => body };
  };
  w.eval(read('static/js/timetable-meeting-policy.js'));
  w.eval(read('static/js/page-timetable-workspace-split.js') + '\n;globalThis.__splitAudit = { S, DRAWER };');
  await settle();
  const { S: state } = w.__splitAudit;
  state.scenarioId = 1; state.scenarioMeta = { id: 1, status: 'draft' }; state.boards = [board];
  state.cols = 1; state.rows = 1;
  state.panes[0].boardId = 2; state.panes[0].boardData = boardData();
  w.renderPane(0);
  requests.length = 0;
  const lensRequests = () => requests.filter(row => row.url.endsWith('/plan-lens/'));
  return { w, d, state, requests, notices, delays, lensRequests };
}

test('the plan lens rides the preview lane: a busy answer is retried after Retry-After', async t => {
  let calls = 0;
  const h = await harness(t, url => (url.endsWith('/plan-lens/') && calls++ === 0 ? BUSY : undefined));
  await h.w.loadPlanLens();
  assert.equal(h.lensRequests().length, 2);
  assert.ok(h.delays.includes(3000), `Retry-After 3 s should be honoured: ${h.delays}`);
  assert.equal(h.state.planLens.loaded, true);
  assert.deepEqual(h.state.planLens.sections['111'].actual_plans, { AI2: 6, DS: 0 });
  assert.deepEqual(h.notices.filter(row => row.kind === 'error'), []);
});

test('a failed or superseded lens answer never clears a lens that is already loaded', async t => {
  let fail = false;
  const late = deferred();
  const h = await harness(t, url => {
    if (!url.endsWith('/plan-lens/')) return undefined;
    if (fail === 'late') return late.promise;
    return fail ? { httpError: 500, error: { message: 'boom' } } : undefined;
  });
  await h.w.loadPlanLens();
  assert.equal(h.state.planLens.loaded, true);
  fail = true;
  await h.w.loadPlanLens();
  assert.equal(h.state.planLens.loaded, true);
  assert.deepEqual(Object.keys(h.state.planLens.sections), ['110', '111']);
  // An older answer arriving after a newer request is dropped.
  fail = 'late';
  const older = h.w.loadPlanLens();
  fail = false;
  await h.w.loadPlanLens();
  late.resolve({ plan_lens: { plans: [], courses: {}, sections: {} } });
  await older;
  assert.deepEqual(Object.keys(h.state.planLens.sections), ['110', '111']);
});

test('a lens for a scenario the user has left is dropped', async t => {
  const late = deferred();
  const h = await harness(t, url => (url.endsWith('/plan-lens/') ? late.promise : undefined));
  const pending = h.w.loadPlanLens();
  await settle();
  h.w.resetPlanLens();
  h.state.scenarioId = 2;
  late.resolve(LENS());
  await pending;
  assert.equal(h.state.planLens.loaded, false);
  assert.equal(Object.keys(h.state.planLens.sections).length, 0);
});

test('a scenario load never waits for the lens; the panes repaint when it arrives', async t => {
  const lens = deferred();
  const h = await harness(t, url => (url.endsWith('/plan-lens/') ? lens.promise : undefined));
  const select = h.d.getElementById('twsScenario');
  const option = h.d.createElement('option');
  option.value = '1'; option.textContent = 'Scn';
  select.appendChild(option);
  select.value = '1';
  const stillWaiting = (async () => { for (let i = 0; i < 400; i++) await settle(); return 'still waiting'; })();
  const loaded = await Promise.race([h.w.onScenarioChange(), stillWaiting]);
  assert.equal(loaded, true, 'the boards loaded while the lens was still out');
  assert.ok(h.requests.some(row => row.url === '/ops/tw/boards/2/'));
  assert.equal(h.state.planLens.loaded, false);
  assert.equal(h.d.querySelectorAll('.plan-badges').length, 0);
  lens.resolve(LENS());
  await wait(() => h.state.planLens.loaded);
  await wait(() => h.d.querySelectorAll('.plan-badges').length > 0);
});
