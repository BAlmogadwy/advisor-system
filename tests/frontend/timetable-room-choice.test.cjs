/* Change room's shared rules (static/js/timetable-room-choice.js): which rooms
 * a meeting may take by hand, and the words each room is shown with. One case
 * table (tests/fixtures/room_choice_cases.json, which the split-view parity
 * test in timetable-manual-clash-ruling.test.cjs also reads) drives fits,
 * occupied, the verdict in English and Arabic, the misfit reasons and, where a
 * case gives them, the facts: by the legacy rule, and (5c-audit) by the
 * quality audit for an answer that carries it. The module runs alone, with
 * only what base.html loads on every page. */
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
// A case's target: the shared one with the case's fields over it; a case asked
// with the audit (5c-audit) carries the shared audit target, its fields over it.
const targetOf = c => {
  const target = { ...table.target, ...(c.target || {}) };
  if ('audit_target' in c) target.audit = { ...table.audit_target, ...c.audit_target };
  return target;
};
// Judged by the audit: the target says it judged the meeting and the room
// carries its answer.
const audited = c => 'audit_target' in c && targetOf(c).audit.assessed === true && Boolean(c.room?.audit);
const kindOf = c => `${c.verdict.group}/${c.verdict.tone}`;

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
    const target = targetOf(c);
    const placement = 'placement' in c ? c.placement : table.placement;
    assert.equal(choice.codeKey(c.room?.room_code), c.code_key, c.name);
    assert.equal(choice.fits(c.room), c.fits, c.name);
    assert.equal(choice.occupied(c.room), c.occupied, c.name);
    for (const language of ['en', 'ar']) {
      w.LANGUAGE_CODE = language;
      assert.deepEqual(plain(choice.verdict(c.room, placement, target)), { group: c.verdict.group, tone: c.verdict.tone, words: c.verdict[language] }, `${c.name} (${language})`);
      assert.deepEqual(plain(choice.misfitReasons(c.room, target, language === 'ar')), c.reasons[language], `${c.name} (${language})`);
      if (c.facts) assert.equal(choice.factsHtml(c.room, target, language === 'ar'), c.facts[language], `${c.name} (${language})`);
    }
  }
  // Every group and tone is in the table, and by the legacy rule only a room
  // that fits is offered. No verdict is context: an info row never rings a
  // card (§7.1), and every room the dialog offers that Issues will list does
  // ring one.
  const legacy = table.cases.filter(c => !audited(c));
  const verdicts = legacy.map(kindOf);
  for (const kind of ['free/ok', 'outside/warning', 'inUse/critical', 'inUse/warning', 'misfit/']) assert.ok(verdicts.includes(kind), kind);
  assert.deepEqual([...new Set(verdicts)].sort(), ['free/ok', 'inUse/critical', 'inUse/warning', 'misfit/', 'outside/warning']);
  assert.ok(legacy.every(c => (c.verdict.group === 'misfit') === !c.fits));
  // A room outside the programme rooms, each way it can be offered: free, held
  // here, held elsewhere; and one that does not fit, which stays a misfit.
  const outside = c => c.room?.department_fit === false;
  for (const kind of ['outside/warning', 'inUse/critical', 'inUse/warning', 'misfit/']) {
    assert.ok(legacy.some(c => outside(c) && kindOf(c) === kind), `outside the programme rooms: ${kind}`);
  }
  // For each fit flag, a held room that misses on that flag alone: being held
  // never makes a misfit choosable, whichever check says so.
  const flags = ['fits_type', 'fits_gender', 'fits_capacity'];
  for (const flag of flags) {
    assert.ok(legacy.some(c => c.occupied && flags.every(f => (c.room[f] === false) === (f === flag))), `a held room that misses only ${flag}`);
  }
  // Judged by the audit: every group but the legacy outside one, and a misfit
  // exactly where the audit refuses for gender or type (the owner's answer to
  // Q1), held or not; seats alone never refuse.
  const byAudit = table.cases.filter(audited);
  assert.deepEqual([...new Set(byAudit.map(kindOf))].sort(), ['flagged/warning', 'free/ok', 'inUse/critical', 'inUse/warning', 'misfit/']);
  const refusedFor = c => c.room.audit.reasons.filter(reason => ['gender', 'type'].includes(reason));
  assert.ok(byAudit.every(c => (c.verdict.group === 'misfit') === (refusedFor(c).length > 0)));
  for (const reason of ['gender', 'type']) {
    assert.ok(byAudit.some(c => c.room.audit.reasons.join() === reason), `refused for ${reason} alone`);
  }
  assert.ok(byAudit.some(c => c.occupied && c.verdict.group === 'misfit'), 'a held room the audit refuses');
  assert.ok(byAudit.some(c => c.room.audit.reasons.join() === 'capacity' && c.verdict.group === 'flagged'), 'short of seats alone: choosable');
  // Both ways the two rules part: a room the legacy rule refuses that the
  // audit offers (seats), and one it offers that the audit refuses (gender).
  assert.ok(byAudit.some(c => !c.fits && c.verdict.group !== 'misfit'), 'refused by the legacy rule, offered by the audit');
  assert.ok(byAudit.some(c => c.fits && c.verdict.group === 'misfit'), 'offered by the legacy rule, refused by the audit');
  // Each of Issues' room kinds is said, alone and with others, in Issues' order.
  const lead = /^(Blocks publishing|Lowers quality): ([^·]*)/;
  const kindsSaid = byAudit.map(c => (lead.exec(c.verdict.en)?.[2] || '').trim()).filter(Boolean);
  for (const said of ['Room too small', 'Room unsuitable', 'Room double-booked', 'Room too small, Room unsuitable',
    'Room double-booked, Room too small, Room unsuitable', 'Room too small, Room also booked on another board']) assert.ok(kindsSaid.includes(said), said);
  // An answer that asked the audit and is judged by the legacy rule: not
  // assessed, assessed but not exactly true, a room with no audit.
  for (const [shape, holds] of Object.entries({
    'not assessed': c => c.audit_target.assessed === false,
    'assessed as text': c => c.audit_target.assessed === 'true',
    'a room with no audit': c => !c.room.audit,
  })) assert.ok(table.cases.some(c => 'audit_target' in c && !audited(c) && holds(c)), shape);
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
  // And the audit's words: its verdicts, what Issues say of the current room,
  // its notes and its reasons.
  const auditCase = name => table.cases.find(c => c.name.startsWith(name));
  const auditShown = () => ['en', 'ar'].flatMap(language => {
    w.LANGUAGE_CODE = language;
    const ar = language === 'ar';
    const said = c => choice.verdict(c.room, table.placement, targetOf(c)).words;
    const split = auditCase('a copy that stays in a small room');
    const target = { ...targetOf(split), audit: { ...targetOf(split).audit, capacity_source: 'default', boards: [...targetOf(split).audit.boards, { board_id: 4, board_label: 'GEN T1', programmes: [] }],
      current: split.room.audit } };
    return [
      said(split), said(auditCase('held on this board, too small')), said(auditCase('held only on another board')),
      said(auditCase('outside two other boards')), said(auditCase('a board with no programme')),
      choice.currentVerdict(target, table.placement, null).words, ...choice.auditNotes(target, ar).map(note => note.text),
      ...choice.misfitReasons(auditCase('a room of the wrong type and gender by the audit').room, targetOf(auditCase('a room of the wrong type and gender by the audit')), ar),
    ];
  });
  const before = [...shown(), ...auditShown()];
  // Every word and count, at every depth, in both languages, is written over.
  const scribble = words => {
    for (const [key, value] of Object.entries(words)) {
      if (value && typeof value === 'object') scribble(value);
      else Reflect.set(words, key, typeof value === 'function' ? () => 'changed' : 'changed {c}');
    }
  };
  scribble(choice.words(false));
  scribble(choice.words(true));
  assert.deepEqual([...shown(), ...auditShown()], before);
  assert.equal(choice.words(false).toneLead.critical, 'Blocks publishing');
  // The refused reasons and Issues' order are handed out frozen too.
  assert.throws(() => { 'use strict'; choice.REFUSED_AUDIT_REASONS.push('capacity'); });
  assert.throws(() => { 'use strict'; choice.KIND_ORDER.reverse(); });
  assert.deepEqual(plain(choice.REFUSED_AUDIT_REASONS), ['gender', 'type']);
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
  const seatsAt = workspace.indexOf('const seatsNeeded = ');
  const seatsEnd = workspace.indexOf('\n/* ', seatsAt);
  assert.ok(seatsAt > end && seatsEnd > seatsAt, "the page's seatsNeeded is where this test reads it");
  const inPool = c => c.room?.department_fit !== false;
  const legacy = table.cases.filter(c => !audited(c));
  const here = legacy.find(c => c.verdict.tone === 'critical' && inPool(c)).room;
  const elsewhere = legacy.find(c => c.verdict.tone === 'warning' && c.verdict.group === 'inUse' && inPool(c)).room;
  const outside = legacy.find(c => c.verdict.group === 'outside').room;
  for (const arabic of [false, true]) {
    const page = vm.runInNewContext(`${workspace.slice(begin, end)}\n${workspace.slice(seatsAt, seatsEnd)}\n;({ ISSUE_KINDS, ISSUE_KIND_ORDER, TONE_LEAD, seatsNeeded })`,
      { IS_AR: arabic, pluralAr: w.pluralAr });
    const label = kind => page.ISSUE_KINDS[kind][arabic ? 'ar' : 'en'];
    const own = w.TimetableRoomChoice.words(arabic);
    assert.deepEqual(plain(own.toneLead), plain(page.TONE_LEAD));
    // The room kinds, in Issues' order (5c-audit added "Room too small").
    assert.deepEqual(Object.keys(own.kinds), ['room', 'roomSmall', 'roomUnsuitable', 'roomElsewhere']);
    assert.deepEqual(plain(page.ISSUE_KIND_ORDER.filter(kind => kind in own.kinds)), Object.keys(own.kinds));
    assert.deepEqual(plain(w.TimetableRoomChoice.KIND_ORDER), Object.keys(own.kinds));
    for (const kind of Object.keys(own.kinds)) assert.equal(own.kinds[kind], label(kind), kind);
    // The seats a meeting needs, as Issues' "Room too small" row says them.
    for (const n of [0, 1, 2, 3, 11, 26, 100]) assert.equal(own.needsSeats(n), page.seatsNeeded(n), `${n} (${arabic ? 'ar' : 'en'})`);
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

/* ── 5c-audit: judged by the quality audit (D20c as amended; Q1) ── */
const AI_BOARD = { board_id: 2, board_label: 'AI T3', programmes: ['AI'] };
const auditTarget = (extra = {}) => ({ ...table.target, audit: { ...table.audit_target, ...extra } });
const auditRoom = (code, audit = {}, extra = {}) => ({ room_code: code, room_type: 'lecture', capacity: 40, section: 'M', available: true,
  fits_type: true, fits_gender: true, fits_capacity: true, department_fit: true, occupied_by: [],
  audit: { compatible: true, undersized: false, reasons: [], programme_board_ids: [], quality_reasons: [], ...audit }, ...extra });

test('the refused reasons are one list: gender and type by default; another list moves the line, and the verdict, reasons and facts follow it', t => {
  const choice = load(t).TimetableRoomChoice;
  const target = auditTarget();
  const typeOff = auditRoom('T1', { reasons: ['type'], quality_reasons: ['incompatible'] });
  const short = auditRoom('S1', { reasons: ['capacity'], undersized: true, quality_reasons: ['undersized'] }, { capacity: 20, available: false, fits_capacity: false });
  const both = auditRoom('B1', { reasons: ['type', 'gender'], quality_reasons: ['incompatible'] });
  const view = (room, refused) => plain([choice.verdict(room, table.placement, target, refused).group, choice.refusals(room, target, refused),
    choice.misfitReasons(room, target, false, refused)]);
  // The owner's answer: the wrong type is refused, seats short a warning.
  assert.deepEqual(view(typeOff), ['misfit', ['type'], ['This meeting needs a lecture room']]);
  assert.deepEqual(view(short), ['flagged', [], []]);
  // Gender alone refused (Q1's other answer): the wrong type is "Room unsuitable".
  assert.deepEqual(view(typeOff, ['gender']), ['flagged', [], []]);
  assert.equal(choice.verdict(typeOff, table.placement, target, ['gender']).words, 'Lowers quality: Room unsuitable');
  assert.deepEqual(view(both, ['gender']), ['misfit', ['gender'], ['This meeting needs a Male (M) room']]);
  // Seats refused too (the design's first default): a misfit whose facts say how many it lacks.
  assert.deepEqual(view(short, ['gender', 'type', 'capacity']), ['misfit', ['capacity'], []]);
  assert.match(choice.factsHtml(short, target, false), /20 seats · 6 short/);
  // Without the audit the list is never read: the legacy rule decides.
  const { audit, ...legacyShort } = short;
  assert.ok(audit);
  assert.deepEqual(plain(choice.refusals(legacyShort, target, [])), ['legacy']);
  assert.deepEqual(plain(choice.refusals(short, table.target, [])), ['legacy']);
  assert.deepEqual(plain(choice.refusals(table.cases[0].room, table.target)), []);
  assert.equal(choice.verdict(short, table.placement, table.target, []).group, 'misfit');
});

test("the rows Issues will list for a saved room are its quality reasons as Issues' kinds, in Issues' order; without the audit, the pool alone", t => {
  const choice = load(t).TimetableRoomChoice;
  const target = auditTarget();
  const kinds = reasons => plain(choice.issueKinds(auditRoom('K1', { quality_reasons: reasons }), target));
  assert.deepEqual(kinds([]), []);
  assert.deepEqual(kinds(['undersized']), ['roomSmall']);
  assert.deepEqual(kinds(['incompatible']), ['roomUnsuitable']);
  assert.deepEqual(kinds(['incompatible', 'undersized']), ['roomSmall', 'roomUnsuitable']);
  // "missing" is the board's (No room), "clash" the room's use; neither is a quality kind.
  assert.deepEqual(kinds(['missing', 'clash', 'incompatible']), ['roomUnsuitable']);
  assert.deepEqual(kinds(['constructor', '__proto__', 'toString']), []);
  // Without the audit: outside the programme rooms, or nothing, whatever the room carries.
  assert.deepEqual(plain(choice.issueKinds(auditRoom('K2', { quality_reasons: ['undersized'] }, { department_fit: false }), table.target)), ['roomUnsuitable']);
  assert.deepEqual(plain(choice.issueKinds(auditRoom('K3', { quality_reasons: ['undersized'] }), table.target)), []);
  // The audit's answer counts only as an object, on a target assessed exactly true.
  assert.equal(choice.auditOf({ audit: 'yes' }, target), null);
  assert.equal(choice.auditOf(auditRoom('K4'), { audit: { assessed: 1 } }), null);
  assert.equal(choice.auditOf(null, target), null);
  assert.deepEqual(plain(choice.auditOf(auditRoom('K5', { quality_reasons: ['undersized'] }), target)).quality_reasons, ['undersized']);
});

test('the current room is said as Issues say it now: its kinds, why, and what the meeting needs of it; nothing when Issues list nothing', t => {
  const w = load(t);
  const choice = w.TimetableRoomChoice;
  const p = { id: 10, board_id: 2, room: 'R3' };
  const current = (audit, extra = {}) => auditTarget({ current: { compatible: false, undersized: false, reasons: [], programme_board_ids: [], quality_reasons: [], ...audit }, ...extra });
  const said = (target, room = null, at = p) => plain(choice.currentVerdict(target, at, room));
  // A lab where the meeting needs a lecture room: unsuitable, and why.
  assert.deepEqual(said(current({ reasons: ['type'], quality_reasons: ['incompatible'] })),
    { tone: 'warning', words: 'Lowers quality: Room unsuitable · This meeting needs a lecture room' });
  // Too small and outside the pool, held on this board: critical, the clash first.
  assert.deepEqual(said(current({ reasons: ['programme', 'capacity'], programme_board_ids: [2], quality_reasons: ['undersized', 'incompatible'] }), { occupied_by: [{ board_id: 2 }] }),
    { tone: 'critical', words: "Blocks publishing: Room double-booked, Room too small, Room unsuitable · needs 26 seats · outside this board's programme rooms" });
  // Held only on another board, the audit flagging nothing.
  assert.deepEqual(said(current({ compatible: true }), { occupied_by: [{ board_id: 3 }] }), { tone: 'warning', words: 'Lowers quality: Room also booked on another board' });
  // A code the inventory does not know, split from a copy that stays: unsuitable, and where the copy is.
  assert.deepEqual(said(current({ reasons: ['split'], quality_reasons: ['incompatible'] }, { kept: [{ placement_id: 31, board_id: 3, board_label: 'DS T3', room: 'R50' }] }), undefined, { ...p, room: 'GHOST' }),
    { tone: 'warning', words: 'Lowers quality: Room unsuitable · the same class stays in \u2066R50\u2069 (\u2066DS T3\u2069)' });
  // The wrong gender, in Arabic.
  w.LANGUAGE_CODE = 'ar';
  assert.deepEqual(said(current({ reasons: ['gender'], quality_reasons: ['incompatible'] })),
    { tone: 'warning', words: 'يُضعف الجودة: القاعة غير مناسبة · هذا اللقاء يحتاج إلى قاعة طلاب (M)' });
  w.LANGUAGE_CODE = 'en';
  // Nothing Issues list: no words; no current room, no audit, not assessed: none.
  assert.equal(choice.currentVerdict(current({ compatible: true }), p, { occupied_by: [] }), null);
  assert.equal(choice.currentVerdict(auditTarget({ current: null }), p, { occupied_by: [{ board_id: 2 }] }), null);
  assert.equal(choice.currentVerdict(table.target, p, { occupied_by: [{ board_id: 2 }] }), null);
  assert.equal(choice.currentVerdict({ audit: { assessed: false, current: { quality_reasons: ['incompatible'] } } }, p, null), null);
});

test('the notes say what the audit holds of the meeting: copies that stay, by room; boards with no programme; seats assumed. None without it', t => {
  const choice = load(t).TimetableRoomChoice;
  const target = auditTarget({
    capacity_source: 'default', required_capacity: 40,
    boards: [AI_BOARD, { board_id: 4, board_label: 'GEN T1', programmes: [] }],
    kept: [{ placement_id: 31, board_id: 3, board_label: 'DS T3', room: 'R50' }, { placement_id: 32, board_id: 5, board_label: 'CS T5', room: ' r50 ' },
      { placement_id: 33, board_id: 6, board_label: 'IS T1', room: 'UNASSIGNED' }, { placement_id: 34, board_id: 7, board_label: 'X', room: '' }],
  });
  assert.deepEqual(plain(choice.auditNotes(target, false)), [
    { key: 'split', tone: 'warning', text: 'The same class stays in \u2066R50\u2069 (\u2066DS T3\u2069, \u2066CS T5\u2069): any other room leaves it split, which Issues shows as “Room unsuitable”.' },
    { key: 'noProgramme', tone: 'warning', text: '\u2066GEN T1\u2069 has no programme, so Issues shows every room as “Room unsuitable”.' },
    { key: 'defaultSeats', tone: 'info', text: 'This course has no budget row, so the quality check assumes 40 seats.' },
  ]);
  assert.deepEqual(plain(choice.auditNotes(target, true)).map(note => note.text), [
    'تبقى الشعبة نفسها في \u2066R50\u2069 (\u2066DS T3\u2069، \u2066CS T5\u2069)؛ وأي قاعة أخرى تتركها مقسّمة، فتعرضها «المشاكل» «القاعة غير مناسبة».',
    'ليس للوحة \u2066GEN T1\u2069 برنامج، لذا تعرض «المشاكل» كل قاعة «القاعة غير مناسبة».',
    'لا يوجد صف ميزانية لهذا المقرر، لذا يفترض فحص الجودة 40 مقعداً.',
  ]);
  // Seats in the form of their number.
  const assumed = (n, ar) => choice.auditNotes(auditTarget({ capacity_source: 'default', required_capacity: n }), ar)[0].text;
  assert.deepEqual([1, 2, 3, 11, 100].map(n => assumed(n, true)), ['مقعداً واحداً', 'مقعدين', '3 مقاعد', '11 مقعداً', '100 مقعد']
    .map(seats => `لا يوجد صف ميزانية لهذا المقرر، لذا يفترض فحص الجودة ${seats}.`));
  assert.deepEqual([1, 2].map(n => assumed(n, false)), ['This course has no budget row, so the quality check assumes 1 seat.', 'This course has no budget row, so the quality check assumes 2 seats.']);
  // A budgeted course, one board with a programme, no copy staying: nothing.
  assert.deepEqual(plain(choice.auditNotes(auditTarget(), false)), []);
  assert.deepEqual(plain(choice.auditNotes(auditTarget({ capacity_source: 'default', required_capacity: null }), false)), []);
  // Without the audit, or not assessed: none.
  assert.deepEqual(plain(choice.auditNotes(table.target, false)), []);
  assert.deepEqual(plain(choice.auditNotes({ audit: { ...target.audit, assessed: false } }, false)), []);
});

test('a board label or room from the answer goes into the words as it is: never a replace pattern, its markup left for the caller to escape', t => {
  const choice = load(t).TimetableRoomChoice;
  const label = '<b>"$&$1"</b>';
  const target = auditTarget({
    capacity_source: 'default', required_capacity: 40,
    boards: [AI_BOARD, { board_id: 3, board_label: label, programmes: ['DS'] }, { board_id: 4, board_label: "$'GEN", programmes: [] }],
    kept: [{ placement_id: 31, board_id: 3, board_label: label, room: 'R$`5' }],
  });
  const room = auditRoom('R60', { reasons: ['programme', 'split'], programme_board_ids: [3, 4], quality_reasons: ['incompatible'] });
  assert.equal(choice.verdict(room, table.placement, target).words, 'Lowers quality: Room unsuitable · outside the programme rooms of '
    + `\u2066${label}\u2069 · \u2066$'GEN\u2069 has no programme · the same class stays in \u2066R$\`5\u2069 (\u2066${label}\u2069)`);
  assert.deepEqual(plain(choice.auditNotes(target, false)).map(note => note.text).slice(0, 2), [
    `The same class stays in \u2066R$\`5\u2069 (\u2066${label}\u2069): any other room leaves it split, which Issues shows as “Room unsuitable”.`,
    "\u2066$'GEN\u2069 has no programme, so Issues shows every room as “Room unsuitable”.",
  ]);
  // A board the audit does not list is named by its id.
  const unknown = auditRoom('R61', { reasons: ['programme'], programme_board_ids: [9], quality_reasons: ['incompatible'] });
  assert.equal(choice.verdict(unknown, table.placement, target).words, 'Lowers quality: Room unsuitable · outside the programme rooms of \u20669\u2069');
});
