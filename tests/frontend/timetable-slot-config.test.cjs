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

test('the editor text round-trips, with Windows line endings and spaced columns', () => {
  const { SC } = moduleIn();
  const text = SC.rowsToText(LABS.slice(0, 1));
  assert.equal(text, 'Lab 1\t09:00\t10:40');
  assert.deepEqual(JSON.parse(JSON.stringify(SC.parseText('Lab 1\t09:00\t10:40\r\nLab 2   10:45   12:25\r\n13:00\t14:40\r\n\r\n'))), [
    { label: 'Lab 1', start: '09:00', end: '10:40' },
    { label: 'Lab 2', start: '10:45', end: '12:25' },
    { label: '13:00-14:40', start: '13:00', end: '14:40' },
  ]);
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
});

test('the hidden meetings are escaped, their times isolated, and the rest counted', () => {
  const { SC } = moduleIn('ar');
  const html = SC.hiddenMeetingsHtml({ hidden_count: 3, hidden: [
    { board_label: 'Term <2>', course_code: 'CS112', section: 'S1', day: 'MON', start_time: '10:50', end_time: '12:05', is_locked: false },
  ] });
  assert.match(html, /<bdi>Term &lt;2&gt;<\/bdi>/);
  assert.match(html, /الاثنين <bdi dir="ltr">10:50–12:05<\/bdi>/);
  assert.match(html, /و2 غيرها/);
  assert.match(html, /يترك 3 من اللقاءات/);
});

test('save repeats once with the fingerprint, and stops when declined', async () => {
  const { SC } = moduleIn();
  const sent = [];
  const hides = { code: 'SLOT_EDIT_HIDES_PLACEMENTS', details: { fingerprint: 'f1', hidden: [] } };
  const send = async extra => { sent.push(extra); return extra.confirm_hidden ? { data: { ok: true }, error: null } : { data: null, error: hides }; };
  // Objects made in the page's realm: compare their JSON.
  const plain = value => JSON.parse(JSON.stringify(value));
  assert.deepEqual(await SC.save({ send, confirmHidden: async () => true, reportInvalid: assert.fail }), { ok: true });
  assert.deepEqual(plain(sent), [{}, { confirm_hidden: 'f1' }]);
  sent.length = 0;
  assert.equal(await SC.save({ send, confirmHidden: async () => false, reportInvalid: assert.fail }), null);
  assert.deepEqual(plain(sent), [{}]);
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
  const h = await split(t, body => body.confirm_hidden === 'fp9'
    ? { ok: true, scenario: { ...h.S.scenarioMeta, lab_slot_config: body.lab_slot_config } }
    : { httpError: 409, error: { code: 'SLOT_EDIT_HIDES_PLACEMENTS', message: 'x', details: hidden } });
  const asked = [];
  h.w.dlg = { confirm: async ({ body }) => { asked.push(body); return true; } };
  h.w.openSlotEditorModal();
  assert.equal(h.d.getElementById('twsSlotLab').value, 'Lab 1\t09:00\t10:40\nBundle\t13:00\t16:00\nOnline 1\t15:50\t17:30\nOnline 2\t17:40\t19:20');
  assert.match(h.d.getElementById('twsModalBody').textContent, /online-only columns \(15:50, 17:40\) stay online-only/);
  h.d.querySelector('#twsModalFooter button.primary').click();
  await settle(); await settle();
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[1].confirm_hidden, 'fp9');
  assert.match(asked[0], /AI201 S1/);
  assert.ok(h.notices.some(n => n.kind === 'success'));
});
