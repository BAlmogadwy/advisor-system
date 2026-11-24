/* The new staff planner screen, part one: load a student, the rules, the courses
   table and the registered week. No build, no options, no apply yet.

   Every node is made with createElement and textContent: nothing here writes
   markup, so the HTML-sink allow-list does not grow. Every visible string has an
   Arabic twin (IS_AR), counts go through pluralAr (shared-utils.js), and Latin
   codes and clock times sit in a left-to-right <bdi>. */
(() => {
  'use strict';

  const IS_AR = document.documentElement.lang === 'ar';
  const $ = (id) => document.getElementById(id);

  /* ── Strings ───────────────────────────────────────────────────────────── */
  const forms = (one, two, few, many) => ({ one, two, few, many, other: many });
  const T = {
    enterStudent: IS_AR ? 'أدخل رقم الطالب.' : 'Enter a student ID.',
    enterYear: IS_AR ? 'أدخل سنة من أربعة أرقام.' : 'Enter a four-digit year.',
    loading: IS_AR ? 'جارٍ تحميل بيانات الطالب…' : 'Loading student…',
    loaded: IS_AR ? 'تم تحميل بيانات الطالب.' : 'Student loaded.',
    notFound: IS_AR ? 'لا يوجد طالب بهذا الرقم.' : 'No student found with that ID.',
    failed: IS_AR ? 'تعذّر تحميل بيانات الطالب. حاول مرة أخرى.' : 'The student could not be loaded. Try again.',
    planFailed: IS_AR
      ? 'تعذّر تحميل خطة الطالب، لذا لا تُعرض مقررات إضافية.'
      : "The student's plan could not be loaded, so no extra courses are listed.",
    sectionsFailed: IS_AR ? 'تعذّر تحميل بيانات الشُعب.' : 'Section data could not be loaded.',
    unnamed: IS_AR ? 'طالب بلا اسم' : 'Unnamed student',
    noStudentTitle: IS_AR ? 'لم يتم تحميل بيانات طالب' : 'No student loaded',
    noStudentHint: IS_AR ? 'أدخل رقم الطالب أعلاه واضغط جلب.' : 'Enter a student ID above and press Fetch.',
    gpa: IS_AR ? 'المعدل التراكمي' : 'GPA',
    registeredCredits: IS_AR ? 'الساعات المسجلة' : 'Registered credits',
    creditCap: IS_AR ? 'الحد الأعلى للساعات' : 'Credit cap',
    advisory: (max) => (max ? (IS_AR ? `إرشادي · الأقصى ${max}` : `advisory · max ${max}`) : (IS_AR ? 'إرشادي' : 'advisory')),
    programme: IS_AR ? 'البرنامج' : 'Programme',
    idLabel: IS_AR ? 'الرقم' : 'ID',
    registered: IS_AR ? 'المسجّل' : 'Registered',
    registeredFixed: IS_AR ? 'المسجّل · ثابت هنا' : 'Registered · fixed here',
    registeredIgnored: IS_AR ? 'المسجّل · مُتجاهَل في هذا البناء' : 'Registered · ignored in this build',
    newInPlan: IS_AR ? 'جديد في هذه الخطة' : 'New in this plan',
    mustTake: IS_AR ? 'إلزامي' : 'Must-take',
    couldAdd: IS_AR ? 'يمكن إضافته' : 'Could be added',
    blocked: IS_AR ? 'غير مؤهل' : 'Blocked',
    missing: IS_AR ? 'ناقص:' : 'Missing:',
    inThisPlan: IS_AR ? '(ضمن هذه الخطة)' : '(in this plan)',
    prereqNotMet: IS_AR ? 'المتطلبات غير مستوفاة' : 'Prerequisites not met',
    anySection: IS_AR ? 'أي شعبة' : 'any section',
    pinned: IS_AR ? 'مثبّتة' : 'pinned',
    noRegistered: IS_AR ? 'لا مقررات مسجلة في هذا الفصل.' : 'No registered courses this term.',
    nothingAdded: IS_AR ? 'لم تُضف مقررات بعد.' : 'No courses added yet.',
    loadFirstCourses: IS_AR ? 'حمّل طالبًا لعرض مقرراته.' : 'Load a student to see their courses.',
    loadingPlan: IS_AR ? 'جارٍ تحميل خطة الطالب…' : "Loading the student's plan…",
    noSections: IS_AR ? 'لا شُعب هذا الفصل' : 'No sections this term',
    allFull: IS_AR ? 'كل الشُعب ممتلئة' : 'All sections full',
    unknown: '—',
    loadFirstWeek: IS_AR ? 'حمّل طالبًا لعرض أسبوعه.' : 'Load a student to see the week.',
    noClassesAtAll: IS_AR ? 'لا محاضرات مسجلة في هذا الفصل.' : 'No registered classes this term.',
    noClasses: IS_AR ? 'لا محاضرات' : 'No classes',
    dayOff: IS_AR ? 'يوم إجازة' : 'day off',
    free: IS_AR ? 'فارغ' : 'Free',
    noGap: IS_AR ? 'بلا فجوات' : 'no gap',
    noFreeDay: IS_AR ? 'لا يوم فارغ' : 'no free day',
    noTimedClasses: IS_AR ? 'بلا محاضرات' : 'No timed classes',
    removeCourse: (code) => (IS_AR ? `إزالة ${code}` : `Remove ${code}`),
    addCourse: (code) => (IS_AR ? `إضافة ${code}` : `Add ${code}`),
    mustTakeFor: (code) => (IS_AR ? `إلزامي ${code}` : `Must-take ${code}`),
    registeredLock: IS_AR ? 'مسجّل' : 'Registered',
    added: (code) => (IS_AR ? ['أُضيف ', bdi(code), ' إلى الخطة.'] : ['Added ', bdi(code), ' to the plan.']),
    removed: (code) => (IS_AR ? ['أُزيل ', bdi(code), ' من الخطة.'] : ['Removed ', bdi(code), ' from the plan.']),
    noMatch: IS_AR ? 'لا مقرر مطابق' : 'No course matches',
    free2: IS_AR ? 'متاحة' : 'free',
    over: IS_AR ? 'زيادة' : 'over',
    legendRegistered: IS_AR ? 'المسجّل' : 'Registered',
    classWord: (n) => (IS_AR
      ? pluralAr(n, forms('محاضرة واحدة', 'محاضرتان', '{n} محاضرات', '{n} محاضرة'))
      : `${n} ${n === 1 ? 'class' : 'classes'}`),
    dayWord: (n) => (IS_AR
      ? pluralAr(n, forms('يوم واحد', 'يومان', '{n} أيام', '{n} يومًا'))
      : `${n} ${n === 1 ? 'day' : 'days'}`),
    sectionWord: (n) => (IS_AR
      ? pluralAr(n, forms('شعبة واحدة', 'شعبتان', '{n} شعب', '{n} شعبة'))
      : `${n} ${n === 1 ? 'section' : 'sections'}`),
    seatsLeft: (n) => (IS_AR
      ? pluralAr(n, forms('مقعد واحد متبقٍ', 'مقعدان متبقيان', '{n} مقاعد متبقية', '{n} مقعدًا متبقيًا'))
      : `${n} ${n === 1 ? 'seat' : 'seats'} left`),
    freeDays: (n) => (IS_AR
      ? pluralAr(n, forms('يوم فارغ', 'يومان فارغان', '{n} أيام فارغة', '{n} يومًا فارغًا'))
      : `${n} free days`),
    courseWord: (n) => (IS_AR
      ? pluralAr(n, forms('مقرر واحد', 'مقرران', '{n} مقررات', '{n} مقررًا'))
      : `${n} ${n === 1 ? 'course' : 'courses'}`),
    untimed: (n) => (IS_AR
      ? `${pluralAr(n, forms('مقرر مسجّل واحد', 'مقرران مسجّلان', '{n} مقررات مسجّلة', '{n} مقررًا مسجّلًا'))} بلا مواعيد في الجدول.`
      : `${n} registered ${n === 1 ? 'course has' : 'courses have'} no timetable slots.`),
    rulesChanged: (n) => (IS_AR
      ? pluralAr(n, forms('قاعدة واحدة تغيّرت', 'قاعدتان تغيّرتا', '{n} قواعد تغيّرت', '{n} قاعدة تغيّرت'))
      : `${n} ${n === 1 ? 'rule' : 'rules'} changed`),
    credits: (n) => (IS_AR
      ? pluralAr(n, forms('ساعة واحدة', 'ساعتان', '{n} ساعات', '{n} ساعة'))
      : `${n} cr`),
    creditUnit: (n) => (IS_AR ? pluralAr(n, forms('ساعة', 'ساعتان', 'ساعات', 'ساعة')) : 'cr'),
    minutes: (n) => (IS_AR ? `${n} د` : `${n} min`),
    hoursMinutes: (h, m) => (IS_AR ? `${h} س${m ? ` ${m} د` : ''}` : `${h} h${m ? ` ${m} min` : ''}`),
    ends: IS_AR ? 'تنتهي' : 'ends',
    gap: IS_AR ? 'فجوة' : 'gap',
    freeOne: (day) => (IS_AR ? `${day} فارغ` : `${day} free`),
    maxHint: (cap, reg) => {
      if (cap && reg) return IS_AR ? `إرشادي ${cap} · النظامي ${reg}` : `advisory ${cap} · regulatory ${reg}`;
      if (cap) return IS_AR ? `إرشادي ${cap}` : `advisory ${cap}`;
      return IS_AR ? 'الحد الإرشادي غير معروف.' : 'The advisory cap is not known.';
    },
    maxError: (max) => (IS_AR ? `أدخل عددًا صحيحًا من 1 إلى ${max}.` : `Enter a whole number from 1 to ${max}.`),
    // The rules summary.
    ruleMode: { keep: IS_AR ? 'الإبقاء على المسجّل' : 'Keep Registered', ignore: IS_AR ? 'تجاهل المسجّل' : 'Ignore Registered' },
    ruleElig: { strict: IS_AR ? 'أهلية صارمة' : 'Strict eligibility', relaxed: IS_AR ? 'أهلية مرنة' : 'Relaxed eligibility' },
    ruleMax: (n) => (n === null ? (IS_AR ? 'الحد الأقصى غير محدد' : 'Max credits not set') : (IS_AR ? `الحد الأقصى ${n}` : `Max credits ${n}`)),
    ruleProg: { true: IS_AR ? 'شُعب البرنامج فقط' : 'This programme only', false: IS_AR ? 'أي برنامج' : 'Any programme' },
    ruleFull: { true: IS_AR ? 'الشُعب الممتلئة مسموحة' : 'Full sections allowed', false: IS_AR ? 'بلا شُعب ممتلئة' : 'No full sections' },
    ruleSwap: { true: IS_AR ? 'اقتراح تبديلات' : 'Suggest swaps', false: IS_AR ? 'بلا تبديلات' : 'No swaps' },
  };

  const DAY_ORDER = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
  const ACADEMIC_DAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU'];
  const DAY_SHORT = IS_AR
    ? { SUN: 'أحد', MON: 'اثن', TUE: 'ثلا', WED: 'أرب', THU: 'خم', FRI: 'جمع', SAT: 'سبت' }
    : { SUN: 'Sun', MON: 'Mon', TUE: 'Tue', WED: 'Wed', THU: 'Thu', FRI: 'Fri', SAT: 'Sat' };
  const DAY_FULL_AR = { SUN: 'الأحد', MON: 'الاثنين', TUE: 'الثلاثاء', WED: 'الأربعاء', THU: 'الخميس', FRI: 'الجمعة', SAT: 'السبت' };
  const DAY_ALIASES = {
    SUN: 'SUN', SUNDAY: 'SUN', 'الأحد': 'SUN', 'الاحد': 'SUN',
    MON: 'MON', MONDAY: 'MON', 'الاثنين': 'MON', 'الإثنين': 'MON',
    TUE: 'TUE', TUESDAY: 'TUE', 'الثلاثاء': 'TUE',
    WED: 'WED', WEDNESDAY: 'WED', 'الأربعاء': 'WED',
    THU: 'THU', THURSDAY: 'THU', 'الخميس': 'THU',
    FRI: 'FRI', FRIDAY: 'FRI', 'الجمعة': 'FRI',
    SAT: 'SAT', SATURDAY: 'SAT', 'السبت': 'SAT',
  };

  const DEFAULT_RULES = Object.freeze({
    mode: 'keep', eligibility: 'strict', maxCredits: null, programOnly: true, allowFull: false, swap: false,
  });
  const SEAT_WARNING = 5;

  /* ── State: everything the screen draws is read from here ──────────────── */
  const state = {
    ctxGen: 0,         // student requests: only the newest may write
    gen: 0,            // the student on screen: a new one retires the old one's plan and sections
    catGen: 0,         // section-catalogue requests: only the newest may write
    ctx: null,         // {studentId, year, term} of the student on screen
    student: null,
    baseline: [],
    recs: [],
    planCourses: null, // null = not loaded / failed; [] = loaded, nothing
    planState: 'idle', // idle | loading | ready | error
    catalog: null,     // Map normalised course code -> [section]; null = unknown
    shortlist: [],
    rules: { ...DEFAULT_RULES },
    shownElig: null,   // the eligibility the suggestions on screen were loaded with
    defaults: { ...DEFAULT_RULES },
    focus: null,       // {kind, code}: where focus goes after the next redraw
  };

  /* ── Small helpers ─────────────────────────────────────────────────────── */
  function el(tag, cls, ...kids) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    for (const kid of kids) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(typeof kid === 'number' ? String(kid) : kid);
    }
    return node;
  }
  function bdi(text) {
    const node = document.createElement('bdi');
    node.dir = 'ltr';
    node.textContent = String(text);
    return node;
  }
  // A name may be Arabic or Latin: isolate it and let its own first letter set the direction.
  function isolate(text) {
    const node = document.createElement('bdi');
    node.textContent = String(text);
    return node;
  }
  function svg(parts, size) {
    const ns = 'http://www.w3.org/2000/svg';
    const root = document.createElementNS(ns, 'svg');
    root.setAttribute('viewBox', '0 0 24 24');
    root.setAttribute('width', String(size || 16));
    root.setAttribute('height', String(size || 16));
    root.setAttribute('fill', 'none');
    root.setAttribute('stroke', 'currentColor');
    root.setAttribute('stroke-width', '1.75');
    root.setAttribute('stroke-linecap', 'round');
    root.setAttribute('stroke-linejoin', 'round');
    root.setAttribute('aria-hidden', 'true');
    for (const [tag, attrs] of parts) {
      const node = document.createElementNS(ns, tag);
      for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
      root.append(node);
    }
    return root;
  }
  const ICONS = {
    lock: [['rect', { x: 5, y: 11, width: 14, height: 9, rx: 2 }], ['path', { d: 'M8 11V8a4 4 0 0 1 8 0v3' }]],
    plus: [['path', { d: 'M12 5v14M5 12h14' }]],
    x: [['path', { d: 'M6 6l12 12M18 6L6 18' }]],
  };
  const norm = (code) => String(code || '').replace(/\s+/g, '').toUpperCase();
  const finitePositive = (value) => {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : null;
  };
  const finiteNonNegative = (value) => {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 ? n : null;
  };
  function toMins(value) {
    if (value === null || value === undefined) return null;
    const m = /^(\d{1,2}):(\d{2})/.exec(String(value).trim());
    if (!m) return null;
    const h = Number(m[1]);
    const mi = Number(m[2]);
    return h > 23 || mi > 59 ? null : h * 60 + mi;
  }
  const hhmm = (mins) => `${String(Math.floor(mins / 60)).padStart(2, '0')}:${String(mins % 60).padStart(2, '0')}`;
  const normDay = (value) => {
    const v = String(value || '').trim();
    return DAY_ALIASES[v.toUpperCase()] || DAY_ALIASES[v] || null;
  };
  function courseKeyOf(row) {
    const key = norm(row && row.course_key);
    if (key) return key;
    const code = norm(row && row.course_code);
    const num = norm(row && row.course_number);
    return code && num && code !== num ? `${code}${num}` : code || num;
  }
  function sectionCapacity(section) {
    const max = finiteNonNegative(section && section.available_capacity);
    const taken = finiteNonNegative(section && section.registered_count);
    if (max === null || taken === null) return { known: false, full: false, remaining: null };
    const remaining = Math.max(0, max - taken);
    return { known: true, full: remaining === 0, remaining };
  }
  function durationText(mins) {
    if (mins < 60) return T.minutes(mins);
    return T.hoursMinutes(Math.floor(mins / 60), mins % 60);
  }

  /* ── The one status line ───────────────────────────────────────────────── */
  function setStatus(kind, ...parts) {
    const line = $('plbStatus');
    line.dataset.kind = kind || '';
    line.replaceChildren(...parts.map((p) => (typeof p === 'string' ? document.createTextNode(p) : p)));
  }

  /* ── Rules ─────────────────────────────────────────────────────────────── */
  function changedRuleKeys() {
    return Object.keys(DEFAULT_RULES).filter((key) => state.rules[key] !== state.defaults[key]);
  }
  function renderRules() {
    const r = state.rules;
    $('plbRules1').textContent = [T.ruleMode[r.mode], T.ruleElig[r.eligibility], T.ruleMax(r.maxCredits)].join(' · ');
    $('plbRules2').textContent = [T.ruleProg[r.programOnly], T.ruleFull[r.allowFull], T.ruleSwap[r.swap]].join(' · ');
    const changed = changedRuleKeys().length;
    const badge = $('plbRulesChanged');
    badge.hidden = changed === 0;
    badge.textContent = changed ? T.rulesChanged(changed) : '';
  }
  const regulatoryMax = () => (state.student && finitePositive(state.student.regulatory_max_credits)) || 99;

  function fillRulesDialog(rules) {
    $('plbRuleMode').value = rules.mode;
    $('plbRuleElig').value = rules.eligibility;
    $('plbRuleMax').value = rules.maxCredits === null ? '' : String(rules.maxCredits);
    $('plbRuleProg').checked = rules.programOnly;
    $('plbRuleFull').checked = rules.allowFull;
    $('plbRuleSwap').checked = rules.swap;
    $('plbRuleMaxErr').hidden = true;
    $('plbRuleMaxErr').textContent = '';
  }
  function openRulesDialog() {
    const cap = state.student ? finitePositive(state.student.credit_cap) : null;
    const reg = state.student ? finitePositive(state.student.regulatory_max_credits) : null;
    $('plbRuleMaxHint').textContent = T.maxHint(cap, reg);
    fillRulesDialog(state.rules);
    const dialog = $('plbRulesDialog');
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    $('plbRuleMode').focus();
  }
  function closeRulesDialog() {
    const dialog = $('plbRulesDialog');
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  }
  function applyRulesDialog() {
    const raw = $('plbRuleMax').value.trim();
    let maxCredits = null;
    if (raw !== '') {
      const ceiling = regulatoryMax();
      maxCredits = /^\d{1,2}$/.test(raw) ? Number(raw) : NaN;
      if (!Number.isInteger(maxCredits) || maxCredits < 1 || maxCredits > ceiling) {
        const err = $('plbRuleMaxErr');
        err.textContent = T.maxError(ceiling);
        err.hidden = false;
        $('plbRuleMax').setAttribute('aria-invalid', 'true');
        $('plbRuleMax').focus();
        return;
      }
    }
    $('plbRuleMax').removeAttribute('aria-invalid');
    const before = { ...state.rules };
    state.rules = {
      mode: $('plbRuleMode').value === 'ignore' ? 'ignore' : 'keep',
      eligibility: $('plbRuleElig').value === 'relaxed' ? 'relaxed' : 'strict',
      maxCredits,
      programOnly: $('plbRuleProg').checked,
      allowFull: $('plbRuleFull').checked,
      swap: $('plbRuleSwap').checked,
    };
    closeRulesDialog();
    renderRules();
    // The other rules apply at once, whatever happens to the reload below.
    renderCourses();
    if (state.ctx && (state.rules.programOnly !== before.programOnly || state.rules.mode !== before.mode)) {
      loadCatalog(state.gen);
    }
    // The suggestions depend on eligibility: reload them, as the current page does. If no reload
    // lands, loadStudent puts back the eligibility the page was loaded with.
    if (state.ctx && state.rules.eligibility !== before.eligibility) loadStudent({ reuse: true });
  }

  /* ── Student ───────────────────────────────────────────────────────────── */
  function renderContextChip() {
    const chip = $('plbContext');
    if (!state.ctx) { chip.hidden = true; return; }
    chip.replaceChildren(bdi(`${state.ctx.studentId} · ${state.ctx.year}/${state.ctx.term}`));
    chip.hidden = false;
  }
  function stat(value, label, sub) {
    return el('div', 'plb-stat', el('b', '', value), el('span', '', label), sub ? el('span', '', sub) : null);
  }
  function renderStudent() {
    const host = $('plbStudent');
    const st = state.student;
    if (!st) {
      host.replaceChildren(el('p', 'plb-empty', el('strong', '', T.noStudentTitle), el('br'), T.noStudentHint));
      return;
    }
    const gpa = finiteNonNegative(st.gpa);
    const registered = finiteNonNegative(st.registered_credits);
    const cap = finitePositive(st.credit_cap);
    const reg = finitePositive(st.regulatory_max_credits);
    host.replaceChildren(
      el('div', 'plb-who',
        el('div', 'plb-sn', st.name ? String(st.name) : T.unnamed),
        el('div', 'plb-smeta', `${T.idLabel} `, bdi(st.student_id), st.program ? ' · ' : null, st.program ? bdi(st.program) : null)),
      el('div', 'plb-stats',
        stat(gpa === null ? '—' : gpa.toFixed(2), T.gpa),
        stat(registered === null ? '—' : String(registered), T.registeredCredits),
        stat(cap === null ? '—' : String(cap), T.creditCap, cap === null ? null : T.advisory(reg && reg > cap ? reg : null))),
    );
  }

  /* ── The registered week: model, tab facts and grid ────────────────────── */
  function registeredCourses() {
    const seen = new Map();
    for (const row of state.baseline) {
      const key = `${courseKeyOf(row)}|${String(row.section || '').trim()}`;
      if (!courseKeyOf(row)) continue;
      if (!seen.has(key)) {
        seen.set(key, {
          key: courseKeyOf(row),
          code: String(row.course_code || row.course_key || ''),
          name: String(row.course_name || ''),
          section: String(row.section || '').trim(),
          credits: finiteNonNegative(row.credits),
          timed: false,
        });
      }
      const entry = seen.get(key);
      if (entry.credits === null) entry.credits = finiteNonNegative(row.credits);
      if (normDay(row.day) && toMins(row.start_time) !== null && toMins(row.end_time) !== null) entry.timed = true;
    }
    return [...seen.values()];
  }
  function weekModel() {
    const meetings = [];
    for (const row of state.baseline) {
      const day = normDay(row.day);
      const st = toMins(row.start_time);
      const en = toMins(row.end_time);
      if (!day || st === null || en === null || en <= st) continue;
      meetings.push({
        day, st, en,
        code: String(row.course_code || row.course_key || ''),
        section: String(row.section || '').trim(),
        key: courseKeyOf(row),
      });
    }
    const days = ACADEMIC_DAYS.slice();
    for (const extra of ['FRI', 'SAT']) if (meetings.some((m) => m.day === extra)) days.push(extra);
    const byDay = {};
    for (const day of days) byDay[day] = meetings.filter((m) => m.day === day).sort((a, b) => a.st - b.st || a.en - b.en);
    const starts = meetings.map((m) => m.st);
    const ends = meetings.map((m) => m.en);
    const startMin = Math.min(480, starts.length ? Math.floor(Math.min(...starts) / 60) * 60 : 480);
    const endMin = Math.max(900, ends.length ? Math.ceil(Math.max(...ends) / 60) * 60 : 900);
    // The longest wait between two classes on one day.
    let longestGap = 0;
    for (const day of days) {
      let reach = null;
      for (const m of byDay[day]) {
        if (reach !== null && m.st > reach) longestGap = Math.max(longestGap, m.st - reach);
        reach = reach === null ? m.en : Math.max(reach, m.en);
      }
    }
    return {
      meetings, days, byDay, startMin, endMin, longestGap,
      lastEnd: ends.length ? Math.max(...ends) : null,
      campusDays: days.filter((d) => byDay[d].length).length,
      freeDays: ACADEMIC_DAYS.filter((d) => !byDay[d].length),
    };
  }
  function renderTab(model) {
    const credits = registeredCredits();
    $('plbTabCredits').textContent = credits === null || !state.ctx ? '' : T.credits(credits);
    const facts1 = $('plbTabFacts1');
    const facts2 = $('plbTabFacts2');
    if (!state.ctx) { facts1.textContent = '—'; facts2.textContent = ''; return; }
    if (!model.meetings.length) { facts1.textContent = T.noTimedClasses; facts2.textContent = ''; return; }
    let free;
    if (model.freeDays.length === 0) free = T.noFreeDay;
    else if (model.freeDays.length === 1) free = T.freeOne(IS_AR ? DAY_FULL_AR[model.freeDays[0]] : DAY_SHORT[model.freeDays[0]]);
    else free = T.freeDays(model.freeDays.length);
    facts1.textContent = `${T.dayWord(model.campusDays)} · ${free}`;
    facts2.replaceChildren(
      `${T.ends} `, bdi(hhmm(model.lastEnd)), ' · ',
      model.longestGap > 0 ? `${T.gap} ${durationText(model.longestGap)}` : T.noGap);
  }
  function weekBlock(m, rowStart, rowEnd, col) {
    const rows = rowEnd - rowStart;
    const range = `${hhmm(m.st)}-${hhmm(m.en)}`;
    const block = el('div', 'plb-b');
    block.style.gridColumn = String(col);
    block.style.gridRow = `${rowStart} / ${rowEnd}`;
    block.dataset.code = m.key;
    const label = `${m.code}${m.section ? ` ${IS_AR ? 'شعبة' : 'section'} ${m.section}` : ''}, ${DAY_SHORT[m.day]} ${range}`;
    block.setAttribute('role', 'img');
    block.setAttribute('aria-label', label);
    block.append(
      el('span', 'plb-cd', bdi(m.code)),
      m.section ? el('span', 'plb-bs', bdi(`§${m.section}`)) : null,
      el('span', 'plb-bt', bdi(range)));
    return block;
  }
  function renderWeek() {
    const host = $('plbWeek');
    const model = weekModel();
    renderTab(model);
    if (!state.ctx) { host.replaceChildren(el('p', 'plb-empty', T.loadFirstWeek)); return; }
    const untimed = registeredCourses().filter((c) => !c.timed).length;
    const note = untimed ? el('p', 'plb-note', T.untimed(untimed)) : null;
    if (!model.meetings.length) {
      host.replaceChildren(...[el('p', 'plb-empty', T.noClassesAtAll), note].filter(Boolean));
      return;
    }
    const rows = (model.endMin - model.startMin) / 15;
    const footRow = rows + 2;
    const grid = el('div', 'plb-wk');
    grid.style.setProperty('--plb-days', String(model.days.length));
    grid.style.setProperty('--plb-rows', String(rows));
    model.days.forEach((day, i) => {
      const head = el('div', 'plb-dh', DAY_SHORT[day]);
      head.style.gridColumn = String(i + 2);
      head.style.gridRow = '1';
      grid.append(head);
      const line = el('div', 'plb-vl');
      line.style.gridColumn = String(i + 2);
      grid.append(line);
    });
    for (let mins = model.startMin; mins <= model.endMin; mins += 60) {
      const row = 2 + (mins - model.startMin) / 15;
      const rule = el('div', 'plb-hl');
      rule.style.gridRow = String(row);
      grid.append(rule);
      if (mins < model.endMin) {
        const label = el('div', 'plb-tm', bdi(hhmm(mins)));
        label.style.gridRow = String(row);
        grid.append(label);
      }
    }
    model.days.forEach((day, i) => {
      const list = model.byDay[day];
      if (ACADEMIC_DAYS.includes(day) && !list.length) {
        const wash = el('div', 'plb-free', T.free);
        wash.style.gridColumn = String(i + 2);
        wash.style.gridRow = `2 / ${footRow}`;
        grid.append(wash);
      }
      for (const m of list) {
        const rowStart = 2 + Math.floor((m.st - model.startMin) / 15);
        const rowEnd = Math.max(rowStart + 1, 2 + Math.ceil((m.en - model.startMin) / 15));
        grid.append(weekBlock(m, rowStart, rowEnd, i + 2));
      }
      const foot = list.length
        ? el('div', 'plb-df', el('b', '', T.classWord(list.length)),
          el('span', '', `${T.ends} `, bdi(hhmm(Math.max(...list.map((m) => m.en))))))
        : el('div', 'plb-df', el('b', '', T.noClasses), el('span', '', T.dayOff));
      foot.style.gridColumn = String(i + 2);
      foot.style.gridRow = String(footRow);
      grid.append(foot);
    });
    host.replaceChildren(...[el('div', 'plb-wkwrap', grid), note].filter(Boolean));
  }

  /* ── Courses ───────────────────────────────────────────────────────────── */
  function registeredCredits() {
    const list = registeredCourses();
    const known = list.filter((c) => c.credits !== null);
    return known.length ? known.reduce((sum, c) => sum + c.credits, 0) : null;
  }
  const inShortlist = (code) => state.shortlist.some((c) => norm(c.course_code) === norm(code));
  function planPool() {
    if (!state.planCourses) return [];
    // Kept registered courses are fixed; when they are ignored they can be planned again.
    const includeStudying = state.rules.mode === 'ignore';
    const registeredKeys = new Set(includeStudying ? [] : registeredCourses().map((c) => c.key));
    return state.planCourses
      .filter((c) => !inShortlist(c.code) && !registeredKeys.has(c.code))
      .filter((c) => (c.status === 'not_taken' && c.canRegister) || (includeStudying && c.status === 'studying'))
      .sort((a, b) => b.importance - a.importance);
  }
  function blockedRecs() {
    return state.recs.filter((r) => String(r.status) !== 'Eligible' && !inShortlist(r.course_code));
  }
  function shortlistFromRecs(recs) {
    return recs
      .filter((r) => r.status === 'Eligible')
      .map((r) => ({
        course_code: String(r.course_code || ''), course_name: String(r.course_name || ''),
        credits: Number(r.credits) || 0, priority: r.priority || 'Med', status: 'Eligible',
        missing_prerequisites: [], must_take: false, score: Number(r.score) || 0,
      }));
  }
  function itemFromPlan(course) {
    return {
      course_code: course.code, course_name: course.name, credits: course.credits,
      priority: 'Med', status: 'Eligible', missing_prerequisites: [], must_take: false,
      score: Math.round(course.importance * 20),
    };
  }
  function planCredits() {
    return state.shortlist.reduce((sum, c) => sum + (Number(c.credits) || 0), 0);
  }
  function usedCredits() {
    const registered = state.rules.mode === 'keep' ? registeredCredits() : 0;
    return (registered || 0) + planCredits();
  }
  function sectionInfo(code) {
    if (!state.catalog) return T.unknown;
    const sections = state.catalog.get(norm(code)) || [];
    if (!sections.length) return T.noSections;
    const caps = sections.map(sectionCapacity);
    if (caps.every((c) => c.known && c.full)) return T.allFull;
    if (sections.length === 1) {
      const days = [...new Set((sections[0].meetings || []).map((m) => normDay(m.day)).filter(Boolean))];
      const left = caps[0].known && caps[0].remaining < SEAT_WARNING ? caps[0].remaining : null;
      const parts = [];
      if (days.length) parts.push(days.map((d) => DAY_SHORT[d]).join(' '));
      if (left !== null) parts.push(T.seatsLeft(left));
      return parts.length ? parts.join(' · ') : T.sectionWord(1);
    }
    return T.sectionWord(sections.length);
  }
  function iconButton(icon, label, act, code, extra) {
    const button = el('button', `plb-ib ${extra || ''}`.trim(), svg(ICONS[icon]));
    button.type = 'button';
    button.setAttribute('aria-label', label);
    button.title = label;
    button.dataset.act = act;
    button.dataset.code = norm(code);
    return button;
  }
  function groupBody(name, left, right) {
    const body = el('tbody', 'plb-group');
    body.dataset.group = name;
    body.append(el('tr', 'plb-grp', el('th', 'plb-mic', left), el('td', 'plb-mic plb-grp-end', right || '')));
    body.firstChild.firstChild.colSpan = 3;
    body.firstChild.lastChild.colSpan = 2;
    body.firstChild.firstChild.scope = 'rowgroup';
    return body;
  }
  function emptyRow(text) {
    const row = el('tr', 'plb-tr plb-tr--note', el('td', 'plb-empty-cell', text));
    row.firstChild.colSpan = 5;
    return row;
  }
  function courseRow(code, name, cells) {
    const row = el('tr', 'plb-tr');
    row.dataset.code = norm(code);
    row.append(el('td', 'plb-code', bdi(code)), el('td', 'plb-name', name ? isolate(name) : ''), ...cells);
    return row;
  }
  function blockedReason(rec) {
    const missing = (rec.missing_prerequisites || []).map(String).filter(Boolean);
    const cell = el('td', 'plb-sc plb-sc--blocked', el('span', 'plb-chip', T.blocked));
    cell.colSpan = 3;
    if (!missing.length) { cell.append(' ', T.prereqNotMet); return cell; }
    cell.append(' ', `${T.missing} `);
    missing.forEach((m, i) => { if (i) cell.append(', '); cell.append(bdi(m)); });
    if (missing.every((m) => inShortlist(m))) cell.append(` ${T.inThisPlan}`);
    return cell;
  }
  function renderCourses() {
    const table = $('plbTable');
    table.querySelectorAll('tbody').forEach((n) => n.remove());
    const addInput = $('plbAdd');
    addInput.disabled = !state.ctx;
    if (!state.ctx) {
      const body = el('tbody', 'plb-group');
      body.append(emptyRow(T.loadFirstCourses));
      table.append(body);
      renderCreditsHeader();
      state.focus = null;
      return;
    }
    const keep = state.rules.mode === 'keep';

    // Registered
    const registered = registeredCourses();
    const regCredits = registeredCredits();
    const regBody = groupBody('registered', keep ? T.registeredFixed : T.registeredIgnored,
      regCredits === null ? '' : T.credits(regCredits));
    if (!registered.length) regBody.append(emptyRow(T.noRegistered));
    for (const course of registered) {
      const lock = el('td', 'plb-act');
      const icon = el('span', 'plb-lock', svg(ICONS.lock, 14));
      icon.setAttribute('role', 'img');
      icon.setAttribute('aria-label', T.registeredLock);
      lock.append(icon);
      regBody.append(courseRow(course.code, course.name, [
        el('td', 'plb-sc', course.section ? bdi(`§${course.section}`) : T.unknown), el('td'), lock]));
    }
    table.append(regBody);

    // New in this plan
    const newBody = groupBody('new', `${T.newInPlan} · ${T.credits(planCredits())}`, T.mustTake);
    if (!state.shortlist.length) newBody.append(emptyRow(T.nothingAdded));
    for (const item of state.shortlist) {
      const code = item.course_code;
      const pin = item.pinned_sections && item.pinned_sections.length ? item.pinned_sections[0] : null;
      const sectionCell = el('td', 'plb-sc');
      if (pin && pin.section) sectionCell.append(`${T.pinned} `, bdi(`§${pin.section}`));
      else sectionCell.append(T.anySection);
      const box = el('input', 'plb-tick');
      box.type = 'checkbox';
      box.checked = !!item.must_take;
      box.setAttribute('aria-label', T.mustTakeFor(code));
      box.dataset.act = 'must';
      box.dataset.code = norm(code);
      const remove = iconButton('x', T.removeCourse(code), 'remove', code);
      newBody.append(courseRow(code, item.course_name, [
        sectionCell, el('td', 'plb-act', el('label', 'plb-tickwrap', box)), el('td', 'plb-act', remove)]));
    }
    table.append(newBody);

    // Could be added
    const pool = planPool();
    const cap = state.rules.maxCredits;
    const free = cap === null ? null : cap - usedCredits();
    const couldLeft = free === null ? '' : (free >= 0 ? `${free} ${T.creditUnit(free)} ${T.free2}` : '');
    if (state.planState === 'loading') {
      const body = groupBody('could', T.couldAdd, '');
      body.append(emptyRow(T.loadingPlan));
      table.append(body);
    } else if (pool.length) {
      const body = groupBody('could', T.couldAdd, couldLeft);
      for (const course of pool) {
        const add = iconButton('plus', T.addCourse(course.code), 'add', course.code, 'plb-ib--add');
        body.append(courseRow(course.code, course.name, [
          el('td', 'plb-sc', sectionInfo(course.code)), el('td'), el('td', 'plb-act', add)]));
      }
      table.append(body);
    }

    // Blocked
    const blocked = blockedRecs();
    if (blocked.length) {
      const body = groupBody('blocked', T.blocked, '');
      for (const rec of blocked) {
        const row = el('tr', 'plb-tr');
        row.dataset.code = norm(rec.course_code);
        row.append(el('td', 'plb-code', bdi(rec.course_code)), el('td', 'plb-name plb-name--off', rec.course_name || ''), blockedReason(rec));
        body.append(row);
      }
      table.append(body);
    }
    renderCreditsHeader();
    restoreFocus();
  }
  function renderCreditsHeader() {
    const header = $('plbCredits');
    delete header.dataset.over;                // "over" is only ever said against a known cap
    if (!state.ctx) { header.textContent = ''; return; }
    const used = usedCredits();
    const cap = state.rules.maxCredits;
    if (cap === null) { header.textContent = T.credits(used); return; }
    const free = cap - used;
    const lead = IS_AR ? `${used} من ${cap} ${T.creditUnit(cap)}` : `${used} of ${cap} ${T.creditUnit(cap)}`;
    const tail = free >= 0 ? `${free} ${T.free2}` : `${-free} ${T.over}`;
    header.textContent = `${lead} · ${tail}`;
    header.dataset.over = free < 0 ? 'true' : 'false';
  }
  function restoreFocus() {
    const want = state.focus;
    state.focus = null;
    if (!want) return;
    const code = norm(want.code);
    let target = document.querySelector(`#plbTable [data-act="${want.kind}"][data-code="${CSS.escape(code)}"]`);
    if (!target) target = $('plbAdd');
    if (target && !target.disabled) target.focus();
  }
  function addCourseToPlan(item) {
    if (inShortlist(item.course_code)) return;
    state.shortlist.push(item);
    state.focus = { kind: 'remove', code: item.course_code };
    setStatus('ok', ...T.added(item.course_code));
    renderCourses();
  }
  function removeCourseFromPlan(code) {
    const at = state.shortlist.findIndex((c) => norm(c.course_code) === norm(code));
    if (at < 0) return;
    const [gone] = state.shortlist.splice(at, 1);
    state.focus = { kind: 'add', code: gone.course_code };
    setStatus('ok', ...T.removed(gone.course_code));
    renderCourses();
  }

  /* ── "Add a course" ────────────────────────────────────────────────────── */
  let activeOption = -1;
  function closeList() {
    $('plbAddList').hidden = true;
    $('plbAdd').setAttribute('aria-expanded', 'false');
    $('plbAdd').removeAttribute('aria-activedescendant');
    activeOption = -1;
  }
  function matches(query) {
    const needle = query.trim();
    if (!needle) return [];
    const code = norm(needle);
    const lower = needle.toLowerCase();
    return planPool().filter((c) => c.code.includes(code) || c.name.toLowerCase().includes(lower)).slice(0, 8);
  }
  function renderList() {
    const query = $('plbAdd').value;
    const list = $('plbAddList');
    if (!query.trim() || !state.ctx) { closeList(); return; }
    const found = matches(query);
    list.replaceChildren();
    if (!found.length) {
      list.append(el('li', 'plb-opt plb-opt--none', T.noMatch));
    }
    found.forEach((course, i) => {
      const item = el('li', 'plb-opt', bdi(course.code), ' ', course.name, ' · ', T.credits(course.credits));
      item.id = `plbOpt${i}`;
      item.setAttribute('role', 'option');
      item.dataset.code = course.code;
      list.append(item);
    });
    list.hidden = false;
    $('plbAdd').setAttribute('aria-expanded', 'true');
    activeOption = -1;
  }
  function pickOption(index) {
    const nodes = [...$('plbAddList').querySelectorAll('[role="option"]')];
    const node = nodes[index];
    if (!node) return;
    const course = planPool().find((c) => c.code === node.dataset.code);
    $('plbAdd').value = '';
    closeList();
    if (course) addCourseToPlan(itemFromPlan(course));
  }
  function highlight(index) {
    const nodes = [...$('plbAddList').querySelectorAll('[role="option"]')];
    nodes.forEach((n, i) => n.setAttribute('aria-selected', i === index ? 'true' : 'false'));
    activeOption = index;
    if (nodes[index]) $('plbAdd').setAttribute('aria-activedescendant', nodes[index].id);
    else $('plbAdd').removeAttribute('aria-activedescendant');
  }

  /* ── Data: plan and section catalogue ──────────────────────────────────── */
  async function postJson(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (err) { data = null; }
    return { response, data };
  }
  async function loadPlan(run) {
    state.planState = 'loading';
    state.planCourses = null;
    renderCourses();
    try {
      const url = `/report/student-plan/?student_id=${encodeURIComponent(state.ctx.studentId)}`
        + `&eligibility_mode=${encodeURIComponent(state.rules.eligibility)}`;
      const response = await fetch(url);
      const data = await response.json();
      if (run !== state.gen) return;
      if (!response.ok || !data || data.error) throw new Error('plan');
      const courses = [];
      for (const term of data.terms || []) {
        for (const c of term.courses || []) {
          const code = norm(c.course_code);
          if (!code) continue;
          courses.push({
            code, name: String(c.course_name || c.description || ''),
            credits: Number(c.credit_hours) || 0, importance: Number(c.importance_score) || 0,
            status: String(c.status || ''), canRegister: !!c.can_register,
          });
        }
      }
      state.planCourses = courses;
      state.planState = 'ready';
    } catch (err) {
      if (run !== state.gen) return;
      state.planCourses = [];
      state.planState = 'error';
      setStatus('warn', T.planFailed);
    }
    renderCourses();
    loadCatalog(run);
  }
  async function loadCatalog(run) {
    if (!state.ctx) return;
    const mine = ++state.catGen;
    const codes = [...new Set([...planPool().map((c) => c.code), ...state.shortlist.map((c) => norm(c.course_code))])];
    if (!codes.length) { state.catalog = new Map(); renderCourses(); return; }
    let result = null;
    try {
      result = await postJson('/ops/planner/sections-catalog/', {
        student_id: state.ctx.studentId, academic_year: state.ctx.year, term: state.ctx.term,
        course_codes: codes, program_sections_only: state.rules.programOnly,
      });
    } catch (err) { result = null; }
    if (run !== state.gen || mine !== state.catGen) return;
    if (!result || !result.response.ok || !result.data || !Array.isArray(result.data.sections)) {
      state.catalog = null;
      setStatus('warn', T.sectionsFailed);
    } else {
      const map = new Map();
      for (const section of result.data.sections) {
        const key = courseKeyOf(section);
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(section);
      }
      state.catalog = map;
    }
    renderCourses();
  }

  /* ── Loading a student ─────────────────────────────────────────────────── */
  async function loadStudent(options) {
    const reuse = !!(options && options.reuse && state.ctx);
    const studentId = reuse ? state.ctx.studentId : $('plbStudentId').value.trim();
    const year = reuse ? state.ctx.year : $('plbYear').value.trim();
    const term = reuse ? state.ctx.term : $('plbTerm').value;
    if (!/^\d+$/.test(studentId)) { setStatus('error', T.enterStudent); return; }
    if (!/^\d{4}$/.test(year)) { setStatus('error', T.enterYear); return; }

    const mine = ++state.ctxGen;
    const failed = (line) => {
      $('plbRoot').removeAttribute('aria-busy');
      setStatus('error', line);
      // The page still shows what the last accepted reply brought, so the rule goes back to
      // match it. This also covers a rules reload that a newer, failing fetch overtook.
      if (state.ctx && state.shownElig && state.rules.eligibility !== state.shownElig) {
        state.rules.eligibility = state.shownElig;
        renderRules();
      }
    };
    $('plbRoot').setAttribute('aria-busy', 'true');
    setStatus('loading', T.loading);
    let result;
    try {
      result = await postJson('/ops/planner/context/', {
        student_id: studentId, academic_year: year, term, eligibility_mode: state.rules.eligibility,
      });
    } catch (err) {
      if (mine === state.ctxGen) failed(T.failed);  // otherwise a newer request owns the page
      return;
    }
    if (mine !== state.ctxGen) return;         // everything below writes page state
    const { response, data } = result;
    if (!response.ok || !data || data.error || !data.student) {
      const code = data && data.error && data.error.code;
      failed(response.status === 404 || code === 'STUDENT_NOT_FOUND' ? T.notFound : T.failed);
      return;
    }
    $('plbRoot').removeAttribute('aria-busy');
    // Only an accepted student retires the plan and section requests of the one on screen. A
    // fetch that failed above leaves them alive, or their groups would say "loading" for good.
    const run = ++state.gen;

    const previousId = state.student ? String(state.student.student_id) : null;
    state.ctx = { studentId: String(data.student.student_id || studentId), year: String(data.year || year), term: String(data.term || term) };
    state.student = data.student;
    state.baseline = Array.isArray(data.baseline) ? data.baseline : [];
    state.recs = Array.isArray(data.recommendations) ? data.recommendations : [];
    if (data.eligibility_mode === 'strict' || data.eligibility_mode === 'relaxed') {
      state.rules.eligibility = data.eligibility_mode;
    }
    state.shownElig = state.rules.eligibility;
    // Max credits starts at the student's cap, and is kept while the same student is reloaded
    // (an eligibility change reloads the student and must not undo the number just chosen).
    const cap = finitePositive(data.student.credit_cap);
    state.defaults.maxCredits = cap;
    if (previousId !== String(data.student.student_id)) state.rules.maxCredits = cap;
    state.shortlist = shortlistFromRecs(state.recs);
    state.planCourses = null;
    state.catalog = null;
    $('plbRoot').dataset.loaded = 'true';
    $('plbYear').value = state.ctx.year;
    if ([...$('plbTerm').options].some((o) => o.value === state.ctx.term)) $('plbTerm').value = state.ctx.term;
    setStatus('ok', T.loaded);
    renderContextChip();
    renderStudent();
    renderRules();
    renderWeek();
    renderCourses();
    loadPlan(run);
  }

  /* ── Wiring ────────────────────────────────────────────────────────────── */
  function wire() {
    $('plbLoad').addEventListener('submit', (event) => { event.preventDefault(); loadStudent(); });
    $('plbEditRules').addEventListener('click', openRulesDialog);
    $('plbRulesCancel').addEventListener('click', closeRulesDialog);
    $('plbRulesReset').addEventListener('click', () => fillRulesDialog(state.defaults));
    $('plbRulesForm').addEventListener('submit', (event) => { event.preventDefault(); applyRulesDialog(); });

    $('plbTable').addEventListener('click', (event) => {
      const target = event.target.closest('[data-act]');
      if (!target || target.tagName !== 'BUTTON') return;
      const code = target.dataset.code;
      if (target.dataset.act === 'remove') removeCourseFromPlan(code);
      else if (target.dataset.act === 'add') {
        const course = planPool().find((c) => c.code === code);
        if (course) addCourseToPlan(itemFromPlan(course));
      }
    });
    $('plbTable').addEventListener('change', (event) => {
      const target = event.target;
      if (!target || target.dataset.act !== 'must') return;
      const item = state.shortlist.find((c) => norm(c.course_code) === target.dataset.code);
      if (item) item.must_take = target.checked;
    });

    const add = $('plbAdd');
    add.addEventListener('input', renderList);
    add.addEventListener('keydown', (event) => {
      const options = $('plbAddList').querySelectorAll('[role="option"]').length;
      if (event.key === 'ArrowDown' && options) { event.preventDefault(); highlight((activeOption + 1) % options); }
      else if (event.key === 'ArrowUp' && options) { event.preventDefault(); highlight((activeOption - 1 + options) % options); }
      else if (event.key === 'Enter') {
        if (options) { event.preventDefault(); pickOption(activeOption < 0 ? 0 : activeOption); }
      } else if (event.key === 'Escape') { closeList(); }
    });
    $('plbAddList').addEventListener('mousedown', (event) => {
      const node = event.target.closest('[role="option"]');
      if (!node) return;
      event.preventDefault();
      pickOption([...$('plbAddList').querySelectorAll('[role="option"]')].indexOf(node));
    });
    add.addEventListener('blur', closeList);
  }

  function init() {
    const root = $('plbRoot');
    if (!root) return;
    const term = root.dataset.defaultTerm;
    if ([...$('plbTerm').options].some((o) => o.value === term)) $('plbTerm').value = term;
    wire();
    renderRules();
    renderStudent();
    renderWeek();
    renderCourses();
    // /planner/?student=123456&year=1448&term=1 opens a student, as the current page does.
    const params = new URLSearchParams(window.location.search);
    const student = params.get('student') || params.get('student_id') || params.get('sid');
    if (student) {
      $('plbStudentId').value = student;
      const year = params.get('year') || params.get('academic_year');
      const paramTerm = params.get('term');
      if (year) $('plbYear').value = year;
      if (paramTerm && [...$('plbTerm').options].some((o) => o.value === paramTerm)) $('plbTerm').value = paramTerm;
      loadStudent();
    }
  }
  init();
})();
