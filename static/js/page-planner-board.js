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
  // Phones: the picker shows one panel at a time and the section popover is a sheet under its row.
  const PHONE = window.matchMedia('(max-width: 768px)');

  /* ── Strings ───────────────────────────────────────────────────────────── */
  const forms = (one, two, few, many) => ({ one, two, few, many, other: many });
  const T = {
    enterStudent: IS_AR ? 'أدخل رقم الطالب.' : 'Enter a student ID.',
    enterYear: IS_AR ? 'أدخل سنة من أربعة أرقام.' : 'Enter a four-digit year.',
    loading: IS_AR ? 'جارٍ تحميل بيانات الطالب…' : 'Loading student…',
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
    registeredCredits: IS_AR ? 'مسجّل هذا الفصل' : 'Registered this term',
    advisory: (max) => (max ? (IS_AR ? `إرشادي · الأقصى ${max}` : `advisory · max ${max}`) : (IS_AR ? 'إرشادي' : 'advisory')),
    programme: IS_AR ? 'البرنامج' : 'Programme',
    registered: IS_AR ? 'المسجّل' : 'Registered',
    blocked: IS_AR ? 'غير مؤهل' : 'Blocked',
    missing: IS_AR ? 'ناقص:' : 'Missing:',
    inThisPlan: IS_AR ? '(ضمن هذه الخطة)' : '(in this plan)',
    prereqNotMet: IS_AR ? 'المتطلبات غير مستوفاة' : 'Prerequisites not met',
    anySection: IS_AR ? 'أي شعبة' : 'Any section',
    noRegistered: IS_AR ? 'لا مقررات مسجلة في هذا الفصل.' : 'No registered courses this term.',
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
    // "2 registered courses have no timetable slots (CS340, CS341)."
    untimed: (codes) => {
      const n = codes.length;
      const list = codes.flatMap((c, i) => (i ? [IS_AR ? '، ' : ', ', bdi(c)] : [bdi(c)]));
      const lead = IS_AR
        ? `${pluralAr(n, forms('مقرر مسجّل واحد', 'مقرران مسجّلان', '{n} مقررات مسجّلة', '{n} مقررًا مسجّلًا'))} بلا مواعيد في الجدول`
        : `${n} registered ${n === 1 ? 'course has' : 'courses have'} no timetable slots`;
      return [`${lead} (`, ...list, ').'];
    },
    // The registrar's timetable was mapped to sections only in part (n rows came from a fallback).
    mappingPartial: (n) => (IS_AR ? `جودة الربط: جزئي (${n} احتياطي)` : `Mapping quality: partial (${n} fallback)`),
    advisor: IS_AR ? 'المرشد' : 'Advisor',
    // A section's programmes: "For AI / DS"; in Arabic «لبرنامج AI», «لبرامج AI / DS»; none: "Unassigned".
    forProgrammes: (list) => {
      if (!list.length) return [IS_AR ? 'غير مرتبط ببرنامج' : 'Unassigned'];
      return [IS_AR ? (list.length === 1 ? 'لبرنامج ' : 'لبرامج ') : 'For ', bdi(list.join(' / '))];
    },
    // The course picker (zone 02)
    srcRecommended: (n) => (IS_AR ? `مقترحة لهذا الطالب (${n})` : `Recommended for this student (${n})`),
    srcPlan: (n) => (IS_AR ? `خطة هذا الطالب (${n})` : `This student's plan (${n})`),
    srcEligible: (n) => (IS_AR ? `كل ما يمكنه تسجيله (${n})` : `Everything they can register (${n})`),
    srcAll: (n) => (n === null ? (IS_AR ? 'مقررات كل البرامج' : "All programmes' courses") : (IS_AR ? `مقررات كل البرامج (${n})` : `All programmes' courses (${n})`)),
    mustTake: IS_AR ? 'إلزامي' : 'Must-take',
    tabAvail: (n) => (IS_AR ? `المتاحة · ${n}` : `Available · ${n}`),
    tabPlan: (n) => (IS_AR ? `خطتك · ${n}` : `Your plan · ${n}`),
    addCourse: (code, secs) => {
      if (!secs.length) return IS_AR ? `إضافة ${code}` : `Add ${code}`;
      const list = IS_AR ? secs.join(' و') : (secs.length > 1 ? `${secs.slice(0, -1).join(', ')} and ${secs[secs.length - 1]}` : secs[0]);
      return IS_AR ? `إضافة ${code}، الشُعب ${list}` : `Add ${code}, section${secs.length > 1 ? 's' : ''} ${list}`;
    },
    phoneSum: (n, load) => `${T.courseWord(n)} · ${load}`,
    availHead: (n) => [IS_AR ? 'المتاحة' : 'Available', T.courseWord(n)],
    planHead: (n) => [IS_AR ? 'خطتك' : 'Your plan', T.courseWord(n)],
    tickedLine: (k, cr) => (IS_AR ? `${k} محدد · ${cr} ${T.creditUnit(cr)}` : `${k} ticked · ${cr} cr`),
    plannedN: (n) => (IS_AR ? `${n} مخطَّطة` : `${n} planned`),
    registeredN: (n) => (IS_AR ? `${n} مسجّلة` : `${n} registered`),
    overLimitN: (n) => (IS_AR ? `${n} ${T.creditUnit(n)} فوق الحد` : `${n} cr over the limit`),
    tagRecommended: IS_AR ? 'مقترح' : 'Recommended',
    tagCompare: IS_AR ? 'للمقارنة فقط' : 'Compare only',
    // An open elective row a registered course outside the plan is counted for (an assumption).
    filledBy: (code) => (IS_AR
      ? [bdi(code), ' (مسجّل) يُحتسب له على الأرجح']
      : [bdi(code), ' (registered) likely counts for it']),
    compareWhy: IS_AR
      ? 'لا شعبة من برنامج هذا الطالب: يمكن بناؤه فقط مع «الشُعب من: أي برنامج».'
      : "No section of this student's programme: it can be built only with Sections from: Any programme.",
    tagFewSeats: IS_AR ? 'مقاعد قليلة' : 'Few seats',
    creditsUnknown: IS_AR ? '؟ ساعة' : '? cr',
    skippedUnknown: (codes, added) => [...(added ? [IS_AR ? `أُضيفت ${T.courseWord(added)}. ` : `Added ${added}. `] : []), IS_AR ? 'لم تُضف لأن ساعاتها غير معروفة: ' : 'Not added, their credits are not known: ', ...codes.flatMap((c, i) => (i ? [IS_AR ? '، ' : ', ', bdi(c)] : [bdi(c)]))],
    creditsUnknownWhy: IS_AR ? 'عدد ساعات المقرر غير معروف، لذا لا يمكن إضافته.' : 'The credits of this course are not known, so it cannot be added.',
    leftTag: (n) => (IS_AR ? `${n} متبقٍ` : `${n} left`),
    secHead: IS_AR ? 'الشعبة' : 'Sec',
    meetsHead: IS_AR ? 'اللقاء' : 'Meets',
    seatsHead: IS_AR ? 'المقاعد' : 'Seats',
    addOnly: (n) => (IS_AR ? `أضف ${T.sectionWord(n)} فقط` : `Add only these ${T.sectionWord(n)}`),
    showAll: (n) => (IS_AR ? `عرض الكل ${n}` : `Show all ${n}`),
    blockedFold: (n) => (IS_AR ? `${n} لا يمكن تسجيلها بعد · متطلبات سابقة ناقصة` : `${n} can't be registered yet · prerequisites missing`),
    sectionsOf: (code) => (IS_AR ? `شُعب ${code}` : `Sections of ${code}`),
    tickCourse: (code) => (IS_AR ? `تحديد ${code}` : `Tick ${code}`),
    tickSection: (code, label) => (IS_AR ? `تحديد الشعبة ${label} من ${code}` : `Tick section ${label} of ${code}`),
    tickPlanned: (code) => (IS_AR ? `اختيار ${code} في الخطة` : `Select ${code} in the plan`),
    nothingHere: IS_AR ? 'لا مقررات لإضافتها من هذه القائمة.' : 'Nothing to add from this list.',
    // The "Recommended" source when it has nothing to show: there was nothing to recommend, or the
    // adviser has already moved every recommended course into Your plan (Fetch adds none itself).
    noRecs: IS_AR ? 'لا مقررات مقترحة لهذا الطالب.' : 'Nothing is recommended for this student.',
    recsAllPlanned: IS_AR ? 'كل المقررات المقترحة موجودة في خطتك.' : 'Every recommended course is already in Your plan.',
    allLoading: IS_AR ? 'جارٍ تحميل كل مقررات هذا الفصل…' : 'Loading every course offered this term…',
    allFailed: IS_AR ? 'تعذّر تحميل قائمة كل المقررات.' : 'The list of all courses could not be loaded.',
    sectionsBtn: (labels) => (IS_AR
      ? `${labels.length === 1 ? 'الشعبة' : 'الشعب'} ${labels.join('، ')}`
      : `${labels.length === 1 ? 'Section' : 'Sections'} ${labels.join(', ')}`),
    sectionsOfPlanned: (code, label) => `${code}: ${label}`,
    required: IS_AR ? 'إلزامي' : 'Required',
    requiredOn: (code) => (IS_AR ? `${code} إلزامي، اضغط للإلغاء` : `${code} is required, press to undo`),
    makeRequired: (code) => (IS_AR ? `اجعل ${code} إلزاميًا` : `Make ${code} required`),
    hintA: IS_AR ? 'حدّد مقررات من القائمة المجاورة واضغط ' : 'Tick courses on the left and press ',
    hintB: IS_AR ? ' أو انقر مرتين على أحدها' : ', or double-click one',
    xAdd: IS_AR ? 'إضافة المقررات المحدّدة إلى الخطة' : 'Add the ticked courses',
    xAddAll: IS_AR ? 'إضافة كل المقررات المعروضة' : 'Add every course shown',
    xRem: IS_AR ? 'إزالة المقررات المحدّدة من الخطة' : 'Remove the ticked courses from the plan',
    xRemAll: IS_AR ? 'إزالة كل مقررات الخطة' : 'Remove every planned course',
    addedMany: (n) => (IS_AR ? `أُضيفت ${T.courseWord(n)} إلى الخطة.` : `Added ${n} courses to the plan.`),
    removedMany: (n) => (IS_AR ? `أُزيلت ${T.courseWord(n)} من الخطة.` : `Removed ${n} courses from the plan.`),
    // The registered courses use the whole limit and nothing is planned yet: not "over", just no room.
    noRoomLeft: IS_AR ? 'لا متّسع ضمن الحد' : 'no room left under the limit',
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
    maxError: (max) => (IS_AR ? `أدخل عددًا صحيحًا من 1 إلى ${max}.` : `Enter a whole number from 1 to ${max}.`),
    // The student band and the current-timetable disclosure.
    termButton: (year, term) => (IS_AR ? [`الفصل ${term}، `, bdi(year)] : [`Term ${term}, `, bdi(year)]),
    bandProgress: IS_AR ? 'التقدم' : 'Progress',
    bandRegistered: IS_AR ? 'المسجّل الآن' : 'Registered now',
    bandGraduates: IS_AR ? 'يتخرّج كما هو الحال' : 'Graduates as it stands',
    optionsCurrent: IS_AR ? 'الخيارات أدناه تطابق هذه المقررات والقواعد' : 'The options below match these courses and rules',
    optionsStale: IS_AR ? 'الخيارات قديمة، أعد البناء' : 'Options are out of date — rebuild',
    kicker: IS_AR ? 'المخطِّط' : 'Planner',
    afterFirstBuild: IS_AR ? 'بعد أول بناء' : 'After the first build',
    curTTNone: IS_AR ? 'بلا محاضرات' : 'No timed classes',

    // Build, options and apply.
    buildFirst: IS_AR ? 'ابنِ الخيارات' : 'Build options',
    rebuild: IS_AR ? 'أعد بناء الخيارات' : 'Rebuild options',
    building: IS_AR ? 'جارٍ بناء الخيارات… قد يستغرق ذلك حتى 10 ثوانٍ' : 'Building options… this can take up to 10 seconds',
    planChanged: IS_AR ? 'تغيّرت الخطة. ابنِ الخيارات مرة أخرى لعرضها.' : 'The plan changed. Build again to see options.',
    buildFailed: IS_AR ? 'تعذّر بناء الخيارات. حاول مرة أخرى.' : 'The build failed. Try again.',
    tooMany: IS_AR ? 'طلبات بناء كثيرة. حاول بعد لحظات.' : 'Too many builds. Try again in a moment.',
    sessionExpired: IS_AR ? 'انتهت جلستك. سجّل الدخول مرة أخرى ثم أعد المحاولة.' : 'Your session has expired. Sign in again, then retry.',
    requestFailed: IS_AR ? 'فشل الطلب' : 'Request failed',
    noOption: IS_AR ? 'لم يُعثر على خيار يحقق القيود الحالية.' : 'No option could be built under the current constraints.',
    noFeasible: IS_AR
      ? 'لا توجد نتيجة صالحة تحقق جميع المقررات الإلزامية والشُعب المحدَّدة ضمن القيود الحالية.'
      : 'No valid result satisfies every must-take course and the sections chosen under the current constraints.',
    failMust: IS_AR ? 'تعذر إدراج المقرر الإلزامي ضمن القيود الحالية.' : 'The must-take course could not be placed under the current constraints.',
    failPinned: IS_AR ? 'تعذر استخدام الشُعب المحدَّدة.' : 'The chosen sections could not be used.',
    failCredits: IS_AR ? 'يتعارض حد الساعات مع القيود المطلوبة.' : 'The credit limit conflicts with the required constraints.',
    // In keep mode the limit covers the registered courses too. A course that only lacks room is
    // said so with the numbers, or it reads as a clash (the old page's sentences, #287).
    noRoomUnderLimit: IS_AR
      ? 'لا يمكن إضافة مقررات: المقررات المسجلة تستنفد حد الساعات كاملًا.'
      : 'Nothing can be added: the registered courses use the whole credit limit.',
    overLimit: (credits, limit) => (IS_AR
      ? `${credits} ساعات، أعلى من حد ${limit} ساعة لهذا البناء`
      : `${credits} credits, above the ${limit}-credit limit of this build`),
    noRoom: (limit, kept) => (IS_AR
      ? `لا متّسع ضمن حد ${limit} ساعة: الساعات المسجلة ${kept}`
      : `No room left under the ${limit}-credit limit: ${kept} credits are registered`),
    aboveRoom: (credits, room, limit, kept) => (IS_AR
      ? `${credits} ساعات، والمتبقي من حد ${limit} ساعة ${room} فقط (المسجّل ${kept})`
      : `${credits} credits, above the ${room} left of the ${limit}-credit limit (${kept} registered)`),
    besideOthers: (limit, kept) => (IS_AR
      ? `تعذّر إدراجه ضمن حد ${limit} ساعة (المسجّل منها ${kept})، أو أن شُعبه متعارضة`
      : `Could not fit within the ${limit}-credit limit (${kept} of it registered), or its sections clash`),
    withinLimit: (limit) => (IS_AR
      ? `تعذّر إدراجه ضمن حد ${limit} ساعة، أو أن شُعبه متعارضة`
      : `Could not fit within the ${limit}-credit limit, or its sections clash`),
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
    reviewOpt: (name) => (IS_AR ? ['مراجعة وتطبيق ', bdi(name)] : ['Review and apply ', bdi(name)]),
    gradWith: (name) => (IS_AR ? ['يتخرّج مع ', bdi(name)] : ['Graduates with ', bdi(name)]),
    glanceOpt: (name) => (IS_AR ? ['الخيار ', bdi(name), ' في لمحة'] : ['Option ', bdi(name), ' at a glance']),
    glanceReg: IS_AR ? 'الأسبوع المسجّل في لمحة' : 'Registered week at a glance',
    fLoad: IS_AR ? 'الحمل' : 'Load',
    fCampus: IS_AR ? 'في الحرم' : 'On campus',
    fFree: IS_AR ? 'اليوم الفارغ' : 'Free day',
    fLast: IS_AR ? 'آخر محاضرة تنتهي' : 'Last class ends',
    fNone: IS_AR ? 'لا يوجد' : 'none',
    loadOf: (n, cap) => (IS_AR ? `${n} من ${cap} ساعة` : `${n} of ${cap} cr`),
    loadOnly: (n) => (IS_AR ? `${n} ساعة` : `${n} cr`),
    fOnly: IS_AR ? 'فقط' : 'only',
    placedAll: IS_AR ? 'وُضعت جميع المقررات المخطَّطة' : 'All planned courses placed',
    notPlaced: (n) => (IS_AR
      ? pluralAr(n, forms('مقرر واحد لم يوضع', 'مقرران لم يوضعا', '{n} مقررات لم توضع', '{n} مقررًا لم يوضع'))
      : `${n} not placed`),
    offDay: (day) => (IS_AR ? `${day} إجازة` : `${day} off`),
    offDays: (n) => (IS_AR ? `${n} أيام إجازة` : `${n} days off`),
    keptTag: IS_AR ? 'مسجّل' : 'registered',
    comparisonOnly: IS_AR
      ? 'للمقارنة فقط: تتضمن الخيارات شُعب برامج أخرى'
      : "Comparison only: other programmes' sections are included",
    nothingToSave: IS_AR ? 'لا شُعب في هذا الخيار للحفظ' : 'This option has no sections to save',
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
    // Where a save went (the code: StudentTermSection rows with source "planner" are the WORKING
    // snapshot class; registrar rows outrank them when this page reads the student back).
    savedWhere: (n, year, term) => (IS_AR
      ? [`تم حفظ ${pluralAr(n, forms('شعبة واحدة', 'شعبتين', '{n} شعب', '{n} شعبة'))} كجدول مخطَّط لهذا الطالب للفصل `, bdi(`${year}/${term}`), '. المقررات المسجّلة لم تتغيّر.']
      : [`Saved ${n} ${n === 1 ? 'section' : 'sections'} as this student's planned timetable for `, bdi(`${year}/${term}`), '. Registered courses are unchanged.']),
    savedShown: IS_AR
      ? 'لا جدول مسجّل لهذا الطالب في هذا الفصل، لذا تعرض هذه الصفحة الشُعب المحفوظة كأسبوعه.'
      : 'No registered timetable is on file for this term, so this page now shows the saved sections as the week.',
    // The out-of-date build.
    stale: IS_AR ? 'تغيّرت الخطة منذ هذا البناء. أعد البناء قبل التطبيق.' : 'The plan changed since this build. Rebuild to apply.',
    staleOld: IS_AR ? 'مضى على هذا البناء أكثر من 30 دقيقة. أعد البناء قبل التطبيق.' : 'This build is over 30 minutes old. Rebuild before applying.',
    notInBuild: IS_AR ? 'ليس في هذا البناء' : 'not in this build',
    // The course detail panel.
    close: IS_AR ? 'إغلاق' : 'Close',
    detailSections: IS_AR ? 'الشعبة' : 'Section',
    anySectionOpt: IS_AR ? 'أي شعبة' : 'Any section',
    anySectionHint: IS_AR ? 'يختار البناء الشعبة المناسبة.' : 'The build chooses the section.',
    seatsTaken: (taken, max) => (IS_AR ? `${taken} من ${max} مشغولة` : `${taken} of ${max} taken`),
    seatsFull: IS_AR ? 'ممتلئة' : 'Full',
    seatsUnknown: IS_AR ? 'السعة غير معروفة' : 'seats unknown',
    clashFlag: IS_AR ? 'يتعارض مع مقرر مسجّل' : 'Clashes with a registered class',
    inOptions: (names) => [IS_AR ? 'في ' : 'in ', ...names.flatMap((n, i) => (i ? [IS_AR ? '، ' : ', ', bdi(n)] : [bdi(n)]))],
    cannotFull: IS_AR ? 'غير متاحة: الشُعب الممتلئة غير مسموحة في القواعد.' : 'Not available: full sections are off in the rules.',
    cannotClash: IS_AR ? 'غير متاحة: تتعارض مع مقرر مسجّل، والمسجّل مُبقى عليه.' : 'Not available: it clashes with a registered class, and registered courses are kept.',
    tickBroken: IS_AR ? 'محدَّدة، لكن لم يعد استخدامها ممكنًا. ' : 'Ticked, but it can no longer be used. ',
    tickGone: IS_AR ? 'محدَّدة، لكنها ليست في قائمة الشُعب الحالية.' : 'Ticked, but it is not in the current section list.',
    noCatalog: IS_AR ? 'بيانات الشُعب غير متاحة.' : 'Section data is not available.',
    // "Only these": several allowed sections for a course.
    onlyOpt: IS_AR ? 'هذه فقط' : 'Only these',
    onlyNone: IS_AR
      ? 'حدِّد الشُعب التي يجوز للبناء استخدامها. ما لم تُحدَّد شعبة، تُستخدم أي شعبة.'
      : 'Tick the sections the build may use. Until one is ticked, any section may be used.',
    onlyPicks: IS_AR ? 'يختار البناء إحدى الشُعب المحدَّدة.' : 'The build picks one of the ticked sections.',
    sixMax: IS_AR ? 'ست شُعب على الأكثر.' : 'Six sections at most.',
    // On an option's tab, the other ticked sections: " · or §4", " · or 2 others".
    orOthers: (labels) => {
      if (labels.length === 1) return [' · ', IS_AR ? 'أو ' : 'or ', bdi(`§${labels[0]}`)];
      const n = labels.length;
      return [' · ', IS_AR
        ? `أو ${pluralAr(n, forms('شعبة أخرى', 'شعبتان أخريان', '{n} شعب أخرى', '{n} شعبة أخرى'))}`
        : `or ${n} others`];
    },
    // The status line after a change to the allowed sections.
    onlyStatus: (code, labels) => {
      const tag = (l) => bdi(`§${l}`);
      if (!labels.length) return [bdi(code), IS_AR ? ': يجوز استخدام أي شعبة.' : ': any section may be used.'];
      if (labels.length === 1) {
        return IS_AR ? [bdi(code), ': يجوز استخدام ', tag(labels[0]), ' فقط.'] : [bdi(code), ': only ', tag(labels[0]), ' may be used.'];
      }
      if (labels.length === 2) {
        return IS_AR ? [bdi(code), ': يجوز استخدام ', tag(labels[0]), ' أو ', tag(labels[1]), ' فقط.']
          : [bdi(code), ': only ', tag(labels[0]), ' or ', tag(labels[1]), ' may be used.'];
      }
      return [bdi(code), IS_AR ? `: يجوز استخدام ${T.sectionWord(labels.length)} فقط.` : `: only ${T.sectionWord(labels.length)} may be used.`];
    },
    // The week's dashed blocks.
    altTag: (label) => [el('span', 'plb-alt-w', IS_AR ? 'بديل ' : 'alt '), bdi(`§${label}`)],
    legendAlt: IS_AR ? 'بديل' : 'Alternative',
    // Why these options.
    whyMethod: {
      A: IS_AR ? 'تحاول جدولة أكبر عدد ممكن من مقررات الخطة.' : 'Tries to schedule as many courses of the plan as it can.',
      B: IS_AR ? 'تبحث عن أسبوع مضغوط: أيام أقل وفجوات أقل معًا.' : 'Looks for a compact week: few days and few gaps together.',
      C: IS_AR ? 'تقلّل أيام الحضور أولًا ثم الفجوات.' : 'Uses the fewest days first, then the fewest gaps.',
    },
    whyMethodName: (letter) => (IS_AR ? ['الطريقة ', bdi(letter)] : ['Method ', bdi(letter)]),
    whyCount: (n) => (IS_AR
      ? (n ? pluralAr(n, forms('خيار واحد', 'خياران', '{n} خيارات', '{n} خيارًا')) : 'بلا خيارات')
      : (n ? `${n} ${n === 1 ? 'option' : 'options'}` : 'no option')),
    whyUnscheduled: IS_AR ? 'مقررات غير مجدولة' : 'Courses left unscheduled',
    whyAllPlaced: IS_AR ? 'جُدول كل مقرر في الخطة في كل خيار.' : 'Every course in the plan was scheduled in each option.',
    // Projected graduation (the wording is the current page's, page-planner.js PROJECTION_TEXT).
    gradNone: IS_AR ? 'ابنِ الخيارات لعرض التوقع.' : 'Build options to see the projection.',
    gradLoading: IS_AR ? 'جارٍ حساب الإكمال المتوقع…' : 'Calculating projected completion…',
    gradExpired: IS_AR ? 'أعد بناء الخيارات لتحديث التوقع.' : 'Build the options again to refresh the projection.',
    gradUnavailable: IS_AR ? 'التوقع غير متاح' : 'Projection unavailable',
    gradBusy: IS_AR ? 'طلبات كثيرة جدًا. حاول بعد لحظات.' : 'Too many requests. Try again in a moment.',
    gradRetry: IS_AR ? 'حاول مرة أخرى' : 'Try again',
    gradUnresolved: IS_AR ? 'تعذّر تحديد فصل الإكمال' : 'Completion term unresolved',
    gradNoReg: IS_AR ? 'لا يوجد تسجيل رسمي لهذا الفصل' : 'No registrar registration for this term',
    // Shorter than the current page's sentence: here it sits beside the registered line's own term.
    // A screen reader hears the line on its own, so there the sentence says which term is meant.
    gradSame: IS_AR ? 'نفس الفصل' : 'Same term',
    gradSameSaid: IS_AR ? 'نفس فصل التسجيل الحالي' : 'the same term as registered',
    gradAsRegistered: IS_AR ? 'كما هو مسجّل' : 'As registered',
    gradPerTerm: IS_AR ? 'الساعات في كل فصل' : 'Credits per term',
    gradReasons: IS_AR ? 'أسباب على مستوى المقررات' : 'Course-level reasons',
    gradWaiting: IS_AR ? 'لا يمكن دراسة أي مقرر في هذا الفصل' : 'No course can be taken in this term',
    gradNoCourses: IS_AR ? 'بلا مقررات' : 'no courses',
    gradRetainedExpected: IS_AR
      ? 'يتضمن الخيار المقررات المحتفَظ بها من الخطة المتوقعة.'
      : 'This option includes retained courses from the expected plan.',
    gradRetainedWorking: IS_AR
      ? 'يتضمن الخيار المقررات المحتفَظ بها من الجدول قيد العمل.'
      : 'This option includes retained courses from the working timetable.',
    // "Term 2, 1449": the year is a left-to-right island of its own.
    gradTerm: (year, term) => (IS_AR ? [`الفصل ${term}، `, bdi(year)] : [`Term ${term}, `, bdi(year)]),
    gradTermWord: (n) => (IS_AR
      ? (n === 1 ? 'فصل' : n === 2 ? 'فصلان' : n <= 10 ? 'فصول' : 'فصلًا')
      : (n === 1 ? 'term' : 'terms')),
    gradTermCount: (n) => (IS_AR
      ? (n === 1 ? 'فصل واحد' : n === 2 ? 'فصلان' : `${n} ${n <= 10 ? 'فصول' : 'فصلًا'}`)
      : `${n} ${n === 1 ? 'term' : 'terms'}`),
    gradTiming: (delta) => {
      const n = Math.abs(delta);
      if (!IS_AR) return `${n} ${n === 1 ? 'term' : 'terms'} ${delta < 0 ? 'sooner' : 'later'}`;
      let label = `بـ${n} ${n <= 10 ? 'فصول' : 'فصلًا'}`;
      if (n === 1) label = 'بفصل واحد';
      if (n === 2) label = 'بفصلين';
      return delta < 0 ? `أبكر ${label}` : `أبعد ${label}`;
    },
    gradAssumes: (planning, cap) => (IS_AR
      ? ['يشمل هذا الفصل (', bdi(planning), ') · يفترض اجتياز المقررات · حتى ', `${cap} ${cap === 1 ? 'ساعة' : cap === 2 ? 'ساعتان' : cap <= 10 ? 'ساعات' : 'ساعة'}`, ' في كل فصل لاحق']
      : ['Includes this term (', bdi(planning), ') · assumes courses are passed · up to ', `${cap} cr`, ' per future term']),
    gradCompletes: IS_AR ? '، يكتمل في ' : ', completes ',
    gradSep: IS_AR ? '، ' : ', ',
    // Option names, from the option's own week: "Mornings only", "Two afternoons", "Five lighter days".
    optMornings: IS_AR ? 'صباحي فقط' : 'Mornings only',
    optAfternoons: (n) => (IS_AR
      ? pluralAr(n, forms('يوم واحد بعد الظهر', 'يومان بعد الظهر', '{n} أيام بعد الظهر', '{n} يومًا بعد الظهر'))
      : (n === 1 ? 'One afternoon' : `${NUMBER_EN[n] || n} afternoons`)),
    optLighter: (n) => (IS_AR
      ? pluralAr(n, forms('يوم واحد أخف', 'يومان أخف', '{n} أيام أخف', '{n} يومًا أخف'))
      : (n === 1 ? 'One lighter day' : `${NUMBER_EN[n] || n} lighter days`)),
    suggested: IS_AR ? 'المقترح' : 'Suggested',
    // The guidance column.
    gWatch: IS_AR ? 'انتبه' : 'Watch',
    gStatus: IS_AR ? 'الحالة' : 'Status',
    gDepends: IS_AR ? 'يعتمد على' : 'Depends on',
    // What one course does to the forecast, said only when it changes graduation:
    // "If postponed: 1 term later" / "1 term sooner" (a tag in the row's meta area).
    ifPostponed: (delta) => `${IS_AR ? 'إن أُجِّل: ' : 'If postponed: '}${T.gradTiming(delta)}`,
    viewOption: (name) => (IS_AR ? ['عرض ', bdi(name)] : ['View ', bdi(name)]),
    whyOnly: IS_AR ? 'الجدول الوحيد الذي وجده هذا البناء.' : 'The only timetable this build found.',
    whyAllCourses: IS_AR ? 'الخيار الوحيد الذي يجدول كل المقررات.' : 'The only option that schedules every course.',
    // Why the suggested option comes before the next one: what the two share, then the key that
    // decided, with both values. "Same terms and days on campus as B1; ends earlier: 14:00
    // against 14:15."
    whyDecided: (name, shared, decider, mine, theirs) => {
      const word = IS_AR
        ? { terms: 'عدد الفصول', days: 'أيام الحضور', end: 'وقت الانتهاء' }
        : { terms: 'terms', days: 'days on campus', end: 'finish time' };
      const clause = IS_AR
        ? { unplaced: 'مقررات غير مجدولة أقل', terms: 'فصول أقل', days: 'أيام حضور أقل', end: 'ينتهي أبكر', gap: 'فجوات أقصر' }
        : { unplaced: 'fewer courses left out', terms: 'fewer terms', days: 'fewer days on campus', end: 'ends earlier', gap: 'shorter gaps' };
      const against = IS_AR ? ' مقابل ' : ' against ';
      const values = [': ', ...mine, against, ...theirs, '.'];
      const list = shared.map((key) => word[key]);
      if (!list.length) {
        const opening = clause[decider];
        return IS_AR
          ? [`${opening} من `, bdi(name), ...values]
          : [`${opening.charAt(0).toUpperCase()}${opening.slice(1)} than `, bdi(name), ...values];
      }
      const joined = IS_AR
        ? list.join(' و')
        : (list.length > 1 ? `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}` : list[0]);
      return IS_AR
        ? ['مثل ', bdi(name), ` في ${joined}؛ ${clause[decider]}`, ...values]
        : [`Same ${joined} as `, bdi(name), `; ${clause[decider]}`, ...values];
    },
    whyNoGap: IS_AR ? 'بلا فجوات' : 'none',
    whyLevel: (name) => (IS_AR
      ? ['يتساوى مع ', bdi(name), ' في كل المعايير؛ وقد ورد في نتيجة البناء قبله.']
      : ['Level with ', bdi(name), ' on every count; the build listed it first.']),
    whyTimed: (name) => (IS_AR
      ? ['له مواعيد محددة، بخلاف ', bdi(name), '.']
      : ['It has timed classes; ', bdi(name), ' has none.']),
    seatLine: (label, n) => [bdi(label), IS_AR ? ': ' : ' has ', T.seatsLeft(n)],
    fallbackLine: (name, facts) => [bdi(name), IS_AR ? ' هو البديل: ' : ' is the fallback: ', ...facts, '.'],
    allNearlyFull: IS_AR ? 'كل الخيارات تستخدم شعبة شبه ممتلئة.' : 'Every option uses a section that is nearly full.',
    statusScheduled: (done, total) => (IS_AR
      ? (done === total ? `${done} من ${total} مقررات مجدولة` : `${done} من ${total} مجدولة`)
      : (done === total ? `${done} of ${total} courses scheduled` : `${done} of ${total} scheduled`)),
    statusNone: IS_AR ? 'لا مقررات جديدة في هذا البناء' : 'No new courses in this build',
    statusLeftOut: IS_AR ? 'غير مجدولة: ' : 'not scheduled: ',
    statusClean: IS_AR ? 'بلا تعارضات' : 'no conflicts',
    statusClash: (n) => (IS_AR
      ? pluralAr(n, forms('يتعارض لقاء واحد مع لقاء مسجّل', 'يتعارض لقاءان مع لقاءات مسجّلة', 'تتعارض {n} لقاءات مع لقاءات مسجّلة', 'يتعارض {n} لقاءً مع لقاءات مسجّلة'))
      : (n === 1 ? '1 class clashes with a registered one' : `${n} classes clash with registered ones`)),
    detailsWord: IS_AR ? 'التفاصيل' : 'details',

    // The registered courses as rows of Your plan (S7). A row the registrar holds is "Registered";
    // one held only as a forecast (no registrar rows, an earlier apply) is "Expected".
    regTag: IS_AR ? 'مسجّل' : 'Registered',
    expTag: IS_AR ? 'متوقع' : 'Expected',
    // Its section control in Keep says the section ("§M1"); the row's own tag says "Registered", and
    // the control's name says both ("CS301: §M1 · registered"), so the name keeps its room.
    regSection: (label, word) => `§${label} · ${IS_AR ? word : word.toLowerCase()}`,
    // Its quiet tag once editable: "Registered §M1".
    regTagSec: (word, label) => [word, ...(label ? [' ', bdi(`§${label}`)] : [])],
    // A registered course with no section, or one with no usable meetings: counted, never placed.
    loadOnlyNote: IS_AR ? 'ساعاته محتسبة، بلا موعد' : 'counted, no timetable',
    planHeadReg: (reg, added) => [IS_AR ? 'خطتك' : 'Your plan',
      IS_AR ? `${reg} مسجّلة + ${added} مضافة` : `${reg} registered + ${added} added`],
    convLead: IS_AR ? 'أصبحت المقررات المسجّلة قابلة للتعديل (تجاهل). ' : 'Registered courses are now editable (Ignore). ',
    convSection: (code) => (IS_AR ? ['اختر شعبة للمقرر ', bdi(code), '.'] : ['Choose a section for ', bdi(code), '.']),
    convNotRequired: (code) => (IS_AR ? [bdi(code), ' لم يعد إلزاميًا.'] : [bdi(code), ' is no longer required.']),
    convCleared: IS_AR ? 'خطتك فارغة: ابدأ من جديد.' : 'Your plan is empty: start from scratch.',
    convManual: IS_AR ? 'يمكنك تعديل أي منها أو إضافة مقررات.' : 'Change any of them, or add courses.',
    convBar: IS_AR ? 'المقررات المسجّلة قابلة للتعديل الآن' : 'Registered courses are editable now',
    changesWord: (n) => (IS_AR
      ? pluralAr(n, forms('تغيير واحد', 'تغييران', '{n} تغييرات', '{n} تغييرًا'))
      : `${n} ${n === 1 ? 'change' : 'changes'}`),
    backToKeep: IS_AR ? 'العودة إلى الإبقاء' : 'Back to Keep',
    backToKeepDrops: (n) => (IS_AR ? `العودة إلى الإبقاء (تلغي ${T.changesWord(n)})` : `Back to Keep (drops ${T.changesWord(n)})`),
    backDone: IS_AR ? 'أُبقيت المقررات المسجّلة كما سُجّلت.' : 'The registered courses are kept as registered again.',
    // The one confirm, by what it asks about.
    askMode: (n) => (IS_AR
      ? `العودة إلى الإبقاء تلغي ${T.changesWord(n)} في المقررات المسجّلة، وتبقى المقررات التي أضفتها.`
      : `Back to Keep drops ${T.changesWord(n)} to the registered courses. The courses you added stay.`),
    askModeGo: IS_AR ? 'إلغاء التغييرات' : 'Drop the changes',
    askModeKeep: IS_AR ? 'متابعة التعديل' : 'Keep editing',
    askEligConverted: IS_AR
      ? 'تغيير قاعدة المتطلبات السابقة يعيد تحميل المقررات المقترحة، ويزيل المقررات التي أضفتها، ويعيد المقررات المسجّلة كما سُجّلت.'
      : 'Changing the prerequisite rule reloads the recommended courses, clears the courses you added and keeps the registered courses as registered again.',
    askBoth: (n) => (IS_AR
      ? `الاستعادة تعيد القواعد المعتادة: يعاد تحميل المقررات المقترحة، وتزال المقررات التي أضفتها، وتلغى التغييرات على المقررات المسجّلة (${T.changesWord(n)}).`
      : `Reset puts back the usual rules: the recommended courses reload, the courses you added are cleared and ${T.changesWord(n)} to the registered courses ${n === 1 ? 'is' : 'are'} dropped.`),
    askBothGo: IS_AR ? 'استعد وامسح' : 'Reset and clear',
    askKeepCurrent: IS_AR ? 'إبقاء الحالي' : 'Keep current',
    // The section menu of an editable registered course.
    ownSection: IS_AR ? 'شعبته المسجّلة' : 'Registered section',
    clashRequired: (label) => (IS_AR ? ['يتعارض مع ', bdi(label), ' (إلزامي)'] : ['Clashes with ', bdi(label), ' (required)']),
    loadingSections: IS_AR ? 'جارٍ تحميل الشُعب…' : 'Loading the sections…',
    unrequire: (codes) => [IS_AR ? 'لا يمكن إدراج كل المقررات الإلزامية معًا: ' : 'Required courses that cannot all be placed: ',
      ...codes.flatMap((c, i) => (i ? [IS_AR ? '، ' : ', ', bdi(c)] : [bdi(c)])),
      IS_AR ? '. أزل علامة «إلزامي» عن المقررات التي يجوز للبناء تغييرها.' : '. Turn Required off on those the build may move.'],
    // What an option changes against the registration.
    chgLead: IS_AR ? 'التغييرات: ' : 'Changes: ',
    chgNone: IS_AR ? 'لا تغيير على التسجيل' : 'No change to the registration',
    chg: {
      moved: (c) => [bdi(c.code), ' ', bdi(`§${c.from}`), IS_AR ? ' ‹ ' : ' › ', bdi(`§${c.to}`)],
      dropped: (c) => [bdi(c.code), IS_AR ? ' أُزيل' : ' dropped'],
      notPlaced: (c) => [bdi(c.code), IS_AR ? ' لم يوضع' : ' not placed'],
      added: (c) => [bdi(`+${c.code}`)],
    },
    applyConverted: IS_AR ? 'يحفظ هذا الخطة كما بُنيت، ولا يتغيّر جدول التسجيل الرسمي.' : "This saves the plan as built. The registrar's timetable is unchanged.",
    ownBlock: IS_AR ? 'الشعبة المسجّلة' : 'registered section',
    // The credits line when the registered load is above the limit set.
    regOver: (n) => (IS_AR
      ? `المسجّل يزيد على الحد: ${T.credits(n)}، لا يتسع إلا لاستبدال مقرر بآخر بالساعات نفسها`
      : `registered courses are ${n} cr over the limit: only a swap for the same credits fits`),
    capRaised: (limit) => (IS_AR
      ? `الحد ${T.credits(limit)}، وهو المسجّل: يتسع لاستبدال مقرر بآخر بالساعات نفسها`
      : `the limit stands at ${limit} cr, the registered load: a swap for the same credits fits`),
  };
  const NUMBER_EN = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven'];

  const DAY_ORDER = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
  const ACADEMIC_DAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU'];
  const DAY_SHORT = IS_AR
    ? { SUN: 'أحد', MON: 'اثن', TUE: 'ثلا', WED: 'أرب', THU: 'خم', FRI: 'جمع', SAT: 'سبت' }
    : { SUN: 'Sun', MON: 'Mon', TUE: 'Tue', WED: 'Wed', THU: 'Thu', FRI: 'Fri', SAT: 'Sat' };
  const DAY_FULL_EN = { SUN: 'Sunday', MON: 'Monday', TUE: 'Tuesday', WED: 'Wednesday', THU: 'Thursday', FRI: 'Friday', SAT: 'Saturday' };
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
    filledBy: {},      // elective row code -> {course_code, basis}: the registered course it likely counts for
    planCourses: null, // null = not loaded / failed; [] = loaded, nothing
    planState: 'idle', // idle | loading | ready | error
    curTT: { digest: null, drawn: null }, // the registered week the disclosure last drew
    bandEnds: '',      // the completion term the student band shows
    planCredits: null, // {done, total}: the plan's credits passed and in all, once the plan reply is in
    catalog: null,     // Map normalised course code -> [section]; null = unknown
    catState: 'idle',  // the section catalogue: idle | loading | ready | error
    shortlist: [],
    // Ignore with the registered courses as rows (S7). `converted` is null in Keep; once a control of
    // a registered row is used it holds the registered courses as they were ({regs}), so a change
    // can be counted and Back to Keep can drop it. `loadOnly`: the registered courses with no
    // usable section, kept OUT of the shortlist (counted in every credit figure, never placed).
    converted: null,
    loadOnly: [],
    pickTab: 'avail',  // phones: which picker panel shows (avail | plan)
    hues: {},          // this student's course colours: code -> 1..HUES, kept while the plan changes
    rules: { ...DEFAULT_RULES },
    shownElig: null,   // the eligibility the recommendations on screen were loaded with
    defaults: { ...DEFAULT_RULES },
    focus: null,       // {kind, code}: where focus goes after the next redraw
    // The build loop. `build` is null until a reply is accepted, and again when another student
    // loads or a save lands: {options, shortlist, rules, at, madeAt, stale, ...}. A change to the
    // plan or the rules keeps it and sets `stale` ('changed'; 'old' after 30 minutes).
    build: null,
    selected: null,    // name of the option tab on screen; null = the Registered tab
    buildGen: 0,       // bumped whenever a reply in flight must no longer be believed
    inflight: 0,       // build requests not yet answered: the button waits for all of them
    applying: false,   // a save request is in flight
    fetchKeyboard: false, // Fetch had the keyboard when a load disabled it (see releaseFetch)
    maxDirty: false,   // the max-credits field was typed in since it last showed the applied value
    moreOpen: false,   // the "More options" disclosure
    detail: null,      // {code, name, credits, only}: the planned course whose section popover is open
    pick: null,        // the picker's own state (source, search, ticks, open rows); see newPick
    allCatalog: null,  // Map course key -> [section]: every course offered this term (source 4); null = not loaded
    allState: 'idle',  // idle | loading | ready | error
    allGen: 0,         // whole-term catalogue requests: only the newest may write
    altShown: null,    // what the week drew as dashed blocks last (see syncAlt)
    whyOpen: false,    // the "Why these options" disclosure
    gradWhy: false,    // the "Course-level reasons" disclosure of the projection band
  };
  const STALE_AFTER_MS = 30 * 60 * 1000;

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
  // Appending null to a node writes the word "null" on the page. Only `el()` skips empty children,
  // so optional children handed straight to a node's append or replaceChildren go through this.
  const present = (...list) => list.filter((kid) => kid !== null && kid !== undefined && kid !== false);
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
    chev: [['path', { d: 'M6 9l6 6 6-6' }]],
    next: [['path', { d: 'M9 6l6 6-6 6' }]],
    nextAll: [['path', { d: 'M5 6l6 6-6 6M12 6l6 6-6 6' }]],
    pin: [['path', { d: 'M9 4h6l-1 6 3 3H7l3-3-1-6z' }], ['path', { d: 'M12 13v7' }]],
    tick: [['path', { d: 'M5 12.5l4.5 4.5L19 7.5' }]],
    warn: [['path', { d: 'M12 4l9 16H3z' }], ['path', { d: 'M12 10v4.5M12 17.2v.3' }]],
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
  // A section's meetings, one group per distinct start and end: the days that share it (week order)
  // and the range, earliest first. A meeting with no usable time keeps its day in a group of its own.
  function meetingGroups(meetings) {
    const byTime = new Map();
    for (const m of meetings || []) {
      const st = toMins(m.start_time);
      const en = toMins(m.end_time);
      const range = st !== null && en !== null ? `${hhmm(st)}-${hhmm(en)}` : '';
      const day = normDay(m.day);
      if (!range && !day) continue;
      if (!byTime.has(range)) byTime.set(range, { range, start: range ? st : 1e9, days: [] });
      const group = byTime.get(range);
      if (day && !group.days.includes(day)) group.days.push(day);
    }
    const rank = (day) => DAY_ORDER.indexOf(day);
    for (const group of byTime.values()) group.days.sort((a, b) => rank(a) - rank(b));
    return [...byTime.values()].sort((a, b) => a.start - b.start || rank(a.days[0]) - rank(b.days[0]));
  }
  // "Sun 08:00-09:15 · Tue 10:00-11:15"; one group reads "Mon Wed 10:00-11:15". Times are isolated.
  function whenNodes(meetings) {
    const parts = [];
    meetingGroups(meetings).forEach((group, i) => {
      if (i) parts.push(' · ');
      const days = group.days.map((d) => DAY_SHORT[d]).join(' ');
      parts.push(...present(days || null, days && group.range ? ' ' : null, group.range ? bdi(group.range) : null));
    });
    return parts;
  }
  const programmesOf = (section) => [...new Set(((section && section.programs) || []).map((p) => String(p).trim()).filter(Boolean))];
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
  // 1 to SEAT_WARNING seats left. An unknown capacity is never a warning, and a full one is not "few".
  const fewSeats = (cap) => cap.known && cap.remaining >= 1 && cap.remaining <= SEAT_WARNING;
  // A duration never breaks across two lines ("1 س 45 د" stays together).
  function nb(text) {
    const node = document.createElement('span');
    node.className = 'plb-nb';
    node.textContent = String(text);
    return node;
  }
  function findSection(tsid) {
    if (tsid === null || tsid === undefined) return null;
    for (const map of [state.catalog, state.allCatalog]) {
      if (!map) continue;
      for (const list of map.values()) {
        for (const section of list) if (String(section.term_section_id) === String(tsid)) return section;
      }
    }
    return null;
  }
  function durationText(mins) {
    if (mins < 60) return T.minutes(mins);
    return T.hoursMinutes(Math.floor(mins / 60), mins % 60);
  }

  /* ── Course names: one helper decides what the board calls a course ──────── */
  // Two names reach the board. The curriculum's (English): the plan's rows, the recommendations,
  // and the items made from them. The registrar's section name (Arabic): the registered rows and
  // the sections of the catalogue. Shown side by side they mixed languages in one list, so every
  // name on the board (Your plan, Available, at a glance, the popover, the week's labels) comes
  // from here: an English screen shows the curriculum name when it is known, else the section
  // name; an Arabic screen the section name when it is known, else the curriculum name. A "name"
  // that is only the course's code is not a name.
  const NAMES = { refs: null, curriculum: new Map(), section: new Map() };
  function nameMaps() {
    const refs = [state.planCourses, state.recs, state.baseline, state.catalog, state.allCatalog];
    if (NAMES.refs && refs.every((ref, i) => ref === NAMES.refs[i])) return NAMES;
    const curriculum = new Map();
    const section = new Map();
    const put = (map, code, name) => {
      const key = norm(code).split('::')[0];
      const text = String(name || '').trim();
      if (key && text && norm(text) !== key && !map.has(key)) map.set(key, text);
    };
    for (const course of state.planCourses || []) put(curriculum, course.code, course.name);
    for (const rec of state.recs) put(curriculum, rec.course_code, rec.course_name);
    for (const row of state.baseline) put(section, courseKeyOf(row), row.course_name);
    for (const map of [state.catalog, state.allCatalog]) {
      if (!map) continue;
      for (const [key, list] of map) for (const sec of list) put(section, key, sec.course_name);
    }
    Object.assign(NAMES, { refs, curriculum, section });
    return NAMES;
  }
  function courseName(code, fallback) {
    const maps = nameMaps();
    const key = norm(code).split('::')[0];
    const curriculum = maps.curriculum.get(key) || '';
    const section = maps.section.get(key) || '';
    return (IS_AR ? section || curriculum : curriculum || section) || String(fallback || '').trim();
  }
  // The name node of a list row: an isolated name, or nothing.
  const nameNode = (cls, code, fallback) => {
    const name = courseName(code, fallback);
    return el('span', cls, name ? isolate(name) : '');
  };

  /* ── The one status line ───────────────────────────────────────────────── */
  // A conversion of the registered courses is said once, in front of the change that made it
  // (convertRegistered sets this; the next message carries it).
  let convLead = false;
  function setStatus(kind, ...parts) {
    const line = $('plbStatus');
    const lead = convLead && parts.length ? [T.convLead] : [];
    convLead = false;
    line.dataset.kind = kind || '';
    line.replaceChildren(...[...lead, ...parts].map((p) => (typeof p === 'string' ? document.createTextNode(p) : p)));
  }

  /* ── Rules ─────────────────────────────────────────────────────────────── */
  function changedRuleKeys() {
    return Object.keys(DEFAULT_RULES).filter((key) => state.rules[key] !== state.defaults[key]);
  }
  function renderRules() {
    const r = state.rules;
    // The controls on the board show the rules in force.
    $('plbProgOnly').checked = r.programOnly;
    $('plbProgAny').checked = !r.programOnly;
    $('plbFullIn').checked = r.allowFull;
    $('plbSwapIn').checked = r.swap;
    $('plbRegKeep').checked = r.mode === 'keep';
    $('plbRegIgnore').checked = r.mode === 'ignore';
    $('plbEligStrict').checked = r.eligibility === 'strict';
    $('plbEligRelaxed').checked = r.eligibility === 'relaxed';
    const field = $('plbMaxIn');
    if (!state.maxDirty) {
      field.value = r.maxCredits === null ? '' : String(r.maxCredits);
      field.removeAttribute('aria-invalid');
      $('plbMaxInErr').hidden = true;
      $('plbMaxInErr').textContent = '';
    }
    const changed = changedRuleKeys().length;
    const badge = $('plbRulesChanged');
    badge.hidden = changed === 0;
    badge.textContent = changed ? T.rulesChanged(changed) : '';
    $('plbRulesUsual').hidden = changed !== 0;
  }
  const regulatoryMax = () => (state.student && finitePositive(state.student.regulatory_max_credits)) || 99;
  // Max credits as typed: '' is "not set" (null); a whole number from 1 to the regulatory maximum;
  // anything else is refused.
  function parseMaxCredits(raw) {
    // Arabic-Indic and Persian digits are read as the digits they are.
    const text = raw.trim().replace(/[\u0660-\u0669\u06F0-\u06F9]/g, (c) => String(c.charCodeAt(0) & 15))
      // Leading zeros are not part of the number ("018" is 18).
      .replace(/^0+(?=\d)/, '');
    if (text === '') return { ok: true, value: null };
    const ceiling = regulatoryMax();
    const value = /^\d{1,2}$/.test(text) ? Number(text) : NaN;
    if (!Number.isInteger(value) || value < 1 || value > ceiling) return { ok: false, ceiling };
    return { ok: true, value };
  }

  // What a change of rules does, whichever control made it. `quiet`: the caller redraws the
  // courses itself, once (a conversion of the registered courses).
  function commitRules(next, quiet) {
    const before = { ...state.rules };
    closeEligConfirm(false);
    state.rules = next;
    renderRules();
    // Options built under other rules are not options for these: they stay, marked out of date.
    if (Object.keys(DEFAULT_RULES).some((key) => state.rules[key] !== before[key])) planChanged();
    // The other rules apply at once, whatever happens to the reload below.
    if (!quiet) renderCourses();
    // The registered week loses its lock in Ignore (and the legend says it).
    if (state.ctx && state.rules.mode !== before.mode) renderWeek();
    if (state.ctx && (state.rules.programOnly !== before.programOnly || state.rules.mode !== before.mode)) {
      loadCatalog(state.gen);
    }
    // The recommendations depend on eligibility: reload them, as the current page does. If no reload
    // lands, loadStudent puts back the eligibility the page was loaded with.
    if (state.ctx && state.rules.eligibility !== before.eligibility) loadStudent({ reuse: true });
  }
  // A switch on the board.
  function changeRuleInPlace(key, value) {
    if (state.rules[key] === value) return;
    // Keep and Ignore move the registered courses: Ignore makes them rows of the plan (the same
    // conversion as a change to one of them), Keep drops those rows again (and asks first when
    // any of them was changed).
    if (key === 'mode' && state.ctx) {
      if (value === 'ignore') {
        closeEligConfirm(false);
        convertRegistered();
        renderCourses();
        renderActions();
        // Said only when there were registered courses to make editable.
        if (state.converted) setStatus('ok', T.convManual);
      } else requestBackToKeep('switch');
      return;
    }
    // The reload clears Your plan back to the registered courses: when the adviser has added
    // courses or changed a registered one, an inline confirm says so and asks. The switch keeps
    // showing the rule in force until "Change and clear".
    if (key === 'eligibility' && state.ctx && isDirty()) {
      renderRules();
      openEligConfirm('elig', value);
      return;
    }
    closeEligConfirm(false);
    commitRules({ ...state.rules, [key]: value });
  }
  // The one confirm of the rules zone. `confirmKind`: what it asks about ('elig': a reload of the
  // recommendations; 'mode': Back to Keep with changes; 'both': Reset, which does the two).
  // `eligPending`: the eligibility asked about; `eligReset`: the question came from "Reset", so Go
  // puts back every usual rule, not only the eligibility. Null/false while it is closed.
  let eligPending = null;
  let eligReset = false;
  let confirmKind = null;
  // The template's own words (the eligibility question), kept for when it asks that again.
  let eligWords = null;
  // Escape closes the confirm wherever the keyboard is while it is open.
  function eligEscape(event) {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    closeEligConfirm(true);
  }
  function openEligConfirm(kind, value, reset) {
    // One question at a time: the notice bar's own Back to Keep question closes.
    closeConvAsk(false);
    if (!eligWords) {
      eligWords = { text: $('plbEligConfirmText').textContent, go: $('plbEligGo').textContent, keep: $('plbEligKeep').textContent };
    }
    confirmKind = kind;
    eligPending = value;
    eligReset = !!reset;
    const n = changesCount();
    let words = eligWords;
    if (kind === 'mode') words = { text: T.askMode(n), go: T.askModeGo, keep: T.askModeKeep };
    else if (kind === 'both') words = { text: T.askBoth(n), go: T.askBothGo, keep: T.askKeepCurrent };
    else if (state.converted) words = { ...eligWords, text: T.askEligConverted };
    $('plbEligConfirmText').textContent = words.text;
    $('plbEligGo').textContent = words.go;
    $('plbEligKeep').textContent = words.keep;
    $('plbEligConfirm').dataset.kind = kind;
    $('plbEligConfirm').hidden = false;
    document.addEventListener('keydown', eligEscape);
    $('plbEligGo').focus();
  }
  // `focusBack`: the keyboard returns to the switch asked about (Keep or Escape). Any other change
  // closes the confirm and leaves the keyboard where it is.
  function closeEligConfirm(focusBack) {
    const box = $('plbEligConfirm');
    if (box.hidden) return;
    const kind = confirmKind;
    box.hidden = true;
    eligPending = null;
    eligReset = false;
    confirmKind = null;
    document.removeEventListener('keydown', eligEscape);
    if (focusBack) {
      const on = document.querySelector(kind === 'mode' ? 'input[name="plbRegIn"]:checked' : 'input[name="plbEligIn"]:checked');
      if (on) on.focus();
    }
  }
  function goEligConfirm() {
    const kind = confirmKind;
    if (!kind) return;
    const value = eligPending;
    const reset = eligReset;
    const wasInside = $('plbEligConfirm').contains(document.activeElement);
    closeEligConfirm(false);
    if (kind === 'mode') {
      backToKeep(true);
      if (reset) commitRules({ ...state.defaults });
      return;
    }
    // The reload ends in Keep with the registered courses as registered (loadStudent), and only
    // once it lands: a reload that fails leaves the plan as it was. Reset puts back Keep itself,
    // so it drops an edited plan at once.
    if (reset && state.converted) backToKeep(false);
    commitRules(reset ? { ...state.defaults } : { ...state.rules, eligibility: value });
    if (wasInside) {
      const on = document.querySelector('input[name="plbEligIn"]:checked');
      if (on) on.focus();
    }
  }
  // "Reset": back to the usual rules, Keep among them. Putting back another eligibility reloads the
  // student, which clears what the adviser added or changed, so then it asks first, as the switch
  // does; going back to Keep with changes to the registered courses asks the same as Back to Keep.
  function resetRules() {
    const reload = state.ctx && state.defaults.eligibility !== state.rules.eligibility;
    const changes = state.converted ? changesCount() : 0;
    if (reload && isDirty()) {
      openEligConfirm(changes ? 'both' : 'elig', state.defaults.eligibility, true);
      return;
    }
    if (changes) {
      renderRules();
      openEligConfirm('mode', null, true);
      return;
    }
    closeEligConfirm(false);
    if (state.converted) backToKeep(false);
    commitRules({ ...state.defaults });
  }
  function commitMaxInPlace() {
    const field = $('plbMaxIn');
    const max = parseMaxCredits(field.value);
    const err = $('plbMaxInErr');
    if (!max.ok) {
      err.textContent = T.maxError(max.ceiling);
      err.hidden = false;
      field.setAttribute('aria-invalid', 'true');
      return;
    }
    field.removeAttribute('aria-invalid');
    err.hidden = true;
    err.textContent = '';
    if (max.value !== state.rules.maxCredits) commitRules({ ...state.rules, maxCredits: max.value });
    // What was applied is what the field says ("018" becomes "18").
    field.value = max.value === null ? '' : String(max.value);
    state.maxDirty = false;
  }

  /* ── Student ───────────────────────────────────────────────────────────── */
  // The header shows the term as one button; Year and Semester sit behind it.
  function renderTermBtn() {
    const year = $('plbYear').value.trim() || '—';
    const term = $('plbTerm').value;
    $('plbTermBtn').replaceChildren(...T.termButton(year, term), svg(ICONS.chev, 12));
    $('plbKicker').replaceChildren(`${T.kicker} · `, ...T.termButton(year, term));
  }
  function setTermPop(open, focusBack) {
    $('plbTermPop').hidden = !open;
    $('plbTermBtn').setAttribute('aria-expanded', String(open));
    if (open) $('plbYear').focus();
    else if (focusBack) $('plbTermBtn').focus();
  }
  function renderContextChip() {
    const chip = $('plbContext');
    if (!state.ctx) { chip.hidden = true; return; }
    chip.replaceChildren(bdi(`${state.ctx.studentId} · ${state.ctx.year}/${state.ctx.term}`));
    chip.hidden = false;
  }
  // One fact of the student band: a mono label, a big number, a small line.
  function fact(label, big, ...rest) {
    return el('div', 'plb-sf', el('span', 'plb-mic', label), big, ...rest);
  }
  // Plan credits passed over the plan's total, from the plan reply; null while it is loading or failed.
  function planProgress() {
    return state.planState === 'ready' && state.planCredits && state.planCredits.total > 0 ? state.planCredits : null;
  }
  // The completion term the band's last fact shows ('' before there is one).
  function bandEndsKey() {
    const row = projectionOf() ? projectionOf().baseline : null;
    return isComplete(row) ? `${row.completion_academic_year}/${row.completion_term}` : '';
  }
  function renderStudent() {
    const host = $('plbStudent');
    const st = state.student;
    if (!st) {
      host.replaceChildren(el('p', 'plb-empty', el('strong', '', T.noStudentTitle), el('br'), T.noStudentHint));
      return;
    }
    const gpa = finiteNonNegative(st.gpa);
    // This term's registered credits, counted from the timetable on screen. The reply's
    // `registered_credits` is the registrar's running total (144 for a senior): beside an
    // 18-credit cap it reads as a mistake, and it is not what this screen is about.
    const registered = registeredCredits();
    const courses = registeredCourses();
    const advisor = st.advisor_name ? String(st.advisor_name).trim() : '';
    const progress = planProgress();
    const baseline = projectionOf() ? projectionOf().baseline : null;
    const ends = isComplete(baseline) ? completionParts(baseline) : [];
    state.bandEnds = bandEndsKey();
    host.replaceChildren(
      fact(T.programme,
        el('div', 'plb-big', st.program ? bdi(st.program) : '—'),
        el('div', 'plb-smeta', st.name ? isolate(st.name) : T.unnamed, ' · ', bdi(st.student_id),
          advisor ? ` · ${T.advisor} ` : null, advisor ? isolate(advisor) : null)),
      fact(T.bandProgress,
        el('div', 'plb-big', ...(progress
          ? [bdi(`${progress.done} / ${progress.total}`), ' ', el('span', 'plb-big-u', T.creditUnit(progress.total))]
          : ['—'])),
        progress ? (() => {
          const bar = el('div', 'plb-bar', el('i'));
          bar.firstChild.style.inlineSize = `${Math.min(100, Math.round((progress.done / progress.total) * 100))}%`;
          bar.setAttribute('aria-hidden', 'true');
          return bar;
        })() : null,
        el('div', 'plb-smeta', `${T.gpa} `, gpa === null ? '—' : gpa.toFixed(2))),
      fact(T.bandRegistered,
        el('div', 'plb-big', registered === null ? '—' : String(registered)),
        // The codes themselves are on the Current timetable line just below: not repeated here.
        el('div', 'plb-smeta', T.courseWord(courses.length ? new Set(courses.map((c) => c.key)).size : 0))),
      fact(T.bandGraduates,
        ends.length ? el('div', 'plb-big', ...ends) : el('div', 'plb-big plb-big--muted', T.afterFirstBuild)),
    );
  }

  /* ── The registered week: model, tab facts and grid ────────────────────── */
  // The registrar's own sources (core/services/timetable_snapshots.py REGISTRAR_SOURCES): a row of
  // any other source is a forecast (an expected plan, or sections saved from this page).
  const REGISTRAR_SOURCES = new Set(['scraper_timetable', 'fallback_studying']);
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
          // For the rows of Your plan: the section the student holds, whether a build can place it
          // (every meeting has a day and a start before its end), and who says so.
          tsid: null,
          complete: true,
          fallback: false,
          registrar: false,
        });
      }
      const entry = seen.get(key);
      if (entry.credits === null) entry.credits = finiteNonNegative(row.credits);
      const st = toMins(row.start_time);
      const en = toMins(row.end_time);
      if (normDay(row.day) && st !== null && en !== null) entry.timed = true;
      if (!(normDay(row.day) && st !== null && en !== null && en > st)) entry.complete = false;
      if (entry.tsid === null && row.term_section_id) entry.tsid = row.term_section_id;
      const source = String(row.source || '').trim().toLowerCase();
      if (source === 'fallback_studying' || !row.term_section_id) entry.fallback = true;
      if (REGISTRAR_SOURCES.has(source)) entry.registrar = true;
    }
    return [...seen.values()].map((entry) => ({ ...entry, complete: entry.complete && entry.tsid !== null }));
  }
  // Registered rows the registrar's timetable took from a fallback mapping: how many sections.
  function fallbackRows() {
    const keys = new Set();
    for (const row of state.baseline) {
      if (!String(row.source || '').toLowerCase().includes('fallback')) continue;
      keys.add(String(row.term_section_id || `${courseKeyOf(row)}|${row.section || ''}`));
    }
    return keys.size;
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
      // The section the student holds (an edited plan kept it): marked, in words too.
      const own = state.build && state.build.rules.mode === 'ignore'
        && (state.build.registered || []).some((c) => String(c.tsid) === String(mapping.term_section_id));
      for (const mm of mapping.meetings || []) {
        const day = normDay(mm.day);
        const st = toMins(mm.start_time);
        const en = toMins(mm.end_time);
        if (!day || st === null || en === null || en <= st) continue;
        meetings.push({
          day, st, en,
          code: shownCode(key || String(mapping.course_code || '')),
          section: String(mapping.section || '').trim(),
          key,
          kind: 'new',
          own,
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
        ...(model.longestGap > 0 ? [`${T.gap} `, nb(durationText(model.longestGap))] : [T.noGap]),
      ],
    };
  }
  function renderTab(model) {
    const credits = registeredCredits();
    $('plbTabCredits').textContent = credits === null || !state.ctx ? '' : T.credits(credits);
    // The registered week is locked only while it is kept (Ignore: the registrar's classes, no lock).
    $('plbTabRegistered').querySelector('.plb-tab-lock').hidden = state.rules.mode !== 'keep';
    const facts1 = $('plbTabFacts1');
    const facts2 = $('plbTabFacts2');
    if (!state.ctx) { facts1.textContent = '—'; facts2.textContent = ''; return; }
    const facts = factsOf(model);
    const projection = projectionOf();
    const terms = projection ? termsNode(projection.baseline) : null;
    facts1.replaceChildren(...present(terms, terms ? ' · ' : null, facts.first));
    facts2.replaceChildren(...facts.second);
  }
  // A planned course still in the plan opens its detail panel from the week; the registered ones
  // stay pictures.
  function plannedItem(m) {
    if (m.kind !== 'new') return null;
    return state.shortlist.find((c) => norm(c.course_code) === norm(m.key)) || null;
  }
  // One colour per course (planned and registered), given the first time the course appears for
  // this student and kept while other courses come and go; a hue freed by a removed course goes to
  // a NEW course only. Ten hues (--plb-c1..10): past ten courses the least-used hue is shared.
  const HUES = 10;
  function planKeys() {
    const keys = state.shortlist.map((c) => norm(c.course_code));
    for (const c of registeredCourses()) if (c.key && !keys.includes(norm(c.key))) keys.push(norm(c.key));
    if (state.build) for (const c of state.build.shortlist) if (!keys.includes(norm(c.course_code))) keys.push(norm(c.course_code));
    return keys;
  }
  function hueOf(code) {
    const key = norm(code);
    const keys = planKeys();
    if (!keys.includes(key)) return 0;
    for (const k of Object.keys(state.hues)) if (!keys.includes(k)) delete state.hues[k];
    if (!state.hues[key]) {
      // The first free hue; when all ten are taken, the one fewest courses share (the lowest of
      // those), never a count-based guess that can land on the most-used one.
      const uses = new Array(HUES + 1).fill(0);
      for (const hue of Object.values(state.hues)) if (hue >= 1 && hue <= HUES) uses[hue] += 1;
      let pick = 1;
      for (let hue = 2; hue <= HUES; hue += 1) if (uses[hue] < uses[pick]) pick = hue;
      state.hues[key] = pick;
    }
    return state.hues[key];
  }
  function swatch(code) {
    const mark = el('i', 'plb-sw');
    const hue = hueOf(code);
    if (hue) mark.dataset.hue = String(hue);
    mark.setAttribute('aria-hidden', 'true');
    return mark;
  }
  function weekBlock(m, rowStart, rowEnd, col, inert) {
    const rows = rowEnd - rowStart;
    const range = `${hhmm(m.st)}-${hhmm(m.en)}`;
    const item = inert ? null : plannedItem(m);
    const block = el(item ? 'button' : 'div', 'plb-b');
    if (item) {
      block.type = 'button';
      block.dataset.act = 'week-open';
      block.setAttribute('aria-controls', 'plbPop');
    }
    block.style.gridColumn = String(col);
    block.style.gridRow = `${rowStart} / ${rowEnd}`;
    block.dataset.span = String(rows);         // its height in rows, for fitBlocks()
    block.dataset.code = m.key;
    block.dataset.kind = m.kind || 'reg';
    if (m.clash) block.dataset.clash = 'true';
    if (hueOf(m.key)) block.dataset.hue = String(hueOf(m.key));
    const label = `${m.code}${m.section ? ` ${IS_AR ? 'شعبة' : 'section'} ${m.section}` : ''}, ${DAY_SHORT[m.day]} ${range}`
      + `${m.kind === 'new' ? `, ${T.plannedBlock}` : ''}${m.own ? `, ${T.ownBlock}` : ''}${m.clash ? `, ${T.clashBlock}` : ''}`;
    if (m.own) block.dataset.own = 'true';
    if (item) {
      const name = courseName(m.key, item.course_name);
      const said = `${m.code}${name ? ` ${name}` : ''}, ${T.detailsWord}`;
      block.setAttribute('aria-label', `${said}. ${label}`);
      block.setAttribute('aria-expanded', String(!!state.detail && state.detail.code === norm(m.key)));
      if (state.detail && state.detail.code === norm(m.key)) block.dataset.open = 'true';
    } else {
      block.setAttribute('role', 'img');
      block.setAttribute('aria-label', label);
    }
    // A registered class is locked while it is kept; in Ignore it is the registrar's class, no lock.
    const locked = m.kind !== 'new' && (inert || weekKeepsRegistered());
    block.append(...present(
      el('span', 'plb-cd', locked ? svg(ICONS.lock, 11) : null, bdi(m.code)),
      m.section ? el('span', 'plb-bs', bdi(`§${m.section}`)) : null,
      el('span', 'plb-bt', bdi(range))));
    return block;
  }
  // While the panel of a course in the plan is open, each ticked section of it that the week does not
  // already draw solid is drawn dashed (an alternative). A list of its own: it is never part of a
  // week's facts, a day's count, an option's name or the order of the tabs.
  function altMeetings(opt) {
    const d = state.detail;
    if (!d || !state.ctx) return [];
    const item = state.shortlist.find((c) => norm(c.course_code) === d.code);
    if (!item) return [];
    const used = opt ? mappingFor(opt, item.course_code) : null;
    const usedId = used ? String(used.term_section_id) : null;
    const out = [];
    for (const pin of pinsOf(item)) {
      const id = String(pin.term_section_id);
      if (id === usedId) continue;
      const section = findSection(id);
      if (!section) continue;
      for (const mm of section.meetings || []) {
        const day = normDay(mm.day);
        const st = toMins(mm.start_time);
        const en = toMins(mm.end_time);
        if (!day || st === null || en === null || en <= st) continue;
        out.push({ day, st, en, code: String(item.course_code), key: d.code, section: String(section.section || pinLabel(pin)).trim() });
      }
    }
    return out;
  }
  const altSignature = (opt, list) => `${opt ? opt.name : ''}|${list.map((m) => `${m.key}:${m.section}:${m.day}:${m.st}:${m.en}`).join(',')}`;
  // The week is drawn again only when what the open panel adds to it changed.
  function syncAlt() {
    if (!state.ctx) return;
    const opt = currentOption();
    if (altSignature(opt, altMeetings(opt)) !== state.altShown) renderWeek();
  }
  function altBlock(m, rowStart, rowEnd, col) {
    const block = el('div', 'plb-gb',
      el('span', 'plb-cd', bdi(m.code)),
      el('span', 'plb-bs', ...T.altTag(m.section)),
      el('span', 'plb-bt', bdi(`${hhmm(m.st)}-${hhmm(m.en)}`)));
    block.style.gridColumn = String(col);
    block.style.gridRow = `${rowStart} / ${rowEnd}`;
    block.dataset.span = String(rowEnd - rowStart);
    block.dataset.alt = 'true';
    block.dataset.code = m.key;
    block.setAttribute('aria-hidden', 'true');
    return block;
  }
  // Whether the week on screen keeps the registered classes (a lock on them): the build's mode on an
  // option's tab, the board's on the Registered tab.
  function weekKeepsRegistered() {
    const opt = currentOption();
    return opt ? state.build.rules.mode === 'keep' : state.rules.mode === 'keep';
  }
  function renderLegend(opt, alt) {
    const showRegistered = !opt || state.build.rules.mode === 'keep';
    const parts = [];
    if (showRegistered && weekKeepsRegistered()) parts.push(el('span', 'plb-lg-i', svg(ICONS.lock, 11), T.legendRegistered));
    else if (showRegistered) parts.push(el('span', 'plb-lg-i', el('i', 'plb-k'), T.legendRegistered));
    if (opt) parts.push(...(parts.length ? [' '] : []), el('span', 'plb-lg-i', el('i', 'plb-k plb-k--new'), T.planned));
    // An edited plan's option: its classes at the sections the student holds are marked.
    if (opt && optionMeetings(opt).some((m) => m.own)) parts.push(' ', el('span', 'plb-lg-i', el('i', 'plb-k plb-k--new plb-k--own'), T.ownSection));
    if (alt) parts.push(...(parts.length ? [' '] : []), el('span', 'plb-lg-i', el('i', 'plb-k plb-k--alt'), T.legendAlt));
    $('plbLegend').replaceChildren(...parts);
    for (const mark of $('plbLegend').querySelectorAll('i, svg')) mark.setAttribute('aria-hidden', 'true');
  }
  function renderWeek() {
    const host = $('plbWeek');
    const regModel = weekModel(registeredMeetings());
    renderTab(regModel);
    renderCurTT();
    const opt = currentOption();
    const alts = altMeetings(opt);
    state.altShown = altSignature(opt, alts);
    renderLegend(opt, alts.length > 0);
    const name = $('plbWeekName');
    if (opt) {
      const said = optionNames().get(opt.name);
      name.replaceChildren(...T.optionName(opt.name), ...(said ? [' · ', said] : []));
    } else name.textContent = T.registered;
    $('plbWeekPanel').setAttribute('aria-labelledby', opt ? tabId(opt.name) : 'plbTabRegistered');
    renderGlance();
    if (!state.ctx) { host.replaceChildren(el('p', 'plb-empty', T.loadFirstWeek)); return; }
    const showRegistered = !opt || state.build.rules.mode === 'keep';
    // One entry per course (CODE::NAME, never the bare code); a code two courses share is told
    // apart by the name.
    const untimedRows = showRegistered
      ? [...new Map(registeredCourses().filter((c) => !c.timed).map((c) => [`${c.code}::${c.name}`, c])).values()]
      : [];
    const sharedCodes = new Set(untimedRows.map((c) => c.code).filter((code, i, all) => all.indexOf(code) !== i));
    const untimed = untimedRows.map((c) => (sharedCodes.has(c.code) && c.name ? `${c.code} ${c.name}` : c.code));
    const partial = showRegistered ? fallbackRows() : 0;
    const clashes = opt ? weekMeetings(opt).filter((m) => m.kind === 'new' && m.clash).length : 0;
    const notes = [
      untimed.length ? el('p', 'plb-note', ...T.untimed(untimed)) : null,
      partial ? el('p', 'plb-note', T.mappingPartial(partial)) : null,
      clashes ? el('p', 'plb-note', T.clashNote(clashes)) : null,
    ].filter(Boolean);
    drawWeekGrid(opt ? weekMeetings(opt) : registeredMeetings(), host, notes, alts);
    markOpenBlocks();
  }
  // The week's grid for `meetings`, drawn into `host` with `notes` under it. `alts` are the dashed
  // alternatives of an open course panel. The one builder for the week and for the current-timetable
  // disclosure.
  function drawWeekGrid(meetings, host, notes, alts, inert) {
    const model = weekModel(meetings);
    if (!model.meetings.length && !alts.length) {
      host.replaceChildren(...[el('p', 'plb-empty', T.noClassesAtAll), ...notes]);
      return;
    }
    // The grid's hours and days make room for the alternatives; every count and fact stays the solid week's.
    const geo = alts.length ? weekModel([...model.meetings, ...alts]) : model;
    const rows = (geo.endMin - geo.startMin) / 15;
    const footRow = rows + 2;
    const grid = el('div', 'plb-wk');
    grid.style.setProperty('--plb-days', String(geo.days.length));
    grid.style.setProperty('--plb-rows', String(rows));
    geo.days.forEach((day, i) => {
      const head = el('div', 'plb-dh', DAY_SHORT[day]);
      head.style.gridColumn = String(i + 2);
      head.style.gridRow = '1';
      grid.append(head);
      const line = el('div', 'plb-vl');
      line.style.gridColumn = String(i + 2);
      grid.append(line);
    });
    for (let mins = geo.startMin; mins <= geo.endMin; mins += 60) {
      const row = 2 + (mins - geo.startMin) / 15;
      const rule = el('div', 'plb-hl');
      rule.style.gridRow = String(row);
      grid.append(rule);
      if (mins < geo.endMin) {
        const label = el('div', 'plb-tm', bdi(hhmm(mins)));
        label.style.gridRow = String(row);
        grid.append(label);
      }
    }
    geo.days.forEach((day, i) => {
      const list = model.byDay[day] || [];
      if (ACADEMIC_DAYS.includes(day) && !list.length) {
        const wash = el('div', 'plb-free', T.free);
        wash.style.gridColumn = String(i + 2);
        wash.style.gridRow = `2 / ${footRow}`;
        grid.append(wash);
      }
      // The dashed ones go first: where a solid block shares their time it paints over them.
      for (const m of alts.filter((a) => a.day === day)) {
        const rowStart = 2 + Math.floor((m.st - geo.startMin) / 15);
        grid.append(altBlock(m, rowStart, Math.max(rowStart + 1, 2 + Math.ceil((m.en - geo.startMin) / 15)), i + 2));
      }
      for (const m of list) {
        const rowStart = 2 + Math.floor((m.st - geo.startMin) / 15);
        const rowEnd = Math.max(rowStart + 1, 2 + Math.ceil((m.en - geo.startMin) / 15));
        grid.append(weekBlock(m, rowStart, rowEnd, i + 2, inert));
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
    fitBlocks(grid);
  }
  // How many lines each block of a drawn week holds, from its own geometry, never from a number of
  // minutes: its height is its span of 15-minute rows (the grid's --plb-row each, less its margins),
  // and two lines need its padding and borders plus the line heights of the code and the time; a
  // third line is the section's, when a narrow column pushes it off the code's line. A block too
  // short for two lines reads on one ("one": code · time · §section), one too short for three keeps
  // the code and the section on its first line ("two"); a taller one wraps freely. All read from
  // the stylesheet, so a change of row height or type size changes the answer with it.
  function fitBlocks(grid) {
    const px = (value) => parseFloat(value) || 0;
    const row = px(getComputedStyle(grid).getPropertyValue('--plb-row'));
    if (row <= 0) return;
    for (const block of grid.querySelectorAll('.plb-b, .plb-gb')) {
      delete block.dataset.fit;
      const own = getComputedStyle(block);
      const height = (Number(block.dataset.span) || 1) * row - px(own.marginTop) - px(own.marginBottom);
      const chrome = px(own.paddingTop) + px(own.paddingBottom) + px(own.borderTopWidth) + px(own.borderBottomWidth);
      const line = (selector) => {
        const part = block.querySelector(`:scope > ${selector}`);
        return part ? px(getComputedStyle(part).lineHeight) : 0;
      };
      const two = chrome + line('.plb-cd') + line('.plb-bt');
      if (height < two) block.dataset.fit = 'one';
      else if (height < two + line('.plb-bs')) block.dataset.fit = 'two';
    }
  }
  // The registered classes in one line: "CS101 Sun Tue 10:00-11:15; AI201 Mon 08:00-09:15".
  function curTTDigest() {
    const rows = state.baseline.filter((r) => normDay(r.day) && toMins(r.start_time) !== null && toMins(r.end_time) !== null);
    const codes = [...new Set(rows.map((r) => String(r.course_code || r.course_key || '')).filter(Boolean))];
    if (!codes.length) return [T.curTTNone];
    // Codes only, never meetings or times: the opened week has the detail.
    return codes.flatMap((code, i) => (i ? [' · ', bdi(code)] : [bdi(code)]));
  }
  // The "Current timetable" disclosure under the student band: closed by default, the registered
  // week drawn with the week's own grid when it is opened, redrawn while it stays open.
  const weekKey = () => state.baseline.map((r) => [r.course_code, r.course_key, r.section, r.day, r.start_time, r.end_time].join('|')).join(';');
  function renderCurTT() {
    const box = $('plbCurTT');
    box.hidden = !state.ctx;
    if (!state.ctx) { $('plbCurTTWeek').replaceChildren(); state.curTT = { digest: null, drawn: null }; return; }
    const key = weekKey();
    // The digest and the grid are drawn again only when the registered week changed (a student
    // load), not on every redraw of the week below.
    if (state.curTT.digest !== key) {
      $('plbCurTTDigest').replaceChildren(...curTTDigest());
      state.curTT.digest = key;
    }
    if (!box.open) { $('plbCurTTWeek').replaceChildren(); state.curTT.drawn = null; return; }
    if (state.curTT.drawn === key) return;
    state.curTT.drawn = key;
    const untimedRows = [...new Map(registeredCourses().filter((c) => !c.timed).map((c) => [`${c.code}::${c.name}`, c])).values()];
    const shared = new Set(untimedRows.map((c) => c.code).filter((code, i, all) => all.indexOf(code) !== i));
    const untimed = untimedRows.map((c) => (shared.has(c.code) && c.name ? `${c.code} ${c.name}` : c.code));
    drawWeekGrid(registeredMeetings(), $('plbCurTTWeek'), untimed.length ? [el('p', 'plb-note', ...T.untimed(untimed))] : [], [], true);
  }
  // While a course's panel is open its blocks in the week are marked.
  function markOpenBlocks() {
    const open = state.detail ? state.detail.code : null;
    for (const block of document.querySelectorAll('#plbWeek .plb-b[data-act="week-open"]')) {
      const on = open !== null && norm(block.dataset.code) === open;
      if (on) block.dataset.open = 'true'; else delete block.dataset.open;
      block.setAttribute('aria-expanded', String(on));
    }
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
    const held = (state.build.loadOnly || []).reduce((sum, r) => sum + (finitePositive(r.credits) || 0), 0);
    return added + (state.build.rules.mode === 'keep' ? registeredCredits() || 0 : held);
  }
  // WHAT AN OPTION CHANGES AGAINST THE REGISTRATION (a build of an edited plan), course by course,
  // by section id: the same section says nothing; another one "CS205 §M1 › §M3"; a registered
  // course the adviser removed "GS311 dropped"; one still Required-off in the plan that the build
  // left out "not placed"; a course the student does not hold "+IS482" (one they hold, added back
  // from Available, is not "+"). A load-only course is "dropped" only when it was removed. null
  // when there is nothing to compare with (a Keep build, or no registered course).
  function changesOf(opt) {
    const build = state.build;
    if (!build || build.rules.mode !== 'ignore' || !build.registered || !build.registered.length) return null;
    const out = [];
    const regKeys = new Set();
    for (const c of build.registered) {
      const key = norm(c.key);
      const code = shownCode(c.code || c.key);
      regKeys.add(key);
      if (!c.complete) {
        if (!(build.loadOnly || []).some((r) => norm(r.key) === key)) out.push({ kind: 'dropped', code });
        continue;
      }
      const m = mappingFor(opt, key);
      if (m) {
        if (String(m.term_section_id) !== String(c.tsid)) out.push({ kind: 'moved', code, from: c.section || '?', to: String(m.section || '').trim() || '?' });
      } else if (build.shortlist.some((it) => norm(it.course_code) === key)) out.push({ kind: 'notPlaced', code });
      else out.push({ kind: 'dropped', code });
    }
    for (const m of opt.mappings || []) {
      const key = courseKeyOf(m) || norm(m.course_code);
      if (!regKeys.has(key)) out.push({ kind: 'added', code: shownCode(key) });
    }
    return out;
  }
  function changesNodes(list) {
    if (!list.length) return [T.chgNone];
    return list.flatMap((c, i) => [...(i ? [' · '] : []), ...T.chg[c.kind](c)]);
  }
  function optionIds(opt) {
    return (opt.mappings || []).map((m) => m.term_section_id).filter(Boolean);
  }
  // Different build methods often arrive at the same timetable: on real data six options were two.
  // One tab per distinct timetable (the same sections, the same courses left out). The first
  // option stands for the others and remembers them, so nothing the reply said is lost.
  function distinctOptions(options) {
    const seen = new Map();
    const out = [];
    // The key names everything that makes two options the same to an adviser: each course's
    // section (by id AND label, so a reply without ids cannot collide) at its times, and each
    // course left out WITH its reason, so no reason is ever dropped by a merge.
    const placed = (m) => `${courseKeyOf(m) || String(m.course_code || '')}#${m.term_section_id || ''}#${String(m.section || '').trim()}`
      + `@${(m.meetings || []).map((mm) => `${mm.day} ${mm.start_time}-${mm.end_time}`).sort().join('/')}`;
    const left = (u) => `${String(u.course_code || '')}:${String(u.reason_code || u.reason || '')}`;
    for (const opt of options) {
      const key = `${(opt.mappings || []).map(placed).sort().join(',')}|${(opt.unscheduled || []).map(left).sort().join(',')}`;
      // An option with nothing to save is a placeholder for a failure: never folded into another.
      const mergeable = optionIds(opt).length > 0;
      const first = mergeable ? seen.get(key) : null;
      if (first) { first.aliases.push({ name: String(opt.name), method: opt.method }); continue; }
      const kept = { ...opt, aliases: [], replyIndex: out.length };
      if (mergeable) seen.set(key, kept);
      out.push(kept);
    }
    return out;
  }
  // The fewest seats left in any section the option uses, when that is a warning; else null.
  function fewestSeats(opt) {
    let least = null;
    for (const mapping of opt.mappings || []) {
      const section = findSection(mapping.term_section_id);
      if (!section) continue;
      const cap = sectionCapacity(section);
      if (fewSeats(cap) && (least === null || cap.remaining < least)) least = cap.remaining;
    }
    return least;
  }
  const modelKey = (model) => JSON.stringify([model.campusDays, model.freeDays, model.lastEnd, model.longestGap]);
  const sectionKey = (m) => `${courseKeyOf(m) || norm(m.course_code)}|${String(m.section || '').trim()}`;
  // For a tab that reads like another: the sections it does not share with every other option.
  function differenceLine(opt, options) {
    const others = options.filter((o) => o !== opt).map((o) => new Set((o.mappings || []).map(sectionKey)));
    const unique = (opt.mappings || []).filter((m) => others.some((set) => !set.has(sectionKey(m))));
    if (!unique.length) return null;
    const parts = [];
    unique.slice(0, 2).forEach((m, i) => {
      const section = String(m.section || '').trim();
      if (i) parts.push(', ');
      parts.push(bdi(`${courseKeyOf(m) || norm(m.course_code)}${section ? ` §${section}` : ''}`));
    });
    if (unique.length > 2) parts.push(' ', bdi(`+${unique.length - 2}`));
    return el('span', 'plb-tab-x plb-tab-diff', ...parts);
  }
  function optionTab(opt, alike, ctx) {
    const facts = factsOf(weekModel(weekMeetings(opt)));
    const said = ctx.names.get(opt.name);
    const unplaced = unplacedFor(opt).length;
    const terms = termsNode(forecastFor(opt.name));
    const seats = fewestSeats(opt);
    const flags = [unplaced ? T.notScheduled(unplaced) : null, seats === null ? null : T.seatsLeft(seats)].filter(Boolean);
    const changes = changesOf(opt);
    const selected = state.selected === opt.name;
    // An option that stands for identical ones carries their names too: "A1 | B2 | C2".
    const also = (opt.aliases || []).map((a) => el('span', 'plb-tab-aka', ' | ', bdi(a.name)));
    const tab = el('button', 'plb-tab',
      el('span', 'plb-tab-n', bdi(opt.name), ...also, ...(said ? [' ', el('span', 'plb-tab-nm', said)] : []),
        ...(ctx.showCredits ? [' ', el('span', 'plb-tab-cr', T.credits(optionCredits(opt)))] : [])),
      el('span', 'plb-tab-x', ...present(terms, terms ? ' · ' : null, facts.first)),
      ctx.suggested === opt.name
        ? el('span', 'plb-tab-x plb-tab-x--sg', el('span', '', ...facts.second), ' ', el('span', 'plb-mic plb-sg', T.suggested))
        : el('span', 'plb-tab-x', ...facts.second),
      alike ? differenceLine(opt, state.build.options) : null,
      flags.length ? el('span', 'plb-tab-x plb-tab-warn', flags.join(' · ')) : null,
      changes ? el('span', 'plb-tab-x plb-tab-chg', T.chgLead, ...changesNodes(changes)) : null);
    tab.type = 'button';
    tab.id = tabId(opt.name);
    tab.dataset.opt = opt.name;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(selected));
    tab.setAttribute('aria-controls', 'plbWeekPanel');
    tab.tabIndex = selected ? 0 : -1;
    return tab;
  }
  function moreNode(hidden, ctx) {
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
      const terms = termsNode(forecastFor(opt.name));
      const said = ctx.names.get(opt.name);
      const item = el('button', 'plb-more-item', bdi(opt.name), ...(said ? [' ', said] : []),
        ctx.showCredits ? ` · ${T.credits(optionCredits(opt))}` : null, ' · ',
        ...present(terms, terms ? ' · ' : null), facts.first,
        ctx.suggested === opt.name ? ' · ' : null, ctx.suggested === opt.name ? el('b', 'plb-sg-in', T.suggested) : null);
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
    // Tabs whose three facts are the same each say what sets them apart.
    const keys = shown.map((opt) => modelKey(weekModel(weekMeetings(opt))));
    const ctx = state.build
      ? { names: optionNames(), showCredits: creditsDiffer(), suggested: suggestedName() }
      : null;
    shown.forEach((opt, i) => {
      tabs.insertBefore(optionTab(opt, keys.filter((k) => k === keys[i]).length > 1, ctx), registered);
    });
    if (state.build && state.build.stale) strip.dataset.stale = state.build.stale;
    else delete strip.dataset.stale;
    // "More options" is a disclosure, not a tab: it sits beside the tab list, never inside it.
    if (hidden.length) strip.append(moreNode(hidden, ctx));
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

  /* ── One order for the options, their names and the guidance ───────────── */
  const NEVER = Number.POSITIVE_INFINITY;
  const AFTERNOON_FROM = 13 * 60;       // a class that ends after 13:00 makes its day an afternoon
  const modelOfOption = (opt) => weekModel(weekMeetings(opt));
  // What the order looks at. A week with no timed class is the worst on every week key.
  function rankKeys(opt) {
    const model = modelOfOption(opt);
    const timed = model.meetings.length > 0;
    return {
      model,
      unplaced: unplacedFor(opt).length,
      days: timed ? model.campusDays : NEVER,
      end: timed ? model.lastEnd : NEVER,
      gap: timed ? model.longestGap : NEVER,
    };
  }
  // The keys of the order, in order: fewer courses left unscheduled; fewer projected terms; fewer
  // days on campus; an earlier last class; a shorter longest gap. Then the reply's own order.
  const ORDER_KEYS = ['unplaced', 'terms', 'days', 'end', 'gap'];
  // Every option's keys. The terms key takes part only when EVERY option has a complete forecast
  // (otherwise it is null for all of them): used for some pairs and skipped for others it would
  // not be an order at all (A before C on terms, B before A and C before B on days), and the
  // suggested option would depend on the order the reply happened to list them in.
  function orderKeys(options, withTerms) {
    const rows = withTerms ? options.map((opt) => forecastFor(opt.name)) : [];
    const byTerms = rows.length > 0 && rows.every(isComplete);
    return new Map(options.map((opt, i) => [opt, {
      ...rankKeys(opt),
      terms: byTerms ? rows[i].terms_including_planning_term : null,
    }]));
  }
  function rankOptions(options, withTerms) {
    const keys = orderKeys(options, withTerms);
    const before = (a, b) => {
      const ka = keys.get(a);
      const kb = keys.get(b);
      for (const key of ORDER_KEYS) if (ka[key] !== kb[key]) return ka[key] < kb[key];
      return a.replyIndex < b.replyIndex;
    };
    const out = [];
    for (const opt of options) {
      let at = out.length;
      while (at > 0 && before(opt, out[at - 1])) at -= 1;
      out.splice(at, 0, opt);
    }
    return out;
  }
  // The order as it stands now (with the forecast, once it is there). The tabs do not follow it.
  const rankedNow = () => (state.build ? rankOptions(state.build.options, projectionOf() !== null) : []);
  const suggestedName = () => (rankedNow()[0] || { name: null }).name;

  // A short name from the option's own week, in the order the tabs were laid out in. Several
  // options that read the same: the one with the most days on campus is renamed for its days, and
  // any that still collide, the later ones in the order, go without a name. A week with no timed
  // class has none.
  function optionNames() {
    const names = new Map();
    const build = state.build;
    if (!build) return names;
    const rows = build.options.map((opt) => {
      const model = modelOfOption(opt);
      const afternoons = model.days.filter((d) => model.byDay[d].some((m) => m.en > AFTERNOON_FROM)).length;
      let said = null;
      if (model.meetings.length) said = afternoons ? T.optAfternoons(afternoons) : T.optMornings;
      return { opt, said, days: model.campusDays };
    });
    const groups = new Map();
    for (const row of rows) {
      if (row.said === null) continue;
      if (!groups.has(row.said)) groups.set(row.said, []);
      groups.get(row.said).push(row);
    }
    for (const group of groups.values()) {
      if (group.length < 2) continue;
      // "Lighter days" is true only of the one week that spreads the same kind of days over more
      // of them than any other in its group. Weeks with as many days each are not lighter than
      // one another: they keep the shared name, and the later ones lose it below.
      const most = Math.max(...group.map((row) => row.days));
      const widest = group.filter((row) => row.days === most);
      if (widest.length === 1) widest[0].said = T.optLighter(most);
    }
    const used = new Set();
    for (const row of rows) {
      if (row.said !== null && used.has(row.said)) row.said = null;
      if (row.said !== null) used.add(row.said);
      names.set(row.opt.name, row.said);
    }
    return names;
  }
  // Credits are said on a tab only when the options differ in them.
  function creditsDiffer() {
    return new Set(state.build.options.map(optionCredits)).size > 1;
  }

  // The sections an option uses that have 1 to SEAT_WARNING seats left, fewest first.
  function flaggedSections(opt) {
    const found = [];
    for (const mapping of opt.mappings || []) {
      const section = findSection(mapping.term_section_id);
      if (!section) continue;
      const cap = sectionCapacity(section);
      if (!fewSeats(cap)) continue;
      const label = `${courseKeyOf(mapping) || norm(mapping.course_code)} §${String(mapping.section || '').trim() || '?'}`;
      found.push({ label, remaining: cap.remaining });
    }
    return found.sort((a, b) => a.remaining - b.remaining);
  }

  // One sentence saying why the first option of the order is suggested. It is the order itself put
  // into words: what the first option and the next one have in common, then the key that decided
  // between them, with both values. So it can never claim more than the order does.
  function whySuggested(first, ranked) {
    const next = ranked[1];
    if (!next) return [T.whyOnly];
    const keys = orderKeys(ranked, projectionOf() !== null);
    const mine = keys.get(first);
    const theirs = keys.get(next);
    if (mine.unplaced === 0 && ranked.slice(1).every((opt) => keys.get(opt).unplaced > 0)) return [T.whyAllCourses];
    const used = ORDER_KEYS.filter((key) => mine[key] !== null);
    const decider = used.find((key) => mine[key] !== theirs[key]);
    if (!decider) return T.whyLevel(next.name);
    // A week with no timed class has no days, finish or gap to quote.
    if (mine[decider] === NEVER || theirs[decider] === NEVER) return T.whyTimed(next.name);
    const said = {
      unplaced: (n) => [String(n)],
      terms: (n) => [String(n)],
      days: (n) => [String(n)],
      end: (mins) => [bdi(hhmm(mins))],
      gap: (mins) => (mins > 0 ? [nb(durationText(mins))] : [T.whyNoGap]),
    }[decider];
    // What the two share is worth saying only for what an adviser weighs: terms, days, finish.
    const shared = used.slice(0, used.indexOf(decider)).filter((key) => key !== 'unplaced');
    return T.whyDecided(next.name, shared, decider, said(mine[decider]), said(theirs[decider]));
  }
  // "A1 is the fallback: 4 terms, Thu free, ends 14:15."
  function fallbackLine(opt) {
    const model = modelOfOption(opt);
    const row = forecastFor(opt.name);
    const left = unplacedFor(opt).length;
    const facts = [];
    if (isComplete(row)) facts.push([T.gradTermCount(row.terms_including_planning_term)]);
    if (model.meetings.length) {
      if (!model.freeDays.length) facts.push([T.noFreeDay]);
      else if (model.freeDays.length === 1) facts.push([T.freeOne(IS_AR ? DAY_FULL_AR[model.freeDays[0]] : DAY_SHORT[model.freeDays[0]])]);
      else facts.push([T.freeDays(model.freeDays.length)]);
      facts.push([`${T.ends} `, bdi(hhmm(model.lastEnd))]);
    }
    if (left) facts.push([T.notScheduled(left)]);
    return T.fallbackLine(opt.name, facts.flatMap((parts, i) => (i ? [T.gradSep, ...parts] : parts)));
  }
  function viewButton(name, id) {
    const button = el('button', 'plb-lk plb-view', ...T.viewOption(name), svg(ICONS.next, 12));
    button.type = 'button';
    button.id = id;
    button.dataset.view = name;
    return button;
  }
  // What the selected tab says about itself: its courses, its clashes; for Registered, its size.
  function statusRow() {
    const opt = currentOption();
    if (!opt) {
      const list = registeredCourses();
      const credits = registeredCredits();
      return { kind: 'ok', parts: list.length ? [T.courseWord(list.length), ...(credits === null ? [] : [` · ${T.credits(credits)}`])] : [T.noRegistered] };
    }
    const total = state.build.shortlist.length;
    const left = unplacedFor(opt).map((item) => String(item.course_code));
    const clashes = weekMeetings(opt).filter((m) => m.kind === 'new' && m.clash).length;
    if (!total) return { kind: 'ok', parts: [T.statusNone] };
    const parts = [T.statusScheduled(total - left.length, total)];
    if (left.length) parts.push(' · ', T.statusLeftOut, ...left.flatMap((code, i) => (i ? [T.gradSep, bdi(code)] : [bdi(code)])));
    if (clashes) parts.push(' · ', T.statusClash(clashes));
    else if (!left.length) parts.push(' · ', T.statusClean);
    return { kind: left.length || clashes ? 'warn' : 'ok', parts };
  }
  function renderGuide() {
    const guide = $('plbGuide');
    const build = state.build;
    const show = !!(build && build.options.length);
    guide.hidden = !show;
    if (!show) {
      guide.replaceChildren();
      delete guide.dataset.stale;
      return;
    }
    if (build.stale) guide.dataset.stale = build.stale; else delete guide.dataset.stale;
    const ranked = rankedNow();
    const first = ranked[0];
    const names = optionNames();
    const nameOf = (opt) => names.get(opt.name);
    const row = (kind, label, body) => el('div', `plb-fr${kind ? ` plb-fr--${kind}` : ''}`, el('dt', 'plb-mic', label), el('dd', 'plb-gv', ...body));

    const said = nameOf(first);
    const rows = [row('', T.suggested, [
      el('b', 'plb-gt', bdi(first.name), ...(said ? [' ', said] : [])),
      el('div', 'plb-gv-row plb-gv-row--own',
        el('span', 'plb-gx', ...whySuggested(first, ranked)),
        state.selected === first.name ? null : viewButton(first.name, 'plbGuideView')),
    ])];

    const flagged = flaggedSections(first);
    if (flagged.length) {
      const fallback = ranked.find((opt) => opt !== first && fewestSeats(opt) === null);
      rows.push(row('watch', T.gWatch, [
        ...flagged.map((f) => el('b', 'plb-wb', svg(ICONS.warn, 14), el('span', '', ...T.seatLine(f.label, f.remaining)))),
        el('div', 'plb-gv-row',
          el('span', 'plb-gx', ...(fallback ? fallbackLine(fallback) : [T.allNearlyFull])),
          fallback && state.selected !== fallback.name ? viewButton(fallback.name, 'plbGuideFallback') : null),
      ]));
    }

    const depends = dependsOn();
    if (depends.length) {
      rows.push(row('depends', T.gDepends, depends.map((entry) => {
        const line = el('span', 'plb-gx plb-dep', bdi(entry.code), ' · ', entry.effect.text.replace(/^[A-Z]/, (c) => c.toLowerCase()));
        line.dataset.code = entry.code;
        return line;
      })));
    }

    const status = statusRow();
    rows.push(row('', T.gStatus, [
      el('span', 'plb-okl', svg(status.kind === 'ok' ? ICONS.tick : ICONS.warn, 14), el('span', '', ...status.parts)),
    ]));
    rows[rows.length - 1].querySelector('.plb-okl').dataset.kind = status.kind;
    guide.replaceChildren(el('dl', 'plb-fx', ...rows));
  }


  /* ── Option at a glance: the facts of the selected week, its sections and the guidance ─────── */
  function dayLabel(day, full) {
    return IS_AR ? DAY_FULL_AR[day] : (full ? DAY_FULL_EN[day] : DAY_SHORT[day]);
  }
  // The same model the tab facts read (weekModel of the week's meetings): one source of truth.
  function glanceFacts(opt) {
    const model = weekModel(opt ? weekMeetings(opt) : registeredMeetings());
    const credits = opt ? optionCredits(opt) : registeredCredits();
    const cap = opt && state.build && state.build.rules ? state.build.rules.maxCredits : state.rules.maxCredits;
    let load = '—';
    if (credits !== null) load = cap === null || cap === undefined ? T.loadOnly(credits) : T.loadOf(credits, cap);
    const timed = model.meetings.length > 0;
    let free = '—';
    if (timed) {
      if (!model.freeDays.length) free = T.fNone;
      else if (model.freeDays.length === 1) free = dayLabel(model.freeDays[0], true);
      else free = model.freeDays.map((d) => dayLabel(d, false)).join(IS_AR ? '، ' : ', ');
    }
    return { model, load, campus: timed ? T.dayWord(model.campusDays) : '—', free, last: timed ? hhmm(model.lastEnd) : '—' };
  }
  function sectionRows(opt) {
    const rows = [];
    if (opt) {
      const placed = (opt.mappings || []).map((m) => {
        const item = state.build.shortlist.find((c) => norm(c.course_code) === courseKeyOf(m) || norm(c.course_code) === norm(m.course_code));
        const code = String((item && item.course_code) || m.course_code || courseKeyOf(m));
        return { code, name: item && item.course_name ? String(item.course_name) : '', section: String(m.section || '').trim(), limited: !!(item && pinsOf(item).length) };
      });
      const order = planKeys();
      const at = (r) => { const i = order.indexOf(norm(r.code)); return i < 0 ? 999 : i; };
      placed.sort((a, b) => at(a) - at(b));
      for (const r of placed) {
        const chip = r.section ? el('span', 'plb-sch', bdi(`§${r.section}`), r.limited ? ` ${T.fOnly}` : '') : null;
        if (chip && r.limited) chip.dataset.only = 'true';
        const row = el('li', 'plb-sr', swatch(r.code), el('span', 'plb-code', bdi(r.code)),
          nameNode('plb-nm', r.code, r.name), chip);
        row.dataset.code = norm(r.code);
        rows.push(row);
      }
    }
    if (opt && state.build.rules.mode === 'ignore') {
      for (const r of state.build.loadOnly || []) {
        const row = el('li', 'plb-sr plb-sr--load', swatch(r.key), el('span', 'plb-code', bdi(shownCode(r.code || r.key))),
          nameNode('plb-nm', r.key, r.name), el('span', 'plb-kt', T.loadOnlyNote));
        row.dataset.code = norm(r.key);
        rows.push(row);
      }
    }
    if (!opt || state.build.rules.mode === 'keep') {
      for (const c of registeredCourses()) {
        // A registered course has its colour too: the same swatch as its blocks in the week.
        const row = el('li', 'plb-sr plb-sr--kept', swatch(c.key), el('span', 'plb-code', bdi(c.code)),
          nameNode('plb-nm', c.key, c.name),
          c.section ? el('span', 'plb-sch', bdi(`§${c.section}`)) : null, el('span', 'plb-kt', T.keptTag));
        row.dataset.code = c.key;
        rows.push(row);
      }
    }
    return rows;
  }
  function renderGlance() {
    const box = $('plbGlance');
    box.hidden = !state.ctx;
    if (!state.ctx) return;
    const opt = currentOption();
    const f = glanceFacts(opt);
    $('plbGlanceTitle').replaceChildren(...(opt ? T.glanceOpt(opt.name) : [T.glanceReg]));
    const fact = (id, label, value) => {
      $(id).replaceChildren(el('dt', 'plb-mic', label), el('dd', 'plb-fv', value));
    };
    fact('plbFactLoad', T.fLoad, f.load);
    fact('plbFactCampus', T.fCampus, f.campus);
    fact('plbFactFree', T.fFree, f.free);
    fact('plbFactLast', T.fLast, f.last);
    $('plbSecs').replaceChildren(...sectionRows(opt));
  }
  // The apply bar's own words about the selected option: all placed, or which are not; and a summary.
  function renderApplyStatus() {
    const status = $('plbApplyStatus');
    const sum = $('plbApplySum');
    const opt = currentOption();
    const build = state.build;
    const show = !!(opt && build && !build.stale && build.shortlist.length);
    status.hidden = !show;
    sum.hidden = !show;
    if (!show) { status.replaceChildren(); sum.replaceChildren(); return; }
    const left = unplacedFor(opt).map((item) => String(item.course_code));
    status.dataset.kind = left.length ? 'warn' : 'ok';
    status.replaceChildren(svg(left.length ? ICONS.warn : ICONS.tick, 14),
      el('span', '', ...(left.length ? [T.notPlaced(left.length), ': ', ...joinCodes(left)] : [T.placedAll])));
    const f = glanceFacts(opt);
    const parts = [...T.optionName(opt.name)];
    const add = (text) => parts.push(' · ', text);
    if (f.load !== '—') add(f.load);
    if (f.model.meetings.length) {
      add(f.campus);
      const n = f.model.freeDays.length;
      if (n === 1) add(T.offDay(dayLabel(f.model.freeDays[0], true)));
      else if (n > 1) add(T.offDays(n));
    }
    sum.replaceChildren(...parts);
  }

  /* ── Courses ───────────────────────────────────────────────────────────── */
  function registeredCredits() {
    const list = registeredCourses();
    const known = list.filter((c) => c.credits !== null);
    return known.length ? known.reduce((sum, c) => sum + c.credits, 0) : null;
  }
  const inShortlist = (code) => state.shortlist.some((c) => norm(c.course_code) === norm(code));

  /* ── The registered courses as rows of Your plan (S7) ──────────────────── */
  // The registered courses Your plan lists. An elective slot the plan names (AI1) with 0 credits is
  // how the plan names a course already counted (its mapped elective), not a registration: no row.
  function planRegistered() {
    return registeredCourses().filter((c) => !(c.fallback && c.credits === 0));
  }
  // The registered courses once each: a course held at two sections is one course to the build
  // (the server reads it so), so it becomes one row, pinned to the first section listed.
  function heldOnce() {
    const seen = new Set();
    return planRegistered().filter((c) => {
      const key = norm(c.key);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }
  // What the student holds, as one comparable string (course and section, in any order).
  const holdingsSig = (regs) => regs.map((c) => `${norm(c.key)}|${String(c.tsid)}`).sort().join(',');
  const regWord = (c) => (c && c.registrar ? T.regTag : T.expTag);
  // A CODE::NAME key is shown by its code; the key stays the course's identity everywhere else.
  const shownCode = (code) => String(code || '').split('::')[0];
  const isKeptRow = (code) => state.rules.mode === 'keep' && planRegistered().some((c) => norm(c.key) === norm(code));
  const isLoadOnly = (code) => state.loadOnly.some((r) => norm(r.key) === norm(code));
  const loadOnlyCredits = () => state.loadOnly.reduce((sum, r) => sum + (finitePositive(r.credits) || 0), 0);
  // Every row Your plan lists, by course key: planned, registered (Keep) and load only (Ignore).
  function planRowCodes() {
    return [
      ...state.shortlist.map((c) => norm(c.course_code)),
      ...(state.rules.mode === 'keep' ? planRegistered().map((c) => norm(c.key)) : state.loadOnly.map((r) => norm(r.key))),
    ];
  }
  // A registered course as a row of the plan: Required, pinned to the section the student holds.
  // Its credits are the registered figure; when no source knows them they are said to be unknown
  // (the build counts the registered figure itself, server-side).
  function itemFromRegistered(c) {
    const credits = finitePositive(c.credits);
    return {
      course_code: norm(c.key), course_name: c.name, credits: credits || 0, priority: 'Med', status: 'Eligible',
      missing_prerequisites: [], must_take: true, score: 0,
      pinned_sections: [{ term_section_id: Number(c.tsid), section: c.section }],
      origin: 'registered',
      registered_section: { term_section_id: Number(c.tsid), section: c.section },
      ...(credits === null ? { credits_unknown: true } : {}),
    };
  }
  // THE ONE ENTRY POINT: every control of a registered row in Keep calls this first (the section
  // control, Required, ×, ‹ with registered rows ticked, «), and so does the switch to Ignore. Each
  // registered course becomes a row of its kind: a section with usable meetings a Required row
  // pinned to it; one without (no section, or no usable meetings) a load-only row. Then the mode is
  // Ignore (builds go out of date, the catalogue reloads with the registered sections in it). The
  // caller applies its own change, redraws once and says what happened (convLead puts "Registered
  // courses are now editable" in front of it). A tick alone only selects: it never converts.
  function convertRegistered() {
    if (!state.ctx || state.rules.mode !== 'keep') return false;
    const regs = heldOnce();
    if (!regs.length) {
      // Nothing registered: Ignore is only the rule.
      commitRules({ ...state.rules, mode: 'ignore' }, true);
      return true;
    }
    const items = [];
    const loadOnly = [];
    for (const c of regs) {
      if (inShortlist(c.key)) continue;
      if (c.complete) items.push(itemFromRegistered(c));
      else loadOnly.push({ key: norm(c.key), code: c.code, name: c.name, credits: c.credits, section: c.section, tsid: c.tsid, registrar: c.registrar });
    }
    state.converted = { regs: regs.map((c) => ({ ...c })) };
    state.shortlist = [...state.shortlist, ...items];
    state.loadOnly = loadOnly;
    convLead = true;
    commitRules({ ...state.rules, mode: 'ignore' }, true);
    return true;
  }
  // How many registered courses the edited plan no longer holds as they were registered: dropped,
  // another section or any section, no longer Required.
  function changesCount() {
    if (!state.converted) return 0;
    let n = 0;
    for (const c of state.converted.regs) {
      const key = norm(c.key);
      if (!c.complete) {
        if (!isLoadOnly(key)) n += 1;
        continue;
      }
      const item = state.shortlist.find((it) => norm(it.course_code) === key);
      const pins = pinsOf(item).map((p) => String(p.term_section_id));
      if (!item || item.origin !== 'registered' || !item.must_take || pins.length !== 1 || pins[0] !== String(c.tsid)) n += 1;
    }
    return n;
  }
  // Courses the adviser added (from Available): every planned row that is not a registered one.
  const addedItems = () => state.shortlist.filter((it) => it.origin !== 'registered');
  // What a reload would lose: courses added, or a registered row changed.
  const isDirty = () => addedItems().length > 0 || changesCount() > 0;
  // BACK TO KEEP: the registered courses are kept as registered again. The converted and load-only
  // rows go (and a registered course added back from Available: Keep holds it already); the courses
  // added from Available stay; ticks and the popover close; builds go out of date; the keyboard goes
  // to the Keep switch.
  function backToKeep(focusSwitch) {
    const regKeys = new Set(planRegistered().map((c) => norm(c.key)));
    state.shortlist = state.shortlist.filter((it) => it.origin !== 'registered' && !regKeys.has(norm(it.course_code)));
    state.loadOnly = [];
    state.converted = null;
    state.pick.planTicked.clear();
    state.detail = null;
    closeConvAsk(false);
    commitRules({ ...state.rules, mode: 'keep' }, true);
    renderCourses();
    renderActions();
    setStatus('ok', T.backDone);
    if (focusSwitch) $('plbRegKeep').focus();
  }
  // Back to Keep from the switch, the notice bar or Reset: it asks first when a registered row was
  // changed (the switch in the rules zone, the bar in the bar itself).
  function requestBackToKeep(from) {
    if (!state.converted) { commitRules({ ...state.rules, mode: 'keep' }); return; }
    if (!changesCount()) { backToKeep(true); return; }
    if (from === 'bar') { openConvAsk(); return; }
    renderRules();               // the switch shows the rule in force while it asks
    openEligConfirm('mode', null, false);
  }
  // The notice bar above Your plan, while the registered courses are rows: its words are a live
  // region of their own (always in the page, empty in Keep, so a change of words is announced).
  function renderConvBar() {
    const on = !!(state.ctx && state.converted);
    $('plbConvBar').dataset.on = String(on);
    const text = on ? T.convBar : '';
    if ($('plbConvText').textContent !== text) $('plbConvText').textContent = text;
    const back = $('plbBackKeep');
    back.hidden = !on;
    const n = on ? changesCount() : 0;
    const label = n ? T.backToKeepDrops(n) : T.backToKeep;
    if (back.textContent !== label) back.textContent = label;
    if (!on) closeConvAsk(false);
    else if (!$('plbConvAsk').hidden) $('plbConvAskText').textContent = T.askMode(n);
  }
  function convAskEscape(event) {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    closeConvAsk(true);
  }
  function openConvAsk() {
    // One question at a time: the rules zone's confirm closes.
    closeEligConfirm(false);
    $('plbConvAskText').textContent = T.askMode(changesCount());
    $('plbConvGo').textContent = T.askModeGo;
    $('plbConvKeep').textContent = T.askModeKeep;
    $('plbConvAsk').hidden = false;
    document.addEventListener('keydown', convAskEscape);
    $('plbConvGo').focus();
  }
  function closeConvAsk(focusBack) {
    const ask = $('plbConvAsk');
    if (ask.hidden) return;
    ask.hidden = true;
    document.removeEventListener('keydown', convAskEscape);
    if (focusBack && !$('plbBackKeep').hidden) $('plbBackKeep').focus();
  }
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
  // A recommended course as the build takes it: the recommender's priority and score ride along.
  function itemFromRec(r) {
    return {
      course_code: String(r.course_code || ''), course_name: String(r.course_name || ''),
      credits: Number(r.credits) || 0, priority: r.priority || 'Med', status: 'Eligible',
      missing_prerequisites: [], must_take: false, score: Number(r.score) || 0,
    };
  }
  const eligibleRec = (code) => state.recs.find((r) => norm(r.course_code) === norm(code) && String(r.status) === 'Eligible') || null;
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
  // The section list of a course as the picker and the popover read it: the catalogue the rules
  // asked for, else the whole-term catalogue (the "All programmes' courses" source) when it has it.
  function catalogSections(code) {
    const own = state.catalog ? state.catalog.get(code) : null;
    if (own && own.length) return own;
    const all = state.allCatalog ? state.allCatalog.get(code) : null;
    return all || own || [];
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
  function blockedReason(rec) {
    const missing = (rec.missing_prerequisites || []).map(String).filter(Boolean);
    const cell = el('span', 'plb-sc plb-sc--blocked', el('span', 'plb-chip', T.blocked));
    if (!missing.length) { cell.append(' ', T.prereqNotMet); return cell; }
    cell.append(' ', `${T.missing} `);
    missing.forEach((m, i) => { if (i) cell.append(', '); cell.append(bdi(m)); });
    if (missing.every((m) => inShortlist(m))) cell.append(` ${T.inThisPlan}`);
    return cell;
  }

  /* ── Courses: the picker (zone 02) ─────────────────────────────────────── */
  const AVAIL_CAP = 40;
  const SOURCES = ['recommended', 'plan', 'eligible', 'all'];
  const newPick = () => ({
    src: 'recommended', find: '', ticked: new Set(), secs: new Map(), opened: new Set(),
    planTicked: new Set(), blockedOpen: false, showAll: false,
  });
  // What the search box compares: Arabic-Indic digits read as digits, no case, no spaces.
  const fold = (text) => String(text || '')
    .replace(/[\u0660-\u0669]/g, (d) => String(d.charCodeAt(0) - 0x0660))
    .replace(/[\u06f0-\u06f9]/g, (d) => String(d.charCodeAt(0) - 0x06f0))
    .toLowerCase().replace(/\s+/g, '');
  // Keep: the registered courses; Ignore: the load-only rows (the edited rows are in the shortlist).
  const takenKeys = () => new Set(state.rules.mode === 'keep' ? registeredCourses().map((c) => c.key) : state.loadOnly.map((r) => norm(r.key)));
  // A course the picker can offer: {code, name, credits|null, rec, plan (the plan's row), sections}.
  function sourceLists(src) {
    const kept = takenKeys();
    const free = (code) => !!code && !inShortlist(code) && !kept.has(code);
    const recCodes = new Set(state.recs.map((r) => norm(r.course_code)));
    const poolBy = new Map(planPool().map((c) => [c.code, c]));
    const planBy = new Map((state.planCourses || []).map((c) => [c.code, c]));
    const fromRec = (r) => {
      const code = norm(r.course_code);
      return { code, name: String(r.course_name || ''), credits: Number(r.credits) || 0, rec: true, plan: poolBy.get(code) || null, recRow: r };
    };
    const fromPlan = (c) => ({ code: c.code, name: c.name, credits: c.credits, rec: recCodes.has(c.code), plan: c, recRow: null });
    const out = { avail: [], blocked: [] };
    const seen = new Set();
    const push = (list, entry) => { if (!seen.has(entry.code)) { seen.add(entry.code); list.push(entry); } };
    if (src === 'recommended') {
      for (const r of state.recs) {
        const code = norm(r.course_code);
        if (!free(code)) continue;
        if (String(r.status) === 'Eligible') push(out.avail, fromRec(r));
        else push(out.blocked, { ...fromRec(r), reason: blockedReason(r) });
      }
    } else if (src === 'plan') {
      const ignoring = state.rules.mode === 'ignore';
      for (const c of state.planCourses || []) {
        if (!free(c.code)) continue;
        if (!(c.status === 'not_taken' || (ignoring && c.status === 'studying'))) continue;
        if (c.canRegister) push(out.avail, fromPlan(c));
        else push(out.blocked, { ...fromPlan(c), reason: blockedReason({ missing_prerequisites: [] }) });
      }
    } else if (src === 'eligible') {
      for (const c of planPool()) push(out.avail, fromPlan(c));
      for (const r of state.recs) {
        const code = norm(r.course_code);
        if (String(r.status) === 'Eligible' && free(code)) push(out.avail, fromRec(r));
      }
    } else if (state.allCatalog) {
      const mine = String((state.student && state.student.program) || '').trim().toLowerCase();
      const codes = [...state.allCatalog.keys()].sort();
      for (const code of codes) {
        if (!free(code)) continue;
        const sections = state.allCatalog.get(code);
        const named = sections.find((s) => String(s.course_name || '').trim());
        const hours = sections.map((s) => Number(s.credit_hours)).find((n) => Number.isFinite(n) && n > 0);
        const entry = {
          code, name: named ? String(named.course_name).trim() : (planBy.has(code) ? planBy.get(code).name : ''),
          credits: hours === undefined ? null : hours, rec: recCodes.has(code), plan: poolBy.get(code) || null, recRow: null,
          compareOnly: !sections.some((s) => programmesOf(s).some((p) => p.toLowerCase() === mine)),
        };
        push(out.avail, entry);
      }
    }
    return out;
  }
  const listsFor = (src) => {
    const lists = sourceLists(src);
    const needle = fold(state.pick.find);
    if (!needle) return lists;
    const keep = (e) => fold(e.code).includes(needle) || fold(e.name).includes(needle)
      || fold(courseName(e.code, e.name)).includes(needle);
    return { avail: lists.avail.filter(keep), blocked: lists.blocked.filter(keep) };
  };
  const entrySections = (entry, src) => (src === 'all' && state.allCatalog ? state.allCatalog.get(entry.code) || [] : catalogSections(entry.code));
  const sortSections = (list) => list.slice().sort((a, b) => String(a.section || '').localeCompare(String(b.section || ''), 'en', { numeric: true }));
  // What a course moved into Your plan is, whichever source it came from: the recommendation when
  // the course is recommended (the build orders by the recommender's priority and score, as it did
  // when Fetch used to fill the plan with them), else the plan's row, else one built from the
  // whole-term catalogue.
  function makeItem(entry) {
    const rec = entry.recRow && String(entry.recRow.status) === 'Eligible' ? entry.recRow : eligibleRec(entry.code);
    if (rec) return itemFromRec(rec);
    if (entry.plan) return itemFromPlan(entry.plan);
    return {
      course_code: entry.code, course_name: entry.name, credits: entry.credits || 0, priority: 'Med',
      status: 'Eligible', missing_prerequisites: [], must_take: false, score: 0,
    };
  }
  function pinsFor(entry, src) {
    const ids = state.pick.secs.get(entry.code);
    if (!ids || !ids.size) return [];
    return sortSections(entrySections(entry, src))
      .filter((s) => ids.has(String(s.term_section_id)))
      .slice(0, MAX_ONLY)
      .map((s) => ({ term_section_id: Number(s.term_section_id), section: String(s.section || '').trim() }));
  }
  // Credits unknown: the course cannot be added (its credits would be a guess).
  const addable = (entry) => entry.credits !== null;

  function renderSource() {
    const select = $('plbSource');
    select.disabled = !state.ctx;
    const counts = {};
    for (const src of SOURCES) {
      if (src === 'all' && !state.allCatalog) { counts[src] = null; continue; }
      const l = sourceLists(src);
      counts[src] = l.avail.length + l.blocked.length;
    }
    const names = {
      recommended: T.srcRecommended(counts.recommended), plan: T.srcPlan(counts.plan),
      eligible: T.srcEligible(counts.eligible), all: T.srcAll(counts.all),
    };
    if (!select.options.length) {
      for (const src of SOURCES) { const o = document.createElement('option'); o.value = src; select.append(o); }
    }
    [...select.options].forEach((o) => { o.textContent = names[o.value]; });
    select.value = state.pick.src;
  }
  // The context reply's elective rows filled by a registered course (the forecast's policy,
  // elective_row_filling): the map by row code, and each recommendation's own `filled_by`.
  function filledRows(data) {
    const out = {};
    const take = (code, filled) => {
      if (code && isObj(filled) && String(filled.course_code || '').trim()) out[norm(code)] = { course_code: String(filled.course_code).trim(), basis: String(filled.basis || '') };
    };
    if (isObj(data.elective_rows_filled)) for (const [code, filled] of Object.entries(data.elective_rows_filled)) take(code, filled);
    for (const rec of Array.isArray(data.recommendations) ? data.recommendations : []) take(rec.course_code, rec.filled_by);
    return out;
  }
  const filledByOf = (code) => state.filledBy[norm(code)] || null;
  function tagNode(text, kind, title) {
    const node = el('span', `plb-flag${kind ? ` plb-flag--${kind}` : ''}`, text);
    if (title) node.title = title;
    return node;
  }
  function sectionRowNode(entry, section, src) {
    const model = sectionModel(section);
    const ids = state.pick.secs.get(entry.code) || new Set();
    const ticked = ids.has(model.id);
    const input = el('input', 'plb-tick');
    input.type = 'checkbox';
    input.checked = ticked;
    input.disabled = !ticked && (model.reasons.length > 0 || ids.size >= MAX_ONLY);
    if (!addable(entry)) { input.disabled = true; input.title = T.creditsUnknownWhy; }
    input.dataset.act = 'sectick';
    input.dataset.code = entry.code;
    input.dataset.tsid = model.id;
    input.setAttribute('aria-label', T.tickSection(entry.code, model.label || '?'));
    const instructors = [...new Set((section.meetings || []).map((m) => String(m.instructor || '').trim()).filter(Boolean))];
    const meets = el('span', 'plb-sec-meets', ...present(
      ...(model.when.length ? model.when : [T.unknown]),
      instructors.length ? ' · ' : null, instructors.length ? isolate(instructors.join('، ')) : null,
      model.clash ? ' ' : null, model.clash ? tagNode(T.clashFlag, 'bad') : null));
    const seats = el('span', 'plb-sec-seats');
    if (!model.cap.known) seats.append(T.unknown);
    else if (model.cap.full) seats.append(tagNode(T.seatsFull, 'bad'));
    else {
      seats.append(String(model.cap.remaining));
      if (fewSeats(model.cap)) seats.append(' ', tagNode(T.leftTag(model.cap.remaining), 'warn'));
    }
    const row = el('label', 'plb-sec', input, el('span', 'plb-sec-n', bdi(model.label || '?')), meets, seats);
    row.dataset.tsid = model.id;
    if (input.disabled) row.dataset.off = 'true';
    void src;
    return row;
  }
  function availRowNode(entry, src) {
    const code = entry.code;
    const sections = entrySections(entry, src);
    const open = state.pick.opened.has(code);
    const ticked = state.pick.ticked.has(code);
    const secsId = `plbSecs_${code.replace(/[^A-Za-z0-9_-]/g, '_')}`;
    const chev = el('button', 'plb-chev', svg(ICONS.chev, 14));
    chev.type = 'button';
    chev.dataset.act = 'open';
    chev.dataset.code = code;
    chev.setAttribute('aria-expanded', String(open));
    chev.setAttribute('aria-controls', secsId);
    chev.setAttribute('aria-label', T.sectionsOf(code));
    const box = el('input', 'plb-tick');
    box.type = 'checkbox';
    box.checked = ticked;
    box.disabled = !addable(entry);
    box.dataset.act = 'tick';
    box.dataset.code = code;
    box.setAttribute('aria-label', T.tickCourse(code));
    if (box.disabled) box.title = T.creditsUnknownWhy;
    const known = src === 'all' || state.catalog !== null;
    const caps = sections.map(sectionCapacity);
    const openCaps = caps.filter((c) => c.known && !c.full);
    const metaText = [entry.credits === null ? T.creditsUnknown : T.credits(entry.credits)];
    if (known) metaText.push(sections.length ? T.sectionWord(sections.length) : T.noSections);
    const tags = [];
    if (entry.rec) tags.push(tagNode(T.tagRecommended, ''));
    // An open elective row a registered course outside the plan likely counts for says so, while
    // that course is still a row of Your plan (in either mode: removed, it counts for nothing). It
    // stays addable (an assumption; the adviser may know better).
    const filledRow = filledByOf(code);
    const filled = filledRow && planRowCodes().some((key) => shownCode(key) === norm(filledRow.course_code)) ? filledRow : null;
    if (filled) {
      const tag = el('span', 'plb-flag plb-flag--filled', ...T.filledBy(filled.course_code));
      tag.dataset.filledBy = norm(filled.course_code);
      tags.push(tag);
    }
    if (src === 'all' && entry.compareOnly) tags.push(tagNode(T.tagCompare, 'warn', T.compareWhy));
    if (known && sections.length && caps.every((c) => c.known && c.full)) tags.push(tagNode(T.allFull, 'bad'));
    else if (openCaps.length && openCaps.every(fewSeats)) tags.push(tagNode(T.tagFewSeats, 'warn'));
    // Phones have no transfer column: every row carries its own "+" (only the ticked sections when some are).
    // On an option's tab a tag in the meta area says when adding the course changes graduation.
    const effect = currentOption() ? effectNode(code, 'could') : null;
    if (effect) tags.push(effect);
    const plus = el('button', 'plb-addbtn', svg(ICONS.plus, 18));
    plus.type = 'button';
    plus.dataset.act = 'add';
    plus.dataset.code = code;
    plus.disabled = !addable(entry);
    plus.setAttribute('aria-label', T.addCourse(code, pinsFor(entry, src).map((p) => p.section)));
    const main = el('div', 'plb-ar-main', chev, box, el('span', 'plb-code', bdi(code)),
      nameNode('plb-nm', code, entry.name),
      el('span', 'plb-meta', metaText.join(' · '), ...tags.flatMap((t) => [' ', t])), plus);
    if (entry.compareOnly && src === 'all') main.title = T.compareWhy;
    if (!addable(entry)) main.title = T.creditsUnknownWhy;
    const body = el('div', 'plb-secs');
    body.id = secsId;
    body.hidden = !open;
    if (open) {
      if (!sections.length) body.append(el('p', 'plb-empty', known ? T.noSections : T.noCatalog));
      else {
        body.append(el('div', 'plb-sec plb-sec--h', el('span', ''), el('span', 'plb-mic', T.secHead),
          el('span', 'plb-mic', T.meetsHead), el('span', 'plb-mic', T.seatsHead)));
        for (const section of sortSections(sections)) body.append(sectionRowNode(entry, section, src));
        const n = addable(entry) ? (state.pick.secs.get(code) || new Set()).size : 0;
        if (n) {
          const only = el('button', 'btn btn-sm btn-neutral plb-only', T.addOnly(n), svg(ICONS.next, 14));
          only.querySelector('svg').classList.add('plb-dir');
          only.type = 'button';
          only.dataset.act = 'only';
          only.dataset.code = code;
          body.append(el('div', 'plb-only-row', only));
        }
      }
    }
    const row = el('div', 'plb-arow', main, body);
    row.dataset.code = code;
    if (ticked) row.dataset.ticked = 'true';
    return row;
  }
  // Why a source shows nothing. "Recommended" says which truth it is: nothing was recommended, or
  // every recommended course is already in Your plan (moved there, or registered and kept).
  function emptyText(src) {
    if (state.pick.find.trim()) return T.noMatch;
    if (src !== 'recommended') return T.nothingHere;
    return state.recs.length ? T.recsAllPlanned : T.noRecs;
  }
  // "Available · 11 courses": the title in bold, the count muted (the text reads the same).
  function paneHead(node, [title, count]) {
    node.replaceChildren(`${title} `, el('span', 'plb-h3-n', `· ${count}`));
  }
  function renderAvailable() {
    const host = $('plbAvailList');
    const src = state.pick.src;
    const ready = !!state.ctx;
    const lists = ready ? listsFor(src) : { avail: [], blocked: [] };
    const nodes = [];
    if (!ready) nodes.push(el('p', 'plb-empty plb-pad', T.loadFirstCourses));
    else if (src === 'all' && state.allState === 'loading') nodes.push(el('p', 'plb-empty plb-pad', T.allLoading));
    else if (src === 'all' && state.allState === 'error') nodes.push(el('p', 'plb-empty plb-pad', T.allFailed));
    else if ((src === 'plan' || src === 'eligible') && state.planState === 'loading' && !lists.avail.length) nodes.push(el('p', 'plb-empty plb-pad', T.loadingPlan));
    else if (!lists.avail.length && !lists.blocked.length) nodes.push(el('p', 'plb-empty plb-pad', emptyText(src)));
    // The list grows with its content (the page scrolls); a long source shows its first rows and a button.
    const shown = state.pick.showAll ? lists.avail : lists.avail.slice(0, AVAIL_CAP);
    for (const entry of shown) nodes.push(availRowNode(entry, src));
    if (shown.length < lists.avail.length) {
      const more = el('button', 'btn btn-sm btn-neutral plb-showall', T.showAll(lists.avail.length));
      more.type = 'button';
      more.dataset.act = 'showall';
      nodes.push(el('div', 'plb-showall-row', more));
    }
    if (lists.blocked.length) {
      const open = state.pick.blockedOpen;
      const fold2 = el('button', 'plb-fold', svg(ICONS.chev, 14), T.blockedFold(lists.blocked.length));
      fold2.type = 'button';
      fold2.dataset.act = 'fold';
      fold2.id = 'plbFold';
      fold2.setAttribute('aria-expanded', String(open));
      fold2.setAttribute('aria-controls', 'plbBlocked');
      const list = el('div', 'plb-blocked');
      list.id = 'plbBlocked';
      list.hidden = !open;
      if (open) {
        for (const entry of lists.blocked) {
          const row = el('div', 'plb-arow plb-arow--off', el('div', 'plb-ar-main plb-ar-main--off',
            el('span', 'plb-code', bdi(entry.code)), nameNode('plb-nm', entry.code, entry.name), entry.reason));
          row.dataset.code = entry.code;
          list.append(row);
        }
      }
      nodes.push(el('div', 'plb-foldrow', fold2, list));
    }
    host.replaceChildren(...nodes);
    const tickedEntries = lists.avail.filter((e) => state.pick.ticked.has(e.code));
    const tickedCr = tickedEntries.reduce((sum, e) => sum + (e.credits || 0), 0);
    paneHead($('plbAvailCount'), T.availHead(ready ? lists.avail.length : 0));
    $('plbAvailTicked').textContent = ready ? T.tickedLine(tickedEntries.length, tickedCr) : '';
    return lists;
  }
  function planRowNode(item) {
    const code = item.course_code;
    const key = norm(code);
    const pins = pinsOf(item);
    const opt = currentOption();
    const box = el('input', 'plb-tick');
    box.type = 'checkbox';
    box.checked = state.pick.planTicked.has(key);
    box.dataset.act = 'ptick';
    box.dataset.code = key;
    box.setAttribute('aria-label', T.tickPlanned(code));
    // On an option's tab the line says what that option did with the course.
    const parts = [];
    let miss = false;
    const chosen = opt ? mappingFor(opt, code) : null;
    if (opt) {
      if (chosen) {
        parts.push(bdi(`§${String(chosen.section || '').trim() || '?'}`));
        const section = findSection(chosen.term_section_id);
        const cap = section ? sectionCapacity(section) : null;
        if (cap && fewSeats(cap)) parts.push(' · ', el('span', 'plb-few', T.seatsLeft(cap.remaining)));
        const used = String(chosen.term_section_id);
        if (pins.length > 1 && pins.some((p) => String(p.term_section_id) === used)) {
          const others = pins.filter((p) => String(p.term_section_id) !== used).map(pinLabel);
          parts.push(el('span', 'plb-or', ...T.orOthers(others)));
        }
      } else if (!state.build.shortlist.some((c) => norm(c.course_code) === key)) {
        parts.push(T.notInBuild);
      } else { miss = true; parts.push(T.notScheduledHere); }
    }
    const credits = el('span', 'plb-cr', item.credits_unknown ? T.creditsUnknown : T.credits(Number(item.credits) || 0));
    // A registered row the adviser can edit keeps a quiet "Registered §M1".
    const held = item.origin === 'registered' && state.converted ? state.converted.regs.find((c) => norm(c.key) === key) : null;
    const regTag = item.origin === 'registered'
      ? el('span', 'plb-flag plb-reg-tag', ...T.regTagSec(regWord(held), (item.registered_section && item.registered_section.section) || ''))
      : null;
    const sub = el('span', 'plb-sl', credits, ...present(regTag ? ' ' : null, regTag),
      ...(opt ? [el('span', `plb-sc${miss ? ' plb-sc--miss' : ''}`, ...parts)] : []),
      ...present(opt ? effectNode(code, 'new') : null));
    const open = !!state.detail && state.detail.code === key;
    const label = pins.length === 0 ? T.anySection : T.sectionsBtn(pins.map(pinLabel));
    const secbtn = el('button', 'plb-secbtn', el('span', 'plb-secbtn-t', label), svg(ICONS.chev, 12));
    secbtn.type = 'button';
    secbtn.dataset.act = 'detail';
    secbtn.dataset.code = key;
    if (pins.length) secbtn.dataset.limited = 'true';
    secbtn.setAttribute('aria-haspopup', 'dialog');
    secbtn.setAttribute('aria-expanded', String(open));
    secbtn.setAttribute('aria-controls', 'plbPop');
    secbtn.setAttribute('aria-label', T.sectionsOfPlanned(code, label));
    // A registered row is Required as it comes: its pin says so pressed, without the word (the row
    // already carries its section and its "Registered" tag), so the name keeps its room.
    const req = el('button', 'plb-req', svg(ICONS.pin, 14), item.must_take && item.origin !== 'registered' ? el('span', 'plb-req-t', T.required) : null);
    req.type = 'button';
    req.dataset.act = 'must';
    req.dataset.code = key;
    req.setAttribute('aria-pressed', String(!!item.must_take));
    req.setAttribute('aria-label', item.must_take ? T.requiredOn(code) : T.makeRequired(code));
    req.title = item.must_take ? T.requiredOn(code) : T.makeRequired(code);
    const remove = iconButton('x', T.removeCourse(code), 'remove', code);
    const row = el('div', 'plb-prow', box, el('span', 'plb-code', swatch(code), bdi(shownCode(code))),
      el('span', 'plb-pmain', nameNode('plb-nm plb-nm--wrap', code, item.course_name), sub),
      secbtn, req, remove);
    row.dataset.code = key;
    if (item.origin === 'registered') row.dataset.reg = 'edit';
    if (open) row.dataset.open = 'true';
    return row;
  }
  // The credits of a registered course as its row says them: unknown for a section no source
  // gives hours to, never "0 cr".
  const regCredits = (c) => (c.credits === null || (!c.fallback && !c.credits) ? T.creditsUnknown : T.credits(c.credits));
  // A registered course in Keep (`keep`): a row like a planned one (tick, section, Required, ×)
  // that the build keeps exactly as registered. Using any of its controls but the tick makes the
  // registered courses editable (convertRegistered). One with no usable section is load only:
  // counted, never placed, so it has no section control and no Required. In Ignore the same row
  // stands for a load-only course of the edited plan.
  function registeredRowNode(held, keep) {
    const key = norm(held.key);
    const code = shownCode(held.code || held.key);
    const box = el('input', 'plb-tick');
    box.type = 'checkbox';
    box.checked = state.pick.planTicked.has(key);
    box.dataset.act = 'ptick';
    box.dataset.code = key;
    box.setAttribute('aria-label', T.tickPlanned(code));
    const loadOnly = !keep || !held.complete;
    const tag = el('span', 'plb-flag plb-reg-tag', regWord(held));
    const sub = el('span', 'plb-sl', el('span', 'plb-cr', regCredits(held)), ' ', tag,
      ...(loadOnly ? [' ', el('span', 'plb-load-note', T.loadOnlyNote)] : []));
    const parts = [box, el('span', 'plb-code', swatch(key), bdi(code)),
      el('span', 'plb-pmain', nameNode('plb-nm plb-nm--wrap', key, held.name), sub)];
    if (!loadOnly) {
      const secbtn = el('button', 'plb-secbtn', el('span', 'plb-secbtn-t', bdi(`§${held.section || '?'}`)), svg(ICONS.chev, 12));
      secbtn.type = 'button';
      secbtn.dataset.act = 'detail';
      secbtn.dataset.code = key;
      secbtn.dataset.limited = 'true';
      secbtn.setAttribute('aria-haspopup', 'dialog');
      secbtn.setAttribute('aria-expanded', 'false');
      secbtn.setAttribute('aria-controls', 'plbPop');
      secbtn.setAttribute('aria-label', T.sectionsOfPlanned(code, T.regSection(held.section || '?', regWord(held))));
      const req = el('button', 'plb-req', svg(ICONS.pin, 14));
      req.type = 'button';
      req.dataset.act = 'must';
      req.dataset.code = key;
      req.setAttribute('aria-pressed', 'true');
      req.setAttribute('aria-label', T.requiredOn(code));
      req.title = T.requiredOn(code);
      parts.push(secbtn, req);
    }
    parts.push(iconButton('x', T.removeCourse(code), 'remove', key));
    const row = el('div', `plb-prow${keep ? ' plb-prow--kept' : ''}${loadOnly ? ' plb-prow--load' : ''}`, ...parts);
    row.dataset.code = key;
    row.dataset.reg = keep ? 'keep' : 'load';
    return row;
  }
  function renderPlanPane() {
    const host = $('plbPlanList');
    const ready = !!state.ctx;
    const nodes = [];
    // The popover follows its row in DOM order, so Tab goes from the button into it and back.
    const pop = $('plbPop');
    const hadFocus = pop.contains(document.activeElement) ? document.activeElement : null;
    const open = state.detail ? state.detail.code : null;
    let popPlaced = false;
    for (const item of state.shortlist) {
      nodes.push(planRowNode(item));
      if (open && norm(item.course_code) === open) { nodes.push(pop); popPlaced = true; }
    }
    if (!popPlaced) nodes.push(pop);
    const keep = state.rules.mode === 'keep';
    const regs = ready && keep ? planRegistered() : [];
    for (const course of regs) nodes.push(registeredRowNode(course, true));
    if (ready && !keep) for (const held of state.loadOnly) nodes.push(registeredRowNode(held, false));
    if (ready) {
      const hint = el('p', 'plb-hint', T.hintA, svg(ICONS.next, 13), T.hintB);
      hint.querySelector('svg').classList.add('plb-dir');
      nodes.push(hint);
    }
    host.replaceChildren(...nodes);
    if (hadFocus) hadFocus.focus({ preventScroll: true });
    // "Your plan · 6 registered + 2 added" while the registered courses are kept; else every row.
    paneHead($('plbPlanCount'), regs.length ? T.planHeadReg(regs.length, state.shortlist.length)
      : T.planHead(ready ? state.shortlist.length + (keep ? 0 : state.loadOnly.length) : 0));
    renderCreditsHeader();
  }
  function renderXfer(lists) {
    const ready = !!state.ctx;
    const shown = lists ? lists.avail : [];
    const canAdd = ready && shown.some((e) => state.pick.ticked.has(e.code) && addable(e));
    const canAll = ready && shown.some(addable);
    const rows = planRowCodes();
    const planned = rows.length;
    const ticked = [...state.pick.planTicked].filter((k) => rows.includes(k));
    // The glyphs are SVG chevrons: "towards the plan" is drawn pointing right, and the stylesheet
    // flips every `.plb-dir` under right-to-left, where the plan is on the left.
    const set = (id, enabled, label, left, all) => {
      const b = $(id);
      b.disabled = !enabled;
      b.setAttribute('aria-label', label);
      b.title = label;
      if (!b.firstChild) {
        b.replaceChildren(svg(all ? ICONS.nextAll : ICONS.next, 20));
        b.firstChild.classList.add('plb-dir');
        b.dataset.side = left ? 'plan-to-list' : 'list-to-plan';
      }
    };
    set('plbXAdd', canAdd, T.xAdd, false, false);
    set('plbXAddAll', canAll, T.xAddAll, false, true);
    set('plbXRem', ready && ticked.length > 0, T.xRem, true, false);
    set('plbXRemAll', ready && planned > 0, T.xRemAll, true, true);
  }
  // Phones: the two-tab switch above the panels. The choice is kept across every redraw.
  function renderPickTabs(lists) {
    const ready = !!state.ctx;
    $('plbZoneCourses').dataset.tab = state.pickTab;
    const set = (id, name, text) => {
      const tab = $(id);
      tab.textContent = text;
      tab.setAttribute('aria-selected', String(state.pickTab === name));
      tab.tabIndex = state.pickTab === name ? 0 : -1;
    };
    set('plbPickTabAvail', 'avail', T.tabAvail(ready && lists ? lists.avail.length : 0));
    // Every row Your plan lists (the registered ones are rows too, S7).
    set('plbPickTabPlan', 'plan', T.tabPlan(ready ? planRowCodes().length : 0));
  }
  function setPickTab(name, focus) {
    if (name !== 'avail' && name !== 'plan') return;
    state.pickTab = name;
    renderCourses();
    if (focus) $(name === 'avail' ? 'plbPickTabAvail' : 'plbPickTabPlan').focus();
  }
  function renderCreditsHeader() {
    const load = $('plbPlanLoad');
    const line = $('plbMeterLine');
    const meter = $('plbMeter');
    delete load.dataset.over;                  // "over" is only ever said against a known cap
    delete line.dataset.over;
    const phoneSum = $('plbPhoneSum');
    delete phoneSum.dataset.over;
    if (!state.ctx) { load.textContent = ''; line.textContent = ''; meter.hidden = true; phoneSum.textContent = ''; return; }
    const keep = state.rules.mode === 'keep';
    // Keep: the registered load and the planned courses. Ignore, the registered courses being rows:
    // the registered rows (and load-only hours) the plan still holds, and the courses added.
    const regKeys = new Set(state.converted ? state.converted.regs.map((c) => norm(c.key)) : []);
    const itemCredits = (list) => list.reduce((sum, c) => sum + (Number(c.credits) || 0), 0);
    const reg = keep ? registeredCredits() || 0
      : itemCredits(state.shortlist.filter((c) => regKeys.has(norm(c.course_code)))) + loadOnlyCredits();
    const planned = keep ? planCredits() : itemCredits(state.shortlist.filter((c) => !regKeys.has(norm(c.course_code))));
    const used = reg + planned;
    const cap = state.rules.maxCredits;
    load.replaceChildren(el('b', '', used), el('span', 'plb-pload-u', cap === null ? ` ${T.creditUnit(used)}` : ` / ${cap} ${T.creditUnit(cap)}`));
    const parts = [T.plannedN(planned)];
    if (keep || state.converted) parts.push(T.registeredN(reg));
    let text = parts.join(' + ');
    let over = false;
    let overBy = 0;
    if (cap !== null) {
      // The limit is the build's. Keep: what the registered courses leave of it is the room for the
      // planned ones (the build's `credit_room`); registered courses that use the whole limit leave
      // no room, and above it they are said to be over it, with what still fits: a swap for a course
      // of the same credits (a change to a registered row makes them editable). Ignore: the limit
      // is never below the load the student holds (the build raises it to that), so the edited plan
      // may hold as much as is registered.
      const held = keep ? reg : (state.converted ? registeredCredits() || 0 : 0);
      const limit = keep ? cap : Math.max(cap, held);
      const room = keep ? Math.max(0, cap - reg) : limit;
      const free = room - (keep ? planned : used);
      if (free < 0) { over = true; overBy = -free; text += ` · ${T.overLimitN(-free)}`; }
      if (keep && reg > cap) { over = true; text += ` · ${T.regOver(reg - cap)}`; }
      else if (!keep && limit > cap) text += `${free > 0 ? ` · ${free} ${T.creditUnit(free)} ${T.free2}` : ''} · ${T.capRaised(limit)}`;
      else if (free >= 0 && room === 0) text += ` · ${T.noRoomLeft}`;
      else if (free >= 0) text += ` · ${free} ${T.creditUnit(free)} ${T.free2}`;
      load.dataset.over = over ? 'true' : 'false';
      line.dataset.over = over ? 'true' : 'false';
      const total = Math.max(limit, used) || 1;
      meter.hidden = false;
      meter.firstChild.style.width = `${(planned / total) * 100}%`;
      meter.lastChild.style.width = `${(reg / total) * 100}%`;
      meter.dataset.over = over ? 'true' : 'false';
    } else meter.hidden = true;
    line.textContent = text;
    const sum = T.phoneSum(planRowCodes().length, cap === null ? T.loadOnly(used) : T.loadOf(used, cap));
    let phone = sum;
    if (overBy) phone = `${sum} · ${T.overLimitN(overBy)}`;
    else if (keep && cap !== null && reg > cap) phone = `${sum} · ${T.overLimitN(reg - cap)}`;
    phoneSum.textContent = phone;
    if (cap !== null) phoneSum.dataset.over = over ? 'true' : 'false';
  }
  // The courses zone, drawn again from the state. Keyboard focus is found again by its action,
  // course and section; where that control has gone it falls to the search box.
  // Ticked sections that the catalogue no longer lists (a rule or a reload changed it) are dropped;
  // a course left with none of its ticked sections is no longer ticked. Only against a loaded list.
  function pruneTicks() {
    const all = state.pick.src === 'all';
    const loaded = all ? state.allCatalog : state.catalog;
    if (!loaded) return;
    for (const [code, ids] of [...state.pick.secs]) {
      const listed = new Set((all ? loaded.get(code) || [] : catalogSections(code)).map((x) => String(x.term_section_id)));
      for (const id of [...ids]) if (!listed.has(id)) ids.delete(id);
      if (!ids.size) { state.pick.secs.delete(code); state.pick.ticked.delete(code); }
    }
  }
  function renderCourses() {
    const pick = $('plbPick');
    const was = document.activeElement;
    const inPick = was && pick.contains(was) && !was.closest('#plbPop');
    const mark = inPick ? { act: was.dataset.act, code: was.dataset.code, tsid: was.dataset.tsid, id: was.id } : null;
    const ready = !!state.ctx;
    $('plbFind').disabled = !ready;
    if (!ready) { state.focus = null; state.detail = null; state.pick = newPick(); }
    if (ready) pruneTicks();
    const lists = renderAvailable();
    renderSource();
    renderPlanPane();
    renderXfer(ready ? lists : null);
    renderPickTabs(ready ? lists : null);
    renderConvBar();
    renderDetail();
    restoreFocus(mark);
  }
  function restoreFocus(mark) {
    const want = state.focus;
    state.focus = null;
    let target = null;
    const q = (a, c, t) => document.querySelector(`#plbPick [data-act="${CSS.escape(a)}"][data-code="${CSS.escape(c)}"]${t ? `[data-tsid="${CSS.escape(t)}"]` : ''}`);
    if (want && want.kind !== 'find') target = q(want.kind, norm(want.code));
    else if (!want && mark) {
      if (mark.act && mark.code) target = q(mark.act, mark.code, mark.tsid);
      else if (mark.id) target = document.getElementById(mark.id);
    }
    if (target && !target.disabled && !target.hidden) { target.focus({ preventScroll: true }); return; }
    if ((want || mark) && !$('plbFind').disabled) $('plbFind').focus({ preventScroll: true });
  }
  function addEntries(list, how) {
    const added = [];
    const unknown = [];
    for (const { entry, pins } of list) {
      if (inShortlist(entry.code)) continue;      // already planned: nothing to move
      if (!addable(entry)) { unknown.push(entry.code); continue; }
      const item = makeItem(entry);
      if (pins && pins.length) item.pinned_sections = pins;
      state.shortlist.push(item);
      added.push(item);
      state.pick.ticked.delete(entry.code);
      state.pick.secs.delete(entry.code);
    }
    if (!added.length) {
      if (unknown.length) setStatus('warn', ...T.skippedUnknown(unknown));
      return;
    }
    planChanged();
    setStatus('ok', ...(added.length === 1 ? T.added(added[0].course_code) : [T.addedMany(added.length)]));
    state.focus = how === 'row' ? { kind: 'remove', code: added[0].course_code } : null;
    renderCourses();
    if (unknown.length) setStatus('warn', ...T.skippedUnknown(unknown, added.length));
    if (how !== 'row' && (document.activeElement === document.body || document.activeElement.disabled)) $('plbFind').focus({ preventScroll: true });
  }
  function removeCoursesFromPlan(codes, how) {
    // A registered row in Keep: the registered courses become editable rows first.
    if (codes.some((code) => isKeptRow(code))) convertRegistered();
    const gone = [];
    for (const code of codes) {
      const at = state.shortlist.findIndex((c) => norm(c.course_code) === norm(code));
      const held = state.loadOnly.findIndex((r) => norm(r.key) === norm(code));
      if (at >= 0) gone.push(...state.shortlist.splice(at, 1));
      else if (held >= 0) gone.push(...state.loadOnly.splice(held, 1).map((r) => ({ course_code: r.key })));
      else continue;
      state.pick.planTicked.delete(norm(code));
      // "Only these" with nothing ticked belonged to the course that has just gone, not to the panel.
      if (state.detail && state.detail.code === norm(code)) delete state.detail.only;
    }
    if (!gone.length) return;
    planChanged();
    const emptied = how === 'all' && !planRowCodes().length && state.converted;
    setStatus('ok', ...(emptied ? [T.convCleared]
      : (gone.length === 1 ? T.removed(shownCode(gone[0].course_code)) : [T.removedMany(gone.length)])));
    state.focus = how === 'row' ? { kind: 'tick', code: gone[0].course_code } : null;
    if (state.build) renderWeek();
    renderCourses();
    if (how !== 'row' && (document.activeElement === document.body || document.activeElement.disabled)) $('plbFind').focus({ preventScroll: true });
  }
  const shownEntries = () => listsFor(state.pick.src).avail;
  function moveTicked() {
    const list = shownEntries().filter((e) => state.pick.ticked.has(e.code)).map((entry) => ({ entry, pins: pinsFor(entry, state.pick.src) }));
    addEntries(list, '');
  }
  function moveAll() {
    addEntries(shownEntries().map((entry) => ({ entry, pins: [] })), '');
  }
  function moveOne(code, onlyPins, how) {
    const entry = shownEntries().find((e) => e.code === code);
    if (!entry) return;
    addEntries([{ entry, pins: pinsFor(entry, state.pick.src) }], onlyPins ? '' : (how || 'row'));
  }
  function setSource(src) {
    if (!SOURCES.includes(src) || src === state.pick.src) return;
    state.pick.src = src;
    state.pick.ticked.clear();
    state.pick.secs.clear();
    state.pick.opened.clear();
    state.pick.blockedOpen = false;
    state.pick.showAll = false;
    if (src === 'all' && state.allState !== 'ready' && state.allState !== 'loading') loadAllCatalog(state.gen);
    renderCourses();
  }
  function tickCourse(code, on) {
    if (on) state.pick.ticked.add(code);
    else { state.pick.ticked.delete(code); state.pick.secs.delete(code); }
    renderCourses();
  }
  function tickSection(code, tsid, on) {
    const ids = state.pick.secs.get(code) || new Set();
    if (on) { if (ids.size >= MAX_ONLY) { renderCourses(); return; } ids.add(tsid); state.pick.ticked.add(code); }
    else ids.delete(tsid);
    if (ids.size) state.pick.secs.set(code, ids); else state.pick.secs.delete(code);
    renderCourses();
  }
  function planTick(code, on) {
    if (on) state.pick.planTicked.add(code); else state.pick.planTicked.delete(code);
    renderXfer(listsFor(state.pick.src));
  }
  function toggleRequired(code) {
    // A registered row in Keep is shown Required: pressing it makes the registered courses
    // editable, then takes Required off that one.
    const converted = isKeptRow(code) && convertRegistered();
    const item = state.shortlist.find((c) => norm(c.course_code) === code);
    if (!item) return;
    item.must_take = !item.must_take;
    if (state.build || state.inflight) planChanged();
    state.focus = { kind: 'must', code };
    renderCourses();
    if (converted) {
      renderActions();
      setStatus('ok', ...T.convNotRequired(shownCode(code)));
    }
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
    state.planCredits = null;
    renderStudent();
    renderCourses();
    try {
      const url = `/report/student-plan/?student_id=${encodeURIComponent(state.ctx.studentId)}`
        + `&eligibility_mode=${encodeURIComponent(state.rules.eligibility)}`;
      const response = await fetch(url);
      const data = await response.json();
      if (run !== state.gen) return;
      if (!response.ok || !data || data.error) throw new Error('plan');
      const courses = [];
      let creditsDone = 0;
      let creditsAll = 0;
      for (const term of data.terms || []) {
        for (const c of term.courses || []) {
          // Every row of the plan counts toward the total; the rows the student has passed toward the first number.
          const hours = Number(c.credit_hours) || 0;
          creditsAll += hours;
          if (String(c.status || '') === 'passed') creditsDone += hours;
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
      state.planCredits = { done: creditsDone, total: creditsAll };
      state.planState = 'ready';
    } catch (err) {
      if (run !== state.gen) return;
      state.planCourses = [];
      state.planCredits = null;
      state.planState = 'error';
      setStatus('warn', T.planFailed);
    }
    renderStudent();
    renderCourses();
    loadCatalog(run);
    // A build that was forecast before these rows were known is asked once more, now naming them.
    // The question is settled here: a later reload of the plan never asks. Only a forecast that
    // offers a retry is left open, so that the retry names the rows.
    const build = state.build;
    if (build && build.token && !build.addCodes && build.projection) {
      const live = ['loading', 'ready'].includes(build.projection.state);
      if (state.planState === 'ready' && live && planPool().length) loadProjection(build, true);
      else if (state.planState !== 'ready' || live) build.addCodes = [];
    }
  }
  async function loadCatalog(run) {
    if (!state.ctx) return;
    const mine = ++state.catGen;
    const codes = [...new Set([
      ...planPool().map((c) => c.code), ...state.shortlist.map((c) => norm(c.course_code)),
      ...state.recs.map((r) => norm(r.course_code)).filter(Boolean),
    ])];
    if (!codes.length) { state.catalog = new Map(); state.catState = 'ready'; renderCourses(); return; }
    state.catState = 'loading';
    let result = null;
    try {
      const body = {
        student_id: state.ctx.studentId, academic_year: state.ctx.year, term: state.ctx.term,
        course_codes: codes, program_sections_only: state.rules.programOnly,
      };
      // The registered courses as rows: their sections are listed whatever the programme filter
      // says (the server reads which they are).
      if (state.converted) body.include_registered = true;
      result = await postJson('/ops/planner/sections-catalog/', body);
    } catch (err) { result = null; }
    if (run !== state.gen || mine !== state.catGen) return;
    state.catState = result && result.response.ok ? 'ready' : 'error';
    if (!result || !result.response.ok || !result.data || !Array.isArray(result.data.sections)) {
      state.catalog = null;
      state.catState = 'error';
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
    // The glance's names: the plan (asked just before) brings the curriculum's, the sections the
    // registrar's.
    renderGlance();
    if (state.build) keepingFocus(() => { renderStrip(); renderGuide(); });
  }

  // Source 4 of the picker: every course offered this term, all programmes. One request, made the
  // first time the source is chosen (and again for another student), grouped by course key.
  async function loadAllCatalog(run) {
    if (!state.ctx) return;
    const mine = ++state.allGen;
    state.allState = 'loading';
    state.allCatalog = null;
    renderCourses();
    let result = null;
    try {
      result = await postJson('/ops/planner/sections-catalog/', {
        student_id: state.ctx.studentId, academic_year: state.ctx.year, term: state.ctx.term,
        program_sections_only: false,
      });
    } catch (err) { result = null; }
    if (run !== state.gen || mine !== state.allGen) return;
    if (!result || !result.response.ok || !result.data || !Array.isArray(result.data.sections)) {
      state.allState = 'error';
      setStatus('warn', T.allFailed);
    } else {
      const map = new Map();
      for (const section of result.data.sections) {
        const key = courseKeyOf(section);
        if (!key) continue;
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(section);
      }
      state.allCatalog = map;
      state.allState = 'ready';
    }
    renderCourses();
  }

  /* ── Loading a student ─────────────────────────────────────────────────── */
  // Disabling the button that has the keyboard drops the keyboard on the page: it comes back to
  // Fetch when the load ends, unless the user has moved on meanwhile.
  function releaseFetch() {
    const button = $('plbFetch');
    button.disabled = false;
    const lost = !document.activeElement || document.activeElement === document.body;
    if (state.fetchKeyboard && lost) button.focus();
    state.fetchKeyboard = false;
  }
  async function loadStudent(options) {
    const reuse = !!(options && options.reuse && state.ctx);
    // After an apply in Ignore the board stays as it was built (decision 3): the plan comes back.
    const restore = reuse && options.restore ? options.restore : null;
    const studentId = reuse ? state.ctx.studentId : $('plbStudentId').value.trim();
    const year = reuse ? state.ctx.year : $('plbYear').value.trim();
    const term = reuse ? state.ctx.term : $('plbTerm').value;
    if (!/^\d+$/.test(studentId)) { setStatus('error', T.enterStudent); return; }
    if (!/^\d{4}$/.test(year)) { setStatus('error', T.enterYear); setTermPop(true); return; }

    const mine = ++state.ctxGen;
    const failed = (line) => {
      releaseFetch();
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
    if (document.activeElement === $('plbFetch')) state.fetchKeyboard = true;
    $('plbFetch').disabled = true;             // one load at a time; the newest request frees it
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
    releaseFetch();
    closeEligConfirm(false);
    // Only an accepted student retires the plan and section requests of the one on screen. A
    // fetch that failed above leaves them alive, or their groups would say "loading" for good.
    const run = ++state.gen;
    const previousId = state.student ? String(state.student.student_id) : null;
    // Course colours belong to the student: the same student reloaded keeps them, so a kept
    // (out-of-date) build is still drawn in the colours it was shown in.
    if (previousId !== String(data.student.student_id)) state.hues = {};
    const previousCtx = state.ctx ? { ...state.ctx } : null;
    // Options belong to the student they were built for. The same student reloaded (a rule that
    // reloads the suggestions) keeps them, marked out of date; another student removes them.
    if (reuse && previousId === String(data.student.student_id)) planChanged();
    else { dropBuild(true); state.detail = null; }

    state.ctx = { studentId: String(data.student.student_id || studentId), year: String(data.year || year), term: String(data.term || term) };
    state.student = data.student;
    state.baseline = Array.isArray(data.baseline) ? data.baseline : [];
    state.recs = Array.isArray(data.recommendations) ? data.recommendations : [];
    state.filledBy = filledRows(data);
    if (data.eligibility_mode === 'strict' || data.eligibility_mode === 'relaxed') {
      state.rules.eligibility = data.eligibility_mode;
    }
    state.shownElig = state.rules.eligibility;
    // Max credits starts at the student's cap, and is kept while the same student is reloaded
    // (an eligibility change reloads the student and must not undo the number just chosen).
    const cap = finitePositive(data.student.credit_cap);
    state.defaults.maxCredits = cap;
    if (previousId !== String(data.student.student_id)) state.rules.maxCredits = cap;
    // FETCH ADDS NO COURSE (owner, 2026-11-28). Your plan starts as the registered courses (the
    // kept rows) and nothing planned, for every student and in both modes; the recommendations
    // wait in Available ("Recommended for this student"). The recommender proposes a term's worth
    // as if this term's registrations were already done, so prefilling it put 8 credits on top of
    // a full 18-credit registration. A reload (a rule, a save) clears the plan the same way.
    state.shortlist = [];
    // Every load starts in Keep: the registered courses as registered, nothing of an edit kept.
    state.rules.mode = 'keep';
    state.converted = null;
    state.loadOnly = [];
    // After a save in Ignore the plan comes back as built, while the student still holds what it
    // was edited against (a save never changes the registrar's timetable). A student with only a
    // forecast now holds the saved sections themselves: the board reads them back in Keep, where
    // the rows are the plan as saved and nothing is a change against an old forecast.
    if (restore && holdingsSig(heldOnce()) === holdingsSig(restore.converted.regs)) {
      state.rules.mode = 'ignore';
      state.shortlist = restore.shortlist;
      state.loadOnly = restore.loadOnly;
      state.converted = restore.converted;
    }
    closeConvAsk(false);
    state.planCourses = null;
    state.planCredits = null;
    state.catalog = null;
    state.catState = 'idle';
    // The picker starts again on the same source; the whole-term list belongs to this student and term.
    state.pick = { ...newPick(), src: state.pick ? state.pick.src : 'recommended', find: state.pick ? state.pick.find : '' };
    // The whole-term list is kept for the same student and term (an eligibility switch reloads
    // the student, not the term); another student or term fetches it again.
    const sameTerm = !!previousCtx && previousCtx.studentId === state.ctx.studentId
      && previousCtx.year === state.ctx.year && previousCtx.term === state.ctx.term;
    if (!sameTerm) {
      state.allCatalog = null;
      state.allState = 'idle';
      state.allGen += 1;
    }
    $('plbRoot').dataset.loaded = 'true';
    $('plbYear').value = state.ctx.year;
    if ([...$('plbTerm').options].some((o) => o.value === state.ctx.term)) $('plbTerm').value = state.ctx.term;
    renderTermBtn();
    setStatus('');
    renderContextChip();
    renderStudent();
    renderRules();
    renderWeek();
    renderCourses();
    renderActions();
    loadPlan(run);
    // A request still on its way belonged to the retired run: ask again; a loaded list stays.
    if (state.pick.src === 'all' && state.allState !== 'ready') loadAllCatalog(run);
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
    state.gradWhy = false;
    if (say) setBuildLine('warn', T.planChanged);
    else setBuildLine('');
    renderStrip();
    renderWeek();
    renderCourses();
    renderActions();
    // The button that opened the review is disabled now: put the keyboard on the next step.
    if (reviewing && !$('plbBuild').disabled) $('plbBuild').focus();
  }

  // The plan or a rule changed: a build in flight was made for the old plan and is not believed;
  // a build on screen stays, marked out of date, and can no longer be applied.
  function planChanged() {
    state.buildGen += 1;
    setStale('changed');
  }
  function setStale(kind) {
    const build = state.build;
    if (!build || build.stale === 'changed' || build.stale === kind) return;
    build.stale = kind;
    // A review left open lists a build that can no longer be applied: close it and say why.
    const reviewing = $('plbApplyDialog').open && !state.applying;
    if (reviewing) closeApply();
    setBuildLine('warn', kind === 'old' ? T.staleOld : T.stale);
    keepingFocus(() => {
      renderStrip();
      renderWeek();
      renderCourses();
      renderActions();
    });
    if (reviewing && !$('plbBuild').disabled) $('plbBuild').focus();
  }
  // A redraw replaces the nodes it draws. When the page redraws by itself (the age check runs on
  // a timer, a forecast lands when the server answers) the keyboard must stay where it was: the same
  // control is found again by its id, by its action and course in the table, by the option it
  // stands for in "More options", or by its place among its course's blocks in the week.
  const weekBlocksOf = (code) => [...document.querySelectorAll(`#plbWeek [data-act="week-open"][data-code="${CSS.escape(code)}"]`)];
  function keepingFocus(redraw) {
    const was = document.activeElement;
    const id = was && was.id ? was.id : '';
    const act = was && was.dataset ? was.dataset.act : '';
    const code = was && was.dataset ? was.dataset.code : '';
    const more = was && was.dataset ? was.dataset.more : '';
    const inWeek = act === 'week-open' && code ? weekBlocksOf(code).indexOf(was) : -1;
    redraw();
    if (document.activeElement && document.activeElement !== document.body) return;
    let again = id ? document.getElementById(id) : null;
    if (!again && act && code) {
      again = document.querySelector(`#plbPick [data-act="${CSS.escape(act)}"][data-code="${CSS.escape(code)}"]`);
    }
    if (!again && more) again = document.querySelector(`#plbMoreList [data-more="${CSS.escape(more)}"]`);
    if (!again && inWeek >= 0) again = weekBlocksOf(code)[inWeek] || weekBlocksOf(code)[0] || null;
    if (again && !again.disabled) again.focus({ preventScroll: true });  // a redraw never scrolls the page
  }
  // A build older than 30 minutes is out of date. Checked when Review and apply is pressed, when
  // the page becomes visible again, and once a minute: there is no other timer.
  function checkAge() {
    const build = state.build;
    if (!build || build.stale || Date.now() - build.madeAt < STALE_AFTER_MS) return false;
    setStale('old');
    return true;
  }

  // Why a course was not scheduled. DID_NOT_FIT names the limit and, in keep mode, how much of it the
  // registered courses use (the reply's details since #287); a reply without them reads as before.
  function whyNotScheduled(u) {
    const code = String(u.reason_code || '');
    const detail = Array.isArray(u.details) && isObj(u.details[0]) ? u.details[0] : {};
    const limit = Number(detail.credit_limit);
    if (code === 'DID_NOT_FIT' && Number.isFinite(limit) && limit > 0) {
      const credits = Number(detail.course_credits);
      const kept = Number(detail.registered_credits);
      if (Number.isFinite(credits) && credits > limit) return T.overLimit(credits, limit);
      if (Number.isFinite(kept) && kept > 0) {
        const room = Math.max(0, limit - kept);
        if (room === 0) return T.noRoom(limit, kept);
        if (Number.isFinite(credits) && credits > room) return T.aboveRoom(credits, room, limit, kept);
        return T.besideOthers(limit, kept);
      }
      return T.withinLimit(limit);
    }
    return T.reason[code] || String(u.reason || '') || T.noReason;
  }
  function reasonLines(build) {
    const lines = [];
    // With no room left at all, the lead and the courses' own line say it, with the numbers.
    const noRoom = build.constraints.credit_room === 0;
    for (const f of build.failures) {
      if (noRoom && f.kind === 'max_credits') continue;
      const code = String(f.course_code || '');
      let reason;
      if (IS_AR) reason = f.kind === 'must_take' ? T.failMust : (f.kind === 'pinned_section' ? T.failPinned : T.failCredits);
      else reason = String(f.reason || T.noFeasible);
      lines.push([...(code ? [bdi(code), ': '] : []), reason]);
    }
    const groups = new Map();
    for (const u of build.unscheduled) {
      const why = whyNotScheduled(u);
      if (!groups.has(why)) groups.set(why, []);
      if (u.course_code) groups.get(why).push(String(u.course_code));
    }
    for (const [why, codes] of groups) lines.push(codes.length ? [...T.notScheduledList(codes), ' — ', why] : [why]);
    if (!lines.length && !noRoom) lines.push([T.noFeasible]);
    // An edited plan whose Required rows cannot all be placed: name them, and say what to change.
    const musts = [...new Set(build.failures.filter((f) => f.kind === 'must_take' && f.course_code).map((f) => shownCode(String(f.course_code))))];
    if (build.rules.mode === 'ignore' && musts.length && !build.options.length) lines.push(T.unrequire(musts));
    return lines;
  }
  function applicability() {
    const opt = currentOption();
    const build = state.build;
    if (build && build.stale) return { ok: false, why: null };       // the status line says why
    if (build && build.options.length && !build.rules.programOnly) return { ok: false, why: T.comparisonOnly };
    if (!opt) return { ok: false, why: null };
    if (!optionIds(opt).length) return { ok: false, why: T.nothingToSave };
    return { ok: true, why: null };
  }
  // One filled button at a time. No build: Build options. A build that can be applied: Review
  // and apply, with Rebuild as an outline. A build out of date: Rebuild, the outline in the brand
  // colour. Anything else (comparison only, nothing to save, the Registered tab): both outlines.
  function setTone(button, tone) {
    button.classList.remove('btn-primary', 'btn-neutral', 'btn-outline-primary');
    button.classList.add(tone);
  }
  function renderActions() {
    const building = state.inflight > 0;
    const build = $('plbBuild');
    build.textContent = state.build ? T.rebuild : T.buildFirst;
    build.disabled = !state.ctx || building || state.applying;
    if (building) build.setAttribute('aria-busy', 'true'); else build.removeAttribute('aria-busy');
    const verdict = applicability();
    if (!state.build) { setTone(build, 'btn-primary'); setTone($('plbReview'), 'btn-neutral'); }
    else if (state.build.stale) { setTone(build, 'btn-outline-primary'); setTone($('plbReview'), 'btn-neutral'); }
    else if (verdict.ok) { setTone(build, 'btn-neutral'); setTone($('plbReview'), 'btn-primary'); }
    else { setTone(build, 'btn-neutral'); setTone($('plbReview'), 'btn-neutral'); }
    $('plbReview').disabled = !verdict.ok || building || state.applying;
    const reviewOpt = currentOption();
    $('plbReview').replaceChildren(...(reviewOpt ? T.reviewOpt(reviewOpt.name) : [T.review]));
    renderApplyStatus();
    const why = $('plbApplyWhy');
    why.textContent = verdict.why || '';
    why.hidden = !verdict.why;
    const stale = !!(state.build && state.build.stale);
    $('plbActs').dataset.stale = stale ? state.build.stale : '';
    if (stale) $('plbReview').setAttribute('aria-describedby', 'plbBuildLine');
    else $('plbReview').removeAttribute('aria-describedby');
    renderWhy();
    renderGuide();
    renderGrad();
    holdBandHeight(false);
    // Beside Rebuild: whether the options below still match the courses and rules (nothing before a
    // build, nor while one runs: a new build retires the last one at once). A live region: it is never hidden, so the line it says is in the
    // accessibility tree already when its words change, and a screen reader announces it ("Options
    // are out of date — rebuild"). Empty, it takes no room (the stylesheet). Only a change of words
    // is written: a redraw that says the same is not announced again.
    const bstate = $('plbBuildState');
    const row = bstate.closest('.plb-buildrow');
    const said = state.build ? (state.build.stale ? T.optionsStale : T.optionsCurrent) : '';
    bstate.dataset.kind = state.build && state.build.stale ? 'warn' : 'ok';
    if (bstate.textContent !== said) bstate.textContent = said;
    row.dataset.stale = state.build && state.build.stale ? 'true' : '';
    const last = $('plbLastBuild');
    if (state.build) last.replaceChildren(`${T.lastBuild} `, bdi(state.build.at));
    else last.replaceChildren();
    last.hidden = !state.build;
    const note = $('plbBuildNote');
    if (state.build && !state.build.options.length) {
      note.replaceChildren(
        el('p', 'plb-buildnote-lead', state.build.constraints.credit_room === 0 ? T.noRoomUnderLimit : T.noOption),
        el('ul', 'plb-buildnote-list', ...reasonLines(state.build).map((parts) => el('li', '', ...parts))));
      note.hidden = false;
    } else {
      note.replaceChildren();
      note.hidden = true;
    }
  }

  const methodOf = (opt) => String(opt.method || opt.name || '').charAt(0).toUpperCase();
  // "Why these options": what each method tries to do, how many options it gave, and what was left out.
  function renderWhy() {
    const build = state.build;
    const button = $('plbWhyBtn');
    const panel = $('plbWhy');
    if (!build) state.whyOpen = false;
    button.hidden = !build;
    button.setAttribute('aria-expanded', String(state.whyOpen));
    panel.hidden = !build || !state.whyOpen;
    if (panel.hidden) { panel.replaceChildren(); return; }
    const lines = [];
    const reasonOf = whyNotScheduled;
    // What the build as a whole left out is said once, below: not again under every option.
    const everywhere = new Set(build.unscheduled.map((u) => `${String(u.course_code || '')}|${reasonOf(u)}`));
    for (const opt of build.options) {
      const groups = new Map();
      for (const u of opt.unscheduled || []) {
        const why = reasonOf(u);
        if (everywhere.has(`${String(u.course_code || '')}|${why}`)) continue;
        if (!groups.has(why)) groups.set(why, []);
        if (u.course_code) groups.get(why).push(String(u.course_code));
      }
      for (const [why, codes] of groups) lines.push([bdi(opt.name), ': ', ...(codes.length ? [...T.notScheduledList(codes), ' — '] : []), why]);
    }
    if (build.failures.length || build.unscheduled.length) lines.push(...reasonLines(build));
    const allPlaced = !lines.length && build.options.length && build.options.every((o) => !unplacedFor(o).length);
    // Each method reads: "Method B (3 options). Looks for a compact week ...".
    panel.replaceChildren(...present(
      el('ul', 'plb-why-list', ...['A', 'B', 'C'].map((letter) => el('li', '',
        el('b', '', ...T.whyMethodName(letter)), ' ',
        el('span', 'plb-why-n', `(${T.whyCount(build.options.flatMap((o) => [o, ...(o.aliases || [])]).filter((o) => methodOf(o) === letter).length)}).`),
        ' ', T.whyMethod[letter]))),
      lines.length ? el('p', 'plb-why-h', T.whyUnscheduled) : null,
      lines.length ? el('ul', 'plb-why-list', ...lines.map((parts) => el('li', '', ...parts))) : null,
      allPlaced ? el('p', 'plb-why-p', T.whyAllPlaced) : null));
  }
  function setWhy(open, focus) {
    state.whyOpen = open;
    renderWhy();
    if (focus) $('plbWhyBtn').focus();
  }

  /* ── Projected graduation ──────────────────────────────────────────────── */
  const isObj = (value) => !!value && typeof value === 'object' && !Array.isArray(value);
  const termLabel = (pos) => `${pos.academic_year}/${pos.term}`;
  // The reply of the projection endpoint, once it is believed; null before, and for any other state.
  function projectionOf() {
    const projection = state.build && state.build.projection;
    return projection && projection.state === 'ready' ? projection : null;
  }
  // What the forecast says about one option: its row, "unavailable" when the reply has none, null when
  // there is no projection to read.
  function forecastFor(name) {
    const projection = projectionOf();
    if (!projection) return null;
    const row = projection.options[name];
    return isObj(row) ? row : { status: 'unavailable' };
  }
  const isComplete = (row) => !!row && row.status === 'complete'
    && Number.isInteger(row.terms_including_planning_term) && row.terms_including_planning_term > 0;
  // "4 terms" in bold, to open the first facts line of a tab or an item; null when the forecast is not complete.
  function termsNode(row) {
    return isComplete(row) ? el('b', 'plb-tab-terms', T.gradTermCount(row.terms_including_planning_term)) : null;
  }
  // What one course does to the SELECTED option's forecast, from the projection's `course_effects`.
  // group 'new' reads what postponing a planned course costs; 'could' what adding one would do.
  // null for anything the reply has no usable effect for (also on Registered, while loading, unavailable).
  function effectFor(code, group) {
    const opt = currentOption();
    const row = opt ? forecastFor(opt.name) : null;
    if (!isComplete(row) || !isObj(row.course_effects)) return null;
    const map = row.course_effects[group === 'new' ? 'postponed' : 'added'];
    const effect = isObj(map) ? map[norm(code)] : null;
    if (!isComplete(effect) || !Number.isInteger(effect.delta_terms)) return null;
    const delta = effect.delta_terms;
    // A course that leaves the graduation term where it is says nothing.
    if (delta === 0) return null;
    if (group === 'new') {
      return delta > 0 ? { text: T.ifPostponed(delta), tone: 'strong', delta } : null;
    }
    return { text: T.gradTiming(delta), tone: delta < 0 ? 'brand' : 'muted', delta };
  }
  function effectNode(code, group) {
    const effect = effectFor(code, group);
    if (!effect) return null;
    const node = el('span', `plb-fxt plb-fxt--${effect.tone} plb-flag`, effect.text);
    node.dataset.fx = group;
    if (state.build && state.build.stale) node.dataset.stale = state.build.stale;
    return node;
  }
  // The planned courses whose postponement costs the most terms (at most two): "AI331 · if postponed: 5 terms".
  function dependsOn() {
    return state.shortlist
      .map((item) => ({ code: norm(item.course_code), effect: effectFor(item.course_code, 'new') }))
      .filter((entry) => entry.effect && entry.effect.delta > 0)
      .sort((a, b) => b.effect.delta - a.effect.delta || (a.code < b.code ? -1 : 1))
      .slice(0, 2);
  }
  const creditList = (row) => (Array.isArray(row.credits_by_term) ? row.credits_by_term.filter(isObj) : []);
  const completionParts = (row) => (row.completion_academic_year && row.completion_term
    ? T.gradTerm(row.completion_academic_year, row.completion_term) : []);
  function resultOf(delta) {
    if (!Number.isInteger(delta)) return null;
    if (delta === 0) return { kind: 'same', text: T.gradSame, said: T.gradSameSaid };
    return { kind: delta < 0 ? 'sooner' : 'later', text: T.gradTiming(delta) };
  }

  /* The sentences behind "Course-level reasons": every code the server can send, in the current page's words. */
  const gradPos = (pos) => (isObj(pos) && pos.academic_year && pos.term ? T.gradTerm(pos.academic_year, pos.term) : []);
  const joinCodes = (codes) => codes.flatMap((code, i) => (i ? [T.gradSep, bdi(code)] : [bdi(code)]));
  function gradReason(reason) {
    if (!isObj(reason)) return null;
    const course = reason.course_code ? bdi(reason.course_code) : '';
    const related = reason.related_course_code ? bdi(reason.related_course_code) : '';
    const before = gradPos(reason.before);
    const after = gradPos(reason.after);
    switch (reason.code) {
      case 'same_course_set':
        return [IS_AR ? 'يتضمن الخيار المقررات نفسها في التسجيل الحالي.' : 'This option includes the same courses as current registration.'];
      case 'course_moved_earlier':
      case 'course_moved_later':
        return IS_AR ? ['انتقل ', course, ' من ', ...before, ' إلى ', ...after, '.'] : [course, ' moves from ', ...before, ' to ', ...after, '.'];
      case 'prerequisite_moved_earlier':
      case 'prerequisite_moved_later': {
        const earlier = reason.code === 'prerequisite_moved_earlier';
        if (IS_AR) {
          return ['انتقل المتطلب السابق ', course, ' من ', ...before, ' إلى ', ...after, '، وانتقل المقرر ', related,
            earlier ? ' أيضًا إلى فصل أبكر.' : ' أيضًا إلى فصل لاحق.'];
        }
        return ['Prerequisite ', course, ' moves from ', ...before, ' to ', ...after, '. Its dependent course ', related,
          earlier ? ' also moves earlier.' : ' also moves later.'];
      }
      case 'requirement_resolved':
        return IS_AR ? ['أمكن إدراج ', course, ' في التوقع.'] : [course, ' can now be placed in the projection.'];
      case 'requirement_unresolved':
        return IS_AR ? ['تعذّر إدراج ', course, ' في التوقع.'] : [course, ' could not be placed in the projection.'];
      case 'course_added':
        return IS_AR ? ['يتضمن هذا الخيار ', course, ' في فصل التخطيط.'] : ['This option includes ', course, ' in the planning term.'];
      case 'course_removed':
        return IS_AR ? ['يخلو هذا الخيار من ', course, ' في فصل التخطيط.'] : ['This option omits ', course, ' from the planning term.'];
      case 'academic_plan_unavailable':
        return [IS_AR ? 'الخطة الدراسية غير متاحة.' : 'The academic plan is unavailable.'];
      case 'no_registered_timetable':
        return [IS_AR ? 'يلزم وجود جدول مسجّل رسميًا لمقارنة موعد التخرّج.' : 'A registrar timetable is needed to compare graduation dates.'];
      case 'unsupported_planning_term':
        return [IS_AR ? 'يشمل توقع التخرّج الفصلين الدراسيين الأول والثاني فقط.' : 'Graduation forecasts are available for regular terms 1 and 2.'];
      case 'unmet_course_prerequisites': {
        const missing = joinCodes(Array.isArray(reason.missing_prerequisites) ? reason.missing_prerequisites : []);
        return IS_AR
          ? ['لم تكتمل متطلبات ', course, ' قبل فصل التخطيط: ', ...missing, '.']
          : [course, ' requires ', ...missing, ' before the planning term.'];
      }
      case 'section_metadata_unavailable':
        return [IS_AR ? 'بيانات إحدى الشعب غير كافية لحساب التوقع.' : 'Section data is incomplete for this projection.'];
      default:
        return null;
    }
  }
  function gradBlocked(requirement) {
    const code = bdi(requirement.code);
    const prerequisites = Array.isArray(requirement.missing_course_prerequisites) ? requirement.missing_course_prerequisites : [];
    const gates = [];
    if (prerequisites.length) gates.push([IS_AR ? 'المتطلبات السابقة المتبقية ' : 'missing prerequisites ', ...joinCodes(prerequisites)]);
    const hours = requirement.credit_hour_gate ? Number(requirement.credit_hour_gate.remaining) : 0;
    if (hours > 0) gates.push([IS_AR ? `الساعات الإضافية المطلوبة ${hours}` : `${hours} more credits required`]);
    if (requirement.project_sequence_gate) gates.push([IS_AR ? 'قيد تسلسل مقررات المشروع' : 'project sequence requirement']);
    if (!gates.length) return IS_AR ? ['تعذّر إدراج ', code, ' في التوقع.'] : [code, ' could not be placed in the projection.'];
    return [code, ' · ', ...gates.flatMap((gate, i) => (i ? [T.gradSep, ...gate] : gate))];
  }
  // An option's reasons, then the requirements it cannot place. A "could not be placed" reason is
  // skipped when the same course is listed as a blocked requirement (the current page's rule).
  function gradWhyItems(row) {
    // A requirement with no course code names nothing: it is not listed.
    const blocking = (Array.isArray(row.unresolved_requirements) ? row.unresolved_requirements : [])
      .filter((r) => isObj(r) && r.code);
    const detailed = new Set(blocking.map((r) => r.code));
    const items = (Array.isArray(row.reasons) ? row.reasons : [])
      .filter((r) => !(isObj(r) && r.code === 'requirement_unresolved' && detailed.has(r.course_code)))
      .map(gradReason).filter(Boolean);
    items.push(...blocking.map(gradBlocked));
    return items;
  }

  /* The runway: two lines on one term axis. */
  function tickNodes(list) {
    return list.map((entry, i) => {
      const waiting = !!entry.waiting;
      const credits = Number(entry.credits) || 0;
      const tick = el('div', `plb-tk${waiting ? ' plb-tk--wait' : ''}${i === list.length - 1 ? ' plb-tk--end' : ''}`,
        waiting ? '–' : String(credits),
        i === 0 && !waiting ? el('span', 'plb-u', T.creditUnit(credits)) : null);
      tick.setAttribute('aria-hidden', 'true');
      tick.title = waiting ? T.gradWaiting : `${termLabel(entry)} · ${T.credits(credits)}`;
      return tick;
    });
  }
  // The whole line as one sentence for a screen reader; the ticks and the axis are hidden from it.
  function gradSentence(labelParts, row, result, list) {
    const parts = [...labelParts, ': ', T.gradTermCount(row.terms_including_planning_term), T.gradCompletes, ...completionParts(row)];
    if (result) parts.push(T.gradSep, result.said || result.text);
    parts.push('. ', T.gradPerTerm, ': ');
    list.forEach((entry, i) => {
      if (i) parts.push(T.gradSep);
      parts.push(bdi(termLabel(entry)), ' ', entry.waiting ? T.gradNoCourses : String(Number(entry.credits) || 0));
    });
    parts.push('.');
    return el('p', 'sr-only', ...parts);
  }
  function gradNote(kind, row) {
    if (kind === 'reg' && row && row.reason_code === 'no_registered_timetable') return T.gradNoReg;
    return row && row.status === 'unresolved' ? T.gradUnresolved : T.gradUnavailable;
  }
  function gradLine(kind, row, label, result) {
    const line = el('div', `plb-rwl plb-rwl--${kind}`);
    if (!isComplete(row)) {
      line.classList.add('plb-rwl--note');
      line.append(
        el('div', 'plb-rl', el('span', 'plb-st', el('span', 'plb-mic', ...label()))),
        el('p', 'plb-rwn', gradNote(kind, row)));
      return line;
    }
    const list = creditList(row);
    const count = row.terms_including_planning_term;
    // The option's result (its completion term and how it compares) is the band's header; only
    // the registered line ends with its term.
    let land = null;
    if (kind !== 'opt') {
      land = el('div', 'plb-land', el('b', '', ...completionParts(row)));
      land.style.setProperty('--plb-from', String(list.length + 2));
    }
    line.append(
      el('div', 'plb-rl',
        el('span', 'plb-num', String(count)),
        el('span', 'plb-st', el('b', '', T.gradTermWord(count)), el('span', 'plb-mic', ...label()))),
      el('i', 'plb-brk'),
      ...tickNodes(list),
      ...(land ? [land] : []),
      gradSentence(label(), row, result, list));
    return line;
  }
  function gradAxis(list, cols) {
    const axis = el('div', 'plb-rwl plb-rwl--ax', el('span', 'plb-axn plb-mic', T.gradPerTerm), el('i', 'plb-brk'));
    axis.setAttribute('aria-hidden', 'true');
    // Every column starts with its label; fitRunway() thins them to what the cells have room for.
    // The ticks keep a title either way.
    for (let i = 0; i < cols; i += 1) {
      const entry = list[i];
      const cell = el('span', `plb-ax${i === 0 ? ' plb-ax--now' : ''}`, entry ? bdi(termLabel(entry)) : null);
      if (entry) cell.dataset.term = termLabel(entry);
      axis.append(cell);
    }
    return axis;
  }
  const spanOf = (cols) => (cols <= 5 ? 's' : cols <= 8 ? 'm' : cols <= 14 ? 'l' : 'xl');
  function gradRunway(projection) {
    const opt = currentOption();
    const regRow = isObj(projection.baseline) ? projection.baseline : { status: 'unavailable' };
    const optRow = opt ? forecastFor(opt.name) : null;
    const lines = [];
    if (opt) {
      const result = isComplete(optRow) && isComplete(regRow) ? resultOf(optRow.delta_terms) : null;
      lines.push(gradLine('opt', optRow, () => T.optionName(opt.name), result));
    }
    lines.push(gradLine('reg', regRow, () => [T.gradAsRegistered], null));
    const lists = [optRow, regRow].filter(isComplete).map(creditList);
    const longest = lists.reduce((best, list) => (list.length > best.length ? list : best), []);
    const cols = longest.length;
    const runway = el('div', 'plb-rw', ...lines, cols ? gradAxis(longest, cols) : null);
    runway.dataset.span = spanOf(cols);
    runway.style.setProperty('--plb-cols', String(Math.max(cols, 1)));
    return runway;
  }

  /* Fitting the runway. Whether the row layout has room for its texts, how many term labels the
     axis can carry, whether the first tick can keep its unit and how large the result can be all
     depend on the language and on the fonts that actually loaded. So they are measured, not
     assumed: after every draw, when the band's width changes, and when the web fonts arrive. The
     stylesheet makes only the first guess (the row layout from a width per number of columns). */
  const AXIS_GAP = 4;  // least room, in px, between two neighbouring term labels
  const TICK_GAP = 2;  // and between the texts of two neighbouring ticks
  function textBox(node) {
    const range = document.createRange();
    range.selectNodeContents(node);
    return range.getBoundingClientRect();
  }
  // The least horizontal room between neighbouring texts; Infinity when there are fewer than two.
  function leastGap(nodes) {
    const boxes = nodes.filter((node) => node.textContent.trim()).map(textBox).sort((a, b) => a.left - b.left);
    let least = Infinity;
    for (let i = 1; i < boxes.length; i += 1) least = Math.min(least, boxes[i].left - boxes[i - 1].right);
    return least;
  }
  // A label on every `step`-th column, counted from the first; 0 keeps the first label only.
  function labelAxis(runway, step) {
    runway.querySelectorAll('.plb-ax').forEach((cell, i) => {
      const show = cell.dataset.term && (step ? i % step === 0 : i === 0);
      cell.replaceChildren(...(show ? [bdi(cell.dataset.term)] : []));
    });
  }
  const axisFits = (runway) => leastGap([...runway.querySelectorAll('.plb-ax')]) >= AXIS_GAP;
  const ticksFit = (runway) => [...runway.querySelectorAll('.plb-rwl--opt, .plb-rwl--reg')]
    .every((line) => leastGap([...line.querySelectorAll('.plb-tk')]) >= TICK_GAP);
  const within = (box, outer) => box.left >= outer.left - 0.5 && box.right <= outer.right + 0.5;
  function fitRunway() {
    const grad = $('plbGrad');
    const runway = grad.querySelector('.plb-rw');
    if (!runway) return;
    const band = grad.getBoundingClientRect();
    if (!band.width) return;                      // not laid out: nothing to measure
    const isRow = () => getComputedStyle(runway).display === 'grid';
    // Start from the stylesheet's own choice, with every label, the unit, and the result at
    // reading size.
    delete runway.dataset.fit;
    delete runway.dataset.unit;
    labelAxis(runway, 1);
    if (isRow()) {
      // The row layout may drop the unit (the caption says it) and thin its labels to every
      // other one. If its ticks still touch, its labels still crowd or a text leaves the band,
      // the runway is stacked instead.
      if (!ticksFit(runway)) runway.dataset.unit = 'off';
      if (!axisFits(runway)) labelAxis(runway, 2);
      // (An element that is not drawn has an empty box at the origin: it is not "outside".)
      const inBand = (node) => {
        const box = node.getBoundingClientRect();
        return (!box.width && !box.height) || within(box, band);
      };
      const texts = [...runway.querySelectorAll('.plb-rl, .plb-land, .plb-land > *, .plb-rwn')];
      if (!ticksFit(runway) || !axisFits(runway) || !texts.every(inBand)) {
        runway.dataset.fit = 'stacked';
        delete runway.dataset.unit;
        labelAxis(runway, 1);
      }
    }
    if (!isRow()) {
      if (!ticksFit(runway)) runway.dataset.unit = 'off';
      for (const step of [2, 4, 0]) {
        if (axisFits(runway)) break;
        labelAxis(runway, step);
      }
      return;
    }
  }

  // The band's header: what the selected option graduates with, its completion term and how it
  // compares to the registered week. Only from a complete forecast; otherwise the line above speaks.
  function renderGradHead(ready) {
    const top = $('plbGradTop');
    const opt = currentOption();
    const regRow = ready && isObj(ready.baseline) ? ready.baseline : null;
    const row = ready ? (opt ? forecastFor(opt.name) : regRow) : null;
    top.hidden = !isComplete(row);
    if (top.hidden) { $('plbGradTerm').replaceChildren(); $('plbGradPill').hidden = true; return; }
    $('plbGradMic').replaceChildren(...(opt ? T.gradWith(opt.name) : [T.gradAsRegistered]));
    $('plbGradTerm').replaceChildren(...completionParts(row));
    const pill = $('plbGradPill');
    const result = opt && isComplete(regRow) ? resultOf(row.delta_terms) : null;
    pill.hidden = !result;
    pill.textContent = result ? result.text : '';
    if (result) pill.dataset.result = result.kind;
  }
  function renderGrad() {
    // "Graduates as it stands" reads the same forecast; the band is drawn again only when its word changes.
    if (state.student && bandEndsKey() !== state.bandEnds) renderStudent();
    const grad = $('plbGrad');
    const hadFocus = grad.contains(document.activeElement);
    const line = $('plbGradLine');
    const retry = $('plbGradRetry');
    const whyBtn = $('plbGradWhyBtn');
    const why = $('plbGradWhy');
    const build = state.build;
    const projection = build && build.projection ? build.projection : null;
    const kind = projection ? projection.state : 'none';
    const sayings = {
      none: T.gradNone, loading: T.gradLoading, expired: T.gradExpired, unavailable: T.gradUnavailable,
      busy: T.gradBusy, signedout: T.sessionExpired, ready: '',
    };
    grad.dataset.state = kind;
    if (build && build.stale) grad.dataset.stale = build.stale; else delete grad.dataset.stale;
    line.textContent = sayings[kind] || '';
    line.hidden = kind === 'ready';
    const canRetry = kind === 'unavailable' || kind === 'busy';
    retry.hidden = !canRetry;
    retry.textContent = canRetry ? T.gradRetry : '';

    // What follows the line: the runway, the assumptions, the warning and the reasons.
    const ready = kind === 'ready' ? projection : null;
    $('plbGradRunway').replaceChildren(...(ready ? [gradRunway(ready)] : []));
    const asm = $('plbGradAsm');
    const kept = $('plbGradRetained');
    const stale = $('plbGradStale');
    asm.hidden = true;
    kept.hidden = true;
    stale.hidden = !(ready && build.stale);
    asm.replaceChildren();
    kept.replaceChildren();
    stale.textContent = ready && build.stale ? T.gradExpired : '';
    let items = [];
    let disclosure = false;
    if (ready) {
      const opt = currentOption();
      const row = opt ? forecastFor(opt.name) : null;
      // The assumptions explain a forecast: with no line drawn there is nothing for them to explain.
      const drawn = isComplete(ready.baseline) || isComplete(row);
      const cap = Number.isFinite(Number(ready.cap)) && Number(ready.cap) > 0 ? Number(ready.cap) : 18;
      const planning = isObj(ready.planning) && ready.planning.academic_year && ready.planning.term
        ? termLabel(ready.planning)
        : (state.ctx ? `${state.ctx.year}/${state.ctx.term}` : '');
      asm.hidden = !drawn;
      if (drawn) asm.replaceChildren(...T.gradAssumes(planning, cap));
      const keptText = drawn
        ? { expected_plan: T.gradRetainedExpected, working_timetable: T.gradRetainedWorking }[ready.retainedKind]
        : '';
      kept.hidden = !keptText;
      kept.textContent = keptText || '';
      if (opt) {
        items = gradWhyItems(row);
        // A complete forecast keeps its reasons behind the button; one that failed lists them at once.
        disclosure = isComplete(row) && items.length > 0;
      }
    }
    whyBtn.hidden = !disclosure;
    whyBtn.setAttribute('aria-expanded', String(state.gradWhy));
    whyBtn.replaceChildren(T.gradReasons, svg(ICONS.chev, 12));
    const open = items.length > 0 && (disclosure ? state.gradWhy : true);
    why.hidden = !open;
    why.replaceChildren(...(open ? items.map((parts) => el('li', '', ...parts)) : []));
    renderGradHead(ready);
    fitRunway();
    holdBandHeight(false);

    // A control that was just hidden took the keyboard with it: keep it in the band.
    if (hadFocus) {
      const here = document.activeElement;
      if (!here || !grad.contains(here) || here.closest('[hidden]')) {
        const next = [retry, whyBtn, line].find((node) => !node.closest('[hidden]')) || grad;
        next.focus();
      }
    }
  }
  // The tabs sit under the band and must not jump away from the pointer as the page changes
  // tab: the band is as tall as its tallest column, and the guidance and the runway differ from
  // one option to the next. So within one build, side by side, it never gets shorter than it has
  // been (a change of width starts the count again).
  function holdBandHeight(reset) {
    const band = document.querySelector('.plb-band');
    if (!band) return;
    band.style.minHeight = '';
    const build = state.build;
    if (!build) return;
    if (reset) build.bandHeight = 0;
    build.bandHeight = Math.max(build.bandHeight || 0, band.getBoundingClientRect().height);
    band.style.minHeight = `${build.bandHeight}px`;
  }
  function setGradWhy(open, focus) {
    state.gradWhy = open;
    renderGrad();
    if (focus && !$('plbGradWhyBtn').hidden) $('plbGradWhyBtn').focus();
  }

  // One request per build: the options kept on screen are forecast from the build's own signed token.
  // A reply is believed only if its build is still on screen and it answers the newest request for
  // it. It is NOT tied to buildGen: a plan or rule change only marks the build out of date, and the
  // forecast of the options still shown must still arrive.
  // `quiet` is the second request of a build first forecast before its "Could be added" rows were
  // known: a forecast already on screen stays there and is replaced only by a good reply.
  async function loadProjection(build, quiet) {
    const run = (build.projRun += 1);
    const kept = quiet && build.projection && build.projection.state === 'ready' ? build.projection : null;
    if (!kept) {
      build.projection = { state: 'loading' };
      renderGrad();
    }
    // "What if" the first six "Could be added" rows, in the table's order, were added. Fixed when the
    // build is first forecast: a later change to the list does not re-ask, a retry asks the same.
    // While the plan is still loading there are no rows to name: loadPlan() asks again once it has them.
    if (!build.addCodes && state.planState !== 'loading') build.addCodes = planPool().slice(0, 6).map((c) => c.code);
    const body = { token: build.token };
    if (build.addCodes && build.addCodes.length) body.add = build.addCodes;
    let result = null;
    try { result = await postJson('/ops/planner/graduation-projection/', body); } catch (err) { result = null; }
    if (state.build !== build || run !== build.projRun) return;
    let next = { state: 'unavailable' };
    if (result) {
      const { response, data } = result;
      if (sessionGone(response, data)) next = { state: 'signedout' };
      else if (response.status === 409) next = { state: 'expired' };
      else if (response.status === 429) next = { state: 'busy' };
      else if (response.ok && isObj(data) && data.ok && isObj(data.baseline) && isObj(data.options)) {
        next = {
          state: 'ready',
          baseline: data.baseline,
          options: data.options,
          retainedKind: data.retained_baseline_kind || null,
          cap: data.future_max_credits_per_term,
          planning: data.planning_term,
        };
      }
    }
    // The forecast on screen stays unless a good reply replaces it, or the session has gone.
    if (kept && next.state !== 'ready' && next.state !== 'signedout') return;
    build.projection = next;
    keepingFocus(() => {
      renderStrip();
      renderTab(weekModel(registeredMeetings()));
      renderCourses();
      renderActions();
    });
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
      // Ignore: the registered courses held as load only (the server checks each one is).
      ...(state.rules.mode === 'ignore' ? { registered_load_only: state.loadOnly.filter((r) => finitePositive(r.credits)).map((r) => r.key) } : {}),
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
    state.whyOpen = false;
    state.gradWhy = false;
    const gen = ++state.buildGen;
    const rules = { mode: state.rules.mode, programOnly: state.rules.programOnly, maxCredits: state.rules.maxCredits };
    const shortlist = JSON.parse(JSON.stringify(state.shortlist));
    // What the options are compared with (Changes against the registration): the registered
    // courses and the load-only rows as they were when this build was asked for.
    const registered = state.converted ? JSON.parse(JSON.stringify(state.converted.regs)) : [];
    const loadOnly = JSON.parse(JSON.stringify(state.loadOnly));
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
      const distinct = feasible ? distinctOptions(options) : [];
      state.build = {
        options: distinct,
        shortlist,
        rules,
        registered,
        loadOnly,
        madeAt: now.getTime(),
        stale: null,
        at: `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`,
        failures: Array.isArray(summary.hard_constraint_failures) ? summary.hard_constraint_failures : [],
        unscheduled: Array.isArray(data.unscheduled) ? data.unscheduled : [],
        constraints: isObj(data.constraints) ? data.constraints : {},
        token: typeof data.graduation_projection_token === 'string' ? data.graduation_projection_token : '',
        projection: null,
        projRun: 0,
      };
      // The tabs are laid out now, in the order WITHOUT the terms key (no forecast yet), and keep
      // that layout for the build: the forecast moves only the Suggested label and the guidance.
      state.build.options = rankOptions(distinct, false);
      state.selected = state.build.options.length ? String(state.build.options[0].name) : null;
      setBuildLine('');
      renderStrip();
      renderWeek();
      renderCourses();
      // The forecast of what the page kept; no options or no token: no request.
      if (state.build.options.length && state.build.token) loadProjection(state.build);
    } finally {
      renderActions();
      if (!state.inflight && (hadFocus || document.activeElement === document.body) && !button.disabled) button.focus();
    }
  }

  function openApply() {
    if (checkAge()) return;
    const opt = currentOption();
    if (!opt || !applicability().ok || state.applying) return;
    const build = state.build;
    $('plbApplyTitle').replaceChildren(...(IS_AR ? ['مراجعة وتطبيق الخيار ', bdi(opt.name)] : ['Review and apply option ', bdi(opt.name)]));
    $('plbApplySub').replaceChildren(bdi(`${state.ctx.studentId} · ${state.ctx.year}/${state.ctx.term}`));
    $('plbApplyList').replaceChildren(...(opt.mappings || []).filter((m) => m.term_section_id).map((m) => {
      const key = courseKeyOf(m) || String(m.course_code || '');
      const item = build.shortlist.find((c) => norm(c.course_code) === key);
      return el('li', 'plb-apply-row',
        el('span', '', bdi(key), ...(m.section ? [' ', bdi(`§${String(m.section).trim()}`)] : []), ...(courseName(key, item && item.course_name) ? [' ', isolate(courseName(key, item && item.course_name))] : [])),
        el('span', 'plb-apply-when', ...whenNodes(m.meetings)));
    }));
    const registered = registeredCourses();
    const changes = changesOf(opt);
    let said = T.droppedRegistered;
    if (build.rules.mode === 'keep') {
      said = el('span', '', T.keptRegistered(registered.length), ' ',
        ...registered.flatMap((c, i) => (i ? [' · ', bdi(c.code)] : [bdi(c.code)])));
    } else if (changes) said = el('span', '', T.applyConverted, ' ', T.chgLead, ...changesNodes(changes));
    $('plbApplyReg').replaceChildren(said);
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
    checkAge();
    const opt = currentOption();
    if (!opt || !applicability().ok) {
      // Nothing applicable is left to save (the options went, or went out of date, while this
      // was open): never go quiet. A stale build already says why in the status line.
      closeApply();
      if (!(state.build && state.build.stale)) setBuildLine('warn', T.planChanged);
      return;
    }
    const savedIds = optionIds(opt);
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
    // An edited plan says so, and which registered courses the adviser took out of it: the clash
    // check sets their registrar rows aside (and those of the courses this option places).
    const built = state.build;
    const edited = built.rules.mode === 'ignore';
    const changes = changesOf(opt);
    if (edited) {
      body.mode = 'ignore';
      body.dropped_registered = (built.registered || []).map((c) => norm(c.key)).filter((key) => (
        !built.shortlist.some((it) => norm(it.course_code) === key) && !(built.loadOnly || []).some((r) => norm(r.key) === key)));
    }
    // After the save the board stays as built (decision 3): the plan comes back after the reload.
    const restore = edited && state.converted ? {
      shortlist: JSON.parse(JSON.stringify(state.shortlist)),
      loadOnly: JSON.parse(JSON.stringify(state.loadOnly)),
      converted: JSON.parse(JSON.stringify(state.converted)),
    } : null;
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
    // Where the plan went: the sections are saved as the student's planned (working) timetable.
    // The registered rows are not touched, and the registrar's rows outrank these when the page
    // reads the student back (Snapshot.EFFECTIVE), so a student with registrations looks unchanged.
    const sentences = [T.savedWhere(savedIds.length, ctx.year, ctx.term)];
    if (removed.length) {
      sentences.push([T.removedPlanned(removed.length), ' (',
        ...removed.flatMap((x, i) => [i ? ', ' : '', bdi(`${x.course_code || ''} ${x.section || ''}`.trim())]), ').']);
    }
    if (clashes.length) {
      sentences.push([T.clashSaved(clashes.length), ' (',
        ...clashes.flatMap((x, i) => [i ? ', ' : '', bdi(`${x.course_code || ''} ${x.section || ''} · ${x.clashes_with || ''}`.trim())]), ').']);
    }
    // The registrar's timetable is unchanged; what the option changes against it is said.
    if (changes) sentences.push([T.chgLead, ...changesNodes(changes), '.']);
    setBuildLine('busy', T.loading);
    const reloaded = state.ctx && state.ctx.studentId === ctx.studentId ? await loadStudent({ reuse: true, restore }) : false;
    if (reloaded) {
      // With no registrar timetable the saved sections are what this page reads back as the week.
      if (state.baseline.some((row) => String(row.source || '') === 'planner')) sentences.splice(1, 0, [T.savedShown]);
      const said = sentences.flatMap((parts, i) => (i ? [' ', ...parts] : parts));
      setBuildLine(removed.length || clashes.length ? 'warn' : 'ok', ...said);
      setStatus('ok', T.saved);
    } else {
      setBuildLine('warn', T.savedNoReload);
    }
    renderActions();
    const build = $('plbBuild');
    if (!build.disabled) build.focus();
  }

  /* ── The course detail panel ───────────────────────────────────────────── */
  function detailInfo() {
    const d = state.detail;
    if (!d) return null;
    const item = state.shortlist.find((c) => norm(c.course_code) === d.code);
    if (item) return { inPlan: true, item, name: courseName(item.course_code, item.course_name), credits: Number(item.credits) || 0 };
    return null;
  }
  function sectionClashes(section) {
    const registered = registeredMeetings();
    return (section.meetings || []).some((mm) => {
      const day = normDay(mm.day);
      const st = toMins(mm.start_time);
      const en = toMins(mm.end_time);
      return !!day && st !== null && en !== null && en > st
        && registered.some((r) => r.day === day && st < r.en && r.st < en);
    });
  }
  // The sections the student holds: their own is never full or unusable for them.
  const ownSections = () => new Set(planRegistered().map((c) => String(c.tsid)).filter((id) => id && id !== 'null'));
  // Ignore: the Required rows pinned to one section that this section would clash with.
  function requiredClashes(section) {
    if (state.rules.mode !== 'ignore') return [];
    const own = courseKeyOf(section);
    const mine = (section.meetings || []).map((mm) => ({ day: normDay(mm.day), st: toMins(mm.start_time), en: toMins(mm.end_time) }))
      .filter((m) => m.day && m.st !== null && m.en !== null && m.en > m.st);
    const out = [];
    for (const item of state.shortlist) {
      const pins = pinsOf(item);
      if (!item.must_take || pins.length !== 1 || norm(item.course_code) === own) continue;
      const other = findSection(pins[0].term_section_id);
      if (!other) continue;
      const hit = (other.meetings || []).some((mm) => {
        const day = normDay(mm.day);
        const st = toMins(mm.start_time);
        const en = toMins(mm.end_time);
        return day && st !== null && en !== null && mine.some((m) => m.day === day && m.st < en && st < m.en);
      });
      if (hit) out.push(`${shownCode(item.course_code)} §${pinLabel(pins[0])}`);
    }
    return out;
  }
  // What the panel says about one section.
  function sectionModel(section) {
    const cap = sectionCapacity(section);
    const own = ownSections().has(String(section.term_section_id));
    const reasons = [];
    if (cap.known && cap.full && !state.rules.allowFull && !own) reasons.push(T.cannotFull);
    const clash = state.rules.mode === 'keep' && sectionClashes(section);
    if (clash) reasons.push(T.cannotClash);
    const taken = finiteNonNegative(section.registered_count);
    const max = finiteNonNegative(section.available_capacity);
    let seats = T.seatsUnknown;
    // The student's own section is never "full" for them: its seats are said as a count.
    if (cap.known) seats = cap.full && !own ? T.seatsFull : T.seatsTaken(taken, max);
    const options = state.build
      ? state.build.options.filter((o) => (o.mappings || []).some((m) => String(m.term_section_id) === String(section.term_section_id))).map((o) => o.name)
      : [];
    return {
      id: String(section.term_section_id), label: String(section.section || '').trim(), cap, clash, reasons, seats,
      when: whenNodes(section.meetings), programmes: programmesOf(section), options, own,
      requiredClash: requiredClashes(section),
    };
  }
  // The allowed sections of a course: [{term_section_id, section}], at most MAX_ONLY.
  const MAX_ONLY = 6;
  const pinsOf = (item) => (item && Array.isArray(item.pinned_sections) ? item.pinned_sections : []);
  const pinLabel = (pin) => String(pin.section || '').trim() || '?';
  // A course's sections as the panel lists them (and as a request keeps them).
  function sortedSections(code) {
    return catalogSections(code).slice().sort((a, b) => String(a.section || '').localeCompare(String(b.section || ''), 'en', { numeric: true }));
  }
  // `ticked`: this section is one of the allowed ones; `atMax`: six are ticked already.
  function sectionRow(model, mode, ticked, atMax) {
    const inPlan = mode !== 'read';
    const row = el(inPlan ? 'label' : 'div', 'plb-dt-sec');
    const usable = !model.reasons.length;
    const input = el('input', 'plb-dt-radio');
    input.type = 'checkbox';
    input.name = 'plbOnly';
    input.value = model.id;
    input.dataset.fk = `sec-${model.id}`;
    input.checked = ticked;
    input.disabled = !ticked && (!usable || atMax);   // a tick that became unusable stays, flagged
    const flags = [];
    // A section that cannot be used says why in a sentence below: a chip saying the same is noise.
    if (model.own) flags.push(el('span', 'plb-flag plb-flag--own', T.ownSection));
    if (usable && model.cap.known && model.cap.full && !model.own) flags.push(el('span', 'plb-flag plb-flag--bad', T.seatsFull));
    if (fewSeats(model.cap) && !model.own) flags.push(el('span', 'plb-flag plb-flag--warn', T.seatsLeft(model.cap.remaining)));
    if (usable && model.clash) flags.push(el('span', 'plb-flag plb-flag--bad', T.clashFlag));
    for (const label of model.requiredClash) flags.push(el('span', 'plb-flag plb-flag--warn', ...T.clashRequired(label)));
    if (model.options.length) flags.push(el('span', 'plb-flag', ...T.inOptions(model.options)));
    row.dataset.tsid = model.id;
    if (!usable) row.dataset.unusable = 'true';
    if (model.own) row.dataset.own = 'true';
    row.append(...present(
      inPlan ? input : null,
      el('span', 'plb-dt-sec-body',
        el('span', 'plb-dt-sec-main', bdi(`§${model.label || '?'}`), model.when.length ? ' ' : null, ...model.when),
        el('span', 'plb-dt-sec-seats', ...T.forProgrammes(model.programmes), ' · ', model.seats),
        flags.length ? el('span', 'plb-dt-flags', ...flags) : null,
        !usable ? el('span', 'plb-dt-why', ticked ? T.tickBroken : null, model.reasons.join(' ')) : null)));
    return row;
  }
  // One of the two mode radios: "Any section" and "Only these".
  function modeRow(value, label, hint, checked) {
    const row = el('label', 'plb-dt-sec');
    const input = el('input', 'plb-dt-radio');
    input.type = 'radio';
    input.name = 'plbMode';
    input.value = value;
    input.checked = checked;
    input.dataset.fk = value;
    row.append(input, el('span', 'plb-dt-sec-body', el('span', 'plb-dt-sec-main', label), hint ? el('span', 'plb-dt-sec-seats', hint) : null));
    return row;
  }
  function renderDetail() {
    const aside = $('plbPop');
    const inside = aside.contains(document.activeElement);
    const focusKey = inside ? (document.activeElement.dataset.fk || 'title') : null;
    const info = detailInfo();
    if (!info) {
      state.detail = null;
      aside.hidden = true;
      aside.replaceChildren();
      markPlanRows();
      markOpenBlocks();
      syncAlt();
      // Focus was inside a popover that has gone: put it on the search box.
      if (inside && !$('plbFind').disabled) $('plbFind').focus();
      return;
    }
    const code = state.detail.code;
    const item = info.item || null;
    const pins = pinsOf(item);
    const tickedIds = new Set(pins.map((p) => String(p.term_section_id)));
    const atMax = pins.length >= MAX_ONLY;
    // "Only these" shows once a section is ticked, or once the adviser asked for it in this opening.
    const only = !!item && (pins.length > 0 || state.detail.only === true);
    const title = el('h3', 'plb-dt-title', bdi(shownCode(code)), info.name ? ' ' : null, info.name ? isolate(info.name) : null,
      el('span', 'plb-dt-cr', ` · ${item && item.credits_unknown ? T.creditsUnknown : T.credits(info.credits)}`));
    title.id = 'plbDetailTitle';
    title.tabIndex = -1;
    title.dataset.fk = 'title';
    const close = el('button', 'plb-dt-close', T.close);
    close.type = 'button';
    close.dataset.act = 'dt-close';
    close.dataset.fk = 'close';
    const parts = [el('div', 'plb-dt-head', title, close)];
    const effect = effectNode(code, 'new');
    if (effect) parts.push(el('p', 'plb-dt-fx', effect));

    const list = (state.catalog || state.allCatalog ? catalogSections(code) : null);
    const group = el('fieldset', 'plb-dt-group');
    group.append(el('legend', 'plb-mic', T.detailSections));
    group.append(modeRow('any', T.anySectionOpt, T.anySectionHint, !only), modeRow('only', T.onlyOpt, null, only));
    // While the catalogue is on its way (it reloads when the registered courses become rows) the
    // list is loading, never "no sections".
    // A list from before the reload is not shown either: it can lack the registered sections.
    const loading = state.catState === 'loading';
    if (loading) group.append(el('p', 'plb-dt-hint', T.loadingSections));
    else if (list === null) group.append(el('p', 'plb-dt-hint', T.noCatalog));
    else if (!list.length) group.append(el('p', 'plb-dt-hint', T.noSections));
    else {
      for (const section of sortedSections(code)) {
        group.append(sectionRow(sectionModel(section), 'pick', tickedIds.has(String(section.term_section_id)), atMax));
      }
    }
    // A ticked section that is not in the list any more is flagged, never silently dropped (once
    // the list is in: while it loads, nothing is known to be gone).
    const listed = new Set((list || []).map((section) => String(section.term_section_id)));
    for (const pin of loading ? [] : pins) {
      const id = String(pin.term_section_id);
      if (listed.has(id)) continue;
      const gone = el('label', 'plb-dt-sec');
      gone.dataset.unusable = 'true';
      gone.dataset.tsid = id;
      const input = el('input', 'plb-dt-radio');
      input.type = 'checkbox';
      input.name = 'plbOnly';
      input.value = id;
      input.checked = true;
      input.dataset.fk = `sec-${id}`;
      gone.append(input, el('span', 'plb-dt-sec-body',
        el('span', 'plb-dt-sec-main', bdi(`§${pinLabel(pin)}`)), el('span', 'plb-dt-why', T.tickGone)));
      group.append(gone);
    }
    if (info.inPlan && only) {
      // What "Only these" means right now is said with the radio, not only printed under the list.
      const note = el('p', 'plb-dt-hint plb-dt-note', pins.length ? T.onlyPicks : T.onlyNone);
      note.id = 'plbOnlyNote';
      group.append(note);
      const said = [note.id];
      if (atMax) {
        const most = el('p', 'plb-dt-hint plb-dt-note', T.sixMax);
        most.id = 'plbOnlyMax';
        group.append(most);
        said.push(most.id);
      }
      group.querySelector("input[name='plbMode'][value='only']").setAttribute('aria-describedby', said.join(' '));
    }
    parts.push(group);

    aside.replaceChildren(...parts);
    aside.hidden = false;
    placePop();
    markPlanRows();
    markOpenBlocks();
    syncAlt();
    if (focusKey) {
      const nodes = [...aside.querySelectorAll('[data-fk]')];
      let target = nodes.find((n) => n.dataset.fk === focusKey && !n.disabled);
      // A box that became disabled (or went) hands the keyboard to the mode radio.
      if (!target && focusKey.startsWith('sec-')) target = nodes.find((n) => n.dataset.fk === (only ? 'only' : 'any'));
      (target || title).focus();
    }
  }
  // `fromBlock` is the week block that opened the panel, when one did: closing returns there.
  function openDetail(code, fromBlock) {
    const key = norm(code);
    // The section control of a registered row in Keep: the registered courses become editable
    // rows first, then its menu opens (Escape leaves them editable; Back to Keep is one click away).
    const converted = isKeptRow(key) && convertRegistered();
    const item = state.shortlist.find((c) => norm(c.course_code) === key);
    if (!item) return;
    state.detail = {
      code: key,
      name: String(item.course_name || ''),
      credits: Number(item.credits) || 0,
      weekAt: fromBlock ? weekBlocksOf(fromBlock.dataset.code).indexOf(fromBlock) : -1,
      weekCode: fromBlock ? fromBlock.dataset.code : '',
    };
    // From the week the planned row is brought into view first: the popover hangs under its button.
    const row = document.querySelector(`#plbPlanList .plb-prow[data-code="${CSS.escape(key)}"]`);
    if (fromBlock && row) row.scrollIntoView({ block: 'nearest' });
    if (converted) {
      renderCourses();
      renderActions();
      setStatus('ok', ...T.convSection(shownCode(key)));
    } else renderDetail();
    $('plbDetailTitle').focus({ preventScroll: true });
  }
  // Closing redraws the popover and the open marks only: a click that closes it by landing elsewhere
  // must find the node it hit still in place.
  function closeDetail(returnFocus) {
    const was = state.detail;
    state.detail = null;
    renderDetail();
    if (!returnFocus || !was) return;
    // Back to where it was opened from: the week block, else the row's button.
    const block = was.weekAt >= 0 ? weekBlocksOf(was.weekCode)[was.weekAt] : null;
    const button = block || document.querySelector(`#plbPlanList [data-act="detail"][data-code="${CSS.escape(was.code)}"]`);
    if (button) button.focus();
  }
  function markPlanRows() {
    const open = state.detail ? state.detail.code : null;
    for (const row of document.querySelectorAll('#plbPlanList .plb-prow[data-code]')) {
      const on = open !== null && row.dataset.code === open;
      if (on) row.dataset.open = 'true'; else delete row.dataset.open;
      const button = row.querySelector('.plb-secbtn');
      if (button) button.setAttribute('aria-expanded', String(on));
    }
  }
  // The popover hangs under its course's section button, inside the plan's panel, and stays inside it.
  // The popover hangs under its course's section button, or above it when the room below (inside
  // the courses zone, above its Build row) is too small; failing both it takes the larger side and
  // scrolls inside. It stays inside the plan's panel sideways.
  function placePop() {
    const pop = $('plbPop');
    const plan = $('plbPlan');
    const row = state.detail ? document.querySelector(`#plbPlanList .plb-prow[data-code="${CSS.escape(state.detail.code)}"]`) : null;
    const button = row ? row.querySelector('.plb-secbtn') : null;
    if (!button) { pop.style.top = '0px'; return; }
    if (pop.previousElementSibling !== row) row.after(pop);
    // A phone shows it as a full-width sheet in the flow, directly under its row.
    if (PHONE.matches) { pop.removeAttribute('style'); return; }
    const pr = plan.getBoundingClientRect();
    const br = button.getBoundingClientRect();
    const zone = $('plbZoneCourses').getBoundingClientRect();
    const build = document.querySelector('#plbZoneCourses .plb-buildrow');
    const floor = build ? build.getBoundingClientRect().top - 8 : zone.bottom;
    const gap = 4;
    const width = Math.min(380, pr.width);
    const rtl = document.documentElement.dir === 'rtl';
    const left = rtl ? br.left - pr.left : br.right - pr.left - width;
    pop.style.width = `${width}px`;
    pop.style.left = `${Math.max(0, Math.min(left, pr.width - width))}px`;
    pop.style.maxHeight = 'none';
    const natural = pop.scrollHeight;
    const below = floor - br.bottom - gap;
    const above = br.top - zone.top - gap;
    let top;
    if (natural <= below) top = br.bottom - pr.top + gap;
    else if (natural <= above) top = br.top - pr.top - gap - natural;
    else if (below >= above) {
      pop.style.maxHeight = `${Math.max(120, below)}px`;
      top = br.bottom - pr.top + gap;
    } else {
      const h = Math.max(120, above);
      pop.style.maxHeight = `${h}px`;
      top = br.top - pr.top - gap - Math.min(natural, h);
    }
    pop.style.top = `${top}px`;
  }
  // Write the allowed sections of a course: the shape the build endpoint takes (`pinned_sections`,
  // one to six {term_section_id, section}, in the list's section order). None: the key goes.
  function writeOnly(item, entries) {
    if (entries.length) item.pinned_sections = entries;
    else delete item.pinned_sections;
    planChanged();
    setStatus('ok', ...T.onlyStatus(item.course_code, entries.map(pinLabel)));
    renderCourses();
  }
  function toggleSection(code, id, on) {
    const item = state.shortlist.find((c) => norm(c.course_code) === norm(code));
    if (!item) return;
    const have = new Map(pinsOf(item).map((p) => [String(p.term_section_id), p]));
    if (on) {
      const section = sortedSections(norm(code)).find((x) => String(x.term_section_id) === id);
      // A seventh can never reach the request; a section the list does not have cannot be ticked.
      if (!section || (!have.has(id) && have.size >= MAX_ONLY) || (!have.has(id) && sectionModel(section).reasons.length)) {
        renderCourses();
        return;
      }
      have.set(id, { term_section_id: Number(section.term_section_id), section: String(section.section || '').trim() });
      state.detail.only = true;
    } else have.delete(id);
    const entries = [];
    for (const section of sortedSections(norm(code))) {
      const key = String(section.term_section_id);
      if (have.has(key)) { entries.push(have.get(key)); have.delete(key); }
    }
    entries.push(...have.values());           // ticked, but no longer in the list
    writeOnly(item, entries);
  }
  function chooseAny(code) {
    const item = state.shortlist.find((c) => norm(c.course_code) === norm(code));
    if (!item) return;
    state.detail.only = false;
    if (pinsOf(item).length) writeOnly(item, []);
    else renderCourses();
  }

  /* ── Wiring ────────────────────────────────────────────────────────────── */
  function wire() {
    $('plbLoad').addEventListener('submit', (event) => { event.preventDefault(); loadStudent(); });
    $('plbTermBtn').addEventListener('click', () => setTermPop($('plbTermPop').hidden, false));
    $('plbYear').addEventListener('input', renderTermBtn);
    $('plbTerm').addEventListener('change', renderTermBtn);
    $('plbTermPop').addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { event.preventDefault(); setTermPop(false, true); }
    });
    document.addEventListener('pointerdown', (event) => {
      if (!$('plbTermPop').hidden && !event.target.closest('.plb-termwrap')) setTermPop(false, false);
    });
    // A click anywhere during a load means the user has moved on: Fetch does not take the keyboard back.
    document.addEventListener('pointerdown', () => { state.fetchKeyboard = false; }, true);
    for (const [id, key, value] of [
      ['plbRegKeep', 'mode', 'keep'], ['plbRegIgnore', 'mode', 'ignore'],
      ['plbEligStrict', 'eligibility', 'strict'], ['plbEligRelaxed', 'eligibility', 'relaxed'],
      ['plbProgOnly', 'programOnly', true], ['plbProgAny', 'programOnly', false],
    ]) $(id).addEventListener('change', (event) => { if (event.target.checked) changeRuleInPlace(key, value); });
    $('plbFullIn').addEventListener('change', (event) => changeRuleInPlace('allowFull', event.target.checked));
    $('plbRulesReset').addEventListener('click', resetRules);
    $('plbEligGo').addEventListener('click', goEligConfirm);
    $('plbBackKeep').addEventListener('click', () => requestBackToKeep('bar'));
    $('plbConvGo').addEventListener('click', () => backToKeep(true));
    $('plbConvKeep').addEventListener('click', () => closeConvAsk(true));
    $('plbEligKeep').addEventListener('click', () => closeEligConfirm(true));
    $('plbCurTT').addEventListener('toggle', renderCurTT);
    $('plbMaxIn').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); commitMaxInPlace(); }
    });
    // Typed in: a load leaves the field alone until it is committed or left.
    $('plbMaxIn').addEventListener('input', () => { state.maxDirty = true; });
    $('plbMaxIn').addEventListener('blur', () => { commitMaxInPlace(); state.maxDirty = false; });

    // The picker: one delegated listener per kind of event on the whole zone.
    const pick = $('plbPick');
    pick.addEventListener('click', (event) => {
      const target = event.target.closest('[data-act]');
      if (!target || target.tagName !== 'BUTTON') return;
      const code = target.dataset.code;
      const act = target.dataset.act;
      if (act === 'detail') {
        if (state.detail && state.detail.code === code) closeDetail(true);
        else openDetail(code);
      } else if (act === 'dt-close') closeDetail(true);
      else if (act === 'remove') removeCoursesFromPlan([code], 'row');
      else if (act === 'must') toggleRequired(code);
      else if (act === 'open') {
        if (state.pick.opened.has(code)) state.pick.opened.delete(code); else state.pick.opened.add(code);
        renderCourses();
      } else if (act === 'showall') {
        // The button goes away: the keyboard lands on the first row it revealed, not in Find.
        const before = document.querySelectorAll('#plbAvailList .plb-arow').length;
        state.pick.showAll = true;
        renderCourses();
        const next = document.querySelectorAll('#plbAvailList .plb-arow')[before];
        // The tick, or on phones (where the tick is hidden) the row's + button.
        const target = next && [...next.querySelectorAll('[data-act="tick"], [data-act="add"]')]
          .find((n) => !n.disabled && n.getClientRects().length);
        if (target) target.focus();
      }
      else if (act === 'fold') { state.pick.blockedOpen = !state.pick.blockedOpen; renderCourses(); }
      else if (act === 'only') moveOne(code, true);
      else if (act === 'add') moveOne(code, false, 'plus');
    });
    pick.addEventListener('change', (event) => {
      const target = event.target;
      if (!target || !target.dataset) return;
      if (target.dataset.act === 'tick') tickCourse(target.dataset.code, target.checked);
      else if (target.dataset.act === 'sectick') tickSection(target.dataset.code, target.dataset.tsid, target.checked);
      else if (target.dataset.act === 'ptick') planTick(target.dataset.code, target.checked);
      else if (state.detail) {
        const code = state.detail.code;
        if (target.name === 'plbMode') {
          if (target.value === 'any') chooseAny(code);
          else { state.detail.only = true; renderCourses(); }
        } else if (target.name === 'plbOnly') toggleSection(code, String(target.value), target.checked);
      }
    });
    // Double-click on a row moves it (the same as ticking it and pressing the arrow).
    pick.addEventListener('dblclick', (event) => {
      if (event.target.closest('button, input, label, .plb-secs, .plb-blocked')) return;
      const row = event.target.closest('#plbAvailList .plb-arow:not(.plb-arow--off)');
      if (row) moveOne(row.dataset.code, false);
    });
    for (const button of document.querySelectorAll('[data-xfer]')) {
      button.addEventListener('click', () => {
        const kind = button.dataset.xfer;
        if (kind === 'add') moveTicked();
        else if (kind === 'addall') moveAll();
        else if (kind === 'rem') removeCoursesFromPlan([...state.pick.planTicked], '');
        else if (kind === 'remall') removeCoursesFromPlan(planRowCodes(), 'all');
      });
    }
    window.addEventListener('resize', () => { if (state.detail) placePop(); });
    PHONE.addEventListener('change', () => { if (state.detail) placePop(); });
    const ptabs = $('plbPickTabs');
    ptabs.addEventListener('click', (event) => {
      const tab = event.target.closest('[role="tab"]');
      if (tab) setPickTab(tab.id === 'plbPickTabPlan' ? 'plan' : 'avail', false);
    });
    ptabs.addEventListener('keydown', (event) => {
      const keys = { ArrowLeft: 1, ArrowRight: 1, Home: 'avail', End: 'plan' };
      if (!(event.key in keys)) return;
      event.preventDefault();
      const next = typeof keys[event.key] === 'string' ? keys[event.key] : (state.pickTab === 'avail' ? 'plan' : 'avail');
      setPickTab(next, true);
    });
    $('plbSource').addEventListener('change', (event) => setSource(event.target.value));
    $('plbFind').addEventListener('input', (event) => { state.pick.find = event.target.value; renderCourses(); });
    // The popover closes on a click outside it, once that click has done its work: closing on the
    // press could shift the page (the popover can reach below the plan) before the button is released.
    document.addEventListener('click', (event) => {
      if (state.detail && !event.target.closest('#plbPop, .plb-secbtn, #plbWeek [data-act="week-open"]')) closeDetail(false);
    });
    $('plbWeek').addEventListener('click', (event) => {
      const block = event.target.closest('[data-act="week-open"]');
      if (block) openDetail(block.dataset.code, block);
    });
    $('plbGuide').addEventListener('click', (event) => {
      const view = event.target.closest('[data-view]');
      if (view) selectTab(view.dataset.view, true);
    });
    $('plbWhyBtn').addEventListener('click', () => setWhy(!state.whyOpen, false));
    $('plbGradWhyBtn').addEventListener('click', () => setGradWhy(!state.gradWhy, false));
    $('plbGradRetry').addEventListener('click', () => {
      const build = state.build;
      if (build && build.token && build.projection && ['unavailable', 'busy'].includes(build.projection.state)) loadProjection(build);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector('dialog[open]')) return;
      if (state.detail && event.target.closest && event.target.closest('#plbPop, .plb-secbtn')) { event.preventDefault(); closeDetail(true); return; }
      // Only from the disclosure's own corner: Escape in the course search or on a tab is not
      // a request to close this, and must not pull the keyboard away from where it is.
      if (state.whyOpen && event.target.closest && event.target.closest('#plbWhyBtn, #plbWhy')) { event.preventDefault(); setWhy(false, true); }
      // The reasons close on Escape only while their button is on screen: on a tab whose forecast
      // has none (or lists them without a button) there is nothing to close and Escape is left alone.
      if (state.gradWhy && !$('plbGradWhyBtn').hidden && event.target.closest && event.target.closest('#plbGrad')) {
        event.preventDefault();
        setGradWhy(false, true);
      }
    });
    // The runway is fitted again when the band's width changes (the window, a column appearing)
    // and when the web fonts arrive after the first draw: both change what has room.
    if (typeof ResizeObserver === 'function') {
      let width = 0;
      new ResizeObserver((entries) => {
        const now = Math.round(entries[0].contentRect.width);
        if (now === width) return;
        width = now;
        requestAnimationFrame(() => { fitRunway(); holdBandHeight(true); });
      }).observe($('plbGrad'));
    }
    if (document.fonts && document.fonts.ready) {
      const refit = () => { fitRunway(); holdBandHeight(true); };
      document.fonts.ready.then(refit, () => {});
      if (document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', refit);
    }
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') checkAge(); });
    setInterval(checkAge, 60 * 1000);

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
  }

  function init() {
    const root = $('plbRoot');
    if (!root) return;
    const term = root.dataset.defaultTerm;
    if ([...$('plbTerm').options].some((o) => o.value === term)) $('plbTerm').value = term;
    wire();
    renderTermBtn();
    state.pick = newPick();
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
      renderTermBtn();
      loadStudent();
    } else if (document.activeElement === document.body || !document.activeElement) {
      // An empty screen starts where the work starts. Once, here: nothing takes it back later.
      $('plbStudentId').focus();
    }
  }
  init();
})();
