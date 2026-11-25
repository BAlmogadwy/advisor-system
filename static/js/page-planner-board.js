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
    pinBroken: IS_AR ? 'مثبّتة، لكن لم يعد استخدامها ممكنًا. ' : 'Pinned, but it can no longer be used. ',
    pinGone: IS_AR ? 'مثبّتة، لكنها ليست في قائمة الشُعب الحالية.' : 'Pinned, but it is not in the current section list.',
    noCatalog: IS_AR ? 'بيانات الشُعب غير متاحة.' : 'Section data is not available.',
    readOnlyHint: IS_AR ? 'هذا المقرر ليس في الخطة بعد. أضفه لتثبيت شعبة.' : 'This course is not in the plan yet. Add it to pin a section.',
    removeFromPlan: IS_AR ? 'إزالة من الخطة' : 'Remove from plan',
    addToPlan: IS_AR ? 'إضافة إلى الخطة' : 'Add to plan',
    pinnedSet: (code, section) => (IS_AR ? ['ثُبّتت الشعبة ', bdi(`${code} §${section}`), '.'] : ['Pinned ', bdi(`${code} §${section}`), '.']),
    pinnedCleared: (code) => (IS_AR ? [bdi(code), ': أي شعبة.'] : [bdi(code), ': any section.']),
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
    // The build loop. `build` is null until a reply is accepted, and again when another student
    // loads or a save lands: {options, shortlist, rules, at, madeAt, stale, ...}. A change to the
    // plan or the rules keeps it and sets `stale` ('changed'; 'old' after 30 minutes).
    build: null,
    selected: null,    // name of the option tab on screen; null = the Registered tab
    buildGen: 0,       // bumped whenever a reply in flight must no longer be believed
    inflight: 0,       // build requests not yet answered: the button waits for all of them
    applying: false,   // a save request is in flight
    moreOpen: false,   // the "More options" disclosure
    detail: null,      // {code, name, credits}: the course whose detail panel is open
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
    block.append(...present(
      el('span', 'plb-cd', bdi(m.code)),
      m.section ? el('span', 'plb-bs', bdi(`§${m.section}`)) : null,
      el('span', 'plb-bt', bdi(range))));
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
      const kept = { ...opt, aliases: [] };
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
  function optionTab(opt, alike) {
    const facts = factsOf(weekModel(weekMeetings(opt)));
    const unplaced = unplacedFor(opt).length;
    const terms = termsNode(forecastFor(opt.name));
    const seats = fewestSeats(opt);
    const flags = [unplaced ? T.notScheduled(unplaced) : null, seats === null ? null : T.seatsLeft(seats)].filter(Boolean);
    const selected = state.selected === opt.name;
    // An option that stands for identical ones carries their names too: "A1 | B2 | C2".
    const also = (opt.aliases || []).map((a) => el('span', 'plb-tab-aka', ' | ', bdi(a.name)));
    const tab = el('button', 'plb-tab',
      el('span', 'plb-tab-n', bdi(opt.name), ...also, ' ', el('span', 'plb-tab-cr', `· ${T.credits(optionCredits(opt))}`)),
      el('span', 'plb-tab-x', ...present(terms, terms ? ' · ' : null, facts.first)),
      el('span', 'plb-tab-x', ...facts.second),
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
      const terms = termsNode(forecastFor(opt.name));
      const item = el('button', 'plb-more-item', bdi(opt.name), ` · ${T.credits(optionCredits(opt))} · `, ...present(terms, terms ? ' · ' : null), facts.first);
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
    shown.forEach((opt, i) => {
      tabs.insertBefore(optionTab(opt, keys.filter((k) => k === keys[i]).length > 1), registered);
    });
    if (state.build && state.build.stale) strip.dataset.stale = state.build.stale;
    else delete strip.dataset.stale;
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
      const left = fewSeats(caps[0]) ? caps[0].remaining : null;
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
  function groupBody(name, left, right) {
    const body = el('tbody', 'plb-group');
    body.dataset.group = name;
    body.append(el('tr', 'plb-grp', el('th', 'plb-mic', left), el('td', 'plb-mic plb-grp-end', right || '')));
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
  function courseRow(code, name, section, cells, off) {
    const row = el('tr', 'plb-tr');
    row.dataset.code = norm(code);
    const title = el('span', off ? 'plb-name plb-name--off' : 'plb-name', name ? isolate(name) : '');
    row.append(el('td', 'plb-code', bdi(code)), el('td', 'plb-main', title, section), ...cells);
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
    const newBody = groupBody('new', `${T.newInPlan} · ${T.credits(planCredits())}`, T.mustTake);
    if (!state.shortlist.length) newBody.append(emptyRow(T.nothingAdded));
    for (const item of state.shortlist) {
      const code = item.course_code;
      const pin = item.pinned_sections && item.pinned_sections.length ? item.pinned_sections[0] : null;
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
        } else if (!state.build.shortlist.some((c) => norm(c.course_code) === norm(code))) {
          parts.push(T.notInBuild);       // added after this build was made
        } else { miss = true; parts.push(T.notScheduledHere); }
      } else if (pin && pin.section) parts.push(`${T.pinned} `, bdi(`§${pin.section}`));
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
        [el('td', 'plb-act', el('label', 'plb-tickwrap', box)), el('td', 'plb-act', remove)]);
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
          sectionButton(course.code, [sectionInfo(course.code)]), [el('td'), el('td', 'plb-act', add)]);
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
    planChanged();
    state.focus = keepFocus ? null : { kind: 'add', code: gone.course_code };
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
  // control is found again by its id, by its action and course in the table, or by the option it
  // stands for in "More options".
  function keepingFocus(redraw) {
    const was = document.activeElement;
    const id = was && was.id ? was.id : '';
    const act = was && was.dataset ? was.dataset.act : '';
    const code = was && was.dataset ? was.dataset.code : '';
    const more = was && was.dataset ? was.dataset.more : '';
    redraw();
    if (document.activeElement && document.activeElement !== document.body) return;
    let again = id ? document.getElementById(id) : null;
    if (!again && act && code) {
      again = document.querySelector(`#plbTable [data-act="${CSS.escape(act)}"][data-code="${CSS.escape(code)}"]`);
    }
    if (!again && more) again = document.querySelector(`#plbMoreList [data-more="${CSS.escape(more)}"]`);
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
    if (build && build.stale) return { ok: false, why: null };       // the status line says why
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
    const stale = !!(state.build && state.build.stale);
    $('plbActs').dataset.stale = stale ? state.build.stale : '';
    if (stale) $('plbReview').setAttribute('aria-describedby', 'plbBuildLine');
    else $('plbReview').removeAttribute('aria-describedby');
    renderWhy();
    renderGrad();
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
    const reasonOf = (u) => T.reason[String(u.reason_code || '')] || String(u.reason || '') || T.noReason;
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
    // Past eight columns every other label is dropped, starting from the first; the ticks keep a title.
    const every = cols > 8 ? 2 : 1;
    for (let i = 0; i < cols; i += 1) {
      const entry = list[i];
      axis.append(el('span', `plb-ax${i === 0 ? ' plb-ax--now' : ''}`, entry && i % every === 0 ? bdi(termLabel(entry)) : null));
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

    // A control that was just hidden took the keyboard with it: keep it in the band.
    if (hadFocus) {
      const here = document.activeElement;
      if (!here || !grad.contains(here) || here.closest('[hidden]')) {
        const next = [retry, whyBtn, line].find((node) => !node.closest('[hidden]')) || grad;
        next.focus();
      }
    }
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
  async function loadProjection(build) {
    const run = (build.projRun += 1);
    build.projection = { state: 'loading' };
    renderGrad();
    let result = null;
    try { result = await postJson('/ops/planner/graduation-projection/', { token: build.token }); } catch (err) { result = null; }
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
    build.projection = next;
    keepingFocus(() => {
      renderStrip();
      renderTab(weekModel(registeredMeetings()));
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
      state.build = {
        options: feasible ? distinctOptions(options) : [],
        shortlist,
        rules,
        madeAt: now.getTime(),
        stale: null,
        at: `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`,
        failures: Array.isArray(summary.hard_constraint_failures) ? summary.hard_constraint_failures : [],
        unscheduled: Array.isArray(data.unscheduled) ? data.unscheduled : [],
        token: typeof data.graduation_projection_token === 'string' ? data.graduation_projection_token : '',
        projection: null,
        projRun: 0,
      };
      state.selected = feasible ? String(options[0].name) : null;
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
    const days = [...new Set((section.meetings || []).map((m) => normDay(m.day)).filter(Boolean))];
    const times = [...new Set((section.meetings || []).map((m) => `${m.start_time}-${m.end_time}`))];
    const options = state.build
      ? state.build.options.filter((o) => (o.mappings || []).some((m) => String(m.term_section_id) === String(section.term_section_id))).map((o) => o.name)
      : [];
    return {
      id: String(section.term_section_id), label: String(section.section || '').trim(), cap, clash, reasons, seats,
      days: days.map((d) => DAY_SHORT[d]).join(' '), times: times.join(' / '), options,
    };
  }
  function sectionRow(model, mode, pinned) {
    const inPlan = mode !== 'read';
    const row = el(inPlan ? 'label' : 'div', 'plb-dt-sec');
    const usable = !model.reasons.length;
    const input = el('input', 'plb-dt-radio');
    input.type = 'radio';
    input.name = 'plbPin';
    input.value = model.id;
    input.dataset.fk = `sec-${model.id}`;
    input.checked = pinned;
    input.disabled = !usable && !pinned;       // a pin that became unusable stays selected, flagged
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
        el('span', 'plb-dt-sec-main', bdi(`§${model.label || '?'}`),
          model.days || model.times ? ' ' : null, model.days, model.days && model.times ? ' ' : null, model.times ? bdi(model.times) : null),
        el('span', 'plb-dt-sec-seats', model.seats),
        flags.length ? el('span', 'plb-dt-flags', ...flags) : null,
        !usable ? el('span', 'plb-dt-why', pinned ? T.pinBroken : null, model.reasons.join(' ')) : null)));
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
      // Focus was inside a panel that has gone: put it on the field that adds a course.
      if (inside && !$('plbAdd').disabled) $('plbAdd').focus();
      return;
    }
    const code = state.detail.code;
    const item = info.item || null;
    const pin = item && item.pinned_sections && item.pinned_sections.length ? item.pinned_sections[0] : null;
    const pinnedId = pin ? String(pin.term_section_id) : null;
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

    const list = (state.catalog ? state.catalog.get(code) || [] : null);
    const group = el(info.inPlan ? 'fieldset' : 'div', 'plb-dt-group');
    if (info.inPlan) group.append(el('legend', 'plb-mic', T.detailSections));
    else group.append(el('p', 'plb-mic', T.detailSections));
    if (info.inPlan) {
      const any = el('label', 'plb-dt-sec');
      const input = el('input', 'plb-dt-radio');
      input.type = 'radio';
      input.name = 'plbPin';
      input.value = 'any';
      input.checked = !pin;
      input.dataset.fk = 'any';
      any.append(input, el('span', 'plb-dt-sec-body',
        el('span', 'plb-dt-sec-main', T.anySectionOpt), el('span', 'plb-dt-sec-seats', T.anySectionHint)));
      group.append(any);
    } else {
      group.append(el('p', 'plb-dt-hint', T.readOnlyHint));
    }
    if (list === null) group.append(el('p', 'plb-dt-hint', T.noCatalog));
    else if (!list.length) group.append(el('p', 'plb-dt-hint', T.noSections));
    else {
      const sorted = list.slice().sort((a, b) => String(a.section || '').localeCompare(String(b.section || ''), 'en', { numeric: true }));
      for (const section of sorted) group.append(sectionRow(sectionModel(section), info.inPlan ? 'pick' : 'read', pinnedId === String(section.term_section_id)));
    }
    // A pin whose section is not in the list any more is flagged, never silently dropped.
    if (pin && !(list || []).some((section) => String(section.term_section_id) === pinnedId)) {
      const gone = el('label', 'plb-dt-sec');
      gone.dataset.unusable = 'true';
      gone.dataset.tsid = pinnedId;
      const input = el('input', 'plb-dt-radio');
      input.type = 'radio';
      input.name = 'plbPin';
      input.value = pinnedId;
      input.checked = true;
      input.dataset.fk = `sec-${pinnedId}`;
      gone.append(input, el('span', 'plb-dt-sec-body',
        el('span', 'plb-dt-sec-main', bdi(`§${pin.section || '?'}`)), el('span', 'plb-dt-why', T.pinGone)));
      group.append(gone);
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
    if (focusKey) {
      const target = [...aside.querySelectorAll('[data-fk]')].find((n) => n.dataset.fk === focusKey && !n.disabled) || title;
      target.focus();
    }
  }
  function openDetail(code) {
    const key = norm(code);
    const item = state.shortlist.find((c) => norm(c.course_code) === key);
    const course = item ? null : planPool().find((c) => c.code === key);
    if (!item && !course) return;
    state.detail = {
      code: key,
      name: item ? String(item.course_name || '') : course.name,
      credits: item ? Number(item.credits) || 0 : course.credits,
    };
    renderCourses();
    $('plbDetailTitle').focus();
  }
  function closeDetail(returnFocus) {
    const code = state.detail ? state.detail.code : null;
    state.detail = null;
    renderCourses();
    if (!returnFocus || !code) return;
    const button = document.querySelector(`#plbTable [data-act="detail"][data-code="${CSS.escape(code)}"]`);
    if (button) button.focus();
  }
  // Exactly the shape the current page pins a section in (page-planner.js, the section row click).
  function setPin(code, section) {
    const item = state.shortlist.find((c) => norm(c.course_code) === norm(code));
    if (!item) return;
    if (section) item.pinned_sections = [{ term_section_id: Number(section.term_section_id), section: String(section.section || '').trim() }];
    else delete item.pinned_sections;
    planChanged();
    if (section) setStatus('ok', ...T.pinnedSet(item.course_code, String(section.section || '').trim()));
    else setStatus('ok', ...T.pinnedCleared(item.course_code));
    renderCourses();
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
      } else if (target.name === 'plbPin') {
        if (target.value === 'any') { setPin(code, null); return; }
        const list = state.catalog ? state.catalog.get(code) || [] : [];
        const section = list.find((x) => String(x.term_section_id) === target.value);
        if (section) setPin(code, section);
      }
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
    }
  }
  init();
})();
