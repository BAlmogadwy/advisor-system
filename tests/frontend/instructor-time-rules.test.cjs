/* Teaching-time rules (static/js/instructor-time-rules.js) on Instructor
 * Management: the JavaScript mirror of core/services/instructor_availability.py
 * held to the shared parity fixture, and the rules dialog driven from the
 * real template and page script — quick adds, toggles, All day, the live
 * preview, the save and its answers (validation, STALE), the kept draft,
 * the roster pill, Arabic and typed Arabic-Indic digits.
 *
 * Run by tests/test_instructor_time_rules_frontend.py, which hands over the
 * server's default period grid (ITR_GRID_FILE); run alone, the copy below
 * stands in for it. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const template = read('core/templates/core/instructor_management.html');
const FIXTURE = JSON.parse(read('tests/fixtures/instructor_availability_cases.json'));
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const settle = async () => { for (let i = 0; i < 8; i++) await tick(); };

// The default teaching periods (instructor_availability.default_period_grid).
const GRID_COPY = {
  days: ['SUN', 'MON', 'TUE', 'WED', 'THU'],
  lecture: [
    { label: '09:00-10:15', start: '09:00', end: '10:15' },
    { label: '10:30-11:45', start: '10:30', end: '11:45' },
    { label: '10:50-12:05', start: '10:50', end: '12:05' },
    { label: '13:00-14:15', start: '13:00', end: '14:15' },
    { label: '14:30-15:45', start: '14:30', end: '15:45' },
    { label: '14:45-16:00', start: '14:45', end: '16:00' },
    { label: '16:00-17:15', start: '16:00', end: '17:15' },
  ],
  lab: [
    { label: 'Lab 1', start: '09:00', end: '10:40' },
    { label: 'Lab 2', start: '10:45', end: '12:25' },
    { label: 'Lab 3', start: '13:00', end: '14:40' },
    { label: 'Lab 4', start: '14:45', end: '16:25' },
    { label: 'Lab 5', start: '16:30', end: '18:10' },
  ],
};
const SERVER_GRID = process.env.ITR_GRID_FILE ? JSON.parse(fs.readFileSync(process.env.ITR_GRID_FILE, 'utf8')) : null;
const GRID = SERVER_GRID || GRID_COPY;

const ALL = ['SUN', 'MON', 'TUE', 'WED', 'THU'];
const rule = (kind, days, start, end, note = '') => ({ kind, days, start, end, note });
const fixtureCase = name => {
  const found = FIXTURE.cases.find(c => c.name === name);
  assert.ok(found, `no fixture case named ${name}`);
  return found;
};
// Arabic-Indic (U+0660..) and Eastern Arabic-Indic (U+06F0..) digits, built
// from code points so nothing typed here is an escape.
const arabicIndic = text => text.replace(/[0-9]/g, d => String.fromCharCode(0x0660 + Number(d)));
const easternIndic = text => text.replace(/[0-9]/g, d => String.fromCharCode(0x06F0 + Number(d)));

const OPEN_ANY = { lecture: { open: 35, total: 35 }, lab: { open: 25, total: 25 }, state: 'any' };
const INSTRUCTORS = [
  { id: 1, full_name: 'Dr Hala Omar', full_name_ar: 'د. هالة عمر', email: 'hala@example.edu', employee_no: 'E-1', department: 'CS', max_weekly_hours: 12, is_active: true, time_rules: { count: 0, open: OPEN_ANY } },
  { id: 2, full_name: 'Dr Sami Noor', full_name_ar: '', email: '', employee_no: '', department: 'AI', max_weekly_hours: null, is_active: true,
    time_rules: { count: 1, open: { lecture: { open: 20, total: 35 }, lab: { open: 15, total: 25 }, state: 'limited' } } },
  { id: 3, full_name: 'Dr Thursday Only', full_name_ar: '', email: '', employee_no: '', department: '', max_weekly_hours: null, is_active: true,
    time_rules: { count: 1, open: { lecture: { open: 7, total: 35 }, lab: { open: 5, total: 25 }, state: 'narrow' } } },
  { id: 4, full_name: 'Dr Midday', full_name_ar: '', email: '', employee_no: '', department: '', max_weekly_hours: null, is_active: true,
    time_rules: { count: 1, open: { lecture: { open: 0, total: 35 }, lab: { open: 0, total: 25 }, state: 'none' } } },
];

/* The template in one language: each {% if LANGUAGE_CODE == 'ar' %} branch
   chosen, every other tag removed. */
function renderTemplate(arabic) {
  let html = template.replace(/<script[\s\S]*?<\/script>/g, '');
  html = html.replace(/{% if LANGUAGE_CODE == 'ar' %}([\s\S]*?){% else %}([\s\S]*?){% endif %}/g, (_, ar, en) => (arabic ? ar : en));
  assert.doesNotMatch(html, /{% (if|else|endif)/, 'every language branch is chosen');
  return html.replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
}

/* The page with a server: `saved` holds each instructor's rules and
   revision; `answer(url, opts, server)` may replace any response. */
async function page(t, { arabic = false, answer = () => undefined, saved = {} } = {}) {
  const dom = new JSDOM(`<!doctype html><html lang="${arabic ? 'ar' : 'en'}" dir="${arabic ? 'rtl' : 'ltr'}"><body><main>${renderTemplate(arabic)}</main></body></html>`,
    { runScripts: 'outside-only', url: 'http://localhost/instructor-management/' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, posts = [], gets = [], notices = [];
  const server = {
    rules: { 1: [], 2: [rule('unavailable', ALL, '00:00', '13:00')], 3: [], 4: [], ...saved },
    revision: { 1: 'rev1a', 2: 'rev2a', 3: 'rev3a', 4: 'rev4a' },
    saves: 0,
  };
  w.eval(read('static/js/shared-utils.js'));
  w.eval(read('static/js/ux-controls.js'));
  w.djCsrfToken = 'x';
  w.notify = Object.fromEntries(['success', 'error', 'warning', 'info'].map(type => [type, text => notices.push({ type, text })]));
  w.dlg = { confirm: async () => true };
  const brief = id => {
    const row = INSTRUCTORS.find(i => i.id === id);
    return { id, full_name: row.full_name, full_name_ar: row.full_name_ar, is_active: row.is_active };
  };
  const openOf = rules => w.InstructorTimeRules.summary(w.InstructorTimeRules.compile(rules), GRID);
  w.fetch = async (url, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase();
    if (method === 'POST') posts.push({ url, body: JSON.parse(opts.body || '{}') }); else gets.push(url);
    let body = answer(url, opts, server);
    if (body === undefined) {
      if (url.startsWith('/ops/instructors/list/')) body = { ok: true, instructors: JSON.parse(JSON.stringify(INSTRUCTORS)) };
      else if (url.startsWith('/ops/instructors/time-rules/?')) {
        const id = Number(new URL(url, 'http://localhost').searchParams.get('instructor_id'));
        const rules = server.rules[id];
        body = { ok: true, instructor: brief(id), rules, revision: server.revision[id], open: openOf(rules), grid: GRID };
      } else if (url === '/ops/instructors/time-rules/set/') {
        const sent = JSON.parse(opts.body);
        server.saves += 1;
        server.rules[sent.instructor_id] = sent.rules.map(r => ({ ...r, all_day: r.start === '00:00' && r.end === '24:00' }));
        server.revision[sent.instructor_id] = `rev${sent.instructor_id}-${server.saves}`;
        const rules = server.rules[sent.instructor_id];
        body = { ok: true, instructor: brief(sent.instructor_id), rules, revision: server.revision[sent.instructor_id], open: openOf(rules) };
      } else if (url.startsWith('/ops/instructors/course-assignments/?')) body = { ok: true, courses: [] };
      else body = { ok: true };
    }
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  w.eval(read('static/js/instructor-time-rules.js'));
  w.eval(read('static/js/page-instructor-management.js'));
  w.UXControls.wire();
  if (d.readyState === 'loading') await new Promise(resolve => d.addEventListener('DOMContentLoaded', resolve, { once: true }));
  else d.dispatchEvent(new w.Event('DOMContentLoaded'));
  await settle();
  d.getElementById('imTabBtnRoster').click();
  await settle();
  const $ = id => d.getElementById(id);
  const dialog = $('imRulesDialog');
  const h = {
    w, d, $, posts, gets, notices, server, dialog,
    isOpen: () => w.UXControls.dialog.isOpen(dialog),
    async open(id) {
      const button = d.querySelector(`[data-im-rules="${id}"]`);
      button.focus();
      button.click();
      await settle();
      return button;
    },
    quick: async key => { d.querySelector(`[data-itr-quick="${key}"]`).click(); await settle(); },
    rules: () => [...d.querySelectorAll('#imRulesList > li')],
    field: (index, field) => h.rules()[index].querySelector(`[data-field="${field}"]`),
    pressed: index => [...h.rules()[index].querySelectorAll('[data-day]')].map(b => b.getAttribute('aria-pressed') === 'true'),
    type(index, field, value) {
      const input = h.field(index, field);
      input.focus();
      input.value = value;
      input.dispatchEvent(new w.Event('input', { bubbles: true }));
    },
    blur(index, field) {
      h.field(index, field).dispatchEvent(new w.FocusEvent('focusout', { bubbles: true }));
    },
    escape: async () => {
      (d.activeElement && dialog.contains(d.activeElement) ? d.activeElement : dialog)
        .dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await settle();
    },
    save: async () => { $('imRulesForm').requestSubmit($('imRulesSave')); await settle(); },
    counts: () => $('imRulesCounts').textContent,
    warning: () => $('imRulesWarning').textContent,
    shapes: () => Object.fromEntries([...d.querySelectorAll('#imRulesShapes li')].map(li => [li.dataset.shape, li.dataset.feasible === 'true'])),
    pill: id => d.querySelector(`[data-im-rules="${id}"]`).closest('tr').querySelector('.im-times-pill'),
  };
  return h;
}

// ── The mirror ─────────────────────────────────────────────────

test('parity: every fixture case gives the server module\'s probes, open counts and summary', () => {
  const dom = new JSDOM('<!doctype html><html lang="en"><body></body></html>', { runScripts: 'outside-only' });
  dom.window.eval(read('static/js/shared-utils.js'));
  dom.window.eval(read('static/js/instructor-time-rules.js'));
  const R = dom.window.InstructorTimeRules;
  assert.ok(FIXTURE.cases.length >= 25, `${FIXTURE.cases.length} cases`);
  for (const c of FIXTURE.cases) {
    const compiled = R.compile(c.rules);
    assert.equal(compiled.skipped, 0, `${c.name}: every fixture rule is valid`);
    for (const p of c.probes) {
      const day = R.DAY_CODES.indexOf(p.day);
      assert.equal(R.blocks(compiled, day, R.toMinutes(p.start), R.toMinutes(p.end)), p.blocked,
        `${c.name}: ${p.day} ${p.start}-${p.end}`);
    }
    const count = table => table.flat().filter(Boolean).length;
    assert.deepEqual({ lecture: count(R.openPeriods(compiled, GRID.lecture)), lab: count(R.openPeriods(compiled, GRID.lab)) },
      c.open, `${c.name}: open counts`);
    // JSON through the page's own realm, so objects compare by value; the
    // key order and the shape order are the server's too.
    const summary = JSON.parse(JSON.stringify(R.summary(compiled, GRID)));
    assert.deepEqual(summary, c.summary, `${c.name}: summary`);
    assert.deepEqual(Object.keys(summary), ['lecture', 'lab', 'days', 'shapes', 'no_teaching_days', 'state']);
    assert.deepEqual(summary.shapes.map(s => s.key), ['1x75', '1x100', '2x75', '2x100', '2x75+100']);
  }
});

test('the default grid copy is the server\'s grid', { skip: !SERVER_GRID && 'run through pytest to compare' }, () => {
  assert.deepEqual(GRID_COPY, SERVER_GRID);
});

test('times: the API\'s reading, canonical HH:MM, Arabic-Indic digits of both ranges, bare HHMM', () => {
  const dom = new JSDOM('<!doctype html><html lang="en"><body></body></html>', { runScripts: 'outside-only' });
  dom.window.eval(read('static/js/shared-utils.js'));
  dom.window.eval(read('static/js/instructor-time-rules.js'));
  const R = dom.window.InstructorTimeRules;
  assert.equal(R.toMinutes('09:00'), 540);
  assert.equal(R.toMinutes(' 9:05 '), 545);
  assert.equal(R.toMinutes('24:00'), 1440);
  assert.equal(R.toMinutes('24:00', { end: false }), null, '24:00 is an end only');
  assert.equal(R.toMinutes('23:60'), null);
  assert.equal(R.toMinutes('09:00:00'), null, 'no seconds on the wire');
  assert.equal(R.toMinutes(540), null, 'a number is not a time');
  assert.equal(R.toMinutes(arabicIndic('09:00')), null, 'the wire is ASCII only');
  assert.equal(R.toHhmm(0), '00:00');
  assert.equal(R.toHhmm(545), '09:05');
  assert.equal(R.toHhmm(1440), '24:00');
  const cases = {
    '0900': '09:00', '900': '09:00', '9:00': '09:00', ' 9:00 ': '09:00', '1330': '13:30', '2400': '24:00', '13:30': '13:30',
    [arabicIndic('09:00')]: '09:00', [easternIndic('09:00')]: '09:00', [arabicIndic('1330')]: '13:30', [easternIndic('930')]: '09:30',
    '9': '9', 'abc': 'abc', '': '',
  };
  for (const [typed, expected] of Object.entries(cases)) assert.equal(R.normaliseTime(typed), expected, JSON.stringify(typed));
});

test('validate: the server\'s codes, indexes and order, a duplicate only on an otherwise valid rule', () => {
  const dom = new JSDOM('<!doctype html><html lang="en"><body></body></html>', { runScripts: 'outside-only' });
  dom.window.eval(read('static/js/shared-utils.js'));
  dom.window.eval(read('static/js/instructor-time-rules.js'));
  const R = dom.window.InstructorTimeRules;
  const v = rules => JSON.parse(JSON.stringify(R.validate(rules)));
  assert.deepEqual(v([rule('unavailable', ['SUN'], '09:00', '10:00')]), []);
  assert.deepEqual(v('x'), [{ index: -1, field: 'rules', code: 'rules_not_list' }]);
  assert.deepEqual(v([rule('sometimes', [], '24:00', '9')]), [
    { index: 0, field: 'kind', code: 'kind_invalid' },
    { index: 0, field: 'days', code: 'days_empty' },
    { index: 0, field: 'start', code: 'start_invalid' },
    { index: 0, field: 'end', code: 'end_invalid' },
  ]);
  assert.deepEqual(v([rule('unavailable', ['FRI'], '10:00', '10:00', 'x'.repeat(201))]), [
    { index: 0, field: 'days', code: 'days_invalid' },
    { index: 0, field: 'end', code: 'end_not_after_start' },
    { index: 0, field: 'note', code: 'note_too_long' },
  ]);
  assert.deepEqual(v([rule('unavailable', ['SUN', 'MON'], '09:00', '10:00', 'a'), rule('unavailable', ['MON', 'SUN'], '09:00', '10:00', 'b'), null]), [
    { index: 1, field: 'rule', code: 'duplicate' },
    { index: 2, field: 'rule', code: 'rule_not_object' },
  ]);
  assert.deepEqual(v(Array.from({ length: 21 }, (_, i) => rule('unavailable', ['SUN'], R.toHhmm(i * 10), R.toHhmm(i * 10 + 5)))),
    [{ index: -1, field: 'rules', code: 'too_many' }]);
});

// ── The dialog ─────────────────────────────────────────────────

test('the row button opens the dialog with the saved rules read fresh; Escape and Cancel give the keyboard back to it', async t => {
  const h = await page(t);
  assert.deepEqual([...h.d.querySelectorAll('#imInstructorTable thead th')].map(th => th.textContent.trim()).slice(5, 7), ['Teaching times', 'Status']);
  const button = await h.open(2);
  assert.equal(button.textContent.trim(), 'Times');
  assert.equal(button.getAttribute('aria-label'), 'Teaching times — Dr Sami Noor');
  assert.ok(h.gets.includes('/ops/instructors/time-rules/?instructor_id=2'));
  assert.equal(h.isOpen(), true);
  assert.equal(h.$('imRulesTitle').textContent, 'Teaching times — Dr Sami Noor');
  assert.equal(h.$('imRulesHelp').textContent, 'The timetable builder keeps this instructor\'s classes inside these times. Moves you make by hand are always allowed and are flagged.');
  assert.equal(h.rules().length, 1);
  assert.equal(h.field(0, 'kind').value, 'unavailable');
  assert.deepEqual(h.pressed(0), [true, true, true, true, true]);
  assert.deepEqual([h.field(0, 'start').value, h.field(0, 'end').value], ['00:00', '13:00']);
  assert.equal(h.d.activeElement, h.field(0, 'kind'), 'the keyboard starts on the first rule');
  await h.escape();
  assert.equal(h.isOpen(), false);
  assert.equal(h.d.activeElement, h.d.querySelector('[data-im-rules="2"]'));
  await h.open(2);
  h.$('imRulesCancel').click();
  await settle();
  assert.equal(h.isOpen(), false);
  assert.equal(h.d.activeElement, h.d.querySelector('[data-im-rules="2"]'));
  assert.equal(h.posts.length, 0, 'nothing is sent by closing');
  assert.equal(h.w.InstructorTimeRules.hasDraft(2), false, 'no change, no draft');
});

test('the help line tells the truth: it promises the builder only where the builder applies the rules', async t => {
  const enforced = "The timetable builder keeps this instructor's classes inside these times. Moves you make by hand are always allowed and are flagged.";
  const pending = 'The timetable builder does not apply these rules yet. They are saved here and take effect once teaching-time rules are switched on for the builder.';
  const pendingAr = 'لا يطبّق منشئ الجدول هذه القواعد بعد. تُحفظ هنا، ويبدأ العمل بها عند تفعيل قواعد أوقات التدريس في المنشئ.';
  for (const [flag, arabic, expected] of [['0', false, pending], ['0', true, pendingAr], ['1', false, enforced], ['', false, enforced]]) {
    const h = await page(t, { arabic });
    h.dialog.dataset.rulesEnforced = flag;
    await h.open(1);
    assert.equal(h.$('imRulesHelp').textContent, expected, `data-rules-enforced="${flag}"${arabic ? ' in Arabic' : ''}`);
    assert.equal(h.dialog.getAttribute('aria-describedby'), 'imRulesHelp');
  }
});

test('each quick add appends one filled, editable rule and puts the keyboard on what is left to choose', async t => {
  const h = await page(t);
  await h.open(1);
  assert.equal(h.$('imRulesEmpty').hidden, false);
  const expect = [
    ['no_mornings', 'unavailable', [true, true, true, true, true], false, '00:00', '13:00', 'kind'],
    ['no_afternoons', 'unavailable', [true, true, true, true, true], false, '13:00', '24:00', 'kind'],
    ['day_off', 'unavailable', [false, false, false, false, false], true, '00:00', '24:00', 'days'],
    ['only_days', 'available_only', [false, false, false, false, false], true, '00:00', '24:00', 'days'],
    ['only_between', 'available_only', [true, true, true, true, true], false, '09:00', '13:00', 'start'],
    ['add', 'unavailable', [false, false, false, false, false], false, '', '', 'kind'],
  ];
  for (const [key, kind, days, allDay, start, end, focus] of expect) {
    const before = h.rules().length;
    await h.quick(key);
    const index = h.rules().length - 1;
    assert.equal(index, before, `${key}: one rule added`);
    assert.equal(h.field(index, 'kind').value, kind, key);
    assert.deepEqual(h.pressed(index), days, key);
    assert.equal(h.field(index, 'all_day').checked, allDay, key);
    assert.deepEqual([h.field(index, 'start').value, h.field(index, 'end').value], [start, end], key);
    assert.equal(h.field(index, 'start').disabled, allDay, key);
    const target = focus === 'days' ? h.rules()[index].querySelector('[data-day]') : h.field(index, focus);
    assert.equal(h.d.activeElement, target, `${key}: the keyboard goes to ${focus}`);
    assert.equal(h.$('imRulesAnnounce').textContent, `Rule ${index + 1} added.`);
  }
  assert.equal(h.$('imRulesEmpty').hidden, true);
  assert.deepEqual(h.rules().map(li => li.querySelector('.im-rule-num').textContent), ['Rule 1', 'Rule 2', 'Rule 3', 'Rule 4', 'Rule 5', 'Rule 6']);
  assert.equal(h.rules()[2].querySelector('[data-itr-remove]').getAttribute('aria-label'), 'Remove rule 3');
  assert.equal(h.rules()[2].getAttribute('role'), 'group');
  assert.equal(h.rules()[2].querySelector('[data-field="days"]').getAttribute('aria-label'), 'Days of rule 3');
  // Every control has a name: its own label or aria-label.
  for (const control of h.d.querySelectorAll('#imRulesList input, #imRulesList select')) {
    const named = control.getAttribute('aria-label') || (control.id && h.d.querySelector(`label[for="${control.id}"]`)) || control.closest('label');
    assert.ok(named, `${control.outerHTML} has no name`);
  }
  for (const input of h.d.querySelectorAll('#imRulesList [data-field="start"], #imRulesList [data-field="end"]')) {
    assert.equal(input.type, 'text');
    assert.equal(input.getAttribute('inputmode'), 'numeric');
    assert.equal(input.getAttribute('dir'), 'ltr');
    assert.ok(input.classList.contains('ux-rows-time'));
  }
});

test('remove takes the rule out, renumbers the rest and keeps the keyboard in the list', async t => {
  const h = await page(t);
  await h.open(1);
  await h.quick('no_mornings');
  await h.quick('no_afternoons');
  await h.quick('only_between');
  h.rules()[1].querySelector('[data-itr-remove]').click();
  await settle();
  assert.deepEqual(h.rules().map(li => h.field(h.rules().indexOf(li), 'start').value), ['00:00', '09:00']);
  assert.equal(h.rules()[1].querySelector('.im-rule-num').textContent, 'Rule 2');
  assert.equal(h.d.activeElement, h.rules()[1].querySelector('[data-itr-remove]'), 'the next rule\'s Remove');
  h.rules()[1].querySelector('[data-itr-remove]').click();
  h.rules()[0].querySelector('[data-itr-remove]').click();
  await settle();
  assert.equal(h.rules().length, 0);
  assert.equal(h.$('imRulesEmpty').hidden, false);
  assert.equal(h.d.activeElement, h.$('imRulesAddRule'));
  assert.match(h.counts(), /Lecture periods open: 35 of 35/);
});

test('day toggles are pressed buttons; All day fills and locks the times and gives the typed ones back', async t => {
  const h = await page(t);
  await h.open(1);
  await h.quick('add');
  const toggles = [...h.rules()[0].querySelectorAll('[data-day]')];
  assert.deepEqual(toggles.map(b => [b.tagName, b.type, b.textContent]), [['BUTTON', 'button', 'Sun'], ['BUTTON', 'button', 'Mon'], ['BUTTON', 'button', 'Tue'], ['BUTTON', 'button', 'Wed'], ['BUTTON', 'button', 'Thu']]);
  toggles[4].click();
  await settle();
  assert.deepEqual(h.pressed(0), [false, false, false, false, true]);
  h.type(0, 'start', '13:00');
  h.type(0, 'end', '15:00');
  assert.match(h.counts(), /Lecture periods open: 32 of 35/, 'Thu 13:00-14:15, 14:30-15:45, 14:45-16:00 blocked');
  toggles[4].click();
  await settle();
  assert.deepEqual(h.pressed(0), [false, false, false, false, false]);
  assert.match(h.counts(), /Lecture periods open: 35 of 35/, 'a rule with no day is left out of the preview');
  assert.equal(h.$('imRulesSkipped').hidden, false);
  toggles[0].click();
  const allDay = h.field(0, 'all_day');
  allDay.checked = true;
  allDay.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  await settle();
  assert.deepEqual([h.field(0, 'start').value, h.field(0, 'end').value, h.field(0, 'start').disabled, h.field(0, 'end').disabled], ['00:00', '24:00', true, true]);
  assert.match(h.counts(), /Lecture periods open: 28 of 35/);
  assert.equal(h.$('imRulesSkipped').hidden, true);
  allDay.checked = false;
  allDay.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  await settle();
  assert.deepEqual([h.field(0, 'start').value, h.field(0, 'end').value, h.field(0, 'start').disabled], ['13:00', '15:00', false]);
});

test('the live preview of the owner\'s examples: counts, course types, the days left and the warnings', async t => {
  const h = await page(t);
  await h.open(1);
  assert.equal(h.$('imRulesState').textContent.trim().startsWith('Any time'), true);
  assert.equal(h.warning(), '');
  const tables = [...h.d.querySelectorAll('#imRulesDialog table.im-grid')];
  assert.equal(tables.length, 2);
  assert.match(tables[0].querySelector('caption').textContent, /Lecture periods .* default teaching periods/);
  assert.match(tables[1].querySelector('caption').textContent, /Lab periods .* default teaching periods/);
  assert.deepEqual([...tables[0].querySelectorAll('thead th')].map(th => th.getAttribute('scope')), Array(8).fill('col'));
  assert.deepEqual([...tables[0].querySelectorAll('tbody th')].map(th => [th.getAttribute('scope'), th.textContent]), [['row', 'Sun'], ['row', 'Mon'], ['row', 'Tue'], ['row', 'Wed'], ['row', 'Thu']]);
  assert.equal(tables[0].querySelector('thead th bdi').getAttribute('dir'), 'ltr');

  // "No mornings": unavailable every day 00:00-13:00.
  await h.quick('no_mornings');
  assert.equal(h.counts(), 'Lecture periods open: 20 of 35 · Lab periods open: 15 of 25');
  assert.match(h.$('imRulesState').textContent, /Limited/);
  assert.equal(h.warning(), '');
  assert.equal(h.$('imRulesNoDays').hidden, true);
  const cell = tables => tables.querySelector('tbody tr td');
  const firstCell = cell(h.d.querySelector('#imRulesLectureGrid table'));
  assert.equal(firstCell.dataset.open, 'false');
  assert.equal(firstCell.querySelector('.ux-tone-icon').dataset.tone, 'critical', 'a shape, not colour alone');
  assert.equal(firstCell.textContent, 'Blocked');
  assert.equal(h.d.querySelector('#imRulesLectureGrid tbody tr td:nth-of-type(4)').textContent, 'Open');
  assert.deepEqual(h.shapes(), { '1x75': true, '1x100': true, '2x75': true, '2x100': true, '2x75+100': true });

  // "Only 11:00-13:00": no period lies wholly inside it.
  h.rules()[0].querySelector('[data-itr-remove]').click();
  await h.quick('only_between');
  h.type(0, 'start', '11:00');
  assert.equal(h.counts(), 'Lecture periods open: 0 of 35 · Lab periods open: 0 of 25');
  assert.equal(h.$('imRulesWarning').getAttribute('role'), 'status');
  assert.equal(h.$('imRulesWarning').dataset.tone, 'critical');
  assert.equal(h.warning(), 'With these rules the instructor has no teachable period; the builder will leave their sections without this teacher.');
  assert.equal(h.$('imRulesNoDays').textContent, 'No teaching on: Sun, Mon, Tue, Wed, Thu');
  assert.match(h.$('imRulesState').textContent, /No open period/);
  assert.deepEqual(Object.values(h.shapes()), [false, false, false, false, false]);

  // "Only Thursday": something is open, but no two-day course fits.
  h.rules()[0].querySelector('[data-itr-remove]').click();
  await h.quick('only_days');
  h.rules()[0].querySelectorAll('[data-day]')[4].click();
  await settle();
  assert.equal(h.counts(), 'Lecture periods open: 7 of 35 · Lab periods open: 5 of 25');
  assert.equal(h.$('imRulesWarning').dataset.tone, 'warning');
  assert.equal(h.warning(), 'Some course types cannot be scheduled for this instructor with these rules.');
  assert.equal(h.$('imRulesNoDays').textContent, 'No teaching on: Sun, Mon, Tue, Wed');
  assert.match(h.$('imRulesState').textContent, /Very limited/);
  assert.deepEqual(h.shapes(), { '1x75': true, '1x100': true, '2x75': false, '2x100': false, '2x75+100': false });
  const shapeRows = [...h.d.querySelectorAll('#imRulesShapes li')];
  assert.equal(shapeRows[0].textContent, '1-credit course (one 75-minute meeting)Possible');
  assert.equal(shapeRows[4].textContent, '4-credit course (three meetings on three different days)Not possible');
  assert.equal(shapeRows[4].querySelector('.ux-tone-icon').dataset.tone, 'critical');

  // "Never Thursday": the kind flips; every course type fits again.
  const kind = h.field(0, 'kind');
  kind.value = 'unavailable';
  kind.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  assert.equal(h.counts(), 'Lecture periods open: 28 of 35 · Lab periods open: 20 of 25');
  assert.equal(h.warning(), '');
  assert.equal(h.$('imRulesNoDays').textContent, 'No teaching on: Thu');
  assert.equal(h.$('imRulesNoDays').hidden, false);
});

test('save sends the rules with the revision they were based on; the pill changes and the keyboard is back on the row', async t => {
  const h = await page(t);
  assert.match(h.pill(1).textContent, /Any time/);
  await h.open(1);
  await h.quick('no_mornings');
  h.type(0, 'note', '  Council  ');
  await h.save();
  assert.deepEqual(h.posts.map(p => p.url), ['/ops/instructors/time-rules/set/']);
  assert.deepEqual(h.posts[0].body, {
    instructor_id: 1,
    expected_revision: 'rev1a',
    rules: [{ kind: 'unavailable', days: ['SUN', 'MON', 'TUE', 'WED', 'THU'], start: '00:00', end: '13:00', note: 'Council' }],
  });
  assert.equal(h.isOpen(), false);
  assert.deepEqual(h.notices, [{ type: 'success', text: 'Teaching times saved for Dr Hala Omar' }]);
  assert.equal(h.pill(1).dataset.state, 'limited');
  assert.equal(h.pill(1).querySelector('bdi').textContent, '20/35');
  assert.equal(h.d.activeElement, h.d.querySelector('[data-im-rules="1"]'), 'the new row\'s Times');
  assert.equal(h.gets.filter(url => url.startsWith('/ops/instructors/list/')).length, 1, 'no reload of the roster');
  assert.equal(h.w.InstructorTimeRules.hasDraft(1), false);
  // The next open reads the new revision and saves against it.
  await h.open(1);
  await h.quick('day_off');
  h.rules()[1].querySelectorAll('[data-day]')[2].click();
  await h.save();
  assert.equal(h.posts[1].body.expected_revision, 'rev1-1');
  assert.deepEqual(h.posts[1].body.rules[1], { kind: 'unavailable', days: ['TUE'], start: '00:00', end: '24:00', note: '' });
});

test('typed times are made canonical: on leaving the field and before the save, Arabic-Indic digits of both ranges included', async t => {
  const h = await page(t);
  await h.open(1);
  await h.quick('add');
  h.rules()[0].querySelectorAll('[data-day]')[0].click();
  h.type(0, 'start', arabicIndic('0900'));
  h.blur(0, 'start');
  assert.equal(h.field(0, 'start').value, '09:00');
  h.type(0, 'end', easternIndic('13:30'));
  assert.match(h.counts(), /Lecture periods open: 31 of 35/, 'the preview reads the typed digits at once');
  await h.save();
  assert.deepEqual(h.posts[0].body.rules[0], { kind: 'unavailable', days: ['SUN'], start: '09:00', end: '13:30', note: '' });
  const again = await page(t);
  await again.open(1);
  await again.quick('add');
  again.rules()[0].querySelectorAll('[data-day]')[1].click();
  again.type(0, 'start', '900');
  again.type(0, 'end', '1015');
  await again.save();
  assert.deepEqual([again.posts[0].body.rules[0].start, again.posts[0].body.rules[0].end], ['09:00', '10:15']);
});

test('a rule the server would refuse is said on its row before anything is sent', async t => {
  const h = await page(t);
  await h.open(1);
  await h.quick('no_mornings');
  await h.quick('add');
  h.type(1, 'start', '25:00');
  await h.save();
  assert.equal(h.posts.length, 0, 'nothing sent');
  assert.equal(h.isOpen(), true);
  assert.equal(h.rules()[0].querySelector('[aria-invalid]'), null, 'the first rule is fine');
  assert.equal(h.field(1, 'days').getAttribute('aria-invalid'), 'true');
  assert.equal(h.field(1, 'start').getAttribute('aria-invalid'), 'true');
  assert.equal(h.field(1, 'end').getAttribute('aria-invalid'), 'true');
  const message = h.rules()[1].querySelector('.ux-field-error');
  assert.equal(message.hidden, false);
  assert.equal(h.field(1, 'start').getAttribute('aria-describedby'), message.id);
  assert.match(message.textContent, /Choose at least one day\..*Type the start as a 24-hour time/);
  assert.equal(h.d.activeElement, h.rules()[1].querySelector('[data-day]'), 'the keyboard on the first invalid control');
  assert.equal(h.$('imRulesError').textContent, 'Nothing was saved: correct the marked rules.');
  assert.equal(h.$('imRulesError').getAttribute('role'), 'alert');
  // Editing the rule clears its marks.
  h.rules()[1].querySelectorAll('[data-day]')[0].click();
  await settle();
  assert.equal(h.rules()[1].querySelectorAll('[aria-invalid]').length, 0);
  assert.equal(message.hidden, true);
});

test('the server\'s validation errors land on the rows and fields they name, by index, field and code', async t => {
  const errors = [
    { index: 1, field: 'note', code: 'note_too_long' },
    { index: 1, field: 'start', code: 'start_invalid' },
    { index: 2, field: 'rule', code: 'duplicate' },
    { index: -1, field: 'rules', code: 'too_many' },
    { index: 9, field: 'kind', code: 'kind_invalid' },
  ];
  const h = await page(t, { answer: url => (url === '/ops/instructors/time-rules/set/'
    ? { httpError: 400, ok: false, error: { code: 'VALIDATION', message: 'The time rules are not valid', details: { errors } } } : undefined) });
  await h.open(1);
  await h.quick('no_mornings');
  await h.quick('no_afternoons');
  await h.quick('only_between');
  await h.save();
  assert.equal(h.posts.length, 1);
  assert.equal(h.isOpen(), true);
  assert.equal(h.rules()[0].querySelector('[aria-invalid]'), null);
  assert.equal(h.rules()[0].getAttribute('aria-invalid'), null);
  assert.equal(h.field(1, 'start').getAttribute('aria-invalid'), 'true');
  assert.equal(h.field(1, 'note').getAttribute('aria-invalid'), 'true');
  assert.equal(h.field(1, 'end').getAttribute('aria-invalid'), null);
  assert.equal(h.rules()[1].querySelector('.ux-field-error').textContent,
    'Type the start as a 24-hour time, such as 09:00. Keep the note to 200 characters.', 'in field order');
  assert.equal(h.rules()[2].getAttribute('aria-invalid'), 'true', 'a "rule" error marks the whole rule');
  assert.equal(h.rules()[2].querySelector('.ux-field-error').textContent, 'This rule repeats an earlier one.');
  assert.equal(h.d.activeElement, h.field(1, 'start'), 'the first invalid control, rule then field order');
  assert.equal(h.$('imRulesError').hidden, false);
  assert.equal(h.$('imRulesError').textContent,
    'Nothing was saved: correct the marked rules. An instructor can have at most 20 rules. Choose what the rule does.');
  assert.equal(h.notices.length, 0, 'no toast behind the dialog');
});

test('STALE keeps the draft and offers both answers: load their version, or save mine with their revision', async t => {
  const theirs = [rule('available_only', ['THU'], '00:00', '24:00', 'Colleague')];
  let stale = true;
  const h = await page(t, { answer: (url, opts) => (url === '/ops/instructors/time-rules/set/' && stale
    ? { httpError: 409, ok: false, error: { code: 'STALE', message: 'The time rules changed since they were loaded', details: { rules: theirs.map(r => ({ ...r, all_day: true })), revision: 'rev1-theirs' } } }
    : undefined) });
  await h.open(1);
  await h.quick('no_mornings');
  await h.save();
  assert.equal(h.isOpen(), true);
  assert.equal(h.$('imRulesStale').hidden, false);
  assert.equal(h.$('imRulesStaleText').textContent, 'A colleague changed these rules while you were editing.');
  assert.equal(h.$('imRulesStaleText').getAttribute('role'), 'alert');
  assert.equal(h.$('imRulesLoadTheirs').textContent, 'Load their version');
  assert.equal(h.$('imRulesSaveMine').textContent, 'Save mine instead');
  assert.equal(h.d.activeElement, h.$('imRulesLoadTheirs'));
  assert.equal(h.rules().length, 1, 'the draft is kept');
  assert.equal(h.field(0, 'end').value, '13:00');
  // Save mine: the same rules again, against their revision.
  stale = false;
  h.$('imRulesSaveMine').click();
  await settle();
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[1].body.expected_revision, 'rev1-theirs');
  assert.deepEqual(h.posts[1].body.rules, h.posts[0].body.rules);
  assert.equal(h.isOpen(), false);
  // Load theirs: the colleague's rules replace the draft.
  stale = true;
  await h.open(1);
  await h.quick('no_afternoons');
  await h.save();
  h.$('imRulesLoadTheirs').click();
  await settle();
  assert.equal(h.$('imRulesStale').hidden, true);
  assert.equal(h.rules().length, 1);
  assert.equal(h.field(0, 'kind').value, 'available_only');
  assert.deepEqual(h.pressed(0), [false, false, false, false, true]);
  assert.equal(h.field(0, 'all_day').checked, true);
  assert.equal(h.field(0, 'note').value, 'Colleague');
  assert.match(h.counts(), /7 of 35/);
  stale = false;
  await h.save();
  assert.equal(h.posts.at(-1).body.expected_revision, 'rev1-theirs');
});

test('closing never loses work: the draft comes back with a notice, and Discard reads the saved rules again', async t => {
  const h = await page(t);
  await h.open(2);
  await h.quick('day_off');
  h.rules()[1].querySelectorAll('[data-day]')[3].click();
  await settle();
  await h.escape();
  assert.equal(h.isOpen(), false);
  assert.equal(h.w.InstructorTimeRules.hasDraft(2), true);
  assert.equal(h.posts.length, 0);
  // Another instructor's dialog is not touched by it.
  await h.open(1);
  assert.equal(h.$('imRulesRestored').hidden, true);
  assert.equal(h.rules().length, 0);
  await h.escape();
  assert.equal(h.w.InstructorTimeRules.hasDraft(1), false);
  // Back to the first: restored.
  await h.open(2);
  assert.equal(h.$('imRulesRestored').hidden, false);
  assert.equal(h.$('imRulesRestoredText').textContent, 'Unsaved changes restored.');
  assert.equal(h.$('imRulesDiscard').textContent, 'Discard changes');
  assert.equal(h.rules().length, 2);
  assert.deepEqual(h.pressed(1), [false, false, false, true, false]);
  const reads = h.gets.filter(url => url.startsWith('/ops/instructors/time-rules/')).length;
  h.$('imRulesDiscard').click();
  await settle();
  assert.equal(h.gets.filter(url => url.startsWith('/ops/instructors/time-rules/')).length, reads + 1, 'the saved rules are read again');
  assert.equal(h.$('imRulesRestored').hidden, true);
  assert.equal(h.rules().length, 1);
  assert.equal(h.w.InstructorTimeRules.hasDraft(2), false);
  assert.equal(h.d.activeElement, h.field(0, 'kind'));
  // A restored draft still saves against the revision it was based on.
  await h.quick('no_afternoons');
  await h.escape();
  h.server.revision[2] = 'rev2-moved';
  await h.open(2);
  await h.save();
  assert.equal(h.posts.at(-1).body.expected_revision, 'rev2a');
});

test('a successful save clears the kept draft: the next open shows the saved rules and saves against the new revision', async t => {
  const h = await page(t);
  await h.open(1);
  await h.quick('no_mornings');
  await h.escape();
  assert.equal(h.w.InstructorTimeRules.hasDraft(1), true);
  await h.open(1);
  assert.equal(h.$('imRulesRestored').hidden, false, 'the draft is restored');
  await h.save();
  assert.equal(h.isOpen(), false);
  assert.equal(h.posts[0].body.expected_revision, 'rev1a');
  assert.equal(h.w.InstructorTimeRules.hasDraft(1), false);
  await h.open(1);
  assert.equal(h.$('imRulesRestored').hidden, true, 'nothing to restore after the save');
  assert.equal(h.rules().length, 1);
  await h.quick('no_afternoons');
  await h.save();
  assert.equal(h.posts[1].body.expected_revision, 'rev1-1', 'the saved revision, not the old draft\'s');
});

/* The page with its set-rules POST held until `release()` is called. */
async function heldSave(t, options) {
  const h = await page(t, options);
  const serve = h.w.fetch;
  h.release = null;
  h.w.fetch = async (url, opts = {}) => {
    if (url === '/ops/instructors/time-rules/set/') await new Promise(resolve => { h.release = resolve; });
    return serve(url, opts);
  };
  return h;
}

test('a dialog closed while its save is in flight: Times says so, and the next draft closed with Escape is still kept', async t => {
  const h = await heldSave(t);
  await h.open(1);
  await h.quick('no_mornings');
  await h.save();
  assert.equal(typeof h.release, 'function', 'the save is in flight');
  assert.equal(h.isOpen(), true);
  await h.escape();
  assert.equal(h.isOpen(), false);
  // A Times button while the save is pending is not silently ignored.
  await h.open(2);
  assert.equal(h.isOpen(), false);
  assert.deepEqual(h.notices, [{ type: 'info', text: 'Still saving the previous changes. Try again in a moment.' }]);
  // The save lands: saved, no draft left behind for it.
  h.release();
  await settle();
  assert.equal(h.server.saves, 1);
  assert.equal(h.notices.at(-1).type, 'success');
  assert.equal(h.w.InstructorTimeRules.hasDraft(1), false);
  assert.equal(h.pill(1).dataset.state, 'limited');
  // Another instructor: a draft closed with Escape is kept and comes back.
  await h.open(2);
  assert.equal(h.isOpen(), true);
  await h.quick('no_afternoons');
  await h.escape();
  assert.equal(h.isOpen(), false);
  assert.equal(h.w.InstructorTimeRules.hasDraft(2), true, 'closing never loses work');
  await h.open(2);
  assert.equal(h.$('imRulesRestored').hidden, false);
  assert.equal(h.rules().length, 2);
  assert.equal(h.field(1, 'start').value, '13:00');

  const ar = await heldSave(t, { arabic: true });
  await ar.open(1);
  await ar.quick('no_mornings');
  await ar.save();
  await ar.escape();
  await ar.open(2);
  assert.deepEqual(ar.notices, [{ type: 'info', text: 'لا يزال حفظ التعديلات السابقة جارياً. حاول مجدداً بعد لحظات.' }]);
  ar.release();
  await settle();
});

test('the roster pill says what the rules leave, in words as well as tone', async t => {
  const h = await page(t);
  const read = id => {
    const pill = h.pill(id);
    return [pill.dataset.state, pill.className, pill.querySelector('[aria-hidden]').textContent, pill.querySelector('.sr-only').textContent, pill.title];
  };
  assert.deepEqual(read(1), ['any', 'pill-status pill-muted im-times-pill', 'Any time', 'Any time: no teaching-time rules.', 'Any time: no teaching-time rules.']);
  assert.deepEqual(read(2).slice(0, 4), ['limited', 'pill-status pill-a im-times-pill', 'Limited 20/35',
    'Limited: 1 rule; 20 of 35 lecture periods open, and every course type still fits.']);
  assert.equal(h.pill(2).querySelector('bdi').getAttribute('dir'), 'ltr');
  assert.deepEqual(read(3).slice(0, 4), ['narrow', 'pill-status pill-r im-times-pill', 'Very limited', 'Very limited: 1 rule; some course types can no longer be scheduled.']);
  assert.deepEqual(read(4).slice(0, 4), ['none', 'pill-status pill-r im-times-pill', 'No open period', 'No open period: the rules (1 rule) leave no teaching period.']);
  assert.equal(h.pill(4).tagName, 'SPAN');
  // A row without a summary shows a dash, not a guess.
  assert.equal(String(h.w.InstructorTimeRules.pill(undefined)), '<span class="im-none" aria-label="—">—</span>');
  assert.equal(h.d.querySelector('#imInstructorTable tbody tr td[colspan]'), null);
  h.$('imInstructorSearch').value = 'nobody at all';
  h.$('imInstructorSearch').dispatchEvent(new h.w.Event('input'));
  assert.equal(h.d.querySelector('#imInstructorTable tbody td').getAttribute('colspan'), '8', 'the empty row spans every column');
  assert.equal(h.d.querySelectorAll('#imInstructorTable thead th').length, 8);
});

test('a failed save or read is said where the user is looking', async t => {
  let mode = 'network';
  const h = await page(t, { answer: url => {
    if (url !== '/ops/instructors/time-rules/set/') return undefined;
    if (mode === 'network') throw new Error('offline');
    if (mode === 'role') return { httpError: 403, error: 'Insufficient role: requires GENERAL_ACADEMIC_ADVISOR' };
    return { httpError: 404, ok: false, error: { code: 'NOT_FOUND', message: 'Instructor not found' } };
  } });
  await h.open(1);
  await h.quick('no_mornings');
  await h.save();
  assert.equal(h.$('imRulesError').textContent, 'Could not save the rules. Check your connection and try again.');
  assert.equal(h.isOpen(), true);
  mode = 'role';
  await h.save();
  assert.equal(h.$('imRulesError').textContent, 'You do not have permission to change these rules.');
  mode = 'missing';
  await h.save();
  assert.equal(h.$('imRulesError').textContent, 'This instructor no longer exists. Reload the page.');
  assert.equal(h.notices.length, 0);
  await h.escape();
  const broken = await page(t, { answer: url => (url.startsWith('/ops/instructors/time-rules/?') ? { httpError: 500, ok: false } : undefined) });
  await broken.open(1);
  assert.equal(broken.isOpen(), false);
  assert.deepEqual(broken.notices, [{ type: 'error', text: 'Could not load the teaching times' }]);
});

test('Arabic: every word of the dialog, the page\'s own day names, isolated times, and the pill', async t => {
  const h = await page(t, { arabic: true });
  const button = h.d.querySelector('[data-im-rules="1"]');
  assert.equal(button.textContent.trim(), 'الأوقات');
  assert.equal(button.getAttribute('aria-label'), 'أوقات التدريس — Dr Hala Omar');
  assert.equal(h.pill(1).querySelector('[aria-hidden]').textContent, 'أي وقت');
  assert.equal(h.pill(2).querySelector('[aria-hidden]').textContent, 'محدود 20/35');
  assert.equal(h.pill(2).querySelector('.sr-only').textContent, 'محدود: قاعدة واحدة، وتبقى 20 من أصل 35 فترة محاضرات متاحة، ويمكن جدولة كل أنواع المقررات.');
  assert.equal(h.pill(3).querySelector('[aria-hidden]').textContent, 'محدود جداً');
  assert.equal(h.pill(4).querySelector('[aria-hidden]').textContent, 'لا فترة متاحة');
  await h.open(1);
  const title = h.$('imRulesTitle');
  assert.equal(title.textContent, 'أوقات التدريس — د. هالة عمر', 'the Arabic name in Arabic');
  assert.equal(title.querySelector('bdi').textContent, 'د. هالة عمر');
  assert.equal(h.$('imRulesHelp').textContent, 'يُبقي منشئ الجدول محاضرات هذا العضو ضمن هذه الأوقات. أما النقل اليدوي فمسموح دائماً، ويُشار إليه بتنبيه.');
  assert.deepEqual([...h.d.querySelectorAll('[data-itr-quick]')].map(b => b.textContent.trim()),
    ['لا تدريس صباحاً', 'لا تدريس بعد الظهر', 'يوم إجازة', 'أيام محددة فقط', 'في فترة محددة فقط', 'إضافة قاعدة']);
  await h.quick('only_days');
  h.rules()[0].querySelectorAll('[data-day]')[4].click();
  await settle();
  assert.deepEqual([...h.rules()[0].querySelectorAll('[data-day]')].map(b => b.textContent), ['الأحد', 'الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس']);
  assert.deepEqual([...h.field(0, 'kind').options].map(o => o.textContent), ['لا يدرّس', 'يدرّس فقط']);
  assert.equal(h.rules()[0].querySelector('.im-rule-num').textContent, 'القاعدة 1');
  assert.equal(h.rules()[0].querySelector('[data-itr-remove]').getAttribute('aria-label'), 'حذف القاعدة 1');
  assert.equal(h.counts(), 'فترات المحاضرات المتاحة: 7 من 35 · فترات المعامل المتاحة: 5 من 25');
  assert.deepEqual([...h.$('imRulesCounts').querySelectorAll('bdi')].map(b => b.getAttribute('dir')), ['ltr', 'ltr', 'ltr', 'ltr']);
  assert.equal(h.warning(), 'لا يمكن بهذه القواعد جدولة بعض أنواع المقررات لهذا العضو.');
  assert.equal(h.$('imRulesNoDays').textContent, 'لا تدريس في: الأحد، الإثنين، الثلاثاء، الأربعاء');
  assert.deepEqual([...h.d.querySelectorAll('#imRulesShapes .im-shape-verdict')].map(v => v.textContent), ['ممكن', 'ممكن', 'غير ممكن', 'غير ممكن', 'غير ممكن']);
  assert.match(h.d.querySelector('#imRulesLectureGrid caption').textContent, /^فترات المحاضرات/);
  assert.equal(h.d.querySelector('#imRulesLectureGrid thead th bdi').textContent, '09:00–10:15');
  // Nothing in the dialog is left in English.
  const visible = [...h.dialog.querySelectorAll('*')].filter(e => !e.closest('[hidden]') && !e.closest('bdi'))
    .flatMap(e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim())).filter(Boolean);
  assert.deepEqual(visible.filter(text => /[A-Za-z]{3,}/.test(text)), []);
  // An error, in Arabic, on its row.
  h.type(0, 'note', 'x'.repeat(201));
  await h.save();
  assert.equal(h.rules()[0].querySelector('.ux-field-error').textContent, 'اجعل الملاحظة في حدود 200 حرف.');
  assert.equal(h.$('imRulesError').textContent, 'لم يُحفظ شيء: صحّح القواعد المشار إليها.');
  h.type(0, 'note', '');
  await h.save();
  assert.deepEqual(h.notices.map(n => n.text), ['حُفظت أوقات تدريس د. هالة عمر']);
});
