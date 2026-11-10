/* A programme's default weekly meetings, in the browser (PR 2, design §6).
 *
 * Owner, 2026-11-10: each programme sets how its 3-credit courses meet each
 * week (Section Planning), Male and Female alike. The budget rows the
 * workspace reads (compute_scenario_budget with the scenario's programmes)
 * say what the scenario's programmes decided for a course
 * (`programme_meeting_pattern`) and whether the course differs from a value
 * they agreed on (`meeting_pattern_drift`). What the browser makes of it:
 *
 * - The Add course / section dialog (static/js/timetable-course-editor.js):
 *   a new course's Weekly meetings starts on "Programme default", which sends
 *   nothing (Standard and 2 × 100 min are sent as chosen); a course in the
 *   plan names its programmes' default, or each programme's vote when they
 *   differ, under what it meets now, and when it differs offers "Use
 *   programme default", which saves that value and gives the keyboard back to
 *   "Save weekly meetings"; the dialog can open on a course; an add says the
 *   weekly meetings its programmes gave it.
 * - One weekly-meetings line over the course list (classic and split): the
 *   courses in their programmes' shape, those whose programmes differ, those
 *   that differ from their programmes' default, each opening the dialog on
 *   it, and "Use programme defaults (N)", which saves them one after another
 *   and stops at the first refusal. Nothing at all when no course has
 *   anything to say.
 * - "Use programme default(s)" never posts a value read earlier: the server
 *   saves what the programmes decide when asked (use_programme_default,
 *   naming the value shown) and refuses (409) one gone or changed, which the
 *   page says in its own words before reading the list again. A run names
 *   the placed sections its saves left meeting the old way, as a warning.
 *   On the split page the line never acts on a scenario other than the one
 *   it was drawn for, and is off while the page's own optimise job runs.
 * - A chip on a course that differs from its programmes' default, or whose
 *   programmes differ: classic's rows to place AND its rows placed in full,
 *   split's sidebar cards. The academic department stays as it was.
 *
 * The real page scripts and templates; controlled HTTP answers; no server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); };
const wait = async condition => { for (let i = 0; i < 120; i++) { if (condition()) return; await settle(); } assert.fail('UI did not settle'); };
const FSI = String.fromCharCode(0x2066), PDI = String.fromCharCode(0x2069);
const iso = text => `${FSI}${text}${PDI}`;
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ── The budget rows, as compute_scenario_budget answers them with the scenario's programmes ── */
const decided = (status, meeting_pattern, programmes, label = '', label_ar = '') => ({ status, meeting_pattern, label, label_ar, programmes });
const vote = (program, meeting_pattern) => ({ program, meeting_pattern });
const AGREE_100 = decided('agree', '2x100', [vote('AI2', '2x100'), vote('CS2', ''), vote('DS2', '2x100')], '2 × 100 min', '٢ × ١٠٠ دقيقة');
const AGREE_STANDARD = decided('agree', 'standard', [vote('CS2', 'standard')], 'Standard (2 × 75 min)', 'قياسي (٢ × ٧٥ دقيقة)');
const MIXED = decided('mixed', '', [vote('AI2', '2x100'), vote('CS2', 'standard'), vote('DS2', '')]);
const NONE = decided('none', '', [vote('AI2', '')]);
const AMBIGUOUS = decided('ambiguous', '', []);
const base = { credit_hours: 3, programme_term: 7, planned_sections: 1, used_sections: 0, used_section_labels: [], remaining_sections: 1,
  max_per_section: 40, total_demand: 30, required_meetings_per_section: 2, requires_full_section_pattern: true,
  meeting_pattern: '', meeting_pattern_label: '', meeting_pattern_label_ar: '', meeting_pattern_choices: ['', '2x100'], meeting_durations: [75, 75] };
const as2x100 = { meeting_pattern: '2x100', meeting_pattern_label: '2 × 100 min', meeting_pattern_label_ar: '٢ × ١٠٠ دقيقة', meeting_durations: [100, 100] };
const course = (code, name, department, extra) => ({ ...base, course_code: code, course_key: `${code}::${name.toUpperCase().replace(/ /g, '_')}`, course_name: name, department, ...extra });
// Agreed on 2 x 100; the scenario still meets 2 x 75: it differs.
const STAT103 = course('STAT103', 'Probability', 'MATH', { programme_meeting_pattern: AGREE_100, meeting_pattern_drift: true });
// Agreed on 2 x 100, and taught so: in its programmes' shape.
const MATH243 = course('MATH243', 'Linear Algebra', 'MATH', { ...as2x100, programme_meeting_pattern: AGREE_100, meeting_pattern_drift: false });
// The programmes differ: it kept its credit hours' pattern.
const COE226 = course('COE226', 'Digital Logic', 'COE', { programme_meeting_pattern: MIXED, meeting_pattern_drift: false });
// Agreed on standard; set to 2 x 100 in the scenario: it differs.
const AI333 = course('AI333', 'Planning', 'AI', { ...as2x100, programme_meeting_pattern: AGREE_STANDARD, meeting_pattern_drift: true });
// Nobody set it; the scenario's own 2 x 100 (scenario 669's way): nothing to say.
const AI201 = course('AI201', 'Data Structures', 'AI', { ...as2x100, programme_meeting_pattern: NONE, meeting_pattern_drift: false });
// Two courses behind a typed code: never guessed.
const MGT324 = course('MGT324', 'Entrepreneurship', 'MGT', { course_key: 'MGT324', programme_meeting_pattern: AMBIGUOUS, meeting_pattern_drift: false });
// An English bundle: one choice, so nothing is said even with votes.
const ENG101 = course('ENG101', 'English Language Skills I', 'ENG', { meeting_pattern_choices: [''], meeting_durations: [180, 180, 180, 180],
  required_meetings_per_section: 4, programme_meeting_pattern: AGREE_100, meeting_pattern_drift: false });
const BUDGET = [STAT103, MATH243, COE226, AI333, AI201, MGT324, ENG101];
// The same rows from a server without the programmes (or the frozen default answer).
const plain = row => { const { programme_meeting_pattern: _p, meeting_pattern_drift: _d, ...rest } = row; return rest; };

/* The weekly-meetings save's answer: the course set to the posted value (or,
   asked for its programmes' default, to theirs), its row no longer differing
   when that is its programmes' value. */
const programmesValue = row => (row?.programme_meeting_pattern?.meeting_pattern === '2x100' ? '2x100' : '');
const savedAnswer = (rows, body) => {
  const value = body.use_programme_default ? programmesValue(rows.find(row => row.course_key === body.course_key)) : body.meeting_pattern;
  const budget = rows.map(row => {
    if (row.course_key !== body.course_key) return row;
    const shaped = value === '2x100' ? { ...row, ...as2x100 }
      : { ...row, meeting_pattern: '', meeting_pattern_label: '', meeting_pattern_label_ar: '', meeting_durations: [75, 75] };
    const wanted = row.programme_meeting_pattern?.meeting_pattern === '2x100' ? '2x100' : '';
    return { ...shaped, meeting_pattern_drift: row.programme_meeting_pattern?.status === 'agree' && shaped.meeting_pattern !== wanted };
  });
  return { ok: true, course_key: body.course_key, meeting_pattern: value, previous: '', changed: true, budget, mismatched_sections: [],
    ...(body.use_programme_default ? { meeting_pattern_source: 'programmes' } : {}) };
};
const refusal = message => ({ httpError: 400, ok: false, error: { code: 'VALIDATION_MEETING_PATTERN', message } });
// The server's 409s for a programme default gone or changed since the list was read.
const CHANGED = { httpError: 409, ok: false, error: { code: 'PROGRAMME_DEFAULT_CHANGED', message: 'STAT103’s programme default has changed since the page was drawn; it was not saved. Read the list again.' } };
const unavailable = reason => ({ httpError: 409, ok: false, error: { code: 'PROGRAMME_DEFAULT_UNAVAILABLE', message: 'server words', details: { reason } } });
// "Use programme default(s)": what the page posts for a row.
const useDefault = (row, expected) => ({ course_key: row.course_key, use_programme_default: true, expected_meeting_pattern: expected });

/* ── The dialog on its own, as both pages open it ── */
const shown = element => Boolean(element) && !element.closest('[hidden]');
const control = (rootEl, words) => {
  const found = [...rootEl.querySelectorAll('label')].filter(label => shown(label) && label.firstChild?.textContent === words)
    .map(label => label.querySelector('select, input'));
  assert.equal(found.length, 1, `one control on show labelled “${words}”`);
  return found[0];
};
const button = (rootEl, words) => [...rootEl.querySelectorAll('button')].find(item => shown(item) && item.textContent === words) || null;
const options = select => [...select.options].map(option => [option.value, option.textContent]);
const lineAfterNow = form => {
  const now = [...form.querySelectorAll('p')].find(p => shown(p) && /^(Weekly meetings now|اللقاءات الأسبوعية حالياً): /.test(p.textContent));
  const next = now?.nextElementSibling;
  return next && next.tagName === 'P' && shown(next) ? next.textContent : null;
};
const EDITOR = 'dialog[aria-label="Add course / section"] form, dialog[aria-label="إضافة مقرر / شعبة"] form';

function editor(t, { answer = body => savedAnswer(BUDGET, body), arabic = false, published = false, budget = BUDGET, courseKey, returnFocus, reread } = {}) {
  const dom = new JSDOM('<!doctype html><body><button id="opener">+ Course / section</button></body>', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, calls = [], saved = [];
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); this.dispatchEvent(new w.Event('close')); };
  w.eval(read('static/js/timetable-course-editor.js'));
  const open = () => w.TimetableCourseEditor.open({
    scenarioId: 42, published, budget, arabic, courseKey, returnFocus, reread,
    request: async (url, opts, onError) => {
      const body = JSON.parse(opts.body);
      calls.push({ url, body });
      const reply = await answer(body, url);
      if (reply?.httpError) { onError?.(reply.error.message, reply.error); return null; }
      return reply;
    },
    onSaved: async (rows, change) => { saved.push({ rows, change }); },
  });
  open();
  const form = d.querySelector(EDITOR);
  const courseSelect = control(form, arabic ? 'المقرر' : 'Course');
  const choose = key => { courseSelect.value = key; courseSelect.dispatchEvent(new w.Event('change')); };
  const status = () => form.querySelector('[role=status]').textContent;
  const alert = () => form.querySelector('[role=alert]').textContent;
  return { w, d, form, calls, saved, choose, status, alert, courseSelect };
}

for (const arabic of [false, true]) test(`(${arabic ? 'ar' : 'en'}) a new course starts on Programme default, which sends nothing; Standard and 2 × 100 min are sent as chosen`, async t => {
  const h = editor(t, { arabic, answer: () => ({ ok: true, budget: [] }) });
  const weekly = control(h.form, arabic ? 'اللقاءات الأسبوعية' : 'Weekly meetings');
  assert.deepEqual(options(weekly), arabic
    ? [['', 'افتراضي البرامج'], ['standard', 'قياسي (٢ × ٧٥ دقيقة)'], ['2x100', '٢ × ١٠٠ دقيقة']]
    : [['', 'Programme default'], ['standard', 'Standard (2 × 75 min)'], ['2x100', '2 × 100 min']]);
  assert.equal(weekly.value, '', 'Programme default is the starting choice');
  const code = control(h.form, arabic ? 'رمز المقرر' : 'Course code');
  const sent = [];
  for (const value of ['', 'standard', '2x100']) {
    code.value = 'stat103';
    weekly.value = value;
    await h.form.onsubmit({ preventDefault() {} });
    sent.push(['meeting_pattern' in h.calls.at(-1).body, h.calls.at(-1).body.meeting_pattern]);
  }
  assert.deepEqual(sent, [[false, undefined], [true, 'standard'], [true, '2x100']]);
  // Another credit hours: back to Programme default, and closed.
  const fresh = editor(t, { arabic });
  const hours = control(fresh.form, arabic ? 'الساعات المعتمدة' : 'Credit hours');
  const choice = control(fresh.form, arabic ? 'اللقاءات الأسبوعية' : 'Weekly meetings');
  choice.value = 'standard';
  hours.value = '4'; hours.dispatchEvent(new fresh.w.Event('input'));
  assert.deepEqual([choice.value, choice.disabled], ['', true]);
});

test('a course in the plan names its programmes’ default, or each vote when they differ, under what it meets now', async t => {
  const h = editor(t);
  const lines = Object.fromEntries(BUDGET.map(row => { h.choose(row.course_key); return [row.course_code, lineAfterNow(h.form)]; }));
  assert.deepEqual(lines, {
    // The programmes that chose it, never the one that did not (CS2).
    STAT103: `Programme default: 2 × 100 min (${iso('AI2')}, ${iso('DS2')})`,
    MATH243: `Programme default: 2 × 100 min (${iso('AI2')}, ${iso('DS2')})`,
    COE226: `Programmes differ: ${iso('AI2')} 2 × 100 min · ${iso('CS2')} standard · ${iso('DS2')} not set`,
    AI333: `Programme default: standard (${iso('CS2')})`,
    AI201: null,
    MGT324: null,
    ENG101: null,
  });
  // A server without the programmes: nothing new is said.
  const old = editor(t, { budget: BUDGET.map(plain) });
  old.choose(STAT103.course_key);
  assert.equal(lineAfterNow(old.form), null);
  assert.equal(button(old.form, 'Use programme default'), null);

  const ar = editor(t, { arabic: true });
  ar.choose(STAT103.course_key);
  assert.equal(lineAfterNow(ar.form), `افتراضي البرامج: ٢ × ١٠٠ دقيقة (${iso('AI2')}، ${iso('DS2')})`);
  ar.choose(COE226.course_key);
  assert.equal(lineAfterNow(ar.form), `البرامج مختلفة: ${iso('AI2')} ٢ × ١٠٠ دقيقة · ${iso('CS2')} قياسي · ${iso('DS2')} غير محدد`);
});

test('“Use programme default” is offered only where the course differs, and saves its programmes’ value with the weekly-meetings endpoint', async t => {
  const h = editor(t);
  const offered = Object.fromEntries(BUDGET.map(row => { h.choose(row.course_key); return [row.course_code, Boolean(button(h.form, 'Use programme default'))]; }));
  assert.deepEqual(offered, { STAT103: true, MATH243: false, COE226: false, AI333: true, AI201: false, MGT324: false, ENG101: false });

  for (const [row, value, words] of [[STAT103, '2x100', '2 × 100 min'], [AI333, '', '2 × 75 min']]) {
    const s = editor(t);
    s.choose(row.course_key);
    const use = button(s.form, 'Use programme default');
    use.focus();
    use.click();
    await wait(() => s.status());
    // Asked for what its programmes decide now, naming the value shown.
    assert.deepEqual(s.calls, [{ url: '/ops/tw/scenarios/42/budget/meeting-pattern/', body: useDefault(row, value) }]);
    assert.equal(s.status(), `Saved: ${iso(row.course_code)} is now set to ${words}.`);
    // The change is the page script's own object (another realm): compared by its entries.
    assert.deepEqual(s.saved.map(item => ({ ...item.change })), [{ action: 'meeting_pattern', course_key: row.course_key }]);
    // Done: no longer offered, and the keyboard is on “Save weekly meetings”.
    assert.equal(button(s.form, 'Use programme default'), null);
    assert.equal(s.d.activeElement, button(s.form, 'Save weekly meetings'));
    assert.equal(button(s.form, 'Save weekly meetings').disabled, false);
  }
});

test('“Use programme default” refused: the server’s words in the alert, still offered, the keyboard back on it; never on a published scenario', async t => {
  const message = 'This scenario is busy: a timetable job holds it.';
  let outside = null;
  const h = editor(t, { answer: () => { outside.focus(); return refusal(message); } });
  outside = h.d.getElementById('opener');
  h.choose(STAT103.course_key);
  const use = button(h.form, 'Use programme default');
  use.focus();
  use.click();
  await wait(() => h.alert());
  await settle();
  assert.deepEqual([h.alert(), h.status(), h.saved.length], [message, '', 0]);
  assert.equal(button(h.form, 'Use programme default'), use);
  assert.equal(use.disabled, false);
  assert.equal(h.d.activeElement, use, 'the keyboard comes back to the button it left');

  const p = editor(t, { published: true });
  p.choose(STAT103.course_key);
  assert.equal(button(p.form, 'Use programme default').disabled, true);
  await button(p.form, 'Use programme default').onclick();
  assert.deepEqual(p.calls, []);
});

for (const arabic of [false, true]) test(`(${arabic ? 'ar' : 'en'}) “Use programme default” gone or changed since the list was read: said in the page’s words, and the course is read again`, async t => {
  // Section Planning set STAT103 back to standard since: it meets its default now.
  const now = { ...STAT103, programme_meeting_pattern: decided('agree', 'standard', [vote('AI2', 'standard'), vote('DS2', 'standard')],
    'Standard (2 × 75 min)', 'قياسي (٢ × ٧٥ دقيقة)'), meeting_pattern_drift: false };
  let reads = 0;
  const h = editor(t, { arabic, answer: () => CHANGED, reread: async () => { reads++; return [now, ...BUDGET.filter(row => row !== STAT103)]; } });
  h.choose(STAT103.course_key);
  const words = arabic ? 'استخدام افتراضي البرامج' : 'Use programme default';
  button(h.form, words).click();
  await wait(() => h.alert());
  await settle();
  assert.equal(h.alert(), arabic ? 'لم يُحفظ: تغيّر افتراضي البرامج له منذ عُرضت القائمة.' : 'Not saved: its programmes’ default changed since the list was drawn.');
  assert.deepEqual([reads, h.saved.length, h.status()], [1, 0, '']);
  // The course as read now: its programmes' standard, nothing more to use.
  assert.equal(lineAfterNow(h.form), arabic ? `افتراضي البرامج: قياسي (${iso('AI2')}، ${iso('DS2')})` : `Programme default: standard (${iso('AI2')}, ${iso('DS2')})`);
  assert.equal(button(h.form, words), null);

  // Any other refusal is the server's words, and nothing is read again.
  const other = editor(t, { arabic, answer: () => refusal('busy'), reread: async () => { reads++; return []; } });
  other.choose(STAT103.course_key);
  button(other.form, words).click();
  await wait(() => other.alert());
  assert.deepEqual([other.alert(), reads], ['busy', 1]);
});

test('the page’s words for each reason a programme default is not used; none for another refusal', async t => {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  dom.window.eval(read('static/js/timetable-course-editor.js'));
  const { refusalWords } = dom.window.TimetableCourseEditor;
  const said = arabic => [CHANGED, unavailable('mixed'), unavailable('none'), unavailable('ambiguous'), refusal('x'), {}].map(reply => refusalWords(reply.error, arabic));
  assert.deepEqual(said(false), ['its programmes’ default changed since the list was drawn.', 'its programmes differ now.', 'no programme sets it now.',
    'two courses share its code, so its programmes’ default cannot be told.', '', '']);
  assert.deepEqual(said(true), ['تغيّر افتراضي البرامج له منذ عُرضت القائمة.', 'برامجه مختلفة الآن.', 'لم يعد أي برنامج يضبطه.',
    'يشترك مقرران في رمزه، فلا يُعرف افتراضي البرامج له.', '', '']);
});

test('the dialog opens on a course of the plan when the page names one; on a new course otherwise', async t => {
  const h = editor(t, { courseKey: AI333.course_key });
  assert.equal(h.courseSelect.value, AI333.course_key);
  assert.equal(lineAfterNow(h.form), `Programme default: standard (${iso('CS2')})`);
  assert.ok(button(h.form, 'Use programme default'));
  const missing = editor(t, { courseKey: 'NOPE101::GONE' });
  assert.equal(missing.courseSelect.value, '');
});

test('an add says the weekly meetings its programmes gave the course, or that they differ; any other add is the Add it was', async t => {
  const cases = [
    [{ value: '2x100', source: 'programmes', status: 'agree' }, false,
      `Added ${iso('STAT103')} — 2 × 100 min from its programmes. Choose the section from Required sections to place it.`],
    [{ value: '2x100', source: 'programmes', status: 'agree' }, true,
      `أُضيف ${iso('STAT103')} — ٢ × ١٠٠ دقيقة حسب برامجه. اختر الشعبة من قائمة الشعب المطلوبة لوضعها.`],
    [{ value: '', source: 'standard', status: 'mixed' }, false,
      `Added ${iso('STAT103')} — its programmes differ, so it keeps its credit hours’ weekly meetings. Choose the section from Required sections to place it.`],
    [{ value: '', source: 'standard', status: 'none' }, false, null],
    [{ value: '2x100', source: 'request', status: 'agree' }, false, null],
    [null, false, null],
    [undefined, false, null],
  ];
  for (const [applied, arabic, message] of cases) {
    const h = editor(t, { arabic, answer: () => ({ ok: true, budget: ['updated'], ...(applied === undefined ? {} : { meeting_pattern_applied: applied }) }) });
    control(h.form, arabic ? 'رمز المقرر' : 'Course code').value = 'stat103';
    await h.form.onsubmit({ preventDefault() {} });
    assert.deepEqual(h.saved.map(item => ({ rows: item.rows, change: item.change && { ...item.change } })),
      [{ rows: ['updated'], change: message ? { action: 'add', course_key: 'STAT103', message } : undefined }], JSON.stringify(applied));
    assert.equal(h.d.querySelector('dialog'), null, 'Add closes the dialog');
  }
});

test('the keyboard is handed back to the page when the control that opened the dialog was drawn again while it was open', async t => {
  for (const [redrawn, expected] of [[false, 0], [true, 1]]) {
    const dom = new JSDOM('<!doctype html><body><button id="opener">STAT103</button></body>', { runScripts: 'outside-only' });
    t.after(() => dom.window.close());
    const w = dom.window, d = w.document;
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
    w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); this.dispatchEvent(new w.Event('close')); };
    w.eval(read('static/js/timetable-course-editor.js'));
    let asked = 0;
    d.getElementById('opener').focus();
    w.TimetableCourseEditor.open({ scenarioId: 42, published: false, budget: BUDGET, courseKey: STAT103.course_key,
      request: async () => null, onSaved: async () => {}, returnFocus: () => { asked++; } });
    // The page drew its line again while the dialog was open: the opener is gone.
    if (redrawn) d.getElementById('opener').remove();
    [...d.querySelectorAll('dialog button')].find(item => item.textContent === 'Close').click();
    assert.deepEqual([asked, d.querySelector('dialog')], [expected, null], redrawn ? 'opener gone' : 'opener still there');
  }
});

/* ── The weekly-meetings line, drawn by the course editor's module ── */
function line(t, rows, options = {}) {
  const dom = new JSDOM('<!doctype html><body><div id="box" hidden></div><p id="after" tabindex="-1"></p></body>', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, opened = [], applied = [];
  w.eval(read('static/js/timetable-course-editor.js'));
  const box = d.getElementById('box');
  const draw = (list = rows, more = {}) => w.TimetableCourseEditor.renderMeetingSummary(box, list, {
    onOpen: key => opened.push(key), onApply: drift => applied.push(Array.from(drift, row => row.course_key)), fallback: d.getElementById('after'), ...options, ...more,
  });
  const apply = draw();
  return { w, d, box, draw, apply, opened, applied, text: () => box.querySelector('p')?.textContent ?? null };
}

test('the line names the courses in their programmes’ shape, those whose programmes differ and those that differ, then offers their defaults', async t => {
  const h = line(t, [...BUDGET, { ...COE226, course_code: 'COE227', course_key: 'COE227::BUSES', meeting_pattern: '2x100', ...as2x100 }]);
  assert.equal(h.box.hidden, false);
  assert.equal(h.text(), 'Weekly meetings: MATH243 2 × 100 min (programme default) · Programmes differ: COE226 (kept 2 × 75 min), '
    + 'COE227 (set here to 2 × 100 min) · Differs from programme default: AI333, STAT103');
  assert.equal(h.apply.textContent, 'Use programme defaults (2)');
  assert.deepEqual([...h.box.querySelectorAll('button[data-summary-key]:not([data-summary-apply])')].map(item => [item.textContent, item.getAttribute('aria-label')]), [
    ['MATH243', 'MATH243 Linear Algebra: its weekly meetings'], ['COE226', 'COE226 Digital Logic: its weekly meetings'],
    ['COE227', 'COE227 Digital Logic: its weekly meetings'], ['AI333', 'AI333 Planning: its weekly meetings'], ['STAT103', 'STAT103 Probability: its weekly meetings']]);
  // A course opens the dialog on it; the apply runs the courses that differ, as shown.
  button(h.box, 'COE226').click();
  h.apply.click();
  assert.deepEqual([h.opened, h.applied], [[COE226.course_key], [[AI333.course_key, STAT103.course_key]]]);

  const ar = line(t, BUDGET, { arabic: true });
  assert.equal(ar.text(), 'اللقاءات الأسبوعية: MATH243 ٢ × ١٠٠ دقيقة (افتراضي البرامج) · البرامج مختلفة: COE226 (بقي على ٢ × ٧٥ دقيقة) · يختلف عن افتراضي البرامج: AI333، STAT103');
  assert.equal(ar.apply.textContent, 'استخدام افتراضي البرامج (2)');
});

test('nothing at all when no course has anything to say: programmes unset, two courses behind a code, an English bundle, or an older server', async t => {
  for (const rows of [[AI201, MGT324, ENG101], BUDGET.map(plain), [], [{ ...STAT103, meeting_pattern_choices: [''] }]]) {
    const h = line(t, rows);
    assert.deepEqual([h.box.hidden, h.box.childNodes.length, h.apply], [true, 0, null]);
  }
  // Only what is said: no apply button without a course that differs.
  const quiet = line(t, [MATH243, COE226]);
  assert.equal(quiet.box.querySelector('[data-summary-apply]'), null);
  assert.equal(quiet.text(), 'Weekly meetings: MATH243 2 × 100 min (programme default) · Programmes differ: COE226 (kept 2 × 75 min)');
});

test('the line is drawn again only when what it says changed, and the keyboard stays on the same course', async t => {
  const h = line(t, BUDGET);
  const stat = button(h.box, 'STAT103');
  stat.focus();
  h.draw([...BUDGET]);
  assert.equal(button(h.box, 'STAT103'), stat, 'the same nodes: nothing changed');
  // Published now: the apply is turned off where it is.
  h.draw(BUDGET, { disabled: true });
  assert.deepEqual([button(h.box, 'STAT103'), h.box.querySelector('[data-summary-apply]').disabled], [stat, true]);
  // AI333 meets its default now: drawn again, the keyboard on STAT103's new button.
  h.draw(BUDGET.map(row => (row === AI333 ? { ...row, meeting_pattern: '', meeting_durations: [75, 75], meeting_pattern_drift: false } : row)));
  assert.notEqual(button(h.box, 'STAT103'), stat);
  assert.equal(h.d.activeElement, button(h.box, 'STAT103'));
  // Nothing left to say: the box hides, and the keyboard goes to the page's fallback.
  h.d.activeElement.blur();
  button(h.box, 'MATH243').focus();
  h.draw([AI201]);
  assert.deepEqual([h.box.hidden, h.d.activeElement.id], [true, 'after']);
});

/* ── The classic page ── */
const scenario = { id: 1, name: 'Programme defaults', status: 'draft', slot_config: [{ label: '09:00', start: '09:00', end: '10:15' }] };
const board = { id: 2, label: 'Term 7', nominal_term: 7, primary_count: 1 };
const at = (id, extra = {}) => ({ id, course_code: `AI${id}`, course_key: `AI${id}`, section: 'S1', term_section_id: id + 100, day: 'SUN',
  start_time: '09:00', end_time: '10:15', room: 'R1', meetings: [], available_capacity: 24, needs_room: true, room_state: 'assigned',
  is_online: false, shared_with_other_scenario: false, ...extra });
const quality = { ok: true, scenario_id: 1, instructor: { assessed: true, working_days: 1, idle_minutes: 0 }, soft_seats: { assessed: true, unresolved: 0 },
  rooms: { assessed: true, shortages: 0, details: [] }, assigned_student_clashes: { assessed: true, count: 0 } };
const quiet = { summary: { critical: 0, warning: 0 }, overlaps: [], instructor_clashes: [], room_clashes: [], cross_board_conflicts: [], hard_overlap_threshold: 20 };
const READY = { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] };

async function classic(t, { arabic = false, budget = () => BUDGET, save = body => savedAnswer(BUDGET, body), status = 'draft', add = null } = {}) {
  const template = read('core/templates/core/timetable_workspace.html');
  const html = template.replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost/timetable-workspace/' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [];
  w.addEventListener('error', e => errors.push(e.error));
  t.after(() => assert.deepEqual(errors, []));
  w.eval(read('static/js/shared-utils.js'));
  w.LANGUAGE_CODE = arabic ? 'ar' : 'en';
  d.documentElement.lang = arabic ? 'ar' : 'en';
  w._injectCsrf = x => x;
  w.esc = esc;
  w.debounce = fn => fn;
  w.notify = Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => notices.push({ type, text })]));
  w.dlg = { confirm: async () => false };
  w.Element.prototype.scrollIntoView = () => {};
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); this.dispatchEvent(new w.Event('close')); };
  const shownScenario = { ...scenario, status };
  w.fetch = async (url, opts = {}) => {
    requests.push({ url, opts });
    let body;
    if (url === '/ops/tw/scenarios/1/budget/meeting-pattern/' && opts.method === 'POST') body = await save(JSON.parse(opts.body));
    else if (url === '/ops/tw/scenarios/1/budget/' && opts.method === 'POST' && add) body = add(JSON.parse(opts.body));
    else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [shownScenario] };
    else if (url === '/ops/tw/scenarios/1/') body = { scenario: shownScenario, active_job: null };
    else if (url.includes('/quality/')) body = quality;
    else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
    else if (url === '/ops/tw/boards/2/') body = { placements: [at(10)], slot_config: scenario.slot_config };
    else if (url.includes('/conflicts/')) body = quiet;
    else if (url.includes('/readiness/')) body = { readiness: READY };
    else if (url === '/ops/tw/scenarios/1/budget/') body = { budget: budget() };
    else if (url.includes('/capacity/')) body = { courses: [], totals: {} };
    else if (url.includes('/unplaced/')) body = { unplaced: [] };
    if (body === undefined) assert.fail(`Unexpected request ${url}`);
    if (body.httpError) return { ok: false, status: body.httpError, json: async () => body };
    return { ok: true, status: 200, json: async () => body };
  };
  d.getElementById('twYear').value = '1448'; d.getElementById('twTerm').value = '1';
  for (const file of ['timetable-meeting-policy', 'timetable-slot-config', 'timetable-section-teacher', 'timetable-programme-mark',
    'timetable-room-choice', 'timetable-hotspots', 'ux-controls', 'timetable-course-editor']) w.eval(read(`static/js/${file}.js`));
  w.eval(read('static/js/page-timetable-workspace.js'));
  await wait(() => d.getElementById('twScenario').options.length === 2);
  d.getElementById('twScenario').value = '1';
  await w.onScenarioChange();
  await wait(() => w.TW.loadedBoardId === 2 && d.querySelector('#twSectionList .tw-section-item')
    && d.querySelector('.tw-workspace').dataset.busy === 'false');
  const row = key => d.querySelector(`#twSectionList [data-focus-key="place:${key}|S1"]`) || d.querySelector(`#twSectionList [data-focus-key="place:${key}|complete"]`);
  const facts = key => [...row(key).querySelectorAll('.meta.ux-meta')].map(meta => [...meta.children].map(span => span.textContent));
  const summary = () => d.getElementById('twMeetingSummary');
  const posts = () => requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/meeting-pattern/').map(item => JSON.parse(item.opts.body));
  return { w, d, requests, notices, row, facts, summary, posts };
}

for (const arabic of [false, true]) test(`(${arabic ? 'ar' : 'en'}) classic list: a chip where a course differs or its programmes differ, on rows to place and rows placed in full; the department as it was`, async t => {
  const complete = { ...STAT103, course_code: 'STAT104', course_key: 'STAT104::PLACED', used_sections: 1, used_section_labels: ['S1'], remaining_sections: 0 };
  const h = await classic(t, { arabic, budget: () => [...BUDGET, complete] });
  const facts = Object.fromEntries([...BUDGET, complete].map(c => [c.course_code, h.facts(c.course_key)]));
  const [cr, cap, meets, placed, demand] = arabic ? ['3 س', 'سعة 40', 'لقاءان أسبوعياً', '1/1 موضوعة', '30 طالب'] : ['3 cr', 'cap 40', '2 meetings a week', '1/1 placed', 'demand 30'];
  const two = arabic ? '٢ × ١٠٠ دقيقة' : '2 × 100 min';
  assert.deepEqual(facts.STAT103[0], ['MATH', cr, arabic ? 'افتراضي البرامج ٢ × ١٠٠ دقيقة' : 'programme default 2 × 100 min', cap, meets]);
  assert.deepEqual(facts.MATH243[0], ['MATH', cr, two, cap, meets], 'in its programmes’ shape: nothing new');
  assert.deepEqual(facts.COE226[0], ['COE', cr, arabic ? 'البرامج مختلفة' : 'programmes differ', cap, meets]);
  assert.deepEqual(facts.AI333[0], ['AI', cr, two, arabic ? 'افتراضي البرامج قياسي' : 'programme default standard', cap, meets]);
  assert.deepEqual(facts.AI201[0], ['AI', cr, two, cap, meets], 'nobody set it: the scenario’s own 2 × 100 says nothing new');
  assert.deepEqual(facts.ENG101[0].slice(0, 3), ['ENG', cr, cap]);
  // Placed in full: the chip after its counts.
  assert.deepEqual(facts.STAT104, [[placed, demand, arabic ? 'افتراضي البرامج ٢ × ١٠٠ دقيقة' : 'programme default 2 × 100 min']]);
  const old = await classic(t, { arabic, budget: () => [complete, ...BUDGET].map(plain) });
  assert.deepEqual(old.facts('STAT104::PLACED'), [[placed, demand]], 'a course with no programme decision: as before');
  assert.deepEqual(old.facts(STAT103.course_key)[0], ['MATH', cr, cap, meets]);
});

test('classic: the weekly-meetings line sits over the list, a course in it opens the dialog on that course; none for a scenario whose programmes set nothing', async t => {
  const h = await classic(t);
  const box = h.summary();
  assert.equal(box.nextElementSibling.id, 'twSectionList');
  assert.equal(box.hidden, false);
  assert.equal(box.querySelector('p').textContent, 'Weekly meetings: MATH243 2 × 100 min (programme default) · Programmes differ: COE226 (kept 2 × 75 min) · Differs from programme default: AI333, STAT103');
  button(box, 'COE226').click();
  const form = h.d.querySelector(EDITOR);
  assert.equal(control(form, 'Course').value, COE226.course_key);
  assert.equal(lineAfterNow(form), `Programmes differ: ${iso('AI2')} 2 × 100 min · ${iso('CS2')} standard · ${iso('DS2')} not set`);

  const none = await classic(t, { budget: () => BUDGET.map(plain) });
  assert.deepEqual([none.summary().hidden, none.summary().childNodes.length], [true, 0]);
});

test('classic: “Use programme defaults” saves each course in turn, reads the list again and says it; the line then says they meet their defaults', async t => {
  let budget = BUDGET;
  let release = null;
  const h = await classic(t, {
    budget: () => budget,
    save: async body => {
      if (!release) await new Promise(resolve => { release = resolve; });
      const answer = savedAnswer(budget, body);
      budget = answer.budget;
      return answer;
    },
  });
  const reads = h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length;
  const apply = button(h.summary(), 'Use programme defaults (2)');
  assert.equal(apply.disabled, false);
  apply.click();
  await wait(() => h.posts().length === 1);
  for (let i = 0; i < 6; i++) await settle();
  assert.equal(h.posts().length, 1, 'one at a time: the next waits for this answer');
  assert.equal(apply.disabled, true, 'the page is busy while it runs');
  release();
  await wait(() => h.notices.length === 1);
  assert.deepEqual(h.posts(), [useDefault(AI333, ''), useDefault(STAT103, '2x100')]);
  assert.deepEqual(h.notices, [{ type: 'success', text: '2 courses now meet as their programmes set them.' }]);
  assert.equal(h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length, reads + 1, 'the list is read again once');
  assert.equal(h.summary().querySelector('p').textContent, 'Weekly meetings: MATH243, STAT103 2 × 100 min (programme default) · Programmes differ: COE226 (kept 2 × 75 min)');
  assert.equal(button(h.summary(), 'Use programme defaults (2)'), null);
});

for (const arabic of [false, true]) test(`(${arabic ? 'ar' : 'en'}) classic: “Use programme defaults” stops at the first refusal and says which course and why; the ones before it stay saved`, async t => {
  const message = 'STAT103 is not in this scenario’s section budget.';
  let budget = BUDGET;
  const h = await classic(t, {
    arabic,
    budget: () => budget,
    save: body => {
      if (body.course_key === STAT103.course_key) return { httpError: 404, ok: false, error: { code: 'NOT_FOUND', message } };
      const answer = savedAnswer(budget, body);
      budget = answer.budget;
      return answer;
    },
  });
  // Three that differ: the third is never sent.
  budget = [...BUDGET, { ...STAT103, course_code: 'STAT105', course_key: 'STAT105::SAMPLING' }];
  await h.w.loadBudget();
  button(h.summary(), arabic ? 'استخدام افتراضي البرامج (3)' : 'Use programme defaults (3)').click();
  await wait(() => h.notices.length === 1);
  assert.deepEqual(h.posts().map(body => body.course_key), [AI333.course_key, STAT103.course_key]);
  assert.deepEqual(h.notices, [{ type: 'warning', text: arabic
    ? `توقف عند ${iso('STAT103')}: ${message} ضُبط قبله 1 من 3.`
    : `Stopped at ${iso('STAT103')}: ${message} 1 of 3 set before it.` }]);
  assert.equal(button(h.summary(), arabic ? 'استخدام افتراضي البرامج (2)' : 'Use programme defaults (2)').disabled, false, 'offered again for the two left');
});

// The placed sections the saves leave meeting the old way (their `mismatched_sections`).
const LEFT = {
  [AI333.course_key]: [{ course_code: 'AI333', section: 'S1', locked: true, externally_owned: false },
    { course_code: 'AI333', section: 'S2', locked: false, externally_owned: true }],
  [STAT103.course_key]: [{ course_code: 'STAT103', section: 'S3', locked: false, externally_owned: false }],
};
const leftWords = arabic => (arabic
  ? `عدد المقررات التي صارت تجتمع كما ضبطتها برامجها: 2. عدد الشعب الموضوعة التي ما زالت تجتمع بالطريقة السابقة: 3 (${iso('AI333 S1')}، ${iso('AI333 S2')}، ${iso('STAT103 S3')}). `
    + `احذفها ثم شغّل «تحسين» (أو «إعادة بناء كاملة») لتوضع بلقاءاتها الأسبوعية الجديدة. مقفلة (ألغِ قفلها قبل الحذف): ${iso('AI333 S1')}. يملكها سيناريو آخر (عدّلها هناك): ${iso('AI333 S2')}.`
  : `2 courses now meet as their programmes set them. 3 placed sections still meet the old way: ${iso('AI333 S1')}, ${iso('AI333 S2')}, ${iso('STAT103 S3')}. `
    + `Remove them and run Optimise (or Full rebuild) to place them in their new weekly meetings. Locked (unlock before removing): ${iso('AI333 S1')}. Owned by another scenario (change there): ${iso('AI333 S2')}.`);

for (const arabic of [false, true]) test(`(${arabic ? 'ar' : 'en'}) classic: “Use programme defaults” names the placed sections its saves left meeting the old way, as a warning`, async t => {
  let budget = BUDGET;
  const h = await classic(t, {
    arabic,
    budget: () => budget,
    save: body => { const answer = savedAnswer(budget, body); budget = answer.budget; return { ...answer, mismatched_sections: LEFT[body.course_key] }; },
  });
  button(h.summary(), arabic ? 'استخدام افتراضي البرامج (2)' : 'Use programme defaults (2)').click();
  await wait(() => h.notices.length === 1);
  assert.deepEqual(h.notices, [{ type: 'warning', text: leftWords(arabic) }]);
});

for (const [reply, why] of [[CHANGED, 'its programmes’ default changed since the list was drawn.'], [unavailable('mixed'), 'its programmes differ now.']]) {
  test(`classic: “Use programme defaults” refused for a programme default ${reply.error.code === 'PROGRAMME_DEFAULT_CHANGED' ? 'changed' : 'gone'} since the list was read stops there, says why in the page’s words and reads the list again`, async t => {
    let budget = BUDGET;
    const h = await classic(t, {
      budget: () => budget,
      save: body => {
        if (body.course_key === STAT103.course_key) return reply;
        const answer = savedAnswer(budget, body);
        budget = answer.budget;
        return { ...answer, mismatched_sections: LEFT[body.course_key] };
      },
    });
    const reads = h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length;
    button(h.summary(), 'Use programme defaults (2)').click();
    await wait(() => h.notices.length === 1);
    assert.deepEqual(h.posts(), [useDefault(AI333, ''), useDefault(STAT103, '2x100')]);
    assert.deepEqual(h.notices, [{ type: 'warning', text: `Stopped at ${iso('STAT103')}: ${why} 1 of 2 set before it. 2 placed sections still meet the old way: `
      + `${iso('AI333 S1')}, ${iso('AI333 S2')}. Remove them and run Optimise (or Full rebuild) to place them in their new weekly meetings. `
      + `Locked (unlock before removing): ${iso('AI333 S1')}. Owned by another scenario (change there): ${iso('AI333 S2')}.` }]);
    assert.equal(h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length, reads + 1, 'the list is read again');
  });
}

test('classic: the dialog’s refused programme default reads the list again, and the dialog shows the course as read now', async t => {
  let budget = BUDGET;
  const h = await classic(t, { budget: () => budget, save: () => CHANGED });
  button(h.summary(), 'STAT103').click();
  const form = h.d.querySelector(EDITOR);
  const reads = h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length;
  // Section Planning set it back to standard since: no longer different.
  budget = BUDGET.map(row => (row === STAT103 ? { ...row, programme_meeting_pattern: AGREE_STANDARD, meeting_pattern_drift: false } : row));
  button(form, 'Use programme default').click();
  await wait(() => form.querySelector('[role=alert]').textContent);
  await wait(() => button(form, 'Use programme default') === null);
  assert.equal(form.querySelector('[role=alert]').textContent, 'Not saved: its programmes’ default changed since the list was drawn.');
  assert.equal(h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length, reads + 1);
  assert.equal(lineAfterNow(form), `Programme default: standard (${iso('CS2')})`);
  // The page's line was drawn again from the list as read now.
  assert.equal(button(h.summary(), 'Use programme defaults (1)').textContent, 'Use programme defaults (1)');
  assert.deepEqual(h.notices, []);
});

for (const [applied, said] of [
  [{ value: '2x100', source: 'programmes', status: 'agree' }, `Added ${iso('STAT109')} — 2 × 100 min from its programmes. Choose the section from Required sections to place it.`],
  [{ value: '', source: 'standard', status: 'none' }, 'Added. Choose the section from Required sections to place it.'],
]) test(`classic: an add ${applied.source === 'programmes' ? 'names the weekly meetings its programmes gave the course' : 'its programmes said nothing about is the Added it was'}`, async t => {
  const h = await classic(t, { add: () => ({ ok: true, budget: BUDGET, meeting_pattern_applied: applied }) });
  h.d.getElementById('twAddCourse').click();
  const form = h.d.querySelector(EDITOR);
  control(form, 'Course code').value = 'stat109';
  await form.onsubmit({ preventDefault() {} });
  await wait(() => h.notices.length === 1);
  assert.deepEqual(h.notices, [{ type: 'success', text: said }]);
});

test('classic: on a published scenario the line is said, its “Use programme defaults” is off, and stays off when the list is drawn again', async t => {
  const h = await classic(t, { status: 'published' });
  assert.equal(button(h.summary(), 'Use programme defaults (2)').disabled, true);
  assert.ok(button(h.summary(), 'STAT103'));
  // Filtering the list draws it (and the line) again, with no other sync after it.
  const search = h.d.getElementById('twSectionSearch');
  search.value = 'STAT';
  search.dispatchEvent(new h.w.Event('input'));
  await settle();
  assert.equal(button(h.summary(), 'Use programme defaults (2)').disabled, true);
});

/* ── The split page ── */
const TERM7 = { id: 2, label: 'Term 7', nominal_term: 7, primary_count: 1, placement_count: 0 };
async function split(t, { arabic = false, budget = () => BUDGET, save = body => savedAnswer(BUDGET, body), status = 'draft', add = null, route = () => undefined } = {}) {
  const html = read('core/templates/core/timetable_workspace_split.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://split.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [];
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  Object.assign(w, { LANGUAGE_CODE: arabic ? 'ar' : 'en', djCsrfToken: 'audit', INITIAL_SCENARIO: '', INITIAL_BOARD: '', DEFAULT_YEAR: '1448', DEFAULT_TERM: '1' });
  w.esc = esc;
  w.notify = Object.fromEntries(['success', 'warning', 'error', 'info'].map(kind => [kind, message => notices.push({ kind, message })]));
  w.console.error = () => {};
  w.matchMedia = () => ({ matches: false });
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); this.dispatchEvent(new w.Event('close')); };
  w.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    let body = await route(url, options);
    if (body !== undefined) { /* the test's own answer */ }
    else if (url === '/ops/tw/scenarios/1/budget/meeting-pattern/' && options.method === 'POST') body = await save(JSON.parse(options.body));
    else if (url === '/ops/tw/scenarios/1/budget/' && options.method === 'POST' && add) body = add(JSON.parse(options.body));
    else if (url === '/ops/tw/scenarios/1/budget/') body = { budget: budget() };
    else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
    else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [TERM7] };
    else if (url.endsWith('/conflicts/')) body = { overlaps: [], instructor_clashes: [], room_clashes: [] };
    else body = { ok: true, candidates: [], instructors: [], summary: {} };
    return { ok: !body.httpError, status: body.httpError || 200, headers: { get: () => null }, json: async () => body };
  };
  w.eval(read('static/js/shared-utils.js'));
  for (const file of ['timetable-meeting-policy', 'timetable-slot-config', 'timetable-section-teacher', 'timetable-programme-mark', 'timetable-course-editor']) {
    w.eval(read(`static/js/${file}.js`));
  }
  w.eval(`${read('static/js/page-timetable-workspace-split.js')}\n;globalThis.__splitAudit = { S, SB, COPY_ACTIVITY };`);
  await settle();
  const { S: state, SB: sidebar, COPY_ACTIVITY: activity } = w.__splitAudit;
  state.scenarioId = 1; state.scenarioMeta = { id: 1, status };
  sidebar.budget = budget();
  w.renderSidebar();
  const card = key => d.querySelector(`.tws-sec-item[data-key="${key}"]`);
  const chips = key => [...card(key).querySelectorAll('.meta .dem')].map(span => span.textContent);
  const summary = () => d.getElementById('twsMeetingSummary');
  const posts = () => requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/meeting-pattern/').map(item => JSON.parse(item.options.body));
  return { w, d, requests, notices, state, sidebar, activity, card, chips, summary, posts };
}

for (const arabic of [false, true]) test(`(${arabic ? 'ar' : 'en'}) split sidebar: the chip where a course differs or its programmes differ, placed in full or not; its department line as it was`, async t => {
  const exhausted = { ...STAT103, course_code: 'STAT104', course_key: 'STAT104::PLACED', used_sections: 1, remaining_sections: 0 };
  const h = await split(t, { arabic, budget: () => [...BUDGET, exhausted] });
  const [stu, cap] = arabic ? ['30 طالب', 'حد 40'] : ['30 stu', 'cap 40'];
  const two = arabic ? '٢ × ١٠٠ دقيقة' : '2 × 100 min';
  assert.deepEqual(h.chips(STAT103.course_key), [stu, '3h', arabic ? 'افتراضي البرامج ٢ × ١٠٠ دقيقة' : 'programme default 2 × 100 min', cap]);
  assert.deepEqual(h.chips(MATH243.course_key), [stu, '3h', two, cap]);
  assert.deepEqual(h.chips(COE226.course_key), [stu, '3h', arabic ? 'البرامج مختلفة' : 'programmes differ', cap]);
  assert.deepEqual(h.chips(AI333.course_key), [stu, '3h', two, arabic ? 'افتراضي البرامج قياسي' : 'programme default standard', cap]);
  assert.deepEqual(h.chips(AI201.course_key), [stu, '3h', two, cap]);
  assert.ok(h.card('STAT104::PLACED').classList.contains('exhausted'));
  assert.deepEqual(h.chips('STAT104::PLACED'), [stu, '3h', arabic ? 'افتراضي البرامج ٢ × ١٠٠ دقيقة' : 'programme default 2 × 100 min', cap]);
  assert.equal(h.card(STAT103.course_key).querySelector('.meta').textContent.trim(), 'MATH · T7');
  assert.equal(h.card(STAT103.course_key).getAttribute('title'), 'MATH · 3h');
});

test('split: the weekly-meetings line over the sidebar opens the dialog on a course, and “Use programme defaults” saves each in turn, then reads the sidebar again', async t => {
  let budget = BUDGET;
  const h = await split(t, { budget: () => budget, save: body => { const answer = savedAnswer(budget, body); budget = answer.budget; return answer; } });
  const box = h.summary();
  assert.equal(box.nextElementSibling.id, 'twsSidebarBody');
  assert.equal(box.querySelector('p').textContent, 'Weekly meetings: MATH243 2 × 100 min (programme default) · Programmes differ: COE226 (kept 2 × 75 min) · Differs from programme default: AI333, STAT103');
  button(box, 'STAT103').click();
  const form = h.d.querySelector(EDITOR);
  assert.equal(control(form, 'Course').value, STAT103.course_key);
  assert.ok(button(form, 'Use programme default'));
  [...form.querySelectorAll('button')].find(item => item.textContent === 'Close').click();

  const reads = h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length;
  const apply = button(box, 'Use programme defaults (2)');
  assert.equal(apply.disabled, false);
  apply.click();
  await wait(() => h.notices.length === 1);
  assert.deepEqual(h.posts(), [useDefault(AI333, ''), useDefault(STAT103, '2x100')]);
  assert.deepEqual(h.notices, [{ kind: 'success', message: '2 courses now meet as their programmes set them.' }]);
  assert.equal(h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length, reads + 1);
  assert.equal(box.querySelector('p').textContent, 'Weekly meetings: MATH243, STAT103 2 × 100 min (programme default) · Programmes differ: COE226 (kept 2 × 75 min)');
  assert.deepEqual(h.chips(STAT103.course_key), ['30 stu', '3h', '2 × 100 min', 'cap 40']);
});

test('split: a refusal stops “Use programme defaults” and is said; on a published scenario it is off; no line for programmes that set nothing', async t => {
  const message = 'Cannot modify published scenario.';
  const h = await split(t, { save: body => (body.course_key === AI333.course_key ? { httpError: 400, ok: false, error: { code: 'SCENARIO_PUBLISHED', message } } : savedAnswer(BUDGET, body)) });
  button(h.summary(), 'Use programme defaults (2)').click();
  await wait(() => h.notices.length === 1);
  assert.deepEqual(h.posts().map(body => body.course_key), [AI333.course_key]);
  assert.deepEqual(h.notices, [{ kind: 'warning', message: `Stopped at ${iso('AI333')}: ${message} 0 of 2 set before it.` }]);

  const p = await split(t, { status: 'published' });
  assert.equal(button(p.summary(), 'Use programme defaults (2)').disabled, true);
  const none = await split(t, { budget: () => BUDGET.map(plain) });
  assert.deepEqual([none.summary().hidden, none.summary().childNodes.length], [true, 0]);
});

test('split: “Use programme defaults” is off while it runs, then on again for what is left', async t => {
  let release = null;
  const h = await split(t, {
    save: async body => {
      if (!release) await new Promise(resolve => { release = resolve; });
      return body.course_key === AI333.course_key ? savedAnswer(BUDGET, body) : refusal('busy');
    },
  });
  const apply = button(h.summary(), 'Use programme defaults (2)');
  apply.click();
  await wait(() => h.posts().length === 1);
  assert.equal(apply.disabled, true, 'off while it runs');
  apply.click();
  await settle();
  assert.equal(h.posts().length, 1, 'a second click runs nothing');
  release();
  await wait(() => h.notices.length === 1);
  assert.equal(button(h.summary(), 'Use programme defaults (2)').disabled, false, 'on again: the sidebar still differs from both');
});

for (const arabic of [false, true]) test(`(${arabic ? 'ar' : 'en'}) split: “Use programme defaults” names the placed sections its saves left meeting the old way, as a warning`, async t => {
  let budget = BUDGET;
  const h = await split(t, {
    arabic,
    budget: () => budget,
    save: body => { const answer = savedAnswer(budget, body); budget = answer.budget; return { ...answer, mismatched_sections: LEFT[body.course_key] }; },
  });
  button(h.summary(), arabic ? 'استخدام افتراضي البرامج (2)' : 'Use programme defaults (2)').click();
  await wait(() => h.notices.length === 1);
  assert.deepEqual(h.notices, [{ kind: 'warning', message: leftWords(arabic) }]);
});

test('split: a programme default changed since the sidebar was read stops the run in the page’s words', async t => {
  const h = await split(t, { save: () => CHANGED });
  button(h.summary(), 'Use programme defaults (2)').click();
  await wait(() => h.notices.length === 1);
  assert.deepEqual(h.notices, [{ kind: 'warning', message: `Stopped at ${iso('AI333')}: its programmes’ default changed since the list was drawn. 0 of 2 set before it.` }]);
});

test('split: choosing another scenario draws the line empty at once, before anything of the next one is read', async t => {
  let gate = null;
  const h = await split(t, {
    route: async url => {
      if (url !== '/ops/tw/scenarios/2/') return undefined;
      await new Promise(resolve => { gate = resolve; });
      return { httpError: 404, ok: false, error: { code: 'NOT_FOUND', message: 'gone' } };
    },
  });
  assert.ok(button(h.summary(), 'Use programme defaults (2)'));
  const select = h.d.getElementById('twsScenario');
  select.add(new h.w.Option('Scenario 2', '2'));
  select.value = '2';
  const loading = h.w.onScenarioChange();
  await wait(() => gate);
  assert.deepEqual([h.summary().hidden, h.summary().querySelector('button')], [true, null], 'scenario 1’s line is gone while 2 loads');
  gate();
  await loading;
  // No scenario chosen: empty too.
  h.sidebar.budget = BUDGET; h.w.renderSidebar();
  assert.ok(button(h.summary(), 'Use programme defaults (2)'));
  select.value = '';
  await h.w.onScenarioChange();
  assert.deepEqual([h.summary().hidden, h.summary().querySelector('button')], [true, null]);
});

for (const arabic of [false, true]) test(`(${arabic ? 'ar' : 'en'}) split: the dialog’s refused programme default reads the sidebar again, and the dialog shows the course as read now`, async t => {
  let budget = BUDGET;
  const h = await split(t, { arabic, budget: () => budget, save: () => unavailable('mixed') });
  button(h.summary(), 'STAT103').click();
  const form = h.d.querySelector(EDITOR);
  const reads = h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length;
  // DS2 chose standard since: its programmes differ now.
  budget = BUDGET.map(row => (row === STAT103 ? { ...row, programme_meeting_pattern: MIXED, meeting_pattern_drift: false } : row));
  const words = arabic ? 'استخدام افتراضي البرامج' : 'Use programme default';
  button(form, words).click();
  await wait(() => button(form, words) === null);
  assert.equal(form.querySelector('[role=alert]').textContent, arabic ? 'لم يُحفظ: برامجه مختلفة الآن.' : 'Not saved: its programmes differ now.');
  assert.equal(h.requests.filter(item => item.url === '/ops/tw/scenarios/1/budget/').length, reads + 1);
  assert.equal(lineAfterNow(form), arabic
    ? `البرامج مختلفة: ${iso('AI2')} ٢ × ١٠٠ دقيقة · ${iso('CS2')} قياسي · ${iso('DS2')} غير محدد`
    : `Programmes differ: ${iso('AI2')} 2 × 100 min · ${iso('CS2')} standard · ${iso('DS2')} not set`);
  assert.deepEqual(h.notices, []);
});

test('split: a line drawn for one scenario never acts on another', async t => {
  const h = await split(t);
  const apply = button(h.summary(), 'Use programme defaults (2)');
  // The page moved to scenario 2 without drawing the line again.
  h.state.scenarioId = 2;
  apply.click();
  await settle();
  assert.deepEqual(h.requests.filter(item => item.url.includes('/meeting-pattern/')), []);
  assert.deepEqual(h.notices, []);
});

test('split: a run that ends after the page moved to another scenario says nothing there and reads nothing of it', async t => {
  let release = null;
  const h = await split(t, {
    save: async body => {
      if (!release) await new Promise(resolve => { release = resolve; });
      return savedAnswer(BUDGET, body);
    },
  });
  let restarted = 0;
  h.w.restartCreateSectionAssist = () => { restarted++; return true; };
  Object.assign(h.state.createAssist, { active: true, payload: { course_key: STAT103.course_key } });
  button(h.summary(), 'Use programme defaults (2)').click();
  await wait(() => release);
  h.state.scenarioId = 2;
  h.sidebar.budget = [];
  release();
  await wait(() => h.posts().length === 2);
  for (let i = 0; i < 6; i++) await settle();
  assert.deepEqual(h.posts(), [useDefault(AI333, ''), useDefault(STAT103, '2x100')], 'scenario 1’s run finishes where it started');
  assert.deepEqual([h.notices, restarted], [[], 0]);
  assert.deepEqual(h.requests.filter(item => item.url.startsWith('/ops/tw/scenarios/2/')), []);
});

test('split: “Use programme defaults” is off while the page’s own optimise job runs, and on again after it', async t => {
  let finish = null;
  const h = await split(t, {
    route: async url => {
      if (url === '/ops/tw/scenarios/1/optimise-v2/') return { ok: true, job_id: 'job-7', seed: 3 };
      if (url === '/planner-jobs/job-7/') {
        await new Promise(resolve => { finish = resolve; });
        return { status: 'failed', error_message: 'stopped' };
      }
      return undefined;
    },
  });
  h.w.confirm = () => true;
  // The scenario read again after the job: the same sidebar (this test's own).
  h.w.onScenarioChange = async () => true;
  assert.equal(button(h.summary(), 'Use programme defaults (2)').disabled, false);
  const running = h.w.doOptimise('current');
  await wait(() => finish);
  assert.equal(h.activity.optimising, true);
  const apply = button(h.summary(), 'Use programme defaults (2)');
  assert.equal(apply.disabled, true, 'off while the job runs');
  await h.w.applySplitProgrammeDefaults([AI333]);
  assert.deepEqual(h.posts(), [], 'nothing is sent while the job holds the scenario');
  finish();
  await running;
  assert.equal(h.activity.optimising, false);
  assert.equal(button(h.summary(), 'Use programme defaults (2)').disabled, false, 'on again after it');
});

test('split: an add names the weekly meetings its programmes gave the course', async t => {
  const h = await split(t, { arabic: true, add: () => ({ ok: true, budget: BUDGET, meeting_pattern_applied: { value: '2x100', source: 'programmes', status: 'agree' } }) });
  h.d.getElementById('twsAddCourse').click();
  const form = h.d.querySelector(EDITOR);
  control(form, 'رمز المقرر').value = 'stat109';
  await form.onsubmit({ preventDefault() {} });
  await wait(() => h.notices.length === 1);
  assert.deepEqual(h.notices, [{ kind: 'success', message: `أُضيف ${iso('STAT109')} — ٢ × ١٠٠ دقيقة حسب برامجه. اختر الشعبة من قائمة الشعب المطلوبة لوضعها.` }]);
});

test('split: a section being placed of a course “Use programme defaults” changed starts again from its row as read now; another course’s is left', async t => {
  for (const [placing, restarts] of [[STAT103.course_key, 1], [COE226.course_key, 0]]) {
    let budget = BUDGET;
    const h = await split(t, { budget: () => budget, save: body => { const answer = savedAnswer(budget, body); budget = answer.budget; return answer; } });
    let restarted = 0;
    h.w.restartCreateSectionAssist = () => { restarted++; return true; };
    Object.assign(h.state.createAssist, { active: true, payload: { course_key: placing } });
    button(h.summary(), 'Use programme defaults (2)').click();
    await wait(() => h.notices.length === 1);
    assert.equal(restarted, restarts, placing);
  }
});
