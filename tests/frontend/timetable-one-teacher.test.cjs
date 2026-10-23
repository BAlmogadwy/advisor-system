/* One teacher per section (J1): the drawer shows the section's own teacher —
 * a name, "to be decided" with its reason, or None — and hides the link editor
 * that never had data; the run outcome and the quality panel say how many
 * sections have a teacher, in English and Arabic. Without the rule (no
 * `teacher` key, no coverage) every surface renders as before. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const split = read('static/js/page-timetable-workspace-split.js');
const workspace = read('static/js/page-timetable-workspace.js');
const slice = (source, begin, end) => source.slice(source.indexOf(begin), source.indexOf(end, source.indexOf(begin)));
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };

async function drawer(t, teacher, { arabic = false, meetingName = '', route = null } = {}) {
  const html = read('core/templates/core/timetable_workspace_split.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://split.test/', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], errors = [];
  const placement = {
    id: 10, term_section_id: 110, course_code: 'AI201', course_key: 'AI201', course_name: 'Course', section: 'S3',
    day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'R1', is_locked: false,
    meetings: [{ day: 'SUN', start_time: '09:00', end_time: '10:15', room: 'R1', instructor: meetingName }],
    ...(teacher === undefined ? {} : { teacher }),
  };
  const board = { id: 2, label: 'Board', nominal_term: 1, primary_count: 1, placement_count: 1 };
  const boardData = () => ({ board, placements: [placement], slot_config: [{ start: '09:00', end: '10:15' }] });
  w.addEventListener('error', event => errors.push(event.error));
  t.after(() => { w.close(); assert.deepEqual(errors, []); });
  Object.assign(w, { LANGUAGE_CODE: arabic ? 'ar' : 'en', djCsrfToken: 'audit', INITIAL_SCENARIO: '', INITIAL_BOARD: '', DEFAULT_YEAR: '1448', DEFAULT_TERM: '1' });
  w.esc = esc;
  w.notify = { success: () => {}, warning: () => {}, error: () => {} };
  w.console.error = () => {};
  w.matchMedia = () => ({ matches: false });
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.fetch = async url => {
    requests.push(url);
    // A test's own answer: {status, body}; no body is a page that is not JSON.
    const own = route && route(url);
    if (own) {
      return {
        ok: own.status < 400, status: own.status,
        json: async () => { if (own.body === undefined) throw new SyntaxError('not JSON'); return own.body; },
      };
    }
    let body;
    if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [] };
    else if (url === '/ops/tw/boards/2/') body = boardData();
    else if (url.startsWith('/ops/tw/boards/?')) body = { boards: [board] };
    else if (url.endsWith('/conflicts/')) body = { overlaps: [], instructor_clashes: [], room_clashes: [] };
    else if (url.endsWith('/plan-lens/')) body = { plan_lens: { plans: [], courses: {}, sections: {} } };
    else body = { ok: true, candidates: [], instructors: [], summary: {} };
    return { ok: true, status: 200, json: async () => body };
  };
  w.eval(read('static/js/timetable-meeting-policy.js'));
  // The page loads the shared slot-column module before its own script.
  w.eval(read('static/js/timetable-slot-config.js'));
  w.eval(read('static/js/timetable-section-teacher.js'));
  w.eval(read('static/js/timetable-programme-mark.js'));
  w.eval(split + '\n;globalThis.__splitAudit = { S };');
  await settle();
  const { S: state } = w.__splitAudit;
  state.scenarioId = 1; state.scenarioMeta = { id: 1, status: 'draft' }; state.boards = [board];
  state.cols = 1; state.rows = 1;
  state.panes[0].boardId = 2; state.panes[0].boardData = boardData();
  w.renderPane(0);
  requests.length = 0;
  w.openDrawer(0, 10);
  await settle();
  return { w, d, state, requests, chips: d.getElementById('twsDrawerInstrChips') };
}

/* The right panel's Selection tab for the drawer's placement. */
async function selection(h) {
  h.state.selectedPlacementId = 10;
  h.w.openSelectionInspector();
  await settle();
  return h.d.getElementById('twsRpanelBody');
}

test('the drawer names the section\'s own teacher and hides the dead link editor', async t => {
  const h = await drawer(t, { id: 7, name: 'Dr B', status: 'engine', source: 'engine', reason: null }, { meetingName: 'stale' });
  assert.equal(h.chips.textContent.trim(), 'Dr B');
  assert.equal(h.d.querySelector('.tws-drawer-instr-edit'), null);
  assert.equal(h.d.getElementById('twsDrawerInstrInput'), null);
  assert.ok(!h.requests.some(url => url.startsWith('/ops/tw/instructors/')), h.requests.join(' '));
});

for (const arabic of [false, true]) {
  test(`the drawer says a section's teacher is to be decided, and why (${arabic ? 'ar' : 'en'})`, async t => {
    const h = await drawer(t, { id: null, name: null, status: 'tbd', source: null, reason: 'd10' }, { arabic, meetingName: 'stale' });
    const tbd = h.chips.querySelector('.tws-instr-tbd'), hint = h.chips.querySelector('.tws-instr-hint');
    assert.equal(tbd.textContent, arabic ? 'يُحدَّد المدرس لاحقاً' : 'Teacher to be decided');
    assert.match(hint.textContent, arabic ? /أقصى عدد مسموح به من شعب هذا المقرر/ : /most sections of this course/);
    assert.doesNotMatch(h.chips.textContent, /stale/);
    assert.equal(h.d.querySelector('.tws-drawer-instr-edit'), null);
  });
  test(`the drawer says None for a section nobody may teach (${arabic ? 'ar' : 'en'})`, async t => {
    const h = await drawer(t, { id: null, name: null, status: 'no_links', source: null, reason: null }, { arabic });
    assert.equal(h.chips.textContent.trim(), arabic ? 'لا يوجد' : 'None');
  });
  // B2 (stage 3): an English section with no one complete weekly pattern.
  test(`the drawer shows an unknown teacher as a dash and why, never the meeting's name (${arabic ? 'ar' : 'en'})`, async t => {
    const h = await drawer(t, { id: null, name: null, status: 'unknown', source: null, reason: 'incomplete_english_pattern' }, { arabic, meetingName: 'Registrar Name' });
    assert.equal(h.chips.querySelector('.tws-instr-none').textContent, '—');
    assert.equal(h.chips.querySelector('.tws-instr-hint').textContent, arabic
      ? 'لم يُعرض المدرّس: ليس لهذه الشعبة الإنجليزية نمط أسبوعي كامل واحد على كل اللوحات. احذفها وضعها من جديد.'
      : 'Teacher not shown: this English section does not have one complete weekly pattern on every board. Remove it and place it again.');
    assert.doesNotMatch(h.chips.textContent, /Registrar Name/);
    assert.equal(h.d.querySelector('.tws-drawer-instr-edit'), null);
  });
}

for (const arabic of [false, true]) {
  // Review of B2: split's Selection tab and the drawer's meeting rows printed
  // the meeting row's name beside the drawer's "—".
  test(`split's Selection tab shows an unknown teacher as a dash and why, and protects no one (${arabic ? 'ar' : 'en'})`, async t => {
    const h = await drawer(t, { id: null, name: null, status: 'unknown', source: null, reason: 'incomplete_english_pattern' }, { arabic, meetingName: 'Registrar Name' });
    const body = await selection(h);
    const field = body.querySelector('#twsSelInstructor');
    assert.equal(field.querySelector('.tws-instr-none').textContent, '—');
    assert.match(field.querySelector('.tws-instr-hint').textContent, arabic ? /^لم يُعرض المدرّس:/ : /^Teacher not shown: this English section/);
    assert.doesNotMatch(body.textContent, /Registrar Name/);
    assert.equal(body.querySelector('[data-protect="instructor"]').disabled, true);
  });
}

test('split\'s Selection tab names the rule\'s teacher and protects that teacher, never a stale meeting name', async t => {
  const h = await drawer(t, { id: 7, name: 'Dr B', status: 'engine', source: 'engine', reason: null }, { meetingName: 'stale' });
  const body = await selection(h);
  assert.equal(body.querySelector('#twsSelInstructor').textContent.trim(), 'Dr B');
  assert.doesNotMatch(body.textContent, /stale/);
  const protect = body.querySelector('[data-protect="instructor"]');
  assert.equal(protect.disabled, false);
  protect.click();
  await settle();
  assert.deepEqual([...h.state.protections.instructors], ['DR B']);
  assert.match(h.d.getElementById('twsRpanelBody').querySelector('.tws-protect-note').textContent, /protected instructor Dr B/);
});

test('split\'s Selection tab without the rule keeps the meeting\'s name', async t => {
  const h = await drawer(t, undefined, { meetingName: 'Registrar Name' });
  const body = await selection(h);
  assert.equal(body.querySelector('#twsSelInstructor').textContent, 'Registrar Name');
  assert.equal(body.querySelector('[data-protect="instructor"]').disabled, false);
});

test('the drawer\'s meeting rows name no one with the rule on, as classic; without it, the meeting\'s name', async t => {
  for (const teacher of [{ id: null, name: null, status: 'unknown', source: null, reason: 'incomplete_english_pattern' }, { id: 7, name: 'Dr B', status: 'engine', source: 'engine', reason: null }]) {
    const h = await drawer(t, teacher, { meetingName: 'Registrar Name' });
    const rows = [...h.d.querySelectorAll('#twsDrawerBody .meeting-row')].map(row => row.textContent);
    assert.equal(rows.length, 1);
    assert.doesNotMatch(rows[0], /Registrar Name/);
    assert.match(rows[0], /R1$/);
  }
  const off = await drawer(t, undefined, { meetingName: 'Registrar Name' });
  assert.match(off.d.querySelector('#twsDrawerBody .meeting-row').textContent, /R1 · Registrar Name$/);
});

for (const arabic of [false, true]) {
  // Review of B2: the 409 in words never reached the page; the Students panel
  // said "Calculating..." for ever.
  test(`the Students panel shows the server's words when it refuses, and asks again next time (${arabic ? 'ar' : 'en'})`, async t => {
    const words = arabic
      ? 'تحتاج تعارضات الطلاب الفعلية إلى نمط أسبوعي كامل واحد لكل شعبة إنجليزية، والشعبة ENG102 S1 ليست كذلك. احذفها وضعها من جديد.'
      : 'Student blockers need every English section on one complete weekly pattern; ENG102 S1 is not. Remove it and place it again.';
    const route = url => (url.endsWith('/student-blockers/')
      ? { status: 409, body: { ok: false, error: { code: 'INCOMPLETE_ENGLISH_PATTERN', message: `${words}<b>` } } }
      : null);
    const h = await drawer(t, undefined, { arabic, route });
    h.w.toggleRpanel(true);
    h.w.setRpanelTab('students');
    await settle();
    const body = h.d.getElementById('twsRpanelBody');
    assert.equal(body.querySelector('[role="status"]').textContent, `ⓘ${words}<b>`, 'escaped, never markup');
    assert.equal(body.querySelector('b'), null);
    assert.doesNotMatch(body.textContent, /Calculating|جاري حساب/);
    assert.equal(h.d.getElementById('twsRpCountStudents').textContent, '—');
    h.w.setRpanelTab('issues');
    h.w.setRpanelTab('students');
    await settle();
    assert.equal(h.requests.filter(url => url.endsWith('/student-blockers/')).length, 2, 'a refusal is not cached');
  });
  test(`the Students panel says so in its own words when the answer is not JSON (${arabic ? 'ar' : 'en'})`, async t => {
    const h = await drawer(t, undefined, { arabic, route: url => (url.endsWith('/student-blockers/') ? { status: 500 } : null) });
    h.w.toggleRpanel(true);
    h.w.setRpanelTab('students');
    await settle();
    assert.equal(h.d.getElementById('twsRpanelBody').textContent, arabic
      ? 'ⓘتعذّر حساب تعارضات الطلاب الفعلية. حاول مرة أخرى.'
      : 'ⓘStudent blockers could not be calculated. Try again.');
  });
}

test('the shared rule reads an unknown teacher as its own state, in the page’s language', () => {
  for (const [lang, hint] of [['en', /^Teacher not shown: this English section/], ['ar', /^لم يُعرض المدرّس:/]]) {
    const dom = new JSDOM('<!doctype html><html><body></body></html>', { runScripts: 'outside-only' });
    dom.window.LANGUAGE_CODE = lang;
    dom.window.eval(read('static/js/timetable-section-teacher.js'));
    const rule = dom.window.TimetableSectionTeacher;
    const seen = rule.teacherOf({ teacher: { status: 'unknown', reason: 'incomplete_english_pattern', name: 'Stale' }, meetings: [{ instructor: 'Registrar Name' }] });
    assert.equal(seen.state, 'unknown');
    assert.equal(seen.name, '');
    assert.match(seen.hint, hint);
    assert.equal(rule.words.unknown, '—');
    dom.window.close();
  }
});

test('a section the map leaves out keeps its meeting name', async t => {
  for (const teacher of [null, { id: null, name: null, status: 'no_links', source: null, reason: null }]) {
    const h = await drawer(t, teacher, { meetingName: 'Registrar Name' });
    assert.equal(h.chips.textContent.trim(), 'Registrar Name');
  }
});

test('without the rule the drawer keeps its link editor', async t => {
  const h = await drawer(t, undefined, { meetingName: 'stale' });
  assert.equal(h.chips.textContent.trim(), 'None');
  assert.ok(h.d.querySelector('.tws-drawer-instr-edit'));
  assert.ok(h.requests.some(url => url.startsWith('/ops/tw/instructors/')));
});

const COVERAGE = { assigned: 40, tbd: 3, tbd_d10_floor: 2, no_links: 5, sections_total: 48 };

function outcome(source, arabic) {
  const context = vm.createContext({ IS_AR: arabic, esc, Number, $: () => null });
  vm.runInContext(slice(source, '/* Timetable outcome helpers', '/* End timetable outcome helpers. */'), context);
  return context;
}

for (const [name, source] of [['split', split], ['workspace', workspace]]) {
  for (const arabic of [false, true]) {
    test(`${name} run outcome reports teacher coverage beside days and idle (${arabic ? 'ar' : 'en'})`, () => {
      const c = outcome(source, arabic);
      const current = {
        instructor_schedule_metric: {
          before: { working_days: 3, idle_minutes: 105 }, after: { working_days: 2, idle_minutes: 15 },
          coverage: { before: { ...COVERAGE, assigned: 39, tbd: 4 }, after: COVERAGE },
        },
      };
      const html = c.renderInstructorOutcome(current);
      assert.match(html, arabic
        ? /شعب لها مدرس: 40 \/ 43 · لم يُحدَّد بعد: 3 \(D10: 2\)/
        : /Sections with a teacher: 40 \/ 43 · to be decided: 3 \(D10: 2\)/);
      const full = c.renderInstructorOutcome({ quality_score: { instructor_schedule: { working_days: 2, idle_minutes: 15 } }, section_teachers: { enabled: true, coverage: COVERAGE } });
      assert.match(full, arabic ? /شعب لها مدرس: 40 \/ 43/ : /Sections with a teacher: 40 \/ 43/);
    });
  }
  test(`${name} run outcome without the rule has no coverage row`, () => {
    const c = outcome(source, false);
    const html = c.renderInstructorOutcome({ instructor_schedule_metric: { before: { working_days: 3, idle_minutes: 105 }, after: { working_days: 2, idle_minutes: 15 } } });
    assert.match(html, /Total working days/);
    assert.doesNotMatch(html, /Sections with a teacher/);
    assert.equal(c.renderInstructorOutcome({}), '');
  });
}

function quality(arabic, coverage) {
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, { id, textContent: '', title: '', hidden: id === 'twQualityTeacherCoverageRow', classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)), contains: name => classes.has(name) } });
    }
    return elements.get(id);
  };
  const context = vm.createContext({
    IS_AR: arabic, esc, Number, Boolean, $,
    // The strip also redraws the inspector's Issues count; that has its own
    // tests in timetable-workspace.test.cjs.
    renderBoardIssueViews: () => {},
    TW: { scenarioId: 1, boardId: 2, placements: [], conflicts: { summary: { critical: 0 } }, quality: { instructor: { assessed: true, working_days: 2, idle_minutes: 15, ...(coverage ? { coverage } : {}) } } },
  });
  vm.runInContext(slice(workspace, '/* Timetable outcome helpers', '/* End timetable outcome helpers. */'), context);
  vm.runInContext(slice(workspace, 'function renderWorkspaceQuality()', 'function activeBoardRoomShortages('), context);
  context.renderWorkspaceQuality();
  return { row: $('twQualityTeacherCoverageRow'), cell: $('twQualityTeacherCoverage') };
}

for (const arabic of [false, true]) {
  test(`the quality grid shows teacher coverage (${arabic ? 'ar' : 'en'})`, () => {
    const q = quality(arabic, COVERAGE);
    assert.equal(q.row.hidden, false);
    assert.equal(q.cell.textContent, '40 / 43');
    assert.equal(q.cell.classList.contains('danger'), true);  // one TBD beyond the D10 floor
    assert.match(q.cell.title, arabic ? /لم يُحدَّد بعد: 3/ : /to be decided: 3 \(D10: 2\)/);
    assert.equal(quality(arabic, { ...COVERAGE, tbd: 2 }).cell.classList.contains('danger'), false);
  });
}

test('the quality grid hides the coverage row without the rule', () => {
  const q = quality(false, null);
  assert.equal(q.row.hidden, true);
  assert.equal(q.cell.textContent, '');
});

test('the quality strip template carries the coverage stat, hidden until the rule reports it', () => {
  const html = read('core/templates/core/timetable_workspace.html');
  assert.match(html, /<div class="ux-stat" id="twQualityTeacherCoverageRow" hidden>/);
  assert.match(html, /<dd id="twQualityTeacherCoverage">/);
});

test('the coverage stat is the first of the instructors group, before its days and idle time', () => {
  // The strip wraps its stats rather than counting grid columns, so the
  // contract is where the cell sits: in the instructors' own labelled list.
  const html = read('core/templates/core/timetable_workspace.html')
    .replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(html);
  const doc = dom.window.document;
  const row = doc.getElementById('twQualityTeacherCoverageRow');
  const list = row.closest('dl');
  assert.equal(list.getAttribute('aria-labelledby'), 'twStatInstructorScope');
  assert.deepEqual([...list.children].map(cell => cell.querySelector('dd').id),
    ['twQualityTeacherCoverage', 'twQualityInstructorDays', 'twQualityInstructorIdle']);
  assert.equal(row.hidden, true);
  dom.window.close();
});
