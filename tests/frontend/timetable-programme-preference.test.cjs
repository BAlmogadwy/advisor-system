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
  w.eval(read('static/js/page-timetable-workspace-split.js') + '\n;globalThis.__splitAudit = { S, DRAWER, RP };');
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

/* ── the drawer ─────────────────────────────────────────────────────── */

const MARKED = { preferred_programme: 'AI2', preferred_programme_editable: true };

async function drawer(t, options = {}, intercept = () => undefined) {
  const h = await harness(t, intercept, { placementExtra: MARKED, ...options });
  if (options.published) h.state.scenarioMeta.status = 'published';
  await h.w.loadPlanLens();
  h.requests.length = 0;
  h.w.openDrawer(0, 11);
  await settle();
  const select = () => h.d.getElementById('twsDrawerProgrammeSelect');
  const block = () => h.d.getElementById('twsDrawerProgramme');
  return { ...h, select, block };
}

test('the drawer offers Any and the lens programmes, the mark selected, and the seated mix', async t => {
  const h = await drawer(t);
  assert.equal(h.requests.length, 0, `opening the drawer sends nothing: ${h.requests.map(r => r.url)}`);
  const options = [...h.select().options].map(option => [option.value, option.textContent]);
  assert.deepEqual(options, [['', 'Any'], ['AI2', 'AI2'], ['DS', 'DS']]);
  assert.equal(h.select().value, 'AI2');
  assert.equal(h.select().disabled, false);
  assert.match(h.block().textContent, /Mainly for programme/);
  const mix = h.d.getElementById('twsDrawerProgrammeMix');
  assert.equal(mix.textContent, '6: AI2 6');
  // One LTR isolate around the figure (it leads with digits), codes isolated inside.
  assert.equal(mix.querySelector(':scope > bdi[dir="ltr"]').textContent, '6: AI2 6');
  assert.deepEqual([...mix.querySelectorAll('bdi:not([dir])')].map(node => node.textContent), ['AI2']);
});

test('changing the mark posts once, then reloads the lens once, and shows the new mix', async t => {
  let lensCalls = 0;
  const h = await drawer(t, {}, (url, options) => {
    if (url.endsWith('/programme-preference/')) return { ok: true, term_section_id: 111, preferred_programme: 'DS', changed: true };
    if (url.endsWith('/plan-lens/') && ++lensCalls > 1) {
      const lens = LENS();
      lens.plan_lens.sections['111'].actual_plans = { AI2: 1, DS: 5 };
      return lens;
    }
    return undefined;
  });
  h.select().value = 'DS';
  h.select().dispatchEvent(new h.w.Event('change', { bubbles: true }));
  await wait(() => h.requests.some(row => row.url.endsWith('/plan-lens/')));
  await wait(() => /DS 5/.test(h.d.getElementById('twsDrawerProgrammeMix')?.textContent || ''));
  const posts = h.requests.filter(row => row.url.endsWith('/programme-preference/'));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/ops/tw/placements/11/programme-preference/');
  assert.equal(posts[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(posts[0].options.body), { preferred_programme: 'DS' });
  for (let i = 0; i < 10; i++) await settle();
  assert.equal(h.requests.filter(row => row.url.endsWith('/plan-lens/')).length, 1);
  assert.equal(h.select().value, 'DS');
  assert.equal(h.state.panes[0].boardData.placements[1].preferred_programme, 'DS');
  assert.ok(h.notices.some(row => row.kind === 'success'));
});

test('an unchanged or refused save sends no lens reload and keeps the stored mark', async t => {
  for (const answer of [
    { ok: true, term_section_id: 111, preferred_programme: 'AI2', changed: false },
    { httpError: 409, ok: false, error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'owned elsewhere' } },
  ]) {
    const h = await drawer(t, {}, url => (url.endsWith('/programme-preference/') ? answer : undefined));
    h.select().value = 'DS';
    h.select().dispatchEvent(new h.w.Event('change', { bubbles: true }));
    await wait(() => h.requests.some(row => row.url.endsWith('/programme-preference/')) && !h.select().disabled);
    for (let i = 0; i < 6; i++) await settle();
    assert.equal(h.requests.filter(row => row.url.endsWith('/plan-lens/')).length, 0);
    assert.equal(h.select().value, 'AI2');
  }
});

test('the mark cannot be changed on a published scenario or a section owned elsewhere', async t => {
  const published = await drawer(t, { published: true });
  assert.equal(published.select().disabled, true);
  const owned = await drawer(t, { placementExtra: { preferred_programme: '', preferred_programme_editable: false } });
  assert.equal(owned.select().disabled, true);
  assert.equal(owned.select().value, '');
});

test('while the lens loads the mix says so; a programme nobody in the course has is warned', async t => {
  const pending = deferred();
  const gate = { reloading: false };
  const h = await drawer(t, { placementExtra: { preferred_programme: 'CYS', preferred_programme_editable: true } }, url => (
    url.endsWith('/plan-lens/') && gate.reloading ? pending.promise : undefined));
  // The current mark is offered even when the lens does not list it.
  assert.deepEqual([...h.select().options].map(option => option.value), ['', 'AI2', 'DS', 'CYS']);
  assert.match(h.block().textContent, /No CYS student needs this course/);
  gate.reloading = true;
  const reload = h.w.loadPlanLens();
  await settle();
  assert.match(h.d.getElementById('twsDrawerProgrammeMix').textContent, /Loading programme mix/);
  pending.resolve(LENS({ programme_grouping: { students_regrouped: 7, student_gap_minutes_change: 150 } }));
  await reload;
  assert.equal(h.d.getElementById('twsDrawerProgrammeMix').textContent, '6: AI2 6');
  assert.match(h.block().textContent, /Grouping moved 7 students; total student waiting \+150 min/);
});

test('in Arabic the drawer and the selection panel speak Arabic', async t => {
  const h = await drawer(t, { arabic: true }, url => (url.endsWith('/plan-lens/')
    ? LENS({ programme_grouping: { students_regrouped: 3, student_gap_minutes_change: -20 } }) : undefined));
  assert.match(h.block().textContent, /مخصّصة أساساً لبرنامج/);
  assert.match(h.block().textContent, /المقاعد/);
  assert.match(h.block().textContent, /نقل التجميع 3 طالباً؛ إجمالي انتظار الطلاب −20 دقيقة/);
  // A signed figure inside Arabic prose is isolated, or it is painted «20−».
  assert.equal(h.block().querySelector('.tws-programme-note bdi[dir="ltr"]').textContent, '−20');
  assert.equal(h.d.getElementById('twsDrawerProgrammeMix').querySelector('bdi[dir="ltr"]').textContent, '6: AI2 6');
  assert.equal(h.select().options[0].textContent, 'أي برنامج');
  h.state.selectedPlacementId = 11; h.state.selectedPaneIdx = 0;
  Object.assign(h.w.__splitAudit.RP, { open: true, tab: 'selection' });
  h.w.renderRpanel();
  assert.match(h.d.body.textContent, /مخصّصة أساساً لبرنامج\s*AI2/);
});

test('without the feature the drawer renders exactly as before', async t => {
  const h = await harness(t);
  await h.w.loadPlanLens();
  h.requests.length = 0;
  h.w.openDrawer(0, 11);
  await settle();
  assert.equal(h.d.getElementById('twsDrawerProgramme'), null);
  assert.equal(h.d.getElementById('twsDrawerProgrammeSelect'), null);
  assert.doesNotMatch(h.d.getElementById('twsDrawerBody').textContent, /Mainly for programme|Seated/);
  assert.equal(h.requests.length, 0);
});
