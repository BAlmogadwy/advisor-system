/* Stage 3, B4: a class in an online-only window, as the classic page says it.
 *
 * The server's publish check warns about a meeting of a course that is not
 * online for its board, drawn in an online-only window
 * (online_window_room_class: {placement_id, course_code, section, day,
 * slot_label, start, end}). It never blocks: Publish lists it with the
 * warnings that do not block publishing, in the page's language, and still
 * publishes. A default window ("Online N") is named in the page's language;
 * any other label as it was typed, isolated, and never as markup. The pair, the
 * window and the times are isolated, so Arabic never reverses them. Before
 * B4 the page knew no words for the code and showed the code itself.
 *
 * D3a owns the card's ring and the Issues row (D-7); MRI is not touched
 * (owner decision 2026-10-23).
 *
 * The real page script and template; controlled HTTP answers; no server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const wait = async condition => { for (let i = 0; i < 120; i++) { if (condition()) return; await settle(); } assert.fail('UI did not settle'); };
const FSI = String.fromCharCode(0x2066), PDI = String.fromCharCode(0x2069);
const iso = text => `${FSI}${text}${PDI}`;

const scenario = { id: 1, name: 'Online windows', status: 'draft', slot_config: [{ label: '09:00', start: '09:00', end: '10:15' }] };
const boards = [{ id: 2, label: 'Term 2', nominal_term: 2, primary_count: 1 }, { id: 3, label: 'Term 4', nominal_term: 4, primary_count: 1 }];
const at = (id, extra = {}) => ({ id, course_code: `AI${id}`, course_key: `AI${id}`, section: 'S1', term_section_id: id + 100, day: 'SUN',
  start_time: '09:00', end_time: '10:15', room: 'R1', meetings: [], available_capacity: 24, needs_room: true, room_state: 'assigned',
  is_online: false, shared_with_other_scenario: false, ...extra });
const quality = { ok: true, scenario_id: 1, instructor: { assessed: true, working_days: 1, idle_minutes: 0 }, soft_seats: { assessed: true, unresolved: 0 },
  rooms: { assessed: true, shortages: 0, details: [] }, assigned_student_clashes: { assessed: true, count: 0 } };
const quiet = { summary: { critical: 0, warning: 0 }, overlaps: [], instructor_clashes: [], room_clashes: [], cross_board_conflicts: [], hard_overlap_threshold: 20 };

// The server's item (core/services/timetable_workspace.py, check_publish_readiness).
const windowItem = (params = {}, board = boards[1]) => ({ code: 'online_window_room_class', board_id: board.id, board_label: board.label, count: null,
  params: { placement_id: 41, course_code: 'CS114', section: 'S1', day: 'SUN', slot_label: 'Online 2', start: '17:40', end: '19:20', ...params } });
const readinessWith = (...items) => ({ ready: true, blockers: [], blocker_items: [],
  warnings: items.map(i => `Board '${i.board_label}': ${i.params.course_code} ${i.params.section} meets in ${i.params.slot_label} (…), a window kept for online courses`),
  warning_items: items });

async function classic(t, { arabic = false, readiness = readinessWith() } = {}) {
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
  w.esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  w.debounce = fn => fn;
  w.notify = Object.fromEntries(['error', 'warning', 'success'].map(type => [type, text => notices.push({ type, text })]));
  // The confirm dialog: what it was asked to show, answered "yes".
  w.dlg = { confirm: async options => { dialogs.push(options); return true; } };
  w.Element.prototype.scrollIntoView = () => {};
  w.fetch = async (url, opts = {}) => {
    requests.push({ url, opts });
    let body;
    if (url.startsWith('/ops/tw/scenarios/?')) body = { scenarios: [scenario] };
    else if (url === '/ops/tw/scenarios/1/') body = { scenario, active_job: null };
    else if (url.includes('/quality/')) body = quality;
    else if (url.startsWith('/ops/tw/boards/?')) body = { boards };
    else if (url === '/ops/tw/boards/2/') body = { placements: [at(10)], slot_config: scenario.slot_config, online_course_codes: ['GS104'] };
    else if (url.includes('/conflicts/')) body = quiet;
    else if (url.includes('/readiness/')) body = { readiness };
    else if (url.includes('/budget/')) body = { budget: [] };
    else if (url.includes('/capacity/')) body = { courses: [], totals: {} };
    else if (url.includes('/unplaced/')) body = { unplaced: [] };
    else if (url === '/ops/tw/scenarios/1/publish/') body = { ok: true, scenario: { ...scenario, status: 'published' } };
    else assert.fail(`Unexpected request ${url}`);
    return { ok: true, status: 200, json: async () => body };
  };
  d.getElementById('twYear').value = '1448'; d.getElementById('twTerm').value = '1';
  for (const file of ['timetable-meeting-policy', 'timetable-slot-config', 'timetable-section-teacher', 'timetable-programme-mark',
    'timetable-room-choice', 'timetable-hotspots', 'ux-controls']) w.eval(read(`static/js/${file}.js`));
  w.eval(read('static/js/page-timetable-workspace.js'));
  await wait(() => d.getElementById('twScenario').options.length === 2);
  d.getElementById('twScenario').value = '1';
  await w.onScenarioChange();
  await wait(() => w.TW.loadedBoardId === 2);
  const publishes = () => requests.filter(r => r.url === '/ops/tw/scenarios/1/publish/' && r.opts.method === 'POST');
  // The confirm body as a document fragment, and its warning lines as text.
  const body = options => { const box = d.createElement('div'); box.innerHTML = String(options.body); return box; };
  const lines = options => [...body(options).querySelectorAll('.tw-readiness-list[data-tone="warning"] li')]
    .map(li => li.querySelector('span:not(.ux-tone-icon)').textContent);
  return { w, d, requests, notices, dialogs, publishes, body, lines };
}

const WORDS = {
  en: `${iso('CS114 S1')} meets in Online 2 (Sun ${iso('17:40–19:20')}), a window kept for online courses.`,
  ar: `تجتمع ${iso('CS114 S1')} في «عن بُعد 2» (الأحد ${iso('17:40–19:20')})، وهي فترة مخصصة للمقررات عن بُعد.`,
};

for (const lang of ['en', 'ar']) test(`(${lang}) Publish lists a class in an online window as a warning in words, and still publishes`, async t => {
  const h = await classic(t, { arabic: lang === 'ar', readiness: readinessWith(windowItem()) });
  await h.w.publishScenario();
  assert.equal(h.dialogs.length, 1, 'one dialog: the publish confirmation, not a list of blockers');
  const [confirm] = h.dialogs;
  assert.equal(confirm.kind, 'warning', 'the confirmation (the blockers list is a danger dialog)');
  assert.match(h.body(confirm).textContent, lang === 'ar' ? /تحذيرات لا تمنع النشر/ : /Warnings that do not block publishing/);
  assert.deepEqual(h.lines(confirm), [WORDS[lang]]);
  assert.doesNotMatch(h.body(confirm).textContent, /online_window_room_class/);
  // The item belongs to another board: the line offers to open it.
  assert.deepEqual([...h.body(confirm).querySelectorAll('.tw-readiness-go')].map(b => b.dataset.board), ['3']);
  // Never refused: the confirmed publish is sent, and the scenario is published.
  assert.equal(h.publishes().length, 1);
  await wait(() => h.w.TW.scenario?.status === 'published');
});

for (const lang of ['en', 'ar']) test(`(${lang}) a window with a label of its own is named as typed, isolated, never as markup`, async t => {
  const label = 'Evening <b>late</b>';
  const h = await classic(t, { arabic: lang === 'ar', readiness: readinessWith(windowItem({ slot_label: label, day: 'thu', start: '19:30', end: '21:10' }, boards[0])) });
  await h.w.publishScenario();
  const [confirm] = h.dialogs;
  const expected = lang === 'ar'
    ? `تجتمع ${iso('CS114 S1')} في «${iso(label)}» (الخميس ${iso('19:30–21:10')})، وهي فترة مخصصة للمقررات عن بُعد.`
    : `${iso('CS114 S1')} meets in ${iso(label)} (Thu ${iso('19:30–21:10')}), a window kept for online courses.`;
  assert.deepEqual(h.lines(confirm), [expected]);
  assert.equal(h.body(confirm).querySelector('b'), null, 'the label is text');
  // On the open board: no "Open board" button.
  assert.equal(h.body(confirm).querySelector('.tw-readiness-go'), null);
  assert.equal(h.publishes().length, 1);
});

test('every window line is its own warning, after the others, in the server’s order', async t => {
  const other = { code: 'board_warnings', board_id: 2, board_label: 'Term 2', count: 2, params: {} };
  const second = windowItem({ placement_id: 42, course_code: 'CS212', section: 'S2', day: 'MON', slot_label: 'Online 3', start: '19:30', end: '21:10' });
  const readiness = readinessWith(windowItem(), second);
  readiness.warning_items = [other, ...readiness.warning_items];
  readiness.warnings = ["Board 'Term 2': 2 warnings", ...readiness.warnings];
  const h = await classic(t, { readiness });
  await h.w.publishScenario();
  assert.deepEqual(h.lines(h.dialogs[0]), [
    'Board “Term 2”: 2 warnings.',
    WORDS.en,
    `${iso('CS212 S2')} meets in Online 3 (Mon ${iso('19:30–21:10')}), a window kept for online courses.`,
  ]);
  assert.equal(h.publishes().length, 1);
});

test('the default window names: Online N in the page’s language, nothing else changed', async t => {
  for (const [arabic, expected] of [[false, ['Online 1', 'Online 12', iso('online 1'), iso('Online'), iso('Lab 5'), '—']],
    [true, ['عن بُعد 1', 'عن بُعد 12', iso('online 1'), iso('Online'), iso('Lab 5'), '—']]]) {
    const h = await classic(t, { arabic });
    assert.deepEqual(['Online 1', ' Online 12 ', 'online 1', 'Online', 'Lab 5', ''].map(h.w.onlineWindowName), expected);
  }
});
