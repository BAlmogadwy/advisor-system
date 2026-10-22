/* Change room's shared rules (static/js/timetable-room-choice.js): which rooms
 * a meeting may take by hand, and the words each room is shown with. One case
 * table (tests/fixtures/room_choice_cases.json, which the split-view parity
 * test in timetable-manual-clash-ruling.test.cjs also reads) drives fits,
 * occupied, the verdict in English and Arabic, the misfit reasons and, where a
 * case gives them, the facts. The module runs alone, with only what base.html
 * loads on every page. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const table = JSON.parse(read('tests/fixtures/room_choice_cases.json'));
// The module's answers are made in the page's realm; compare their content.
const plain = value => JSON.parse(JSON.stringify(value));

function load(t, { language = 'en', lang = 'en' } = {}) {
  const dom = new JSDOM(`<!doctype html><html lang="${lang}"><body></body></html>`, { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window;
  // base.html loads shared-utils (pluralAr) on every page.
  w.eval(read('static/js/shared-utils.js'));
  if (language !== null) w.LANGUAGE_CODE = language;
  w.eval(read('static/js/timetable-room-choice.js'));
  return w;
}

test('each room in the case table fits, is in use and reads as the table says, in English and Arabic', t => {
  const w = load(t);
  const choice = w.TimetableRoomChoice;
  for (const c of table.cases) {
    const target = { ...table.target, ...(c.target || {}) };
    const placement = 'placement' in c ? c.placement : table.placement;
    assert.equal(choice.codeKey(c.room?.room_code), c.code_key, c.name);
    assert.equal(choice.fits(c.room), c.fits, c.name);
    assert.equal(choice.occupied(c.room), c.occupied, c.name);
    for (const language of ['en', 'ar']) {
      w.LANGUAGE_CODE = language;
      assert.deepEqual(plain(choice.verdict(c.room, placement)), { group: c.verdict.group, tone: c.verdict.tone, words: c.verdict[language] }, `${c.name} (${language})`);
      assert.deepEqual(plain(choice.misfitReasons(c.room, target, language === 'ar')), c.reasons[language], `${c.name} (${language})`);
      if (c.facts) assert.equal(choice.factsHtml(c.room, target, language === 'ar'), c.facts[language], `${c.name} (${language})`);
    }
  }
  // Every group and tone is in the table, and only a room that fits is offered.
  // No verdict is context: an info row never rings a card (§7.1), and every
  // room the dialog offers that Issues will list does ring one.
  const verdicts = table.cases.map(c => `${c.verdict.group}/${c.verdict.tone}`);
  for (const kind of ['free/ok', 'outside/warning', 'inUse/critical', 'inUse/warning', 'misfit/']) assert.ok(verdicts.includes(kind), kind);
  assert.deepEqual([...new Set(verdicts)].sort(), ['free/ok', 'inUse/critical', 'inUse/warning', 'misfit/', 'outside/warning']);
  assert.ok(table.cases.every(c => (c.verdict.group === 'misfit') === !c.fits));
  // A room outside the programme rooms, each way it can be offered: free, held
  // here, held elsewhere; and one that does not fit, which stays a misfit.
  const outside = c => c.room?.department_fit === false;
  for (const kind of ['outside/warning', 'inUse/critical', 'inUse/warning', 'misfit/']) {
    assert.ok(table.cases.some(c => outside(c) && `${c.verdict.group}/${c.verdict.tone}` === kind), `outside the programme rooms: ${kind}`);
  }
  // For each fit flag, a held room that misses on that flag alone: being held
  // never makes a misfit choosable, whichever check says so.
  const flags = ['fits_type', 'fits_gender', 'fits_capacity'];
  for (const flag of flags) {
    assert.ok(table.cases.some(c => c.occupied && flags.every(f => (c.room[f] === false) === (f === flag))), `a held room that misses only ${flag}`);
  }
  // And the cases the normalisations need are still there.
  const shapes = {
    'a board id as text': c => typeof c.placement?.board_id === 'string' || c.room?.occupied_by?.some(e => typeof e.board_id === 'string'),
    'no board on either side': c => c.occupied && c.verdict.tone === 'warning' && 'placement' in c && c.placement?.board_id == null,
    'a need in another case': c => c.target?.required_type && c.target.required_type !== c.target.required_type.trim().toLowerCase()
      && c.target.required_gender && c.target.required_gender !== c.target.required_gender.trim().toUpperCase() && c.reasons.en.length === 2,
    'a room type and gender in another case': c => c.facts && String(c.room.room_type ?? '') !== String(c.room.room_type ?? '').trim().toLowerCase()
      && String(c.room.section ?? '') !== String(c.room.section ?? '').trim().toUpperCase(),
    'a room without seats': c => c.facts && c.room.capacity === null,
    'a meeting without a seat need': c => c.facts && c.target?.required_capacity === null,
    'a $ in the room list': c => c.facts && c.reasons.en.length > 0
      && [c.target?.required_type, c.room.building, c.room.wing, c.room.floor].every(value => /\$[&`'$]/.test(String(value ?? ''))),
    'a padded place, a blank one and floor 0': c => c.facts && /^\s.*\S\s*$/.test(String(c.room.building ?? ''))
      && typeof c.room.wing === 'string' && c.room.wing.length > 0 && !c.room.wing.trim() && c.room.floor === 0,
    'a misfit whose needs are not named': c => !c.fits && c.room.fits_type === false && c.room.fits_gender === false
      && c.target && c.target.required_type === null && c.target.required_gender === null && c.reasons.en.length === 0,
  };
  for (const [shape, holds] of Object.entries(shapes)) assert.ok(table.cases.some(holds), shape);
});

test('writing to the words handed out changes no later verdict, reason or fact', t => {
  const w = load(t);
  const choice = w.TimetableRoomChoice;
  const held = table.cases.find(c => c.verdict.tone === 'critical').room;
  const elsewhere = table.cases.find(c => c.verdict.tone === 'warning' && c.verdict.group === 'inUse').room;
  const heldOutside = table.cases.find(c => c.verdict.group === 'inUse' && c.room.department_fit === false).room;
  const both = table.cases.find(c => c.reasons.en.length === 2 && !c.target);
  const studio = table.cases.find(c => c.target?.required_type === 'studio');
  const room = { room_type: 'lab', capacity: 40, section: 'F', building: 'B', wing: 'W', floor: 1 };
  const shown = () => ['en', 'ar'].flatMap(language => {
    w.LANGUAGE_CODE = language;
    const ar = language === 'ar';
    return [
      choice.verdict(held, table.placement).words, choice.verdict(elsewhere, table.placement).words,
      choice.verdict(table.cases[0].room, table.placement).words, choice.verdict(table.cases[1].room, table.placement).words,
      choice.verdict(heldOutside, table.placement).words,
      ...choice.misfitReasons(both.room, table.target, ar), ...choice.misfitReasons(studio.room, { ...table.target, ...studio.target }, ar),
      choice.factsHtml(room, { required_capacity: 30 }, ar), choice.factsHtml({ ...room, room_type: 'lecture', section: 'M' }, { required_capacity: 50 }, ar),
    ];
  });
  const before = shown();
  // Every word and count, at every depth, in both languages, is written over.
  const scribble = words => {
    for (const [key, value] of Object.entries(words)) {
      if (value && typeof value === 'object') scribble(value);
      else Reflect.set(words, key, typeof value === 'function' ? () => 'changed' : 'changed {c}');
    }
  };
  scribble(choice.words(false));
  scribble(choice.words(true));
  assert.deepEqual(shown(), before);
  assert.equal(choice.words(false).toneLead.critical, 'Blocks publishing');
});

test("a misfit's reasons follow the page's language when none is given", t => {
  const w = load(t, { language: 'ar' });
  const lab = table.cases.find(c => c.room?.room_code === 'R3');
  assert.deepEqual(plain(w.TimetableRoomChoice.misfitReasons(lab.room, table.target)), lab.reasons.ar);
  assert.deepEqual(plain(w.TimetableRoomChoice.misfitReasons(lab.room, table.target, false)), lab.reasons.en);
});

test('the language is read when the words are asked for, not when the module loads', t => {
  // No LANGUAGE_CODE yet: the document's own language decides.
  const w = load(t, { language: null, lang: 'ar' });
  const free = table.cases[0].room;
  assert.equal(w.TimetableRoomChoice.verdict(free, table.placement).words, 'متاحة ومناسبة');
  w.LANGUAGE_CODE = 'en';
  assert.equal(w.TimetableRoomChoice.verdict(free, table.placement).words, 'Free and fits');
  assert.equal(w.TimetableRoomChoice.words().free, 'Free and fits');
  assert.equal(w.TimetableRoomChoice.words(true).free, 'متاحة ومناسبة');
});

test("a room in use, or outside the programme rooms, is worded and toned as the page's Issues word what it saves", t => {
  const w = load(t);
  const workspace = read('static/js/page-timetable-workspace.js');
  const begin = workspace.indexOf('const ISSUE_KINDS = {');
  const end = workspace.indexOf('const issueKindLabel', begin);
  assert.ok(begin >= 0 && end > begin, "the page's ISSUE_KINDS and TONE_LEAD are where this test reads them");
  const inPool = c => c.room?.department_fit !== false;
  const here = table.cases.find(c => c.verdict.tone === 'critical' && inPool(c)).room;
  const elsewhere = table.cases.find(c => c.verdict.tone === 'warning' && c.verdict.group === 'inUse' && inPool(c)).room;
  const outside = table.cases.find(c => c.verdict.group === 'outside').room;
  for (const arabic of [false, true]) {
    const page = vm.runInNewContext(`${workspace.slice(begin, end)}\n;({ ISSUE_KINDS, TONE_LEAD })`, { IS_AR: arabic });
    const label = kind => page.ISSUE_KINDS[kind][arabic ? 'ar' : 'en'];
    const own = w.TimetableRoomChoice.words(arabic);
    assert.deepEqual(plain(own.toneLead), plain(page.TONE_LEAD));
    assert.deepEqual(Object.keys(own.kinds), ['room', 'roomElsewhere', 'roomUnsuitable']);
    for (const kind of Object.keys(own.kinds)) assert.equal(own.kinds[kind], label(kind), kind);
    w.LANGUAGE_CODE = arabic ? 'ar' : 'en';
    const onThisBoard = w.TimetableRoomChoice.verdict(here, table.placement);
    const onAnother = w.TimetableRoomChoice.verdict(elsewhere, table.placement);
    const unsuitable = w.TimetableRoomChoice.verdict(outside, table.placement);
    assert.equal(onThisBoard.tone, page.ISSUE_KINDS.room.tone);
    assert.equal(onThisBoard.words, `${page.TONE_LEAD.critical}: ${label('room')}`);
    assert.equal(onAnother.tone, page.ISSUE_KINDS.roomElsewhere.tone);
    assert.equal(onAnother.words, `${page.TONE_LEAD.warning}: ${label('roomElsewhere')}`);
    // Saved, a room outside the programme rooms is Issues' "Room unsuitable",
    // a warning: the verdict leads with it, then says why.
    assert.equal(unsuitable.tone, page.ISSUE_KINDS.roomUnsuitable.tone);
    assert.equal(unsuitable.words, `${page.TONE_LEAD.warning}: ${label('roomUnsuitable')} · ${own.outsidePool}`);
  }
});

test("a room's facts name its type, seats, spare or short seats, gender and place, with Arabic counts", t => {
  const w = load(t);
  const facts = w.TimetableRoomChoice.factsHtml;
  const room = { room_code: 'R101', room_type: 'lecture', capacity: 40, section: 'M', building: 'B', wing: 'W', floor: 2 };
  const target = { required_capacity: 30 };
  assert.equal(facts(room, target, false), 'Lecture room · 40 seats · 10 spare · Male (M) · Building <bdi>B</bdi> · Wing <bdi>W</bdi> · Floor <bdi>2</bdi>');
  assert.equal(facts(room, target, true), 'قاعة محاضرات · 40 مقعداً · 10 مقاعد فائضة · طلاب (M) · المبنى <bdi>B</bdi> · الجناح <bdi>W</bdi> · الطابق <bdi>2</bdi>');
  const lab = { room_type: 'lab', capacity: 1, section: 'F', building: '', wing: null, floor: 0 };
  assert.equal(facts(lab, { required_capacity: 2 }, false), 'Lab · 1 seat · 1 short · Female (F) · Floor <bdi>0</bdi>');
  assert.equal(facts(lab, { required_capacity: 2 }, true), 'معمل · مقعد واحد · ينقصها مقعد واحد · طالبات (F) · الطابق <bdi>0</bdi>');
  // Arabic counts take the form of their number: 1, 2, 3–10, 11–99, 100+.
  const seats = n => facts({ capacity: n }, null, true);
  assert.deepEqual([1, 2, 3, 11, 100].map(seats), ['مقعد واحد', 'مقعدان', '3 مقاعد', '11 مقعداً', '100 مقعد']);
  const spare = n => facts({ capacity: 100 + n }, { required_capacity: 100 }, true).split(' · ')[1];
  assert.deepEqual([0, 1, 2, 3, 11, 100].map(spare), ['بلا فائض', 'مقعد فائض', 'مقعدان فائضان', '3 مقاعد فائضة', '11 مقعداً فائضاً', '100 مقعد فائض']);
  const short = n => facts({ capacity: 200 - n }, { required_capacity: 200 }, true).split(' · ')[1];
  assert.deepEqual([1, 2, 3, 11, 100].map(short), ['ينقصها مقعد واحد', 'ينقصها مقعدان', 'ينقصها 3 مقاعد', 'ينقصها 11 مقعداً', 'ينقصها 100 مقعد']);
  assert.equal(facts({ capacity: 0 }, { required_capacity: 0 }, false), '0 seats · 0 spare');
  // Codes from the room list are escaped and isolated; without a need, no spare or short.
  assert.equal(facts({ room_type: 'studio', capacity: 12, section: 'X', building: '<b>&' }, {}, false), '<bdi>studio</bdi> · 12 seats · <bdi>X</bdi> · Building <bdi>&lt;b&gt;&amp;</bdi>');
  assert.equal(facts(null, target, false), '');
});
