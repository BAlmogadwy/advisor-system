/* Change room's shared rules (static/js/timetable-room-choice.js): which rooms
 * a meeting may take by hand, and the words each room is shown with. One case
 * table (tests/fixtures/room_choice_cases.json, which the split-view parity
 * test in timetable-manual-clash-ruling.test.cjs also reads) drives fits,
 * occupied, the verdict in English and Arabic, and the misfit reasons. The
 * module runs alone, with only what base.html loads on every page. */
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
    assert.equal(choice.codeKey(c.room?.room_code), c.code_key, c.name);
    assert.equal(choice.fits(c.room), c.fits, c.name);
    assert.equal(choice.occupied(c.room), c.occupied, c.name);
    for (const language of ['en', 'ar']) {
      w.LANGUAGE_CODE = language;
      assert.deepEqual(plain(choice.verdict(c.room, table.placement)), { group: c.verdict.group, tone: c.verdict.tone, words: c.verdict[language] }, `${c.name} (${language})`);
      assert.deepEqual(plain(choice.misfitReasons(c.room, target, language === 'ar')), c.reasons[language], `${c.name} (${language})`);
    }
  }
  // Every group and tone is in the table, and only a room that fits is offered.
  const verdicts = table.cases.map(c => `${c.verdict.group}/${c.verdict.tone}`);
  for (const kind of ['free/ok', 'free/info', 'inUse/critical', 'inUse/warning', 'misfit/']) assert.ok(verdicts.includes(kind), kind);
  assert.ok(table.cases.every(c => (c.verdict.group === 'misfit') === !c.fits));
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

test("a room in use is worded and toned as the page's Issues word the clash it saves", t => {
  const w = load(t);
  const workspace = read('static/js/page-timetable-workspace.js');
  const begin = workspace.indexOf('const ISSUE_KINDS = {');
  const end = workspace.indexOf('const issueKindLabel', begin);
  assert.ok(begin >= 0 && end > begin, "the page's ISSUE_KINDS and TONE_LEAD are where this test reads them");
  const here = table.cases.find(c => c.verdict.tone === 'critical').room;
  const elsewhere = table.cases.find(c => c.verdict.tone === 'warning').room;
  for (const arabic of [false, true]) {
    const page = vm.runInNewContext(`${workspace.slice(begin, end)}\n;({ ISSUE_KINDS, TONE_LEAD })`, { IS_AR: arabic });
    const own = w.TimetableRoomChoice.words(arabic);
    assert.deepEqual(plain(own.toneLead), plain(page.TONE_LEAD));
    assert.equal(own.kinds.room, page.ISSUE_KINDS.room[arabic ? 'ar' : 'en']);
    assert.equal(own.kinds.roomElsewhere, page.ISSUE_KINDS.roomElsewhere[arabic ? 'ar' : 'en']);
    w.LANGUAGE_CODE = arabic ? 'ar' : 'en';
    const onThisBoard = w.TimetableRoomChoice.verdict(here, table.placement);
    const onAnother = w.TimetableRoomChoice.verdict(elsewhere, table.placement);
    assert.equal(onThisBoard.tone, page.ISSUE_KINDS.room.tone);
    assert.equal(onThisBoard.words, `${page.TONE_LEAD.critical}: ${page.ISSUE_KINDS.room[arabic ? 'ar' : 'en']}`);
    assert.equal(onAnother.tone, page.ISSUE_KINDS.roomElsewhere.tone);
    assert.equal(onAnother.words, `${page.TONE_LEAD.warning}: ${page.ISSUE_KINDS.roomElsewhere[arabic ? 'ar' : 'en']}`);
    assert.equal(w.TimetableRoomChoice.verdict(table.cases[1].room, table.placement).words.split(': ')[0], page.TONE_LEAD.info);
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
