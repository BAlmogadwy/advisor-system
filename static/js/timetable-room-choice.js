/* Change room: which rooms a meeting may take by hand, and the words each
   room is shown with.

   It reads one room candidate as preview_placement_room_candidates sends it
   (fits_type, fits_gender, fits_capacity, available, occupied_by,
   department_fit, room_type, capacity, section, building, wing, floor) and
   that answer's `target` (required_type, required_gender, required_capacity).

   The rules that decide whether a room can be chosen are split view's,
   copied as they are (page-timetable-workspace-split.js: roomCodeKey,
   roomIsOccupied, roomFitsSection), so the two pages never disagree about a
   room; a parity test holds them together until split view calls this module
   itself (design record F3).

   The words are the module's own, so it never reads a page's globals. A room
   in use is described as the page's Issues describe the clash it saves
   (TONE_LEAD and ISSUE_KINDS in page-timetable-workspace.js); a test pins the
   two equal. Verdicts and misfit reasons are plain text, which the caller
   escapes; factsHtml is HTML. */
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

  // Arabic counts use pluralAr (shared-utils.js, loaded by base.html on every
  // page), read when a count is worded.
  const WORDS = {
    en: {
      toneLead: { critical: 'Blocks publishing', warning: 'Lowers quality', info: 'For context' },
      kinds: { room: 'Room double-booked', roomElsewhere: 'Room also booked on another board' },
      free: 'Free and fits',
      outsidePool: "outside this board's programme rooms",
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
      kinds: { room: 'القاعة محجوزة مرتين', roomElsewhere: 'القاعة محجوزة في لوحة أخرى' },
      free: 'متاحة ومناسبة',
      outsidePool: 'خارج قاعات برامج هذه اللوحة',
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
  };
  const words = ar => WORDS[arabicOf(ar) ? 'ar' : 'en'];

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

  /* {group, tone, words} for a room offered to placement `p`; the first rule
     that applies wins: a misfit, then in use, then free. A room this board
     already uses at that time is the clash publishing refuses (critical); one
     used only on other boards lowers quality (warning). Once a room fits, only
     `occupied_by` decides: the server's own tone and `available` say "not
     free", which is not "cannot be chosen", and the server already leaves out
     the copies of this class that move with this meeting. */
  function verdict(room, p) {
    if (!fits(room)) return { group: 'misfit', tone: '', words: '' };
    const w = words();
    if (occupied(room)) {
      const boardId = p ? p.board_id : undefined;
      const here = boardId != null && room.occupied_by.some(entry => entry && entry.board_id === boardId);
      const tone = here ? 'critical' : 'warning';
      return { group: 'inUse', tone, words: `${w.toneLead[tone]}: ${w.kinds[here ? 'room' : 'roomElsewhere']}` };
    }
    if (room.department_fit === false) return { group: 'free', tone: 'info', words: `${w.toneLead.info}: ${w.outsidePool}` };
    return { group: 'free', tone: 'ok', words: w.free };
  }

  /* Why a room cannot be chosen, as plain text: its type, then its gender,
     each named from what the meeting needs (`target`). The lead ("This
     meeting needs") opens the first reason only, so the list reads as one
     line joined with " · ". Seats are not a reason here: factsHtml already
     says "{n} short" for exactly those rooms. */
  function misfitReasons(room, target, ar) {
    if (!room) return [];
    const w = words(ar);
    const items = [];
    if (room.fits_type === false) {
      const type = String(target?.required_type || '').trim();
      const kind = type.toLowerCase();
      if (kind === 'lecture' || kind === 'lab') items.push(w.needs[kind]);
      else if (type) items.push(w.needs.type.replace('{c}', isolate(type)));
    }
    if (room.fits_gender === false) {
      const gender = String(target?.required_gender || '').trim().toUpperCase();
      if (gender === 'M' || gender === 'F') items.push(w.needs[gender]);
    }
    return items.map((item, i) => (i ? item : `${w.needsLead} ${item}`));
  }

  /* What the room is, as HTML joined with " · ": its type, seats, the seats to
     spare or short of what the meeting needs, its gender, building, wing and
     floor. Codes from the room list are isolated (<bdi>); a fact the room
     list leaves empty is left out. */
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
      const need = target?.required_capacity == null ? NaN : Number(target.required_capacity);
      if (Number.isFinite(need)) parts.push(escapeHtml(seats >= need ? w.spare(seats - need) : w.short(need - seats)));
    }
    const gender = String(room.section || '').trim().toUpperCase();
    if (gender === 'M' || gender === 'F') parts.push(escapeHtml(w.genders[gender]));
    else if (gender) parts.push(code(gender));
    for (const key of ['building', 'wing', 'floor']) {
      const value = String(room[key] ?? '').trim();
      if (value) parts.push(escapeHtml(w[key]).replace('{c}', code(value)));
    }
    return parts.join(' · ');
  }

  window.TimetableRoomChoice = { codeKey, occupied, fits, verdict, misfitReasons, factsHtml, words };
})();
