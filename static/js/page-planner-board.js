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
    // Build, options and apply.
    buildFirst: IS_AR ? 'ابنِ الخيارات' : 'Build options',
    rebuild: IS_AR ? 'أعد البناء' : 'Rebuild',
    building: IS_AR ? 'جارٍ بناء الخيارات… قد يستغرق ذلك حتى 10 ثوانٍ' : 'Building options… this can take up to 10 seconds',
    planChanged: IS_AR ? 'تغيّرت الخطة. ابنِ الخيارات مرة أخرى لعرضها.' : 'The plan changed. Build again to see options.',
    buildFailed: IS_AR ? 'تعذّر بناء الخيارات. حاول مرة أخرى.' : 'The build failed. Try again.',
    tooMany: IS_AR ? 'طلبات بناء كثيرة. حاول بعد لحظات.' : 'Too many builds. Try again in a moment.',
    sessionExpired: IS_AR ? 'انتهت جلستك. سجّل الدخول مرة أخرى ثم أعد المحاولة.' : 'Your session has expired. Sign in again, then retry.',
    requestFailed: IS_AR ? 'فشل الطلب' : 'Request failed',
    noOption: IS_AR ? 'لم يُعثر على خيار يحقق القيود الحالية.' : 'No option could be built under the current constraints.',
    noFeasible: IS_AR
      ? 'لا توجد نتيجة صالحة تحقق جميع المقررات الإلزامية والشُعب المثبّتة ضمن القيود الحالية.'
      : 'No valid result satisfies every must-take course and exact pinned section under the current constraints.',
    failMust: IS_AR ? 'تعذر إدراج المقرر الإلزامي ضمن القيود الحالية.' : 'The must-take course could not be placed under the current constraints.',
    failPinned: IS_AR ? 'تعذر استخدام الشعبة المثبّتة بالضبط.' : 'The exact pinned section could not be used.',
    failCredits: IS_AR ? 'يتعارض حد الساعات مع القيود المطلوبة.' : 'The credit limit conflicts with the required constraints.',
    notScheduledList: (codes) => [IS_AR ? 'غير مجدولة: ' : 'Not scheduled: ', ...codes.flatMap((c, i) => (i ? [', ', bdi(c)] : [bdi(c)]))],
    reason: {
      DID_NOT_FIT: IS_AR ? 'تعذّر إدراجه ضمن حد الساعات، أو أن شُعبه متعارضة' : 'Could not fit within the credit limit, or its sections clash',
      ALL_SECTIONS_CLASH: IS_AR ? 'كل الشُعب تتعارض مع الخطة المختارة' : 'Every section clashes with the chosen plan',
      NOT_ON_FILE: IS_AR ? 'لا توجد شعبة مطروحة لهذا الطالب هذا الفصل' : 'No section is offered to this student this term',
      MEETING_DATA_INCOMPLETE: IS_AR ? 'بيانات لقاءات الشعبة غير مكتملة' : 'Section meeting data is incomplete',
      SEARCH_BUDGET_EXHAUSTED: IS_AR ? 'انتهى وقت البحث قبل إدراجه' : 'The search ran out of time before placing it',
      SOLVER_MODEL_INVALID: IS_AR ? 'رفض المُحلّل هذا البناء' : 'The solver rejected this build',
    },
    noReason: IS_AR ? 'سبب غير محدد' : 'no reason given',
    lastBuild: IS_AR ? 'آخر بناء' : 'Last build',
    moreOptions: (n) => (IS_AR ? `خيارات أخرى (${n})` : `More options (${n})`),
    moreLabel: IS_AR ? 'خيارات أخرى' : 'More options',
    optionName: (name) => (IS_AR ? ['الخيار ', bdi(name)] : ['Option ', bdi(name)]),
    planned: IS_AR ? 'المخطَّط' : 'Planned',
    plannedBlock: IS_AR ? 'مخطَّط' : 'planned',
    clashBlock: IS_AR ? 'يتعارض مع مقرر مسجّل' : 'clashes with a registered class',
    clashNote: (n) => (IS_AR
      ? `${pluralAr(n, forms('لقاء مخطَّط واحد يتعارض', 'لقاءان مخطَّطان يتعارضان', '{n} لقاءات مخطَّطة تتعارض', '{n} لقاءً مخطَّطًا يتعارض'))} مع لقاء مسجّل.`
      : `${n} planned ${n === 1 ? 'class overlaps' : 'classes overlap'} a registered class.`),
    notScheduled: (n) => (IS_AR
      ? pluralAr(n, forms('مقرر واحد غير مجدول', 'مقرران غير مجدولين', '{n} مقررات غير مجدولة', '{n} مقررًا غير مجدول'))
      : `${n} not scheduled`),
    notScheduledHere: IS_AR ? 'غير مجدول في هذا الخيار' : 'not scheduled in this option',
    review: IS_AR ? 'مراجعة وتطبيق' : 'Review and apply',
    comparisonOnly: IS_AR
      ? 'للمقارنة فقط: تتضمن الخيارات شُعب برامج أخرى'
      : "Comparison only: other programmes' sections are included",
    nothingToSave: IS_AR ? 'لا شُعب في هذا الخيار للحفظ' : 'This option has no sections to save',
    safe: IS_AR ? 'لا يُحفظ شيء ولا يُحجز أي مقعد حتى تضغط تطبيق.' : 'Nothing is saved and no seat is held until you apply.',
    keptRegistered: (n) => (IS_AR
      ? `المقررات المسجّلة تبقى كما هي (${n}). لا يغيّرها هذا الحفظ.`
      : `Registered courses stay as they are (${n}). This save does not change them.`),
    droppedRegistered: IS_AR
      ? 'تم تجاهل المقررات المسجّلة عند بناء هذا الخيار. لا يغيّرها هذا الحفظ، وقد تتعارض معه الشُعب المحفوظة.'
      : 'Registered courses were ignored when this option was built. This save does not change them, so the saved sections may clash with them.',
    replaceWarn: IS_AR
      ? 'يستبدل هذا جدول الطالب المخطَّط المحفوظ لهذا الفصل. تُزال أي شُعب مخططة محفوظة سابقًا غير موجودة في هذا الخيار.'
      : "This replaces the student's saved planned sections for this term. Any previously saved planned section that is not in this option is removed.",
    apply: IS_AR ? 'تطبيق' : 'Apply',
    cancel: IS_AR ? 'إلغاء' : 'Cancel',
    saving: IS_AR ? 'جارٍ الحفظ…' : 'Saving…',
    saved: IS_AR ? 'تم الحفظ' : 'Saved',
    saveFailed: IS_AR ? 'فشل حفظ الشُعب. لم يُحفظ شيء.' : 'The sections could not be saved. Nothing was saved.',
    savedNoReload: IS_AR ? 'تم الحفظ، لكن تعذّرت إعادة تحميل الطالب. اضغط جلب.' : 'Saved, but the student could not be reloaded. Press Fetch.',
    removedPlanned: (n) => (IS_AR
      ? pluralAr(n, forms('أُزيل مقرر مخطط محفوظ سابقًا', 'أُزيل مقرران مخططان محفوظان سابقًا', 'أُزيلت {n} مقررات مخططة محفوظة سابقًا', 'أُزيل {n} مقررًا مخططًا محفوظًا سابقًا'))
      : `${n} previously saved planned ${n === 1 ? 'course was' : 'courses were'} removed`),
    clashSaved: (n) => (IS_AR
      ? pluralAr(n, forms('يتعارض لقاء واحد مع الجدول المسجّل', 'يتعارض لقاءان مع الجدول المسجّل', 'تتعارض {n} لقاءات مع الجدول المسجّل', 'يتعارض {n} لقاءً مع الجدول المسجّل'))
      : `${n} ${n === 1 ? 'class clashes' : 'classes clash'} with the registered timetable`),
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
    // The build loop. `build` is null until a reply is accepted, and again whenever the plan or
    // the rules it was built from change: {options, shortlist, rules, at, ...}.
    build: null,
    selected: null,    // name of the option tab on screen; null = the Registered tab
    buildGen: 0,       // bumped whenever a reply in flight must no longer be believed
    inflight: 0,       // build requests not yet answered: the button waits for all of them
    applying: false,   // a save request is in flight
    moreOpen: false,   // the "More options" disclosure
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
    // Options built under other rules are not options for these.
    if (Object.keys(DEFAULT_RULES).some((key) => state.rules[key] !== before[key])) dropBuild(true);
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
  function registeredMeetings() {
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
        kind: 'reg',
      });
    }
    return meetings;
  }
  // The meetings an option's mappings add; a mapping with no usable time adds nothing.
  function optionMeetings(opt) {
    const meetings = [];
    for (const mapping of opt.mappings || []) {
      const key = courseKeyOf(mapping);
      for (const mm of mapping.meetings || []) {
        const day = normDay(mm.day);
        const st = toMins(mm.start_time);
        const en = toMins(mm.end_time);
        if (!day || st === null || en === null || en <= st) continue;
        meetings.push({
          day, st, en,
          code: key || String(mapping.course_code || ''),
          section: String(mapping.section || '').trim(),
          key,
          kind: 'new',
        });
      }
    }
    return meetings;
  }
  // What the student's week holds under an option: the registered classes (unless this build
  // ignored them) and the option's own, a planned class marked when it overlaps a registered one.
  function weekMeetings(opt) {
    const keepRegistered = !opt || state.build.rules.mode === 'keep';
    const reg = keepRegistered ? registeredMeetings() : [];
    const added = opt ? optionMeetings(opt) : [];
    for (const m of added) {
      for (const r of reg) {
        if (r.day === m.day && m.st < r.en && r.st < m.en) { m.clash = true; r.clash = true; }
      }
    }
    return [...reg, ...added];
  }
  function weekModel(meetings) {
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
  // The three lines a tab shows about a week: how many days, which day is free, when it ends.
  function factsOf(model) {
    if (!model.meetings.length) return { first: T.noTimedClasses, second: [] };
    let free;
    if (model.freeDays.length === 0) free = T.noFreeDay;
    else if (model.freeDays.length === 1) free = T.freeOne(IS_AR ? DAY_FULL_AR[model.freeDays[0]] : DAY_SHORT[model.freeDays[0]]);
    else free = T.freeDays(model.freeDays.length);
    return {
      first: `${T.dayWord(model.campusDays)} · ${free}`,
      second: [
        `${T.ends} `, bdi(hhmm(model.lastEnd)), ' · ',
        model.longestGap > 0 ? `${T.gap} ${durationText(model.longestGap)}` : T.noGap,
      ],
    };
  }
  function renderTab(model) {
    const credits = registeredCredits();
    $('plbTabCredits').textContent = credits === null || !state.ctx ? '' : T.credits(credits);
    const facts1 = $('plbTabFacts1');
    const facts2 = $('plbTabFacts2');
    if (!state.ctx) { facts1.textContent = '—'; facts2.textContent = ''; return; }
    const facts = factsOf(model);
    facts1.textContent = facts.first;
    facts2.replaceChildren(...facts.second);
  }
  function weekBlock(m, rowStart, rowEnd, col) {
    const rows = rowEnd - rowStart;
    const range = `${hhmm(m.st)}-${hhmm(m.en)}`;
    const block = el('div', 'plb-b');
    block.style.gridColumn = String(col);
    block.style.gridRow = `${rowStart} / ${rowEnd}`;
    block.dataset.code = m.key;
    block.dataset.kind = m.kind || 'reg';
    if (m.clash) block.dataset.clash = 'true';
    const label = `${m.code}${m.section ? ` ${IS_AR ? 'شعبة' : 'section'} ${m.section}` : ''}, ${DAY_SHORT[m.day]} ${range}`
      + `${m.kind === 'new' ? `, ${T.plannedBlock}` : ''}${m.clash ? `, ${T.clashBlock}` : ''}`;
    block.setAttribute('role', 'img');
    block.setAttribute('aria-label', label);
    block.append(
      el('span', 'plb-cd', bdi(m.code)),
      m.section ? el('span', 'plb-bs', bdi(`§${m.section}`)) : null,
      el('span', 'plb-bt', bdi(range)));
    return block;
  }
  function renderLegend(opt) {
    const showRegistered = !opt || state.build.rules.mode === 'keep';
    const parts = [];
    if (showRegistered) parts.push(el('span', 'plb-lg-i', el('i', 'plb-k'), T.legendRegistered));
    if (opt) parts.push(...(parts.length ? [' '] : []), el('span', 'plb-lg-i', el('i', 'plb-k plb-k--new'), T.planned));
    $('plbLegend').replaceChildren(...parts);
    for (const mark of $('plbLegend').querySelectorAll('i')) mark.setAttribute('aria-hidden', 'true');
  }
  function renderWeek() {
    const host = $('plbWeek');
    const regModel = weekModel(registeredMeetings());
    renderTab(regModel);
    const opt = currentOption();
    renderLegend(opt);
    const name = $('plbWeekName');
    if (opt) name.replaceChildren(...T.optionName(opt.name));
    else name.textContent = T.registered;
    $('plbWeekPanel').setAttribute('aria-labelledby', opt ? tabId(opt.name) : 'plbTabRegistered');
    if (!state.ctx) { host.replaceChildren(el('p', 'plb-empty', T.loadFirstWeek)); return; }
    const showRegistered = !opt || state.build.rules.mode === 'keep';
    const untimed = showRegistered ? registeredCourses().filter((c) => !c.timed).length : 0;
    const clashes = opt ? weekMeetings(opt).filter((m) => m.kind === 'new' && m.clash).length : 0;
    const notes = [
      untimed ? el('p', 'plb-note', T.untimed(untimed)) : null,
      clashes ? el('p', 'plb-note', T.clashNote(clashes)) : null,
    ].filter(Boolean);
    const model = opt ? weekModel(weekMeetings(opt)) : regModel;
    if (!model.meetings.length) {
      host.replaceChildren(...[el('p', 'plb-empty', T.noClassesAtAll), ...notes]);
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
    host.replaceChildren(el('div', 'plb-wkwrap', grid), ...notes);
  }

  /* ── The options: tabs, their facts and the selected option ────────────── */
  const tabId = (name) => `plbTab_${String(name).replace(/[^A-Za-z0-9_-]/g, '_')}`;
  function currentOption() {
    if (!state.build || state.selected === null) return null;
    return state.build.options.find((o) => o.name === state.selected) || null;
  }
  // The mapping an option chose for a course of the plan, or null.
  function mappingFor(opt, code) {
    const want = norm(code);
    return (opt.mappings || []).find((m) => courseKeyOf(m) === want || norm(m.course_code) === want) || null;
  }
  // The plan's courses (as built) this option could not place.
  function unplacedFor(opt) {
    return state.build.shortlist.filter((item) => !mappingFor(opt, item.course_code));
  }
  function optionCredits(opt) {
    const added = (opt.mappings || []).reduce((sum, m) => {
      const item = state.build.shortlist.find((c) => norm(c.course_code) === courseKeyOf(m) || norm(c.course_code) === norm(m.course_code));
      return sum + (item ? Number(item.credits) || 0 : 0);
    }, 0);
    return added + (state.build.rules.mode === 'keep' ? registeredCredits() || 0 : 0);
  }
  function optionIds(opt) {
    return (opt.mappings || []).map((m) => m.term_section_id).filter(Boolean);
  }
  function optionTab(opt) {
    const facts = factsOf(weekModel(weekMeetings(opt)));
    const unplaced = unplacedFor(opt).length;
    const selected = state.selected === opt.name;
    const tab = el('button', 'plb-tab',
      el('span', 'plb-tab-n', bdi(opt.name), ' ', el('span', 'plb-tab-cr', `· ${T.credits(optionCredits(opt))}`)),
      el('span', 'plb-tab-x', facts.first),
      el('span', 'plb-tab-x', ...facts.second),
      unplaced ? el('span', 'plb-tab-x plb-tab-warn', T.notScheduled(unplaced)) : null);
    tab.type = 'button';
    tab.id = tabId(opt.name);
    tab.dataset.opt = opt.name;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(selected));
    tab.setAttribute('aria-controls', 'plbWeekPanel');
    tab.tabIndex = selected ? 0 : -1;
    return tab;
  }
  function moreNode(hidden) {
    const wrap = el('div', 'plb-more');
    wrap.id = 'plbMore';
    const button = el('button', 'plb-more-btn', T.moreOptions(hidden.length));
    button.type = 'button';
    button.id = 'plbMoreBtn';
    button.setAttribute('aria-expanded', String(state.moreOpen));
    button.setAttribute('aria-controls', 'plbMoreList');
    const list = el('div', 'plb-more-list');
    list.id = 'plbMoreList';
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', T.moreLabel);
    list.hidden = !state.moreOpen;
    for (const opt of hidden) {
      const facts = factsOf(weekModel(weekMeetings(opt)));
      const item = el('button', 'plb-more-item', bdi(opt.name), ` · ${T.credits(optionCredits(opt))} · ${facts.first}`);
      item.type = 'button';
      item.dataset.more = opt.name;
      list.append(item);
    }
    wrap.append(button, list);
    return wrap;
  }
  function renderStrip() {
    const tabs = $('plbTabs');
    const strip = $('plbStrip');
    tabs.querySelectorAll('[data-opt]').forEach((n) => n.remove());
    strip.querySelectorAll('#plbMore').forEach((n) => n.remove());
    const registered = $('plbTabRegistered');
    registered.setAttribute('aria-selected', String(state.selected === null));
    registered.tabIndex = state.selected === null ? 0 : -1;
    const options = state.build ? state.build.options : [];
    // The first three are tabs; a later option chosen from "More options" takes the fourth place.
    const shown = options.slice(0, 3);
    const chosen = options.findIndex((o) => o.name === state.selected);
    if (chosen >= 3) shown.push(options[chosen]);
    const hidden = options.filter((o) => !shown.includes(o));
    for (const opt of shown) tabs.insertBefore(optionTab(opt), registered);
    // "More options" is a disclosure, not a tab: it sits beside the tab list, never inside it.
    if (hidden.length) strip.append(moreNode(hidden));
    else state.moreOpen = false;
  }
  function selectTab(name, focus) {
    state.selected = name;
    state.moreOpen = false;
    renderStrip();
    renderWeek();
    renderCourses();
    renderActions();
    if (focus) {
      const tab = name === null ? $('plbTabRegistered') : $(tabId(name));
      if (tab) tab.focus();
    }
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
    const opt = currentOption();

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
      const chosen = opt ? mappingFor(opt, code) : null;
      if (opt) {
        // On an option's tab the cell says what that option did with the course.
        if (chosen) sectionCell.append(bdi(`§${String(chosen.section || '').trim() || '?'}`));
        else { sectionCell.classList.add('plb-sc--miss'); sectionCell.append(T.notScheduledHere); }
      } else if (pin && pin.section) sectionCell.append(`${T.pinned} `, bdi(`§${pin.section}`));
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
    dropBuild(true);
    state.focus = { kind: 'remove', code: item.course_code };
    setStatus('ok', ...T.added(item.course_code));
    renderCourses();
  }
  function removeCourseFromPlan(code) {
    const at = state.shortlist.findIndex((c) => norm(c.course_code) === norm(code));
    if (at < 0) return;
    const [gone] = state.shortlist.splice(at, 1);
    dropBuild(true);
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
    dropBuild(true);   // options belong to the student and the plan they were built from

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
    renderActions();
    loadPlan(run);
    return true;
  }

  /* ── Build, rebuild and apply ──────────────────────────────────────────── */
  function setBuildLine(kind, ...parts) {
    const line = $('plbBuildLine');
    line.dataset.kind = kind || '';
    line.replaceChildren(...parts.map((p) => (typeof p === 'string' ? document.createTextNode(p) : p)));
  }
  // The server's own words for a refusal: {error: {message}} or {error: "text"}; else a fixed line.
  function errorText(data) {
    const e = data && data.error;
    if (e && typeof e === 'object' && e.message) return String(e.message);
    if (typeof e === 'string' && e) return e;
    return T.requestFailed;
  }
  // The session is gone on a redirect to sign-in, a 401, or a 403 that is not one of the app's own
  // refusals (the CSRF failure page). A 403 that carries the server's reason (role, student scope)
  // is a refusal like any other and is shown in the server's words.
  const sessionGone = (response, data) => response.redirected || response.status === 401
    || (response.status === 403 && !(data && typeof data === 'object' && (data.error || data.reason_code)));

  // The options are gone: the plan, the rules or the student they were built from changed. Any
  // reply still in flight is no longer believed. `say` puts the plain line where the options were.
  function dropBuild(say) {
    state.buildGen += 1;
    if (!state.build) return;
    // A review left open lists options that no longer exist: close it and say why. A save that is
    // already on its way is left to finish and report.
    const reviewing = $('plbApplyDialog').open && !state.applying;
    if (reviewing) closeApply();
    state.build = null;
    state.selected = null;
    state.moreOpen = false;
    if (say) setBuildLine('warn', T.planChanged);
    else setBuildLine('');
    renderStrip();
    renderWeek();
    renderCourses();
    renderActions();
    // The button that opened the review is disabled now: put the keyboard on the next step.
    if (reviewing && !$('plbBuild').disabled) $('plbBuild').focus();
  }

  function reasonLines(build) {
    const lines = [];
    for (const f of build.failures) {
      const code = String(f.course_code || '');
      let reason;
      if (IS_AR) reason = f.kind === 'must_take' ? T.failMust : (f.kind === 'pinned_section' ? T.failPinned : T.failCredits);
      else reason = String(f.reason || T.noFeasible);
      lines.push([...(code ? [bdi(code), ': '] : []), reason]);
    }
    const groups = new Map();
    for (const u of build.unscheduled) {
      const why = T.reason[String(u.reason_code || '')] || String(u.reason || '') || T.noReason;
      if (!groups.has(why)) groups.set(why, []);
      if (u.course_code) groups.get(why).push(String(u.course_code));
    }
    for (const [why, codes] of groups) lines.push(codes.length ? [...T.notScheduledList(codes), ' — ', why] : [why]);
    if (!lines.length) lines.push([T.noFeasible]);
    return lines;
  }
  function applicability() {
    const opt = currentOption();
    const build = state.build;
    if (build && build.options.length && !build.rules.programOnly) return { ok: false, why: T.comparisonOnly };
    if (!opt) return { ok: false, why: null };
    if (!optionIds(opt).length) return { ok: false, why: T.nothingToSave };
    return { ok: true, why: null };
  }
  function renderActions() {
    const building = state.inflight > 0;
    const build = $('plbBuild');
    build.textContent = state.build ? T.rebuild : T.buildFirst;
    build.disabled = !state.ctx || building || state.applying;
    if (building) build.setAttribute('aria-busy', 'true'); else build.removeAttribute('aria-busy');
    const verdict = applicability();
    $('plbReview').disabled = !verdict.ok || building || state.applying;
    const why = $('plbApplyWhy');
    why.textContent = verdict.why || '';
    why.hidden = !verdict.why;
    const last = $('plbLastBuild');
    if (state.build) last.replaceChildren(`${T.lastBuild} `, bdi(state.build.at));
    else last.replaceChildren();
    last.hidden = !state.build;
    const note = $('plbBuildNote');
    if (state.build && !state.build.options.length) {
      note.replaceChildren(
        el('p', 'plb-buildnote-lead', T.noOption),
        el('ul', 'plb-buildnote-list', ...reasonLines(state.build).map((parts) => el('li', '', ...parts))));
      note.hidden = false;
    } else {
      note.replaceChildren();
      note.hidden = true;
    }
  }

  // Exactly the body the current page posts for the same choices (page-planner.js, runBuilderRequest).
  function buildBody() {
    return {
      student_id: state.ctx.studentId,
      academic_year: state.ctx.year,
      term: state.ctx.term,
      mode: state.rules.mode,
      eligibility_mode: state.rules.eligibility,
      swap: state.rules.swap,
      program_sections_only: state.rules.programOnly,
      allow_full_sections: state.rules.allowFull,
      max_credits: Number(state.rules.maxCredits || 0),
      shortlist: state.shortlist,
      baseline: state.baseline,
    };
  }
  async function runBuild() {
    if (!state.ctx || state.inflight > 0 || state.applying) return;
    const button = $('plbBuild');
    const hadFocus = document.activeElement === button;
    state.inflight += 1;
    // A new build retires the last one; the Registered week is what shows meanwhile.
    state.build = null;
    state.selected = null;
    state.moreOpen = false;
    const gen = ++state.buildGen;
    const rules = { mode: state.rules.mode, programOnly: state.rules.programOnly };
    const shortlist = JSON.parse(JSON.stringify(state.shortlist));
    const body = buildBody();
    setBuildLine('busy', T.building);
    renderStrip();
    renderWeek();
    renderCourses();
    renderActions();
    let result = null;
    try { result = await postJson('/ops/planner/build/', body); } catch (err) { result = null; }
    state.inflight -= 1;
    try {
      if (gen !== state.buildGen) {
        // The plan, the rules or the student changed while this ran: its answer is not for this page.
        if (state.inflight === 0) setBuildLine('warn', T.planChanged);
        return;
      }
      if (!result) { setBuildLine('error', T.buildFailed); return; }
      const { response, data } = result;
      if (sessionGone(response, data)) { setBuildLine('error', T.sessionExpired); return; }
      if (response.status === 429) { setBuildLine('error', T.tooMany); return; }
      if (!data || typeof data !== 'object') { setBuildLine('error', T.buildFailed); return; }
      if (data.error) { setBuildLine('error', errorText(data)); return; }
      if (!response.ok) { setBuildLine('error', T.buildFailed); return; }
      const summary = data.summary || {};
      const options = Array.isArray(data.options) ? data.options : [];
      const feasible = Boolean(summary.best_feasible && options.length);
      const now = new Date();
      state.build = {
        options: feasible ? options : [],
        shortlist,
        rules,
        at: `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`,
        failures: Array.isArray(summary.hard_constraint_failures) ? summary.hard_constraint_failures : [],
        unscheduled: Array.isArray(data.unscheduled) ? data.unscheduled : [],
      };
      state.selected = feasible ? String(options[0].name) : null;
      setBuildLine('');
      renderStrip();
      renderWeek();
      renderCourses();
    } finally {
      renderActions();
      if (!state.inflight && (hadFocus || document.activeElement === document.body) && !button.disabled) button.focus();
    }
  }

  function openApply() {
    const opt = currentOption();
    if (!opt || !applicability().ok || state.applying) return;
    const build = state.build;
    $('plbApplyTitle').replaceChildren(...(IS_AR ? ['مراجعة وتطبيق الخيار ', bdi(opt.name)] : ['Review and apply option ', bdi(opt.name)]));
    $('plbApplySub').replaceChildren(bdi(`${state.ctx.studentId} · ${state.ctx.year}/${state.ctx.term}`));
    $('plbApplyList').replaceChildren(...(opt.mappings || []).filter((m) => m.term_section_id).map((m) => {
      const key = courseKeyOf(m) || String(m.course_code || '');
      const item = build.shortlist.find((c) => norm(c.course_code) === key);
      const days = [...new Set((m.meetings || []).map((mm) => normDay(mm.day)).filter(Boolean))];
      const times = [...new Set((m.meetings || []).map((mm) => `${mm.start_time}-${mm.end_time}`))];
      return el('li', 'plb-apply-row',
        el('span', '', bdi(key), ...(m.section ? [' ', bdi(`§${String(m.section).trim()}`)] : []), ...(item && item.course_name ? [' ', isolate(item.course_name)] : [])),
        el('span', 'plb-apply-when', days.map((d) => DAY_SHORT[d]).join(' '), ...(days.length && times.length ? [' '] : []), ...(times.length ? [bdi(times.join(' / '))] : [])));
    }));
    const registered = registeredCourses();
    $('plbApplyReg').replaceChildren(build.rules.mode === 'keep'
      ? el('span', '', T.keptRegistered(registered.length), ' ',
        ...registered.flatMap((c, i) => (i ? [' · ', bdi(c.code)] : [bdi(c.code)])))
      : T.droppedRegistered);
    $('plbApplyWarn').textContent = T.replaceWarn;
    const err = $('plbApplyErr');
    err.textContent = '';
    err.hidden = true;
    renderApplyButtons();
    const dialog = $('plbApplyDialog');
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    $('plbApplyCancel').focus();
  }
  function closeApply() {
    const dialog = $('plbApplyDialog');
    if (typeof dialog.close === 'function') { if (dialog.open) dialog.close(); } else dialog.removeAttribute('open');
  }
  function renderApplyButtons() {
    $('plbApplyGo').disabled = state.applying;
    $('plbApplyGo').textContent = state.applying ? T.saving : T.apply;
    if (state.applying) $('plbApplyGo').setAttribute('aria-busy', 'true'); else $('plbApplyGo').removeAttribute('aria-busy');
    $('plbApplyCancel').disabled = state.applying;
  }
  async function runApply() {
    if (state.applying) return;                     // a second click cannot post twice
    const opt = currentOption();
    if (!opt || !applicability().ok) {
      // Nothing applicable is left to save (the options went while this was open): never go quiet.
      closeApply();
      setBuildLine('warn', T.planChanged);
      return;
    }
    const ctx = { ...state.ctx };
    state.applying = true;
    renderApplyButtons();
    renderActions();
    // Exactly the body the current page posts (page-planner.js, the applyOption handler).
    const body = {
      student_id: ctx.studentId,
      academic_year: ctx.year,
      term: ctx.term,
      term_section_ids: optionIds(opt),
      confirm_replace: true,
    };
    const refuse = (line) => {
      state.applying = false;
      const err = $('plbApplyErr');
      err.textContent = line;
      err.hidden = false;
      renderApplyButtons();
      renderActions();
    };
    let result = null;
    try { result = await postJson('/ops/planner/save-student-sections/', body); } catch (err) { result = null; }
    if (!result) { refuse(T.saveFailed); return; }
    const { response, data } = result;
    if (sessionGone(response, data)) { refuse(T.sessionExpired); return; }
    if (!data || typeof data !== 'object') { refuse(T.saveFailed); return; }
    if (data.error) { refuse(errorText(data)); return; }
    if (!response.ok) { refuse(T.saveFailed); return; }

    // Saved. What the write took away and what it clashes with is said, as the current page does.
    const removed = Array.isArray(data.removed) ? data.removed : [];
    const clashes = Array.isArray(data.clashes_with_registered) ? data.clashes_with_registered : [];
    state.applying = false;
    renderApplyButtons();
    closeApply();
    dropBuild(false);
    const said = [T.saved];
    if (removed.length) {
      said.push('. ', T.removedPlanned(removed.length), ' (',
        ...removed.flatMap((x, i) => [i ? ', ' : '', bdi(`${x.course_code || ''} ${x.section || ''}`.trim())]), ')');
    }
    if (clashes.length) {
      said.push('. ', T.clashSaved(clashes.length), ' (',
        ...clashes.flatMap((x, i) => [i ? ', ' : '', bdi(`${x.course_code || ''} ${x.section || ''} · ${x.clashes_with || ''}`.trim())]), ')');
    }
    if (removed.length || clashes.length) said.push('.');
    setBuildLine('busy', T.loading);
    const reloaded = state.ctx && state.ctx.studentId === ctx.studentId ? await loadStudent({ reuse: true }) : false;
    if (reloaded) {
      setBuildLine(removed.length || clashes.length ? 'warn' : 'ok', ...said);
      setStatus('ok', T.saved);
    } else {
      setBuildLine('warn', T.savedNoReload);
    }
    renderActions();
    const build = $('plbBuild');
    if (!build.disabled) build.focus();
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
      if (!item) return;
      item.must_take = target.checked;
      if (state.build || state.inflight) {
        dropBuild(true);
        state.focus = { kind: 'must', code: item.course_code };
        renderCourses();
      }
    });

    // The options: tabs (arrow keys, Home and End), "More options", Build and Review and apply.
    $('plbBuild').addEventListener('click', runBuild);
    $('plbReview').addEventListener('click', openApply);
    $('plbApplyForm').addEventListener('submit', (event) => { event.preventDefault(); runApply(); });
    $('plbApplyCancel').addEventListener('click', () => { if (!state.applying) closeApply(); });
    $('plbApplyDialog').addEventListener('cancel', (event) => { if (state.applying) event.preventDefault(); });
    $('plbApplyDialog').addEventListener('close', () => {
      const review = $('plbReview');
      if (!review.disabled && document.activeElement === document.body) review.focus();
    });
    const strip = $('plbStrip');
    strip.addEventListener('click', (event) => {
      const more = event.target.closest('#plbMoreBtn');
      if (more) {
        state.moreOpen = !state.moreOpen;
        more.setAttribute('aria-expanded', String(state.moreOpen));
        $('plbMoreList').hidden = !state.moreOpen;
        return;
      }
      const item = event.target.closest('[data-more]');
      if (item) { selectTab(item.dataset.more, true); return; }
      // The strip is redrawn on every choice, so focus is put back on the chosen tab: without that
      // Enter or Space on a tab would drop the keyboard user at the top of the page.
      const tab = event.target.closest('[role="tab"]');
      if (tab) selectTab(tab.dataset.opt === undefined ? null : tab.dataset.opt, true);
    });
    strip.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && state.moreOpen) {
        state.moreOpen = false;
        renderStrip();
        $('plbMoreBtn').focus();
        return;
      }
      const tab = event.target.closest('[role="tab"]');
      if (!tab) return;
      const all = [...strip.querySelectorAll('[role="tab"]')];
      const at = all.indexOf(tab);
      const rtl = document.documentElement.dir === 'rtl';
      let to = -1;
      if (event.key === (rtl ? 'ArrowLeft' : 'ArrowRight')) to = (at + 1) % all.length;
      else if (event.key === (rtl ? 'ArrowRight' : 'ArrowLeft')) to = (at - 1 + all.length) % all.length;
      else if (event.key === 'Home') to = 0;
      else if (event.key === 'End') to = all.length - 1;
      if (to < 0) return;
      event.preventDefault();
      selectTab(all[to].dataset.opt === undefined ? null : all[to].dataset.opt, true);
    });
    document.addEventListener('click', (event) => {
      if (state.moreOpen && !event.target.closest('#plbMore')) { state.moreOpen = false; renderStrip(); }
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
    renderActions();
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
