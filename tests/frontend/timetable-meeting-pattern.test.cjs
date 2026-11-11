/* Weekly meetings: a 3-credit course may meet as two 100-minute meetings a
 * week instead of its credit hours' standard pattern (owner, 2026-11-10), as
 * a scenario's choice. What the browser shows of it:
 *
 * - The Add course / section dialog (static/js/timetable-course-editor.js,
 *   both pages): a new course's Weekly meetings, open only at 3 credit hours
 *   and posted with it; a course already in the plan shows its weekly
 *   meetings and, when its credit hours allow another, changes them with its
 *   own Save (POST /ops/tw/scenarios/<id>/budget/meeting-pattern/), never
 *   with "Add to scenario", which holds a change not saved yet instead of
 *   dropping it. What a course meets now is its minutes ("4 × 180 min" for an
 *   English bundle), never "from credit hours". Its status line says saved
 *   or unchanged and which placed sections still meet the old way (locked
 *   ones, and those another scenario owns, named apart); a refusal is the
 *   server's words in its alert, since a toast sits under the modal dialog,
 *   and on the split page only those (a page that is not JSON, no answer or
 *   a bare status leave the dialog's own words).
 * - The shape chip next to the credit hours: the classic course list, the
 *   Place dialog's subtitle and the split page's sidebar card. Nothing for a
 *   course on its standard pattern.
 * - The Lab tag of a Place meeting: its room type when the server names one
 *   (a 2 × 100 min meeting is a lecture in a 100-minute column of the lab
 *   grid), else its column's kind, exactly as before.
 * - A create refused for another weekly shape (FULL_SECTION_PATTERN_REQUIRED):
 *   classic's Place dialog says the shape's minutes; split view says it, and
 *   the section being placed starts again for the course's weekly meetings
 *   now, as it does after a weekly-meetings save of that course.
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
// Back from 2 × 100 min, a 3-credit course meets its credit hours' 2 × 75.
const shaped = (row, pattern) => ({ ...row, meeting_pattern: pattern,
  meeting_pattern_label: SHAPE_LABELS[pattern]?.[0] || '', meeting_pattern_label_ar: SHAPE_LABELS[pattern]?.[1] || '',
  meeting_durations: pattern === '2x100' ? [100, 100] : row.meeting_pattern === '2x100' ? [75, 75] : row.meeting_durations,
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
// The words on show (a hidden control's options are not).
const shownText = root => {
  const walker = root.ownerDocument.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
  let text = '';
  for (let node = walker.nextNode(); node; node = walker.nextNode()) if (shown(node.parentElement)) text += node.textContent;
  return text;
};
// "Weekly meetings now: …", the line over a chosen course's choice.
const nowLine = root => [...root.querySelectorAll('p')].find(p => shown(p) && /^(Weekly meetings now|اللقاءات الأسبوعية حالياً): /.test(p.textContent))?.textContent ?? null;
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

test('a new course: Weekly meetings is open at 3 credit hours only, and any other credit hours set it back to Programme default', async t => {
  const h = editor(t);
  const hours = control(h.form, 'Credit hours');
  const weekly = control(h.form, 'Weekly meetings');
  const hint = h.d.getElementById(weekly.getAttribute('aria-describedby'));
  assert.deepEqual(options(weekly), [['', 'Programme default'], ['standard', 'Standard (2 × 75 min)'], ['2x100', '2 × 100 min']]);
  assert.deepEqual([hours.value, weekly.disabled, weekly.value, shown(hint)], ['3', false, '', false]);
  for (const credits of ['4', '2', '']) {
    weekly.value = '2x100';
    hours.value = credits;
    hours.dispatchEvent(new h.w.Event('input'));
    assert.deepEqual([weekly.disabled, weekly.value, shown(hint)], [true, '', true], `${credits || 'no'} credit hours`);
    assert.equal(hint.textContent, '2 × 100 min is only for 3-credit courses.');
    // Back at 3: open again, on Programme default, which it was set back to.
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
  await e.form.onsubmit({ preventDefault() {} });
  assert.deepEqual([e.calls[0].url, e.calls[0].body.course_key, 'meeting_pattern' in e.calls[0].body],
    ['/ops/tw/scenarios/42/budget/', 'STAT103::PROBABILITY', false]);
});

for (const arabic of [false, true]) test(`(${arabic ? 'ar' : 'en'}) Add to scenario holds a weekly-meetings change not saved yet: nothing is sent, the alert says so, the keyboard goes to the choice`, async t => {
  const h = editor(t, { arabic, answer: () => ({ ok: true, budget: [] }) });
  h.choose('STAT103::PROBABILITY');
  const weekly = control(h.form, arabic ? 'اللقاءات الأسبوعية' : 'Weekly meetings');
  weekly.value = '';
  await h.form.onsubmit({ preventDefault() {} });
  assert.deepEqual(h.calls, [], 'the change is never dropped by adding sections');
  assert.equal(h.alert(), arabic
    ? 'تغيّرت اللقاءات الأسبوعية ولم تُحفظ: استخدم «حفظ اللقاءات الأسبوعية» أولاً، أو أعدها كما كانت، ثم أضف الشعب.'
    : 'The weekly meetings are changed but not saved: use Save weekly meetings first, or set them back, then add sections.');
  assert.equal(h.d.activeElement, weekly);
  assert.equal(h.form.querySelector('[type=submit]').disabled, false);
  assert.ok(h.d.querySelector('dialog[open]'), 'the dialog stays open');
  // Set back: the sections are added as before, with no weekly meetings sent.
  weekly.value = '2x100';
  await h.form.onsubmit({ preventDefault() {} });
  assert.deepEqual([h.calls.length, h.calls[0].body.course_key, 'meeting_pattern' in h.calls[0].body, h.alert()], [1, 'STAT103::PROBABILITY', false, '']);
});

// A row the server answers with a shape but no other choice: a stored
// 2 × 100 read with the engines' "missing credit hours mean 3"
// (shape_for), while the choices go by the exact credit hours (pattern_choices).
const ZERO_CREDIT = shaped({ ...AI222, course_code: 'AI000', course_key: 'AI000::SEMINAR', credit_hours: 0, meeting_pattern_choices: [''] }, '2x100');

test('Add to scenario is never held for a course whose weekly meetings are not offered (one choice, or an older answer)', async t => {
  for (const [budget, key] of [[BUDGET, 'CS240::SYSTEMS'], [[{ course_key: 'AI101::PLAN', course_code: 'AI101', course_name: 'Plan' }], 'AI101::PLAN'],
    [[ZERO_CREDIT], 'AI000::SEMINAR']]) {
    const h = editor(t, { budget, answer: () => ({ ok: true, budget: [] }) });
    h.choose(key);
    await h.form.onsubmit({ preventDefault() {} });
    assert.deepEqual([h.calls.length, h.calls[0]?.body.course_key, h.alert()], [1, key, ''], key);
  }
});

test('a course in the plan shows its weekly meetings; a choice and its own Save only when its credit hours allow another', async t => {
  const h = editor(t);
  assert.equal(button(h.form, 'Save weekly meetings'), null, 'a new course has no Save of its own');
  h.choose('STAT103::PROBABILITY');
  const weekly = control(h.form, 'Weekly meetings');
  assert.match(h.form.textContent, /Weekly meetings now: 2 × 100 min/);
  assert.deepEqual([options(weekly), weekly.value], [[['', 'Standard (from credit hours)'], ['2x100', '2 × 100 min']], '2x100']);
  assert.ok(button(h.form, 'Save weekly meetings'));
  // A course on its standard pattern: what it meets now is its minutes; the
  // choice still names the pattern.
  h.choose('AI222::SEARCH');
  assert.equal(nowLine(h.form), 'Weekly meetings now: 2 × 75 min');
  assert.deepEqual([control(h.form, 'Weekly meetings').value, control(h.form, 'Weekly meetings').selectedOptions[0].textContent],
    ['', 'Standard (from credit hours)']);
  // A 4-credit course has one choice: shown, never offered.
  h.choose('CS240::SYSTEMS');
  assert.equal(nowLine(h.form), 'Weekly meetings now: 75 + 75 + 100 min');
  assert.equal([...h.form.querySelectorAll('label')].some(label => shown(label) && label.firstChild?.textContent === 'Weekly meetings'), false);
  assert.equal(button(h.form, 'Save weekly meetings'), null);
  // An older answer with no choices and no minutes: shown as standard, nothing offered.
  const old = editor(t, { budget: [{ course_key: 'AI101::PLAN', course_code: 'AI101', course_name: 'Plan' }] });
  old.choose('AI101::PLAN');
  assert.equal(nowLine(old.form), 'Weekly meetings now: Standard (from credit hours)');
  assert.equal(button(old.form, 'Save weekly meetings'), null);
  assert.equal(old.form.checkValidity(), true);
});

// ENG101 as compute_scenario_budget answers it: the English policy's four
// 180-minute meetings, no shape, no other choice.
const ENG101 = { ...AI222, course_code: 'ENG101', course_key: 'ENG101::ENGLISH_LANGUAGE_SKILLS_I', course_name: 'English Language Skills I', department: 'ENG',
  meeting_pattern_choices: [''], meeting_durations: [180, 180, 180, 180], required_meetings_per_section: 4 };

test('an English bundle’s weekly meetings are its four 180-minute meetings, never “from credit hours”, in English and in Arabic', async t => {
  for (const [arabic, expected] of [[false, 'Weekly meetings now: 4 × 180 min'], [true, 'اللقاءات الأسبوعية حالياً: ٤ × ١٨٠ دقيقة']]) {
    const h = editor(t, { arabic, budget: [...BUDGET, ENG101] });
    h.choose(ENG101.course_key);
    assert.equal(nowLine(h.form), expected);
    assert.doesNotMatch(shownText(h.form), /from credit hours|حسب الساعات المعتمدة/);
    assert.equal(button(h.form, arabic ? 'حفظ اللقاءات الأسبوعية' : 'Save weekly meetings'), null, 'nothing to choose');
  }
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
  // The page reads its budget again; this is no "Added". It is told which
  // course was saved (split view starts that course's placing again).
  assert.equal(h.saved.length, 1);
  assert.deepEqual(Object.entries(h.saved[0].change), [['action', 'meeting_pattern'], ['course_key', 'AI222::SEARCH']]);
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

test('back to the standard pattern: the save names it by its minutes, as the line above it does', async t => {
  for (const [arabic, expected, now] of [
    [false, `Saved: ${iso('STAT103')} is now set to 2 × 75 min.`, 'Weekly meetings now: 2 × 75 min'],
    [true, `حُفظ: ضُبط ${iso('STAT103')} على ٢ × ٧٥ دقيقة.`, 'اللقاءات الأسبوعية حالياً: ٢ × ٧٥ دقيقة'],
  ]) {
    const h = editor(t, { arabic });
    h.choose('STAT103::PROBABILITY');
    control(h.form, arabic ? 'اللقاءات الأسبوعية' : 'Weekly meetings').value = '';
    button(h.form, arabic ? 'حفظ اللقاءات الأسبوعية' : 'Save weekly meetings').click();
    await wait(() => h.status());
    assert.deepEqual([h.calls[0].body, h.status(), nowLine(h.form)], [{ course_key: 'STAT103::PROBABILITY', meeting_pattern: '' }, expected, now]);
  }
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
  assert.deepEqual(options(weekly), [['', 'افتراضي البرامج'], ['standard', 'قياسي (٢ × ٧٥ دقيقة)'], ['2x100', '٢ × ١٠٠ دقيقة']]);
  const hours = control(h.form, 'الساعات المعتمدة');
  hours.value = '4'; hours.dispatchEvent(new h.w.Event('input'));
  assert.equal(h.d.getElementById(weekly.getAttribute('aria-describedby')).textContent, '٢ × ١٠٠ دقيقة للمقررات ذات ٣ ساعات معتمدة فقط.');
  h.choose('STAT103::PROBABILITY');
  assert.equal(nowLine(h.form), 'اللقاءات الأسبوعية حالياً: ٢ × ١٠٠ دقيقة');
  assert.ok(button(h.form, 'حفظ اللقاءات الأسبوعية'));
  // On its standard pattern: its minutes, in the digits of the shape labels;
  // the choice keeps the pattern's name.
  h.choose('AI222::SEARCH');
  assert.equal(nowLine(h.form), 'اللقاءات الأسبوعية حالياً: ٢ × ٧٥ دقيقة');
  assert.equal(control(h.form, 'اللقاءات الأسبوعية').selectedOptions[0].textContent, 'قياسي (حسب الساعات المعتمدة)');
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

async function classic(t, { arabic = false, budget = () => BUDGET, readiness = READY, candidates = () => undefined, save = body => savedAnswer(body), editor = false,
  create = () => undefined } = {}) {
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
    else if (url === '/ops/tw/placements/create-planned/' && opts.method === 'POST') body = await create(JSON.parse(opts.body));
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

/* Place… after the course's weekly meetings changed under it (in another
   tab): the server refuses the pattern the dialog still offered with
   FULL_SECTION_PATTERN_REQUIRED, which the dialog says once it has read the
   patterns again. Meetings of another shape name the shape
   (MeetingShapeMismatch: `details`); another number of meetings names the
   number, exactly as before (no `details`). */
const shapeRefused = { httpError: 400, ok: false, error: { code: 'FULL_SECTION_PATTERN_REQUIRED',
  message: 'STAT103 meets 2 × 100 min a week in this scenario; these meetings are 75 + 75 min. Choose one of its 2 × 100 min patterns.',
  details: { course_code: 'STAT103', meeting_pattern: '2x100', expected_durations: [100, 100], placed_minutes: [75, 75] } } };
const countRefused = { httpError: 400, ok: false, error: { code: 'FULL_SECTION_PATTERN_REQUIRED', message: 'STAT103 needs a complete 2-meeting section pattern.' } };
const placeAlert = h => (h.d.getElementById('twPlaceAlert').hidden ? '' : h.d.getElementById('twPlaceAlert').textContent);

for (const [lang, refused, said] of [
  ['en', shapeRefused, 'This course now meets 2 × 100 min a week; the patterns were ranked again.'],
  ['ar', shapeRefused, 'يجتمع هذا المقرر الآن بواقع ٢ × ١٠٠ دقيقة أسبوعياً؛ أُعيد ترتيب الأنماط.'],
  ['en', countRefused, 'This course now needs 2 meetings a week; the patterns were ranked again.'],
  ['ar', countRefused, 'يحتاج هذا المقرر الآن إلى لقاءين أسبوعياً؛ أُعيد ترتيب الأنماط.'],
]) test(`classic Place (${lang}): a pattern refused for ${refused.error.details ? 'another weekly shape is said in the shape’s minutes' : 'another number of meetings is said as the number, as before'}`, async t => {
  // The patterns first read are two 75-minute meetings; read again, 2 × 100.
  const short = day => ({ kind: 'lect', day, start: '09:00', end: '10:15', slot_idx: 0 });
  let reads = 0;
  const creates = [];
  const h = await classic(t, {
    arabic: lang === 'ar',
    candidates: key => candidatesFor(key, reads++ ? [long('SUN', { room_type: 'lecture' }), long('TUE', { room_type: 'lecture' })] : [short('SUN'), short('TUE')]),
    create: body => { creates.push(body); return refused; },
  });
  await h.place('STAT103::PROBABILITY');
  h.d.getElementById('twPlaceCommit').click();
  await wait(() => placeAlert(h) && reads === 2 && h.d.querySelector('.tw-workspace').dataset.busy === 'false');
  assert.deepEqual(creates.map(body => body.meetings.map(m => `${m.day} ${m.start_time}-${m.end_time}`)), [['SUN 09:00-10:15', 'TUE 09:00-10:15']]);
  assert.equal(placeAlert(h), said);
  assert.deepEqual(h.notices, [], 'said in the dialog, never a toast under it');
  // The patterns on offer now are the shape's.
  assert.deepEqual(h.placeLines(), [lang === 'ar' ? 'الأحد 09:00–10:40، الثلاثاء 09:00–10:40' : 'Sunday 09:00–10:40, Tuesday 09:00–10:40']);
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
/* `answer(url, options)` answers a request first (undefined: the defaults
   below). An answer `{ response }` is the fetch's response as it is (a page
   that is not JSON); a `save` or `answer` that throws makes the fetch fail.
   `board`: pane A shows Term 7 (a 75-minute lecture column at 09:00 and a
   100-minute lab-grid column at 09:00), so a course's targets are drawn. */
const TERM7 = { id: 2, label: 'Term 7', nominal_term: 7, primary_count: 1, placement_count: 0 };
const term7Data = () => ({ board: TERM7, placements: [], slot_config: [{ start: '09:00', end: '10:15' }], lab_slot_config: [{ start: '09:00', end: '10:40' }] });
async function split(t, { arabic = false, budget = () => BUDGET, save = body => savedAnswer(body), answer = () => undefined, board = false } = {}) {
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
    let body = await answer(url, options);
    if (body === undefined) {
      if (url === '/ops/tw/scenarios/1/budget/meeting-pattern/' && options.method === 'POST') body = await save(JSON.parse(options.body));
      else if (url === '/ops/tw/scenarios/1/budget/') body = { budget: budget() };
      else if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
      else if (url === '/ops/tw/boards/2/') body = term7Data();
      else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [TERM7] };
      else if (url.endsWith('/conflicts/')) body = { overlaps: [], instructor_clashes: [], room_clashes: [] };
      else body = { ok: true, candidates: [], instructors: [], summary: {} };
    }
    if (body.response) return body.response;
    return { ok: !body.httpError, status: body.httpError || 200, headers: { get: () => null }, json: async () => body };
  };
  w.eval(read('static/js/shared-utils.js'));
  for (const file of ['timetable-meeting-policy', 'timetable-slot-config', 'timetable-section-teacher', 'timetable-programme-mark', 'timetable-course-editor']) {
    w.eval(read(`static/js/${file}.js`));
  }
  w.eval(`${read('static/js/page-timetable-workspace-split.js')}\n;globalThis.__splitAudit = { S, SB, COPY_ACTIVITY };`);
  await settle();
  const { S: state, SB: sidebar, COPY_ACTIVITY: copying } = w.__splitAudit;
  state.scenarioId = 1; state.scenarioMeta = { id: 1, status: 'draft' };
  if (board) {
    state.boards = [TERM7]; state.cols = 1; state.rows = 1;
    state.panes[0].boardId = 2; state.panes[0].boardData = term7Data();
    w.renderPane(0);
  }
  sidebar.budget = budget();
  w.renderSidebar();
  const chips = key => [...d.querySelector(`.tws-sec-item[data-key="${key}"]`).querySelectorAll('.meta .dem')].map(span => span.textContent);
  // The create-assist targets on show, as their buttons name them (rank left out).
  const targets = () => [...d.querySelectorAll('.tws-create-target')].map(button => button.textContent.trim().replace(/^\d+/, ''));
  const candidateReads = () => requests.filter(row => row.url.includes('/planned-slot-candidates/'))
    .map(row => new URL(row.url, 'http://split.test').searchParams.get('course_key'));
  const budgetReads = () => requests.filter(row => row.url === '/ops/tw/scenarios/1/budget/' && (row.options.method || 'GET') === 'GET').length;
  return { w, d, requests, notices, state, sidebar, copying, chips, targets, candidateReads, budgetReads };
}
// A wait on the page's own timers (a preview read again soon after the last
// one waits PREVIEW_DEBOUNCE_MS, 280 ms, before it asks), up to 3 s.
const waitTimers = async condition => {
  for (let i = 0; i < 150; i++) { if (condition()) return; await new Promise(resolve => setTimeout(resolve, 20)); }
  assert.fail('UI did not settle');
};

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

/* ── Split view: the section being placed (create-assist) follows the course's weekly meetings ── */
/* The targets the server offers a course for its weekly meetings now
   (/planned-slot-candidates/, from its budget row): one 75-minute cell while
   it is on its credit hours' pattern with no section placed, a whole 2 × 75
   pattern once a section is placed that way, and a whole 2 × 100 pattern,
   lectures in the 100-minute columns, once it is set to 2 × 100 min. */
const offered = row => {
  if (row.meeting_pattern === '2x100') {
    return [{ candidate_id: 'p100', kind: 'lab', is_pattern: true, requires_full_section_pattern: true, day: 'MON', start: '09:00', end: '10:40', tone: 'clean', score: 1,
      meetings: [{ kind: 'lab', day: 'MON', start: '09:00', end: '10:40', room_type: 'lecture' }, { kind: 'lab', day: 'WED', start: '09:00', end: '10:40', room_type: 'lecture' }] }];
  }
  if (row.requires_full_section_pattern) {
    return [{ candidate_id: 'p75', kind: 'lect', is_pattern: true, requires_full_section_pattern: true, day: 'SUN', start: '09:00', end: '10:15', tone: 'clean', score: 1,
      meetings: [{ kind: 'lect', day: 'SUN', start: '09:00', end: '10:15' }, { kind: 'lect', day: 'TUE', start: '09:00', end: '10:15' }] }];
  }
  return [{ kind: 'lect', day: 'SUN', start: '09:00', end: '10:15', tone: 'clean', score: 1 }];
};
const offeredNow = (rows, url) => {
  const row = rows.find(item => item.course_key === new URL(url, 'http://split.test').searchParams.get('course_key'));
  return { ok: true, status: 'ready', board: { id: 2, label: 'Term 7', nominal_term: 7 }, candidates: row ? offered(row) : [] };
};
// AI222 with no section placed: a section starts as one 75-minute cell (the
// server's one-cell fallback); with S1 placed as two 75-minute meetings, S2
// is a whole 2 × 75 pattern. Set to 2 × 100 min, a section is a whole 2 × 100
// pattern either way.
const AI222_NONE = { ...AI222, required_meetings_per_section: 1, requires_full_section_pattern: false };
const AI222_S1 = { ...AI222, planned_sections: 2, used_sections: 1, used_section_labels: ['S1'], remaining_sections: 1, requires_full_section_pattern: true };
const as2x100 = row => ({ ...shaped(row, '2x100'), requires_full_section_pattern: true });
const SINGLE = 'Pane A · Term 7 · Slot SUN 09:00';
const PATTERN_75 = 'Pane A - Term 7 - Pattern SUN 09:00, TUE 09:00';
const PATTERN_100 = 'Pane A - Term 7 - Pattern MON 09:00, WED 09:00';
const placeAssist = (h, key) => h.d.querySelector(`.tws-sec-item[data-key="${key}"]`).click();
const createdMeetings = posts => posts.map(body => body.meetings.map(m => `${m.day} ${m.start_time}-${m.end_time}`));

test('split: a weekly-meetings save starts the placing of that course again, from its row and its targets as read now; another course’s save leaves it', async t => {
  let budget = [STAT103, AI222_NONE, CS240];
  const h = await split(t, {
    board: true,
    budget: () => budget,
    save: body => {
      budget = budget.map(row => (row.course_key !== body.course_key ? row : body.meeting_pattern === '2x100' ? as2x100(row) : shaped(row, '')));
      return { ok: true, course_key: body.course_key, meeting_pattern: body.meeting_pattern, previous: '', changed: true, budget, mismatched_sections: [] };
    },
    answer: url => (url.includes('/planned-slot-candidates/') ? offeredNow(budget, url) : undefined),
  });
  placeAssist(h, 'AI222::SEARCH');
  await wait(() => h.targets().length);
  assert.deepEqual(h.targets(), [SINGLE], 'one 75-minute cell, as its credit hours place it');

  h.d.getElementById('twsAddCourse').click();
  const form = h.d.querySelector(EDITOR);
  const course = control(form, 'Course');
  course.value = 'AI222::SEARCH'; course.dispatchEvent(new h.w.Event('change'));
  control(form, 'Weekly meetings').value = '2x100';
  button(form, 'Save weekly meetings').click();
  await waitTimers(() => h.targets()[0] === PATTERN_100);
  assert.deepEqual(h.targets(), [PATTERN_100]);
  assert.deepEqual(h.candidateReads(), ['AI222::SEARCH', 'AI222::SEARCH']);
  const payload = h.state.createAssist.payload;
  assert.deepEqual([payload.course_key, payload.section_label, payload.required_meetings_per_section, payload.requires_full_section_pattern],
    ['AI222::SEARCH', 'S1', 2, true], 'the payload is the row as read now: a whole two-meeting pattern');
  assert.deepEqual(h.notices, []);

  // Another course's weekly meetings: AI222's placing goes on as it is.
  const token = h.state.createAssist.requestToken;
  const reads = h.budgetReads();
  course.value = 'STAT103::PROBABILITY'; course.dispatchEvent(new h.w.Event('change'));
  control(form, 'Weekly meetings').value = '';
  button(form, 'Save weekly meetings').click();
  await wait(() => h.budgetReads() === reads + 1 && form.querySelector('[role=status]').textContent);
  for (let i = 0; i < 4; i++) await settle();
  assert.equal(h.state.createAssist.requestToken, token, 'not started again');
  assert.deepEqual([h.targets(), h.candidateReads().length], [[PATTERN_100], 2]);
});

for (const [lang, before, refused, said, label] of [
  ['en', AI222_S1, 'shape', 'AI222 meets 2 × 100 min a week in this scenario; these meetings are 75 + 75 min. Choose one of its 2 × 100 min patterns.', PATTERN_75],
  ['ar', AI222_S1, 'shape', `لم يُوضع شيء: يجتمع ${iso('AI222')} في هذا السيناريو بواقع ٢ × ١٠٠ دقيقة أسبوعياً.`, PATTERN_75],
  ['en', AI222_NONE, 'count', 'AI222 needs a complete 2-meeting section pattern.', SINGLE],
  ['ar', AI222_NONE, 'count', 'لم يُوضع شيء: يحتاج هذا المقرر الآن إلى نمطه الأسبوعي كاملاً.', SINGLE],
]) test(`split (${lang}): a target refused for its weekly ${refused === 'shape' ? 'shape' : 'number of meetings'} (changed in another tab) is said, and the placing starts again for the course’s weekly meetings now`, async t => {
  let budget = [STAT103, before, CS240];
  const creates = [];
  // The server's refusals (core/timetable_workspace_views.py): MeetingShapeMismatch
  // names the shape in `details`; a wrong number of meetings names none.
  const refusal = refused === 'shape'
    ? { httpError: 400, ok: false, error: { code: 'FULL_SECTION_PATTERN_REQUIRED', message: 'AI222 meets 2 × 100 min a week in this scenario; these meetings are 75 + 75 min. Choose one of its 2 × 100 min patterns.',
      details: { course_code: 'AI222', meeting_pattern: '2x100', expected_durations: [100, 100], placed_minutes: [75, 75] } } }
    : { httpError: 400, ok: false, error: { code: 'FULL_SECTION_PATTERN_REQUIRED', message: 'AI222 needs a complete 2-meeting section pattern.' } };
  const h = await split(t, {
    arabic: lang === 'ar',
    board: true,
    budget: () => budget,
    answer: (url, options) => {
      if (url.includes('/planned-slot-candidates/')) return offeredNow(budget, url);
      if (url === '/ops/tw/placements/create-planned/') { creates.push(JSON.parse(options.body)); return refusal; }
      return undefined;
    },
  });
  placeAssist(h, 'AI222::SEARCH');
  await wait(() => h.targets().length);
  assert.deepEqual(h.targets(), [label]);
  // Another tab sets AI222 to 2 × 100 min; this one still shows the old targets.
  budget = [STAT103, as2x100(before), CS240];
  const reads = h.budgetReads();
  h.d.querySelector('.tws-create-target').click();
  await waitTimers(() => h.targets()[0] === PATTERN_100);
  assert.deepEqual(createdMeetings(creates), [refused === 'shape' ? ['SUN 09:00-10:15', 'TUE 09:00-10:15'] : ['SUN 09:00-10:15']]);
  assert.deepEqual(h.notices, [{ kind: 'warning', message: said }], 'said once, as a warning; never only to the console');
  assert.equal(h.budgetReads(), reads + 1, 'the budget is read again');
  assert.deepEqual(h.targets(), [PATTERN_100]);
  const payload = h.state.createAssist.payload;
  assert.deepEqual([payload.section_label, payload.required_meetings_per_section, payload.requires_full_section_pattern], [before.used_sections ? 'S2' : 'S1', 2, true]);
});

test('split: a course the budget no longer lists (or a budget that could not be read) ends its placing, said on the status line', async t => {
  let unreadable = false;
  const h = await split(t, {
    board: true,
    budget: () => [STAT103, AI222_NONE, CS240],
    save: body => ({ ok: true, course_key: body.course_key, meeting_pattern: body.meeting_pattern, previous: '', changed: true,
      budget: [STAT103, as2x100(AI222_NONE), CS240], mismatched_sections: [] }),
    answer: (url, options) => {
      if (url.includes('/planned-slot-candidates/')) return offeredNow([AI222_NONE], url);
      if (url === '/ops/tw/scenarios/1/budget/' && (options.method || 'GET') === 'GET' && unreadable) return { httpError: 500, ok: false, error: { code: 'SERVER_ERROR', message: 'Server error.' } };
      return undefined;
    },
  });
  placeAssist(h, 'AI222::SEARCH');
  await wait(() => h.targets().length);
  unreadable = true;
  h.d.getElementById('twsAddCourse').click();
  const form = h.d.querySelector(EDITOR);
  const course = control(form, 'Course');
  course.value = 'AI222::SEARCH'; course.dispatchEvent(new h.w.Event('change'));
  control(form, 'Weekly meetings').value = '2x100';
  button(form, 'Save weekly meetings').click();
  await wait(() => !h.state.createAssist.active);
  assert.deepEqual([h.targets(), h.d.getElementById('twsStatusHover').textContent, h.candidateReads().length],
    [[], 'Section placement mode cleared', 1], 'no stale targets are left to click');
});

test('split: a shape refusal starts again only the placing it refused, not one begun while the budget was read', async t => {
  let budget = [STAT103, AI222_S1, CS240];
  let held = null;
  const h = await split(t, {
    board: true,
    budget: () => budget,
    answer: url => {
      if (url.includes('/planned-slot-candidates/')) return offeredNow(budget, url);
      if (url === '/ops/tw/placements/create-planned/') return { httpError: 400, ok: false, error: { code: 'FULL_SECTION_PATTERN_REQUIRED', message: 'AI222 needs a complete 2-meeting section pattern.' } };
      if (url === '/ops/tw/scenarios/1/budget/' && held) return held.then(() => ({ budget }));
      return undefined;
    },
  });
  placeAssist(h, 'AI222::SEARCH');
  await wait(() => h.targets().length);
  budget = [STAT103, as2x100(AI222_S1), CS240];
  let release;
  held = new Promise(resolve => { release = resolve; });
  h.d.querySelector('.tws-create-target').click();
  await wait(() => h.notices.length === 1 && h.budgetReads() === 1);
  // While the budget is read, the user starts placing STAT103.
  placeAssist(h, 'STAT103::PROBABILITY');
  await waitTimers(() => h.targets()[0] === PATTERN_100 && h.candidateReads().length === 2);
  const token = h.state.createAssist.requestToken;
  release();
  // The budget read has ended: the sidebar names AI222's new weekly meetings.
  await wait(() => h.chips('AI222::SEARCH').includes('2 × 100 min'));
  for (let i = 0; i < 4; i++) await settle();
  assert.deepEqual([h.state.createAssist.payload.course_key, h.state.createAssist.requestToken], ['STAT103::PROBABILITY', token], 'STAT103’s placing is left as it is');
  assert.deepEqual(h.candidateReads(), ['AI222::SEARCH', 'STAT103::PROBABILITY']);
});

test('split: a target refused for a reason the page does not word is offered again, the placing kept, nothing read again', async t => {
  const creates = [];
  const h = await split(t, {
    board: true,
    budget: () => [STAT103, AI222_S1, CS240],
    answer: (url, options) => {
      if (url.includes('/planned-slot-candidates/')) return offeredNow([AI222_S1], url);
      if (url === '/ops/tw/placements/create-planned/') {
        creates.push(JSON.parse(options.body));
        return { httpError: 400, ok: false, error: { code: 'VALIDATION_PLANNED_PATTERN', message: 'Invalid planned meeting time.' } };
      }
      return undefined;
    },
  });
  placeAssist(h, 'AI222::SEARCH');
  await wait(() => h.targets().length);
  const target = h.d.querySelector('.tws-create-target');
  const reads = h.budgetReads();
  target.click();
  await wait(() => creates.length === 1);
  for (let i = 0; i < 6; i++) await settle();
  assert.deepEqual([target.isConnected, target.disabled, h.state.createAssist.active, h.targets()], [true, false, true, [PATTERN_75]]);
  assert.deepEqual([h.notices, h.budgetReads(), h.candidateReads().length], [[], reads, 1]);
});

/* The editor's request on the split page: only the server's own refusal (or
   the page's copy-in-progress words) reaches the dialog; anything else is
   the editor's own message, never an HTTP status or a script's error. */
const notJson = status => ({ response: { ok: status < 400, status, headers: { get: () => null },
  json: async () => { throw new SyntaxError('Unexpected token \'<\', "<!DOCTYPE "... is not valid JSON'); } } });

test('split: Save weekly meetings says its own message for a page that is not JSON, no answer or the login page, never a status or an error', async t => {
  for (const [what, reply] of [
    ['a 403 page that is not JSON', () => notJson(403)],
    ['a proxy’s 502 page', () => notJson(502)],
    ['no answer at all', () => { throw new TypeError('Failed to fetch'); }],
    ['the login page, after the session expired', () => notJson(200)],
  ]) {
    const h = await split(t, { save: reply });
    h.d.getElementById('twsAddCourse').click();
    const form = h.d.querySelector(EDITOR);
    const course = control(form, 'Course');
    course.value = 'STAT103::PROBABILITY'; course.dispatchEvent(new h.w.Event('change'));
    button(form, 'Save weekly meetings').click();
    await wait(() => form.querySelector('[role=alert]').textContent);
    assert.equal(form.querySelector('[role=alert]').textContent, 'Could not save the weekly meetings. Check the scenario status, then retry.', what);
    assert.deepEqual([h.notices, form.querySelector('[role=status]').textContent], [[], ''], what);
  }
});

test('split: Add to scenario says its own message for a page that is not JSON; the copy-in-progress words are said before any request', async t => {
  const h = await split(t, { answer: (url, options) => (url === '/ops/tw/scenarios/1/budget/' && options.method === 'POST' ? notJson(500) : undefined) });
  h.d.getElementById('twsAddCourse').click();
  const form = h.d.querySelector(EDITOR);
  control(form, 'Course code').value = 'AI999';
  await form.onsubmit({ preventDefault() {} });
  assert.equal(form.querySelector('[role=alert]').textContent, 'Could not add. Check the values and scenario status, then retry.');
  assert.deepEqual(h.notices, []);

  for (const arabic of [false, true]) {
    const c = await split(t, { arabic });
    c.copying.pending = 'copy-1';
    c.d.getElementById('twsAddCourse').click();
    const editorForm = c.d.querySelector(EDITOR);
    const chosen = control(editorForm, arabic ? 'المقرر' : 'Course');
    chosen.value = 'STAT103::PROBABILITY'; chosen.dispatchEvent(new c.w.Event('change'));
    button(editorForm, arabic ? 'حفظ اللقاءات الأسبوعية' : 'Save weekly meetings').click();
    await wait(() => editorForm.querySelector('[role=alert]').textContent);
    assert.equal(editorForm.querySelector('[role=alert]').textContent, arabic ? 'انتظر انتهاء نسخ السيناريو.' : 'Wait for the scenario copy to finish.');
    assert.deepEqual([c.notices, c.requests.filter(row => row.options.method === 'POST')], [[], []], 'said in the dialog, nothing sent');
  }
});
