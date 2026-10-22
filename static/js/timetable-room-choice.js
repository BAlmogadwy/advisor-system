/* Change room: which rooms a meeting may take by hand, and the words each
   room is shown with.

   It reads one room candidate as preview_placement_room_candidates sends it
   (fits_type, fits_gender, fits_capacity, available, occupied_by,
   department_fit, room_type, capacity, section, building, wing, floor and,
   asked with ?audit=1, `audit`) and that answer's `target` (required_type,
   required_gender, required_capacity and, asked with ?audit=1, `audit`).

   Two rules, one per kind of answer:
   - An answer whose target.audit is assessed says what the quality audit
     (Issues' room rows) will say once each room is saved, and a room is
     judged by it (design record §7.3, 5c-audit; D20c as amended): a room
     the audit refuses for a reason in REFUSED_AUDIT_REASONS (gender, type:
     the owner's answer to Q1) cannot be chosen, and every other room can;
     its words are the rows Issues will list once it is saved (room
     double-booked or booked on another board, room too small, room
     unsuitable), in Issues' order, then why. Whether the page judges by it
     is the page's (it also needs the quality check to judge the scenario's
     rooms): it hands a target without `audit` where it does not.
   - An answer without it (a server before 5c-audit, a meeting the audit
     cannot judge) keeps the legacy rule, exactly as before: split view's,
     copied as it is (page-timetable-workspace-split.js: roomCodeKey,
     roomIsOccupied, roomFitsSection).
   So the two pages agree about every answer without the audit, and differ
   only on answers that carry it: classic judges by the audit, split view
   keeps the legacy rule until it calls this module itself with ?audit=1
   (design record F3). A parity test pins both.

   The words are the module's own, so it never reads a page's globals. A
   room's words are worded as the page's Issues word what saving it brings
   (TONE_LEAD, ISSUE_KINDS, ISSUE_KIND_ORDER and seatsNeeded in
   page-timetable-workspace.js); tests pin the two equal. Verdicts, misfit
   reasons and notes are plain text, which the caller escapes; factsHtml is
   HTML. A value from the room list is put into a sentence as it is: never
   read as a replace() pattern. */
(function () {
  'use strict';
  // The page's language, as the pages read it (base.html's LANGUAGE_CODE), at
  // call time: a page may set it after this script loads. The functions that
  // take `ar` use it when it is given.
  const isArabic = () => (typeof LANGUAGE_CODE === 'string' ? LANGUAGE_CODE : document.documentElement.lang) === 'ar';
  const arabicOf = ar => (ar === undefined ? isArabic() : Boolean(ar));
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // A code inside plain text, isolated left to right: inside an Arabic line a
  // Latin code is otherwise reordered with the words around it.
  const isolate = value => `\u2066${value}\u2069`;

  // Frozen all the way down: words() hands these tables to callers, and a
  // caller writing to them must not change the words of a later verdict.
  const freeze = table => {
    for (const value of Object.values(table)) if (value && typeof value === 'object') freeze(value);
    return Object.freeze(table);
  };
  // Arabic counts use pluralAr (shared-utils.js, loaded by base.html on every
  // page), read when a count is worded.
  const WORDS = freeze({
    en: {
      toneLead: { critical: 'Blocks publishing', warning: 'Lowers quality', info: 'For context' },
      // In Issues' order (the page's ISSUE_KIND_ORDER).
      kinds: { room: 'Room double-booked', roomSmall: 'Room too small', roomUnsuitable: 'Room unsuitable', roomElsewhere: 'Room also booked on another board' },
      free: 'Free and fits',
      outsidePool: "outside this board's programme rooms",
      // The audit's words: why a saved room is on Issues' list.
      needsSeats: n => `needs ${n} ${n === 1 ? 'seat' : 'seats'}`,
      outsideOther: boards => `outside the programme rooms of ${boards}`,
      noProgramme: board => `${board} has no programme`,
      kept: (room, boards) => `the same class stays in ${room} (${boards})`,
      listJoin: ', ',
      // The notes above the rooms, about the meeting rather than one room.
      splitNote: (room, boards, kind) => `The same class stays in ${room} (${boards}): any other room leaves it split, which Issues shows as “${kind}”.`,
      noProgrammeNote: (board, kind) => `${board} has no programme, so Issues shows every room as “${kind}”.`,
      defaultSeatsNote: n => `This course has no budget row, so the quality check assumes ${n} ${n === 1 ? 'seat' : 'seats'}.`,
      needsLead: 'This meeting',
      needs: {
        lecture: 'needs a lecture room', lab: 'needs a lab', type: 'needs a room of type {c}',
        M: 'needs a Male (M) room', F: 'needs a Female (F) room',
      },
      types: { lecture: 'Lecture room', lab: 'Lab' },
      genders: { M: 'Male (M)', F: 'Female (F)' },
      building: 'Building {c}', wing: 'Wing {c}', floor: 'Floor {c}',
      seats: n => `${n} ${n === 1 ? 'seat' : 'seats'}`,
      spare: n => `${n} spare`,
      short: n => `${n} short`,
    },
    ar: {
      toneLead: { critical: 'يمنع النشر', warning: 'يُضعف الجودة', info: 'للاطلاع' },
      kinds: { room: 'القاعة محجوزة مرتين', roomSmall: 'القاعة أصغر من المطلوب', roomUnsuitable: 'القاعة غير مناسبة', roomElsewhere: 'القاعة محجوزة في لوحة أخرى' },
      free: 'متاحة ومناسبة',
      outsidePool: 'خارج قاعات برامج هذه اللوحة',
      needsSeats: n => pluralAr(n, { one: 'يحتاج مقعداً واحداً', two: 'يحتاج مقعدين', few: 'يحتاج {n} مقاعد', many: 'يحتاج {n} مقعداً', other: 'يحتاج {n} مقعد' }),
      outsideOther: boards => `خارج قاعات برامج ${boards}`,
      noProgramme: board => `ليس للوحة ${board} برنامج`,
      kept: (room, boards) => `تبقى الشعبة نفسها في ${room} (${boards})`,
      listJoin: '، ',
      splitNote: (room, boards, kind) => `تبقى الشعبة نفسها في ${room} (${boards})؛ وأي قاعة أخرى تتركها مقسّمة، فتعرضها «المشاكل» «${kind}».`,
      noProgrammeNote: (board, kind) => `ليس للوحة ${board} برنامج، لذا تعرض «المشاكل» كل قاعة «${kind}».`,
      defaultSeatsNote: n => `لا يوجد صف ميزانية لهذا المقرر، لذا يفترض فحص الجودة ${pluralAr(n, { one: 'مقعداً واحداً', two: 'مقعدين', few: '{n} مقاعد', many: '{n} مقعداً', other: '{n} مقعد' })}.`,
      needsLead: 'هذا اللقاء يحتاج',
      needs: {
        lecture: 'إلى قاعة محاضرات', lab: 'إلى معمل', type: 'إلى قاعة من نوع {c}',
        M: 'إلى قاعة طلاب (M)', F: 'إلى قاعة طالبات (F)',
      },
      types: { lecture: 'قاعة محاضرات', lab: 'معمل' },
      genders: { M: 'طلاب (M)', F: 'طالبات (F)' },
      building: 'المبنى {c}', wing: 'الجناح {c}', floor: 'الطابق {c}',
      seats: n => pluralAr(n, { one: 'مقعد واحد', two: 'مقعدان', few: '{n} مقاعد', many: '{n} مقعداً', other: '{n} مقعد' }),
      spare: n => pluralAr(n, { zero: 'بلا فائض', one: 'مقعد فائض', two: 'مقعدان فائضان', few: '{n} مقاعد فائضة', many: '{n} مقعداً فائضاً', other: '{n} مقعد فائض' }),
      short: n => pluralAr(n, { one: 'ينقصها مقعد واحد', two: 'ينقصها مقعدان', few: 'ينقصها {n} مقاعد', many: 'ينقصها {n} مقعداً', other: 'ينقصها {n} مقعد' }),
    },
  });
  const words = ar => WORDS[arabicOf(ar) ? 'ar' : 'en'];

  /* The audit reasons that refuse a room (D20c as amended; the owner's answer
     to Q1, 2026-10-22): the wrong gender and the wrong type. A room short of
     the audit's seats, outside a board's programme rooms, or leaving the
     class split can be chosen: Issues then list it as a warning. */
  const REFUSED_AUDIT_REASONS = Object.freeze(['gender', 'type']);
  // The kinds a saved room can bring, in Issues' order (ISSUE_KIND_ORDER).
  const KIND_ORDER = Object.freeze(['room', 'roomSmall', 'roomUnsuitable', 'roomElsewhere']);
  // The quality audit's reason, as Issues list it (computeBoardIssues):
  // "missing" is read from the board, and "clash" is the room's use.
  const QUALITY_KINDS = Object.freeze({ undersized: 'roomSmall', incompatible: 'roomUnsuitable' });

  function codeKey(roomCode) {
    return String(roomCode || '').trim().toUpperCase();
  }

  /* Owner ruling 2026-10-20: a manual edit is allowed with any clash, and the
     clash is shown. A room that fits the section (type, gender, seats) may be
     assigned by hand even when another section holds it at that time: that is
     a room clash, not a misfit. Suggestions and bulk passes still pick only
     free rooms. */
  function occupied(room) {
    return Array.isArray(room?.occupied_by) && room.occupied_by.length > 0;
  }

  function fits(room) {
    return !!room
      && (!!room.available || occupied(room))
      && room.fits_type !== false
      && room.fits_gender !== false
      && room.fits_capacity !== false;
  }

  /* A board id as text, so 2 and "2" are one board; '' when there is none,
     and no board is ever "this board". */
  const boardKey = id => (id == null ? '' : String(id));
  const heldHere = (room, p) => {
    const board = boardKey(p?.board_id);
    return board !== '' && room.occupied_by.some(entry => boardKey(entry?.board_id) === board);
  };

  /* Outside the board's programme rooms (H14): saved, Issues list it as
     "Room unsuitable" (the quality check holds a room to the meeting's
     compatible rooms, the programme pool among them), a warning that rings
     the card. Only an explicit false counts: a flag the answer leaves out is
     no fact. */
  function outsidePool(room) {
    return room?.department_fit === false;
  }

  /* The audit's answer for a room, when the target says the audit judged the
     meeting (assessed exactly true) and the room carries one; else null, and
     the room is judged by the legacy rule. */
  function auditOf(room, target) {
    const audit = room?.audit;
    return target?.audit?.assessed === true && audit && typeof audit === 'object' ? audit : null;
  }
  const listOf = value => (Array.isArray(value) ? value : []);

  /* Why a room cannot be chosen: the audit reasons among `refused`, in the
     audit's order; without the audit, ['legacy'] for a room that does not
     fit. Empty: it can be chosen. */
  function refusals(room, target, refused = REFUSED_AUDIT_REASONS) {
    const audit = auditOf(room, target);
    if (!audit) return fits(room) ? [] : ['legacy'];
    return listOf(audit.reasons).filter(reason => refused.includes(reason));
  }

  // Issues' kinds for the meeting's quality reasons after the save, in
  // Issues' order.
  const kindsOf = audit => {
    const reasons = listOf(audit?.quality_reasons);
    return KIND_ORDER.filter(kind => reasons.some(reason => QUALITY_KINDS[reason] === kind));
  };
  const inOrder = kinds => KIND_ORDER.filter(kind => kinds.includes(kind));

  /* The rows the quality check will give the meeting once the room is saved,
     as Issues' kinds ("Room too small", "Room unsuitable"), in Issues' order;
     a room's use at that time (a clash) is not among them. Without the
     audit, "Room unsuitable" for a room outside the board's programme rooms
     (the legacy rule's one fact about it). */
  function issueKinds(room, target) {
    const audit = auditOf(room, target);
    if (audit) return kindsOf(audit);
    return outsidePool(room) ? ['roomUnsuitable'] : [];
  }

  // The meeting's boards as the audit lists them, and one board's label
  // (its id when the audit does not list it).
  const boardsOf = target => listOf(target?.audit?.boards).filter(board => board && typeof board === 'object');
  const boardOf = (target, id) => boardsOf(target).find(board => boardKey(board.board_id) === boardKey(id));
  const labelOf = (target, id) => String(boardOf(target, id)?.board_label ?? '').trim() || boardKey(id);
  const hasNoProgramme = board => listOf(board?.programmes).length === 0;
  /* The copies of the class the save leaves where they are, by room: each
     real room but `except`, with the labels of the boards it stays on. */
  function keptRooms(target, except = '') {
    const rooms = new Map();
    listOf(target?.audit?.kept).forEach(copy => {
      const room = String(copy?.room ?? '').trim();
      const key = codeKey(room);
      if (!key || key === 'UNASSIGNED' || key === codeKey(except)) return;
      const entry = rooms.get(key) || { room, boards: [] };
      const label = String(copy?.board_label ?? '').trim() || boardKey(copy?.board_id);
      if (!entry.boards.includes(label)) entry.boards.push(label);
      rooms.set(key, entry);
    });
    return [...rooms.values()];
  }

  /* Why a saved room is on Issues' list, after its kinds: the seats the
     audit needs (whenever "Room too small" is among them, also when only a
     copy that stays is too small); the programme rooms it is outside (this
     board's, else the other boards'); a board that has no programme; and
     the room each copy that stays keeps (the class left split). Codes and
     labels are isolated. */
  function auditDetails(audit, p, target, code, w) {
    const out = [];
    const need = Number(target?.audit?.required_capacity);
    if (kindsOf(audit).includes('roomSmall') && target?.audit?.required_capacity != null && Number.isFinite(need)) out.push(w.needsSeats(need));
    const pooled = [], bare = [];
    listOf(audit.programme_board_ids).forEach(id => {
      const board = boardOf(target, id);
      (board && hasNoProgramme(board) ? bare : pooled).push(id);
    });
    const here = boardKey(p?.board_id);
    if (here !== '' && pooled.some(id => boardKey(id) === here)) out.push(w.outsidePool);
    else if (pooled.length) out.push(w.outsideOther(pooled.map(id => isolate(labelOf(target, id))).join(w.listJoin)));
    bare.forEach(id => out.push(w.noProgramme(isolate(labelOf(target, id)))));
    if (listOf(audit.reasons).includes('split')) {
      keptRooms(target, code).forEach(kept => out.push(w.kept(isolate(kept.room), kept.boards.map(isolate).join(w.listJoin))));
    }
    return out;
  }
  const sentence = (w, tone, kinds, details) => `${w.toneLead[tone]}: ${kinds.map(kind => w.kinds[kind]).join(', ')}${details.map(detail => ` · ${detail}`).join('')}`;

  /* {group, tone, words} for a room offered to placement `p`, judged by the
     answer's `target`.

     Judged by the audit: a room it refuses (`refused`) is a misfit; a room
     in use is inUse (critical when this board uses it at that time, the
     clash publishing refuses; else a warning); a free room the audit will
     flag is flagged, a warning; any other room is free. Its words are
     Issues' card for it once saved: the lead of its tone, then its kinds in
     Issues' order (the clash, then too small, then unsuitable), then why.

     Without the audit, the legacy rule: the first rule that applies wins: a
     misfit, then in use, then outside the programme rooms, then free. A
     room this board already uses at that time is the clash publishing
     refuses (critical); one used only on other boards lowers quality
     (warning). A free room outside the programme rooms lowers quality too
     ("Room unsuitable", as Issues say once it is saved); a room in use that
     is also outside them keeps its clash's group and tone, and its words add
     the pool, so nothing Issues will list is left unsaid. Once a room fits,
     only `occupied_by` decides use: the server's own tone and `available`
     say "not free", which is not "cannot be chosen". Since PR #173 the
     server leaves the section's own same-slot copies (the ones a room change
     moves with this meeting) out of `occupied_by`; before it, those copies
     made a shared section's own room read as in use. */
  function verdict(room, p, target, refused = REFUSED_AUDIT_REASONS) {
    const audit = auditOf(room, target);
    if (audit) {
      if (refusals(room, target, refused).length) return { group: 'misfit', tone: '', words: '' };
      const w = words();
      let kinds = kindsOf(audit);
      let group = kinds.length ? 'flagged' : 'free';
      let tone = kinds.length ? 'warning' : 'ok';
      if (occupied(room)) {
        const here = heldHere(room, p);
        kinds = inOrder([...kinds, here ? 'room' : 'roomElsewhere']);
        group = 'inUse';
        tone = here ? 'critical' : 'warning';
      }
      if (group === 'free') return { group, tone, words: w.free };
      return { group, tone, words: sentence(w, tone, kinds, auditDetails(audit, p, target, room.room_code, w)) };
    }
    if (!fits(room)) return { group: 'misfit', tone: '', words: '' };
    const w = words();
    const pool = outsidePool(room) ? ` · ${w.outsidePool}` : '';
    if (occupied(room)) {
      const here = heldHere(room, p);
      const tone = here ? 'critical' : 'warning';
      return { group: 'inUse', tone, words: `${w.toneLead[tone]}: ${w.kinds[here ? 'room' : 'roomElsewhere']}${pool}` };
    }
    if (pool) return { group: 'outside', tone: 'warning', words: `${w.toneLead.warning}: ${w.kinds.roomUnsuitable}${pool}` };
    return { group: 'free', tone: 'ok', words: w.free };
  }

  // What the meeting needs, worded for a type and a gender it misses on.
  function needItems(typeOff, genderOff, need, w) {
    const items = [];
    if (typeOff) {
      const type = String(need?.required_type || '').trim();
      const kind = type.toLowerCase();
      if (kind === 'lecture' || kind === 'lab') items.push(w.needs[kind]);
      else if (type) items.push(w.needs.type.replace('{c}', () => isolate(type)));
    }
    if (genderOff) {
      const gender = String(need?.required_gender || '').trim().toUpperCase();
      if (gender === 'M' || gender === 'F') items.push(w.needs[gender]);
    }
    return items.map((item, i) => (i ? item : `${w.needsLead} ${item}`));
  }

  /* Why a room cannot be chosen, as plain text: its type, then its gender,
     each named from what the meeting needs. The lead ("This meeting needs")
     opens the first reason only, so the list reads as one line joined with
     " · ". Seats are not a reason here: factsHtml already says "{n} short"
     for exactly those rooms. Judged by the audit, the reasons are the
     refused ones among type and gender, named from the audit's need
     (target.audit); else the flags the room carries, from the target's. */
  function misfitReasons(room, target, ar, refused = REFUSED_AUDIT_REASONS) {
    if (!room) return [];
    const w = words(ar);
    if (auditOf(room, target)) {
      const off = refusals(room, target, refused);
      return needItems(off.includes('type'), off.includes('gender'), target.audit, w);
    }
    return needItems(room.fits_type === false, room.fits_gender === false, target, w);
  }

  /* What Issues say now about the meeting's current room (target.audit.current,
     the audit's answer for that room, also a code the inventory does not
     know), with its use at this time from its row (`room`) when the list
     has it, as {tone, words}: the lead and kinds, then why, then what the
     meeting needs of a type or gender it misses on. null when the audit did
     not judge the meeting, the meeting has no room, or Issues list nothing
     for it. */
  function currentVerdict(target, p, room) {
    const audit = target?.audit?.assessed === true ? target.audit.current : null;
    if (!audit || typeof audit !== 'object') return null;
    const w = words();
    let kinds = kindsOf(audit);
    let tone = kinds.length ? 'warning' : '';
    if (occupied(room)) {
      const here = heldHere(room, p);
      kinds = inOrder([...kinds, here ? 'room' : 'roomElsewhere']);
      tone = here ? 'critical' : 'warning';
    }
    if (!tone) return null;
    const reasons = listOf(audit.reasons);
    const details = [...auditDetails(audit, p, target, p?.room, w), ...needItems(reasons.includes('type'), reasons.includes('gender'), target.audit, w)];
    return { tone, words: sentence(w, tone, kinds, details) };
  }

  /* What the audit says of the meeting rather than of one room, as notes
     ({key, tone, text}, plain text): a copy of the class that stays in its
     room (any other room leaves the class split: "Room unsuitable"); a
     board with no programme (every room is "Room unsuitable"); and seats
     assumed for a course with no budget row. None without the audit. */
  function auditNotes(target, ar) {
    if (target?.audit?.assessed !== true) return [];
    const w = words(ar);
    const notes = [];
    keptRooms(target).forEach(kept => notes.push({ key: 'split', tone: 'warning',
      text: w.splitNote(isolate(kept.room), kept.boards.map(isolate).join(w.listJoin), w.kinds.roomUnsuitable) }));
    boardsOf(target).filter(hasNoProgramme).forEach(board => notes.push({ key: 'noProgramme', tone: 'warning',
      text: w.noProgrammeNote(isolate(labelOf(target, board.board_id)), w.kinds.roomUnsuitable) }));
    const need = Number(target.audit.required_capacity);
    if (target.audit.capacity_source === 'default' && target.audit.required_capacity != null && Number.isFinite(need)) {
      notes.push({ key: 'defaultSeats', tone: 'info', text: w.defaultSeatsNote(need) });
    }
    return notes;
  }

  /* What the room is, as HTML joined with " · ": its type, seats, the seats to
     spare or short of what the meeting needs (the audit's need when the room
     is judged by it), its gender, building, wing and floor. Codes from the
     room list are isolated (<bdi>); a fact the room list leaves empty is
     left out. */
  function factsHtml(room, target, ar) {
    if (!room) return '';
    const w = words(ar);
    const code = value => `<bdi>${escapeHtml(value)}</bdi>`;
    const parts = [];
    const type = String(room.room_type || '').trim();
    const kind = type.toLowerCase();
    if (kind === 'lecture' || kind === 'lab') parts.push(escapeHtml(w.types[kind]));
    else if (type) parts.push(code(type));
    const seats = room.capacity == null ? NaN : Number(room.capacity);
    if (Number.isFinite(seats)) {
      parts.push(escapeHtml(w.seats(seats)));
      const required = auditOf(room, target) ? target.audit.required_capacity : target?.required_capacity;
      const need = required == null ? NaN : Number(required);
      if (Number.isFinite(need)) parts.push(escapeHtml(seats >= need ? w.spare(seats - need) : w.short(need - seats)));
    }
    const gender = String(room.section || '').trim().toUpperCase();
    if (gender === 'M' || gender === 'F') parts.push(escapeHtml(w.genders[gender]));
    else if (gender) parts.push(code(gender));
    for (const key of ['building', 'wing', 'floor']) {
      const value = String(room[key] ?? '').trim();
      if (value) parts.push(escapeHtml(w[key]).replace('{c}', () => code(value)));
    }
    return parts.join(' · ');
  }

  window.TimetableRoomChoice = {
    REFUSED_AUDIT_REASONS, KIND_ORDER, codeKey, occupied, fits, outsidePool, auditOf, refusals, issueKinds,
    verdict, misfitReasons, currentVerdict, auditNotes, factsHtml, words,
  };
})();
