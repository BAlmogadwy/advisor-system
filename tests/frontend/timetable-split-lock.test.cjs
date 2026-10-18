/* Full split script and template: real drawer/inspector/keyboard handlers,
 * controlled HTTP responses, and no connection to an application database. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const source = process.env.TIMETABLE_SPLIT_LOCK_SOURCE
  ? fs.readFileSync(process.env.TIMETABLE_SPLIT_LOCK_SOURCE, 'utf8')
  : read('static/js/page-timetable-workspace-split.js');
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };
const wait = async condition => { for (let i = 0; i < 100; i++) { if (condition()) return; await settle(); } assert.fail('Split UI did not settle'); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

async function harness(t, intercept = () => undefined) {
  const html = read('core/templates/core/timetable_workspace_split.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://split.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [];
  const locks = new Map([[10, false], [11, false]]);
  const placement = id => ({ id, term_section_id: id + 100, course_code: `AI${id}`, course_key: `AI${id}`, course_name: 'Course', section: 'S1', day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'R1', is_locked: locks.get(id), meetings: [] });
  const board = { id: 2, label: 'Board', nominal_term: 1, primary_count: 1, placement_count: 2 };
  const boardData = () => ({ board, placements: [placement(10), placement(11)], slot_config: [{ start: '09:00', end: '10:15' }] });
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  Object.assign(w, { LANGUAGE_CODE: 'en', djCsrfToken: 'audit', INITIAL_SCENARIO: '', INITIAL_BOARD: '', DEFAULT_YEAR: '1448', DEFAULT_TERM: '1' });
  w.esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  w.notify = Object.fromEntries(['success', 'warning', 'error'].map(kind => [kind, message => notices.push({ kind, message })]));
  w.console.error = () => {};
  w.matchMedia = () => ({ matches: false });
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    let body = await intercept(url, options, { w, d, setLocked: (value, id = 10) => { locks.set(id, value); }, boardData });
    if (body === undefined) {
      if (url.endsWith('/lock/')) {
        const id = Number(url.match(/placements\/(\d+)/)[1]);
        locks.set(id, !locks.get(id));
        body = { ok: true, placement_id: id, is_locked: locks.get(id) };
      }
      else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
      else if (url === '/ops/tw/boards/2/') body = boardData();
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
      else if (url.endsWith('/conflicts/')) body = { overlaps: [], instructor_clashes: [], room_clashes: [] };
      else if (url.endsWith('/plan-lens/')) body = { plan_lens: { plans: [], courses: {}, sections: {} } };
      else body = { ok: true, candidates: [], instructors: [], summary: {} };
    }
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  w.eval(read('static/js/timetable-meeting-policy.js'));
  w.eval(source + '\n;globalThis.__splitAudit = { S, RP, DRAWER };');
  await settle();
  const { S: state, RP: panel } = w.__splitAudit;
  state.scenarioId = 1; state.scenarioMeta = { id: 1, status: 'draft' }; state.boards = [board];
  state.cols = 2; state.rows = 1;
  for (const pane of state.panes.slice(0, 2)) { pane.boardId = 2; pane.boardData = boardData(); }
  state.selectedPlacementId = 10; state.selectedPaneIdx = 0;
  panel.open = true; panel.tab = 'selection';
  w.renderPane(0); w.renderPane(1); w.renderRpanel(); w.openDrawer(0, 10);
  await settle();
  const click = (node, detail = 1) => node.dispatchEvent(new w.MouseEvent('click', { bubbles: true, detail }));
  const key = (repeat = false) => d.dispatchEvent(new w.KeyboardEvent('keydown', { bubbles: true, key: 'l', repeat }));
  const lockRequests = () => requests.filter(request => request.url.endsWith('/lock/'));
  const inspector = () => d.querySelector('[data-protect="section"]');
  return { w, d, state, panel, notices, requests, click, key, lockRequests, inspector, getLocked: (id = 10) => locks.get(id) };
}

test('drawer, inspector and repeated keyboard activation share one pending lock write', async t => {
  const save = deferred();
  const h = await harness(t, (url, options, ctx) => url.endsWith('/lock/') ? save.promise.then(() => { ctx.setLocked(true); return { ok: true, placement_id: 10, is_locked: true }; }) : undefined);
  h.click(h.d.getElementById('twsDrawerLock'));
  h.click(h.d.getElementById('twsDrawerLock'), 2);
  h.click(h.inspector()); h.key(); h.key(true);
  assert.equal(h.lockRequests().length, 1);
  assert.equal(h.d.getElementById('twsDrawerLock').disabled, true);
  assert.equal(h.inspector().disabled, true);
  save.resolve();
  await wait(() => !h.d.getElementById('twsDrawerLock').disabled && h.getLocked());
  assert.equal(h.notices.filter(row => row.kind === 'success').length, 1);
  assert.match(h.d.getElementById('twsDrawerLock').textContent, /Unlock/);
});

for (const surface of ['drawer', 'inspector']) test(`${surface}: a completed fast first click ignores the gesture's second click and permits a later unlock`, async t => {
  const h = await harness(t);
  const button = () => surface === 'drawer' ? h.d.getElementById('twsDrawerLock') : h.inspector();
  h.click(button());
  await wait(() => h.getLocked() && !h.d.getElementById('twsDrawerLock').disabled);
  await settle();
  h.click(button(), 2);
  await settle();
  assert.equal(h.lockRequests().length, 1);
  assert.equal(h.getLocked(), true);
  h.click(button(), 1);
  await wait(() => !h.getLocked() && !h.d.getElementById('twsDrawerLock').disabled);
  assert.equal(h.lockRequests().length, 2);
  assert.deepEqual(h.notices.filter(row => row.kind === 'success').map(row => row.message), ['Locked', 'Unlocked']);
});

test('ownership and newly rendered controls remain pending throughout multi-pane reload', async t => {
  const reload = deferred(); let saved = false;
  const h = await harness(t, (url, options, ctx) => {
    if (url.endsWith('/lock/')) { saved = true; ctx.setLocked(true); return { ok: true, placement_id: 10, is_locked: true }; }
    if (saved && url === '/ops/tw/boards/2/') return reload.promise.then(ctx.boardData);
  });
  h.click(h.d.getElementById('twsDrawerLock'));
  await wait(() => saved && h.requests.some(row => row.url === '/ops/tw/boards/2/'));
  h.w.renderRpanel(); h.w.openDrawer(1, 10);
  assert.equal(h.inspector().disabled, true);
  assert.equal(h.d.getElementById('twsDrawerLock').getAttribute('aria-busy'), 'true');
  h.click(h.inspector()); h.key();
  assert.equal(h.lockRequests().length, 1);
  reload.resolve();
  await wait(() => !h.d.getElementById('twsDrawerLock').disabled);
  for (const pane of h.state.panes.slice(0, 2)) assert.equal(pane.boardData.placements.find(row => row.id === 10).is_locked, true);
  assert.equal(h.d.querySelectorAll('.cell.locked[data-placement-id="10"]').length, 2);
  assert.equal(h.inspector().getAttribute('aria-busy'), 'false');
});

test('a held keyboard shortcut toggles once and a subsequent key press unlocks', async t => {
  const h = await harness(t);
  h.key();
  await wait(() => h.getLocked() && !h.d.getElementById('twsDrawerLock').disabled);
  await settle(); h.key(true); await settle();
  assert.equal(h.lockRequests().length, 1);
  h.key();
  await wait(() => !h.getLocked() && !h.d.getElementById('twsDrawerLock').disabled);
  assert.equal(h.lockRequests().length, 2);
});

for (const failure of ['http', 'network']) test(`${failure} failure releases the placement for a real retry without claiming success`, async t => {
  let attempts = 0;
  const h = await harness(t, url => {
    if (!url.endsWith('/lock/') || ++attempts !== 1) return;
    if (failure === 'network') throw new Error('Offline');
    return { httpError: 409, error: { message: 'Scenario busy' } };
  });
  h.click(h.d.getElementById('twsDrawerLock'));
  await wait(() => h.notices.some(row => row.kind === 'error') && !h.d.getElementById('twsDrawerLock').disabled);
  assert.equal(h.notices.filter(row => row.kind === 'success').length, 0);
  assert.equal(h.getLocked(), false);
  h.click(h.inspector());
  await wait(() => h.getLocked() && !h.inspector().disabled);
  assert.equal(h.lockRequests().length, 2);
  assert.deepEqual(h.notices.filter(row => row.kind === 'success').map(row => row.message), ['Locked']);
});

test('the returned server state controls both notice and labels even when the cached state was stale', async t => {
  const h = await harness(t, url => url.endsWith('/lock/') ? { ok: true, placement_id: 10, is_locked: false } : undefined);
  h.click(h.d.getElementById('twsDrawerLock'));
  await wait(() => h.notices.some(row => row.kind === 'success') && !h.d.getElementById('twsDrawerLock').disabled);
  assert.deepEqual(h.notices.filter(row => row.kind === 'success').map(row => row.message), ['Unlocked']);
  assert.equal(h.inspector().textContent, 'Lock section');
  assert.equal(h.d.getElementById('twsDrawerLock').querySelector('.lbl').textContent, 'Lock');
});

for (const failure of ['pane', 'summary']) test(`a saved lock survives a ${failure} refresh failure, warns accurately, and permits a later unlock`, async t => {
  let failRefresh = true, saved = false;
  const h = await harness(t, (url, options, ctx) => {
    if (url.endsWith('/lock/')) { saved = !saved; ctx.setLocked(saved); return { ok: true, placement_id: 10, is_locked: saved }; }
    if (saved && failRefresh && (failure === 'pane' ? url === '/ops/tw/boards/2/' : url.startsWith('/ops/tw/boards/?'))) return { httpError: 500, error: { message: 'Reload failed' } };
  });
  h.click(h.d.getElementById('twsDrawerLock'));
  await wait(() => h.notices.some(row => row.kind === 'warning') && !h.d.getElementById('twsDrawerLock').disabled);
  assert.equal(h.getLocked(), true);
  assert.equal(h.state.panes[0].boardData.placements.find(row => row.id === 10).is_locked, true);
  assert.equal(h.d.getElementById('twsDrawerLock').querySelector('.lbl').textContent, 'Unlock');
  assert.match(h.notices.find(row => row.kind === 'warning').message, /Lock saved/);
  failRefresh = false; h.click(h.d.getElementById('twsDrawerLock'));
  await wait(() => !h.getLocked() && !h.d.getElementById('twsDrawerLock').disabled);
  assert.equal(h.lockRequests().length, 2);
});

test('another placement remains independently usable while one lock is saving', async t => {
  const save = deferred();
  const h = await harness(t, (url, options, ctx) => url === '/ops/tw/placements/10/lock/' ? save.promise.then(() => { ctx.setLocked(true); return { ok: true, placement_id: 10, is_locked: true }; }) : undefined);
  h.click(h.d.getElementById('twsDrawerLock'));
  h.w.openDrawer(0, 11);
  assert.equal(h.d.getElementById('twsDrawerLock').disabled, false);
  h.click(h.d.getElementById('twsDrawerLock'));
  await wait(() => h.getLocked(11) && !h.d.getElementById('twsDrawerLock').disabled);
  assert.equal(h.lockRequests().length, 2);
  assert.equal(h.inspector().disabled, true);
  save.resolve(); await wait(() => !h.inspector().disabled);
  assert.equal(h.getLocked(10), true); assert.equal(h.getLocked(11), true);
});

test('a malformed success response never invents a lock state or leaves controls stuck', async t => {
  const h = await harness(t, url => url.endsWith('/lock/') ? { ok: true, placement_id: 10 } : undefined);
  h.click(h.d.getElementById('twsDrawerLock'));
  await wait(() => h.notices.some(row => row.kind === 'error') && !h.d.getElementById('twsDrawerLock').disabled);
  assert.equal(h.getLocked(), false);
  assert.equal(h.notices.filter(row => row.kind === 'success').length, 0);
});
