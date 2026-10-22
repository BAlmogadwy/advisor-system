/* Production scripts and templates with scripted HTTP; no database/server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const wait = async condition => { for (let i = 0; i < 100; i++) { if (condition()) return; await settle(); await new Promise(resolve => setTimeout(resolve, 5)); } assert.fail('Copy UI did not settle'); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

async function harness(t, split, intercept = () => undefined, arabic = false) {
  const suffix = split ? '_split' : '';
  const template = read(`core/templates/core/timetable_workspace${suffix}.html`);
  const html = template.replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://copy.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [];
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  Object.assign(w, { LANGUAGE_CODE: arabic ? 'ar' : 'en', djCsrfToken: 'test', INITIAL_SCENARIO: '', INITIAL_BOARD: '', DEFAULT_YEAR: '1448', DEFAULT_TERM: '1' });
  w.esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  w._injectCsrf = value => value;
  w.debounce = fn => fn;
  w.notify = Object.fromEntries(['success', 'warning', 'error'].map(kind => [kind, message => notices.push({ kind, message })]));
  w.matchMedia = () => ({ matches: false });
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.console.error = () => {};
  const source = { id: 1, name: 'Original <safe>', status: 'published', academic_year: '1448', term: '1', slot_config: [{ start: '09:00', end: '10:15' }] };
  const copy = { ...source, id: 2, name: 'Independent copy', status: 'draft' };
  const copyBoards = [{ id: 20, label: 'Copied board', nominal_term: 1 }];
  w.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    let body = await intercept(url, options, { w, d, copy, copyBoards });
    if (body === undefined) {
      if (url === '/ops/tw/scenarios/1/copy/') body = { ok: true, scenario: copy, boards: copyBoards, copied_from_scenario_id: 1 };
      else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [source] };
      else if (/\/scenarios\/[12]\/$/.test(url)) body = { scenario: url.includes('/2/') ? copy : source };
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: url.includes('scenario_id=1') ? [{ id: 10, label: 'Original board', nominal_term: 1 }] : copyBoards };
      else if (/\/boards\/(10|20)\/$/.test(url)) body = { placements: [{ id: url.includes('/10/') ? 100 : 200, term_section_id: 300, course_code: 'CS101', section: 'S1', day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'R1', is_locked: true }], slot_config: source.slot_config };
      else if (url.endsWith('/quality/')) body = {};
      else if (url.endsWith('/plan-lens/')) body = { plans: [], courses: {}, sections: {} };
      else if ((options.method || 'GET') !== 'GET') assert.fail(`Unexpected mutation ${url}`);
      else body = { ok: true, courses: [], totals: {}, candidates: [], instructors: [], summary: {}, budget: [], unplaced: [] };
    }
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  w.eval(read('static/js/timetable-meeting-policy.js'));
  // The page loads the shared slot-column module before its own script.
  w.eval(read('static/js/timetable-slot-config.js'));
  w.eval(read('static/js/timetable-section-teacher.js'));
  w.eval(read('static/js/timetable-programme-mark.js'));
  if (!split) {
    d.getElementById('twYear').value = '1448'; d.getElementById('twTerm').value = '1';
    w.eval(read('static/js/dialog.js') + '\n;window.dlg = dlg;');
  }
  w.eval(read(`static/js/page-timetable-workspace${split ? '-split' : ''}.js`) + `\n;globalThis.__copyState = ${split ? 'S' : 'TW'};`);
  const prefix = split ? 'tws' : 'tw';
  await wait(() => d.getElementById(`${prefix}Scenario`).options.length === 2);
  if (!split) { d.getElementById('twScenario').value = '1'; await w.onScenarioChange(); }
  await wait(() => !d.getElementById(`${prefix}CopyScenario`).disabled);
  const button = d.getElementById(`${prefix}CopyScenario`);
  const input = () => d.getElementById(split ? 'twsCopyName' : 'dlg-input-field');
  const submit = () => d.getElementById(split ? 'twsCopySubmit' : '') || d.querySelector('.dlg-backdrop .btn-confirm');
  const open = async () => { button.click(); await wait(() => input()); };
  return { w, d, requests, notices, source, copy, button, input, submit, open, state: w.__copyState, prefix,
    copies: () => requests.filter(item => item.url.endsWith('/copy/')) };
}

for (const split of [false, true]) {
  const label = split ? 'split' : 'main';
  for (const arabic of [false, true]) test(`${label} copies a published scenario with an editable name and opens the independent draft (${arabic ? 'ar' : 'en'})`, async t => {
    const h = await harness(t, split, undefined, arabic);
    h.w.history.replaceState(null, '', '?scenario=1&board=10&view=retained#schedule');
    assert.equal(h.button.tagName, 'BUTTON');
    await h.open();
    assert.equal(h.input().maxLength, 200);
    assert.equal(h.input().value, `Original <safe> ${arabic ? '(نسخة)' : '(copy)'}`);
    assert.ok(h.d.querySelector(`label[for="${h.input().id}"]`));
    assert.match(h.d.querySelector(split ? '#twsModalBody' : '.dlg-body').textContent, arabic ? /مستقلة.*يبقى/ : /independent draft.*original scenario stays unchanged/);
    h.input().value = '  My saved timetable  ';
    h.submit().click();
    await wait(() => h.state.scenarioId === 2 && !h.button.disabled);
    assert.equal(h.copies().length, 1);
    assert.deepEqual(JSON.parse(h.copies()[0].options.body), { name: 'My saved timetable' });
    assert.equal(h.state[split ? 'scenarioMeta' : 'scenario'].status, 'draft');
    assert.equal(h.d.getElementById(`${h.prefix}Scenario`).value, '2');
    const currentUrl = new URL(h.w.location.href);
    assert.equal(currentUrl.searchParams.get('scenario'), '2');
    assert.equal(currentUrl.searchParams.get('board'), '20');
    assert.equal(currentUrl.searchParams.get('view'), 'retained');
    assert.equal(currentUrl.hash, '#schedule');
    assert.ok([...h.d.getElementById(`${h.prefix}Scenario`).options].some(option => option.value === '1'));
    const placements = split ? h.state.panes[0].boardData.placements : h.state.placements;
    assert.equal(placements[0].is_locked, true);
    assert.equal(placements[0].room, 'R1');
    assert.deepEqual(h.requests.filter(item => item.options.method === 'POST').map(item => item.url), ['/ops/tw/scenarios/1/copy/']);
    assert.ok(h.notices.some(item => item.kind === 'success'));
  });

  test(`${label} one pending copy owns submission and scenario loading through completion`, async t => {
    const save = deferred(), load = deferred(); let saved = false;
    const h = await harness(t, split, (url, opts, context) => {
      if (url.endsWith('/copy/')) return save.promise.then(() => { saved = true; return { scenario: context.copy, boards: context.copyBoards }; });
      if (saved && url === '/ops/tw/scenarios/2/') return load.promise.then(() => ({ scenario: context.copy }));
    });
    await h.open();
    h.submit().click(); h.submit().click(); h.button.click();
    await wait(() => h.copies().length === 1);
    assert.equal(h.copies().length, 1);
    assert.equal(h.button.disabled, true);
    assert.equal(h.d.getElementById(`${h.prefix}Scenario`).disabled, true);
    if (split) await h.w.api('/ops/tw/placements/200/lock/', { method: 'POST' });
    else await h.w.twFetch('/ops/tw/placements/200/lock/', { method: 'POST' });
    assert.equal(h.requests.filter(item => item.url.endsWith('/lock/')).length, 0);
    save.resolve(); await wait(() => h.requests.some(item => item.url === '/ops/tw/scenarios/2/'));
    assert.equal(h.button.disabled, true);
    load.resolve(); await wait(() => h.state.scenarioId === 2 && !h.button.disabled);
    assert.equal(h.copies().length, 1);
  });

  test(`${label} blank name uses the server default; HTTP failure permits an explicit retry`, async t => {
    let attempt = 0;
    const h = await harness(t, split, url => {
      if (url.endsWith('/copy/') && ++attempt === 1) return { httpError: 409, error: { message: 'Source planner is running' } };
    });
    await h.open(); h.input().value = ''; h.submit().click();
    await wait(() => h.notices.some(item => item.kind === 'error') && !h.button.disabled);
    assert.equal(h.state.scenarioId, 1);
    assert.equal(h.notices.filter(item => item.kind === 'success').length, 0);
    if (!split) { await wait(() => !h.d.querySelector('.dlg-backdrop')); await h.open(); }
    h.input().value = ''; h.submit().click();
    await wait(() => h.state.scenarioId === 2 && !h.button.disabled);
    assert.equal(h.copies().length, 2);
    assert.deepEqual(JSON.parse(h.copies()[1].options.body), {});
  });

  test(`${label} saved copy with a failed detail load remains selectable without another POST`, async t => {
    const h = await harness(t, split, url => url === '/ops/tw/scenarios/2/' ? { httpError: 500, error: { message: 'Temporary load failure' } } : undefined);
    await h.open(); h.submit().click();
    await wait(() => h.notices.some(item => item.kind === 'warning' && /ID 2/.test(item.message)));
    assert.equal(h.copies().length, 1);
    assert.ok([...h.d.getElementById(`${h.prefix}Scenario`).options].some(option => option.value === '2'));
    assert.equal(h.notices.filter(item => item.kind === 'success').length, 0);
  });

  test(`${label} saved copy load failure clears selection and selecting the copy retries only its reads`, async t => {
    let failures = 0;
    const h = await harness(t, split, url => {
      if (url === '/ops/tw/boards/?scenario_id=2' && failures++ === 0) return { httpError: 500, error: { message: 'Temporary board list failure' } };
    });
    await h.open(); h.submit().click();
    const selector = h.d.getElementById(`${h.prefix}Scenario`);
    await wait(() => h.notices.some(item => item.kind === 'warning') && !selector.disabled);
    assert.equal(h.state.scenarioId, null);
    assert.equal(selector.value, '');
    assert.equal(new URL(h.w.location.href).searchParams.has('scenario'), false);
    assert.equal(new URL(h.w.location.href).searchParams.has('board'), false);
    assert.equal(h.button.disabled, true);
    selector.value = '2'; selector.dispatchEvent(new h.w.Event('change', { bubbles: true }));
    await wait(() => h.state.scenarioId === 2 && h.state.boards.length === 1 && !h.button.disabled);
    assert.equal(h.copies().length, 1);
    assert.equal(new URL(h.w.location.href).searchParams.get('scenario'), '2');
    const placements = split ? h.state.panes[0].boardData.placements : h.state.placements;
    assert.equal(placements[0].id, 200);
  });

  test(`${label} cancel leaves the original selected and performs no writes`, async t => {
    const h = await harness(t, split);
    await h.open();
    const cancel = split ? h.d.querySelector('#twsModalFooter button') : h.d.querySelector('.dlg-backdrop .btn-cancel');
    cancel.click();
    await settle();
    assert.equal(h.copies().length, 0);
    assert.equal(h.state.scenarioId, 1);
    assert.equal(h.d.getElementById(`${h.prefix}Scenario`).value, '1');
    assert.equal(h.button.disabled, false);
  });

  test(`${label} unchanged suggested names defer uniqueness to the server on repeated copies`, async t => {
    const h = await harness(t, split);
    await h.open(); h.submit().click();
    await wait(() => h.state.scenarioId === 2 && !h.button.disabled);
    if (!split) await wait(() => !h.d.querySelector('.dlg-backdrop'));
    h.d.getElementById(`${h.prefix}Scenario`).value = '1';
    await h.w.onScenarioChange();
    assert.equal(new URL(h.w.location.href).searchParams.get('scenario'), '1');
    await h.open(); h.submit().click();
    await wait(() => h.copies().length === 2 && !h.button.disabled);
    assert.deepEqual(h.copies().map(item => JSON.parse(item.options.body)), [{}, {}]);
  });

  test(`${label} a 200-codepoint source ending in emoji produces a valid, submittable default name`, async t => {
    const h = await harness(t, split);
    h.state[split ? 'scenarioMeta' : 'scenario'].name = 'A'.repeat(199) + '😀';
    await h.open();
    assert.equal(h.input().value, 'A'.repeat(199));
    assert.equal(h.submit().disabled, false);
    h.submit().click();
    await wait(() => h.state.scenarioId === 2 && !h.button.disabled);
    assert.deepEqual(JSON.parse(h.copies()[0].options.body), {});
    assert.ok(h.notices.some(item => item.kind === 'success'));
  });

  for (const failure of ['board-list', 'board-detail', 'wrong-board-identity']) {
    test(`${label} saved copy ${failure} failure clears source boards and never claims opened`, async t => {
      let saved = false;
      const h = await harness(t, split, (url, options, ctx) => {
        if (url.endsWith('/copy/')) { saved = true; return { scenario: ctx.copy, boards: ctx.copyBoards }; }
        if (!saved) return;
        if (url === '/ops/tw/boards/?scenario_id=2' && failure === 'board-list') return { httpError: 500, error: { message: 'Board list unavailable' } };
        if (url === '/ops/tw/boards/20/' && failure === 'board-detail') return { httpError: 500, error: { message: 'Board data unavailable' } };
        if (url === '/ops/tw/boards/?scenario_id=2' && failure === 'wrong-board-identity') return { boards: [{ id: 10, label: 'Original board', nominal_term: 1 }] };
      });
      await h.open(); h.submit().click();
      await wait(() => h.notices.some(item => item.kind === 'warning' && /ID 2/.test(item.message)));
      assert.equal(h.copies().length, 1);
      assert.equal(h.notices.filter(item => item.kind === 'success').length, 0);
      assert.equal(h.state.boards.length, 0);
      if (split) {
        assert.ok(h.state.panes.every(pane => pane.boardId === null && pane.boardData === null));
        assert.equal(h.d.querySelector('[data-placement-id="100"]'), null);
      } else {
        assert.equal(h.state.boardId, null);
        assert.equal(h.state.loadedBoardId, null);
        assert.equal(h.state.placements.length, 0);
        assert.equal(h.d.querySelector('.tw-card'), null);
        assert.equal(h.d.getElementById('twBoardTabs').children.length, 0);
      }
      assert.ok([...h.d.getElementById(`${h.prefix}Scenario`).options].some(option => option.value === '2'));
    });
  }

  test(`${label} a source changed while the prompt is open cannot be copied accidentally`, async t => {
    const h = await harness(t, split);
    await h.open();
    h.state.scenarioId = 9;
    h.submit().click(); await settle();
    assert.equal(h.copies().length, 0);
  });

  test(`${label} a mutation already in flight disables Copy and ordinary reads do not`, async t => {
    const write = deferred(), readRequest = deferred();
    const h = await harness(t, split, url => {
      if (url === '/copy-audit-write/') return write.promise.then(() => ({ ok: true }));
      if (url === '/copy-audit-read/') return readRequest.promise.then(() => ({ ok: true }));
    });
    const request = split ? h.w.api : h.w.twFetch;
    const writing = request('/copy-audit-write/', { method: 'POST' });
    assert.equal(h.button.disabled, true);
    h.button.click(); assert.equal(h.input(), null);
    write.resolve(); await writing;
    assert.equal(h.button.disabled, false);
    const reading = request('/copy-audit-read/');
    assert.equal(h.button.disabled, false);
    readRequest.resolve(); await reading;
  });
}

test('main ignores a scenario-list response started before copying after the copy has opened', async t => {
  let hold = false;
  const pending = deferred();
  const h = await harness(t, false, url => hold && url.startsWith('/ops/tw/scenarios/?') ? pending.promise : undefined);
  hold = true;
  const listing = h.w.loadScenarios();
  assert.equal(h.button.disabled, false);
  await h.open(); h.submit().click();
  await wait(() => h.state.scenarioId === 2 && !h.button.disabled);
  pending.resolve({ scenarios: [h.source] });
  await listing;
  assert.equal(h.state.scenarioId, 2);
  assert.equal(h.d.getElementById('twScenario').value, '2');
  assert.ok([...h.d.getElementById('twScenario').options].some(option => option.value === '2'));
  assert.equal(h.copies().length, 1);
  assert.equal(new URL(h.w.location.href).searchParams.get('scenario'), '2');
});

test('split Close targets the current copy, retains its identity through reload state, and preserves ordinary source Close', async t => {
  const h = await harness(t, true);
  assert.equal(h.w.splitCopyReturnUrl(), null);
  await h.open(); h.submit().click();
  await wait(() => h.state.scenarioId === 2 && !h.button.disabled);
  assert.equal(h.w.splitCopyReturnUrl(), '/timetable-workspace/?scenario=2&board=20');
  assert.equal(h.w.history.state.timetableCopiedScenario, 2);
  h.d.getElementById('twsScenario').value = '1';
  await h.w.onScenarioChange();
  assert.equal(h.w.splitCopyReturnUrl(), null);
});

test('split copy prompt traps keyboard focus and Enter submits the labeled name', async t => {
  const h = await harness(t, true);
  await h.open();
  const modal = h.d.getElementById('twsModal');
  const first = modal.querySelector('button:not([disabled])');
  h.submit().focus();
  h.submit().dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
  assert.equal(h.d.activeElement, first);
  first.dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }));
  assert.equal(h.d.activeElement, h.submit());
  h.input().value = 'Keyboard copy';
  h.input().dispatchEvent(new h.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await wait(() => h.state.scenarioId === 2 && !h.button.disabled);
  assert.equal(h.copies().length, 1);
  assert.deepEqual(JSON.parse(h.copies()[0].options.body), { name: 'Keyboard copy' });
});
