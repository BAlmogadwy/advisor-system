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
  dom.window.eval(read('static/js/shared-utils.js'));  // pluralAr, as base.html loads it
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

/* PK6 (design record §7.2 item 6, 6e): the workspace draws a meeting off the
   grid in a strip under its table, so its confirmation says so; split view
   draws none there and keeps its words. Recorded before 6e, to the byte. */
const PK6_DETAILS = {
  hidden: [
    { board_label: 'Term <2>', course_code: 'CS112', section: 'S1', day: 'MON', start_time: '10:50', end_time: '12:05', is_locked: false },
    { board_label: 'Term 3', course_code: 'AI"1', section: 'S2', day: 'SUN', start_time: '09:00', end_time: '10:15', is_locked: true },
  ],
  hidden_count: 5,
  dropped_flags: [
    { list: 'lab_slot_config', label: 'Online 3', start: '19:30', end: '21:10', flags: ['online_only'] },
    { list: 'slot_config', label: 'P', start: '13:00', end: '16:00', flags: ['policy_only'] },
  ],
};
const PK6_BEFORE = {
  en: "<p class=\"tw-readiness-lead\">This change leaves 5 meetings outside the grid's columns. They stay in the timetable and are still published, but none is drawn on the grid until a column has its time again:</p><ul class=\"tw-readiness-list\"><li><bdi>Term &lt;2&gt;</bdi> · <bdi>CS112 S1</bdi> · Mon <bdi dir=\"ltr\">10:50–12:05</bdi></li><li><bdi>Term 3</bdi> · <bdi>AI&quot;1 S2</bdi> · Sun <bdi dir=\"ltr\">09:00–10:15</bdi> · time locked</li></ul><p class=\"tw-readiness-lead\">And 3 meetings more.</p><p class=\"tw-readiness-lead\">And these columns lose their rule; a column that is no longer online-only is one the automatic placers may put room-using classes in:</p><ul class=\"tw-readiness-list\"><li>Labs · <bdi>Online 3</bdi> <bdi dir=\"ltr\">19:30–21:10</bdi> · online-only</li><li>Lectures · <bdi>P</bdi> <bdi dir=\"ltr\">13:00–16:00</bdi> · policy display column</li></ul>",
  ar: "<p class=\"tw-readiness-lead\">هذا التغيير يترك 5 لقاءات خارج أعمدة الجدول. تبقى في الجدول وتُنشر، لكن لا يظهر أي منها في الشبكة حتى تعود أعمدة بأوقاتها:</p><ul class=\"tw-readiness-list\"><li><bdi>Term &lt;2&gt;</bdi> · <bdi>CS112 S1</bdi> · الاثنين <bdi dir=\"ltr\">10:50–12:05</bdi></li><li><bdi>Term 3</bdi> · <bdi>AI&quot;1 S2</bdi> · الأحد <bdi dir=\"ltr\">09:00–10:15</bdi> · الوقت مقفل</li></ul><p class=\"tw-readiness-lead\">عدد إضافي غير معروض: 3.</p><p class=\"tw-readiness-lead\">وهذه الأعمدة تفقد قيدها؛ العمود الذي لم يعد «عن بُعد فقط» يمكن أن يضع فيه المُجدول الآلي محاضرات تحتاج قاعة:</p><ul class=\"tw-readiness-list\"><li>المعامل · <bdi>Online 3</bdi> <bdi dir=\"ltr\">19:30–21:10</bdi> · عن بُعد فقط</li><li>المحاضرات · <bdi>P</bdi> <bdi dir=\"ltr\">13:00–16:00</bdi> · عمود عرض للسياسة</li></ul>",
};
const PK6_LEAD = {
  en: ["but none is drawn on the grid until a column has its time again:", "each is shown under its table until a column has its time again:", "They stay in the timetable and are still published, ", "They stay in the timetable and are still published; "],
  ar: ["لكن لا يظهر أي منها في الشبكة حتى تعود أعمدة بأوقاتها:", "ويظهر كلٌّ منها أسفل جدوله حتى يعود عمود بوقته:", "تبقى في الجدول وتُنشر، ", "تبقى في الجدول وتُنشر، "],
};

test('PK6 the parked lead says each meeting is shown under its table; without the option the words are as before, to the byte', () => {
  for (const lang of ['en', 'ar']) {
    const { SC } = moduleIn(lang);
    const plain = SC.confirmationHtml(PK6_DETAILS);
    assert.equal(plain, PK6_BEFORE[lang], `${lang}: unchanged without the option (split view)`);
    assert.equal(SC.confirmationHtml(PK6_DETAILS, {}), plain);
    assert.equal(SC.confirmationHtml(PK6_DETAILS, { parked: false }), plain);
    const [oldEnd, newEnd, oldJoin, newJoin] = PK6_LEAD[lang];
    const parked = SC.confirmationHtml(PK6_DETAILS, { parked: true });
    assert.equal(parked, plain.replace(oldJoin + oldEnd, newJoin + newEnd), `${lang}: only the lead's end changes`);
    assert.match(parked, lang === 'en'
      ? /^<p class="tw-readiness-lead">This change leaves 5 meetings outside the grid's columns\. They stay in the timetable and are still published; each is shown under its table until a column has its time again:<\/p>/
      : /^<p class="tw-readiness-lead">هذا التغيير يترك 5 لقاءات خارج أعمدة الجدول\. تبقى في الجدول وتُنشر، ويظهر كلٌّ منها أسفل جدوله حتى يعود عمود بوقته:<\/p>/);
    // Nothing hidden: no lead, either way.
    assert.equal(SC.confirmationHtml({ hidden: [], hidden_count: 0 }, { parked: true }), '');
  }
});

/* ── The slot dialog's rows (design record §7.2 item 6, 6f) ── */
const LRI = String.fromCharCode(0x2066);
const PDI = String.fromCharCode(0x2069);
const plain = value => JSON.parse(JSON.stringify(value));

test('SC1 a VALIDATION_SLOTS answer is heard by `invalid` first, then said; without it the answer is as before', async () => {
  const errors = [{ list: 'lab_slot_config', index: 1, field: 'end', message: 'must be after the start' },
    { list: 'slot_config', index: -1, field: 'list', message: 'has more than 40 columns' }];
  const refused = { data: null, error: { code: 'VALIDATION_SLOTS', details: { errors } } };
  const { SC } = moduleIn();
  const heard = [];
  const answer = await SC.save({ send: async () => refused, confirm: assert.fail,
    report: text => heard.push(['report', text]), invalid: list => heard.push(['invalid', plain(list)]) });
  assert.equal(answer, null);
  const sentence = 'The columns were not saved: Labs, line 2: end must be after the start; Lectures: has more than 40 columns';
  assert.deepEqual(heard, [['invalid', errors], ['report', sentence]], 'invalid once, with the columns, before report');
  // No invalid: the report alone, the same sentence (split view's save).
  const before = [];
  assert.equal(await SC.save({ send: async () => refused, confirm: assert.fail, report: text => before.push(text) }), null);
  assert.deepEqual(before, [sentence]);
  // An answer with no columns still reaches invalid, as an empty list.
  const bare = [];
  await SC.save({ send: async () => ({ data: null, error: { code: 'VALIDATION_SLOTS' } }), confirm: assert.fail,
    report: () => {}, invalid: list => bare.push(plain(list)) });
  assert.deepEqual(bare, [[]]);
  // A confirmation is not a refusal: invalid hears nothing of it.
  const quiet = [];
  let n = 0;
  await SC.save({ send: async () => (++n === 1 ? { data: null, error: { code: 'SLOT_EDIT_NEEDS_CONFIRMATION', details: { fingerprint: 'f' } } } : { data: { ok: true }, error: null }),
    confirm: async () => true, report: assert.fail, invalid: list => quiet.push(list) });
  assert.deepEqual(quiet, []);
});

test('SC2 a save sends each row’s label, start and end, trimmed, an empty value kept, never a flag', () => {
  const { SC } = moduleIn();
  const rows = SC.editorRows(LABS);
  rows[0].label = '  Lab one ';
  rows[0].start = ' 9:00';
  rows[1].label = '';
  rows[2].end = '';
  assert.deepEqual(plain(SC.rowsToPayload(rows)), [
    { label: 'Lab one', start: '9:00', end: '10:40' },
    { label: '', start: '13:00', end: '16:00' },
    { label: 'Online 1', start: '15:50', end: '' },
    { label: 'Online 2', start: '17:40', end: '19:20' },
  ]);
  assert.deepEqual(plain(SC.rowsToPayload([{ key: 'r1', flags: ['online_only'] }])), [{ label: '', start: '', end: '' }]);
});

test('SC3 the editor’s rows: every column as a row with its own key and its flags; a missing value is empty', () => {
  const { SC } = moduleIn();
  const rows = SC.editorRows(LABS);
  assert.deepEqual(plain(rows).map(({ key, ...row }) => row), [
    { label: 'Lab 1', start: '09:00', end: '10:40', flags: [] },
    { label: 'Bundle', start: '13:00', end: '16:00', flags: ['policy_only'] },
    { label: 'Online 1', start: '15:50', end: '17:30', flags: ['online_only'] },
    { label: 'Online 2', start: '17:40', end: '19:20', flags: ['online_only'] },
  ]);
  // Keys are unique across calls (a row added later never takes one in use).
  const again = SC.editorRows(LABS);
  const keys = [...rows, ...again, ...SC.editorRows([{}])].map(row => row.key);
  assert.equal(new Set(keys).size, keys.length);
  assert.ok(keys.every(key => /^r\d+$/.test(key)));
  // A flag that is false is no flag; an empty column is empty strings.
  assert.deepEqual(plain(SC.editorRows([{ start: '09:00', online_only: false, policy_only: true }, {}, null])).map(({ key, ...row }) => row), [
    { label: '', start: '09:00', end: '', flags: ['policy_only'] },
    { label: '', start: '', end: '', flags: [] },
    { label: '', start: '', end: '', flags: [] },
  ]);
  assert.deepEqual(plain(SC.editorRows(undefined)), []);
});

test('SC4 a time is read as the server reads it; a row’s length is shown only for a start and a later end', () => {
  const { SC } = moduleIn();
  const read = ['09:00', '9:00', '09:00:00', ' 09:00 ', '23:59', '0:00', '24:00', '9am', '09:60', '7:5', '', null, undefined];
  assert.deepEqual(read.map(value => SC.minutes(value)), [540, 540, 540, 540, 1439, 0, null, null, null, null, null, null, null]);
  assert.deepEqual([
    SC.lengthText('09:00', '10:15'), SC.lengthText('9:00', '10:40:00'), SC.lengthText('09:00', '09:01'),
    SC.lengthText('10:15', '09:00'), SC.lengthText('09:00', '09:00'), SC.lengthText('', '10:15'), SC.lengthText('09:00', '10'),
  ], ['75 min', '100 min', '1 min', '—', '—', '—', '—']);
  const { SC: ar } = moduleIn('ar');
  assert.deepEqual(['09:01', '09:02', '09:03', '09:11', '10:15', '10:40'].map(end => ar.lengthText('09:00', end)),
    ['دقيقة واحدة', 'دقيقتان', '3 دقائق', '11 دقيقةً', '75 دقيقةً', '100 دقيقة']);
  assert.equal(ar.lengthText('10:15', '09:00'), '—');
});

test('SC5 a row’s words: headers, its list, its flags and its buttons named by the row, the name isolated; a wrong field as “{field}: {problem}”', () => {
  const { SC: en } = moduleIn();
  const { SC: ar } = moduleIn('ar');
  assert.deepEqual(plain(en.rowWords.headers), { label: 'Label', start: 'Start', end: 'End', length: 'Length', rule: 'Rule' });
  assert.deepEqual(plain(ar.rowWords.headers), { label: 'الاسم', start: 'البداية', end: 'النهاية', length: 'المدة', rule: 'القيد' });
  assert.deepEqual([plain(en.rowWords.column), plain(ar.rowWords.column)], [
    { slot_config: 'Lecture column', lab_slot_config: 'Lab column' }, { slot_config: 'عمود محاضرات', lab_slot_config: 'عمود معامل' }]);
  assert.deepEqual([plain(en.rowWords.flag), plain(ar.rowWords.flag)], [
    { online_only: 'Online only', policy_only: 'Policy column' }, { online_only: 'عن بُعد فقط', policy_only: 'عمود سياسة' }]);
  assert.deepEqual(['up', 'down', 'remove'].map(kind => en.rowWords[kind]('Lab <1>')),
    [`Move ${LRI}Lab <1>${PDI} up`, `Move ${LRI}Lab <1>${PDI} down`, `Remove ${LRI}Lab <1>${PDI}`]);
  assert.deepEqual(['up', 'down', 'remove'].map(kind => ar.rowWords[kind]('Lab 1')),
    [`نقل ${LRI}Lab 1${PDI} إلى الأعلى`, `نقل ${LRI}Lab 1${PDI} إلى الأسفل`, `حذف ${LRI}Lab 1${PDI}`]);
  const wrong = [
    { list: 'slot_config', index: 0, field: 'end', message: 'must be after the start' },
    { list: 'slot_config', index: 1, field: 'start', message: 'repeats another column' },
    { list: 'slot_config', index: 2, field: 'row', message: 'must be an object' },
    { list: 'slot_config', index: 3, field: 'odd', message: 'is odd' },
  ];
  assert.deepEqual(wrong.map(en.fieldErrorText), ['End: must be after the start', 'Start: repeats another column', 'row: must be an object', 'odd: is odd']);
  assert.deepEqual(wrong.map(ar.fieldErrorText), ['النهاية: يجب أن تكون بعد البداية', 'البداية: تكرر عموداً آخر', 'السطر: غير صالح', 'odd: is odd']);
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
  w.eval(read('static/js/timetable-section-teacher.js'));
  w.eval(read('static/js/timetable-programme-mark.js'));
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
  // Split view draws no strip: its words say the meeting is not drawn (6e, PK6).
  assert.match(asked[0], /none is drawn on the grid/);
  assert.doesNotMatch(asked[0], /under its table/);
  assert.ok(h.notices.some(n => n.kind === 'success'));
});
