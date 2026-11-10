/* Weekly meetings: a 3-credit course may meet as two 100-minute meetings a
 * week instead of its credit hours' standard pattern (owner, 2026-11-10), as
 * a scenario's choice. What the browser shows of it:
 *
 * - The Add course / section dialog (static/js/timetable-course-editor.js,
 *   both pages): a new course's Weekly meetings, open only at 3 credit hours
 *   and posted with it; a course already in the plan shows its weekly
 *   meetings and, when its credit hours allow another, changes them with its
 *   own Save (POST /ops/tw/scenarios/<id>/budget/meeting-pattern/), never
 *   with "Add to scenario". Its status line says saved or unchanged and which
 *   placed sections still meet the old way (locked ones, and those another
 *   scenario owns, named apart); a refusal is the server's words in its
 *   alert, since a toast sits under the modal dialog.
 * - The shape chip next to the credit hours: the classic course list, the
 *   Place dialog's subtitle and the split page's sidebar card. Nothing for a
 *   course on its standard pattern.
 * - The Lab tag of a Place meeting: its room type when the server names one
 *   (a 2 × 100 min meeting is a lecture in a 100-minute column of the lab
 *   grid), else its column's kind, exactly as before.
 * - The Check words for section_meeting_pattern_mismatch and
 *   meeting_pattern_no_window, in English and Arabic; never the code itself.
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

/* ── The budget rows (compute_scenario_budget) ── */
const SHAPE_LABELS = { '2x100': ['2 × 100 min', '٢ × ١٠٠ دقيقة'] };
// A budget row set to `pattern` ('' is the standard pattern), as the server words it.
const shaped = (row, pattern) => ({ ...row, meeting_pattern: pattern,
  meeting_pattern_label: SHAPE_LABELS[pattern]?.[0] || '', meeting_pattern_label_ar: SHAPE_LABELS[pattern]?.[1] || '',
  meeting_durations: pattern === '2x100' ? [100, 100] : row.meeting_durations,
  required_meetings_per_section: pattern === '2x100' ? 2 : row.required_meetings_per_section });
const AI222 = { course_code: 'AI222', course_key: 'AI222::SEARCH', course_name: 'Search', department: 'AI', credit_hours: 3, programme_term: 7,
  planned_sections: 1, used_sections: 0, used_section_labels: [], remaining_sections: 1, max_per_section: 35, total_demand: 30,
  required_meetings_per_section: 2, meeting_pattern: '', meeting_pattern_label: '', meeting_pattern_label_ar: '',
  meeting_pattern_choices: ['', '2x100'], meeting_durations: [75, 75] };
const STAT103 = shaped({ ...AI222, course_code: 'STAT103', course_key: 'STAT103::PROBABILITY', course_name: 'Probability', department: 'MATH',
  planned_sections: 2, remaining_sections: 2, max_per_section: 40, total_demand: 70 }, '2x100');
const CS240 = { ...AI222, course_code: 'CS240', course_key: 'CS240::SYSTEMS', course_name: 'Systems', department: 'CS', credit_hours: 4,
  meeting_pattern_choices: [''], meeting_durations: [75, 75, 100] };
const BUDGET = [STAT103, AI222, CS240];

/* ── The weekly-meetings save's answers ── */
const section = (label, extra = {}) => ({ term_section_id: 500 + Number(label.slice(1)), course_key: 'STAT103::PROBABILITY', course_code: 'STAT103',
  section: label, board_ids: [2], placement_ids: [40 + Number(label.slice(1))], placed_minutes: [75, 75], locked: false, externally_owned: false, ...extra });
const savedAnswer = (body, { changed = true, mismatched = [] } = {}) => ({ ok: true, course_key: body.course_key, meeting_pattern: body.meeting_pattern,
  previous: '', changed, budget: BUDGET.map(row => (row.course_key === body.course_key ? shaped(row, body.meeting_pattern) : row)),
  mismatched_sections: mismatched });
const refusal = message => ({ httpError: 400, ok: false, error: { code: 'VALIDATION_MEETING_PATTERN', message } });

/* ── The dialog on its own, as both pages open it ── */
const shown = element => Boolean(element) && !element.closest('[hidden]');
// A form control by its label's own words, among those on show.
const control = (root, words) => {
  const found = [...root.querySelectorAll('label')].filter(label => shown(label) && label.firstChild?.textContent === words)
    .map(label => label.querySelector('select, input'));
  assert.equal(found.length, 1, `one control on show labelled “${words}”`);
  return found[0];
};
const button = (root, words) => [...root.querySelectorAll('button')].find(item => shown(item) && item.textContent === words) || null;
const options = select => [...select.options].map(option => [option.value, option.textContent]);
// The editor's form, by its dialog's name (a page has other dialogs with forms).
const EDITOR = 'dialog[aria-label="Add course / section"] form, dialog[aria-label="إضافة مقرر / شعبة"] form';

function editor(t, { answer = body => savedAnswer(body), arabic = false, published = false, budget = BUDGET } = {}) {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, calls = [], saved = [];
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); this.dispatchEvent(new w.Event('close')); };
  w.eval(read('static/js/timetable-course-editor.js'));
  w.TimetableCourseEditor.open({
    scenarioId: 42, published, budget, arabic,
    request: async (url, opts, onError) => {
      const body = JSON.parse(opts.body);
      calls.push({ url, body });
      const reply = await answer(body, url);
      if (reply?.httpError) { onError?.(reply.error.message); return null; }
      return reply;
    },
    onSaved: async (rows, change) => { saved.push({ rows, change }); },
  });
  const form = d.querySelector(EDITOR);
  const course = control(form, arabic ? 'المقرر' : 'Course');
  const choose = key => { course.value = key; course.dispatchEvent(new w.Event('change')); };
  const status = () => form.querySelector('[role=status]').textContent;
  const alert = () => form.querySelector('[role=alert]').textContent;
  return { w, d, form, calls, saved, choose, status, alert };
}

test('a new course: Weekly meetings is open at 3 credit hours only, and any other credit hours set it back to Standard', async t => {
  const h = editor(t);
  const hours = control(h.form, 'Credit hours');
  const weekly = control(h.form, 'Weekly meetings');
  const hint = h.d.getElementById(weekly.getAttribute('aria-describedby'));
  assert.deepEqual(options(weekly), [['', 'Standard (from credit hours)'], ['2x100', '2 × 100 min']]);
  assert.deepEqual([hours.value, weekly.disabled, weekly.value, shown(hint)], ['3', false, '', false]);
  for (const credits of ['4', '2', '']) {
    weekly.value = '2x100';
    hours.value = credits;
    hours.dispatchEvent(new h.w.Event('input'));
    assert.deepEqual([weekly.disabled, weekly.value, shown(hint)], [true, '', true], `${credits || 'no'} credit hours`);
    assert.equal(hint.textContent, '2 × 100 min is only for 3-credit courses.');
    // Back at 3: open again, on the standard pattern it was set back to.
    hours.value = '3';
    hours.dispatchEvent(new h.w.Event('change'));
    assert.deepEqual([weekly.disabled, weekly.value, shown(hint)], [false, '', false]);
  }
  // Nothing about weekly meetings is shown for a course already in the plan,
  // and nothing is sent with its sections.
  h.choose('AI222::SEARCH');
  assert.equal([...h.form.querySelectorAll('label')].filter(label => shown(label) && label.firstChild?.textContent === 'Weekly meetings').length, 1);
  assert.equal(shown(weekly.parentElement), false);
  assert.equal(weekly.disabled, true);
});

test('a new course is added with its weekly meetings; the standard pattern is the server’s default and is not sent', async t => {
  const h = editor(t, { answer: () => ({ ok: true, budget: ['updated'] }) });
  const inputs = h.form.querySelectorAll('input');
  inputs[0].value = 'stat999'; inputs[1].value = 'Statistics';
  control(h.form, 'Weekly meetings').value = '2x100';
  await h.form.onsubmit({ preventDefault() {} });
  assert.deepEqual(h.calls[0], { url: '/ops/tw/scenarios/42/budget/', body: { course_key: 'STAT999', course_code: 'stat999', course_name: 'Statistics',
    credit_hours: '3', max_per_section: '40', programme_term: '', sections: '1', meeting_pattern: '2x100' } });
  assert.deepEqual(h.saved, [{ rows: ['updated'], change: undefined }], 'Add is the Add it was');
  assert.equal(h.d.querySelector('dialog'), null, 'Add closes the dialog');
  // Standard: no meeting_pattern, so a typed code already in the plan gains
  // sections exactly as before.
  const s = editor(t, { answer: () => ({ ok: true, budget: [] }) });
  s.form.querySelectorAll('input')[0].value = 'AI222';
  await s.form.onsubmit({ preventDefault() {} });
  assert.equal('meeting_pattern' in s.calls[0].body, false);
  // An existing course's sections never carry its weekly meetings either.
  const e = editor(t, { answer: () => ({ ok: true, budget: [] }) });
  e.choose('STAT103::PROBABILITY');
  control(e.form, 'Weekly meetings').value = '';
  await e.form.onsubmit({ preventDefault() {} });
  assert.deepEqual([e.calls[0].url, e.calls[0].body.course_key, 'meeting_pattern' in e.calls[0].body],
    ['/ops/tw/scenarios/42/budget/', 'STAT103::PROBABILITY', false]);
});

test('a course in the plan shows its weekly meetings; a choice and its own Save only when its credit hours allow another', async t => {
  const h = editor(t);
  assert.equal(button(h.form, 'Save weekly meetings'), null, 'a new course has no Save of its own');
  h.choose('STAT103::PROBABILITY');
  const weekly = control(h.form, 'Weekly meetings');
  assert.match(h.form.textContent, /Weekly meetings now: 2 × 100 min/);
  assert.deepEqual([options(weekly), weekly.value], [[['', 'Standard (from credit hours)'], ['2x100', '2 × 100 min']], '2x100']);
  assert.ok(button(h.form, 'Save weekly meetings'));
  h.choose('AI222::SEARCH');
  assert.match(h.form.textContent, /Weekly meetings now: Standard \(from credit hours\)/);
  assert.equal(control(h.form, 'Weekly meetings').value, '');
  // A 4-credit course has one choice: shown, never offered.
  h.choose('CS240::SYSTEMS');
  assert.match(h.form.textContent, /Weekly meetings now: Standard \(from credit hours\)/);
  assert.equal([...h.form.querySelectorAll('label')].some(label => shown(label) && label.firstChild?.textContent === 'Weekly meetings'), false);
  assert.equal(button(h.form, 'Save weekly meetings'), null);
  // An older answer with no choices: shown as standard, nothing offered.
  const old = editor(t, { budget: [{ course_key: 'AI101::PLAN', course_code: 'AI101', course_name: 'Plan' }] });
  old.choose('AI101::PLAN');
  assert.match(old.form.textContent, /Weekly meetings now: Standard \(from credit hours\)/);
  assert.equal(button(old.form, 'Save weekly meetings'), null);
  assert.equal(old.form.checkValidity(), true);
});

test('Save weekly meetings posts the course’s exact identity to its own endpoint, keeps the dialog open and says it is saved', async t => {
  const h = editor(t);
  h.choose('AI222::SEARCH');
  control(h.form, 'Weekly meetings').value = '2x100';
  button(h.form, 'Save weekly meetings').click();
  await wait(() => h.status());
  assert.deepEqual(h.calls, [{ url: '/ops/tw/scenarios/42/budget/meeting-pattern/', body: { course_key: 'AI222::SEARCH', meeting_pattern: '2x100' } }]);
  assert.equal(h.status(), `Saved: ${iso('AI222')} is now set to 2 × 100 min.`);
  assert.equal(h.alert(), '');
  // The page reads its budget again; this is no "Added".
  assert.equal(h.saved.length, 1);
  assert.deepEqual(Object.entries(h.saved[0].change), [['action', 'meeting_pattern']]);
  assert.equal(h.saved[0].rows.find(row => row.course_key === 'AI222::SEARCH').meeting_pattern, '2x100');
  assert.ok(h.d.querySelector('dialog[open]'), 'the dialog stays open on its status line');
  // What it meets now is the server's answer, kept when the course is chosen again.
  assert.match(h.form.textContent, /Weekly meetings now: 2 × 100 min/);
  h.choose('');
  h.choose('AI222::SEARCH');
  assert.equal(control(h.form, 'Weekly meetings').value, '2x100');
  assert.equal(h.status(), '', 'another choice of course clears the line');
  assert.equal(button(h.form, 'Save weekly meetings').disabled, false);
});

test('an unchanged save says nothing changed; the sections still on the old way are counted, locked and other-scenario ones named apart', async t => {
  const same = editor(t, { answer: body => savedAnswer(body, { changed: false }) });
  same.choose('STAT103::PROBABILITY');
  button(same.form, 'Save weekly meetings').click();
  await wait(() => same.status());
  assert.equal(same.status(), `Nothing changed: ${iso('STAT103')} is already set to 2 × 100 min.`);

  const one = editor(t, { answer: body => savedAnswer(body, { mismatched: [section('S1')] }) });
  one.choose('STAT103::PROBABILITY');
  button(one.form, 'Save weekly meetings').click();
  await wait(() => one.status());
  assert.equal(one.status(), `Saved: ${iso('STAT103')} is now set to 2 × 100 min. 1 placed section still meets the old way: ${iso('S1')}. `
    + 'Remove it and run Optimise (or Full rebuild) to place it in this pattern.');

  const mismatched = [section('S1'), section('S2', { locked: true }), section('S4', { locked: true, externally_owned: true })];
  for (const [arabic, expected] of [
    [false, `Saved: ${iso('STAT103')} is now set to 2 × 100 min. 3 placed sections still meet the old way: ${iso('S1')}, ${iso('S2')}, ${iso('S4')}. `
      + `Remove them and run Optimise (or Full rebuild) to place them in this pattern. Locked (unlock before removing): ${iso('S2')}. `
      + `Owned by another scenario (change there): ${iso('S4')}.`],
    [true, `حُفظ: ضُبط ${iso('STAT103')} على ٢ × ١٠٠ دقيقة. عدد الشعب الموضوعة التي ما زالت تجتمع بالطريقة السابقة: 3 (${iso('S1')}، ${iso('S2')}، ${iso('S4')}). `
      + `احذفها ثم شغّل «تحسين» (أو «إعادة بناء كاملة») لتوضع بهذا النمط. مقفلة (ألغِ قفلها قبل الحذف): ${iso('S2')}. يملكها سيناريو آخر (عدّلها هناك): ${iso('S4')}.`],
  ]) {
    const h = editor(t, { arabic, answer: body => savedAnswer(body, { mismatched }) });
    h.choose('STAT103::PROBABILITY');
    button(h.form, arabic ? 'حفظ اللقاءات الأسبوعية' : 'Save weekly meetings').click();
    await wait(() => h.status());
    assert.equal(h.status(), expected);
  }
});

test('a refusal is the server’s words in the dialog’s alert; with none, the dialog’s own; Save is offered again', async t => {
  const message = 'STAT103 has 4 credit hours; 2 × 100 min a week is only for 3-credit courses.';
  const h = editor(t, { answer: () => refusal(message) });
  h.choose('STAT103::PROBABILITY');
  control(h.form, 'Weekly meetings').value = '';
  button(h.form, 'Save weekly meetings').click();
  await wait(() => h.alert());
  assert.deepEqual([h.alert(), h.status(), h.saved.length], [message, '', 0]);
  assert.equal(button(h.form, 'Save weekly meetings').disabled, false);
  assert.ok(h.d.querySelector('dialog[open]'));
  const silent = editor(t, { arabic: true, answer: () => null });
  silent.choose('STAT103::PROBABILITY');
  button(silent.form, 'حفظ اللقاءات الأسبوعية').click();
  await wait(() => silent.alert());
  assert.equal(silent.alert(), 'تعذر حفظ اللقاءات الأسبوعية. تحقق من حالة السيناريو ثم أعد المحاولة.');
});

test('Arabic: the dialog is right to left, the weekly meetings in Arabic words', async t => {
  const h = editor(t, { arabic: true });
  assert.equal(h.d.querySelector('dialog').dir, 'rtl');
  const weekly = control(h.form, 'اللقاءات الأسبوعية');
  assert.deepEqual(options(weekly), [['', 'قياسي (حسب الساعات المعتمدة)'], ['2x100', '٢ × ١٠٠ دقيقة']]);
  const hours = control(h.form, 'الساعات المعتمدة');
  hours.value = '4'; hours.dispatchEvent(new h.w.Event('input'));
  assert.equal(h.d.getElementById(weekly.getAttribute('aria-describedby')).textContent, '٢ × ١٠٠ دقيقة للمقررات ذات ٣ ساعات معتمدة فقط.');
  h.choose('STAT103::PROBABILITY');
  assert.match(h.form.textContent, /اللقاءات الأسبوعية حالياً: ٢ × ١٠٠ دقيقة/);
  assert.ok(button(h.form, 'حفظ اللقاءات الأسبوعية'));
  h.choose('AI222::SEARCH');
  assert.match(h.form.textContent, /اللقاءات الأسبوعية حالياً: قياسي \(حسب الساعات المعتمدة\)/);
});

test('the keyboard stays on Save after it, saved or refused, though the button was disabled while it ran', async t => {
  for (const reply of [body => savedAnswer(body), () => refusal('Refused.')]) {
    // A browser moves the keyboard off a focused button it disables, out of
    // the dialog (jsdom does not, and its blur() ignores a disabled button):
    // the keyboard is sent outside the dialog while the save runs.
    let outside = null;
    const h = editor(t, { answer: body => { outside.focus(); return reply(body); } });
    outside = h.d.body.appendChild(h.d.createElement('button'));
    h.choose('STAT103::PROBABILITY');
    const save = button(h.form, 'Save weekly meetings');
    save.focus();
    save.click();
    await wait(() => h.status() || h.alert());
    await settle();
    assert.equal(h.d.activeElement, save);
  }
});

test('a published scenario offers no Save of the weekly meetings', async t => {
  const h = editor(t, { published: true });
  h.choose('STAT103::PROBABILITY');
  assert.equal(button(h.form, 'Save weekly meetings').disabled, true);
  await button(h.form, 'Save weekly meetings').onclick();
  assert.deepEqual(h.calls, []);
});

/* ── The classic page ── */
const scenario = { id: 1, name: 'Weekly meetings', status: 'draft', slot_config: [{ label: '09:00', start: '09:00', end: '10:15' }] };
const board = { id: 2, label: 'Term 7', nominal_term: 7, primary_count: 1 };
const at = (id, extra = {}) => ({ id, course_code: `AI${id}`, course_key: `AI${id}`, section: 'S1', term_section_id: id + 100, day: 'SUN',
  start_time: '09:00', end_time: '10:15', room: 'R1', meetings: [], available_capacity: 24, needs_room: true, room_state: 'assigned',
  is_online: false, shared_with_other_scenario: false, ...extra });
const quality = { ok: true, scenario_id: 1, instructor: { assessed: true, working_days: 1, idle_minutes: 0 }, soft_seats: { assessed: true, unresolved: 0 },
  rooms: { assessed: true, shortages: 0, details: [] }, assigned_student_clashes: { assessed: true, count: 0 } };
const quiet = { summary: { critical: 0, warning: 0 }, overlaps: [], instructor_clashes: [], room_clashes: [], cross_board_conflicts: [], hard_overlap_threshold: 20 };
const READY = { ready: true, blockers: [], warnings: [], blocker_items: [], warning_items: [] };
const verdict = { severity: 'ok', counts: { critical: 0, warning: 0, info: 0 }, students: 0, items: [], more: 0, own_section_meeting: false };
// A whole weekly pattern as /planned-slot-candidates/ lists it.
const pattern = (meetings, rank) => ({ candidate_id: `pattern-${rank}`, kind: meetings[0].kind, is_pattern: true, requires_full_section_pattern: true,
  required_meetings: meetings.length, day: meetings[0].day, start: meetings[0].start, end: meetings[0].end, meetings, score: rank, tone: 'clean',
  critical_count: 0, rank, verdict, verdict_rank: rank });
const long = (day, extra = {}) => ({ kind: 'lab', day, start: '09:00', end: '10:40', slot_idx: 0, ...extra });
const candidatesFor = (key, meetings) => ({ ok: true, board: { id: 2, label: 'Term 7', nominal_term: 7 },
  request: { course_code: key.split('::')[0], course_key: key, section_label: 'S1', required_meetings_per_section: 2, requires_full_section_pattern: true },
  status: 'ready', summary: { total: 1 }, candidates: [pattern(meetings, 1)] });

async function classic(t, { arabic = false, budget = () => BUDGET, readiness = READY, candidates = () => undefined, save = body => savedAnswer(body), editor = false } = {}) {
  const template = read('core/templates/core/timetable_workspace.html');
  const html = template.replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost/timetable-workspace/' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, requests = [], notices = [], errors = [], dialogs = [];
  w.addEventListener('error', e => errors.push(e.error));
  t.after(() => assert.deepEqual(errors, []));
  w.eval(read('static/js/shared-utils.js'));
  w.LANGUAGE_CODE = arabic ? 'ar' : 'en';
  d.documentElement.lang = arabic ? 'ar' : 'en';
  w._injectCsrf = x => x;
  w.esc = esc;
  w.debounce = fn => fn;
  w.notify = Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => notices.push({ type, text })]));
  // The publish confirmation: what it was asked to show, answered "no".
  w.dlg = { confirm: async options => { dialogs.push(options); return false; } };
  w.Element.prototype.scrollIntoView = () => {};
  if (editor) {
    // jsdom has no modal dialogs; the editor opens its own with showModal.
    w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
    w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); this.dispatchEvent(new w.Event('close')); };
  }
  w.fetch = async (url, opts = {}) => {
    requests.push({ url, opts });
    let body;
    if (url === '/ops/tw/scenarios/1/budget/meeting-pattern/' && opts.method === 'POST') body = await save(JSON.parse(opts.body));
    else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [scenario] };
    else if (url === '/ops/tw/scenarios/1/') body = { scenario, active_job: null };
    else if (url.includes('/quality/')) body = quality;
    else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
    else if (url === '/ops/tw/boards/2/') body = { placements: [at(10)], slot_config: scenario.slot_config };
    else if (url.includes('/conflicts/')) body = quiet;
    else if (url.includes('/readiness/')) body = { readiness };
    else if (url.includes('/planned-slot-candidates/')) body = candidates(new URL(url, 'http://localhost').searchParams.get('course_key'));
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
  // Loaded and at rest: a write (a Save) is never refused as "busy", and Add course is enabled.
  await wait(() => w.TW.loadedBoardId === 2 && d.querySelector('#twSectionList .tw-section-item')
    && d.querySelector('.tw-workspace').dataset.busy === 'false');
  const row = (key, label = 'S1') => d.querySelector(`#twSectionList [data-focus-key="place:${key}|${label}"]`);
  const facts = key => [...row(key).querySelector('.meta.ux-meta').children].map(span => span.textContent);
  // Enter on a list row opens Place…, as the keyboard gives it.
  const place = async key => {
    row(key).focus();
    row(key).dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    await wait(() => d.querySelectorAll('#twPlaceBody input[name="twPlaceChoice"]').length > 0);
  };
  const placeLines = () => [...d.querySelectorAll('#twPlaceBody input[name="twPlaceChoice"]')]
    .map(radio => d.getElementById(radio.getAttribute('aria-labelledby')).textContent);
  const placeTimes = () => [...d.getElementById('twPlaceTime').options].map(option => option.textContent);
  // The publish confirmation's warning lines, as text.
  const warnings = async () => {
    await w.publishScenario();
    const box = d.createElement('div');
    box.innerHTML = String(dialogs.at(-1).body);
    return [...box.querySelectorAll('.tw-readiness-list[data-tone="warning"] li')].map(li => li.querySelector('span:not(.ux-tone-icon)').textContent);
  };
  return { w, d, requests, notices, row, facts, place, placeLines, placeTimes, warnings };
}

test('classic list: the shape is named next to the credit hours; a course on its standard pattern reads exactly as before', async t => {
  const legacy = ({ meeting_pattern, meeting_pattern_label, meeting_pattern_label_ar, meeting_pattern_choices, meeting_durations, ...row }) => row;
  const h = await classic(t, { budget: () => [STAT103, AI222, legacy({ ...AI222, course_code: 'AI333', course_key: 'AI333::OLD' })] });
  assert.deepEqual(h.facts('STAT103::PROBABILITY'), ['MATH', '3 cr', '2 × 100 min', 'cap 40', '2 meetings a week']);
  assert.deepEqual(h.facts('AI222::SEARCH'), ['AI', '3 cr', 'cap 35', '2 meetings a week']);
  assert.deepEqual(h.facts('AI333::OLD'), ['AI', '3 cr', 'cap 35', '2 meetings a week']);
  const ar = await classic(t, { arabic: true });
  assert.deepEqual(ar.facts('STAT103::PROBABILITY'), ['MATH', '3 س', '٢ × ١٠٠ دقيقة', 'سعة 40', 'لقاءان أسبوعياً']);
  assert.deepEqual(ar.facts('AI222::SEARCH'), ['AI', '3 س', 'سعة 35', 'لقاءان أسبوعياً']);
});

test('Place: a 2 × 100 meeting is a lecture in a 100-minute column, so no Lab tag in its line or its time; the subtitle names the shape', async t => {
  const h = await classic(t, { candidates: key => candidatesFor(key, [long('SUN', { room_type: 'lecture' }), long('TUE', { room_type: 'lecture' })]) });
  await h.place('STAT103::PROBABILITY');
  assert.equal(h.d.getElementById('twPlaceSub').textContent, 'Term 7 · 2 × 100 min · 2 meetings a week · the whole weekly pattern');
  assert.deepEqual(h.placeLines(), ['Sunday 09:00–10:40, Tuesday 09:00–10:40']);
  assert.deepEqual(h.placeTimes(), ['Any time', iso('09:00–10:40')]);
  const ar = await classic(t, { arabic: true, candidates: key => candidatesFor(key, [long('SUN', { room_type: 'lecture' }), long('TUE', { room_type: 'lecture' })]) });
  await ar.place('STAT103::PROBABILITY');
  assert.equal(ar.d.getElementById('twPlaceSub').textContent, 'Term 7 · ٢ × ١٠٠ دقيقة · لقاءان أسبوعياً · النمط الأسبوعي كاملاً');
  assert.doesNotMatch(ar.placeLines().join(' '), /معمل/);
});

test('Place: a meeting that names no room type keeps its column’s tag, and the subtitle its words, exactly as before', async t => {
  const h = await classic(t, { candidates: key => candidatesFor(key, [long('SUN'), long('TUE')]) });
  await h.place('AI222::SEARCH');
  assert.equal(h.d.getElementById('twPlaceSub').textContent, 'Term 7 · 2 meetings a week · the whole weekly pattern');
  assert.deepEqual(h.placeLines(), ['Sunday 09:00–10:40 · Lab, Tuesday 09:00–10:40 · Lab']);
  assert.deepEqual(h.placeTimes(), ['Any time', `${iso('09:00–10:40')} · Lab`]);
  // The rule itself: the room type when named, else the column's kind.
  const line = m => h.w.placeMeetingHtml({ day: 'SUN', start: '09:00', end: '10:40', ...m }).replace(/<[^>]+>/g, '');
  assert.deepEqual([line({ kind: 'lab' }), line({ kind: 'lect' }), line({ kind: 'lab', room_type: 'lecture' }), line({ kind: 'lect', room_type: 'lab' })],
    ['Sunday 09:00–10:40 · Lab', 'Sunday 09:00–10:40', 'Sunday 09:00–10:40', 'Sunday 09:00–10:40 · Lab']);
});

const mismatchItem = (params = {}) => ({ code: 'section_meeting_pattern_mismatch', board_id: 2, board_label: 'Term 7', count: null,
  params: { course_code: 'STAT103', course_key: 'STAT103::PROBABILITY', section: 'S2', placement_ids: [41, 42], meeting_pattern: '2x100',
    expected_label: '2 × 100 min', expected_durations: [100, 100], placed_minutes: [75, 75], ...params } });
const noWindowItem = { code: 'meeting_pattern_no_window', board_id: null, board_label: null, count: null,
  params: { course_code: 'STAT103', course_key: 'STAT103::PROBABILITY', meeting_pattern: '2x100', expected_label: '2 × 100 min', expected_durations: [100, 100] } };
const readinessWith = (...items) => ({ ready: true, blockers: [], blocker_items: [], warnings: items.map(item => `(${item.code})`), warning_items: items });

for (const lang of ['en', 'ar']) test(`(${lang}) Check words a section of another shape and a shape with no column to go to, never their codes`, async t => {
  const arabic = lang === 'ar';
  const h = await classic(t, { arabic, readiness: readinessWith(mismatchItem(), mismatchItem({ section: 'S3', placed_minutes: [100, 75] }), noWindowItem) });
  const lines = await h.warnings();
  assert.deepEqual(lines, arabic ? [
    `تجتمع ${iso('STAT103 S2')} بواقع ٢ × ٧٥ دقيقة، لكن المقرر مضبوط على ٢ × ١٠٠ دقيقة. انقل لقاءاتها إلى أعمدة مدتها ١٠٠ دقيقة، أو احذفها ثم شغّل «تحسين».`,
    `تجتمع ${iso('STAT103 S3')} بواقع ٧٥ + ١٠٠ دقيقة، لكن المقرر مضبوط على ٢ × ١٠٠ دقيقة. انقل لقاءاتها إلى أعمدة مدتها ١٠٠ دقيقة، أو احذفها ثم شغّل «تحسين».`,
    `${iso('STAT103')} مضبوط على ٢ × ١٠٠ دقيقة، لكن هذا السيناريو بلا عمود مدته ١٠٠ دقيقة للمحاضرات الحضورية. استخدم «تعديل الفترات الزمنية…» لإضافة عمود.`,
  ] : [
    `${iso('STAT103 S2')} meets 2 × 75 min but the course is set to 2 × 100 min. Move its meetings into 100-minute columns, or remove it and run Optimise.`,
    `${iso('STAT103 S3')} meets 75 + 100 min but the course is set to 2 × 100 min. Move its meetings into 100-minute columns, or remove it and run Optimise.`,
    `${iso('STAT103')} is set to 2 × 100 min, but this scenario has no 100-minute column for in-person classes. Use Edit time slots… to add one.`,
  ]);
  assert.doesNotMatch(lines.join(' '), /section_meeting_pattern_mismatch|meeting_pattern_no_window/);
});

test('classic: Save weekly meetings re-reads the list (its chip), says it in the dialog, and a refusal stays in the dialog, never a toast', async t => {
  let budget = BUDGET;
  // The budget the page reads again is the one the save answered.
  const s = await classic(t, { editor: true, budget: () => budget, save: body => { const answer = savedAnswer(body); budget = answer.budget; return answer; } });
  s.d.getElementById('twAddCourse').click();
  const form = s.d.querySelector(EDITOR);
  const course = control(form, 'Course');
  course.value = 'AI222::SEARCH'; course.dispatchEvent(new s.w.Event('change'));
  control(form, 'Weekly meetings').value = '2x100';
  button(form, 'Save weekly meetings').click();
  await wait(() => form.querySelector('[role=status]').textContent && s.facts('AI222::SEARCH').includes('2 × 100 min'));
  assert.equal(form.querySelector('[role=status]').textContent, `Saved: ${iso('AI222')} is now set to 2 × 100 min.`);
  assert.deepEqual(s.facts('AI222::SEARCH'), ['AI', '3 cr', '2 × 100 min', 'cap 35', '2 meetings a week']);
  assert.deepEqual(s.notices, [], 'no "Added" toast for a weekly-meetings save');
  assert.ok(s.d.querySelector('dialog[open] form'));

  const message = 'AI222 has 4 credit hours; 2 × 100 min a week is only for 3-credit courses.';
  const r = await classic(t, { editor: true, save: () => refusal(message) });
  r.d.getElementById('twAddCourse').click();
  const refused = r.d.querySelector(EDITOR);
  const chosen = control(refused, 'Course');
  chosen.value = 'AI222::SEARCH'; chosen.dispatchEvent(new r.w.Event('change'));
  control(refused, 'Weekly meetings').value = '2x100';
  const reads = r.requests.filter(row => row.url === '/ops/tw/scenarios/1/budget/').length;
  button(refused, 'Save weekly meetings').click();
  await wait(() => refused.querySelector('[role=alert]').textContent);
  assert.equal(refused.querySelector('[role=alert]').textContent, message);
  assert.deepEqual(r.notices, [], 'said in the dialog, not in a toast under it');
  assert.equal(r.requests.filter(row => row.url === '/ops/tw/scenarios/1/budget/').length, reads, 'nothing saved, nothing read again');
});

/* ── The split page ── */
async function split(t, { arabic = false, budget = () => BUDGET, save = body => savedAnswer(body) } = {}) {
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
    let body;
    if (url === '/ops/tw/scenarios/1/budget/meeting-pattern/' && options.method === 'POST') body = await save(JSON.parse(options.body));
    else if (url === '/ops/tw/scenarios/1/budget/') body = { budget: budget() };
    else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
    else body = { ok: true, candidates: [], instructors: [], summary: {} };
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  w.eval(read('static/js/shared-utils.js'));
  for (const file of ['timetable-meeting-policy', 'timetable-slot-config', 'timetable-section-teacher', 'timetable-programme-mark', 'timetable-course-editor']) {
    w.eval(read(`static/js/${file}.js`));
  }
  w.eval(`${read('static/js/page-timetable-workspace-split.js')}\n;globalThis.__splitAudit = { S, SB };`);
  await settle();
  const { S: state, SB: sidebar } = w.__splitAudit;
  state.scenarioId = 1; state.scenarioMeta = { id: 1, status: 'draft' };
  sidebar.budget = budget();
  w.renderSidebar();
  const chips = key => [...d.querySelector(`.tws-sec-item[data-key="${key}"]`).querySelectorAll('.meta .dem')].map(span => span.textContent);
  return { w, d, requests, notices, state, sidebar, chips };
}

test('split sidebar: the shape is a chip next to the credit hours; a course on its standard pattern has the chips it had', async t => {
  const h = await split(t);
  assert.deepEqual(h.chips('STAT103::PROBABILITY'), ['70 stu', '3h', '2 × 100 min', 'cap 40']);
  assert.deepEqual(h.chips('AI222::SEARCH'), ['30 stu', '3h', 'cap 35']);
  const ar = await split(t, { arabic: true });
  assert.deepEqual(ar.chips('STAT103::PROBABILITY'), ['70 طالب', '3h', '٢ × ١٠٠ دقيقة', 'حد 40']);
});

test('split create-assist: a target names Lab by its room type when named, else by its column, as before', async t => {
  const h = await split(t);
  const target = candidate => h.w.createAssistTargetLabel({ paneIdx: 0, boardLabel: 'Term 7', day: 'SUN', start: '09:00', end: '10:40', ...candidate });
  assert.deepEqual([target({ kind: 'lab' }), target({ kind: 'lect' }), target({ kind: 'lab', room_type: 'lecture' }), target({ kind: 'lect', room_type: 'lab' })],
    ['Pane A · Term 7 · Lab SUN 09:00', 'Pane A · Term 7 · Slot SUN 09:00', 'Pane A · Term 7 · Slot SUN 09:00', 'Pane A · Term 7 · Lab SUN 09:00']);
});

test('split: Save weekly meetings re-reads the sidebar with no "Added" toast; a refusal is the server’s words in the dialog', async t => {
  let budget = BUDGET;
  const h = await split(t, { budget: () => budget, save: body => { const answer = savedAnswer(body); budget = answer.budget; return answer; } });
  h.d.getElementById('twsAddCourse').click();
  const form = h.d.querySelector(EDITOR);
  const course = control(form, 'Course');
  course.value = 'AI222::SEARCH'; course.dispatchEvent(new h.w.Event('change'));
  control(form, 'Weekly meetings').value = '2x100';
  button(form, 'Save weekly meetings').click();
  await wait(() => form.querySelector('[role=status]').textContent && h.chips('AI222::SEARCH').includes('2 × 100 min'));
  assert.equal(form.querySelector('[role=status]').textContent, `Saved: ${iso('AI222')} is now set to 2 × 100 min.`);
  assert.deepEqual(h.chips('AI222::SEARCH'), ['30 stu', '3h', '2 × 100 min', 'cap 35']);
  assert.deepEqual(h.notices, []);

  const message = 'This scenario is published; copy it to a draft to change it.';
  const r = await split(t, { save: () => ({ httpError: 400, ok: false, error: { code: 'SCENARIO_PUBLISHED', message } }) });
  r.d.getElementById('twsAddCourse').click();
  const refused = r.d.querySelector(EDITOR);
  const chosen = control(refused, 'Course');
  chosen.value = 'STAT103::PROBABILITY'; chosen.dispatchEvent(new r.w.Event('change'));
  button(refused, 'Save weekly meetings').click();
  await wait(() => refused.querySelector('[role=alert]').textContent);
  assert.equal(refused.querySelector('[role=alert]').textContent, message);
  assert.deepEqual(r.notices, []);
});
