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
    registeredCredits: IS_AR ? 'مسجّل هذا الفصل' : 'Registered this term',
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
    anySection: IS_AR ? 'أي شعبة' : 'Any section',
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
    resetPlan: IS_AR ? 'العودة إلى المقترح' : 'Reset to suggestions',
    resetDone: (n) => (IS_AR
      ? `أُعيدت المقررات إلى المقترح (${n}).`
      : `Courses reset to the ${n} suggested.`),
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
    readOnlyHint: IS_AR ? 'هذا المقرر ليس في الخطة بعد. أضفه لاختيار شُعبه.' : 'This course is not in the plan yet. Add it to choose its sections.',
    removeFromPlan: IS_AR ? 'إزالة من الخطة' : 'Remove from plan',
    addToPlan: IS_AR ? 'إضافة إلى الخطة' : 'Add to plan',
    // "Only these": several allowed sections for a course.
    onlyOpt: IS_AR ? 'هذه فقط' : 'Only these',
    onlyNone: IS_AR
      ? 'حدِّد الشُعب التي يجوز للبناء استخدامها. ما لم تُحدَّد شعبة، تُستخدم أي شعبة.'
      : 'Tick the sections the build may use. Until one is ticked, any section may be used.',
    onlyPicks: IS_AR ? 'يختار البناء إحدى الشُعب المحدَّدة.' : 'The build picks one of the ticked sections.',
    sixMax: IS_AR ? 'ست شُعب على الأكثر.' : 'Six sections at most.',
    // The table's line: "Only §3", "Only §3 or §4", "Only 3 sections".
    onlyLine: (labels) => {
      const tag = (l) => bdi(`§${l}`);
      if (labels.length === 1) return [IS_AR ? 'فقط ' : 'Only ', tag(labels[0])];
      if (labels.length === 2) return [IS_AR ? 'فقط ' : 'Only ', tag(labels[0]), IS_AR ? ' أو ' : ' or ', tag(labels[1])];
      return [`${IS_AR ? 'فقط ' : 'Only '}${T.sectionWord(labels.length)}`];
    },
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
    whyButton: IS_AR ? 'لماذا هذه الخيارات' : 'Why these options',
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
    // What one course does to the forecast: "If postponed: 5 terms" / "Still 4 terms" / "3 terms, 1 sooner".
    // Arabic puts "no change" after the count: a verb would have to agree with one, two, few and many terms.
    ifPostponed: (n, still) => (IS_AR
      ? `إن أُجِّل: ${T.gradTermCount(n)}${still ? `${T.gradSep}بلا تغيير` : ''}`
      : `If postponed: ${still ? 'still ' : ''}${T.gradTermCount(n)}`),
    ifAdded: (n, delta) => {
      if (delta === 0) return IS_AR ? `${T.gradTermCount(n)}${T.gradSep}بلا تغيير` : `Still ${T.gradTermCount(n)}`;
      if (IS_AR) return `${T.gradTermCount(n)}${T.gradSep}${T.gradTiming(delta)}`;
      return `${T.gradTermCount(n)}${T.gradSep}${Math.abs(delta)} ${delta < 0 ? 'sooner' : 'later'}`;
    },
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
  };
  const NUMBER_EN = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven'];

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
    suggested: [],     // the courses the page suggested when the student loaded ("Reset to suggestions")
    rules: { ...DEFAULT_RULES },
    shownElig: null,   // the eligibility the suggestions on screen were loaded with
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
    moreOpen: false,   // the "More options" disclosure
    detail: null,      // {code, name, credits, only}: the course whose detail panel is open
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
    if (!state.catalog || tsid === null || tsid === undefined) return null;
    for (const list of state.catalog.values()) {
      for (const section of list) if (String(section.term_section_id) === String(tsid)) return section;
    }
    return null;
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
  // The cap's hint ("advisory 18 · regulatory 19"), as the dialog shows it.
  function maxHintText() {
    const cap = state.student ? finitePositive(state.student.credit_cap) : null;
    const reg = state.student ? finitePositive(state.student.regulatory_max_credits) : null;
    return T.maxHint(cap, reg);
  }
  function renderRules() {
    const r = state.rules;
    // The first line is a hidden full summary kept for the record; the controls say the same.
    $('plbRules1').textContent = [T.ruleMode[r.mode], T.ruleElig[r.eligibility], T.ruleMax(r.maxCredits)].join(' · ');
    $('plbRules2').textContent = [T.ruleProg[r.programOnly], T.ruleFull[r.allowFull], T.ruleSwap[r.swap]].join(' · ');
    // The controls on the board show the rules in force.
    $('plbRegKeep').checked = r.mode === 'keep';
    $('plbRegIgnore').checked = r.mode === 'ignore';
    $('plbEligStrict').checked = r.eligibility === 'strict';
    $('plbEligRelaxed').checked = r.eligibility === 'relaxed';
    const field = $('plbMaxIn');
    if (document.activeElement !== field) {
      field.value = r.maxCredits === null ? '' : String(r.maxCredits);
      field.removeAttribute('aria-invalid');
      $('plbMaxInErr').hidden = true;
      $('plbMaxInErr').textContent = '';
    }
    $('plbMaxInHint').textContent = maxHintText();
    const changed = changedRuleKeys().length;
    const badge = $('plbRulesChanged');
    badge.hidden = changed === 0;
    badge.textContent = changed ? T.rulesChanged(changed) : '';
  }
  const regulatoryMax = () => (state.student && finitePositive(state.student.regulatory_max_credits)) || 99;
  // Max credits as typed: '' is "not set" (null); a whole number from 1 to the regulatory maximum;
  // anything else is refused. One reading for the dialog and for the field on the board.
  function parseMaxCredits(raw) {
    const text = raw.trim();
    if (text === '') return { ok: true, value: null };
    const ceiling = regulatoryMax();
    const value = /^\d{1,2}$/.test(text) ? Number(text) : NaN;
    if (!Number.isInteger(value) || value < 1 || value > ceiling) return { ok: false, ceiling };
    return { ok: true, value };
  }

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
  // `preset` ({eligibility}) opens the dialog with that change already chosen, for Apply rules to confirm.
  function openRulesDialog(preset) {
    $('plbRuleMaxHint').textContent = maxHintText();
    fillRulesDialog({ ...state.rules, ...(preset || {}) });
    const note = $('plbRuleEligNote');
    if (preset && preset.eligibility) note.dataset.flag = 'true'; else delete note.dataset.flag;
    const dialog = $('plbRulesDialog');
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    (preset && preset.eligibility ? $('plbRuleElig') : $('plbRuleMode')).focus();
  }
  function closeRulesDialog() {
    const dialog = $('plbRulesDialog');
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  }
  function applyRulesDialog() {
    const max = parseMaxCredits($('plbRuleMax').value);
    if (!max.ok) {
      const err = $('plbRuleMaxErr');
      err.textContent = T.maxError(max.ceiling);
      err.hidden = false;
      $('plbRuleMax').setAttribute('aria-invalid', 'true');
      $('plbRuleMax').focus();
      return;
    }
    $('plbRuleMax').removeAttribute('aria-invalid');
    closeRulesDialog();
    commitRules({
      mode: $('plbRuleMode').value === 'ignore' ? 'ignore' : 'keep',
      eligibility: $('plbRuleElig').value === 'relaxed' ? 'relaxed' : 'strict',
      maxCredits: max.value,
      programOnly: $('plbRuleProg').checked,
      allowFull: $('plbRuleFull').checked,
      swap: $('plbRuleSwap').checked,
    });
  }
  // What a change of rules does, for the dialog and for the controls on the board alike.
  function commitRules(next) {
    const before = { ...state.rules };
    state.rules = next;
    renderRules();
    // Options built under other rules are not options for these: they stay, marked out of date.
    if (Object.keys(DEFAULT_RULES).some((key) => state.rules[key] !== before[key])) planChanged();
    // The other rules apply at once, whatever happens to the reload below.
    renderCourses();
    if (state.ctx && (state.rules.programOnly !== before.programOnly || state.rules.mode !== before.mode)) {
      loadCatalog(state.gen);
    }
    // The suggestions depend on eligibility: reload them, as the current page does. If no reload
    // lands, loadStudent puts back the eligibility the page was loaded with.
    if (state.ctx && state.rules.eligibility !== before.eligibility) loadStudent({ reuse: true });
  }
  // A switch on the board.
  function changeRuleInPlace(key, value) {
    if (state.rules[key] === value) return;
    // The reload would replace a course list the adviser has changed: the dialog says so and asks.
    // The switch goes back to the rule in force; Apply rules makes the change, Cancel leaves it.
    if (key === 'eligibility' && state.ctx && !isSuggested()) {
      renderRules();
      openRulesDialog({ eligibility: value });
      return;
    }
    commitRules({ ...state.rules, [key]: value });
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
    // This term's registered credits, counted from the timetable on screen. The reply's
    // `registered_credits` is the registrar's running total (144 for a senior): beside an
    // 18-credit cap it reads as a mistake, and it is not what this screen is about.
    const registered = registeredCredits();
    const cap = finitePositive(st.credit_cap);
    const reg = finitePositive(st.regulatory_max_credits);
    const advisor = st.advisor_id === null || st.advisor_id === undefined ? '' : String(st.advisor_id).trim();
    host.replaceChildren(
      el('div', 'plb-who',
        el('div', 'plb-sn', st.name ? String(st.name) : T.unnamed),
        el('div', 'plb-smeta', `${T.idLabel} `, bdi(st.student_id), st.program ? ' · ' : null, st.program ? bdi(st.program) : null,
          advisor ? ` · ${T.advisor} ` : null, advisor ? bdi(advisor) : null)),
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
        ...(model.longestGap > 0 ? [`${T.gap} `, nb(durationText(model.longestGap))] : [T.noGap]),
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
  function weekBlock(m, rowStart, rowEnd, col) {
    const rows = rowEnd - rowStart;
    const range = `${hhmm(m.st)}-${hhmm(m.en)}`;
    const item = plannedItem(m);
    const block = el(item ? 'button' : 'div', 'plb-b');
    if (item) {
      block.type = 'button';
      block.dataset.act = 'week-open';
      block.setAttribute('aria-controls', 'plbDetail');
    }
    block.style.gridColumn = String(col);
    block.style.gridRow = `${rowStart} / ${rowEnd}`;
    block.dataset.code = m.key;
    block.dataset.kind = m.kind || 'reg';
    if (m.clash) block.dataset.clash = 'true';
    const label = `${m.code}${m.section ? ` ${IS_AR ? 'شعبة' : 'section'} ${m.section}` : ''}, ${DAY_SHORT[m.day]} ${range}`
      + `${m.kind === 'new' ? `, ${T.plannedBlock}` : ''}${m.clash ? `, ${T.clashBlock}` : ''}`;
    if (item) {
      const said = `${m.code}${item.course_name ? ` ${item.course_name}` : ''}, ${T.detailsWord}`;
      block.setAttribute('aria-label', `${said}. ${label}`);
      block.setAttribute('aria-expanded', String(!!state.detail && state.detail.code === norm(m.key)));
      if (state.detail && state.detail.code === norm(m.key)) block.dataset.open = 'true';
    } else {
      block.setAttribute('role', 'img');
      block.setAttribute('aria-label', label);
    }
    block.append(...present(
      el('span', 'plb-cd', bdi(m.code)),
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
    block.dataset.alt = 'true';
    block.dataset.code = m.key;
    block.setAttribute('aria-hidden', 'true');
    return block;
  }
  function renderLegend(opt, alt) {
    const showRegistered = !opt || state.build.rules.mode === 'keep';
    const parts = [];
    if (showRegistered) parts.push(el('span', 'plb-lg-i', el('i', 'plb-k'), T.legendRegistered));
    if (opt) parts.push(...(parts.length ? [' '] : []), el('span', 'plb-lg-i', el('i', 'plb-k plb-k--new'), T.planned));
    if (alt) parts.push(...(parts.length ? [' '] : []), el('span', 'plb-lg-i', el('i', 'plb-k plb-k--alt'), T.legendAlt));
    $('plbLegend').replaceChildren(...parts);
    for (const mark of $('plbLegend').querySelectorAll('i')) mark.setAttribute('aria-hidden', 'true');
  }
  function renderWeek() {
    const host = $('plbWeek');
    const regModel = weekModel(registeredMeetings());
    renderTab(regModel);
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
    const model = opt ? weekModel(weekMeetings(opt)) : regModel;
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
    markOpenBlocks();
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
    return added + (state.build.rules.mode === 'keep' ? registeredCredits() || 0 : 0);
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
      flags.length ? el('span', 'plb-tab-x plb-tab-warn', flags.join(' · ')) : null);
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
    if (!state.catalog) return [T.unknown];
    const sections = state.catalog.get(norm(code)) || [];
    if (!sections.length) return [T.noSections];
    const caps = sections.map(sectionCapacity);
    if (caps.every((c) => c.known && c.full)) return [T.allFull];
    if (sections.length === 1) {
      const left = fewSeats(caps[0]) ? caps[0].remaining : null;
      const when = whenNodes(sections[0].meetings);
      if (!when.length && left === null) return [T.sectionWord(1)];
      return [...when, ...(when.length && left !== null ? [' · '] : []), ...(left !== null ? [T.seatsLeft(left)] : [])];
    }
    return [T.sectionWord(sections.length)];
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
  // The section line of a course row opens that course's detail panel.
  function sectionButton(code, parts, miss) {
    const open = !!state.detail && state.detail.code === norm(code);
    const button = el('button', `plb-sc plb-sc-btn${miss ? ' plb-sc--miss' : ''}`,
      el('span', 'plb-sc-t', ...parts), svg(ICONS.chev, 12));
    button.type = 'button';
    button.dataset.act = 'detail';
    button.dataset.code = norm(code);
    button.setAttribute('aria-expanded', String(open));
    button.setAttribute('aria-controls', 'plbDetail');
    return button;
  }
  // `lead` (a node) closes the title cell, right before the end cell: "Reset to suggestions" before "Must-take".
  function groupBody(name, left, right, lead) {
    const body = el('tbody', 'plb-group');
    body.dataset.group = name;
    const title = lead ? el('th', 'plb-mic', el('span', 'plb-grp-in', el('span', 'plb-grp-l', left), lead))
      : el('th', 'plb-mic', left);
    body.append(el('tr', 'plb-grp', title, el('td', 'plb-mic plb-grp-end', right || '')));
    body.firstChild.firstChild.colSpan = 2;
    body.firstChild.lastChild.colSpan = 2;
    body.firstChild.firstChild.scope = 'rowgroup';
    return body;
  }
  function emptyRow(text) {
    const row = el('tr', 'plb-tr plb-tr--note', el('td', 'plb-empty-cell', text));
    row.firstChild.colSpan = 4;
    return row;
  }
  // A course row is: code | name over its section line | must-take | action. The name and the
  // section share one cell so the name has the width of the pane: real course titles are long
  // Arabic phrases, and in a column of their own they wrapped onto four or five lines.
  function courseRow(code, name, section, cells, off, effect) {
    const row = el('tr', 'plb-tr');
    row.dataset.code = norm(code);
    const title = el('span', off ? 'plb-name plb-name--off' : 'plb-name', name ? isolate(name) : '');
    // The effect of the course on graduation shares the section line, and wraps under it when short of room.
    const line = effect ? el('span', 'plb-sl', section, effect) : section;
    row.append(el('td', 'plb-code', bdi(code)), el('td', 'plb-main', title, line), ...cells);
    return row;
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
  function renderCourses() {
    const table = $('plbTable');
    // The reset link lives in the "New in this plan" header; it is parked while the rows are rebuilt.
    $('plbResetHome').append($('plbReset'));
    table.querySelectorAll('tbody').forEach((n) => n.remove());
    const addInput = $('plbAdd');
    addInput.disabled = !state.ctx;
    if (!state.ctx) {
      const body = el('tbody', 'plb-group');
      body.append(emptyRow(T.loadFirstCourses));
      table.append(body);
      renderCreditsHeader();
      state.focus = null;
      state.detail = null;
      renderDetail();
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
      regBody.append(courseRow(course.code, course.name,
        el('span', 'plb-sc', course.section ? bdi(`§${course.section}`) : T.unknown), [el('td'), lock]));
    }
    table.append(regBody);

    // New in this plan
    const newBody = groupBody('new', `${T.newInPlan} · ${T.credits(planCredits())}`, T.mustTake, $('plbReset'));
    if (!state.shortlist.length) newBody.append(emptyRow(T.nothingAdded));
    for (const item of state.shortlist) {
      const code = item.course_code;
      const pins = pinsOf(item);
      const parts = [];
      let miss = false;
      const chosen = opt ? mappingFor(opt, code) : null;
      if (opt) {
        // On an option's tab the cell says what that option did with the course.
        if (chosen) {
          parts.push(bdi(`§${String(chosen.section || '').trim() || '?'}`));
          const section = findSection(chosen.term_section_id);
          const cap = section ? sectionCapacity(section) : null;
          if (cap && fewSeats(cap)) parts.push(' · ', el('span', 'plb-few', T.seatsLeft(cap.remaining)));
          // The other sections the adviser allowed, when the one this option used is among them.
          const used = String(chosen.term_section_id);
          if (pins.length > 1 && pins.some((p) => String(p.term_section_id) === used)) {
            const others = pins.filter((p) => String(p.term_section_id) !== used).map(pinLabel);
            parts.push(el('span', 'plb-or', ...T.orOthers(others)));
          }
        } else if (!state.build.shortlist.some((c) => norm(c.course_code) === norm(code))) {
          parts.push(T.notInBuild);       // added after this build was made
        } else { miss = true; parts.push(T.notScheduledHere); }
      } else if (pins.length) parts.push(...T.onlyLine(pins.map(pinLabel)));
      else parts.push(T.anySection);
      const sectionCell = sectionButton(code, parts, miss);
      const box = el('input', 'plb-tick');
      box.type = 'checkbox';
      box.checked = !!item.must_take;
      box.setAttribute('aria-label', T.mustTakeFor(code));
      box.dataset.act = 'must';
      box.dataset.code = norm(code);
      const remove = iconButton('x', T.removeCourse(code), 'remove', code);
      const row = courseRow(code, item.course_name, sectionCell,
        [el('td', 'plb-act', el('label', 'plb-tickwrap', box)), el('td', 'plb-act', remove)], false,
        opt ? effectNode(code, 'new') : null);
      if (state.detail && state.detail.code === norm(code)) row.dataset.open = 'true';
      newBody.append(row);
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
        const row = courseRow(course.code, course.name,
          sectionButton(course.code, sectionInfo(course.code)), [el('td'), el('td', 'plb-act', add)], false,
          opt ? effectNode(course.code, 'could') : null);
        if (state.detail && state.detail.code === course.code) row.dataset.open = 'true';
        body.append(row);
      }
      table.append(body);
    }

    // Blocked
    const blocked = blockedRecs();
    if (blocked.length) {
      const body = groupBody('blocked', T.blocked, '');
      for (const rec of blocked) {
        body.append(courseRow(rec.course_code, rec.course_name, blockedReason(rec), [el('td'), el('td')], true));
      }
      table.append(body);
    }
    renderCreditsHeader();
    renderDetail();
    restoreFocus();
  }
  // The list is the suggested one (every field the build reads, in the same order): nothing to reset.
  const isSuggested = () => JSON.stringify(state.shortlist) === JSON.stringify(state.suggested);
  function renderReset() {
    $('plbReset').disabled = !state.ctx || isSuggested();
  }
  function resetToSuggestions() {
    if (!state.ctx || isSuggested()) return;
    state.shortlist = JSON.parse(JSON.stringify(state.suggested));
    if (state.detail) delete state.detail.only;
    planChanged();
    setStatus('ok', T.resetDone(state.shortlist.length));
    renderCourses();
    if (state.build) renderWeek();
    if ($('plbReset').disabled && !$('plbAdd').disabled) $('plbAdd').focus();
  }
  function renderCreditsHeader() {
    renderReset();
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
  function addCourseToPlan(item, keepFocus) {
    if (inShortlist(item.course_code)) return;
    state.shortlist.push(item);
    planChanged();
    // From the detail panel focus stays in the panel (it lands on its heading), not on the row.
    state.focus = keepFocus ? null : { kind: 'remove', code: item.course_code };
    setStatus('ok', ...T.added(item.course_code));
    renderCourses();
  }
  function removeCourseFromPlan(code, keepFocus) {
    const at = state.shortlist.findIndex((c) => norm(c.course_code) === norm(code));
    if (at < 0) return;
    const [gone] = state.shortlist.splice(at, 1);
    // "Only these" with nothing ticked belonged to the course that has just gone, not to the panel.
    if (state.detail && state.detail.code === norm(code)) delete state.detail.only;
    planChanged();
    state.focus = keepFocus ? null : { kind: 'add', code: gone.course_code };
    setStatus('ok', ...T.removed(gone.course_code));
    if (state.build) renderWeek();
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
    if (state.build) keepingFocus(() => { renderStrip(); renderGuide(); });
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
    const studentId = reuse ? state.ctx.studentId : $('plbStudentId').value.trim();
    const year = reuse ? state.ctx.year : $('plbYear').value.trim();
    const term = reuse ? state.ctx.term : $('plbTerm').value;
    if (!/^\d+$/.test(studentId)) { setStatus('error', T.enterStudent); return; }
    if (!/^\d{4}$/.test(year)) { setStatus('error', T.enterYear); return; }

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
    // Only an accepted student retires the plan and section requests of the one on screen. A
    // fetch that failed above leaves them alive, or their groups would say "loading" for good.
    const run = ++state.gen;
    const previousId = state.student ? String(state.student.student_id) : null;
    // Options belong to the student they were built for. The same student reloaded (a rule that
    // reloads the suggestions) keeps them, marked out of date; another student removes them.
    if (reuse && previousId === String(data.student.student_id)) planChanged();
    else { dropBuild(true); state.detail = null; }

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
    state.suggested = JSON.parse(JSON.stringify(state.shortlist));
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
      again = document.querySelector(`#plbTable [data-act="${CSS.escape(act)}"][data-code="${CSS.escape(code)}"]`);
    }
    if (!again && more) again = document.querySelector(`#plbMoreList [data-more="${CSS.escape(more)}"]`);
    if (!again && inWeek >= 0) again = weekBlocksOf(code)[inWeek] || weekBlocksOf(code)[0] || null;
    if (again && !again.disabled) again.focus();
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
    const count = effect.terms_including_planning_term;
    const delta = effect.delta_terms;
    if (group === 'new') {
      return delta > 0
        ? { text: T.ifPostponed(count, false), tone: 'strong', delta }
        : { text: T.ifPostponed(count, delta === 0), tone: 'muted', delta };
    }
    return { text: T.ifAdded(count, delta), tone: delta < 0 ? 'brand' : 'muted', delta };
  }
  function effectNode(code, group) {
    const effect = effectFor(code, group);
    if (!effect) return null;
    const node = el('span', `plb-fxt plb-fxt--${effect.tone}`, effect.text);
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
    const land = el('div', 'plb-land',
      result ? el('span', 'plb-won', result.text) : null,
      el('b', '', ...completionParts(row)));
    if (result) land.firstElementChild.dataset.result = result.kind;
    land.style.setProperty('--plb-from', String(list.length + 2));
    line.append(
      el('div', 'plb-rl',
        el('span', 'plb-num', String(count)),
        el('span', 'plb-st', el('b', '', T.gradTermWord(count)), el('span', 'plb-mic', ...label()))),
      el('i', 'plb-brk'),
      ...tickNodes(list),
      land,
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
    const won = runway.querySelector('.plb-rwl--opt .plb-won[data-result="sooner"]');
    const isRow = () => getComputedStyle(runway).display === 'grid';
    // Start from the stylesheet's own choice, with every label, the unit, and the result at
    // reading size.
    delete runway.dataset.fit;
    delete runway.dataset.unit;
    if (won) delete won.dataset.size;
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
    // The result is the page's largest teal text where its line has room for it on one line.
    // Its size cannot undo what was fitted above: only a "sooner" result is enlarged, and that
    // belongs to the shorter line, whose landing spans a free column and so sizes no column.
    if (!won) return;
    for (const size of ['xl', 'lg']) {
      won.dataset.size = size;
      const box = won.getBoundingClientRect();
      if (box.height < parseFloat(getComputedStyle(won).lineHeight) * 1.5 && within(box, band)) return;
    }
    delete won.dataset.size;
  }

  function renderGrad() {
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
    fitRunway();

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
  // been (a change of width starts the count again). A phone stacks the columns: nothing is held.
  function holdBandHeight(reset) {
    const band = document.querySelector('.plb-band');
    if (!band) return;
    band.style.minHeight = '';
    const build = state.build;
    const sideBySide = getComputedStyle(band).gridTemplateColumns.split(' ').length > 1;
    if (!build || !sideBySide) return;
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
      const distinct = feasible ? distinctOptions(options) : [];
      state.build = {
        options: distinct,
        shortlist,
        rules,
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
        el('span', '', bdi(key), ...(m.section ? [' ', bdi(`§${String(m.section).trim()}`)] : []), ...(item && item.course_name ? [' ', isolate(item.course_name)] : [])),
        el('span', 'plb-apply-when', ...whenNodes(m.meetings)));
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
    setBuildLine('busy', T.loading);
    const reloaded = state.ctx && state.ctx.studentId === ctx.studentId ? await loadStudent({ reuse: true }) : false;
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
    if (item) return { inPlan: true, item, name: String(item.course_name || ''), credits: Number(item.credits) || 0 };
    const course = planPool().find((c) => c.code === d.code);
    if (course) return { inPlan: false, course, name: course.name, credits: course.credits };
    // While the plan reloads the pool is empty for a moment: the panel waits instead of closing.
    if (state.planState === 'loading') return { inPlan: false, course: null, name: d.name, credits: d.credits };
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
  // What the panel says about one section.
  function sectionModel(section) {
    const cap = sectionCapacity(section);
    const reasons = [];
    if (cap.known && cap.full && !state.rules.allowFull) reasons.push(T.cannotFull);
    const clash = state.rules.mode === 'keep' && sectionClashes(section);
    if (clash) reasons.push(T.cannotClash);
    const taken = finiteNonNegative(section.registered_count);
    const max = finiteNonNegative(section.available_capacity);
    let seats = T.seatsUnknown;
    if (cap.known) seats = cap.full ? T.seatsFull : T.seatsTaken(taken, max);
    const options = state.build
      ? state.build.options.filter((o) => (o.mappings || []).some((m) => String(m.term_section_id) === String(section.term_section_id))).map((o) => o.name)
      : [];
    return {
      id: String(section.term_section_id), label: String(section.section || '').trim(), cap, clash, reasons, seats,
      when: whenNodes(section.meetings), programmes: programmesOf(section), options,
    };
  }
  // The allowed sections of a course: [{term_section_id, section}], at most MAX_ONLY.
  const MAX_ONLY = 6;
  const pinsOf = (item) => (item && Array.isArray(item.pinned_sections) ? item.pinned_sections : []);
  const pinLabel = (pin) => String(pin.section || '').trim() || '?';
  // A course's sections as the panel lists them (and as a request keeps them).
  function sortedSections(code) {
    const list = state.catalog ? state.catalog.get(code) || [] : [];
    return list.slice().sort((a, b) => String(a.section || '').localeCompare(String(b.section || ''), 'en', { numeric: true }));
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
    if (usable && model.cap.known && model.cap.full) flags.push(el('span', 'plb-flag plb-flag--bad', T.seatsFull));
    if (fewSeats(model.cap)) flags.push(el('span', 'plb-flag plb-flag--warn', T.seatsLeft(model.cap.remaining)));
    if (usable && model.clash) flags.push(el('span', 'plb-flag plb-flag--bad', T.clashFlag));
    if (model.options.length) flags.push(el('span', 'plb-flag', ...T.inOptions(model.options)));
    row.dataset.tsid = model.id;
    if (!usable) row.dataset.unusable = 'true';
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
    const aside = $('plbDetail');
    const body = aside.parentElement;
    const inside = aside.contains(document.activeElement);
    const focusKey = inside ? (document.activeElement.dataset.fk || 'title') : null;
    const info = detailInfo();
    if (!info) {
      state.detail = null;
      aside.hidden = true;
      aside.replaceChildren();
      body.classList.add('plb-body--nodetail');
      markOpenBlocks();
      syncAlt();
      // Focus was inside a panel that has gone: put it on the field that adds a course.
      if (inside && !$('plbAdd').disabled) $('plbAdd').focus();
      return;
    }
    const code = state.detail.code;
    const item = info.item || null;
    const pins = pinsOf(item);
    const tickedIds = new Set(pins.map((p) => String(p.term_section_id)));
    const atMax = pins.length >= MAX_ONLY;
    // "Only these" shows once a section is ticked, or once the adviser asked for it in this opening.
    const only = !!item && (pins.length > 0 || state.detail.only === true);
    const title = el('h3', 'plb-dt-title', bdi(code), info.name ? ' ' : null, info.name ? isolate(info.name) : null,
      el('span', 'plb-dt-cr', ` · ${T.credits(info.credits)}`));
    title.id = 'plbDetailTitle';
    title.tabIndex = -1;
    title.dataset.fk = 'title';
    const close = el('button', 'plb-dt-close', T.close);
    close.type = 'button';
    close.dataset.act = 'dt-close';
    close.dataset.fk = 'close';
    const parts = [el('div', 'plb-dt-head', title, close)];
    const effect = effectNode(code, info.inPlan ? 'new' : 'could');
    if (effect) parts.push(el('p', 'plb-dt-fx', effect));

    const list = (state.catalog ? state.catalog.get(code) || [] : null);
    const group = el(info.inPlan ? 'fieldset' : 'div', 'plb-dt-group');
    if (info.inPlan) group.append(el('legend', 'plb-mic', T.detailSections));
    else group.append(el('p', 'plb-mic', T.detailSections));
    if (info.inPlan) {
      group.append(modeRow('any', T.anySectionOpt, T.anySectionHint, !only), modeRow('only', T.onlyOpt, null, only));
    } else {
      group.append(el('p', 'plb-dt-hint', T.readOnlyHint));
    }
    if (list === null) group.append(el('p', 'plb-dt-hint', T.noCatalog));
    else if (!list.length) group.append(el('p', 'plb-dt-hint', T.noSections));
    else {
      for (const section of sortedSections(code)) {
        group.append(sectionRow(sectionModel(section), info.inPlan ? 'pick' : 'read', tickedIds.has(String(section.term_section_id)), atMax));
      }
    }
    // A ticked section that is not in the list any more is flagged, never silently dropped.
    const listed = new Set((list || []).map((section) => String(section.term_section_id)));
    for (const pin of pins) {
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

    const foot = el('div', 'plb-dt-foot');
    if (info.inPlan) {
      const box = el('input', 'plb-tick');
      box.type = 'checkbox';
      box.checked = !!item.must_take;
      box.dataset.act = 'dt-must';
      box.dataset.fk = 'must';
      const remove = el('button', 'btn btn-sm btn-neutral', T.removeFromPlan);
      remove.type = 'button';
      remove.dataset.act = 'dt-remove';
      remove.dataset.fk = 'remove';
      foot.append(el('label', 'plb-dt-must', box, ' ', T.mustTake), remove);
    } else {
      const add = el('button', 'btn btn-sm btn-primary', T.addToPlan);
      add.type = 'button';
      add.dataset.act = 'dt-add';
      add.dataset.fk = 'add';
      add.disabled = !info.course;
      foot.append(add);
    }
    parts.push(foot);
    aside.replaceChildren(...parts);
    aside.hidden = false;
    body.classList.remove('plb-body--nodetail');
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
    const item = state.shortlist.find((c) => norm(c.course_code) === key);
    const course = item ? null : planPool().find((c) => c.code === key);
    if (!item && !course) return;
    state.detail = {
      code: key,
      name: item ? String(item.course_name || '') : course.name,
      credits: item ? Number(item.credits) || 0 : course.credits,
      weekAt: fromBlock ? weekBlocksOf(fromBlock.dataset.code).indexOf(fromBlock) : -1,
      weekCode: fromBlock ? fromBlock.dataset.code : '',
    };
    renderCourses();
    $('plbDetailTitle').focus();
  }
  function closeDetail(returnFocus) {
    const was = state.detail;
    state.detail = null;
    renderCourses();
    if (!returnFocus || !was) return;
    // Back to where it was opened from: the week block, else the row's button.
    const block = was.weekAt >= 0 ? weekBlocksOf(was.weekCode)[was.weekAt] : null;
    const button = block || document.querySelector(`#plbTable [data-act="detail"][data-code="${CSS.escape(was.code)}"]`);
    if (button) button.focus();
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
    $('plbReset').addEventListener('click', resetToSuggestions);
    // A click anywhere during a load means the user has moved on: Fetch does not take the keyboard back.
    document.addEventListener('pointerdown', () => { state.fetchKeyboard = false; }, true);
    $('plbEditRules').addEventListener('click', () => openRulesDialog());
    for (const [id, key, value] of [
      ['plbRegKeep', 'mode', 'keep'], ['plbRegIgnore', 'mode', 'ignore'],
      ['plbEligStrict', 'eligibility', 'strict'], ['plbEligRelaxed', 'eligibility', 'relaxed'],
    ]) $(id).addEventListener('change', (event) => { if (event.target.checked) changeRuleInPlace(key, value); });
    $('plbMaxIn').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); commitMaxInPlace(); }
    });
    $('plbMaxIn').addEventListener('blur', commitMaxInPlace);
    $('plbRulesCancel').addEventListener('click', closeRulesDialog);
    $('plbRulesReset').addEventListener('click', () => fillRulesDialog(state.defaults));
    $('plbRulesForm').addEventListener('submit', (event) => { event.preventDefault(); applyRulesDialog(); });

    $('plbTable').addEventListener('click', (event) => {
      const target = event.target.closest('[data-act]');
      if (!target || target.tagName !== 'BUTTON') return;
      const code = target.dataset.code;
      if (target.dataset.act === 'detail') {
        if (state.detail && state.detail.code === code) closeDetail(true);
        else openDetail(code);
      } else if (target.dataset.act === 'remove') removeCourseFromPlan(code);
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
      renderReset();
      if (state.build || state.inflight) {
        planChanged();
        state.focus = { kind: 'must', code: item.course_code };
        renderCourses();
      }
    });

    // The detail panel: one delegated listener per kind of event.
    const detail = $('plbDetail');
    detail.addEventListener('click', (event) => {
      const target = event.target.closest('[data-act]');
      if (!target || !state.detail) return;
      const code = state.detail.code;
      if (target.dataset.act === 'dt-close') closeDetail(true);
      else if (target.dataset.act === 'dt-remove') removeCourseFromPlan(code, true);
      else if (target.dataset.act === 'dt-add') {
        const course = planPool().find((c) => c.code === code);
        if (course) addCourseToPlan(itemFromPlan(course), true);
      }
    });
    detail.addEventListener('change', (event) => {
      const target = event.target;
      if (!state.detail || !target) return;
      const code = state.detail.code;
      if (target.dataset.act === 'dt-must') {
        const item = state.shortlist.find((c) => norm(c.course_code) === code);
        if (!item) return;
        item.must_take = target.checked;
        planChanged();
        renderCourses();
      } else if (target.name === 'plbMode') {
        if (target.value === 'any') chooseAny(code);
        else { state.detail.only = true; renderCourses(); }
      } else if (target.name === 'plbOnly') {
        toggleSection(code, String(target.value), target.checked);
      }
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
      if (event.target.closest && event.target.closest('#plbDetail')) { event.preventDefault(); closeDetail(true); return; }
      // Only from the disclosure's own corner: Escape in the course search or on a tab is not
      // a request to close this, and must not pull the keyboard away from where it is.
      if (state.whyOpen && event.target.closest && event.target.closest('.plb-acts')) { event.preventDefault(); setWhy(false, true); }
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
    } else if (document.activeElement === document.body || !document.activeElement) {
      // An empty screen starts where the work starts. Once, here: nothing takes it back later.
      $('plbStudentId').focus();
    }
  }
  init();
})();
