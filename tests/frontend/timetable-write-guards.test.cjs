/* Stage 3, B1: the server's write guards, as the pages say them.
 *
 * The server refuses a section label already placed in the scenario
 * (409 SECTION_LABEL_IN_USE, details {course_code, label, next_free}) and the
 * removal of a section another scenario owns (409 PLACEMENT_EXTERNALLY_OWNED,
 * override or not). Every caller of the create and remove endpoints says the
 * refusal in the page's words, in English and in Arabic, instead of the
 * server's English message (classic) or nothing at all (split view, whose
 * writes logged a refusal to the console only):
 *
 * - classic: a drop (a planned section, or an existing one from the unplaced
 *   list), the Place dialog (it moves on to the next free label), the
 *   English pattern chooser, Remove, Undo and Redo;
 * - split view: a drop, Remove, Undo and Redo. A move's own "Move failed"
 *   stays quiet when the refusal was said in words (no double toast).
 * MRI calls neither endpoint (it only moves; stage 3's M1 makes it read-only).
 *
 * The free-label rule itself runs against tests/fixtures/section_label_cases.json,
 * the table the server's first_free_section_labels runs against too.
 *
 * The real page scripts and templates; controlled HTTP answers; no server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const wait = async condition => { for (let i = 0; i < 120; i++) { if (condition()) return; await settle(); } assert.fail('UI did not settle'); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const strip = text => String(text || '').replace(/[\u2066\u2069]/g, '');
const LABEL_CASES = JSON.parse(read('tests/fixtures/section_label_cases.json')).cases;

// The server's answers (core/timetable_workspace_views.py).
const LABEL_IN_USE = (label = 'S1', next = 'S2', course = 'AI201') => ({ httpError: 409, ok: false,
  error: { code: 'SECTION_LABEL_IN_USE', message: `${course} ${label} is already placed in this scenario. The next free section is ${next}.`,
    details: { course_code: course, label, next_free: next } } });
const OWNED = { httpError: 409, ok: false,
  error: { code: 'PLACEMENT_EXTERNALLY_OWNED', message: 'This section is also placed by another scenario, which owns its time and room.' } };
// The words, as each page says them.
const WORDS = {
  en: {
    label: 'AI201 S1 is already placed in this scenario. The next free section is S2.',
    external: "Another scenario owns this section's time and room; change it there.",
  },
  ar: {
    label: 'الشعبة AI201 S1 موضوعة بالفعل في هذا السيناريو. أول شعبة متاحة: S2.',
    external: 'يملك سيناريو آخر وقت هذه الشعبة وقاعتها؛ عدّلها هناك.',
  },
};

/* ── Classic: the page's own harness (tests/frontend/timetable-workspace.test.cjs), trimmed ── */
const scenario = { id: 1, name: 'Write guards', status: 'draft', slot_config: [{ label: '09:00', start: '09:00', end: '10:15' }, { label: '10:30', start: '10:30', end: '11:45' }] };
const board = { id: 2, label: 'Term 7', nominal_term: 7, primary_count: 1 };
const at = (id, extra = {}) => ({ id, course_code: `AI${id}`, course_key: `AI${id}`, section: 'S1', term_section_id: id + 100, day: 'SUN',
  start_time: '09:00', end_time: '10:15', room: 'R1', meetings: [], available_capacity: 24, needs_room: true, room_state: 'assigned',
  is_online: false, shared_with_other_scenario: false, ...extra });
const quality = { ok: true, scenario_id: 1, instructor: { assessed: true, working_days: 1, idle_minutes: 0 }, soft_seats: { assessed: true, unresolved: 0 },
  rooms: { assessed: true, shortages: 0, details: [] }, assigned_student_clashes: { assessed: true, count: 0 } };
const quiet = { summary: { critical: 0, warning: 0 }, overlaps: [], instructor_clashes: [], room_clashes: [], cross_board_conflicts: [], hard_overlap_threshold: 20 };
// AI201 meets once a week (a drop places it at once); AI222 twice (a drop or Enter opens Place…).
const AI201 = { course_code: 'AI201', course_key: 'AI201', course_name: 'Data Structures', department: 'AI', credit_hours: 3, programme_term: 7,
  planned_sections: 3, used_sections: 0, used_section_labels: [], remaining_sections: 3, max_per_section: 40, total_demand: 90, required_meetings_per_section: 1 };
const AI222 = { course_code: 'AI222', course_key: 'AI222', course_name: 'Search', department: 'AI', credit_hours: 3, programme_term: 7,
  planned_sections: 3, used_sections: 1, used_section_labels: ['S1'], remaining_sections: 2, max_per_section: 35, total_demand: 70, required_meetings_per_section: 2 };

/* `answer(url, opts)` answers a request first; undefined falls through to a
   board with AI10 S1 placed and the budget above. Every create-planned the
   page sends is in `posts` (refused or not); one the test does not answer
   is placed. */
async function classic(t, { arabic = false, answer = () => undefined, budget = () => [AI201, AI222] } = {}) {
  const template = read('core/templates/core/timetable_workspace.html');
  const html = template.replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost/timetable-workspace/' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [], posts = [];
  let made = [];
  let nextId = 90;
  w.addEventListener('error', e => errors.push(e.error));
  t.after(() => assert.deepEqual(errors, []));
  w.eval(read('static/js/shared-utils.js'));
  w.LANGUAGE_CODE = arabic ? 'ar' : 'en';
  d.documentElement.lang = arabic ? 'ar' : 'en';
  w._injectCsrf = x => x;
  w.esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  w.debounce = fn => fn;
  w.notify = Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => notices.push({ type, text })]));
  w.dlg = { confirm: async () => true };
  w.Element.prototype.scrollIntoView = () => {};
  w.fetch = async (url, opts = {}) => {
    requests.push({ url, opts });
    if (url === '/ops/tw/placements/create-planned/') posts.push(JSON.parse(opts.body));
    let body = await answer(url, opts, { made: () => made });
    if (body === undefined) {
      if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [scenario] };
      else if (url === '/ops/tw/scenarios/1/') body = { scenario, active_job: null };
      else if (url.includes('/quality/')) body = quality;
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
      else if (url === '/ops/tw/boards/2/') body = { placements: [at(10), ...made], slot_config: scenario.slot_config };
      else if (url.includes('/conflicts/')) body = quiet;
      else if (url.includes('/readiness/')) body = { readiness: { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] } };
      else if (url.includes('/budget/')) body = { budget: budget(made) };
      else if (url.includes('/capacity/')) body = { courses: [], totals: {} };
      else if (url.includes('/unplaced/')) body = { unplaced: [] };
      else if (url.includes('/planned-slot-candidates/')) body = { ok: true, status: 'ready', candidates: [] };
      else if (url === '/ops/tw/placements/create-planned/') {
        const sent = JSON.parse(opts.body);
        const rows = sent.meetings.map(m => at(++nextId, { course_code: sent.course_code, course_key: sent.course_key, section: sent.section_label,
          term_section_id: 500, day: m.day, start_time: m.start_time, end_time: m.end_time }));
        made.push(...rows);
        body = { ok: true, placement: rows[0], placements: rows, validation: { critical_count: 0 }, required_meetings: rows.length };
      } else if (/\/placements\/\d+\/remove\/$/.test(url)) {
        const id = Number(url.match(/placements\/(\d+)/)[1]);
        made = made.filter(row => row.id !== id);
        body = { ok: true, removed: id };
      } else if (url === '/ops/tw/placements/create/') body = { ok: true, placement: at(++nextId) };
      else assert.fail(`Unexpected request ${url}`);
    }
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  d.getElementById('twYear').value = '1448'; d.getElementById('twTerm').value = '1';
  for (const file of ['timetable-meeting-policy', 'timetable-slot-config', 'timetable-section-teacher', 'timetable-programme-mark',
    'timetable-room-choice', 'timetable-hotspots', 'ux-controls']) w.eval(read(`static/js/${file}.js`));
  w.eval(read('static/js/page-timetable-workspace.js'));
  await wait(() => d.getElementById('twScenario').options.length === 2);
  d.getElementById('twScenario').value = '1';
  await w.onScenarioChange();
  await wait(() => w.TW.loadedBoardId === 2 && w.TW.sectionBudget?.length);
  const budgetReads = () => requests.filter(r => r.url.includes('/budget/')).length;
  const writes = () => requests.filter(r => (r.opts.method || 'GET') === 'POST');
  const atRest = () => d.querySelector('.tw-workspace').dataset.busy === 'false';
  const dropAt = (payload, day = 'SUN', start = '10:30') => w.onDrop({ preventDefault() {},
    currentTarget: d.querySelector(`#twGrid [role="gridcell"][data-day="${day}"][data-start="${start}"]`),
    dataTransfer: { getData: () => JSON.stringify(payload) } });
  return { w, d, requests, notices, posts, budgetReads, writes, atRest, dropAt, made: () => made };
}
const dropOf = (row, label) => ({ type: 'create_planned', ...row, section_label: label });

test('the free-label rule is the server’s: one table runs both', async t => {
  const h = await classic(t);
  for (const c of LABEL_CASES) {
    assert.deepEqual(Array.from(h.w.freeSectionLabels({ used_section_labels: c.used, remaining_sections: c.count })), c.free, c.name);
  }
});

for (const lang of ['en', 'ar']) test(`classic (${lang}): a drop refused for a label in use says so with the free label and reads the budget again`, async t => {
  const h = await classic(t, { arabic: lang === 'ar', answer: url => (url === '/ops/tw/placements/create-planned/' ? LABEL_IN_USE() : undefined) });
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  await h.dropAt(dropOf(AI201, 'S1'));
  await wait(() => h.budgetReads() === budgets + 1 && h.atRest());
  assert.deepEqual(h.notices.slice(toasts).map(n => ({ ...n, text: strip(n.text) })), [{ type: 'warning', text: WORDS[lang].label }]);
  assert.deepEqual([h.w.TW.undoStack.length, h.w.TW.redoStack.length], [0, 0], 'nothing to undo');
});

// A section from the unplaced list (an existing section, placed through /placements/create/).
const existingDrop = { type: 'create', term_section_id: 300, course_code: 'AI201', course_key: 'AI201', course_name: 'Data Structures', section_label: 'S1', default_room: '' };

for (const lang of ['en', 'ar']) test(`classic (${lang}): a drop of an existing section whose label another section holds says so and reads the budget again`, async t => {
  const h = await classic(t, { arabic: lang === 'ar', answer: url => (url === '/ops/tw/placements/create/' ? LABEL_IN_USE() : undefined) });
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  const writes = h.writes().length;
  await h.dropAt(existingDrop);
  await wait(() => h.budgetReads() === budgets + 1 && h.atRest());
  assert.deepEqual(h.writes().slice(writes).map(r => [r.url, JSON.parse(r.opts.body).term_section_id]), [['/ops/tw/placements/create/', 300]]);
  assert.deepEqual(h.notices.slice(toasts).map(n => ({ ...n, text: strip(n.text) })), [{ type: 'warning', text: WORDS[lang].label }]);
  assert.deepEqual([h.w.TW.undoStack.length, h.w.TW.redoStack.length], [0, 0], 'nothing to undo');
});

test('classic: a drop of an existing section refused for a reason the page does not word reads nothing again', async t => {
  const h = await classic(t, { answer: url => (url === '/ops/tw/placements/create/'
    ? { httpError: 400, error: { code: 'VALIDATION_PLACEMENT', message: 'Invalid placement time.' } } : undefined) });
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  await h.dropAt(existingDrop);
  await wait(() => h.atRest());
  for (let i = 0; i < 6; i++) await settle();
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'error', text: 'Invalid placement time.' }]);
  assert.equal(h.budgetReads(), budgets, 'nothing to read again');
});

test('classic: a refusal the page does not word is still the server’s message, as an error', async t => {
  const h = await classic(t, { answer: url => (url === '/ops/tw/placements/create-planned/'
    ? { httpError: 400, error: { code: 'VALIDATION_PLANNED_PATTERN', message: 'Invalid planned meeting time.' } } : undefined) });
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  await h.dropAt(dropOf(AI201, 'S1'));
  await wait(() => h.atRest());
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'error', text: 'Invalid planned meeting time.' }]);
  assert.equal(h.budgetReads(), budgets, 'nothing to read again');
});

/* ── Place… (5f): the dialog moves on to the next free label ── */
const placeAlert = h => (h.d.getElementById('twPlaceAlert').hidden ? '' : h.d.getElementById('twPlaceAlert').textContent);
const placeRadios = h => [...h.d.querySelectorAll('#twPlaceBody input[name="twPlaceChoice"]')];
const placeDialogOpen = h => h.d.getElementById('twPlaceDialog').hasAttribute('open');
const PATTERN = { candidate_id: 'p1', kind: 'lect', is_pattern: true, requires_full_section_pattern: true, required_meetings: 2,
  day: 'SUN', start: '09:00', end: '10:15', score: 10, tone: 'clean', critical_count: 0, rank: 1, verdict_rank: 1,
  meetings: [{ kind: 'lect', day: 'SUN', start: '09:00', end: '10:15', slot_idx: 0 }, { kind: 'lect', day: 'TUE', start: '09:00', end: '10:15', slot_idx: 0 }],
  verdict: { severity: 'ok', counts: { critical: 0, warning: 0, info: 0 }, students: 0, items: [], more: 0, own_section_meeting: false } };
const patternsAnswer = { ok: true, board: { id: 2, label: 'Term 7', nominal_term: 7 }, status: 'ready', summary: { total: 1 }, candidates: [PATTERN],
  request: { course_code: 'AI222', course_key: 'AI222', section_label: 'S2', required_meetings_per_section: 2, requires_full_section_pattern: true } };
// The budget once another section took S2 meanwhile.
const takenS2 = () => [AI201, { ...AI222, used_sections: 2, used_section_labels: ['S1', 'S2'], remaining_sections: 1 }];

// `lag`: the budget, read again, does not show the label yet (the server
// counted a section the budget lists under another course key): the refusal
// itself moves the dialog on, not the budget's sync.
for (const [lang, lag] of [['en', false], ['ar', false], ['en', true]]) test(`classic Place (${lang}${lag ? ', budget behind' : ''}): a label placed meanwhile moves the dialog on to the next free label, said in it`, async t => {
  let refuse = true, taken = false;
  const h = await classic(t, {
    arabic: lang === 'ar',
    budget: () => (taken ? takenS2() : [AI201, AI222]),
    answer: url => {
      if (url.includes('/planned-slot-candidates/')) return patternsAnswer;
      if (url === '/ops/tw/placements/create-planned/' && refuse) { refuse = false; taken = !lag; return LABEL_IN_USE('S2', 'S3', 'AI222'); }
      return undefined;
    },
  });
  h.w.openPlaceDialog(h.w.placeRequestOf({ ...AI222, section_label: 'S2' }), { origin: 'place:AI222|S2' });
  await wait(() => placeRadios(h).length > 0);
  const toasts = h.notices.length;
  h.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(h) && h.atRest() && placeRadios(h).length > 0);
  const said = lang === 'ar' ? 'وُضعت الشعبة S2 في هذه الأثناء؛ هذه الآن S3.' : 'Section S2 was placed meanwhile; this is now S3.';
  assert.equal(strip(placeAlert(h)), said);
  assert.equal(strip(h.d.getElementById('twPlaceTitle').textContent), lang === 'ar' ? 'وضع AI222 S3' : 'Place AI222 S3');
  assert.deepEqual([h.notices.slice(toasts), placeDialogOpen(h), h.w.TW.undoStack.length], [[], true, 0], 'said in the dialog, nothing placed');
  // Placed under the free label.
  h.d.getElementById('twPlaceCommit').click();
  await wait(() => h.posts.length === 2 && h.atRest() && !placeDialogOpen(h));
  assert.deepEqual(h.posts.map(body => body.section_label), ['S2', 'S3']);
});

test('classic Place: a label refusal arriving after the dialog was left is said where the user is, naming the section', async t => {
  const held = deferred();
  const h = await classic(t, { answer: url => {
    if (url.includes('/planned-slot-candidates/')) return patternsAnswer;
    if (url === '/ops/tw/placements/create-planned/') return held.promise;
    return undefined;
  } });
  h.w.openPlaceDialog(h.w.placeRequestOf({ ...AI222, section_label: 'S2' }), { origin: 'place:AI222|S2' });
  await wait(() => placeRadios(h).length > 0);
  h.d.getElementById('twPlaceCommit').click();
  await wait(() => h.requests.some(r => r.url === '/ops/tw/placements/create-planned/'));
  h.w.UXControls.dialog.close(h.d.getElementById('twPlaceDialog'), 'cancel');
  const toasts = h.notices.length;
  held.resolve(LABEL_IN_USE('S2', 'S3', 'AI222'));
  await wait(() => h.notices.length > toasts && h.atRest());
  assert.deepEqual(h.notices.slice(toasts).map(n => ({ ...n, text: strip(n.text) })),
    [{ type: 'warning', text: 'AI222 S2 is already placed in this scenario. The next free section is S3.' }]);
});

/* ── The English chooser creates a whole pattern ── */
test('classic: the English chooser’s create refused for a label in use says so and reads the budget again', async t => {
  const h = await classic(t, { answer: url => (url === '/ops/tw/placements/create-planned/' ? LABEL_IN_USE('M1', 'S1', 'ENG101') : undefined) });
  const days = ['SUN', 'MON', 'WED', 'THU'];
  h.w.TimetableMeetingPolicy.choose = async () => ({ pattern_id: 'ENG_INTENSIVE_V1_AM', meetings: days.map(day => ({ day, start_time: '09:00', end_time: '12:00' })) });
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  const result = await h.w.chooseEnglishWorkspacePattern({ type: 'create_planned', course_code: 'ENG101', course_key: 'ENG101', course_name: 'English', section_label: 'M1' });
  await wait(() => h.budgetReads() === budgets + 1);
  assert.equal(result, null);
  assert.deepEqual(h.notices.slice(toasts).map(n => ({ ...n, text: strip(n.text) })),
    [{ type: 'warning', text: 'ENG101 M1 is already placed in this scenario. The next free section is S1.' }]);
  assert.equal(h.w.TW.undoStack.length, 0);
});

/* ── Remove, Undo and Redo ── */
for (const lang of ['en', 'ar']) test(`classic (${lang}): Remove of a section another scenario owns is refused in the page’s words, nothing to undo`, async t => {
  const h = await classic(t, { arabic: lang === 'ar', answer: url => (url === '/ops/tw/placements/10/remove/' ? OWNED : undefined) });
  const toasts = h.notices.length;
  await h.w.removePlacement(10);
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'warning', text: WORDS[lang].external }]);
  assert.equal(JSON.parse(h.writes().at(-1).opts.body).override, undefined, 'no override is sent');
  assert.deepEqual([h.w.TW.undoStack.length, h.w.TW.redoStack.length], [0, 0]);
});

test('classic: Undo of a create whose section another scenario now owns keeps the entry and says why', async t => {
  let owned = false;
  const h = await classic(t, { answer: url => (owned && /\/remove\/$/.test(url) ? OWNED : undefined) });
  await h.dropAt(dropOf(AI201, 'S1'));
  await wait(() => h.w.TW.undoStack.length === 1 && h.atRest());
  owned = true;
  const toasts = h.notices.length;
  await h.w.runHistoryAction(true);
  assert.deepEqual(h.notices.slice(toasts), [{ type: 'warning', text: WORDS.en.external }]);
  assert.deepEqual([h.w.TW.undoStack.length, h.w.TW.redoStack.length], [1, 0], 'the entry stays to be undone later');
});

for (const lang of ['en', 'ar']) test(`classic (${lang}): Redo of a create whose label was placed meanwhile keeps the entry, says the free label, reads the budget again`, async t => {
  let taken = false;
  const h = await classic(t, { arabic: lang === 'ar', answer: url => (taken && url === '/ops/tw/placements/create-planned/' ? LABEL_IN_USE() : undefined) });
  await h.dropAt(dropOf(AI201, 'S1'));
  await wait(() => h.w.TW.undoStack.length === 1 && h.atRest());
  await h.w.runHistoryAction(true);
  assert.deepEqual([h.w.TW.undoStack.length, h.w.TW.redoStack.length], [0, 1]);
  taken = true;
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  await h.w.runHistoryAction(false);
  await wait(() => h.budgetReads() === budgets + 1);
  assert.deepEqual(h.notices.slice(toasts).map(n => ({ ...n, text: strip(n.text) })), [{ type: 'warning', text: WORDS[lang].label }]);
  assert.deepEqual([h.w.TW.undoStack.length, h.w.TW.redoStack.length], [0, 1], 'the entry stays to be redone');
});

test('classic: Undo of a Remove whose label another section took meanwhile keeps the entry, says the free label, reads the budget again', async t => {
  let taken = false;
  const h = await classic(t, { answer: url => (taken && url === '/ops/tw/placements/create/' ? LABEL_IN_USE() : undefined) });
  await h.w.removePlacement(10);
  assert.equal(h.w.TW.undoStack.length, 1);
  taken = true;
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  await h.w.runHistoryAction(true);
  await wait(() => h.budgetReads() === budgets + 1);
  assert.deepEqual(h.notices.slice(toasts).map(n => ({ ...n, text: strip(n.text) })), [{ type: 'warning', text: WORDS.en.label }]);
  assert.deepEqual([h.w.TW.undoStack.length, h.w.TW.redoStack.length], [1, 0]);
});

/* ── Split view: the full split script and template (timetable-split-lock.test.cjs's harness) ── */
async function split(t, { arabic = false, answer = () => undefined } = {}) {
  const html = read('core/templates/core/timetable_workspace_split.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://split.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [];
  const placement = id => ({ id, term_section_id: id + 100, course_code: `AI${id}`, course_key: `AI${id}`, course_name: 'Course', section: 'S1',
    day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'R1', is_locked: false, meetings: [] });
  const splitBoard = { id: 2, label: 'Board', nominal_term: 1, primary_count: 1, placement_count: 2 };
  const boardData = () => ({ board: splitBoard, placements: [placement(10), placement(11)], slot_config: [{ start: '09:00', end: '10:15' }] });
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  Object.assign(w, { LANGUAGE_CODE: arabic ? 'ar' : 'en', djCsrfToken: 'audit', INITIAL_SCENARIO: '', INITIAL_BOARD: '', DEFAULT_YEAR: '1448', DEFAULT_TERM: '1' });
  w.esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  w.notify = Object.fromEntries(['success', 'warning', 'error'].map(kind => [kind, message => notices.push({ kind, message })]));
  w.console.error = () => {};
  w.confirm = () => true;
  w.matchMedia = () => ({ matches: false });
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    let body = await answer(url, options);
    if (body === undefined) {
      if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
      else if (url === '/ops/tw/boards/2/') body = boardData();
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [splitBoard] };
      else if (url.endsWith('/conflicts/')) body = { overlaps: [], instructor_clashes: [], room_clashes: [] };
      else if (url.endsWith('/plan-lens/')) body = { plan_lens: { plans: [], courses: {}, sections: {} } };
      else if (url.endsWith('/budget/')) body = { budget: [AI201] };
      else body = { ok: true, candidates: [], instructors: [], summary: {} };
    }
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  for (const file of ['timetable-meeting-policy', 'timetable-slot-config', 'timetable-section-teacher', 'timetable-programme-mark']) w.eval(read(`static/js/${file}.js`));
  w.eval(`${read('static/js/page-timetable-workspace-split.js')}\n;globalThis.__splitAudit = { S };`);
  await settle();
  const { S: state } = w.__splitAudit;
  state.scenarioId = 1; state.scenarioMeta = { id: 1, status: 'draft' }; state.boards = [splitBoard];
  state.cols = 2; state.rows = 1;
  for (const pane of state.panes.slice(0, 2)) { pane.boardId = 2; pane.boardData = boardData(); }
  w.renderPane(0); w.renderPane(1);
  await settle();
  const cell = (day = 'MON', start = '09:00', end = '10:15') => { const node = d.createElement('div'); Object.assign(node.dataset, { day, start, end }); return node; };
  const budgetReads = () => requests.filter(r => r.url.endsWith('/budget/')).length;
  const posts = () => requests.filter(r => (r.options.method || 'GET') === 'POST');
  return { w, d, state, notices, requests, cell, budgetReads, posts };
}
const splitPayload = (label = 'S1') => ({ type: 'create_planned', course_code: 'AI201', course_key: 'AI201', course_name: 'Data Structures', section_label: label, max_per_section: 40 });

for (const lang of ['en', 'ar']) test(`split (${lang}): a drop refused for a label in use says so with the free label and reads the budget again`, async t => {
  const h = await split(t, { arabic: lang === 'ar', answer: url => (url === '/ops/tw/placements/create-planned/' ? LABEL_IN_USE() : undefined) });
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  const created = await h.w.createPlannedPlacementFromCell(0, h.cell(), splitPayload());
  await wait(() => h.budgetReads() === budgets + 1);
  assert.equal(created, null);
  assert.deepEqual(h.notices.slice(toasts).map(n => ({ ...n, message: strip(n.message) })), [{ kind: 'warning', message: WORDS[lang].label }]);
  assert.deepEqual([h.state.undoStack.length, h.state.redoStack.length], [0, 0]);
});

for (const lang of ['en', 'ar']) test(`split (${lang}): Remove of a section another scenario owns is refused in the page’s words, nothing to undo`, async t => {
  const h = await split(t, { arabic: lang === 'ar', answer: url => (url === '/ops/tw/placements/10/remove/' ? OWNED : undefined) });
  const toasts = h.notices.length;
  await h.w.doRemove(10, 0);
  assert.deepEqual(h.notices.slice(toasts), [{ kind: 'warning', message: WORDS[lang].external }], 'the refusal, never “Removed”');
  assert.deepEqual([h.state.undoStack.length, h.state.redoStack.length], [0, 0]);
});

/* api() says a worded refusal for any write with no handler, a move too:
   the move's own "Move failed" must not repeat it (B1 review). This page has
   no external gate, so a move of an external section reaches the server. */
const splitMove = h => h.w.movePlacementToSlot({ placementId: 10, targetPaneIdx: 0, sourcePaneIdx: 0, day: 'MON', start: '09:00', end: '10:15' });

for (const lang of ['en', 'ar']) test(`split (${lang}): a move refused for a section another scenario owns is said once, in the page’s words`, async t => {
  const h = await split(t, { arabic: lang === 'ar', answer: url => (url === '/ops/tw/placements/10/move/' ? OWNED : undefined) });
  const toasts = h.notices.length;
  assert.equal(await splitMove(h), null);
  assert.deepEqual(h.posts().map(r => r.url), ['/ops/tw/placements/10/move/']);
  assert.deepEqual(h.notices.slice(toasts), [{ kind: 'warning', message: WORDS[lang].external }], 'never also “Move failed”');
  assert.deepEqual([h.state.undoStack.length, h.state.redoStack.length], [0, 0]);
});

test('split: a move refused for a reason the page does not word still says “Move failed”', async t => {
  const h = await split(t, { answer: url => (url === '/ops/tw/placements/10/move/'
    ? { httpError: 400, error: { code: 'VALIDATION_PLACEMENT', message: 'Invalid placement time.' } } : undefined) });
  const toasts = h.notices.length;
  assert.equal(await splitMove(h), null);
  assert.deepEqual(h.notices.slice(toasts), [{ kind: 'error', message: 'Move failed' }]);
});

test('split: Undo of a create whose section another scenario now owns keeps the entry and says why', async t => {
  const h = await split(t, { answer: url => (url === '/ops/tw/placements/10/remove/' ? OWNED : undefined) });
  h.state.undoStack.push({ type: 'create', placement_id: 10, board_id: 2, term_section_id: 110, day: 'SUN', start_time: '09:00', end_time: '10:15', room: '' });
  const toasts = h.notices.length;
  await h.w.doUndo();
  assert.deepEqual(h.notices.slice(toasts), [{ kind: 'warning', message: WORDS.en.external }]);
  assert.deepEqual([h.state.undoStack.length, h.state.redoStack.length], [1, 0], 'back on its stack');
});

test('split: Undo of a full pattern stops at the first meeting another scenario owns and says why', async t => {
  const h = await split(t, { answer: url => (url === '/ops/tw/placements/11/remove/' ? OWNED : undefined) });
  h.state.undoStack.push({ type: 'bundle_create', placement_ids: [10, 11], board_id: 2, payload: splitPayload(), meetings: [], label: 'AI201 S1' });
  const toasts = h.notices.length;
  await h.w.doUndo();
  assert.deepEqual(h.notices.slice(toasts), [{ kind: 'warning', message: WORDS.en.external }]);
  assert.deepEqual([h.state.undoStack.length, h.state.redoStack.length], [1, 0]);
});

for (const lang of ['en', 'ar']) test(`split (${lang}): Redo of a full pattern whose label was placed meanwhile keeps the entry and says the free label`, async t => {
  const h = await split(t, { arabic: lang === 'ar', answer: url => (url === '/ops/tw/placements/create-planned/' ? LABEL_IN_USE() : undefined) });
  h.state.redoStack.push({ type: 'bundle_create', placement_ids: [91, 92], board_id: 2, payload: splitPayload(),
    meetings: [{ day: 'SUN', start_time: '09:00', end_time: '10:15' }, { day: 'TUE', start_time: '09:00', end_time: '10:15' }], label: 'AI201 S1' });
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  await h.w.doRedo();
  await wait(() => h.budgetReads() === budgets + 1);
  assert.deepEqual(h.notices.slice(toasts).map(n => ({ ...n, message: strip(n.message) })), [{ kind: 'warning', message: WORDS[lang].label }]);
  assert.deepEqual([h.state.undoStack.length, h.state.redoStack.length], [0, 1], 'back on its stack');
});

test('split: Undo of a Remove whose label another section took meanwhile keeps the entry and says the free label', async t => {
  const h = await split(t, { answer: url => (url === '/ops/tw/placements/create/' ? LABEL_IN_USE() : undefined) });
  h.state.undoStack.push({ type: 'remove', placement_id: 10, board_id: 2, term_section_id: 110, day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'R1' });
  const toasts = h.notices.length;
  await h.w.doUndo();
  assert.deepEqual(h.notices.slice(toasts).map(n => ({ ...n, message: strip(n.message) })), [{ kind: 'warning', message: WORDS.en.label }]);
  assert.deepEqual([h.state.undoStack.length, h.state.redoStack.length], [1, 0], 'back on its stack');
});

test('split: a refusal the page does not word stays out of the toasts, as before', async t => {
  const h = await split(t, { answer: url => (url === '/ops/tw/placements/create-planned/'
    ? { httpError: 400, error: { code: 'VALIDATION_PLANNED_PATTERN', message: 'Invalid planned meeting time.' } } : undefined) });
  const budgets = h.budgetReads();
  const toasts = h.notices.length;
  assert.equal(await h.w.createPlannedPlacementFromCell(0, h.cell(), splitPayload()), null);
  for (let i = 0; i < 6; i++) await settle();
  assert.deepEqual([h.notices.slice(toasts), h.budgetReads()], [[], budgets]);
});

/* ── One set of words on both pages ── */
test('classic and split say the same words for the same refusal, in English and in Arabic', async t => {
  const errors = [
    OWNED.error,
    LABEL_IN_USE().error,
    LABEL_IN_USE('S3', 'S10', 'CS112').error,
    { code: 'SECTION_LABEL_IN_USE', message: 'x', details: { course_code: 'AI201', label: 'S1' } },
    { code: 'SECTION_LABEL_IN_USE', message: 'x' },
    { code: 'PLACEMENT_LOCKED', message: 'Placement is locked.' },
    null,
  ];
  for (const arabic of [false, true]) {
    const c = await classic(t, { arabic });
    const s = await split(t, { arabic });
    const said = errors.map(error => [c.w.serverRefusalWords(error), s.w.serverRefusalWords(error)]);
    for (const [one, other] of said) assert.equal(one, other);
    // One isolate around the pair and one around the free label, so Arabic never reverses them.
    assert.equal(said[2][0], arabic
      ? 'الشعبة \u2066CS112 S3\u2069 موضوعة بالفعل في هذا السيناريو. أول شعبة متاحة: \u2066S10\u2069.'
      : '\u2066CS112 S3\u2069 is already placed in this scenario. The next free section is \u2066S10\u2069.');
    assert.equal(strip(said[3][0]), arabic ? 'الشعبة AI201 S1 موضوعة بالفعل في هذا السيناريو.' : 'AI201 S1 is already placed in this scenario.');
    assert.equal(said[4][0], arabic ? 'هذه الشعبة موضوعة بالفعل في هذا السيناريو.' : 'This section is already placed in this scenario.');
    assert.deepEqual([said[5][0], said[6][0]], ['', ''], 'a refusal the pages do not word is not theirs to say');
  }
});
