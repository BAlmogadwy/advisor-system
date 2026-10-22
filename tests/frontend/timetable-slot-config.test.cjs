/* The shared slot-column module (static/js/timetable-slot-config.js) and the
 * split view's use of it: the editor text, what a manual move may offer, the
 * error and hidden-meetings wording, and the split editor's guarded save. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };

function moduleIn(lang = 'en') {
  const dom = new JSDOM(`<!doctype html><html lang="${lang}"><body></body></html>`, { runScripts: 'outside-only' });
  dom.window.eval(read('static/js/timetable-slot-config.js'));
  return { dom, SC: dom.window.TimetableSlotConfig };
}

const LABS = [
  { label: 'Lab 1', start: '09:00', end: '10:40' },
  { label: 'Bundle', start: '13:00', end: '16:00', policy_only: true },
  { label: 'Online 1', start: '15:50', end: '17:30', online_only: true },
  { label: 'Online 2', start: '17:40', end: '19:20', online_only: true },
];

test('the editor text round-trips, with Windows line endings, spaced columns and spaced labels', () => {
  const { SC } = moduleIn();
  const text = SC.rowsToText(LABS.slice(0, 1));
  assert.equal(text, 'Lab 1\t09:00\t10:40');
  const parsed = SC.parseLines('Lab 1\t09:00\t10:40\r\nLab 2   10:45   12:25\r\n13:00\t14:40\r\nLab 4 14:45 16:25\r\n\r\n');
  assert.deepEqual(JSON.parse(JSON.stringify(parsed)), { rows: [
    { label: 'Lab 1', start: '09:00', end: '10:40' },
    { label: 'Lab 2', start: '10:45', end: '12:25' },
    { label: '13:00-14:40', start: '13:00', end: '14:40' },
    { label: 'Lab 4', start: '14:45', end: '16:25' },
  ], rejected: [] });
});

test('a line that cannot be read is named by its number, never dropped', () => {
  const { SC } = moduleIn();
  const { rows, rejected } = SC.parseLines('Lab 1\t09:00\t10:40\nevening labs\n\nLab 3\t13:00\t14:4');
  assert.equal(rows.length, 1);
  assert.deepEqual([...rejected], [2, 4]);
  assert.equal(SC.rejectedText([], [2, 4]), 'Nothing was saved — these lines were not understood (label, start, end): Labs: line 2, 4');
});

test('Arabic counts take the form for 1, 2, 3–10 and 11+', () => {
  const { SC } = moduleIn('ar');
  assert.deepEqual([1, 2, 3, 10, 11, 100].map(n => SC.meetingsText(n)), ['لقاءً واحداً', 'لقاءين', '3 لقاءات', '10 لقاءات', '11 لقاءً', '100 لقاء']);
});

test('a manual move is never offered a policy column, nor an evening one unless it is in one', () => {
  const { SC } = moduleIn();
  const starts = slots => [...slots].map(slot => slot.start);
  assert.deepEqual(starts(SC.offeredColumns(LABS, '09:00')), ['09:00']);
  assert.deepEqual(starts(SC.offeredColumns(LABS, '15:50:00')), ['09:00', '15:50', '17:40'], 'an online meeting moves between online windows');
});

test('a wrong column is named in the user’s language', () => {
  const errors = [{ list: 'lab_slot_config', index: 2, field: 'start', message: 'must be a time as HH:MM' }];
  assert.equal(moduleIn('en').SC.errorsText(errors), 'The columns were not saved: Labs, line 3: start must be a time as HH:MM');
  assert.equal(moduleIn('ar').SC.errorsText(errors), 'لم تُحفظ الأعمدة: المعامل، السطر 3: البداية يجب أن تكون وقتاً بالصيغة HH:MM');
  const tooMany = [{ list: 'slot_config', index: -1, field: 'list', message: 'has more than 40 columns' }];
  assert.equal(moduleIn('ar').SC.errorsText(tooMany), 'لم تُحفظ الأعمدة: المحاضرات: فيها أكثر من 40 عموداً');
});

test('the hidden meetings are escaped, their times isolated, and the rest counted', () => {
  const { SC } = moduleIn('ar');
  const html = SC.confirmationHtml({ hidden_count: 3, hidden: [
    { board_label: 'Term <2>', course_code: 'CS112', section: 'S1', day: 'MON', start_time: '10:50', end_time: '12:05', is_locked: false },
  ] });
  assert.match(html, /<bdi>Term &lt;2&gt;<\/bdi>/);
  assert.match(html, /الاثنين <bdi dir="ltr">10:50–12:05<\/bdi>/);
  assert.match(html, /عدد إضافي غير معروض: 2/);
  assert.match(html, /يترك 3 لقاءات خارج/);
});

test('a flagged column the change removes is named with what it was', () => {
  const { SC } = moduleIn();
  const html = SC.confirmationHtml({ hidden: [], hidden_count: 0, dropped_flags: [
    { list: 'lab_slot_config', label: 'Online 3', start: '19:30', end: '21:10', flags: ['online_only'] },
  ] });
  assert.doesNotMatch(html, /outside the grid/);
  assert.match(html, /Labs · <bdi>Online 3<\/bdi> <bdi dir="ltr">19:30–21:10<\/bdi> · online-only/);
  assert.match(html, /automatic placers may put room-using classes in/);
});

test('save repeats once with the fingerprint, and stops when declined', async () => {
  const { SC } = moduleIn();
  const sent = [];
  const asks = { code: 'SLOT_EDIT_NEEDS_CONFIRMATION', details: { fingerprint: 'f1', hidden: [] } };
  const send = async extra => { sent.push(extra); return extra.confirm ? { data: { ok: true }, error: null } : { data: null, error: asks }; };
  // Objects made in the page's realm: compare their JSON.
  const plain = value => JSON.parse(JSON.stringify(value));
  assert.deepEqual(await SC.save({ send, confirm: async () => true, report: assert.fail }), { ok: true });
  assert.deepEqual(plain(sent), [{}, { confirm: 'f1' }]);
  sent.length = 0;
  assert.equal(await SC.save({ send, confirm: async () => false, report: assert.fail }), null);
  assert.deepEqual(plain(sent), [{}]);
});

test('a list that keeps changing under the user is reported, never a silent stop', async () => {
  const { SC } = moduleIn();
  const reports = [];
  const send = async () => ({ data: null, error: { code: 'SLOT_EDIT_NEEDS_CONFIRMATION', details: { fingerprint: 'moving' } } });
  assert.equal(await SC.save({ send, confirm: async () => true, report: text => reports.push(text) }), null);
  assert.deepEqual(reports, ['The timetable kept changing while you reviewed it; nothing was saved. Try again.']);
});

test('the MRI move pool offers the effective columns, never an evening one to a day meeting', () => {
  const vm = require('node:vm');
  const mri = read('static/js/page-timetable-mri.js');
  const start = mri.indexOf('  function moveSlotPool(');
  const body = mri.slice(start, mri.indexOf('\n  function ', start + 10));
  const { SC } = moduleIn();
  const toMinutes = value => { const [h, m] = String(value).split(':').map(Number); return h * 60 + m; };
  const context = vm.createContext({
    window: { TimetableSlotConfig: SC },
    toMinutes,
    durationMinutes: (a, b) => toMinutes(b) - toMinutes(a),
    state: { boardDetails: new Map([[2, { slot_config: [], lab_slot_config: [], effective_slot_config: [], effective_lab_slot_config: LABS }]]) },
  });
  vm.runInContext(body, context);
  const pool = placement => [...context.moveSlotPool({ board_id: 2, ...placement })].map(slot => slot.start);
  assert.deepEqual(pool({ start_time: '09:00', end_time: '10:40' }), ['09:00']);
  assert.deepEqual(pool({ start_time: '15:50', end_time: '17:30' }), ['09:00', '15:50', '17:40']);
});

/* ── The split view ── */
async function split(t, answer) {
  const html = read('core/templates/core/timetable_workspace_split.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://split.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, posts = [], notices = [], errors = [];
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  Object.assign(w, { LANGUAGE_CODE: 'en', djCsrfToken: 'audit', INITIAL_SCENARIO: '', INITIAL_BOARD: '', DEFAULT_YEAR: '1448', DEFAULT_TERM: '1' });
  w.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  w.notify = Object.fromEntries(['success', 'warning', 'error'].map(kind => [kind, message => notices.push({ kind, message })]));
  w.console.error = () => {};
  w.matchMedia = () => ({ matches: false });
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.fetch = async (url, options = {}) => {
    let body;
    if (url.endsWith('/slots/update/')) { posts.push(JSON.parse(options.body)); body = answer(posts.at(-1)); }
    else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
    else body = { ok: true, placements: [], candidates: [], boards: [] };
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  w.eval(read('static/js/timetable-meeting-policy.js'));
  w.eval(read('static/js/timetable-slot-config.js'));
  w.eval(read('static/js/page-timetable-workspace-split.js') + '\n;globalThis.__split = { S };');
  await settle();
  const S = w.__split.S;
  S.scenarioId = 1;
  S.scenarioMeta = { id: 1, name: 'Split', status: 'draft', slot_config: [], lab_slot_config: [],
    effective_slot_config: [{ label: '1', start: '09:00', end: '10:15' }], effective_lab_slot_config: LABS };
  return { w, d, S, posts, notices };
}

test('the split view offers a lab meeting the effective columns without evening or policy ones', async t => {
  const h = await split(t, () => ({ ok: true }));
  const boardData = { effective_lab_slot_config: LABS, effective_slot_config: [], placements: [] };
  const lab = { id: 1, day: 'SUN', start_time: '09:00', end_time: '10:40' };
  assert.deepEqual([...h.w.slotPoolForPlacement(boardData, lab)].map(slot => slot.start), ['09:00']);
});

test('the split editor shows the effective columns and saves only after the hidden meetings are confirmed', async t => {
  const hidden = { hidden: [{ placement_id: 9, board_label: 'Term 2', course_code: 'AI201', section: 'S1', day: 'SUN', start_time: '09:00', end_time: '10:40', is_locked: false }], hidden_count: 1, fingerprint: 'fp9' };
  const h = await split(t, body => body.confirm === 'fp9'
    ? { ok: true, scenario: { ...h.S.scenarioMeta, lab_slot_config: body.lab_slot_config } }
    : { httpError: 409, error: { code: 'SLOT_EDIT_NEEDS_CONFIRMATION', message: 'x', details: hidden } });
  const asked = [];
  h.w.dlg = { confirm: async ({ body }) => { asked.push(body); return true; } };
  h.w.openSlotEditorModal();
  assert.equal(h.d.getElementById('twsSlotLab').value, 'Lab 1\t09:00\t10:40\nBundle\t13:00\t16:00\nOnline 1\t15:50\t17:30\nOnline 2\t17:40\t19:20');
  assert.match(h.d.getElementById('twsModalBody').textContent, /online-only columns \(15:50, 17:40\) stay online-only/);
  h.d.querySelector('#twsModalFooter button.primary').click();
  await settle(); await settle();
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[1].confirm, 'fp9');
  assert.match(asked[0], /AI201 S1/);
  assert.ok(h.notices.some(n => n.kind === 'success'));
});
