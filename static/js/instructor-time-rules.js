/* Per-instructor teaching-time rules: the pure mirror of the rule semantics
   and the staff editor on Instructor Management (#imRulesDialog).

   THE MIRROR. core/services/instructor_availability.py defines what a rule
   set means; this file repeats it exactly, and
   tests/fixtures/instructor_availability_cases.json holds both to the same
   cases (tests/frontend/instructor-time-rules.test.cjs):
   - days are SUN..THU (0..4), times minutes from midnight, every window
     half-open [start, end), a whole day [0, 1440);
   - U_d = the union of the `unavailable` windows on day d; when ANY
     `available_only` rule exists, A_d = the union of the `available_only`
     windows on day d (empty when none names d) and forbidden_d =
     ([0, 1440) minus A_d) union U_d; otherwise forbidden_d = U_d;
   - a meeting (d, s, e) is blocked iff some (a, b) in forbidden_d has
     s < b and a < e: touching is not overlap, so an `available_only` window
     must CONTAIN the meeting;
   - `summary` is the API's `open` object: counts, open days per grid, the
     five course shapes (each meeting on its own day; 75 minutes takes a
     lecture period, longer a lab period), the days with no period at all,
     and the state any / none / narrow / limited, tested in that order.

   THE EDITOR. Opened from a roster row (page-instructor-management.js) with
   the instructor's saved rules read fresh (GET ops/instructors/time-rules/).
   Every change redraws the preview of the default periods at once. Closing
   (Cancel, the close button, Escape) never loses work: an unsaved draft is
   kept per instructor for the life of the page and offered again at the next
   open, with a way to discard it. A save sends the revision it was based on;
   a colleague's save in between comes back as STALE and the user chooses
   whose version wins. Errors are said inside the dialog, on the rule and the
   field the server (or the same check here) names.

   A classic page script declares `T`, `$` and `$$` at the top level, so this
   file keeps everything inside its own function. */
(function () {
  'use strict';

  const AR = document.documentElement.lang === 'ar';

  // ── The rule model (instructor_availability.py) ───────────────
  const DAY_CODES = Object.freeze(['SUN', 'MON', 'TUE', 'WED', 'THU']);
  const MINUTES_PER_DAY = 1440;
  const KIND_UNAVAILABLE = 'unavailable';
  const KIND_AVAILABLE_ONLY = 'available_only';
  const KINDS = Object.freeze([KIND_UNAVAILABLE, KIND_AVAILABLE_ONLY]);
  const MAX_RULES = 20;
  const MAX_NOTE = 200;
  const LECTURE_MEETING_MINUTES = 75;
  const SHAPES = Object.freeze([
    ['1x75', [75]],
    ['1x100', [100]],
    ['2x75', [75, 75]],
    ['2x100', [100, 100]],
    ['2x75+100', [75, 75, 100]],
  ]);
  const STATES = Object.freeze(['any', 'limited', 'narrow', 'none']);

  /* A digit an Arabic keyboard types, Arabic-Indic (U+0660-0669) or Eastern
     Arabic-Indic (U+06F0-06F9), as its ASCII 0-9; every other character as
     it is. Built from code points: nothing typed here can land as a literal
     escape. */
  function asciiDigits(value) {
    let out = '';
    for (const ch of String(value == null ? '' : value)) {
      const code = ch.charCodeAt(0);
      if (code >= 0x0660 && code <= 0x0669) out += String(code - 0x0660);
      else if (code >= 0x06F0 && code <= 0x06F9) out += String(code - 0x06F0);
      else out += ch;
    }
    return out;
  }

  /* Minutes from midnight of a time as the API reads it (wire_minutes):
     a string "H:MM" or "HH:MM" of ASCII digits, surrounding spaces allowed,
     hours 0-23 and minutes 0-59, and "24:00" only as an end. Anything else
     (a number, seconds, another digit set) is null. */
  const WIRE_TIME = /^([0-9]{1,2}):([0-9]{2})$/;
  function toMinutes(value, { end = true } = {}) {
    if (typeof value !== 'string') return null;
    const match = WIRE_TIME.exec(value.trim());
    if (!match) return null;
    const hours = Number(match[1]);
    const minutes = Number(match[2]);
    if (hours === 24 && minutes === 0) return end ? MINUTES_PER_DAY : null;
    if (hours > 23 || minutes > 59) return null;
    return hours * 60 + minutes;
  }

  /* A minute count as HH:MM: 0 -> "00:00", 1440 -> "24:00". */
  function toHhmm(minutes) {
    const hours = Math.floor(minutes / 60);
    return `${String(hours).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
  }

  /* A typed time in the canonical form the API takes: Arabic-Indic digits
     become ASCII, "9:00" becomes "09:00", and three or four digits with no
     colon ("900", "0900", "1330") are read as HHMM. Text that is none of
     these comes back trimmed (and in ASCII digits), for validation to name. */
  const BARE_TIME = /^([0-9]{3,4})$/;
  function normaliseTime(value) {
    const text = asciiDigits(value).trim();
    const colon = WIRE_TIME.exec(text);
    if (colon) return `${colon[1].padStart(2, '0')}:${colon[2]}`;
    const bare = BARE_TIME.exec(text);
    if (bare) {
      const digits = bare[1].padStart(4, '0');
      return `${digits.slice(0, 2)}:${digits.slice(2)}`;
    }
    return text;
  }

  /* One wire rule as {kind, mask, start, end}, or null when the API would
     refuse it (the preview leaves such a rule out until it is complete). */
  function ruleWindow(rule) {
    if (!rule || typeof rule !== 'object' || !KINDS.includes(rule.kind) || !Array.isArray(rule.days)) return null;
    let mask = 0;
    for (const day of rule.days) {
      const index = typeof day === 'string' ? DAY_CODES.indexOf(day.trim().toUpperCase()) : -1;
      if (index < 0) return null;
      mask |= 1 << index;
    }
    const start = toMinutes(rule.start, { end: false });
    const end = toMinutes(rule.end, { end: true });
    if (!mask || start === null || end === null || end <= start) return null;
    return { kind: rule.kind, mask, start, end };
  }

  /* Union of half-open intervals: overlapping AND touching ones merge. */
  function merge(intervals) {
    const sorted = intervals.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const merged = [];
    for (const [start, end] of sorted) {
      const last = merged[merged.length - 1];
      if (last && start <= last[1]) last[1] = Math.max(last[1], end);
      else merged.push([start, end]);
    }
    return merged;
  }

  /* [0, 1440) minus already-merged intervals. */
  function complement(merged) {
    const gaps = [];
    let cursor = 0;
    for (const [start, end] of merged) {
      if (start > cursor) gaps.push([cursor, start]);
      cursor = Math.max(cursor, end);
    }
    if (cursor < MINUTES_PER_DAY) gaps.push([cursor, MINUTES_PER_DAY]);
    return gaps;
  }

  /* A rule set (wire rules) compiled: per day, the merged forbidden
     intervals. `skipped` counts the rules left out as incomplete. */
  function compile(rules) {
    const list = Array.isArray(rules) ? rules : [];
    const valid = list.map(ruleWindow).filter(Boolean);
    const hasAvailableOnly = valid.some(rule => rule.kind === KIND_AVAILABLE_ONLY);
    const forbidden = DAY_CODES.map((_, day) => {
      const bit = 1 << day;
      const windows = kind => valid.filter(rule => rule.kind === kind && (rule.mask & bit)).map(rule => [rule.start, rule.end]);
      const blocked = windows(KIND_UNAVAILABLE);
      return merge(hasAvailableOnly ? blocked.concat(complement(merge(windows(KIND_AVAILABLE_ONLY)))) : blocked);
    });
    return Object.freeze({ forbidden, used: valid.length, skipped: list.length - valid.length });
  }

  /* Whether a meeting [start, end) on day index `day` breaks the rules. */
  function blocks(compiled, day, start, end) {
    if (!(start < end) || !Number.isInteger(day) || day < 0 || day >= DAY_CODES.length) return false;
    const intervals = compiled && compiled.forbidden ? compiled.forbidden[day] : [];
    return intervals.some(([a, b]) => start < b && a < end);
  }

  /* [day][slot]: true when that period is NOT blocked. */
  function openPeriods(compiled, slots) {
    const windows = (slots || []).map(slot => {
      const start = toMinutes(slot.start);
      const end = toMinutes(slot.end);
      if (start === null || end === null) throw new Error(`slot times do not read: ${slot.start}-${slot.end}`);
      return [start, end];
    });
    return DAY_CODES.map((_, day) => windows.map(([start, end]) => !blocks(compiled, day, start, end)));
  }

  /* Whether a weekly pattern fits: each meeting on its own day, that day
     having an open period of the meeting's grid. */
  function shapeFeasible(meetings, lectureDays, labDays) {
    const used = DAY_CODES.map(() => false);
    const place = index => {
      if (index === meetings.length) return true;
      const open = meetings[index] <= LECTURE_MEETING_MINUTES ? lectureDays : labDays;
      for (let day = 0; day < DAY_CODES.length; day++) {
        if (used[day] || !open[day]) continue;
        used[day] = true;
        if (place(index + 1)) return true;
        used[day] = false;
      }
      return false;
    };
    return place(0);
  }

  /* The API's `open` object for a compiled rule set over a period grid. */
  function summary(compiled, grid) {
    const lecture = openPeriods(compiled, grid.lecture);
    const lab = openPeriods(compiled, grid.lab);
    const count = table => ({
      open: table.reduce((n, row) => n + row.filter(Boolean).length, 0),
      total: table.reduce((n, row) => n + row.length, 0),
    });
    const counts = { lecture: count(lecture), lab: count(lab) };
    const lectureDays = lecture.map(row => row.some(Boolean));
    const labDays = lab.map(row => row.some(Boolean));
    const shapes = SHAPES.map(([key, meetings]) => ({ key, feasible: shapeFeasible(meetings, lectureDays, labDays) }));
    let state = 'limited';
    if (counts.lecture.open === counts.lecture.total && counts.lab.open === counts.lab.total) state = 'any';
    else if (!counts.lecture.open && !counts.lab.open) state = 'none';
    else if (!shapes.every(shape => shape.feasible)) state = 'narrow';
    return {
      lecture: counts.lecture,
      lab: counts.lab,
      days: { lecture: lectureDays, lab: labDays },
      shapes,
      no_teaching_days: DAY_CODES.filter((_, day) => !lectureDays[day] && !labDays[day]),
      state,
    };
  }

  /* The API's validation of a rule list, in its shape ({index, field, code},
     index -1 for the list), so a save the server would refuse is said before
     it is sent. The order within a rule is the server's: kind, days, start,
     end, note; a duplicate is named only on a rule with no other problem. */
  function validate(rules) {
    const errors = [];
    if (!Array.isArray(rules)) return [{ index: -1, field: 'rules', code: 'rules_not_list' }];
    if (rules.length > MAX_RULES) errors.push({ index: -1, field: 'rules', code: 'too_many' });
    const seen = new Set();
    rules.forEach((rule, index) => {
      const own = [];
      if (!rule || typeof rule !== 'object' || Array.isArray(rule)) {
        errors.push({ index, field: 'rule', code: 'rule_not_object' });
        return;
      }
      if (!KINDS.includes(rule.kind)) own.push({ index, field: 'kind', code: 'kind_invalid' });
      const days = rule.days;
      let mask = 0;
      if (days == null || (Array.isArray(days) && !days.length)) own.push({ index, field: 'days', code: 'days_empty' });
      else if (!Array.isArray(days)) own.push({ index, field: 'days', code: 'days_invalid' });
      else {
        const indexes = days.map(day => (typeof day === 'string' ? DAY_CODES.indexOf(day.trim().toUpperCase()) : -1));
        if (indexes.includes(-1)) own.push({ index, field: 'days', code: 'days_invalid' });
        else mask = indexes.reduce((m, day) => m | (1 << day), 0);
      }
      const start = toMinutes(rule.start, { end: false });
      if (start === null) own.push({ index, field: 'start', code: 'start_invalid' });
      const end = toMinutes(rule.end, { end: true });
      if (end === null) own.push({ index, field: 'end', code: 'end_invalid' });
      else if (start !== null && end <= start) own.push({ index, field: 'end', code: 'end_not_after_start' });
      const note = rule.note === undefined ? '' : rule.note;
      if (typeof note !== 'string') own.push({ index, field: 'note', code: 'note_invalid' });
      else if (note.trim().length > MAX_NOTE) own.push({ index, field: 'note', code: 'note_too_long' });
      if (own.length) {
        errors.push(...own);
        return;
      }
      const identity = `${rule.kind}|${mask}|${start}|${end}`;
      if (seen.has(identity)) errors.push({ index, field: 'rule', code: 'duplicate' });
      seen.add(identity);
    });
    return errors;
  }

  // ── Words ─────────────────────────────────────────────────────
  const pick = (en, ar) => (AR ? ar : en);
  const ruleCount = n => (AR
    ? pluralAr(n, { zero: 'لا قواعد', one: 'قاعدة واحدة', two: 'قاعدتان', few: '{n} قواعد', many: '{n} قاعدة', other: '{n} قاعدة' })
    : `${n} ${n === 1 ? 'rule' : 'rules'}`);
  const W = {
    title: pick('Teaching times', 'أوقات التدريس'),
    // Said in place of the template's sentence while the builder does not apply
    // the rules yet (the dialog carries data-rules-enforced="0").
    helpPending: pick(
      'The timetable builder does not apply these rules yet. They are saved here and take effect once teaching-time rules are switched on for the builder.',
      'لا يطبّق منشئ الجدول هذه القواعد بعد. تُحفظ هنا، ويبدأ العمل بها عند تفعيل قواعد أوقات التدريس في المنشئ.'),
    ruleN: n => pick(`Rule ${n}`, `القاعدة ${n}`),
    kindOf: n => pick(`What rule ${n} does`, `نوع القاعدة ${n}`),
    daysOf: n => pick(`Days of rule ${n}`, `أيام القاعدة ${n}`),
    removeN: n => pick(`Remove rule ${n}`, `حذف القاعدة ${n}`),
    kind: {
      unavailable: pick('Cannot teach', 'لا يدرّس'),
      available_only: pick('Can teach only', 'يدرّس فقط'),
    },
    allDay: pick('All day', 'طوال اليوم'),
    from: pick('From', 'من'),
    to: pick('To', 'إلى'),
    note: pick('Note (optional)', 'ملاحظة (اختيارية)'),
    notePlaceholder: pick('e.g. council meeting', 'مثل: اجتماع المجلس'),
    open: pick('Open', 'متاحة'),
    blocked: pick('Blocked', 'محجوبة'),
    day: pick('Day', 'اليوم'),
    lectureCaption: pick('Lecture periods (75 minutes): the default teaching periods', 'فترات المحاضرات (75 دقيقة): فترات التدريس الافتراضية'),
    labCaption: pick('Lab periods (100 minutes): the default teaching periods', 'فترات المعامل (100 دقيقة): فترات التدريس الافتراضية'),
    counts: (lecture, lab) => (AR
      ? safeHtml`فترات المحاضرات المتاحة: <bdi dir="ltr">${lecture.open}</bdi> من <bdi dir="ltr">${lecture.total}</bdi> · فترات المعامل المتاحة: <bdi dir="ltr">${lab.open}</bdi> من <bdi dir="ltr">${lab.total}</bdi>`
      : safeHtml`Lecture periods open: <bdi dir="ltr">${lecture.open}</bdi> of <bdi dir="ltr">${lecture.total}</bdi> · Lab periods open: <bdi dir="ltr">${lab.open}</bdi> of <bdi dir="ltr">${lab.total}</bdi>`),
    noTeachingOn: names => pick(`No teaching on: ${names.join(', ')}`, `لا تدريس في: ${names.join('، ')}`),
    shape: {
      '1x75': pick('1-credit course (one 75-minute meeting)', 'مقرر بساعة معتمدة واحدة (لقاء واحد مدته 75 دقيقة)'),
      '1x100': pick('2-credit course (one 100-minute meeting)', 'مقرر بساعتين معتمدتين (لقاء واحد مدته 100 دقيقة)'),
      '2x75': pick('3-credit course (two 75-minute meetings on different days)', 'مقرر بثلاث ساعات معتمدة (لقاءان مدة كلٍّ منهما 75 دقيقة في يومين مختلفين)'),
      '2x100': pick('3-credit course taught as two 100-minute meetings', 'مقرر بثلاث ساعات معتمدة يُدرَّس في لقاءين مدة كلٍّ منهما 100 دقيقة'),
      '2x75+100': pick('4-credit course (three meetings on three different days)', 'مقرر بأربع ساعات معتمدة (ثلاثة لقاءات في ثلاثة أيام مختلفة)'),
    },
    possible: pick('Possible', 'ممكن'),
    notPossible: pick('Not possible', 'غير ممكن'),
    warnNarrow: pick('Some course types cannot be scheduled for this instructor with these rules.',
      'لا يمكن بهذه القواعد جدولة بعض أنواع المقررات لهذا العضو.'),
    warnNone: pick('With these rules the instructor has no teachable period; the builder will leave their sections without this teacher.',
      'بهذه القواعد لا تبقى للعضو أي فترة تدريس متاحة، وسيترك منشئ الجدول شعبه دون إسنادها إليه.'),
    skipped: pick('A rule with no day, or with a missing or unreadable time, is left out of this preview until it is complete.',
      'القاعدة التي لا يوم فيها، أو وقتها ناقص أو غير مقروء، لا تدخل في هذه المعاينة حتى تكتمل.'),
    pill: {
      any: pick('Any time', 'أي وقت'),
      limited: pick('Limited', 'محدود'),
      narrow: pick('Very limited', 'محدود جداً'),
      none: pick('No open period', 'لا فترة متاحة'),
    },
    pillMeaning: (state, count, lecture) => {
      const rules = ruleCount(count);
      if (state === 'any') {
        return count
          ? pick(`Any time: ${rules}, none of which blocks a default teaching period.`, `أي وقت: القواعد (${rules}) لا تحجب أي فترة تدريس افتراضية.`)
          : pick('Any time: no teaching-time rules.', 'أي وقت: لا قواعد لأوقات التدريس.');
      }
      if (state === 'limited') {
        return pick(`Limited: ${rules}; ${lecture.open} of ${lecture.total} lecture periods open, and every course type still fits.`,
          `محدود: ${rules}، وتبقى ${lecture.open} من أصل ${lecture.total} فترة محاضرات متاحة، ويمكن جدولة كل أنواع المقررات.`);
      }
      if (state === 'narrow') {
        return pick(`Very limited: ${rules}; some course types can no longer be scheduled.`,
          `محدود جداً: ${rules}، ولم يعد ممكناً جدولة بعض أنواع المقررات.`);
      }
      return pick(`No open period: the rules (${rules}) leave no teaching period.`,
        `لا فترة متاحة: لا تُبقي القواعد (${rules}) أي فترة تدريس.`);
    },
    errors: {
      kind_invalid: pick('Choose what the rule does.', 'اختر نوع القاعدة.'),
      days_empty: pick('Choose at least one day.', 'اختر يوماً واحداً على الأقل.'),
      days_invalid: pick('These days cannot be read; choose them again.', 'تعذّرت قراءة الأيام؛ اخترها من جديد.'),
      start_invalid: pick('Type the start as a 24-hour time, such as 09:00.', 'اكتب وقت البداية بنظام 24 ساعة، مثل 09:00.'),
      end_invalid: pick('Type the end as a 24-hour time, such as 13:00 (24:00 is the end of the day).', 'اكتب وقت النهاية بنظام 24 ساعة، مثل 13:00 (و24:00 نهاية اليوم).'),
      end_not_after_start: pick('The end must be after the start.', 'يجب أن تكون النهاية بعد البداية.'),
      note_invalid: pick('The note cannot be read; type it again.', 'تعذّرت قراءة الملاحظة؛ اكتبها من جديد.'),
      note_too_long: pick(`Keep the note to ${MAX_NOTE} characters.`, `اجعل الملاحظة في حدود ${MAX_NOTE} حرف.`),
      duplicate: pick('This rule repeats an earlier one.', 'هذه القاعدة تكرّر قاعدة سابقة.'),
      rule_not_object: pick('This rule cannot be read; remove it and add it again.', 'تعذّرت قراءة هذه القاعدة؛ احذفها ثم أضفها من جديد.'),
      too_many: pick(`An instructor can have at most ${MAX_RULES} rules.`, `الحد الأقصى ${MAX_RULES} قاعدة لكل عضو هيئة تدريس.`),
      request: pick('The server could not read this request. Reload the page and try again.', 'تعذّر على الخادم قراءة الطلب. أعد تحميل الصفحة ثم حاول مجدداً.'),
    },
    notSaved: pick('Nothing was saved: correct the marked rules.', 'لم يُحفظ شيء: صحّح القواعد المشار إليها.'),
    roleRequired: pick('You do not have permission to change these rules.', 'لا تملك صلاحية تعديل هذه القواعد.'),
    notFound: pick('This instructor no longer exists. Reload the page.', 'لم يعد عضو هيئة التدريس هذا موجوداً. أعد تحميل الصفحة.'),
    saveFailed: pick('Could not save the rules. Check your connection and try again.', 'تعذّر حفظ القواعد. تحقّق من الاتصال ثم حاول مجدداً.'),
    loadFailed: pick('Could not load the teaching times', 'تعذّر تحميل أوقات التدريس'),
    saved: name => pick(`Teaching times saved for ${name}`, `حُفظت أوقات تدريس ${name}`),
    restored: pick('Unsaved changes restored.', 'استُعيدت التعديلات غير المحفوظة.'),
    stale: pick('A colleague changed these rules while you were editing.', 'عدّل زميل هذه القواعد أثناء تحريرك لها.'),
    added: n => pick(`Rule ${n} added.`, `أُضيفت القاعدة ${n}.`),
    removed: pick('Rule removed.', 'حُذفت القاعدة.'),
    savedLoaded: pick('The saved rules are loaded.', 'حُمِّلت القواعد المحفوظة.'),
    theirsLoaded: pick('Your colleague’s version is loaded.', 'حُمِّلت نسخة الزميل.'),
  };

  /* The roster's pill (and the dialog's): what the rules leave, in words. */
  function pill(timeRules) {
    const open = timeRules && timeRules.open;
    const state = open && open.state;
    if (!STATES.includes(state)) return safeHtml`<span class="im-none" aria-label="—">—</span>`;
    const count = Number(timeRules.count) || 0;
    const meaning = W.pillMeaning(state, count, open.lecture || { open: 0, total: 0 });
    const tone = { any: 'pill-muted', limited: 'pill-a', narrow: 'pill-r', none: 'pill-r' }[state];
    const ratio = state === 'limited' && open.lecture
      ? safeHtml` <bdi dir="ltr">${open.lecture.open}/${open.lecture.total}</bdi>`
      : '';
    return safeHtml`<span class="pill-status ${tone} im-times-pill" data-state="${state}" title="${meaning}"><span aria-hidden="true">${W.pill[state]}${ratio}</span><span class="sr-only">${meaning}</span></span>`;
  }

  // ── The editor ────────────────────────────────────────────────
  const API_GET = '/ops/instructors/time-rules/';
  const API_SET = '/ops/instructors/time-rules/set/';
  const ALL_DAYS = DAY_CODES.map(() => true);
  const NO_DAYS = DAY_CODES.map(() => false);
  const FIELD_ORDER = ['rule', 'kind', 'days', 'start', 'end', 'note'];
  const CLOSE_ICON = safeHtml`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;

  /* Each quick add appends one rule, filled in and editable, and says where
     the keyboard goes next. */
  const QUICK = {
    no_mornings: { fields: { kind: KIND_UNAVAILABLE, days: ALL_DAYS, start: '00:00', end: '13:00' }, focus: 'kind' },
    no_afternoons: { fields: { kind: KIND_UNAVAILABLE, days: ALL_DAYS, start: '13:00', end: '24:00' }, focus: 'kind' },
    day_off: { fields: { kind: KIND_UNAVAILABLE, days: NO_DAYS, allDay: true }, focus: 'days' },
    only_days: { fields: { kind: KIND_AVAILABLE_ONLY, days: NO_DAYS, allDay: true }, focus: 'days' },
    only_between: { fields: { kind: KIND_AVAILABLE_ONLY, days: ALL_DAYS, start: '09:00', end: '13:00' }, focus: 'start' },
    add: { fields: { kind: KIND_UNAVAILABLE, days: NO_DAYS }, focus: 'kind' },
  };

  const S = {
    wired: false,
    busy: false,
    instructor: null,
    grid: null,
    rows: [],
    revision: '',
    savedSnapshot: '[]',
    stale: null,
    restored: false,
    closingSaved: false,
    onSaved: null,
    dayNames: DAY_CODES.slice(),
  };
  /* Unsaved drafts by instructor id, for the life of the page. */
  const drafts = new Map();
  let serial = 0;

  const el = id => document.getElementById(id);
  const dialog = () => el('imRulesDialog');
  const dayName = index => S.dayNames[index] || DAY_CODES[index];
  const instructorName = instructor => (AR && instructor.full_name_ar ? instructor.full_name_ar : instructor.full_name) || '';

  function newRow(fields = {}) {
    return {
      key: `r${++serial}`,
      kind: KINDS.includes(fields.kind) ? fields.kind : KIND_UNAVAILABLE,
      days: DAY_CODES.map((_, i) => Boolean(fields.days && fields.days[i])),
      allDay: Boolean(fields.allDay),
      start: fields.start || '',
      end: fields.end || '',
      note: fields.note || '',
    };
  }

  function rowFromWire(rule) {
    const codes = (Array.isArray(rule.days) ? rule.days : []).map(day => String(day).trim().toUpperCase());
    const allDay = rule.all_day === true || (rule.start === '00:00' && rule.end === '24:00');
    return newRow({
      kind: rule.kind,
      days: DAY_CODES.map(code => codes.includes(code)),
      allDay,
      start: allDay ? '' : rule.start,
      end: allDay ? '' : rule.end,
      note: typeof rule.note === 'string' ? rule.note : '',
    });
  }

  /* What a save sends for a row: canonical times, "24:00" only from All day
     or a typed end. */
  function rowToWire(row) {
    return {
      kind: row.kind,
      days: DAY_CODES.filter((_, i) => row.days[i]),
      start: row.allDay ? '00:00' : normaliseTime(row.start),
      end: row.allDay ? '24:00' : normaliseTime(row.end),
      note: row.note.trim(),
    };
  }

  /* A draft compared with the saved rules: the same rules in the same order
     are no change, whatever spelling a time was typed in. */
  const snapshot = rows => JSON.stringify(rows.map(rowToWire));
  const isDirty = () => snapshot(S.rows) !== S.savedSnapshot;
  const cloneRows = rows => rows.map(row => ({ ...row, days: row.days.slice() }));

  function csrf() {
    try { return typeof getCsrfToken === 'function' ? getCsrfToken() : ''; } catch (_) { return ''; }
  }

  /* {ok, status, data, error}: the server's own error object when it sends
     one (code, message, details), else one made from the status. */
  async function request(url, options = {}) {
    let response;
    try {
      response = await fetch(url, {
        ...options,
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf(), ...(options.headers || {}) },
      });
    } catch (error) {
      return { ok: false, status: 0, data: null, error: { code: 'NETWORK', message: String((error && error.message) || error) } };
    }
    let body = null;
    try { body = await response.json(); } catch (_) { body = null; }
    if (response.ok && body && body.ok) return { ok: true, status: response.status, data: body, error: null };
    const own = body && body.error && typeof body.error === 'object' ? body.error : null;
    const error = own || { code: response.status === 403 ? 'ROLE_REQUIRED' : `HTTP_${response.status}`, message: `HTTP ${response.status}` };
    return { ok: false, status: response.status, data: body, error };
  }

  // ── Drawing ───────────────────────────────────────────────────
  function ruleHtml(row, index) {
    const n = index + 1;
    const id = `itr-${row.key}`;
    return safeHtml`<li class="im-rule" role="group" aria-labelledby="${id}-num" data-rule-key="${row.key}">
  <div class="im-rule-line">
    <span class="im-rule-num" id="${id}-num">${W.ruleN(n)}</span>
    <select class="form-select im-rule-kind" id="${id}-kind" data-field="kind" aria-label="${W.kindOf(n)}">
      <option value="unavailable">${W.kind.unavailable}</option>
      <option value="available_only">${W.kind.available_only}</option>
    </select>
    <div class="ux-seg im-days" role="group" id="${id}-days" data-field="days" aria-label="${W.daysOf(n)}">${DAY_CODES.map((code, day) => safeHtml`<button type="button" class="ux-seg-item im-day" data-day="${day}" aria-pressed="${row.days[day] ? 'true' : 'false'}"><span class="im-day-mark" aria-hidden="true"></span>${dayName(day)}</button>`)}</div>
    <button type="button" class="btn btn-sm btn-neutral btn-icon im-rule-remove" data-itr-remove="${row.key}" data-focus-key="itr-remove" aria-label="${W.removeN(n)}">${CLOSE_ICON}</button>
  </div>
  <div class="im-rule-line im-rule-times">
    <label class="im-form-check im-rule-allday"><input type="checkbox" data-field="all_day"><span>${W.allDay}</span></label>
    <div class="im-rule-field"><label for="${id}-start">${W.from}</label><input type="text" class="form-control ux-rows-time" id="${id}-start" data-field="start" inputmode="numeric" dir="ltr" autocomplete="off" placeholder="09:00" value="${row.allDay ? '00:00' : row.start}"></div>
    <div class="im-rule-field"><label for="${id}-end">${W.to}</label><input type="text" class="form-control ux-rows-time" id="${id}-end" data-field="end" inputmode="numeric" dir="ltr" autocomplete="off" placeholder="13:00" value="${row.allDay ? '24:00' : row.end}"></div>
    <div class="im-rule-field im-rule-note"><label for="${id}-note">${W.note}</label><input type="text" class="form-control" id="${id}-note" data-field="note" maxlength="${MAX_NOTE}" autocomplete="off" placeholder="${W.notePlaceholder}" value="${row.note}"></div>
  </div>
  <p class="ux-field-error" id="${id}-err" hidden></p>
</li>`;
  }

  function renderRules() {
    const list = el('imRulesList');
    list.innerHTML = String(safeHtml`${S.rows.map(ruleHtml)}`);
    S.rows.forEach(row => {
      const item = itemOf(row);
      item.querySelector('[data-field="kind"]').value = row.kind;
      item.querySelector('[data-field="all_day"]').checked = row.allDay;
      item.querySelectorAll('[data-field="start"], [data-field="end"]').forEach(input => { input.disabled = row.allDay; });
    });
    el('imRulesEmpty').hidden = S.rows.length > 0;
  }

  function periodTable(caption, slots, table) {
    return safeHtml`<table class="im-grid">
  <caption class="sr-only">${caption}</caption>
  <thead><tr><th scope="col"><span class="sr-only">${W.day}</span></th>${slots.map(slot => safeHtml`<th scope="col"><bdi dir="ltr">${slot.start}–<wbr>${slot.end}</bdi></th>`)}</tr></thead>
  <tbody>${table.map((row, day) => safeHtml`<tr><th scope="row">${dayName(day)}</th>${row.map(open => safeHtml`<td data-open="${open ? 'true' : 'false'}"><span class="ux-tone-icon" data-tone="${open ? 'ok' : 'critical'}" aria-hidden="true"></span><span class="sr-only">${open ? W.open : W.blocked}</span></td>`)}</tr>`)}</tbody>
</table>`;
  }

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  /* The live preview: what the rules on screen leave open, before saving. */
  function renderPreview() {
    if (!S.grid) return;
    const wire = S.rows.map(rowToWire);
    const compiled = compile(wire);
    const result = summary(compiled, S.grid);
    const count = wire.length;
    el('imRulesState').innerHTML = String(pill({ count, open: result }));
    el('imRulesCounts').innerHTML = String(W.counts(result.lecture, result.lab));
    const noDays = el('imRulesNoDays');
    setText(noDays, result.no_teaching_days.length
      ? W.noTeachingOn(result.no_teaching_days.map(code => dayName(DAY_CODES.indexOf(code)))) : '');
    noDays.hidden = !result.no_teaching_days.length;
    const skipped = el('imRulesSkipped');
    setText(skipped, compiled.skipped ? W.skipped : '');
    skipped.hidden = !compiled.skipped;
    el('imRulesShapes').innerHTML = String(safeHtml`${result.shapes.map(shape => safeHtml`<li class="im-shape" data-shape="${shape.key}" data-feasible="${shape.feasible ? 'true' : 'false'}"><span class="ux-tone-icon" data-tone="${shape.feasible ? 'ok' : 'critical'}" aria-hidden="true"></span><span class="im-shape-name">${W.shape[shape.key]}</span><span class="im-shape-verdict">${shape.feasible ? W.possible : W.notPossible}</span></li>`)}`);
    // The warning is a live region: written only when what it says changes.
    const warning = el('imRulesWarning');
    const tone = result.state === 'none' ? 'critical' : result.state === 'narrow' ? 'warning' : '';
    const words = tone === 'critical' ? W.warnNone : tone === 'warning' ? W.warnNarrow : '';
    if (warning.dataset.words !== words) {
      warning.dataset.words = words;
      warning.dataset.tone = tone;
      warning.innerHTML = words ? String(safeHtml`<span class="ux-tone-icon" data-tone="${tone}" aria-hidden="true"></span><span>${words}</span>`) : '';
    }
    const lecture = openPeriods(compiled, S.grid.lecture);
    const lab = openPeriods(compiled, S.grid.lab);
    // The table's title is shown above its scroller (a caption would scroll
    // away with a table wider than a phone); the caption names it for a
    // screen reader.
    setText(el('imRulesLectureLabel'), W.lectureCaption);
    setText(el('imRulesLabLabel'), W.labCaption);
    el('imRulesLectureGrid').innerHTML = String(periodTable(W.lectureCaption, S.grid.lecture, lecture));
    el('imRulesLabGrid').innerHTML = String(periodTable(W.labCaption, S.grid.lab, lab));
  }

  function renderTitle() {
    const title = el('imRulesTitle');
    title.textContent = `${W.title} — `;
    const name = document.createElement('bdi');
    name.textContent = instructorName(S.instructor);
    title.append(name);
  }

  function renderAll() {
    renderTitle();
    el('imRulesRestored').hidden = !S.restored;
    setText(el('imRulesRestoredText'), S.restored ? W.restored : '');
    showStale(null);
    clearErrors();
    renderRules();
    renderPreview();
  }

  // ── Errors ────────────────────────────────────────────────────
  function say(text) {
    const line = el('imRulesError');
    line.textContent = text || '';
    line.hidden = !text;
  }

  function announce(text) {
    const live = el('imRulesAnnounce');
    live.textContent = '';
    live.textContent = text;
  }

  const itemOf = row => el('imRulesList').querySelector(`[data-rule-key="${row.key}"]`);
  const rowOf = node => {
    const item = node && node.closest ? node.closest('[data-rule-key]') : null;
    return item ? S.rows.find(row => row.key === item.dataset.ruleKey) || null : null;
  };

  /* The control a field's error marks, and the one that takes the keyboard. */
  function controlFor(item, field) {
    if (field === 'rule') return { mark: item, focus: item.querySelector('[data-field="kind"]') };
    const control = item.querySelector(`[data-field="${field}"]`);
    if (!control) return { mark: item, focus: item.querySelector('[data-field="kind"]') };
    if (field === 'days') return { mark: control, focus: control.querySelector('[data-day]') };
    return { mark: control, focus: control };
  }

  function clearRuleErrors(item) {
    if (!item) return;
    [item, ...item.querySelectorAll('[aria-invalid]')].forEach(node => {
      if (node.getAttribute('aria-invalid') === 'true') {
        node.removeAttribute('aria-invalid');
        node.removeAttribute('aria-describedby');
      }
    });
    const message = item.querySelector('.ux-field-error');
    if (message) { message.textContent = ''; message.hidden = true; }
  }

  function clearErrors() {
    el('imRulesList').querySelectorAll('[data-rule-key]').forEach(clearRuleErrors);
    say('');
  }

  /* Errors in the API's shape onto the rows they name, by index and field.
     The keyboard goes to the first invalid control, in rule then field order. */
  function showErrors(errors) {
    clearErrors();
    const general = [];
    const byRow = new Map();
    (errors || []).forEach(error => {
      const index = Number.isInteger(error && error.index) ? error.index : -1;
      if (index >= 0 && index < S.rows.length) {
        if (!byRow.has(index)) byRow.set(index, []);
        byRow.get(index).push(error);
      } else {
        general.push(error);
      }
    });
    let first = null;
    [...byRow.keys()].sort((a, b) => a - b).forEach(index => {
      const item = itemOf(S.rows[index]);
      if (!item) return;
      const list = byRow.get(index).slice().sort((a, b) => FIELD_ORDER.indexOf(a.field) - FIELD_ORDER.indexOf(b.field));
      const message = item.querySelector('.ux-field-error');
      list.forEach(error => {
        const { mark, focus } = controlFor(item, error.field);
        mark.setAttribute('aria-invalid', 'true');
        mark.setAttribute('aria-describedby', message.id);
        if (!first) first = focus;
      });
      message.textContent = [...new Set(list.map(error => W.errors[error.code] || W.errors.request))].join(' ');
      message.hidden = false;
    });
    const lines = [...new Set(general.map(error => W.errors[error && error.code] || W.errors.request))];
    say([W.notSaved, ...lines].join(' '));
    if (first) first.focus();
  }

  function showStale(details) {
    S.stale = details;
    const box = el('imRulesStale');
    box.hidden = !details;
    setText(el('imRulesStaleText'), details ? W.stale : '');
  }

  // ── Actions ───────────────────────────────────────────────────
  function focusRow(row, where) {
    const item = row && itemOf(row);
    if (!item) return;
    const target = where === 'days' ? item.querySelector('[data-day]')
      : where === 'start' ? item.querySelector('[data-field="start"]')
        : item.querySelector('[data-field="kind"]');
    if (target) target.focus();
  }

  function addRule(kind) {
    const quick = QUICK[kind];
    if (!quick) return;
    if (S.rows.length >= MAX_RULES) {
      say(W.errors.too_many);
      return;
    }
    const row = newRow(quick.fields);
    S.rows.push(row);
    say('');
    renderRules();
    renderPreview();
    focusRow(row, quick.focus);
    announce(W.added(S.rows.length));
  }

  function removeRule(row) {
    const index = S.rows.indexOf(row);
    if (index < 0) return;
    S.rows.splice(index, 1);
    renderRules();
    renderPreview();
    const next = S.rows[index] || S.rows[index - 1];
    const target = next ? itemOf(next).querySelector('[data-itr-remove]') : el('imRulesAddRule');
    if (target) target.focus();
    announce(W.removed);
  }

  function useSaved(data) {
    S.rows = (data.rules || []).map(rowFromWire);
    S.revision = data.revision;
    S.savedSnapshot = snapshot(S.rows);
  }

  async function discard() {
    if (S.busy || !S.instructor) return;
    S.busy = true;
    const res = await request(`${API_GET}?instructor_id=${encodeURIComponent(S.instructor.id)}`);
    S.busy = false;
    if (!res.ok) {
      say(W.loadFailed);
      return;
    }
    S.grid = res.data.grid || S.grid;
    useSaved(res.data);
    drafts.delete(S.instructor.id);
    S.restored = false;
    renderAll();
    focusFirst();
    announce(W.savedLoaded);
  }

  function loadTheirs() {
    if (!S.stale) return;
    const stale = S.stale;
    useSaved({ rules: stale.rules || [], revision: stale.revision });
    S.restored = false;
    renderAll();
    focusFirst();
    announce(W.theirsLoaded);
  }

  function saveMine() {
    if (!S.stale) return;
    S.revision = S.stale.revision;
    showStale(null);
    save();
  }

  function setBusy(busy) {
    S.busy = busy;
    const form = el('imRulesForm');
    if (busy) form.setAttribute('aria-busy', 'true'); else form.removeAttribute('aria-busy');
    el('imRulesSave').disabled = busy;
  }

  async function save() {
    if (S.busy || !S.instructor) return;
    // Every typed time in its canonical form, on screen too, before anything is checked.
    S.rows.forEach(row => {
      if (row.allDay) return;
      row.start = normaliseTime(row.start);
      row.end = normaliseTime(row.end);
      const item = itemOf(row);
      if (item) {
        item.querySelector('[data-field="start"]').value = row.start;
        item.querySelector('[data-field="end"]').value = row.end;
      }
    });
    const rules = S.rows.map(rowToWire);
    const problems = validate(rules);
    if (problems.length) {
      showErrors(problems);
      return;
    }
    clearErrors();
    showStale(null);
    setBusy(true);
    const instructor = S.instructor;
    const res = await request(API_SET, {
      method: 'POST',
      body: JSON.stringify({ instructor_id: instructor.id, expected_revision: S.revision, rules }),
    });
    setBusy(false);
    if (S.instructor !== instructor) return;
    if (res.ok) {
      S.revision = res.data.revision;
      S.savedSnapshot = snapshot(S.rows);
      drafts.delete(instructor.id);
      const timeRules = { count: (res.data.rules || []).length, open: res.data.open };
      // The roster is drawn again first, so the row's Times button the dialog
      // gives the keyboard back to is the new one.
      if (typeof S.onSaved === 'function') S.onSaved(instructor.id, timeRules);
      S.closingSaved = true;
      window.UXControls.dialog.close(dialog());
      notify.success(W.saved(instructorName(instructor)));
      return;
    }
    const code = res.error && res.error.code;
    if (code === 'VALIDATION') {
      showErrors((res.error.details && res.error.details.errors) || []);
    } else if (code === 'STALE') {
      const details = res.error.details || {};
      showStale({ rules: details.rules || [], revision: details.revision || '' });
      say('');
      el('imRulesLoadTheirs').focus();
    } else if (code === 'ROLE_REQUIRED' || res.status === 403) {
      say(W.roleRequired);
    } else if (code === 'NOT_FOUND') {
      say(W.notFound);
    } else {
      say(W.saveFailed);
    }
  }

  function focusFirst() {
    const first = el('imRulesList').querySelector('[data-field="kind"]');
    (first || el('imRulesAddNoMornings')).focus();
  }

  function onClosed() {
    const instructor = S.instructor;
    if (instructor && !S.closingSaved) {
      if (isDirty()) drafts.set(instructor.id, { rows: cloneRows(S.rows), revision: S.revision });
      else drafts.delete(instructor.id);
    }
    S.closingSaved = false;
    showStale(null);
  }

  function wire() {
    if (S.wired) return;
    S.wired = true;
    const box = dialog();
    const list = el('imRulesList');
    box.querySelectorAll('[data-itr-quick]').forEach(button => {
      button.addEventListener('click', () => addRule(button.dataset.itrQuick));
    });
    el('imRulesDiscard').addEventListener('click', discard);
    el('imRulesLoadTheirs').addEventListener('click', loadTheirs);
    el('imRulesSaveMine').addEventListener('click', saveMine);
    el('imRulesForm').addEventListener('submit', event => {
      event.preventDefault();
      save();
    });
    box.addEventListener('ux-dialog-close', onClosed);
    list.addEventListener('click', event => {
      const remove = event.target.closest('[data-itr-remove]');
      if (remove) { removeRule(rowOf(remove)); return; }
      const toggle = event.target.closest('[data-day]');
      const row = rowOf(toggle);
      if (!toggle || !row) return;
      const day = Number(toggle.dataset.day);
      row.days[day] = !row.days[day];
      toggle.setAttribute('aria-pressed', row.days[day] ? 'true' : 'false');
      clearRuleErrors(itemOf(row));
      renderPreview();
    });
    list.addEventListener('change', event => {
      const row = rowOf(event.target);
      if (!row) return;
      const field = event.target.dataset.field;
      if (field === 'kind') {
        row.kind = event.target.value;
      } else if (field === 'all_day') {
        row.allDay = event.target.checked;
        const item = itemOf(row);
        const start = item.querySelector('[data-field="start"]');
        const end = item.querySelector('[data-field="end"]');
        start.disabled = end.disabled = row.allDay;
        start.value = row.allDay ? '00:00' : row.start;
        end.value = row.allDay ? '24:00' : row.end;
      } else {
        return;
      }
      clearRuleErrors(itemOf(row));
      renderPreview();
    });
    list.addEventListener('input', event => {
      const row = rowOf(event.target);
      const field = event.target.dataset.field;
      if (!row || !['start', 'end', 'note'].includes(field)) return;
      row[field] = event.target.value;
      clearRuleErrors(itemOf(row));
      renderPreview();
    });
    // A time is put in its canonical form when the field is left.
    list.addEventListener('focusout', event => {
      const row = rowOf(event.target);
      const field = event.target.dataset.field;
      if (!row || row.allDay || !['start', 'end'].includes(field)) return;
      const value = normaliseTime(event.target.value);
      if (value !== event.target.value) event.target.value = value;
      row[field] = value;
      renderPreview();
    });
  }

  /* Open the editor for one roster instructor ({id, full_name, ...}).
     `returnFocus` names the control the keyboard goes back to at close;
     `onSaved(id, {count, open})` lets the page redraw that row's pill;
     `dayNames` are the page's own day words, Sun..Thu. */
  async function open(instructor, { returnFocus, onSaved, dayNames } = {}) {
    const box = dialog();
    if (!box || !instructor || S.busy || window.UXControls.dialog.isOpen(box)) return false;
    wire();
    if (box.dataset.rulesEnforced === '0') el('imRulesHelp').textContent = W.helpPending;
    if (Array.isArray(dayNames) && dayNames.length === DAY_CODES.length) S.dayNames = dayNames.slice();
    S.busy = true;
    const res = await request(`${API_GET}?instructor_id=${encodeURIComponent(instructor.id)}`);
    S.busy = false;
    if (!res.ok) {
      notify.error(W.loadFailed);
      return false;
    }
    S.instructor = { ...instructor, ...(res.data.instructor || {}), id: instructor.id };
    S.grid = res.data.grid;
    S.onSaved = onSaved;
    useSaved(res.data);
    const draft = drafts.get(instructor.id);
    S.restored = Boolean(draft);
    if (draft) {
      S.rows = cloneRows(draft.rows);
      S.revision = draft.revision;
    }
    renderAll();
    return window.UXControls.dialog.open(box, {
      initialFocus: () => el('imRulesList').querySelector('[data-field="kind"]') || el('imRulesAddNoMornings'),
      returnFocus,
    });
  }

  window.InstructorTimeRules = Object.freeze({
    DAY_CODES,
    KINDS,
    MAX_RULES,
    MAX_NOTE,
    SHAPE_KEYS: Object.freeze(SHAPES.map(([key]) => key)),
    asciiDigits,
    toMinutes,
    toHhmm,
    normaliseTime,
    compile,
    blocks,
    openPeriods,
    shapeFeasible,
    summary,
    validate,
    pill,
    open,
    hasDraft: id => drafts.has(id),
  });
}());
