/*
 * Exam Timetable Builder — client-side logic.
 *
 * Flow:
 *   1. Page load     → fetch filter chips (programs, sections) + history
 *   2. Load Courses  → POST filters → render a searchable course selection list
 *   3. Build         → POST config → render KPI cards, schedule grid, conflict matrix
 *   4. Interact      → move exams, pin explicitly, check changes, open KPI details
 *   5. History       → load/delete saved runs
 *
 * Global state:
 *   _coursesLoaded   – whether the course preview is populated
 *   _pinnedCourses   – course identity → fixed exam time and course metadata
 *   _linkedExams     – courses examined as one exam, each member by identity
 *   _currentRunId    – ID of the currently viewed run (for export link)
 *   _drillData       – {overload: [], heavy: [], ...} detail records for KPI drilldown;
 *                      null for a detail the loaded run never recorded
 *   _programCourses  – {programName: Set(course_codes)} for programme-based filtering
 */
const IS_AR = LANGUAGE_CODE === 'ar';
const CAN_DELETE_EXAM_TIMETABLE = document.querySelector('[data-exam-builder]')?.dataset.canDeleteExamTimetable === 'true';
// Lock and unlock buttons are for whoever may Save a timetable; anyone else
// sees which days and periods are locked, and no buttons.
const CAN_EDIT_EXAM_TIMETABLE = document.querySelector('[data-exam-builder]')?.dataset.canEditExamTimetable === 'true';
const T = {
  ready:      IS_AR ? 'جاهز. حدد الفلاتر ثم انقر تحميل المقررات.' : 'Ready. Select filters then click Load Courses.',
  building:   IS_AR ? 'جارٍ بناء الجدول...' : 'Building timetable...',
  done:       IS_AR ? 'تم بناء الجدول بنجاح.' : 'Timetable built successfully.',
  error:      IS_AR ? 'خطأ' : 'Error',
  fillAll:    IS_AR ? 'يرجى ملء جميع الحقول.' : 'Please fill in all fields.',
  invalidDays: IS_AR ? 'عدد أيام الاختبارات يجب أن يكون عدداً صحيحاً من 1 إلى 60.' : 'Exam days must be a whole number from 1 to 60.',
  invalidMax: IS_AR ? 'الحد الأقصى للاختبارات في اليوم يجب أن يكون عدداً صحيحاً من 1 إلى 10.' : 'Max exams/day must be a whole number from 1 to 10.',
  invalidTime: IS_AR ? 'أدخل وقتاً من 00:00 إلى 23:59، مثل 16 أو 16:30.' : 'Enter a time from 00:00 to 23:59, such as 16 or 16:30.',
  incompletePeriod: IS_AR ? 'أكمل وقت البداية والنهاية لإتاحة هذه الفترة للتثبيت.' : 'Enter both start and end times to make this period available for pinning.',
  invalidRange: IS_AR ? 'يجب أن تنتهي كل فترة بعد بدايتها في اليوم نفسه.' : 'Each period must end after it starts on the same day.',
  overlapPeriods: IS_AR ? 'لا يمكن أن تتداخل فترات الاختبارات.' : 'Exam periods must not overlap.',
  noPeriods: IS_AR ? 'أضف فترة اختبار واحدة على الأقل.' : 'Add at least one exam period.',
  noPrograms: IS_AR ? 'اختر برنامجاً واحداً على الأقل.' : 'Select at least one program.',
  noSections: IS_AR ? 'اختر فئة طلاب واحدة على الأقل.' : 'Select at least one student group.',
  filtersChanged: IS_AR ? 'تغيّرت الفلاتر. انقر تحميل المقررات لتحديث القائمة ومراجعة التثبيتات.' : 'Filters changed. Load Courses to refresh the list and review retained pins.',
  pinCourse: IS_AR ? 'تثبيت المقرر' : 'Pin course',
  updatePin: IS_AR ? 'تحديث التثبيت' : 'Update pin',
  choosePinCourse: IS_AR ? 'اختر مقرراً محدداً للاختبار' : 'Choose a selected course',
  choosePinDay: IS_AR ? 'اختر اليوم' : 'Choose a day',
  choosePinPeriod: IS_AR ? 'اختر الفترة' : 'Choose a period',
  pinNotSelected: IS_AR ? 'أعد تحديد هذا المقرر أو ألغِ تثبيته.' : 'Reselect this course or remove its pin.',
  pinUnavailable: IS_AR ? 'هذا المقرر غير موجود ضمن الفلاتر الحالية. أعد تحميله أو ألغِ تثبيته.' : 'This course is unavailable in the current filters. Reload it or remove its pin.',
  pinSlotUnavailable: IS_AR ? 'هذا الموعد غير موجود في إعدادات الجدول. عدّل التثبيت أو ألغِه.' : 'This time is no longer in the timetable settings. Edit or remove the pin.',
  pinNeedsReview: IS_AR ? 'راجع التثبيتات المعلّمة قبل المتابعة.' : 'Resolve the highlighted pins before continuing.',
  pinNeedsCourses: IS_AR ? 'حمّل المقررات للتحقق من المواعيد المثبتة.' : 'Load courses to check the retained pins.',
  pinSettingsChanged: IS_AR ? 'حمّل المقررات لبناء جدول جديد بهذه الإعدادات. ستبقى المواعيد المثبتة محفوظة للمراجعة.' : 'Load Courses to build with these changed settings. Your pins will be retained for review.',
  pinEdit: IS_AR ? 'تعديل' : 'Edit',
  pinRemove: IS_AR ? 'إلغاء التثبيت' : 'Unpin',
  pinsReady: IS_AR ? 'سيحافظ البناء على المواعيد المثبتة أدناه.' : 'The builder will keep the pinned times below.',
  pinsEmpty: IS_AR ? 'لا توجد مواعيد مثبتة. سيختار النظام المواعيد لجميع المقررات المحددة.' : 'No fixed times yet. The builder will schedule all selected courses.',
  saveBeforeExport: IS_AR ? 'توجد تغييرات غير محفوظة. احفظ التغييرات أو حسّن الجدول قبل التصدير.' : 'Unsaved changes. Save Changes or Optimize before exporting.',
  exportReady: IS_AR ? 'تحميل جدول الاختبارات المحفوظ كملف إكسل' : 'Download the saved exam timetable as Excel',
  noHistory:  IS_AR ? 'لا توجد عمليات سابقة.' : 'No previous runs.',
  reqFailed:  IS_AR ? 'فشل الطلب' : 'Request failed',
  loadingRun: IS_AR ? 'جارٍ تحميل النتائج...' : 'Loading results...',
  sameSlot:   IS_AR ? 'الطالب {sid} لديه {n} اختبارات في نفس الفترة #{slot}: {courses}' : 'Student {sid} has {n} exams in same slot #{slot}: {courses}',
  overflow:   IS_AR ? 'فترة إضافية (تجاوز)' : 'Overflow slot',
  bucketViol: IS_AR ? 'مجموعة {prog}/فصل{term}: {courses} في نفس اليوم ({day})' : 'Bucket {prog}/Term{term}: {courses} on same day ({day})',
  infeasible: IS_AR ? 'الجدول غير ممكن! المجموعات التالية تحتوي مقررات أكثر من الأيام المتاحة:' : 'Infeasible schedule! The following buckets have more courses than available days:',
  infeasItem: IS_AR ? '{prog}/فصل{term}: {size} مقررات > {days} أيام ({courses})' : '{prog}/Term{term}: {size} courses > {days} days ({courses})',
  loadingCourses: IS_AR ? 'جارٍ تحميل المقررات...' : 'Loading courses...',
  coursesLoaded:  IS_AR ? 'تم تحميل {n} مقرر. اختر المقررات ثم انقر بناء الجدول.' : '{n} courses loaded. Select courses then click Build Timetable.',
  noCourses:      IS_AR ? 'لا توجد تسجيلات فعلية في الجداول الدراسية المستوردة ضمن الفلاتر المحددة. راجع بيانات الجداول ثم أعد تحميل المقررات.' : 'No actual enrollments from imported student timetables match these filters. Review the timetable imports, then load courses again.',
  selectAll:      IS_AR ? 'تحديد الكل' : 'Select all',
  deselectAll:    IS_AR ? 'إلغاء تحديد الكل' : 'Deselect all',
  selectComputer: IS_AR ? 'الحاسوبية فقط' : 'Only computer courses',
  selectGeneral:  IS_AR ? 'العامة فقط' : 'Only general courses',
  selectOnline:   IS_AR ? 'تحديد الإلكترونية' : 'Select online',
  deselectOnline: IS_AR ? 'إلغاء تحديد الإلكترونية' : 'Deselect online',
  loadFirst:      IS_AR ? 'يرجى تحميل المقررات أولاً.' : 'Please load courses first.',
  noSelected:     IS_AR ? 'يرجى اختيار مقرر واحد على الأقل.' : 'Please select at least one course.',
  loadedRunAction: IS_AR ? 'هذا الجدول محفوظ. استخدم «حفظ التغييرات» أو «إصلاح بأقل تغيير» أو «تحسين الجدول الحالي» أو «إضافة مقررات…» لتعديله. أما «تحميل المقررات» فيبدأ جدولاً جديداً.' : 'This timetable is saved. Use Save Changes, Fix with fewest moves, Optimize or Add courses… to change it. Load Courses starts a new timetable.',
  savingLoaded:   IS_AR ? 'جارٍ حفظ التغييرات...' : 'Saving loaded-run changes...',
  optimizing:     IS_AR ? 'جارٍ تحسين الجدول المحمّل...' : 'Optimizing from loaded run...',
  optimized:      IS_AR ? 'تم حفظ الجدول المحسّن.' : 'Optimized run saved.',
  repairing:      IS_AR ? 'جارٍ إصلاح الجدول بأقل تغيير...' : 'Repairing with the fewest moves...',
  repaired:       IS_AR ? 'تم حفظ الجدول بعد الإصلاح.' : 'Repaired run saved.',
  optimizeNoBetter: IS_AR ? 'لم يُعثر على جدول أفضل، فلم يُحفظ شيء.' : 'No better timetable was found, so nothing was saved.',
  repairUnchanged: IS_AR ? 'لم يُنقل أي اختبار، فلم يُحفظ شيء.' : 'No exam was moved, so nothing was saved.',
  savedChanges:   IS_AR ? 'تم حفظ التغييرات.' : 'Loaded-run changes saved.',
  addingCourses:  IS_AR ? 'جارٍ إضافة المقررات…' : 'Adding courses…',
  coursesAdded:   IS_AR ? 'أُضيفت المقررات وحُفظت النتيجة جدولاً جديداً.' : 'Courses added. Saved as a new timetable.',
  coursesSavedNotAll: IS_AR ? 'حُفظت النتيجة جدولاً جديداً، وبقي بعض المقررات المضافة دون موعد. راجع التقرير.' : 'Saved as a new timetable. Not every course could be placed: see the report.',
  buildPinned:    IS_AR ? 'بناء ({n} مثبت)' : 'Build ({n} pinned)',
  show:           IS_AR ? 'عرض' : 'Show',
  hide:           IS_AR ? 'إخفاء' : 'Hide',
  students:       IS_AR ? 'طلاب' : 'students',
  noConflicts:    IS_AR ? 'لا توجد تعارضات.' : 'No conflicts.',
  rebuild:        IS_AR ? 'إعادة البناء ({n} مثبت)' : 'Rebuild ({n} pinned)',
  pinCount:       IS_AR ? '{n} مقرر مثبت' : '{n} course(s) pinned',
  deleteRun:      IS_AR ? 'حذف هذا السجل؟' : 'Delete this run?',
  deleteRunBody:  IS_AR ? '<p>سيتم حذف هذا السجل نهائياً ولا يمكن التراجع.</p>' : '<p>This run will be permanently deleted. This cannot be undone.</p>',
  deleteConfirm:  IS_AR ? 'حذف نهائياً' : 'Delete permanently',
  deleted:        IS_AR ? 'تم حذف السجل.' : 'Run deleted.',
  deleteFailed:   IS_AR ? 'فشل حذف السجل.' : 'Failed to delete run.',
  showingRuns:    IS_AR ? '{from}-{to} من {total}' : '{from}-{to} of {total}',
};

const CSRF = document.querySelector('[name=csrfmiddlewaretoken]')?.value
  || 'djCsrfToken';

const $ = id => document.getElementById(id);

// Preserve this tab's draft when an API request reaches a login/error page.
// The shared safeFetch helper returns null and cannot retain structured Save
// rejection codes, so this page decodes responses before its existing handlers.
async function readExamResponse(response) {
  const finalPath = response.url ? new URL(response.url, window.location.href).pathname : '';
  const kind = response.status === 401 || (response.redirected && /\/login\/?$/.test(finalPath)) ? 'auth' : null;
  if (kind) {
    const error = new Error(IS_AR ? 'انتهت جلسة تسجيل الدخول. سجّل الدخول في تبويب جديد ثم أعد المحاولة. تغييراتك محفوظة في هذا التبويب.' : 'Your session expired. Sign in in a new tab, then retry. Your changes remain in this tab.');
    error.examRequestKind = kind;
    throw error;
  }
  if (response.status === 429) {
    // The server sends Retry-After; without it the registrar saw an untranslated
    // "Rate limit exceeded" and no idea how long to wait.
    const wait = Math.max(0, Math.ceil(Number(response.headers?.get('Retry-After')) || 0));
    const error = new Error(IS_AR
      ? `طلبات كثيرة على الجدول في وقت قصير. ${wait ? `انتظر ${arabicCount(wait, AR_SECONDS)}` : 'انتظر قليلاً'} ثم أعد المحاولة. تغييراتك باقية في هذا التبويب.`
      : `Too many timetable actions in a short time. ${wait ? `Wait ${wait} second${wait === 1 ? '' : 's'}` : 'Wait a moment'}, then retry. Your changes remain in this tab.`);
    error.examRequestKind = 'throttled';
    throw error;
  }
  let data;
  try {
    const contentType = response.headers?.get('content-type') || '';
    if (contentType && !/\bjson\b/i.test(contentType)) throw new Error('non-json');
    data = await response.json();
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('invalid-json');
  } catch (_) {
    const error = new Error(response.status === 403
      ? (IS_AR ? 'تعذر التحقق من الجلسة أو رمز الأمان. سجّل الدخول مجدداً ثم أعد المحاولة. تغييراتك باقية في هذا التبويب.' : 'Your session or security token could not be verified. Sign in again, then retry. Your changes remain in this tab.')
      : (IS_AR ? 'أعاد الخادم استجابة غير متوقعة. أعد المحاولة. لم تُستبدل مسودتك.' : 'The server returned an unexpected response. Retry the request. Your draft has not been replaced.'));
    error.examRequestKind = response.status === 403 ? 'auth' : 'server';
    throw error;
  }
  return data;
}

// The buckets that make a Build infeasible, one line each.
function infeasibleItems(violations) {
  return violations.map(v => T.infeasItem
    .replace('{prog}', v.program)
    .replace('{term}', v.programme_term)
    .replace('{size}', v.bucket_size)
    .replace('{days}', v.num_days)
    .replace('{courses}', (v.courses || []).join(', ')));
}

function examResponseError(data) {
  const code = data.code || data.error_code;
  if (code === 'run_not_copyable') return new Error(IS_AR
    ? 'لا يمكن فتح هذا الجدول المحفوظ بإصدار التطبيق الحالي، لذا لا يمكن إنشاء نسخة قابلة للتعديل منه.'
    : data.error || 'This saved timetable cannot be opened by this application version and cannot be copied for editing.');
  if (code === 'enrollment_source_changed') {
    const error = new Error(IS_AR ? 'تغيّر مصدر تسجيلات الاختبارات. حمّل المقررات من الجداول الدراسية المستوردة وأعد البناء. لم تتغير مسودتك.' : 'The exam enrollment source has changed. Load Courses from imported student timetables and rebuild. Your draft is unchanged.');
    error.examRequestKind = 'enrollment-source-changed';
    return error;
  }
  if (code === 'job_in_progress') {
    const holder = data.active_job || {};
    const started = holder.status !== 'queued';
    const refused = new Error(holder.mine ? JOB_TEXT.mineRunning(started) : JOB_TEXT.othersRunning(holder.owner || '', started));
    refused.examInfo = true;
    return refused;
  }
  if (code === 'solver_busy') {
    // Not an error: the solver is someone else's for now.
    const busy = new Error(JOB_TEXT.solverBusy(data.holder));
    busy.examRequestKind = 'solver-busy';
    busy.examInfo = true;
    busy.examHolder = data.holder || null;
    return busy;
  }
  if (code === 'job_not_started') return new Error(JOB_TEXT.notStarted);
  if (code === 'job_not_found') return new Error(JOB_TEXT.jobNotFound);
  if (code === 'run_deleted' || code === 'run_not_found') {
    // Worded for wherever an error is shown - beside another timetable, too;
    // JOB_TEXT.runDeleted is the panel's own sentence.
    const gone = new Error(code === 'run_not_found' ? JOB_TEXT.openedGone : JOB_TEXT.savedGone);
    gone.examRunGone = true;
    return gone;
  }
  if (typeof code === 'string' && code.startsWith('job_')) return new Error(JOB_TEXT.noResult);
  if (typeof code === 'string' && code.startsWith('linked_exams')) return linkedExamsRefusal(code, data.field);
  if (typeof code === 'string' && code.startsWith('exam_locks')) return examLocksRefusal(data);
  if (typeof code === 'string' && code.startsWith('add_courses')) return addCoursesRefusal(data);
  if (code !== 'courses_unavailable') return new Error(data.error || T.reqFailed);
  const unavailable = Array.isArray(data.unavailable_courses) ? data.unavailable_courses.map(String).join(', ') : '';
  const error = new Error((IS_AR
    ? 'يتضمن التحديد مقررات دون تسجيلات فعلية في الجداول الدراسية المستوردة. حمّل المقررات لمراجعة القائمة الحالية وإعادة البناء. لم تتغير مسودتك.'
    : 'This selection includes courses without actual enrollments in imported student timetables. Load Courses to review the current list and rebuild. Your draft is unchanged.')
    + (unavailable ? (IS_AR ? ' المقررات غير المتاحة: ' : ' Unavailable courses: ') + unavailable : ''));
  error.examRequestKind = 'courses-unavailable';
  return error;
}

function reviewExamCourseSource() {
  if (_builderBusy) return;
  $('examSetupDetails').open = true;
  $('loadCoursesBtn').focus({ preventScroll: true });
  $('examEnrollmentSource').scrollIntoView({ block: 'center', behavior: 'smooth' });
}

const shownExamRequestErrors = new WeakSet();
function showExamRequestError(error, source) {
  if (error && typeof error === 'object') {
    source = error.examRequestSource || source;
    error.examRequestSource = source;
  }
  const message = error instanceof TypeError
    ? (IS_AR ? 'تعذر الاتصال بالخادم. تحقق من الاتصال ثم أعد المحاولة. تغييراتك باقية في هذا التبويب.' : 'Could not reach the server. Check your connection and retry. Your changes remain in this tab.')
    : error?.message || T.reqFailed;
  // Shown again beside the link it names, in the builder.
  if (error?.examRequestKind === 'linked-exams') recordLinkError(error);
  if (['courses-unavailable', 'enrollment-source-changed'].includes(error.examRequestKind) && _currentResultData) {
    _sourceCoursesRejected = true;
    if (error.examRequestKind === 'enrollment-source-changed') _sourceRebuildRequired = true;
    _evaluatedInputsChanged = true;
    _evaluatedEditorSignature = null;
    _checkState = 'error';
    _checkError = message;
  }
  const editorVisible = !$('etResults').classList.contains('d-none');
  for (const id of ['examRequestError', 'examEditorRequestError']) {
    const banner = $(id);
    if (!banner) continue;
    banner.hidden = false;
    banner.setAttribute('role', (id === 'examEditorRequestError') === editorVisible ? 'alert' : 'note');
    banner.dataset.errorSource = source;
    banner.dataset.errorKind = error.examRequestKind || 'request';
    banner.textContent = message;
    if (error.examRequestKind === 'auth') {
      const link = document.createElement('a');
      const destination = new URL('/login/', window.location.origin);
      destination.searchParams.set('next', window.location.pathname + window.location.search);
      link.href = destination.href;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = IS_AR ? 'تسجيل الدخول في تبويب جديد' : 'Sign in in a new tab';
      banner.append(' ', link);
    } else if (['courses-unavailable', 'enrollment-source-changed'].includes(error.examRequestKind)) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-outline-secondary et-source-review-action';
      button.textContent = IS_AR ? 'مراجعة مصدر المقررات' : 'Review course source';
      button.addEventListener('click', reviewExamCourseSource);
      banner.append(' ', button);
    } else if (error.examRequestKind === 'linked-exams') {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-outline-secondary et-source-review-action';
      button.textContent = IS_AR ? 'مراجعة الاختبارات المرتبطة' : 'Review linked exams';
      button.addEventListener('click', reviewLinkedExams);
      banner.append(' ', button);
    }
  }
  const trackable = error && typeof error === 'object';
  if (!trackable || !shownExamRequestErrors.has(error)) {
    if (trackable) shownExamRequestErrors.add(error);
    notify.error(T.error, message);
  }
  return message;
}

function clearExamRequestError(source) {
  for (const id of ['examRequestError', 'examEditorRequestError']) {
    const banner = $(id);
    if (banner && banner.dataset.errorSource === source) {
      banner.hidden = true;
      banner.replaceChildren();
    }
  }
}

function clearExamCourseSourceError() {
  _sourceCoursesRejected = false;
  _sourceRebuildRequired = false;
  for (const id of ['examRequestError', 'examEditorRequestError']) {
    const banner = $(id);
    if (['courses-unavailable', 'enrollment-source-changed'].includes(banner?.dataset.errorKind)) {
      banner.hidden = true;
      banner.replaceChildren();
    }
  }
}

/* ── Day label generator ── */
const WORK_DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'];
let _loadedRunDaysOverride = null;

function positiveInteger(value, maximum) {
  const raw = String(value).trim();
  const number = /^[0-9]+$/.test(raw) ? Number(raw) : NaN;
  return Number.isInteger(number) && number >= 1 && number <= maximum ? number : null;
}

function inputError(message, field) {
  $('etStatus').textContent = message;
  $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
  const target = field?.id === 'examPinCourse' ? $('examPinCourseSearch')
    : field?.id === 'examLinkCourse' ? $('examLinkCourseSearch') : field;
  let parent = target?.parentElement;
  while (parent) {
    if (parent.tagName === 'DETAILS') parent.open = true;
    parent = parent.parentElement;
  }
  target?.focus();
  return null;
}

function readExamScope() {
  const programs = getCheckedValues('progList');
  const sections = getCheckedValues('secList');
  if (!programs.length) return inputError(T.noPrograms);
  if (!sections.length) return inputError(T.noSections);
  return { programs, sections };
}

// ``label``: a name given elsewhere (the Add courses list names its new
// timetable itself), in place of the setup field's.
function readExamHeader({ label: named = null } = {}) {
  const label = named ?? $('etLabel').value.trim();
  if (!label) return inputError(T.fillAll, $('etLabel'));
  if (positiveInteger($('etNumDays').value, 60) === null) {
    return inputError(T.invalidDays, $('etNumDays'));
  }
  const maxPerDay = positiveInteger($('etMaxPerDay').value, 10);
  if (maxPerDay === null) return inputError(T.invalidMax, $('etMaxPerDay'));
  const days = generateDayLabels();
  if (!days.length) return inputError(T.invalidDays, $('etNumDays'));
  const { periods, errors } = readExamPeriods();
  if (errors.length) return inputError(errors[0].message, errors[0].field);
  return { label, days, periods, maxPerDay };
}

function generateDayLabels() {
  if (_loadedRunDaysOverride) return [..._loadedRunDaysOverride];

  const startDay = $('etStartDay').value;
  const count    = positiveInteger($('etNumDays').value, 60);
  const startIdx = WORK_DAYS.indexOf(startDay);
  if (startIdx === -1 || count === null) return [];

  const days = [];
  let week = 1;
  const needPrefix = count > WORK_DAYS.length;

  for (let i = 0; i < count; i++) {
    const idx = (startIdx + i) % WORK_DAYS.length;
    if (i > 0 && idx === 0) week++;
    days.push(needPrefix ? `W${week}-${WORK_DAYS[idx]}` : WORK_DAYS[idx]);
  }
  return days;
}

// Day labels gain a week prefix after five days. Keep the underlying
// weekday/week identity stable, including a short range crossing Sunday.
function examDayIdentity(day, startDay = $('etStartDay').value) {
  const label = String(day || '');
  const prefixed = /^W([1-9][0-9]*)-(Sun|Mon|Tue|Wed|Thu)$/.exec(label);
  if (prefixed) return `W${Number(prefixed[1])}-${prefixed[2]}`;
  const dayIndex = WORK_DAYS.indexOf(label);
  const startLabel = String(startDay || '');
  const startIndex = WORK_DAYS.indexOf(startLabel.split('-').at(-1));
  if (dayIndex < 0 || startIndex < 0) return `label:${label}`;
  const startWeek = Number(/^W([1-9][0-9]*)-/.exec(startLabel)?.[1] || 1);
  return `W${startWeek + (dayIndex < startIndex ? 1 : 0)}-${label}`;
}

function reconcilePinnedDayLabels() {
  const days = generateDayLabels();
  if (!days.length) return; // Typing a replacement count must not erase identity.
  const labelsByIdentity = new Map(days.map(day => [examDayIdentity(day), day]));
  for (const pin of Object.values(_pinnedCourses)) {
    const label = labelsByIdentity.get(pin.day_identity);
    if (label) pin.day = label;
  }
}

function updateDayPreview() {
  const days = generateDayLabels();
  $('dayPreview').innerHTML = days.map(day => `<span>${escapeAttr(day)}</span>`).join('');
  $('etCalendarSummary').textContent = days.length
    ? (IS_AR ? `${days.length} أيام اختبار · عرض الأيام` : `${days.length} exam days · View days`)
    : T.invalidDays;
}

$('etStartDay').addEventListener('change', () => {
  _loadedRunDaysOverride = null;
  updateDayPreview();
  reconcilePinnedDayLabels();
  renderPinEditor();
});
$('etNumDays').addEventListener('input', () => {
  _loadedRunDaysOverride = null;
  updateDayPreview();
  reconcilePinnedDayLabels();
  renderPinEditor();
});
updateDayPreview();

/* ── Structured period repeater ── */
function normalizeExamTime(value) {
  const raw = String(value).trim().replace(/[٠-٩۰-۹]/g, digit =>
    String(digit.charCodeAt(0) - (digit >= '۰' ? 0x6f0 : 0x660)));
  const match = /^(\d{1,2})(?::([0-5]\d))?$/.exec(raw);
  if (!match || Number(match[1]) > 23) return null;
  return `${match[1].padStart(2, '0')}:${match[2] || '00'}`;
}

// One parser feeds submission, fixed-time choices, and inline feedback.
function readExamPeriods() {
  const rows = [...$('etPeriodsRepeater').querySelectorAll('.et-period-row')];
  const periods = [], windows = [], errors = [];
  if (!rows.length) errors.push({ message: T.noPeriods, field: $('etAddPeriod') });
  rows.forEach((row, index) => {
    const startField = row.querySelector('.et-period-start');
    const endField = row.querySelector('.et-period-end');
    const start = normalizeExamTime(startField.value);
    const end = normalizeExamTime(endField.value);
    let message = '', field = startField;
    if (!startField.value.trim() || !endField.value.trim()) {
      message = T.incompletePeriod;
      field = !startField.value.trim() ? startField : endField;
    } else if (!start || !end) {
      message = T.invalidTime;
      field = !start ? startField : endField;
    } else if (end <= start) {
      message = T.invalidRange;
      field = endField;
    } else if (windows.some(other => start < other.end && other.start < end)) {
      message = T.overlapPeriods;
    }
    if (message) {
      errors.push({ row, field, message: `${IS_AR ? 'الفترة' : 'Period'} ${index + 1}: ${message}` });
    } else {
      windows.push({ start, end });
      periods.push(`${start}-${end}`);
    }
  });
  return { periods, errors };
}

function renderPeriodFeedback(state = readExamPeriods()) {
  $('etPeriodsRepeater').querySelectorAll('.et-period-row').forEach((row, index) => {
    const error = state.errors.find(item => item.row === row);
    row.querySelector('.et-period-number').textContent = index + 1;
    for (const [selector, name] of [
      ['.et-period-start', IS_AR ? 'البداية' : 'start'],
      ['.et-period-end', IS_AR ? 'النهاية' : 'end'],
    ]) {
      const field = row.querySelector(selector);
      field.setAttribute('aria-label', `${IS_AR ? 'الفترة' : 'Period'} ${index + 1} ${name}`);
      field.setAttribute('aria-describedby', 'etPeriodsHelp etPeriodsFeedback');
      field.setAttribute('aria-invalid', String(error?.field === field));
      field.classList.toggle('is-invalid', error?.field === field);
    }
    const remove = row.querySelector('.et-period-remove');
    const label = IS_AR ? `إزالة الفترة ${index + 1}` : `Remove period ${index + 1}`;
    remove.setAttribute('aria-label', label);
    remove.title = label;
  });
  const feedback = $('etPeriodsFeedback');
  feedback.textContent = state.errors.map(error => error.message).join(' ');
  feedback.classList.toggle('d-none', !state.errors.length);
  return state;
}

function syncPeriodsHidden() {
  $('etPeriods').value = renderPeriodFeedback().periods.join(',');
  renderPinEditor();
  updateLoadedRunActions();
}

function addPeriodRow(startVal, endVal, sync = true) {
  const row = document.createElement('div');
  row.className = 'et-period-row';
  row.innerHTML =
    `<span class="et-period-number" aria-hidden="true"></span><input type="text" placeholder="HH:MM" class="form-control form-control-compact et-period-start" value="${escapeAttr(startVal || '')}">` +
    `<span class="et-period-sep">–</span>` +
    `<input type="text" placeholder="HH:MM" class="form-control form-control-compact et-period-end" value="${escapeAttr(endVal || '')}">` +
    `<button type="button" class="btn btn-sm btn-outline-secondary et-period-remove et-period-remove-btn" title="${IS_AR ? 'إزالة' : 'Remove'}">&times;</button>`;
  $('etPeriodsRepeater').appendChild(row);
  if (sync) syncPeriodsHidden();
  return row;
}

function setPeriodRows(periods) {
  const box = $('etPeriodsRepeater');
  box.innerHTML = '';
  const values = periods.length ? periods : ['08:00-10:00', '10:30-12:30', '13:00-15:00'];
  values.forEach(period => {
    const [startVal, endVal] = String(period).split('-').map(s => s.trim());
    addPeriodRow(startVal || '', endVal || '', false);
  });
  syncPeriodsHidden();
}

/* Delegation keeps new and initial rows on the same interaction path. */
$('etPeriodsRepeater').addEventListener('input', syncPeriodsHidden);
function commitPeriodField(event) {
  if (!event.target.matches('.et-period-start, .et-period-end')) return;
  const normalized = normalizeExamTime(event.target.value);
  if (normalized) event.target.value = normalized;
  syncPeriodsHidden();
}
['change', 'focusout'].forEach(eventName => $('etPeriodsRepeater').addEventListener(eventName, commitPeriodField));
$('etPeriodsRepeater').addEventListener('click', event => {
  const remove = event.target.closest('.et-period-remove');
  if (!remove) return;
  const row = remove.closest('.et-period-row');
  const focusTarget = row.nextElementSibling?.querySelector('input')
    || row.previousElementSibling?.querySelector('input') || $('etAddPeriod');
  row.remove();
  syncPeriodsHidden();
  focusTarget.focus();
});

$('etAddPeriod')?.addEventListener('click', () => addPeriodRow('', '').querySelector('input').focus());
renderPeriodFeedback();

/* ── Filter chips (programs & sections) ── */
// Render toggle-able chips for a list of items (e.g. programs or sections).
// Each chip is a <label> wrapping a hidden checkbox; clicking toggles state.
function renderChips(containerId, items, countId) {
  const box = $(containerId);
  if (!items.length) {
    box.innerHTML = `<small class="text-secondary">${IS_AR ? 'لا توجد بيانات' : 'None found'}</small>`;
    $(countId).textContent = '';
    return;
  }
  box.innerHTML = items.map(v =>
    `<label class="et-chip active"><input type="checkbox" value="${escapeAttr(v)}" checked>${escapeAttr(containerId === 'secList' ? studentGroupLabel(v) : v)}</label>`
  ).join('');
  box.querySelectorAll('.et-chip').forEach(chip => {
    const cb = chip.querySelector('input');
    cb.addEventListener('change', () => {
      chip.classList.toggle('active', cb.checked);
      updateChipCount(containerId, countId, items.length);
    });
  });
  updateChipCount(containerId, countId, items.length);
}

function updateChipCount(containerId, countId, total) {
  const checked = $(containerId).querySelectorAll('input:checked').length;
  $(countId).textContent = checked === total
    ? (IS_AR ? `(الكل ${total})` : `(all ${total})`)
    : `(${checked}/${total})`;
}

function getCheckedValues(containerId) {
  return [...$(containerId).querySelectorAll('input:checked')].map(cb => cb.value);
}

function escapeAttr(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function sourceCodeFromDisplay(displayCode, explicitSource) {
  const source = String(explicitSource ?? '').trim();
  if (source) return source;
  return String(displayCode ?? '').replace(/\s+\(\d+\)$/, '');
}

function getCheckedCourseEntries() {
  return [...$('courseList').querySelectorAll('input:checked')].map(cb => ({
    course_code: cb.value,
    source_course_code: cb.dataset.sourceCode || sourceCodeFromDisplay(cb.value),
    course_name: cb.dataset.courseName || '',
    course_identity: cb.dataset.courseIdentity || '',
  }));
}

async function loadFilters() {
  try {
    const res = await fetch('/ops/exam-timetable/filters/', {
      headers: { 'X-CSRFToken': getCsrfToken() || CSRF },
    });
    const data = await readExamResponse(res);
    if (!res.ok || !data.ok) throw examResponseError(data);
    clearExamRequestError('filters');
    // A saved run opened before the chips arrived (a link straight to it)
    // was read with none. Their arrival is not an edit: they take the run's
    // scope, and an unedited run stays unedited.
    const unedited = Boolean(_currentResultData && _savedEditorSignature !== null
      && editorSignature() === _savedEditorSignature && _evaluatedEditorSignature === _savedEditorSignature);
    renderChips('progList', data.programs ?? [], 'progCount');
    renderChips('secList', data.sections ?? [], 'secCount');
    if (_currentResultData?.enrollment_scope) applyScopeChips(_currentResultData.enrollment_scope);
    if (unedited) {
      _savedEditorSignature = editorSignature();
      _observedEditorSignature = _savedEditorSignature;
      _evaluatedEditorSignature = _savedEditorSignature;
      updateLoadedRunActions();
    }
  } catch (err) {
    showExamRequestError(err, 'filters');
  }
}
loadFilters();

/* ── Step 1: Load Courses (preview) ── */
let _coursesLoaded = false;

const COMPUTER_PREFIXES = new Set(['AI', 'DS', 'CS', 'IS', 'COE', 'CYB']);

function clearCoursePreview() {
  clearOutsideCourses();
  $('courseEmptyState').hidden = false;
  $('courseSearch').value = '';
  $('courseVisibility').value = 'all';
  $('courseNoMatches').hidden = true;
  $('coursePreview').classList.add('d-none');
  $('courseList').innerHTML = '';
  $('courseCount').textContent = '';
  $('toggleAllCourses').textContent = '';
  $('selectComputerCourses').textContent = '';
  $('selectGeneralCourses').textContent = '';
  $('toggleOnlineCourses').textContent = '';
  document.querySelectorAll('.select-sep').forEach(s => s.classList.add('d-none'));
  $('buildBtn').disabled = true;
  _coursesLoaded = false;
  updateLoadedRunActions();
  renderPinEditor();
}

function renderCourseList(courses, { inTimetable = false } = {}) {
  clearOutsideCourses();
  _courseMetadata = Object.fromEntries(courses.map(course => [course.course_code, course]));
  reconcilePinsWithCourses();
  reconcileLinksWithCourses();
  $('courseEmptyState').hidden = true;
  $('courseSearch').value = '';
  $('courseVisibility').value = 'all';
  const box = $('courseList');
  box.innerHTML = [...courses].sort((a, b) => a.course_code.localeCompare(b.course_code, 'en', { numeric: true })).map(c => {
    const code = String(c.course_code ?? '');
    const sourceCode = sourceCodeFromDisplay(code, c.source_course_code);
    const plans = c.programs || [];
    const searchable = [code, sourceCode, c.course_name || '', ...plans].join(' ').toLocaleLowerCase();
    const online = c.is_online ? `<span class="et-online-badge">${IS_AR ? 'إلكتروني' : 'Online'}</span>` : '';
    const inRun = inTimetable ? `<span class="et-add-badge">${IS_AR ? 'في الجدول' : 'In timetable'}</span>` : '';
    return `<label class="et-chip et-course-option active" data-search="${escapeAttr(searchable)}">
      <input type="checkbox" value="${escapeAttr(code)}" checked data-online="${c.is_online ? '1' : '0'}" data-source-code="${escapeAttr(sourceCode)}" data-course-name="${escapeAttr(c.course_name || '')}" data-course-identity="${escapeAttr(c.course_identity || '')}">
      <span class="et-course-main"><span class="et-course-code" dir="ltr">${escapeAttr(code)}</span>${online}${inRun}<span class="et-course-name">${escapeAttr(c.course_name || code)}</span></span>
      <span class="et-course-plans">${escapeAttr(plans.join(' · ') || '—')}</span>
      <span class="et-course-level"><span class="et-mobile-label">${IS_AR ? 'الساعات' : 'Credits'} </span>${escapeAttr(c.credit_hours || '—')}</span>
      <span class="et-course-enrollment"><span class="et-mobile-label">${IS_AR ? 'الطلاب' : 'Students'} </span>${escapeAttr(c.enrolled_count ?? 0)}</span>
    </label>`;
  }).join('');
  box.querySelectorAll('input').forEach(cb => cb.addEventListener('change', () => {
    cb.closest('.et-course-option').classList.toggle('active', cb.checked);
    updateCourseCount(courses.length);
  }));
  _coursesLoaded = courses.length > 0;
  $('selectComputerCourses').textContent = T.selectComputer;
  $('selectGeneralCourses').textContent = T.selectGeneral;
  updateCourseCount(courses.length);
}

// Filtering only changes visibility. Every selected identity stays in the payload.
function filterCourseList() {
  const terms = $('courseSearch').value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const visibility = $('courseVisibility').value;
  const rows = [...$('courseList').querySelectorAll('.et-course-option')];
  const focusedRow = document.activeElement?.closest('.et-course-option');
  let shown = 0, selected = 0;
  rows.forEach(row => {
    const cb = row.querySelector('input');
    const matchesState = visibility === 'all' || (visibility === 'selected' && cb.checked)
      || (visibility === 'unselected' && !cb.checked) || (visibility === 'online' && cb.dataset.online === '1');
    row.hidden = !matchesState || !terms.every(term => row.dataset.search.includes(term));
    if (!row.hidden) { shown++; if (cb.checked) selected++; }
  });
  // The other courses of a saved timetable: shown for All, Not selected and
  // (when online) Online; never for Selected - they are not in it.
  const outside = [...($('courseOutsideList')?.querySelectorAll('.et-course-option') || [])];
  let outsideShown = 0;
  outside.forEach(row => {
    const matchesState = visibility === 'all' || visibility === 'unselected' || (visibility === 'online' && row.dataset.online === '1');
    row.hidden = !matchesState || !terms.every(term => row.dataset.search.includes(term));
    if (!row.hidden) outsideShown++;
  });
  $('courseSearchCount').textContent = outside.length
    ? ADD_TEXT.setupCount(shown + outsideShown, rows.length + outside.length, rows.filter(row => row.querySelector('input').checked).length)
    : IS_AR ? `عرض ${shown} من ${rows.length} مقرر` : `Showing ${shown} of ${rows.length} courses`;
  $('courseNoMatches').hidden = shown + outsideShown > 0;
  $('selectVisibleCourses').disabled = shown === selected;
  $('deselectVisibleCourses').disabled = selected === 0;
  if (focusedRow?.hidden) {
    const index = rows.indexOf(focusedRow);
    const next = rows.slice(index + 1).find(row => !row.hidden)
      || rows.slice(0, index).reverse().find(row => !row.hidden);
    (next?.querySelector('input') || $('courseVisibility')).focus();
  }
}

function setVisibleCourses(checked) {
  // Snapshot visible rows first: changing checks can change the active filter.
  const rows = [...$('courseList').querySelectorAll('.et-course-option')].filter(row => !row.hidden);
  rows.forEach(row => {
    row.querySelector('input').checked = checked;
    row.classList.toggle('active', checked);
  });
  updateCourseCount($('courseList').querySelectorAll('input').length);
}
$('courseSearch').addEventListener('input', filterCourseList);
$('courseVisibility').addEventListener('change', filterCourseList);
$('selectVisibleCourses').addEventListener('click', () => setVisibleCourses(true));
$('deselectVisibleCourses').addEventListener('click', () => setVisibleCourses(false));

function updateCourseCount(total) {
  const checked = $('courseList').querySelectorAll('input:checked').length;
  $('courseCount').textContent = IS_AR ? `${checked} من ${total} محدد` : `${checked} of ${total} selected`;
  filterCourseList();
  updateToggleLabel();
  updateOnlineToggleLabel();
  renderPinEditor();
  updateLoadedRunActions();
}

function updateToggleLabel() {
  const all = $('courseList').querySelectorAll('input[type=checkbox]');
  const checked = $('courseList').querySelectorAll('input:checked');
  $('toggleAllCourses').textContent = checked.length === all.length ? T.deselectAll : T.selectAll;
}

$('toggleAllCourses').addEventListener('click', (e) => {
  e.preventDefault();
  const all = $('courseList').querySelectorAll('input[type=checkbox]');
  const checked = $('courseList').querySelectorAll('input:checked');
  const newState = checked.length < all.length;
  all.forEach(cb => {
    cb.checked = newState;
    cb.closest('.et-chip').classList.toggle('active', newState);
  });
  updateCourseCount(all.length);
});

function selectByPrefixGroup(matchComputer) {
  const all = $('courseList').querySelectorAll('input[type=checkbox]');
  all.forEach(cb => {
    const prefix = (cb.value.match(/^[A-Za-z]+/) || [''])[0];
    const isComputer = COMPUTER_PREFIXES.has(prefix);
    cb.checked = matchComputer ? isComputer : !isComputer;
    cb.closest('.et-chip').classList.toggle('active', cb.checked);
  });
  updateCourseCount(all.length);
}

$('selectComputerCourses').addEventListener('click', (e) => {
  e.preventDefault();
  selectByPrefixGroup(true);
});

$('selectGeneralCourses').addEventListener('click', (e) => {
  e.preventDefault();
  selectByPrefixGroup(false);
});

// Single toggle: if any online courses are currently UNchecked, click ticks
// them all on (additive — leaves other selections untouched). If all online
// courses are already checked, click unticks them all. Label adapts to the
// next action.
function updateOnlineToggleLabel() {
  const link = $('toggleOnlineCourses');
  if (!_coursesLoaded) {
    link.hidden = true;
    link.textContent = '';
    return;
  }
  const online = $('courseList').querySelectorAll('input[type=checkbox][data-online="1"]');
  if (!online.length) {
    link.hidden = true;
    link.textContent = '';
    return;
  }
  link.hidden = false;
  const allChecked = Array.from(online).every(cb => cb.checked);
  link.textContent = allChecked ? T.deselectOnline : T.selectOnline;
}

$('toggleOnlineCourses').addEventListener('click', (e) => {
  e.preventDefault();
  const online = $('courseList').querySelectorAll('input[type=checkbox][data-online="1"]');
  if (!online.length) return;
  const allChecked = Array.from(online).every(cb => cb.checked);
  const newState = !allChecked;
  online.forEach(cb => {
    cb.checked = newState;
    cb.closest('.et-chip').classList.toggle('active', newState);
  });
  const total = $('courseList').querySelectorAll('input[type=checkbox]').length;
  updateCourseCount(total);
  updateOnlineToggleLabel();
});

// Presets act once, then return keyboard focus to their disclosure.
document.addEventListener('click', event => {
  document.querySelectorAll('.et-selection-menu[open]').forEach(menu => {
    const action = event.target.closest('button');
    if (action && menu.contains(action)) {
      menu.open = false;
      menu.querySelector('summary').focus({ preventScroll: true });
    } else if (!menu.contains(event.target)) menu.open = false;
  });
});
document.addEventListener('keydown', event => {
  const menu = event.target.closest?.('.et-selection-menu[open]');
  if (event.key !== 'Escape' || !menu) return;
  event.preventDefault();
  menu.open = false;
  menu.querySelector('summary').focus({ preventScroll: true });
});

// Enable / disable the thin-threshold input alongside its toggle
$('etRelaxThin').addEventListener('change', () => {
  $('etThinThreshold').disabled = !$('etRelaxThin').checked;
});

// Clear course list when filters change
['progList', 'secList'].forEach(id => {
  $(id).addEventListener('change', async event => {
    if (hasUnsavedEdits()) {
      const input = event.target;
      const requested = input.checked;
      input.checked = !requested;
      input.closest('.et-chip')?.classList.toggle('active', input.checked);
      updateChipCount(id, id === 'progList' ? 'progCount' : 'secCount', $(id).querySelectorAll('input').length);
      if (!await confirmDiscardDraft()) { updateLoadedRunActions(); return; }
      input.checked = requested;
      input.closest('.et-chip')?.classList.toggle('active', requested);
      updateChipCount(id, id === 'progList' ? 'progCount' : 'secCount', $(id).querySelectorAll('input').length);
    }
    enterFreshBuildMode();
    // Not inside enterFreshBuildMode: a delete has just said "deleted" there.
    settleJobPanel();
    clearCoursePreview();
    $('etStatus').textContent = T.filtersChanged;
    $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';
  });
});

$('loadCoursesBtn').addEventListener('click', async () => {
  if (_builderBusy) return;
  if (!await confirmDiscardDraft()) return;
  const scope = readExamScope();
  if (!scope) return;
  const { programs, sections } = scope;

  $('loadCoursesBtn').disabled = true;
  setBuilderBusy(true);
  $('etStatus').textContent = T.loadingCourses;
  $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';

  try {
    const res = await fetch('/ops/exam-timetable/preview-courses/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() || CSRF },
      body: JSON.stringify({ programs, sections }),
    });
    const data = await readExamResponse(res);
    if (!res.ok || !data.ok) throw examResponseError(data);
    clearExamRequestError('courses');

    const courses = data.courses ?? [];
    enterFreshBuildMode();
    settleJobPanel();
    _currentResultData = null;
    _loadedRunForRebuild = false;
    _scheduleHasDraftMoves = false;
    updatePinBar();
    $('coursePreview').classList.remove('d-none');
    renderCourseList(courses);

    if (courses.length > 0) {
      $('etStatus').textContent = T.coursesLoaded.replace('{n}', courses.length);
      $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';
    } else {
      $('etStatus').textContent = T.noCourses;
      $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    }
  } catch (err) {
    $('etStatus').textContent = T.error + ': ' + showExamRequestError(err, 'courses');
    $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
  } finally {
    $('loadCoursesBtn').disabled = false;
    setBuilderBusy(false);
  }
});

/* ── Step 2: Build Timetable ── */
$('buildBtn').addEventListener('click', async () => {
  if (_builderBusy) return;
  if (_loadedRunForRebuild) {
    $('etStatus').textContent = T.loadedRunAction;
    $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    updateLoadedRunActions();
    return;
  }

  if (!_coursesLoaded) {
    $('etStatus').textContent = T.loadFirst;
    $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    return;
  }

  const selectedCourses = getCheckedValues('courseList');
  const selectedCourseEntries = getCheckedCourseEntries();
  if (!selectedCourses.length) {
    $('etStatus').textContent = T.noSelected;
    $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    return;
  }

  const scope = readExamScope();
  if (!scope) return;
  const header = readExamHeader();
  if (!header) return;
  const { label, days, periods, maxPerDay } = header;
  const { programs, sections } = scope;
  const pinned = validatedPinPayload(header);
  if (!pinned) return;
  const linkedExams = validatedLinkPayload();
  if (!linkedExams) return;
  const randomize = $('etRandomize').checked;
  const thinThreshold = readThinThresholdForPost();
  if (thinThreshold === null) return;
  // A Build from a saved run with locks keeps them, and says so first. There
  // is no Build that drops them: a lock ends only when it is unlocked.
  const locks = validatedBuildLocks(header, scope);
  if (!locks) return;
  if (locks.keep && !await dlg.confirm({
    title: LOCK_TEXT.buildTitle,
    body: `<p>${escapeAttr(LOCK_TEXT.buildKeeps(locks.cells, locks.exams))}</p><p>${escapeAttr(LOCK_TEXT.buildHow)}</p>`,
    icon: 'info',
    confirmLabel: LOCK_TEXT.buildConfirm,
    cancelLabel: LOCK_TEXT.cancel,
  })) return;
  if (_builderBusy) return;

  $('buildBtn').disabled = true;
  clearJobNotice();
  setBuilderBusy(true);
  $('etStatus').textContent = T.building;
  $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';

  let navigateToResult = false;
  try {
    const selectedSet = new Set(selectedCourses);
    const shouldRebuildExactSchedule = false;
    const baseSchedule = shouldRebuildExactSchedule
      ? _currentResultData.schedule
        .filter(e => selectedSet.has(e.course_code))
        .map(e => ({
          course_code: e.course_code,
          source_course_code: e.source_course_code || sourceCodeFromDisplay(e.course_code),
          course_name: e.course_name || '',
          course_identity: e.course_identity || '',
          day: e.day,
          period: e.period,
          slot_index: e.slot_index,
        }))
      : undefined;
    const payload = {
      label,
      days,
      periods,
      max_per_day: maxPerDay,
      programs,
      sections,
      selected_courses: selectedCourses,
      selected_course_entries: selectedCourseEntries,
      pinned,
      linked_exams: linkedExams,
      randomize,
      thin_conflict_threshold: thinThreshold,
      // The saved run the locks are kept from, and the locks kept - always
      // named. Left to inheritance, a saved run deleted since Load Courses
      // (in another tab, by a colleague) would build with no locks and no
      // word; named, it is refused (exam_locks_source_required).
      previous_run_id: locks.keep ? locks.runId : _currentResultData?.run_id,
      ...(locks.keep ? { exam_locks: locks.locks } : {}),
      base_schedule: baseSchedule && baseSchedule.length ? baseSchedule : undefined,
    };
    const { res, data, refusal } = await submitExamAction(payload, 'build', $('buildBtn'));
    if (!res.ok || !data.ok) {
      if (data.feasibility_error && data.violations) {
        throw new Error(`${T.infeasible}\n${infeasibleItems(data.violations).map(item => `\n• ${item}`).join('')}`);
      }
      throw Object.assign(examResponseError(data), { examRefusal: refusal });
    }

    clearExamRequestError('build');
    clearExamCourseSourceError();
    // The setup list becomes the timetable's own courses, exactly as for a
    // saved run: a course left out of the Build is found under "Other
    // courses", and added with Add courses - never by ticking it here, which
    // no longer matches the saved board.
    hydrateHeaderFromRun({ ...data, label });
    renderResults(data);
    navigateToResult = true;
    _loadedRunForRebuild = data.rebuild_mode === 'loaded_schedule';
    _scheduleHasDraftMoves = false;
    updatePinBar();
    $('etStatus').textContent = T.done;
    $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';
    loadHistory();
  } catch (err) {
    if (err.examJobKind || err.examInfo) reportJobOutcome(err);
    else {
      $('etStatus').textContent = T.error + ': ' + showExamRequestError(err, 'build');
      $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
    }
  } finally {
    setBuilderBusy(false);
    updateLoadedRunActions();
    if (navigateToResult) focusExamEditor();
    else if (document.activeElement === document.body && !$('buildBtn').disabled) $('buildBtn').focus({ preventScroll: true });
  }
});

/* ── Program → courses mapping (for schedule filter) ── */
// Built from buckets_summary in each build result.  Allows the schedule
// filter to accept a programme name (e.g. "AI") and highlight all its courses.
function loadedBaseSchedule(selectedCourses) {
  if (!_currentResultData || !Array.isArray(_currentResultData.schedule)) return undefined;
  const selectedSet = new Set(selectedCourses);
  const rows = _currentResultData.schedule
    .filter(e => selectedSet.has(e.course_code))
    .map(e => ({
      course_code: e.course_code,
      source_course_code: e.source_course_code || sourceCodeFromDisplay(e.course_code),
      course_name: e.course_name || '',
      course_identity: e.course_identity || '',
      day: e.day,
      period: e.period,
      slot_index: e.slot_index,
    }));
  return rows.length ? rows : undefined;
}

function readThinThresholdForPost() {
  if (!$('etRelaxThin').checked) return 0;
  const value = positiveInteger($('etThinThreshold').value, 10);
  return value ?? inputError(IS_AR ? 'أدخل عتبة صحيحة من 1 إلى 10 طلاب.' : 'Enter a whole-number enrollment threshold from 1 to 10.', $('etThinThreshold'));
}

function collectLoadedRunPayload(mode, { label: named = null } = {}) {
  if (needsExamSourceRebuild()) return null;
  // Optimize and Fix keep the saved locks - they send none, and the server
  // keeps the loaded run's. A lock change is saved first: Check, then Save.
  if (['optimize_loaded', 'minimum_change_repair', 'add_courses'].includes(mode) && lockChangesSinceSave().length) {
    const origin = { optimize_loaded: $('optimizeLoadedBtn'), minimum_change_repair: $('minChangeBtn'), add_courses: $('addCoursesBtn') }[mode];
    return refuseLockedEdit(LOCK_TEXT.saveLocksFirst(actionLabel(origin)));
  }
  const selectedCourses = getCheckedValues('courseList');
  if (!selectedCourses.length) {
    $('etStatus').textContent = T.noSelected;
    $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    return null;
  }
  const header = readExamHeader({ label: named });
  if (!header) return null;
  const { label, days, periods, maxPerDay } = header;
  // Preserve loaded placements and their QA until a new build is requested.
  // A bare weekday may still exist but refer to a different relative week
  // after changing the start day, so compare both label and day identity.
  const loadedStartDay = _currentResultData?.slots?.[0]?.day;
  if ((_currentResultData?.schedule || []).some(entry => selectedCourses.includes(entry.course_code) && entry.day !== 'OVERFLOW' && (
    !days.includes(entry.day)
    || !periods.includes(entry.period)
    || examDayIdentity(entry.day, loadedStartDay) !== examDayIdentity(entry.day)
  ))) return inputError(T.pinSettingsChanged, $('loadCoursesBtn'));
  const lockProblem = lockHeaderProblem(header);
  if (lockProblem) return refuseLockedEdit(lockProblem);
  const pinned = validatedPinPayload(header);
  if (!pinned) return null;
  const linkedExams = validatedLinkPayload();
  if (!linkedExams) return null;
  const thinThreshold = readThinThresholdForPost();
  if (thinThreshold === null) return null;
  // Check and Save carry the page's locks whenever there are any, or were
  // ([] unlocks them all); otherwise nothing is sent, as before locks existed.
  const sendsLocks = ['check_draft', 'save_loaded_changes'].includes(mode) && (_examLocks.length > 0 || savedLocks().length > 0);
  const baseSchedule = loadedBaseSchedule(selectedCourses);
  if (!baseSchedule) {
    $('etStatus').textContent = T.loadedRunAction;
    $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    return null;
  }
  return {
    label,
    days,
    periods,
    max_per_day: maxPerDay,
    programs: getCheckedValues('progList'),
    sections: getCheckedValues('secList'),
    selected_courses: selectedCourses,
    selected_course_entries: getCheckedCourseEntries(),
    pinned,
    // Always sent: the page holds the links, and [] means "none".
    linked_exams: linkedExams,
    ...(sendsLocks ? { exam_locks: lockPayload() } : {}),
    randomize: $('etRandomize').checked,
    thin_conflict_threshold: thinThreshold,
    previous_run_id: _currentResultData?.run_id,
    // A run saved without rooms gets them once a lock is asked for.
    assign_rooms: _roomsForLocks ? true : (_currentResultData?.assign_rooms ?? true),
    update_run_id: mode === 'optimize_loaded' && _currentResultData?.rebuild_mode === 'optimized_from_loaded'
      ? _currentResultData.run_id
      : undefined,
    mode,
    base_schedule: baseSchedule,
  };
}

/* ── Background jobs: an action runs on the server and reports its stages ── */
// Build, Optimize, Fix and Save used to be one request that said nothing for a
// minute or two. With jobs on, the server answers 202 and a job to follow; any
// other answer is the action's own final answer, exactly as before - which is
// also what the page gets when jobs are off.
//
// The panel follows one job at a time. Every follow has a sequence number, and
// anything that arrives for an older follow - a poll still in flight when the
// registrar starts a new action - is dropped rather than painted over it.
//
// Once a timetable is loaded, the builder's status line is inside a closed
// section, so the panel is the one place an action's outcome is shown and
// announced; the status line is cleared rather than repeating it.

// Tests shorten the waits; the page itself never sets this.
const JOB_POLL = {
  // Until the panel is shown the page asks often, so a short job is known to
  // have ended before anything is shown; after that, every second and a half.
  first: 300, quick: 300, steady: 1500, slow: 3000, slowAfter: 30000,
  // A job still running after this is shown; one that ends sooner never is.
  // With no answer at all, it is shown after `stall` regardless.
  reveal: 1000, stall: 3000,
  // Once shown, the panel stays this long before the result replaces it, so a
  // job ending just after it appeared does not jump the page twice.
  minShown: 1500,
  announce: 1000,
  backoff: [2000, 4000, 8000, 15000],
  resultRetries: 3,
  // A poll unanswered this long counts as a lost connection.
  timeout: 20000,
  // A Check turned away because the solver is busy tries again after this,
  // unless the server said how long to wait.
  checkRetry: 5000,
  ...(window.__examJobPoll || {}),
};

// Isolates, so a Latin name or a clock time keeps its order inside Arabic text.
const isolate = text => `⁨${text}⁩`;
const isolateLtr = text => `⁦${text}⁩`;
function clockTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return isolateLtr(`${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`);
}
// The day of a job's time, when it is not the day the server says it is now:
// an ending kept for this page can be shown the next morning, and "13:00"
// alone would read as today.
function jobDay(iso, nowIso) {
  const date = new Date(iso);
  const now = nowIso ? new Date(nowIso) : new Date();
  if (Number.isNaN(date.getTime()) || Number.isNaN(now.getTime())) return '';
  if (date.toDateString() === now.toDateString()) return '';
  return new Intl.DateTimeFormat(IS_AR ? 'ar-u-nu-latn' : 'en-GB', { day: 'numeric', month: 'short' }).format(date);
}
function duration(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

// The button says "Stop without saving", so a stop the registrar asked for is
// "stopped"; a crash is "failed"; a job the page can no longer see is "lost";
// one the action itself turned down - inputs changed, a Build that cannot fit -
// is "refused", and says nothing was made. "Waiting" is a job not started yet.
const JOB_KIND = {
  build: IS_AR
    ? { waiting: 'بناء الجدول في الانتظار', running: 'جارٍ بناء الجدول', finishing: 'انتهى البناء، جارٍ تحميل النتيجة', done: 'تم بناء الجدول', refused: 'لم يُبنَ جدول', failed: 'تعذّر بناء الجدول', cancelled: 'أُوقف بناء الجدول', lost: 'تعذّرت متابعة بناء الجدول' }
    : { waiting: 'Waiting to build the timetable', running: 'Building the timetable', finishing: 'Build finished, loading the result', done: 'Timetable built', refused: 'No timetable was built', failed: 'Build failed', cancelled: 'Build stopped', lost: 'Lost track of the build' },
  optimize_loaded: IS_AR
    ? { waiting: 'تحسين الجدول في الانتظار', running: 'جارٍ تحسين الجدول الحالي', finishing: 'انتهى تحسين الجدول، جارٍ تحميل النتيجة', done: 'اكتمل تحسين الجدول', refused: 'لم يُحسَّن الجدول', failed: 'تعذّر تحسين الجدول', cancelled: 'أُوقف تحسين الجدول', lost: 'تعذّرت متابعة تحسين الجدول' }
    : { waiting: 'Waiting to optimize the timetable', running: 'Optimizing the current timetable', finishing: 'Optimization finished, loading the result', done: 'Optimization finished', refused: 'The timetable was not optimized', failed: 'Optimization failed', cancelled: 'Optimization stopped', lost: 'Lost track of the optimization' },
  // "Finished", not "fixed": a repair can leave rule breaks it could not clear.
  minimum_change_repair: IS_AR
    ? { waiting: 'الإصلاح بأقل تغيير في الانتظار', running: 'جارٍ الإصلاح بأقل تغيير', finishing: 'انتهى الإصلاح، جارٍ تحميل النتيجة', done: 'اكتمل الإصلاح', refused: 'لم يُطبَّق الإصلاح', failed: 'تعذّر الإصلاح', cancelled: 'أُوقف الإصلاح', lost: 'تعذّرت متابعة الإصلاح' }
    : { waiting: 'Waiting to fix with the fewest moves', running: 'Fixing with the fewest moves', finishing: 'Fix finished, loading the result', done: 'Fix finished', refused: 'The fix was not applied', failed: 'Fix failed', cancelled: 'Fix stopped', lost: 'Lost track of the fix' },
  save_loaded_changes: IS_AR
    ? { waiting: 'حفظ التغييرات في الانتظار', running: 'جارٍ حفظ التغييرات', finishing: 'انتهى الحفظ، جارٍ تحميل النتيجة', done: 'تم حفظ التغييرات', refused: 'لم تُحفظ التغييرات', failed: 'تعذّر الحفظ', cancelled: 'أُوقف الحفظ', lost: 'تعذّرت متابعة الحفظ' }
    : { waiting: 'Waiting to save the changes', running: 'Saving changes', finishing: 'Save finished, loading the result', done: 'Changes saved', refused: 'The changes were not saved', failed: 'Save failed', cancelled: 'Save stopped', lost: 'Lost track of the save' },
  add_courses: IS_AR
    ? { waiting: 'إضافة المقررات في الانتظار', running: 'جارٍ إضافة المقررات إلى الجدول', finishing: 'اكتملت الإضافة، جارٍ تحميل النتيجة', done: 'أُضيفت المقررات', refused: 'لم تُضف أي مقررات', failed: 'تعذّرت إضافة المقررات', cancelled: 'أُوقفت إضافة المقررات', lost: 'تعذّرت متابعة إضافة المقررات' }
    : { waiting: 'Waiting to add courses', running: 'Adding courses to the timetable', finishing: 'Courses added, loading the result', done: 'Courses added', refused: 'No courses were added', failed: 'Adding courses failed', cancelled: 'Adding courses stopped', lost: 'Lost track of adding courses' },
};
const jobTitles = kind => JOB_KIND[kind] || JOB_KIND.build;
const JOB_STAGE = IS_AR ? {
  enrolments: 'قراءة تسجيلات الطلاب',
  conflicts: 'حصر المقررات ذات الطلاب المشتركين',
  place_exams: 'توزيع الاختبارات على الأيام والفترات',
  check_rules: 'التحقق من التعارضات والحد اليومي',
  assign_rooms: 'توزيع القاعات',
  balance_invigilators: 'موازنة أعباء المراقبة',
  save: 'حفظ الجدول',
  read_board: 'قراءة الجدول الحالي',
  fewest_moves: 'تحديد أقل عدد من الاختبارات يلزم نقلها',
} : {
  enrolments: 'Reading student enrollments',
  conflicts: 'Finding courses with shared students',
  place_exams: 'Placing exams in days and periods',
  check_rules: 'Checking clashes and daily limits',
  assign_rooms: 'Assigning rooms',
  balance_invigilators: 'Balancing invigilation duties',
  save: 'Saving the timetable',
  read_board: 'Reading the current timetable',
  fewest_moves: 'Finding the fewest exams to move',
};
const JOB_STATE = IS_AR
  ? { done: 'تمّ', running: 'قيد التنفيذ', pending: 'في الانتظار', skipped: 'لم يلزم', stopped: 'توقّف التنفيذ هنا' }
  : { done: 'done', running: 'in progress', pending: 'not started', skipped: 'not needed', stopped: 'stopped here' };
const JOB_HOLDER = IS_AR ? {
  planner: 'تستخدم عملية تخطيط الجدول الدراسي الخادم الآن',
  exam: 'تستخدم عملية أخرى على جدول الاختبارات الخادم الآن',
  other: 'تستخدم عملية أخرى الخادم الآن',
} : {
  planner: 'A timetable-planner run is using the server',
  exam: 'Another exam timetable action is using the server',
  other: 'Another timetable action is using the server',
};
// An exam job, one run inside its request, or a multistart build: all exam work.
const HOLDER_GROUP = { planner: 'planner', exam_job: 'exam', exam_sync: 'exam', multistart: 'exam' };
const holderText = holder => JOB_HOLDER[HOLDER_GROUP[holder?.kind]] || JOB_HOLDER.other;
const JOB_TEXT = {
  step: (index, count, stage) => IS_AR ? `الخطوة ${index} من ${count}: ${stage}` : `Step ${index} of ${count}: ${stage}`,
  count: current => {
    const { key, done, total } = current;
    if (key === 'place_exams') {
      return IS_AR ? `تم توزيع ${done} من ${arabicCount(total, AR_EXAMS)}` : `${done} of ${total} exam${total === 1 ? '' : 's'} placed`;
    }
    // Rooms are assigned a period at a time, so that is what is counted.
    if (key === 'assign_rooms') return IS_AR ? `القاعات للفترة ${done} من ${total}` : `Rooms for period ${done} of ${total}`;
    // A ceiling, not a target: the search can finish early, so no bar either.
    if (key === 'balance_invigilators') return IS_AR ? `المحاولة ${done} (الحد الأقصى ${total})` : `Attempt ${done} (up to ${total})`;
    return IS_AR ? `${done} من ${total}` : `${done} of ${total}`;
  },
  elapsed: IS_AR ? 'المدة المنقضية' : 'Elapsed',
  took: IS_AR ? 'استغرق' : 'Took',
  queued: (mine, waitingFor) => {
    if (waitingFor?.kind === 'planner') {
      return IS_AR
        ? `${holderText(waitingFor)}، و${mine ? 'ستبدأ عمليتك' : 'ستبدأ هذه العملية'} تلقائياً عند انتهائها.`
        : `${holderText(waitingFor)}. ${mine ? 'Yours starts' : 'It starts'} automatically when that finishes.`;
    }
    return mine
      ? (IS_AR ? 'الخادم مشغول بعملية أخرى على الجدول، وستبدأ عمليتك تلقائياً عند فراغه.' : 'The server is busy with another timetable action. Yours starts automatically when it is free.')
      : (IS_AR ? 'في انتظار الخادم؛ ستبدأ تلقائياً عند فراغه.' : 'Waiting for the server; it starts automatically when the server is free.');
  },
  leave: IS_AR ? 'يمكنك مغادرة الصفحة؛ إن عدت خلال ساعة فستظهر النتيجة هنا.' : 'You can leave this page. If you come back within an hour, the outcome will be shown here.',
  // Unsaved edits exist only in this tab: if the action fails, they are gone.
  keepOpen: IS_AR ? 'أبقِ هذه الصفحة مفتوحة؛ تغييراتك غير المحفوظة موجودة هنا فقط حتى تنتهي العملية.' : 'Keep this page open: your unsaved changes exist only here until it finishes.',
  startedBy: (owner, time) => IS_AR
    ? `بدأت هذه العملية${time ? ` الساعة ${time}` : ''}${owner ? ` بطلب من ${isolate(owner)}` : ''}. لا يمكن البناء أو التحسين أو الإصلاح أو الحفظ حتى تنتهي.`
    : `Started${owner ? ` by ${isolate(owner)}` : ''}${time ? ` at ${time}` : ''}. Build, Optimize, Fix and Save are unavailable until it finishes.`,
  // A job that has not started yet was only asked for.
  // «بطلب من», as every other owner sentence: it agrees with any name.
  requestedBy: (owner, time) => IS_AR
    ? `أُضيفت هذه العملية إلى الانتظار${time ? ` الساعة ${time}` : ''}${owner ? ` بطلب من ${isolate(owner)}` : ''}. لا يمكن البناء أو التحسين أو الإصلاح أو الحفظ حتى تنتهي.`
    : `Requested${owner ? ` by ${isolate(owner)}` : ''}${time ? ` at ${time}` : ''}. Build, Optimize, Fix and Save are unavailable until it finishes.`,
  refusedOther: (action, owner, time, started) => {
    if (!started) {
      return IS_AR
        ? `لم يبدأ «${action}». تنتظر عملية أخرى على الجدول دورها${owner ? ` بطلب من ${isolate(owner)}` : ''}؛ أعد المحاولة بعد انتهائها.`
        : `“${action}” did not start. Another timetable action is waiting its turn${owner ? `, requested by ${isolate(owner)}` : ''}; try again when it finishes.`;
    }
    return IS_AR
      ? `لم يبدأ «${action}». تجري عملية أخرى على الجدول${owner ? ` بطلب من ${isolate(owner)}` : ''}${time ? ` منذ الساعة ${time}` : ''}؛ أعد المحاولة بعد انتهائها.`
      : `“${action}” did not start. Another timetable action is running${owner ? `, started by ${isolate(owner)}` : ''}${time ? ` at ${time}` : ''}; try again when it finishes.`;
  },
  refusedMine: (action, started) => IS_AR
    ? `لم يبدأ «${action}»: ما زالت عملية بدأتها ${started ? 'قيد التنفيذ' : 'تنتظر دورها'}.`
    : `“${action}” did not start: a timetable action you started is ${started ? 'still running' : 'waiting its turn'}.`,
  // Refused while another ran, which has ended since: nothing waits now.
  refusedEnded: action => IS_AR
    ? `لم يبدأ${action ? ` «${action}»` : ''} لأن عملية أخرى كانت قيد التنفيذ، وقد انتهت. أعد المحاولة الآن.`
    : `${action ? `“${action}”` : 'It'} did not start because another action was running; it has finished. Try again now.`,
  cancelling: IS_AR ? 'جارٍ الإيقاف… قد يستغرق ذلك بضع ثوانٍ.' : 'Stopping… this can take a few seconds.',
  cancelFailed: IS_AR ? 'تعذّر الإيقاف. العملية ما زالت قيد التنفيذ.' : 'Could not stop it. It is still running.',
  cancelTooLate: IS_AR ? 'اكتملت العملية قبل إيقافها، وحُفظ الجدول الجديد.' : 'It finished before it could be stopped. The new timetable was saved.',
  cancelTooLateNoRun: IS_AR ? 'اكتملت العملية قبل إيقافها، ولم يُحفظ جدول جديد.' : 'It finished before it could be stopped, without saving a new timetable.',
  cancelled: onScreen => IS_AR
    ? `أُوقفت العملية قبل حفظ أي شيء${onScreen ? '، والجدول المعروض لم يتغيّر' : ''}.`
    : `Stopped before it saved anything.${onScreen ? ' The timetable on screen is unchanged.' : ''}`,
  cancelledBy: (name, onScreen) => IS_AR
    ? `أُوقفت العملية بطلب من ${isolate(name)} قبل حفظ أي شيء${onScreen ? '، والجدول المعروض لم يتغيّر' : ''}.`
    : `Stopped by ${isolate(name)} before it saved anything.${onScreen ? ' The timetable on screen is unchanged.' : ''}`,
  stopOther: {
    // The title is escaped by the dialog; the body is HTML and escapes its own.
    // A job whose owner's account was removed has no name to give.
    title: owner => {
      if (!owner) return IS_AR ? 'إيقاف هذه العملية؟' : 'Stop this action?';
      return IS_AR ? `إيقاف عملية ${isolate(owner)}؟` : `Stop ${isolate(owner)}’s action?`;
    },
    body: (owner, took, started) => {
      const name = escapeAttr(isolate(owner));
      const time = `<bdi dir="ltr">${took}</bdi>`;
      if (IS_AR) {
        return `${started ? `مضى على تنفيذها ${time}.` : `تنتظر منذ ${time} ولم تبدأ بعد.`} لن يُحفظ شيء منها${owner ? `، وسيظهر لـ${name} أنها أُوقفت` : ''}.`;
      }
      return `${started ? `It has run for ${time}.` : `It has waited ${time} and has not started.`} Nothing it has done will be saved${owner ? `, and ${name} will see that it was stopped` : ''}.`;
    },
    confirm: IS_AR ? 'إيقاف العملية' : 'Stop it',
    keep: IS_AR ? 'متابعة التنفيذ' : 'Keep running',
  },
  // `lost`, when given, says where the draft is - before the retry and
  // support advice, so the registrar knows what there is to try again with.
  // The advice names the action: after that sentence, "it" would not.
  failed: (stage, mine, lost = '') => IS_AR
    ? `توقفت العملية عند «${stage}». لم يُحفظ شيء.${lost}${mine ? ' أعد تنفيذ العملية، وإن أخفقت مجدداً فتواصل مع الدعم الفني واذكر المرجع أدناه.' : ''}`
    : `It stopped at “${stage}”. Nothing was saved.${lost}${mine ? ' Try the action again; if it fails again, contact support and quote the reference below.' : ''}`,
  restarted: (mine, lost = '') => IS_AR
    ? `أُعيد تشغيل الخادم أثناء التنفيذ فتوقفت العملية. لم يُحفظ شيء.${lost}${mine ? ' ابدأ العملية من جديد، وإن توقفت مجدداً فتواصل مع الدعم الفني واذكر المرجع أدناه.' : ''}`
    : `The server restarted while this was running, so it stopped. Nothing was saved.${lost}${mine ? ' Start the action again; if the server stops it again, contact support and quote the reference below.' : ''}`,
  timedOut: (mine, lost = '') => IS_AR
    ? `تجاوزت العملية الحد الأقصى لمدة التنفيذ فأوقفها الخادم. لم يُحفظ شيء.${lost}${mine ? ' إن تجاوزت العملية المدة مجدداً فتواصل مع الدعم الفني واذكر المرجع أدناه.' : ''}`
    : `It ran longer than the time limit, so the server stopped it. Nothing was saved.${lost}${mine ? ' If the action runs out of time again, contact support and quote the reference below.' : ''}`,
  neverStarted: (mine, lost = '') => IS_AR
    ? `انتظرت العملية طويلاً دون أن تبدأ فأُوقفت. لم يُحفظ شيء.${lost}${mine ? ' أعد تنفيذ العملية لاحقاً.' : ''}`
    : `It waited too long to start, so it was stopped. Nothing was saved.${lost}${mine ? ' Try the action again later.' : ''}`,
  actionsFree: IS_AR ? ' يمكنك الآن استخدام البناء والتحسين والإصلاح والحفظ.' : ' You can use Build, Optimize, Fix and Save again.',
  reference: id => IS_AR ? `المرجع: ${isolateLtr(String(id).slice(0, 8))}` : `Reference: ${String(id).slice(0, 8)}`,
  reconnecting: IS_AR ? 'انقطع الاتصال بالخادم. قد تكون العملية مستمرة؛ جارٍ إعادة الاتصال…' : 'Lost contact with the server. The action may still be running; reconnecting…',
  signedOut: IS_AR
    ? 'انتهت جلسة تسجيل الدخول. سجّل الدخول من جديد في تبويب جديد؛ العملية مستمرة، وستتابعها هذه الصفحة بعد تسجيل دخولك.'
    : 'Your session expired. Sign in again in a new tab; the action keeps running, and this page picks it up again once you have.',
  signIn: IS_AR ? 'تسجيل الدخول في تبويب جديد' : 'Sign in in a new tab',
  gone: IS_AR ? 'لم تعد هذه الصفحة قادرة على متابعة العملية. إن اكتملت فستجد الجدول في «الجداول المحفوظة».' : 'This page can no longer follow it. If it finished, it is in Saved timetables.',
  noAccess: IS_AR ? 'لم تعد لديك صلاحية متابعة هذه العملية، وقد تكون ما زالت قيد التنفيذ.' : 'You no longer have access to follow this action. It may still be running.',
  savedOwn: IS_AR ? 'حُفظ، وهو معروض أدناه وفي «الجداول المحفوظة».' : 'Saved. It is open below and listed in Saved timetables.',
  savedUnfetched: IS_AR
    ? 'حُفظ الجدول لكن تعذّر عرضه هنا. افتحه الآن، أو لاحقاً من «الجداول المحفوظة».'
    : 'Saved, but it could not be shown here. Open it now, or later from Saved timetables.',
  unfetchedNoRun: IS_AR
    ? 'اكتملت العملية دون حفظ جدول جديد، لكن تعذّر تحميل تقريرها هنا.'
    : 'It finished without saving a new timetable, but its report could not be loaded here.',
  noMoves: IS_AR
    ? 'لم يُنقل أي اختبار، فلم يُحفظ شيء. يوضّح التقرير أسفل شريط أدوات التعديل السبب.'
    : 'No exam was moved, so nothing was saved. The report under the editing toolbar says why.',
  // Optimize found nothing better: the same "finished, saved nothing" ending as a Fix that moved nothing.
  optimiseNoBetter: IS_AR
    ? 'لم يُعثر على جدول أفضل، فلم يُحفظ شيء. تجد التفاصيل في التقرير أسفل شريط أدوات التعديل.'
    : 'No better timetable was found, so nothing was saved. The report under the editing toolbar has the details.',
  // A Fix seen from elsewhere: the report belongs to the page that asked for it.
  movedNothing: IS_AR ? 'لم يُنقل أي اختبار، فلم يُحفظ شيء.' : 'No exam was moved, so nothing was saved.',
  notSaved: reason => {
    // A reason ends the sentence: it gets its full stop unless it has one.
    const end = reason && !/[.!?؟]$/.test(reason.replace(/[\u2066-\u2069]/g, '')) ? '.' : '';
    return IS_AR
      ? `لم يُحفظ جدول جديد${reason ? `: ${reason}${end}` : '.'}`
      : `No new timetable was saved${reason ? `: ${reason}${end}` : '.'}`;
  },
  // Found on returning: the page that asked may be closed, reloaded, left, or
  // still open in another tab. Which, this page cannot know - the draft lives
  // in the page, not the tab - so it says what is true in every case.
  draftGone: IS_AR
    ? ' التغييرات غير المحفوظة موجودة فقط في الصفحة التي أُجريت فيها ما دامت مفتوحة.'
    : ' Unsaved changes exist only on the page they were made on, while it stays open.',
  // What to do about a Save turned down because the changes need checking, by
  // how this page met it: found on returning, followed from another page of
  // the registrar's, or asked for here.
  recheck: {
    away: IS_AR
      ? ' فإن كانت تلك الصفحة ما تزال مفتوحة فافحص التغييرات فيها ثم احفظها، وإلا فافتح الجدول من «الجداول المحفوظة» وأعد إجراء التغييرات.'
      : ' If that page is still open, check the changes there, then save; otherwise open the timetable from Saved timetables and make them again.',
    followed: IS_AR
      ? ' إن كانت الصفحة التي حفظت منها ما تزال مفتوحة فافحص التغييرات فيها ثم احفظها، وإلا فافتح الجدول من «الجداول المحفوظة» وأعد إجراء التغييرات.'
      : ' If the page you saved from is still open, check the changes there, then save; otherwise open the timetable from Saved timetables and make them again.',
    own: IS_AR ? ' افحص التغييرات ثم احفظها.' : ' Check the changes, then save.',
  },
  finishedAway: (time, day = '') => IS_AR
    ? `اكتملت العملية التي بدأتها بينما كانت الصفحة مغلقة، وحُفظ الجدول${day ? ` يوم ${day}` : ''}${time ? ` الساعة ${time}` : ''}.`
    : `The timetable action you started finished while this page was closed; it was saved${day ? ` on ${day}` : ''}${time ? ` at ${time}` : ''}.`,
  finishedSaved: IS_AR ? 'اكتملت العملية التي بدأتها وحُفظ الجدول.' : 'The timetable action you started finished and was saved.',
  finishedNoRun: IS_AR ? 'اكتملت العملية دون حفظ جدول جديد.' : 'It finished without saving a new timetable.',
  savedByOther: owner => IS_AR
    ? `حُفظت النتيجة جدولاً جديداً${owner ? ` بطلب من ${isolate(owner)}` : ''}، ولم تُفتح في هذه الصفحة.`
    : `Saved as a new timetable${owner ? ` by ${isolate(owner)}` : ''}. It is not open on this page.`,
  othersRunning: (owner, started = true) => {
    if (!started) {
      return IS_AR
        ? `تنتظر عملية أخرى على الجدول دورها${owner ? ` بطلب من ${isolate(owner)}` : ''}. انتظر حتى تنتهي ثم أعد المحاولة.`
        : `Another timetable action is waiting its turn${owner ? `, requested by ${isolate(owner)}` : ''}. Wait for it to finish, then try again.`;
    }
    return IS_AR
      ? `تجري الآن عملية أخرى على الجدول${owner ? ` بطلب من ${isolate(owner)}` : ''}. انتظر حتى تنتهي ثم أعد المحاولة.`
      : `Another timetable action is running${owner ? `, started by ${isolate(owner)}` : ''}. Wait for it to finish, then try again.`;
  },
  mineRunning: (started = true) => {
    if (!started) return IS_AR ? 'ما زالت عملية بدأتها تنتظر دورها. انتظر حتى تنتهي.' : 'A timetable action you started is waiting its turn. Wait for it to finish.';
    return IS_AR ? 'ما زالت عملية بدأتها قيد التنفيذ. انتظر حتى تنتهي.' : 'A timetable action you started is still running. Wait for it to finish.';
  },
  // Why a Save was turned down, in the page's words.
  // The check compares everything the timetable is built from: courses,
  // rooms, enrollments and policy - so the reason names the source, not a part.
  inputsChanged: IS_AR ? 'تغيّرت بيانات مصدر الجدول بعد آخر فحص' : "the timetable's source data changed after the last check",
  checkRequired: IS_AR ? 'لم تكن التغييرات قد فُحصت' : 'the changes had not been checked',
  // Listed as the live page lists them - a code may carry its own brackets,
  // "CS111 (2)" - and with the step the live page gives, which belongs to the
  // reason and so stays with it, before where the draft is.
  coursesUnavailable: list => IS_AR
    ? `بعض المقررات المختارة ليس لها تسجيلات في الجداول الدراسية المستوردة${list ? `. المقررات غير المتاحة: ${isolateLtr(list)}` : ''}. حمّل المقررات لمراجعة القائمة الحالية`
    : `some selected courses have no enrollments in the imported student timetables${list ? `. Unavailable courses: ${list}` : ''}. Load Courses to review the current list`,
  // One of the registrar's own, found on returning: said to be theirs, so it
  // is never read as the job the page was just showing.
  yoursWhileAway: (time, day = '') => IS_AR
    ? `بينما كانت الصفحة مغلقة، انتهت العملية التي بدأتها${day ? ` يوم ${day}` : ''}${time ? ` الساعة ${time}` : ''}. `
    : `While this page was closed, the action you started${day ? ` on ${day}` : ''}${time ? ` at ${time}` : ''} ended. `,
  solverBusy: holder => IS_AR ? `${holderText(holder)}. أعد المحاولة عند انتهائها.` : `${holderText(holder)}. Try again when it finishes.`,
  checkWaitLive: holder => IS_AR ? `${holderText(holder)}، وستُفحص تغييراتك عند انتهائها.` : `${holderText(holder)}. Your changes will be checked when it finishes.`,
  checkWaitManual: holder => IS_AR ? `${holderText(holder)}. أعد الفحص عند انتهائها.` : `${holderText(holder)}. Check again when it finishes.`,
  saveWait: holder => IS_AR ? `${holderText(holder)}. أعد الحفظ عند انتهائها.` : `${holderText(holder)}. Save again when it finishes.`,
  notStarted: IS_AR ? 'تعذّر على الخادم بدء هذه العملية. لم يُحفظ شيء؛ أعد المحاولة.' : 'The server could not start this action. Nothing was saved; try again.',
  jobNotFound: IS_AR ? 'لم تعد هذه العملية متاحة.' : 'That timetable action is no longer available.',
  runDeleted: IS_AR ? 'حُذف هذا الجدول من «الجداول المحفوظة».' : 'That timetable was deleted from Saved timetables.',
  // Said where any load failed - beside the timetable on the board, too - so it
  // names the one that was asked for.
  openedGone: IS_AR ? 'الجدول الذي حاولت فتحه حُذف من «الجداول المحفوظة».' : 'The timetable you tried to open has been deleted from Saved timetables.',
  savedGone: IS_AR ? 'الجدول الذي حفظته هذه العملية حُذف من «الجداول المحفوظة».' : 'The timetable this action saved has since been deleted from Saved timetables.',
  noResult: IS_AR ? 'لا توجد نتيجة لهذه العملية.' : 'That timetable action has no result to open.',
};

let _jobFollowSeq = 0;
let _jobFollow = null;       // the follow the panel belongs to
let _jobTicker = null;
let _jobAnnounceTimer = null;

function jobHeaders() {
  return { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() || CSRF };
}

// One request and its body, bounded as a whole: a connection that goes quiet
// after the headers - a result is about a megabyte - would otherwise stall the
// follow with the builder locked. Resolves to { res, data } or { res, error }
// when the body is not the JSON expected; rejects when nothing usable came back
// in time, which counts as a lost connection.
async function jobRequest(url, options = {}) {
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller?.abort();
      reject(new TypeError('The server did not answer in time.'));
    }, JOB_POLL.timeout);
  });
  const exchange = (async () => {
    const res = await fetch(url, controller ? { ...options, signal: controller.signal } : options);
    try {
      return { res, data: await readExamResponse(res) };
    } catch (error) {
      return { res, error };
    }
  })();
  try {
    return await Promise.race([exchange, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

function jobError(kind, message) {
  const error = new Error(message);
  error.examJobKind = kind;
  return error;
}

const jobPause = ms => new Promise(resolve => setTimeout(resolve, ms));
const followIsCurrent = follow => Boolean(follow) && follow === _jobFollow && follow.seq === _jobFollowSeq;
const jobIsOver = job => ['succeeded', 'failed', 'cancelled'].includes(job?.status);

// Someone else's job - or one of the registrar's own from another tab - holds
// the one lane: Build, Optimize, Fix and Save would only be refused.
function jobLaneHeldByOther() {
  return Boolean(_jobFollow && !_jobFollow.own && !_jobFollow.outcome);
}

function jobLaneChanged() {
  updateLoadedRunActions();
  // A Check turned away while the job held the solver can run now.
  if (_checkState === 'waiting' && _checkWaitLive && !jobLaneHeldByOther()) scheduleDraftCheck({ delay: 0 });
}

function startFollow(job, { own = false, kind = job.kind, origin = null } = {}) {
  clearInterval(_jobTicker);
  clearTimeout(_jobAnnounceTimer);
  if (_jobFollow) clearTimeout(_jobFollow.revealTimer);
  _jobFollowSeq += 1;
  const now = Date.parse(job.now) || Date.now();
  _jobFollow = {
    seq: _jobFollowSeq,
    id: job.id,
    kind,
    own,
    origin,
    mine: own || Boolean(job.mine),
    canCancel: own || Boolean(job.can_cancel),
    owner: job.owner || '',
    job,
    // Elapsed time is measured on the server's clock, not this computer's.
    clockOffset: now - Date.now(),
    startedAt: Date.now(),
    revealedAt: 0,
    stageKey: null,
    revealed: false,
    cancelling: false,
    cancelAnswer: null,
    reconnecting: false,
    signedOut: false,
    refusal: null,
    // Unsaved edits went with the action and exist only in this tab.
    draftAtRisk: false,
    offer: false,
    outcome: null,
    message: '',
    pendingAnnounce: null,
  };
  $('examJobLive').textContent = '';
  clearJobNotice();
  if (!own) jobLaneChanged();
  return _jobFollow;
}

function revealJobPanel(follow, { focus = false } = {}) {
  if (!followIsCurrent(follow) || follow.revealed) return;
  clearTimeout(follow.revealTimer);
  follow.revealed = true;
  follow.revealedAt = Date.now();
  const panel = $('examJobPanel');
  panel.hidden = false;
  paintJobPanel(follow);
  if (!follow.outcome) {
    clearInterval(_jobTicker);
    _jobTicker = setInterval(() => paintJobClock(follow), 1000);
    if (!follow.own) {
      const title = `${jobHeadline(follow)}.`;
      announceJob(follow, follow.mine ? title : `${title} ${ownerNote(follow)}`);
    }
  }
  if (focus) focusJobPanel();
}

// A job not yet started is waiting, not running: its title, its note and its
// clock all say so.
const jobHasStarted = job => Boolean(job.started_at);
function jobHeadline(follow) {
  const titles = jobTitles(follow.kind);
  if (follow.outcome) return titles[follow.outcome] || titles.done;
  if (jobIsOver(follow.job)) return titles.finishing;
  return jobIsWaiting(follow) ? titles.waiting : titles.running;
}
// The page's own job reads 'queued' until its thread takes it; that is a wait
// only once the server says what for. Someone else's queued job has not started.
const jobIsWaiting = follow => follow.job.status === 'queued' && (!follow.own || Boolean(follow.job.waiting_for));
function ownerNote(follow) {
  const job = follow.job;
  return jobHasStarted(job)
    ? JOB_TEXT.startedBy(follow.owner, clockTime(job.started_at))
    : JOB_TEXT.requestedBy(follow.owner, clockTime(job.submitted_at));
}

// The button that started an action is inert while it runs, and focus would
// fall to the page body; the panel is where the action is.
function focusJobPanel() {
  $('examJobTitle').focus({ preventScroll: true });
  $('examJobPanel').scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function paintJobClock(follow) {
  if (!followIsCurrent(follow) || !follow.revealed) return;
  const job = follow.job;
  // The time it ran, never the time it waited for its turn.
  const started = Date.parse(job.started_at);
  const clock = $('examJobClock');
  const finished = Date.parse(job.finished_at);
  const ended = Boolean(follow.outcome) || jobIsOver(job);
  if (Number.isNaN(started) || (ended && Number.isNaN(finished))) {
    // Not started yet, or an ending the page never saw: no length to state.
    clock.textContent = '';
    return;
  }
  if (ended) {
    clock.innerHTML = `${escapeAttr(JOB_TEXT.took)} <bdi dir="ltr">${duration((finished - started) / 1000)}</bdi>`;
    return;
  }
  clock.innerHTML = `<span class="visually-hidden">${escapeAttr(JOB_TEXT.elapsed)} </span>`
    + `<bdi dir="ltr">${duration((Date.now() + follow.clockOffset - started) / 1000)}</bdi>`;
}

// While the live region cannot be heard - a dialog set aria-hidden on <main>,
// the fullscreen matrix or a menu made the rest of the page inert, a native
// modal is open - what it would say waits, and is said once it can be heard,
// if it is still true then.
function pageHiddenFromAssistiveTech() {
  for (let node = $('examJobLive'); node; node = node.parentElement) {
    if (node.inert || node.hasAttribute('inert') || node.getAttribute('aria-hidden') === 'true') return true;
  }
  // dlg.js un-hides <main> as it starts to close, and gives focus back only as
  // it removes its backdrop: until then the page is not back.
  return Boolean($('examMoveDialog')?.open) || Boolean($('examDepartmentDialog')?.open)
    || Boolean($('examStudentExportDialog')?.open) || Boolean(document.querySelector('.dlg-backdrop'));
}

// `stillTrue`: asked again before a deferred announcement is made - a lost
// connection that came back, or a stage long passed, is not news any more.
function announceJob(follow, text, stillTrue = () => true) {
  if (!followIsCurrent(follow) || !text) return;
  clearTimeout(_jobAnnounceTimer);
  if (pageHiddenFromAssistiveTech()) {
    follow.pendingAnnounce = { text, stillTrue };
    return;
  }
  follow.pendingAnnounce = null;
  $('examJobLive').textContent = text;
}

function pageExposedAgain() {
  if (pageHiddenFromAssistiveTech()) return;
  // A Saved-timetables reload a job ending owed while a dialog was open.
  if (_historyReloadOwed) {
    _historyReloadOwed = false;
    loadHistory();
  }
  const follow = _jobFollow;
  const pending = follow?.pendingAnnounce;
  if (!pending) return;
  follow.pendingAnnounce = null;
  if (!pending.stillTrue()) return;
  // Cleared first, so the same words are heard again.
  $('examJobLive').textContent = '';
  setTimeout(() => { if (followIsCurrent(follow)) $('examJobLive').textContent = pending.text; }, 0);
}

{
  const exposure = new MutationObserver(pageExposedAgain);
  exposure.observe(document.body, { attributes: true, subtree: true, attributeFilter: ['aria-hidden', 'inert', 'open'] });
  // A dlg.js backdrop is a child of <body>: its removal is when focus is back.
  // Its own observer: observing the same node again would replace the options
  // above, not add to them.
  new MutationObserver(pageExposedAgain).observe(document.body, { childList: true });
}

// Saved timetables is reloaded when a job ends, at a moment nobody chose. With
// a dialog open, a re-render would detach the button the dialog gives focus
// back to; the reload waits until the page is back.
let _historyReloadOwed = false;
function reloadHistoryForJob() {
  if (pageHiddenFromAssistiveTech()) _historyReloadOwed = true;
  else loadHistory();
}

function refusalText(follow) {
  const { action, mine } = follow.refusal;
  const job = follow.job;
  return mine
    ? JOB_TEXT.refusedMine(action, jobHasStarted(job))
    : JOB_TEXT.refusedOther(action, follow.owner, clockTime(job.started_at), jobHasStarted(job));
}

// The stored answer of an action that saved nothing, as a reason in the page's
// language: the buckets that did not fit, a known refusal, or the server's own
// words - isolated, so they keep their order inside Arabic.
function jobRefusalReason(data) {
  if (data?.feasibility_error && Array.isArray(data.violations)) {
    return `${T.infeasible} ${infeasibleItems(data.violations).join('; ')}`;
  }
  const code = data?.code || data?.error_code;
  if (code === 'inputs_changed') return JOB_TEXT.inputsChanged;
  if (code === 'check_required') return JOB_TEXT.checkRequired;
  // Said as a reason, not as the live page's advice: the draft it would speak
  // of may be gone with the page that submitted it. (A job's result carries
  // these codes - execute_exam_action - a lock refusal, a feasibility report,
  // or an error in words.)
  if (code === 'courses_unavailable') {
    return JOB_TEXT.coursesUnavailable(Array.isArray(data.unavailable_courses) ? data.unavailable_courses.map(String).join(', ') : '');
  }
  // A lock refusal in the page's language, naming the cell and courses the
  // answer names - never the page's own locks, which may have changed since.
  if (typeof code === 'string' && code.startsWith('exam_locks')) return examLocksRefusal(data).message;
  // An Add refused while the page was closed: in the page's words, naming its
  // courses - the server's own words are English only.
  if (typeof code === 'string' && code.startsWith('add_courses')) return addCoursesRefusal(data).message;
  if (typeof data?.error === 'string' && data.error) return IS_AR ? isolate(data.error) : data.error;
  return '';
}

// Write only what changed: a screen reader's place in the list, and the title
// that has focus, must not be reset every second and a half.
function setJobText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

function paintJobPanel(follow) {
  if (!followIsCurrent(follow) || !follow.revealed) return;
  const job = follow.job;
  const outcome = follow.outcome;
  // Known to be over - its result still on its way - it is no longer running.
  const over = Boolean(outcome) || jobIsOver(job);
  const panel = $('examJobPanel');
  panel.classList.toggle('is-failed', outcome === 'failed');
  panel.classList.toggle('is-cancelled', outcome === 'cancelled');
  panel.classList.toggle('is-lost', outcome === 'lost');
  panel.classList.toggle('is-refused', outcome === 'refused');
  setJobText($('examJobTitle'), jobHeadline(follow));

  const stages = Array.isArray(job.stages) ? job.stages : [];
  const states = stages.map(stage => (JOB_STATE[stage.state] ? stage.state : 'pending'));
  const list = $('examJobStages');
  const signature = `${over}|${stages.map((stage, index) => `${stage.key}:${states[index]}`).join(',')}`;
  if (list.dataset.signature !== signature) {
    list.dataset.signature = signature;
    list.innerHTML = stages.map((stage, index) => {
      const state = states[index];
      const label = escapeAttr(JOB_STAGE[stage.key] || stage.key);
      // A job that has ended is on no step, whatever its last report said.
      const current = state === 'running' && !over ? ' aria-current="step"' : '';
      return `<li class="et-job-stage is-${state}"${current}>${label}`
        + `<span class="visually-hidden"> (${escapeAttr(JOB_STATE[state])})</span></li>`;
    }).join('');
  }

  const current = job.current && typeof job.current === 'object' ? job.current : null;
  const counted = !outcome && job.status === 'running' && current
    && Number.isFinite(current.done) && Number.isFinite(current.total) && current.total > 0;
  const bar = $('examJobBar');
  if (counted && current.key !== 'balance_invigilators') {
    // A bar only for work the server can count exactly.
    const fraction = Math.max(0, Math.min(1, current.done / current.total));
    if (bar.dataset.stage !== current.key) {
      // A new stage starts from empty; it must not animate back from full.
      const fill = bar.firstElementChild;
      fill.style.transition = 'none';
      bar.style.setProperty('--et-job-fraction', '0');
      void fill.offsetWidth;
      fill.style.transition = '';
      bar.dataset.stage = current.key;
    }
    bar.hidden = false;
    bar.style.setProperty('--et-job-fraction', String(fraction));
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-label', JOB_STAGE[current.key] || current.key);
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    bar.setAttribute('aria-valuenow', String(Math.round(fraction * 100)));
    bar.setAttribute('aria-valuetext', JOB_TEXT.count(current));
  } else {
    bar.hidden = true;
    delete bar.dataset.stage;
    for (const name of ['role', 'aria-label', 'aria-valuemin', 'aria-valuemax', 'aria-valuenow', 'aria-valuetext']) bar.removeAttribute(name);
  }

  let detail = '';
  if (outcome) detail = follow.message;
  else if (follow.signedOut) detail = JOB_TEXT.signedOut;
  else if (follow.cancelling) detail = JOB_TEXT.cancelling;
  else if (follow.reconnecting) detail = JOB_TEXT.reconnecting;
  else if (jobIsWaiting(follow)) detail = JOB_TEXT.queued(follow.mine, job.waiting_for);
  else if (counted) detail = JOB_TEXT.count(current);
  setJobText($('examJobDetail'), detail);

  let note = '';
  const tooLate = ['too_late', 'too_late_no_run'].includes(follow.cancelAnswer) && (!outcome || outcome === 'done' || outcome === 'refused');
  // Over, a job is not running for anyone: no refusal, owner line or promise
  // about leaving the page belongs to it.
  if (!over && follow.refusal) note = refusalText(follow);
  else if (follow.cancelAnswer === 'refused' && !over) note = JOB_TEXT.cancelFailed;
  else if (tooLate) note = follow.cancelAnswer === 'too_late' ? JOB_TEXT.cancelTooLate : JOB_TEXT.cancelTooLateNoRun;
  else if (!over && !follow.mine) note = ownerNote(follow);
  else if (!over && follow.own) note = follow.draftAtRisk ? JOB_TEXT.keepOpen : JOB_TEXT.leave;
  else if ((outcome === 'failed' || outcome === 'lost') && follow.id) note = JOB_TEXT.reference(follow.id);
  paintJobNote(follow, note);

  const cancel = $('examJobCancel');
  // Signed out, a Stop would only be refused; over, there is nothing to stop.
  const hideCancel = over || !follow.canCancel || follow.signedOut;
  // A browser drops the keyboard to the page body the moment a focused button
  // is hidden: it moves to the title first, still in the panel.
  if (hideCancel && !cancel.hidden && document.activeElement === cancel) $('examJobTitle').focus({ preventScroll: true });
  cancel.hidden = hideCancel;
  cancel.setAttribute('aria-disabled', String(follow.cancelling));
  $('examJobOpen').hidden = !follow.offer;
  $('examJobClose').hidden = !outcome;
  paintJobClock(follow);

  // Say a new stage once, after a moment, and only the latest; never every
  // tick of a count, and for someone else's job, only its start and end.
  const key = current?.key || null;
  if (!over && follow.mine && key && key !== follow.stageKey) {
    follow.stageKey = key;
    const index = stages.findIndex(stage => stage.key === key);
    clearTimeout(_jobAnnounceTimer);
    _jobAnnounceTimer = setTimeout(() => {
      if (!followIsCurrent(follow) || follow.outcome) return;
      announceJob(follow, JOB_TEXT.step(index + 1, stages.length, JOB_STAGE[key] || key),
        () => follow.stageKey === key && !follow.outcome && !jobIsOver(follow.job));
    }, JOB_POLL.announce);
  }
}

// Signed out, the note is the way back in: a link, kept while the state lasts
// rather than rebuilt on every poll.
function paintJobNote(follow, text) {
  const note = $('examJobNote');
  if (!follow.outcome && follow.signedOut) {
    if (note.dataset.kind === 'sign-in') return;
    note.dataset.kind = 'sign-in';
    const link = document.createElement('a');
    const destination = new URL('/login/', window.location.origin);
    destination.searchParams.set('next', window.location.pathname + window.location.search);
    link.href = destination.href;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = JOB_TEXT.signIn;
    note.replaceChildren(link);
    return;
  }
  if (note.dataset.kind === 'sign-in') {
    const hadFocus = note.contains(document.activeElement);
    delete note.dataset.kind;
    note.textContent = '';
    if (hadFocus) $('examJobTitle').focus({ preventScroll: true });
  }
  setJobText(note, text);
}

const jobEndingText = follow => [jobHeadline(follow), follow.message].filter(Boolean).join('. ');

// End a follow: 'done', 'refused', 'failed', 'cancelled' or 'lost'. The panel
// stays with what happened, announced once, until it is closed or replaced.
// `quiet`: the caller shows the result itself, so a panel never shown stays so.
function finishFollow(follow, job, outcome, message, { quiet = false } = {}) {
  if (!followIsCurrent(follow)) return;
  if (job) follow.job = job;
  follow.outcome = outcome;
  follow.message = message || '';
  follow.cancelling = false;
  follow.reconnecting = false;
  follow.signedOut = false;
  if (quiet && !follow.revealed) {
    hideJobPanel(follow);
    return;
  }
  clearInterval(_jobTicker);
  clearTimeout(_jobAnnounceTimer);
  const panel = $('examJobPanel');
  const focusInPanel = panel.contains(document.activeElement);
  const focusLost = !document.activeElement || document.activeElement === document.body;
  const wasShown = follow.revealed;
  revealJobPanel(follow);
  paintJobPanel(follow);
  announceJob(follow, jobEndingText(follow));
  // After the registrar's own result, the caller takes focus to it.
  const callerMovesFocus = follow.own && outcome === 'done' && !follow.offer;
  if (!callerMovesFocus && (focusInPanel || (focusLost && follow.own))) $('examJobTitle').focus({ preventScroll: true });
  if (!wasShown && follow.own) panel.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  // Shown now, so not again when the page is next opened.
  if (follow.mine && !_jobsEndedHere.has(follow.id)) _jobsEndedHere.set(follow.id, follow.own ? 'own' : follow.metAway ? 'away' : 'followed');
  if (follow.mine && (outcome === 'failed' || outcome === 'cancelled')) markJobSeen(follow.id);
  jobLaneChanged();
}

function hideJobPanel(follow = _jobFollow) {
  if (follow && !followIsCurrent(follow)) return;
  clearInterval(_jobTicker);
  clearTimeout(_jobAnnounceTimer);
  if (follow) clearTimeout(follow.revealTimer);
  $('examJobPanel').hidden = true;
  _jobFollow = null;
  _jobFollowSeq += 1;
  jobLaneChanged();
}

// Wait, but never while the tab is hidden: a hidden tab polls nothing, and the
// moment it is shown again it polls at once.
function jobWait(ms) {
  return new Promise(resolve => {
    if (!document.hidden) {
      setTimeout(resolve, ms);
      return;
    }
    const onVisible = () => {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', onVisible);
      resolve();
    };
    document.addEventListener('visibilitychange', onVisible);
  });
}

// Resolves to the job's final state, or to null if the panel moved on to
// another job. Throws only when this page can no longer follow it.
async function pollExamJob(follow) {
  let delay = JOB_POLL.first;
  let failures = 0;
  for (;;) {
    await jobWait(delay);
    if (!followIsCurrent(follow)) return null;
    let job;
    try {
      const { res, data, error } = await jobRequest(`/ops/exam-timetable/jobs/${follow.id}/`, { headers: jobHeaders() });
      if (res.status === 404) throw jobError('lost', JOB_TEXT.gone);
      if (res.status === 429) {
        // Polls are not throttled; if something is, wait as long as it asks.
        delay = (Number(res.headers?.get?.('Retry-After')) || 0) * 1000 || JOB_POLL.backoff.at(-1);
        continue;
      }
      if (error) throw error;
      // Access withdrawn, or a request the server will never accept: asking
      // again changes nothing, and "reconnecting" would be a lie.
      if (res.status === 403) throw jobError('lost', JOB_TEXT.noAccess);
      if (res.status >= 400 && res.status < 500 && res.status !== 408) throw jobError('lost', JOB_TEXT.gone);
      if (!res.ok || !data.ok) throw examResponseError(data);
      job = data.job;
    } catch (err) {
      if (!followIsCurrent(follow)) return null;
      if (err.examJobKind) throw err;
      if (err.examRequestKind === 'auth') {
        // Signed out, but the job goes on. Keep asking, slowly: signing in
        // again in another tab shares the cookie, and the next answer resumes.
        // Said once, in the panel, with the way back in - never "retry", which
        // would run a job that is still running a second time.
        if (!follow.signedOut) {
          markSignedOut(follow);
          if (!follow.revealed) revealJobPanel(follow, { focus: follow.own });
        }
        delay = JOB_POLL.slow;
        continue;
      }
      // Anything else is the network or a passing server error: the job may
      // well still be running, so it is never reported as failed for that.
      failures += 1;
      if (failures >= 2 && !follow.reconnecting) {
        follow.reconnecting = true;
        if (!follow.revealed) revealJobPanel(follow, { focus: follow.own });
        paintJobPanel(follow);
        announceJob(follow, JOB_TEXT.reconnecting, () => follow.reconnecting);
      }
      delay = JOB_POLL.backoff[Math.min(failures, JOB_POLL.backoff.length) - 1];
      continue;
    }
    if (!followIsCurrent(follow)) return null;
    failures = 0;
    follow.reconnecting = false;
    follow.signedOut = false;
    follow.job = job;
    follow.owner = job.owner || follow.owner;
    follow.canCancel = follow.own || Boolean(job.can_cancel);
    // Someone has asked it to stop: no second Stop, here or on another page,
    // and no "could not stop it" beside "stopping".
    if (job.stopping) {
      follow.cancelling = true;
      if (follow.cancelAnswer === 'refused') follow.cancelAnswer = null;
    }
    const over = jobIsOver(job);
    if (over) {
      // Ended: a stall reveal now would show a finished job as running, the
      // clock stops at its end, and no stage is announced after it.
      clearTimeout(follow.revealTimer);
      clearInterval(_jobTicker);
      clearTimeout(_jobAnnounceTimer);
    }
    // Shown only once it has been seen running past the threshold, so a short
    // job is known to have ended before anything is shown.
    if (!over && !follow.revealed && Date.now() - follow.startedAt >= JOB_POLL.reveal) revealJobPanel(follow, { focus: follow.own });
    paintJobPanel(follow);
    if (over) return job;
    if (jobIsWaiting(follow) && follow.mine && follow.revealed && !follow.queuedSaid) {
      follow.queuedSaid = true;
      announceJob(follow, JOB_TEXT.queued(true, job.waiting_for), () => jobIsWaiting(follow));
    }
    if (!follow.revealed) delay = JOB_POLL.quick;
    else delay = Date.now() - follow.startedAt > JOB_POLL.slowAfter ? JOB_POLL.slow : JOB_POLL.steady;
  }
}

// Signed out, but the job goes on: said once, in the panel, with the way back
// in. Stop is hidden meanwhile; if it had focus, focus stays in the panel.
function markSignedOut(follow) {
  const hadStop = document.activeElement === $('examJobCancel');
  follow.signedOut = true;
  follow.cancelling = false;
  follow.cancelAnswer = null;
  paintJobPanel(follow);
  if (hadStop) $('examJobTitle').focus({ preventScroll: true });
  announceJob(follow, JOB_TEXT.signedOut, () => follow.signedOut);
}

// The finished action's own answer; asked a few times, because the run is
// already saved and a registrar told only "retry" would save it twice.
async function fetchJobResult(jobId) {
  let delay = 0;
  for (let attempt = 0; attempt < JOB_POLL.resultRetries; attempt += 1) {
    if (delay) await jobWait(delay);
    try {
      const { res, data, error } = await jobRequest(`/ops/exam-timetable/jobs/${jobId}/result/`, { headers: jobHeaders() });
      if (!error) return { res, data };
      if (error.examRequestKind === 'auth') return null;
    } catch (_) {
      // Lost or too slow: asked again below.
    }
    delay = JOB_POLL.backoff[Math.min(attempt, JOB_POLL.backoff.length - 1)];
  }
  return null;
}

// `own`: this page submitted it, so "the timetable on screen" is the one it
// was working on. Seen from anywhere else, no board on screen is involved.
function jobEndMessage(job, mine, own = false, lost = '') {
  const onScreen = own && job.kind !== 'build';
  if (job.status === 'cancelled') {
    return (job.cancelled_by ? JOB_TEXT.cancelledBy(job.cancelled_by, onScreen) : JOB_TEXT.cancelled(onScreen)) + lost;
  }
  if (job.error_code === 'server_restarted') return JOB_TEXT.restarted(mine, lost);
  if (job.error_code === 'timed_out') return JOB_TEXT.timedOut(mine, lost);
  if (job.error_code === 'never_started') return JOB_TEXT.neverStarted(mine, lost);
  const stage = JOB_STAGE[job.current?.key] || jobTitles(job.kind).running;
  return JOB_TEXT.failed(stage, mine, lost);
}

const actionLabel = button => (button?.textContent || '').replace(/\s+/g, ' ').trim();

// Submit an action; resolve to { res, data } exactly as the old single request
// did, whether the server ran it at once or as a job. A refusal because a job
// is running also carries `refusal`: whether the panel now shows that job.
async function submitExamAction(payload, kind, origin) {
  const res = await fetch('/ops/exam-timetable/build/', {
    // Only a page that can follow a job asks for one.
    method: 'POST', headers: { ...jobHeaders(), 'X-Exam-Jobs': '1' }, body: JSON.stringify(payload),
  });
  if (res.status !== 202) {
    const data = await readExamResponse(res);
    if (res.status === 409 && data.error_code === 'job_in_progress') {
      // Show that job where the registrar is looking, with why this action
      // did not start.
      const action = actionLabel(origin);
      const shown = await resumeExamJob({ refusal: { action, mine: Boolean(data.active_job?.mine) }, origin });
      return { res, data, refusal: { shown, action } };
    }
    return { res, data };
  }
  const submitted = await readExamResponse(res);
  return followOwnJob(submitted.job, kind, origin);
}

async function followOwnJob(job, kind, origin) {
  // An ended panel still on screen goes: its buttons must never act on this
  // job, and a short action must not be shown just because one was up already.
  if (_jobFollow?.outcome) hideJobPanel(_jobFollow);
  // An older outcome still owed is superseded: the server offers only the
  // registrar's latest job, and this one is it.
  _ownEndingOwed = null;
  _ownActionsAccepted += 1;
  const follow = startFollow(job, { own: true, kind, origin });
  follow.draftAtRisk = kind === 'save_loaded_changes' || hasUnsavedEdits();
  follow.revealTimer = setTimeout(() => revealJobPanel(follow, { focus: true }), JOB_POLL.stall);
  let final;
  try {
    final = await pollExamJob(follow);
  } catch (err) {
    finishFollow(follow, null, 'lost', err.message);
    reloadHistoryForJob();
    throw err;
  }
  if (!final) throw jobError('superseded', '');
  if (final.status !== 'succeeded') {
    const outcome = final.status === 'cancelled' ? 'cancelled' : 'failed';
    const message = jobEndMessage(final, true, true);
    finishFollow(follow, final, outcome, message);
    throw jobError(outcome, message);
  }
  const fetched = await fetchJobResult(final.id);
  if (!fetched) {
    if (final.result_run_id) {
      // Saved, but its answer never came: offer it, so nobody saves it twice.
      follow.offer = true;
      finishFollow(follow, final, 'done', JOB_TEXT.savedUnfetched);
      reloadHistoryForJob();
      throw jobError('unfetched', JOB_TEXT.savedUnfetched);
    }
    finishFollow(follow, final, noRunOutcome(final), JOB_TEXT.unfetchedNoRun);
    throw jobError('unfetched', JOB_TEXT.unfetchedNoRun);
  }
  if (follow.revealed) await jobPause(Math.max(0, follow.revealedAt + JOB_POLL.minShown - Date.now()));
  const revealed = follow.revealed;
  const { res, data } = fetched;
  if (res.ok && data.ok && data.run_id) finishFollow(follow, final, 'done', JOB_TEXT.savedOwn, { quiet: true });
  // A Fix with nothing it could move saves nothing; the report says why.
  else if (res.ok && data.ok && data.saved === false && revealed) finishFollow(follow, final, 'done', final.kind === 'optimize_loaded' ? (isPlainObject(data.optimisation) || isPlainObject(data.minimum_change) ? JOB_TEXT.optimiseNoBetter : T.optimizeNoBetter) : JOB_TEXT.noMoves);
  // Any other answer - inputs the action refused - is reported as it always was.
  else hideJobPanel(follow);
  return { ...fetched, revealed };
}

// A success that saved no run: a Fix with nothing to move finished; anything
// else was the action turning the request down.
// Optimize ends the same way when it finds nothing better than the board.
const noRunOutcome = job => (['minimum_change_repair', 'optimize_loaded'].includes(job.kind) && !job.refused ? 'done' : 'refused');

// On page load, on a Check the solver turned away, or refused because a job is
// running: follow the job that holds the lane without taking the builder, or
// show how the registrar's own latest job ended while the page was closed.
// Resolves to 'shown' once the panel shows it, 'ended' if the job it would
// show has ended, or 'unknown' if the server could not say; the job is then
// followed in the background.
async function resumeExamJob({ refusal = null, origin = null, owed = null } = {}) {
  let job;
  let ownEnding = null;
  // Asked about by id, the ending this page was promised is kept past the
  // server's hour: it was news when the page was given it.
  const url = `/ops/exam-timetable/jobs/active/${owed ? `?owed=${encodeURIComponent(owed)}` : ''}`;
  try {
    const { res, data } = await jobRequest(url, { headers: jobHeaders() });
    if (!res.ok || !data) return 'unknown';
    job = data.job;
    ownEnding = data.ending?.mine ? data.ending : null;
  } catch (_) {
    return 'unknown';
  }
  const current = _jobFollow;
  if (current && !current.outcome) {
    // Already following it: say why this action did not start.
    if (refusal && (!job || job.id === current.id)) {
      tellRefusal(current, refusal, origin);
      return 'shown';
    }
    return 'unknown';
  }
  if (!job) return 'ended';
  if (jobIsOver(job)) {
    // Between the refusal and this answer it ended: nothing holds the lane. An
    // ending already on screen is not replaced by an older one.
    // The ending owed, with the panel taken meanwhile: it waits for the panel.
    if (owed && current && String(job.id) === owed) return 'deferred';
    if (refusal || !job.mine || current || _jobsSeenHere.has(String(job.id))) return 'ended';
    await showJobEnding(job, null);
    return 'shown';
  }
  const follow = startFollow(job, { origin });
  if (!job.mine && ownEnding) _ownEndingOwed = String(ownEnding.id);
  revealJobPanel(follow);
  if (refusal) tellRefusal(follow, refusal, origin);
  watchJob(follow);
  return 'shown';
}

function tellRefusal(follow, refusal, origin) {
  if (!followIsCurrent(follow)) return;
  follow.refusal = refusal;
  if (origin) follow.origin = origin;
  if (!follow.revealed) revealJobPanel(follow);
  paintJobPanel(follow);
  focusJobPanel();
  announceJob(follow, refusalText(follow));
}

async function watchJob(follow) {
  let final;
  try {
    final = await pollExamJob(follow);
  } catch (err) {
    finishFollow(follow, null, 'lost', err.message);
    reloadHistoryForJob();
    return;
  }
  if (!final || !followIsCurrent(follow)) return;
  reloadHistoryForJob();
  await showJobEnding(final, follow);
}

// A registrar back while someone else's job ran was promised their own outcome
// on return. Once that job's ending is closed, or its timetable opened - the
// panel free - the server is asked again about it: what it says then is
// current (seen elsewhere meanwhile, or its run deleted, it is not offered).
// An action of their own supersedes it (followOwnJob). An answer that does not
// come is asked for again, a few times; the panel taken meanwhile, it waits.
let _ownEndingOwed = null;      // the id of that job
let _ownActionsAccepted = 0;
async function offerPendingOwnEnding() {
  const owed = _ownEndingOwed;
  if (!owed) return;
  _ownEndingOwed = null;
  const actions = _ownActionsAccepted;
  for (let attempt = 0; ; attempt += 1) {
    const shown = await resumeExamJob({ owed });
    // An action of their own since has superseded it.
    if (actions !== _ownActionsAccepted) return;
    // The panel taken meanwhile, or no answer over and over: still owed, and
    // asked for when the panel is next free.
    const waits = shown === 'deferred' || (shown === 'unknown' && (_jobFollow || attempt >= JOB_POLL.backoff.length));
    if (waits) {
      if (!_ownEndingOwed) _ownEndingOwed = owed;
      return;
    }
    if (shown !== 'unknown') return;
    await jobWait(JOB_POLL.backoff[attempt]);
  }
}

// Jobs this page has marked seen: never offered again, whatever a lost or
// slower acknowledgement leaves the server saying. (Reading a job's answer
// needs no entry: the server acknowledges it before answering.) An ending only
// drawn is not acknowledged: replaced before the registrar acted on it, it is
// offered again once the panel is free.
const _jobsSeenHere = new Set();
// Jobs of the registrar's own this page has shown, by how it first met them:
// 'own' (asked for here), 'followed' (from another page), 'away' (found on
// opening). Shown again, they are not news "from while the page was closed",
// and their next step is the one they first had.
const _jobsEndedHere = new Map();

// How a job this page did not submit ended: followed here, or found on opening.
async function showJobEnding(job, follow) {
  const mine = Boolean(job.mine);
  // Met before on this page, it is not news from while the page was closed.
  const met = _jobsEndedHere.get(job.id);
  if (job.status === 'succeeded' && job.has_run) {
    // A saved timetable: open it here, or leave it in Saved timetables.
    let message = JOB_TEXT.savedByOther(job.owner);
    if (mine) {
      message = follow || met
        ? JOB_TEXT.finishedSaved
        : JOB_TEXT.finishedAway(clockTime(job.finished_at), jobDay(job.finished_at, job.now));
    }
    offerJobResult(job, message, follow);
    return;
  }
  // Found on opening, the registrar's own is said to be theirs - and the draft
  // it took is only on the page that asked for it.
  const away = !follow && mine && !met ? JOB_TEXT.yoursWhileAway(clockTime(job.submitted_at), jobDay(job.submitted_at, job.now)) : '';
  const how = follow ? (follow.own ? 'own' : 'followed') : (met || 'away');
  const lost = mine && how === 'away' && !['build', 'add_courses'].includes(job.kind) ? JOB_TEXT.draftGone : '';
  if (job.status === 'succeeded') {
    // Ended without a new timetable. The registrar's own is told why: its
    // stored answer (fetching it also marks it seen), and what to do next.
    const outcome = noRunOutcome(job);
    let message = outcome === 'done' ? (job.kind === 'optimize_loaded' ? T.optimizeNoBetter : JOB_TEXT.movedNothing) : JOB_TEXT.notSaved('');
    let next = '';
    if (mine) {
      const fetched = await fetchJobResult(job.id);
      if (fetched && !(fetched.res.ok && fetched.data?.ok)) {
        message = JOB_TEXT.notSaved(jobRefusalReason(fetched.data));
        const code = fetched.data?.code || fetched.data?.error_code;
        if (['inputs_changed', 'check_required'].includes(code)) next = JOB_TEXT.recheck[how];
      }
    } else {
      message += JOB_TEXT.actionsFree;
    }
    endShownJob(job, follow, outcome, away + message + lost + next);
    return;
  }
  const outcome = job.status === 'cancelled' ? 'cancelled' : 'failed';
  endShownJob(job, follow, outcome, away + jobEndMessage(job, mine, false, mine ? lost : '') + (mine ? '' : JOB_TEXT.actionsFree));
}

// End the follow it was watched on; one found on opening takes the panel only
// if nothing else has taken it meanwhile.
function endShownJob(job, follow, outcome, message) {
  if (follow) {
    finishFollow(follow, job, outcome, message);
  } else if (!_jobFollow) {
    const found = startFollow(job);
    found.metAway = true;
    finishFollow(found, job, outcome, message);
  }
}

function offerJobResult(job, message, follow = null) {
  // Its timetable already on the board - opened from Saved timetables before
  // this page heard the job had ended: nothing to offer, and "not open on this
  // page" would be untrue. The news has been taken up.
  if (job.result_run_id != null && String(job.result_run_id) === String(_currentRunId)) {
    if (job.mine) markJobSeen(job.id);
    if (follow && followIsCurrent(follow)) {
      const hadFocus = $('examJobPanel').contains(document.activeElement);
      hideJobPanel(follow);
      if (hadFocus) focusPastJobPanel();
      offerPendingOwnEnding();
    }
    return;
  }
  const focusInPanel = $('examJobPanel').contains(document.activeElement);
  const offer = follow && followIsCurrent(follow) ? follow : startFollow(job);
  clearInterval(_jobTicker);
  offer.job = job;
  offer.offer = true;
  offer.outcome = 'done';
  offer.message = message;
  offer.cancelling = false;
  offer.reconnecting = false;
  if (offer.mine && !_jobsEndedHere.has(offer.id)) _jobsEndedHere.set(offer.id, offer.own ? 'own' : follow ? 'followed' : 'away');
  revealJobPanel(offer);
  paintJobPanel(offer);
  announceJob(offer, jobEndingText(offer));
  // Stop, which may have had focus, is gone now.
  if (focusInPanel) $('examJobTitle').focus({ preventScroll: true });
  jobLaneChanged();
}

function markJobSeen(jobId) {
  _jobsSeenHere.add(String(jobId));
  jobRequest(`/ops/exam-timetable/jobs/${jobId}/seen/`, { method: 'POST', headers: jobHeaders() }).catch(() => {});
}

$('examJobCancel')?.addEventListener('click', async () => {
  const follow = _jobFollow;
  if (!follow?.revealed || follow.outcome || follow.cancelling || !follow.canCancel) return;
  if (!follow.mine) {
    // Stopping a colleague's work: say whose, and that they will be told.
    const started = jobHasStarted(follow.job);
    const since = Date.parse(started ? follow.job.started_at : follow.job.submitted_at);
    const confirmed = await dlg.confirm({
      title: JOB_TEXT.stopOther.title(follow.owner),
      body: JOB_TEXT.stopOther.body(follow.owner, duration((Date.now() + follow.clockOffset - since) / 1000), started),
      icon: 'warning',
      confirmLabel: JOB_TEXT.stopOther.confirm,
      cancelLabel: JOB_TEXT.stopOther.keep,
      confirmClass: 'danger',
      // Resolved once focus is back on Stop, so what follows moves it on.
      waitForClose: true,
    });
    if (!followIsCurrent(follow)) {
      // The panel moved on while the dialog was open - its run opened on the
      // board, or an owed ending of mine took it - and the dialog gave focus
      // back to a Stop that is hidden: in a browser, to nothing.
      const at = document.activeElement;
      const lost = !at || at === document.body || ($('examJobPanel').contains(at) && Boolean(at.closest('[hidden]')));
      if (lost) {
        if ($('examJobPanel').hidden) focusPastJobPanel();
        else $('examJobTitle').focus({ preventScroll: true });
      }
      return;
    }
    if (follow.outcome) {
      // It ended while the dialog was open (its ending is said as the page
      // comes back); Stop, where the dialog returns focus, is gone.
      $('examJobTitle').focus({ preventScroll: true });
      return;
    }
    if (!confirmed || follow.cancelling) return;
  }
  follow.cancelling = true;
  follow.cancelAnswer = null;
  paintJobPanel(follow);
  announceJob(follow, JOB_TEXT.cancelling, () => follow.cancelling);
  let answer = 'refused';
  try {
    const { res, data, error } = await jobRequest(`/ops/exam-timetable/jobs/${follow.id}/cancel/`, { method: 'POST', headers: jobHeaders() });
    // Signed out, the request lands on the sign-in page - a 200 that stopped nothing.
    if (error?.examRequestKind === 'auth') answer = 'signed_out';
    else if (res.ok && !error) answer = 'accepted';
    else if (res.status === 409) {
      // It ended first. Only a success that saved something was "saved"; any
      // other ending the next poll reports as it is.
      const ended = data?.job;
      if (ended?.status === 'succeeded') answer = ended.has_run ? 'too_late' : 'too_late_no_run';
      else answer = 'ended';
    }
  } catch (_) {
    answer = 'refused';
  }
  // Accepted: it stays "stopping" until the job itself says it stopped.
  if (answer === 'accepted' || !followIsCurrent(follow) || follow.outcome) return;
  if (answer === 'signed_out') {
    markSignedOut(follow);
    return;
  }
  // A stop sent from elsewhere has reached it meanwhile: it is stopping anyway.
  if (answer === 'refused' && follow.job?.stopping) return;
  follow.cancelling = false;
  follow.cancelAnswer = answer === 'ended' ? null : answer;
  paintJobPanel(follow);
  if (answer === 'refused') announceJob(follow, JOB_TEXT.cancelFailed);
  else if (answer === 'too_late') announceJob(follow, JOB_TEXT.cancelTooLate);
  else if (answer === 'too_late_no_run') announceJob(follow, JOB_TEXT.cancelTooLateNoRun);
});

$('examJobOpen')?.addEventListener('click', async () => {
  const follow = _jobFollow;
  const open = $('examJobOpen');
  // Never the run already on the board: an offer is not drawn for it
  // (offerJobResult), and loading it from anywhere takes the offer up
  // (settleJobPanel).
  if (!follow?.revealed || !follow.offer || !follow.job?.result_run_id || open.getAttribute('aria-disabled') === 'true') return;
  // aria-disabled, not disabled: focus stays on the button while it loads.
  open.setAttribute('aria-disabled', 'true');
  try {
    // loadRun asks before discarding a draft; if the registrar keeps it, the
    // offer stays, and nothing has been marked seen. Opened - or found deleted -
    // loadRun settles the panel.
    await loadRun(follow.job.result_run_id, { addReport: follow.job.kind === 'add_courses', optimiseReport: follow.job.kind === 'optimize_loaded' });
  } finally {
    open.setAttribute('aria-disabled', 'false');
  }
});

// The board shows another timetable now, or none: a panel offering the one on
// the board has been taken up, and one saying the registrar's own result "is
// open below" no longer is true.
function settleJobPanel() {
  const follow = _jobFollow;
  if (!follow?.revealed || !follow.outcome || !followIsCurrent(follow)) return;
  const onBoard = String(follow.job?.result_run_id) === String(_currentRunId);
  const stale = follow.offer ? onBoard : follow.own && follow.outcome === 'done' && follow.job?.result_run_id != null && !onBoard;
  if (!stale) return;
  if (follow.offer && follow.mine) markJobSeen(follow.id);
  hideJobPanel(follow);
  offerPendingOwnEnding();
}

// The timetable a panel names was deleted: nothing is left to open, and the
// panel says so instead of offering it, or calling it open below. A load
// that found it gone has already said so aloud, so that one repaints quietly.
function jobRunGone(runId, { announce = true } = {}) {
  const follow = _jobFollow;
  if (!follow?.revealed || !followIsCurrent(follow) || !(follow.offer || follow.outcome === 'done')) return;
  if (String(follow.job?.result_run_id) !== String(runId)) return;
  const hadOpen = document.activeElement === $('examJobOpen');
  if (follow.offer && follow.mine) markJobSeen(follow.id);
  follow.offer = false;
  follow.message = JOB_TEXT.runDeleted;
  paintJobPanel(follow);
  if (announce) announceJob(follow, follow.message);
  // Open, which had focus, is gone.
  if (hadOpen) $('examJobTitle').focus({ preventScroll: true });
}

// Where the keyboard goes when the panel it was in goes: the button that
// started the job if it can take it, else the board, else the setup.
function focusPastJobPanel(origin = null) {
  const usable = element => element && !element.disabled && !element.closest('[inert], [hidden], .d-none, details:not([open])');
  const fallback = $('etResults').classList.contains('d-none') ? $('examSetupSummary') : $('examScheduleHeading');
  const target = usable(origin) ? origin : fallback;
  if (!target) return;
  if (!target.matches('button, a, input, select, textarea, summary')) target.setAttribute('tabindex', '-1');
  // Scrolled to: the button that started it is usually far below the panel.
  target.focus();
}

$('examJobClose')?.addEventListener('click', () => {
  const follow = _jobFollow;
  if (!follow?.revealed) return;
  if (follow.offer && follow.mine) markJobSeen(follow.id);
  const origin = follow.origin;
  hideJobPanel(follow);
  focusPastJobPanel(origin);
  offerPendingOwnEnding();
});

// A job's outcome, or a refusal because one is running, is news, not an error,
// and it is said once, where the registrar is looking.
function reportJobOutcome(err) {
  const status = $('etStatus');
  const quiet = () => {
    status.textContent = '';
    status.className = 'alert mt-2 py-2 mb-0 d-none';
  };
  // The registrar's own job: the panel shows how it ended, and says it.
  if (err.examJobKind) {
    quiet();
    return;
  }
  // A Save the busy solver turned away: the board's check line says it.
  if (err.examRequestKind === 'solver-busy') {
    status.textContent = err.message;
    status.className = 'alert alert-info mt-2 py-2 mb-0';
    return;
  }
  // Refused because a job holds the lane: the panel says so when it could
  // show that job. When the job had already ended, nothing waits any more.
  const refusal = err.examRefusal;
  if (refusal?.shown === 'shown') {
    quiet();
    return;
  }
  showJobNotice(refusal?.shown === 'ended' ? JOB_TEXT.refusedEnded(refusal.action) : err.message);
}

// Once a timetable is loaded, the builder's status line is inside a closed
// section: news for the registrar goes to the board, and is said aloud there.
function showJobNotice(message) {
  const status = $('etStatus');
  // Before a timetable is loaded, or with the setup section opened, the
  // status line is seen - and heard - where the registrar is.
  if ($('etResults').classList.contains('d-none') || $('examSetupDetails').open) {
    status.textContent = message;
    status.className = 'alert alert-info mt-2 py-2 mb-0';
    status.dataset.jobNotice = message;
    return;
  }
  status.textContent = '';
  status.className = 'alert mt-2 py-2 mb-0 d-none';
  const notice = $('examEditorNotice');
  notice.textContent = message;
  notice.hidden = false;
  $('examJobLive').textContent = message;
}

function clearJobNotice() {
  const notice = $('examEditorNotice');
  if (notice) {
    notice.hidden = true;
    notice.textContent = '';
  }
  // Said in the status line instead, it goes the same way - and only if the
  // line still says it.
  const status = $('etStatus');
  if (status?.dataset.jobNotice !== undefined) {
    const stillSaysIt = status.textContent === status.dataset.jobNotice;
    delete status.dataset.jobNotice;
    // Written over since by something else: that is not the notice's to clear.
    if (stillSaysIt) {
      status.textContent = '';
      status.className = 'alert mt-2 py-2 mb-0 d-none';
    }
  }
}

// ``prepared``: a payload already built and checked (Add courses builds it
// before its list closes, so a refusal keeps the registrar's ticks).
async function runLoadedRunAction(mode, button, busyText, successText, prepared = null) {
  if (_builderBusy) return;
  clearRepairReport();
  clearJobNotice();
  let payload = prepared || collectLoadedRunPayload(mode);
  if (!payload) return;
  cancelDraftChecks();
  let focusAfterAction = false;
  button.disabled = true;
  setBuilderBusy(true);
  $('etStatus').textContent = busyText;
  $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';
  try {
    if (mode === 'save_loaded_changes') {
      if (_evaluatedEditorSignature !== editorSignature() || !_currentResultData?.input_fingerprint) {
        if (!await refreshDraftImpact({ force: true })) throw _checkRequestError || new Error(_checkError || (IS_AR ? 'تحقق من التغييرات قبل الحفظ.' : 'Check changes before saving.'));
      }
      if (!_reviewedInputFingerprint || _currentResultData.input_fingerprint !== _reviewedInputFingerprint) {
        _checkState = 'review';
        throw new Error(lockChangesSinceSave().length ? LOCK_TEXT.reviewBeforeSave : IS_AR ? 'تغيّرت بيانات المصدر أو لم يتم التحقق منها. انقر «تحقق من التغييرات» لمراجعتها قبل الحفظ.' : 'Source data changed or has not been reviewed. Click Check changes to review it before saving.');
      }
      payload = collectLoadedRunPayload(mode);
      if (!payload) return;
      payload.expected_input_fingerprint = _currentResultData.input_fingerprint;
    }
    payload.editor_revision = _editorRevision;
    const { res, data, revealed, refusal } = await submitExamAction(payload, mode, button);
    // The panel moved the view to the top of the page: bring the registrar back
    // to the board, where the result - or the report of why nothing moved - is.
    focusAfterAction = Boolean(revealed);
    if (!res.ok || !data.ok) {
      if (data.error_code === 'inputs_changed') {
        _evaluatedEditorSignature = null;
        _checkState = 'error';
        _checkError = data.error;
      }
      throw Object.assign(examResponseError(data), { examRefusal: refusal });
    }
    clearExamRequestError('save-optimize');
    clearExamCourseSourceError();
    if (data.saved === false) {
      // The repair moved nothing, so the server saved nothing: the board on
      // screen - and any unsaved drags on it - is still the registrar's draft.
      $('etStatus').textContent = mode === 'optimize_loaded' ? T.optimizeNoBetter : T.repairUnchanged;
      $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';
      if (data.minimum_change) showRepairReport(data.minimum_change, { saved: false });
      if (data.optimisation) showOptimisationReport(data.optimisation, { saved: false, panelShown: Boolean(revealed) });
      return;
    }
    hydrateHeaderFromRun({ ...data, label: payload.label });
    renderResults(data);
    _loadedRunForRebuild = true;
    _scheduleHasDraftMoves = false;
    updatePinBar();
    updateLoadedRunActions();
    $('etStatus').textContent = mode === 'add_courses' ? addCoursesStatus(data.add_courses) : successText;
    $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';
    // renderResults collapses the setup section that holds etStatus, so the
    // repair report goes to the editing area where it stays visible.
    if (data.minimum_change) showRepairReport(data.minimum_change);
    if (mode === 'optimize_loaded' && data.optimisation) showOptimisationReport(data.optimisation);
    if (mode === 'add_courses') {
      // Saved: the ticks were used.
      _addSent = null;
      clearExamRequestError('add-courses');
      if (data.add_courses) showAddCoursesReport(data.add_courses);
    }
    loadHistory();
  } catch (err) {
    if (err.examJobKind || err.examInfo) reportJobOutcome(err);
    else {
      $('etStatus').textContent = T.error + ': ' + showExamRequestError(err, 'save-optimize');
      $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
    }
  } finally {
    button.disabled = false;
    setBuilderBusy(false);
    updateLoadedRunActions();
    // The panel moved the view away, so the result is brought back; otherwise
    // the viewport stays, and focus never stays on the body after the button
    // that had it went inert.
    if (focusAfterAction) focusExamEditor();
    else if (document.activeElement === document.body) {
      // Save disables itself once nothing is unsaved; the board keeps focus then.
      if (!button.disabled) button.focus({ preventScroll: true });
      else {
        $('examEditHeading').setAttribute('tabindex', '-1');
        $('examEditHeading').focus({ preventScroll: true });
      }
    }
  }
}

$('saveLoadedBtn')?.addEventListener('click', () => {
  runLoadedRunAction('save_loaded_changes', $('saveLoadedBtn'), T.savingLoaded, T.savedChanges);
});

$('optimizeLoadedBtn')?.addEventListener('click', () => {
  runLoadedRunAction('optimize_loaded', $('optimizeLoadedBtn'), T.optimizing, T.optimized);
});

$('minChangeBtn')?.addEventListener('click', () => {
  runLoadedRunAction('minimum_change_repair', $('minChangeBtn'), T.repairing, T.repaired);
});

let _programCourses = {};   // { programName: Set([course_code, ...]) }

/* ── Fixed exam times: identity is stable even when display suffixes change. ── */
let _pinnedCourses = {};    // course_identity → { course_code, course_identity, course_name, day, period }
let _courseMetadata = {};

/* ── Linked exams: courses examined as one exam, keyed by identity. ── */
// Each link is { members: [{ course_identity, course_code, course_name }] },
// in the order the registrar made them: that order is the request's, so a
// refusal naming linked_exams[i] names the row the registrar sees.
let _linkedExams = [];
let _pendingLinkMembers = [];  // identities chosen in the picker, not linked yet
let _linkError = null;         // the server's refusal, while the links it judged stand
let _linkNotice = null;        // { text, error, key } said after a link action, while `key` holds
let _linkDialog = null;        // { identities, options } while the time is chosen

/* ── Locked days and periods: editor state, saved with the timetable like pins and links. ── */
// Each lock is { day, period?, day_identity }: a whole day, or one period of
// it, by the header's own labels. A lock keeps what the SAVED run holds there -
// exams, rooms, invigilators - so a new or changed lock needs a Check, then a Save.
let _examLocks = [];
// After Load Courses from a saved run with locks, the run a Build keeps them
// from: { runId, run } - the saved run, whose cells and exams the Build keeps.
let _lockSource = null;
// A lock keeps saved rooms: a run saved without rooms gets them on its next
// Check and Save first.
let _roomsForLocks = false;

/* ── Current run ID (for export) ── */
let _currentRunId = null;

/* ── KPI drilldown data ── */
let _drillData = { conflicts: [], overload: [], heavy: [], 'thin-courses': [], 'thin-clash': [] };
let _slotsByIndex = {};
let _currentResultData = null;
let _draftImpactSeq = 0;
let _loadedRunForRebuild = false;
let _scheduleHasDraftMoves = false;
let _builderBusy = false;
let _savedEditorSignature = null;
let _observedEditorSignature = null;
let _evaluatedEditorSignature = null;
let _savedResultData = null;
let _evaluatedReportDirty = false;
let _evaluatedInputsChanged = false;
let _reviewedInputFingerprint = null;
let _editorRevision = 0;
let _repairReportRevision = null;  // the board revision the repair report describes
let _renderingResults = false;
let _checkState = 'checked';
let _checkError = '';
// A 'waiting' Check from live update tries again by itself; one the registrar
// asked for (Check, Save) waits for them to ask again.
let _checkWaitLive = false;
let _checkRequestError = null;
let _sourceCoursesRejected = false;
let _sourceRebuildRequired = false;
let _checkTimer = null;
let _checkPromise = null;
let _queuedCheck = false;
let _undoCommands = [];
let _redoCommands = [];
let _moveCourseCode = null;
let _foundExamIdentity = null;
let _drillCourseMetadata = new Map();
// The exams the last checked report keeps in locked cells, by code: a report
// row made only of them gets a lock mark.
let _drillLockedCodes = new Set();
let _departmentContext = null;
let _departmentRequestToken = 0;
let _departmentBusy = false;
let _savedRunsTotal = null;  // saved runs anywhere, once history answers
const LIVE_UPDATE_KEY = 'exam-timetable-live-update';
const LIVE_UPDATE_DELAY = 450;
const examReview = window.ExamReview?.createController({ document, isArabic: IS_AR });
if ($('examLiveUpdate')) {
  try { $('examLiveUpdate').checked = localStorage.getItem(LIVE_UPDATE_KEY) !== 'off'; } catch (_) { $('examLiveUpdate').checked = true; }
}

// Snapshot the inputs and placements that determine the exported timetable.
// Reverting an edit restores export availability without creating another run.
function editorSignature() {
  const periods = [...document.querySelectorAll('#etPeriodsRepeater .et-period-row')]
    .map(row => [row.querySelector('.et-period-start').value.trim(), row.querySelector('.et-period-end').value.trim()]);
  const courses = getCheckedCourseEntries().map(course => [course.course_code, course.course_identity]).sort();
  const pins = Object.values(_pinnedCourses).map(pin => [pin.course_identity, pin.day, pin.period]).sort();
  // A link edit is a timetable edit: it needs a Check before Save.
  const links = linkSignature();
  // So is a lock: by day and period, never by course code.
  const locks = lockSignatureOf(_examLocks);
  const placements = (_currentResultData?.schedule || []).map(entry =>
    [entry.course_identity || entry.course_code, entry.day, entry.period]).sort();
  return JSON.stringify({
    label: $('etLabel').value.trim(),
    days: generateDayLabels(), dayCount: $('etNumDays').value.trim(), periods,
    maxPerDay: $('etMaxPerDay').value.trim(),
    programs: getCheckedValues('progList').sort(), sections: getCheckedValues('secList').sort(),
    relaxThin: $('etRelaxThin').checked,
    thinThreshold: $('etRelaxThin').checked ? $('etThinThreshold').value.trim() : '',
    randomize: $('etRandomize').checked, courses, pins, links, locks, placements,
    // Rooms asked for, so that the run can be locked: a change to Save.
    ...(_roomsForLocks ? { rooms: 'assign' } : {}),
  });
}

// QA added after runs were already saved. Each is a pure function of the
// placements (editorSignature, and the schedule below) and the enrolments
// (input_fingerprint), so a Check measuring it for the first time on an old
// run changes nothing the registrar decided: it must not read as unsaved.
const QA_ADDED_AFTER_SAVED_RUNS = ['multi_exam_day_students', 'same_day_exam_pairs'];

// Compare the saved report with a fresh evaluation, excluding timestamps.
// Inputs can change while placements stay identical; that still needs a save.
// ``unrecorded`` names QA keys the saved report never measured.
function reportSignature(data, unrecorded = []) {
  const qa = { ...(data.qa || {}) };
  for (const key of unrecorded) delete qa[key];
  // Building may omit empty room QA; fixed-placement evaluation authors it.
  // Normalize equivalent defaults without hiding real capacity/staffing changes.
  qa.rooms = {
    rooms_available: 0, rooms_used: 0, total_demand: 0, total_capacity_used: 0, avg_utilization: 0,
    unassigned_room_sections: [], room_double_bookings: [], invigilators_per_day: {},
    invigilators_total: 0, invigilators_total_M: 0, invigilators_total_F: 0,
    ...(qa.rooms || {}),
  };
  qa.room_feasibility_violations ??= [];
  // This records how a prior optimizer reached its result, not its current QA.
  delete qa.rebalance_moves;
  const stable = value => {
    if (Array.isArray(value)) return value.map(stable);
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(Object.keys(value).sort().filter(key => !['snapshot_timestamp', 'created_at', 'generated_at'].includes(key)).map(key => [key, stable(value[key])]));
  };
  return JSON.stringify(stable({
    qa, courses_count: data.courses_count, students_count: data.students_count,
    operations_snapshot: data.operations_snapshot || null,
    conflicts_count: data.conflicts_count, conflicts: data.conflicts || [], credit_map: data.credit_map || {},
    section_enrollment: data.section_enrollment || {}, primary_status: data.primary_status, status_flags: data.status_flags || [],
    schedule: (data.schedule || []).map(entry => ({ identity: entry.course_identity || entry.course_code,
      day: entry.day, period: entry.period, rooms: entry.rooms || [] })).sort((a, b) => a.identity.localeCompare(b.identity)),
  }));
}

function hasUnsavedEdits() {
  return Boolean(_currentResultData && _savedEditorSignature !== null
    && (editorSignature() !== _savedEditorSignature || _evaluatedReportDirty));
}

function needsExamSourceRebuild() {
  return Boolean(_currentResultData && (_sourceRebuildRequired || _currentResultData.enrollment_source !== 'scraper_timetable'));
}

function updateExportState() {
  const button = $('exportXlsx');
  const sourceBlocked = needsExamSourceRebuild() || _sourceCoursesRejected;
  const blocked = sourceBlocked || _builderBusy || _scheduleHasDraftMoves || ['loading', 'error', 'review'].includes(_checkState);
  button.classList.toggle('d-none', !_currentRunId);
  button.classList.toggle('disabled', blocked);
  button.setAttribute('aria-disabled', String(blocked));
  const checkBeforeExport = sourceBlocked ? (IS_AR ? 'راجع مصدر المقررات وأعد بناء الجدول قبل التصدير.' : 'Review the course source and rebuild before exporting.')
    : (IS_AR ? 'تحقق من التغييرات قبل التصدير.' : 'Check changes before exporting.');
  button.title = sourceBlocked ? checkBeforeExport : _scheduleHasDraftMoves ? T.saveBeforeExport : blocked ? checkBeforeExport : T.exportReady;
  if (_currentRunId && !blocked) button.href = `/ops/exam-timetable/${_currentRunId}/export.xlsx`;
  else button.removeAttribute('href');
  $('exportDraftNotice').classList.toggle('d-none', !_currentRunId || !blocked);
  $('exportDraftNotice').textContent = sourceBlocked ? checkBeforeExport : _scheduleHasDraftMoves ? T.saveBeforeExport : checkBeforeExport;
  $('kStatusLabel').textContent = _currentResultData && (sourceBlocked || _evaluatedEditorSignature !== editorSignature() || ['loading', 'error'].includes(_checkState))
    ? (IS_AR ? 'الحالة السابقة (غير محدثة):' : 'Previous status (outdated):')
    : (IS_AR ? 'الحالة:' : 'Status:');
  const departmentButton = $('departmentFilesBtn');
  departmentButton.classList.toggle('d-none', !_currentRunId);
  departmentButton.disabled = blocked || !_currentRunId;
  departmentButton.title = blocked ? button.title : '';
  if (_departmentContext && !departmentContextIsCurrent()) invalidateDepartmentExport();
  // Student data stays in view and says why it waits, not only greyed out.
  const studentButton = $('examStudentDataBtn');
  const studentReason = studentExportBlockReason();
  studentButton.setAttribute('aria-disabled', String(Boolean(studentReason)));
  studentButton.title = studentReason;
  $('examStudentDataReason').textContent = studentReason;
  updateRosterTab();
}

/* The Student lists view tab opens the saved run on the board, else the
   newest saved; with none saved anywhere it waits and says why. */
function updateRosterTab() {
  const link = $('examRosterTab');
  if (!link) return;
  const none = !_currentRunId && _savedRunsTotal === 0;
  link.href = link.dataset.base + (_currentRunId ? `?run=${encodeURIComponent(_currentRunId)}` : '');
  link.setAttribute('aria-disabled', String(none));
  if (none) link.setAttribute('aria-describedby', 'examRosterTabReason');
  else link.removeAttribute('aria-describedby');
  $('examRosterTabReason').hidden = !none;
}
$('examRosterTab')?.addEventListener('click', event => {
  if (event.currentTarget.getAttribute('aria-disabled') === 'true') event.preventDefault();
});

/* Student data follows the Department files rule, and names the reason. */
function studentExportBlockReason() {
  if (!_currentRunId) return $('examStudentDataBtn').dataset.noRunReason;
  if (!departmentExportBlocked()) return '';
  if (needsExamSourceRebuild() || _sourceCoursesRejected) {
    return IS_AR ? 'راجع مصدر المقررات وأعد بناء الجدول قبل التصدير.' : 'Review the course source and rebuild before exporting.';
  }
  if (hasUnsavedEdits()) return T.saveBeforeExport;
  return IS_AR ? 'تحقق من التغييرات قبل التصدير.' : 'Check changes before exporting.';
}

/* Department exports always use the saved run captured when the dialog opens. */
function departmentExportBlocked() {
  return !_currentRunId || _builderBusy || needsExamSourceRebuild() || _sourceCoursesRejected
    || hasUnsavedEdits() || _scheduleHasDraftMoves || ['loading', 'error', 'review'].includes(_checkState);
}

function exportContextIsCurrent(context) {
  return Boolean(context) && !departmentExportBlocked()
    && context.runId === _currentRunId
    && context.signature === editorSignature()
    && context.revision === _editorRevision;
}

function departmentContextIsCurrent() {
  return exportContextIsCurrent(_departmentContext);
}

function departmentExportError(message) {
  $('examDepartmentError').textContent = message;
  $('examDepartmentError').hidden = !message;
}

function departmentExportBusy(busy, message = '') {
  _departmentBusy = busy;
  $('examDepartmentForm').setAttribute('aria-busy', String(busy));
  $('examDepartmentFields').disabled = busy || !_departmentContext;
  $('downloadExamDepartments').disabled = busy || !_departmentContext || $('examDepartmentFields').hidden;
  $('examDepartmentStatus').textContent = message;
  $('examDepartmentStatus').hidden = !message;
}

function invalidateDepartmentExport() {
  _departmentRequestToken++;
  _departmentContext = null;
  departmentExportBusy(false);
  $('examDepartmentFields').hidden = true;
  departmentExportError(IS_AR ? 'تغيّر الجدول أو بدأت عملية أخرى. أغلق هذه النافذة، ثم افحص التغييرات واحفظها قبل فتح ملفات الأقسام مجدداً.'
    : 'The timetable changed or another operation started. Close this dialog, then check and save changes before reopening Department files.');
}

function closeDepartmentExport() {
  _departmentRequestToken++;
  _departmentContext = null;
  departmentExportBusy(false);
  const dialog = $('examDepartmentDialog');
  if (dialog.close) dialog.close();
  else dialog.removeAttribute('open');
  $('departmentFilesBtn').focus({ preventScroll: true });
}

function syncDepartmentSelection() {
  const choices = [...$('examDepartmentList').querySelectorAll('input')];
  const selected = choices.filter(input => input.checked).length;
  $('examDepartmentAll').checked = selected > 0 && selected === choices.length;
  $('examDepartmentAll').indeterminate = selected > 0 && selected < choices.length;
}

function renderDepartmentOptions(data) {
  const departments = Array.isArray(data.departments) ? data.departments : [];
  if (!departments.length) throw new Error(IS_AR ? 'لا توجد أقسام متاحة في هذا الجدول المحفوظ.' : 'No departments are available in this saved timetable.');
  const preferred = departments.find(item => item.id === 'ai-ds'
    || (item.programs || []).some(program => /^(AI|AI2|DS|DS2)$/i.test(program))) || departments[0];
  $('examDepartmentList').replaceChildren();
  departments.forEach(department => {
    const label = document.createElement('label');
    label.className = 'et-department-option';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.name = 'departments';
    checkbox.value = department.id;
    checkbox.checked = department === preferred;
    const text = document.createElement('span');
    const name = document.createElement('strong');
    name.textContent = department.name;
    const programs = document.createElement('small');
    const codes = document.createElement('bdi');
    codes.dir = 'ltr';
    codes.textContent = (department.programs || []).filter(Boolean).join(', ') || (IS_AR ? 'غير مسجل' : 'Not recorded');
    programs.append((IS_AR ? 'البرامج: ' : 'Programs: '), codes);
    const counts = document.createElement('small');
    counts.textContent = IS_AR ? `${department.course_count} مقرر · ${department.student_sittings} تسجيل اختبار`
      : `${department.course_count} courses · ${department.student_sittings} student sittings`;
    text.append(name, programs, counts);
    label.append(checkbox, text);
    $('examDepartmentList').append(label);
  });
  syncDepartmentSelection();
  $('examDepartmentGenders').replaceChildren();
  const genderLabels = IS_AR ? { M: 'طلاب', F: 'طالبات', U: 'غير مسجل' }
    : { M: 'Male students', F: 'Female students', U: 'Not recorded' };
  (data.genders || []).filter(gender => genderLabels[gender]).forEach(gender => {
    const label = document.createElement('label');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.name = 'genders';
    checkbox.value = gender;
    checkbox.checked = true;
    label.append(checkbox, genderLabels[gender]);
    $('examDepartmentGenders').append(label);
  });
  $('examDepartmentDates').replaceChildren();
  (data.days || []).forEach(day => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'date';
    input.dataset.day = day;
    input.className = 'form-control form-control-sm';
    label.append(String(day), input);
    $('examDepartmentDates').append(label);
  });
  $('examDepartmentDatesDetails').open = false;
  $('examDepartmentLanguage').value = 'ar';
  $('examDepartmentFields').hidden = false;
}

function departmentResponseError(data) {
  if (data.code === 'operations_snapshot_required') return new Error(IS_AR
    ? 'افحص التغييرات واحفظ هذا الجدول لتجهيز ملفات الأقسام.'
    : 'Check changes and save this timetable to prepare department exports.');
  return examResponseError(data);
}

async function openDepartmentExport() {
  updateLoadedRunActions();
  if (departmentExportBlocked() || $('examDepartmentDialog').open) return;
  _departmentContext = { runId: _currentRunId, signature: editorSignature(), revision: _editorRevision };
  const token = ++_departmentRequestToken;
  $('examDepartmentFields').hidden = true;
  departmentExportError('');
  departmentExportBusy(true, IS_AR ? 'جارٍ تحميل الأقسام...' : 'Loading departments...');
  const dialog = $('examDepartmentDialog');
  if (dialog.showModal) dialog.showModal();
  else dialog.setAttribute('open', '');
  $('closeExamDepartment').focus({ preventScroll: true });
  try {
    const response = await fetch(`/ops/exam-timetable/${_departmentContext.runId}/departments/?language=${IS_AR ? 'ar' : 'en'}`, {
      headers: { 'X-CSRFToken': getCsrfToken() || CSRF },
    });
    const data = await readExamResponse(response);
    if (token !== _departmentRequestToken) return;
    if (!departmentContextIsCurrent()) { invalidateDepartmentExport(); return; }
    if (!response.ok || !data.ok) throw departmentResponseError(data);
    renderDepartmentOptions(data);
  } catch (error) {
    if (token === _departmentRequestToken) departmentExportError(error instanceof TypeError
      ? (IS_AR ? 'تعذر تحميل الأقسام. تحقق من الاتصال ثم أعد المحاولة.' : 'Could not load departments. Check your connection and retry.') : error.message);
  } finally {
    if (token === _departmentRequestToken) departmentExportBusy(false);
  }
}

async function downloadDepartmentFiles(event) {
  event.preventDefault();
  if (_departmentBusy) return;
  if (!departmentContextIsCurrent()) { invalidateDepartmentExport(); return; }
  const selected = name => [...$('examDepartmentForm').querySelectorAll(`input[name="${name}"]:checked`)].map(input => input.value);
  const departments = selected('departments');
  const genders = selected('genders');
  if (!departments.length || !genders.length) {
    departmentExportError(!departments.length
      ? (IS_AR ? 'اختر قسماً واحداً على الأقل.' : 'Select at least one department.')
      : (IS_AR ? 'اختر فئة طلاب واحدة على الأقل.' : 'Select at least one student group.'));
    return;
  }
  const dates = {};
  for (const input of $('examDepartmentDates').querySelectorAll('input')) {
    if (!input.validity.valid) {
      $('examDepartmentDatesDetails').open = true;
      departmentExportError(IS_AR ? 'أدخل تاريخاً صحيحاً أو اترك الحقل فارغاً.' : 'Enter a valid date or leave the field blank.');
      input.focus();
      return;
    }
    if (input.value) dates[input.dataset.day] = input.value;
  }
  const token = ++_departmentRequestToken;
  const runId = _departmentContext.runId;
  departmentExportError('');
  departmentExportBusy(true, IS_AR ? 'جارٍ تجهيز الملفات...' : 'Preparing files...');
  let objectUrl;
  try {
    const response = await fetch(`/ops/exam-timetable/${runId}/departments/export/`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() || CSRF },
      body: JSON.stringify({ departments, genders, language: $('examDepartmentLanguage').value, dates }),
    });
    if (token !== _departmentRequestToken) return;
    if (!departmentContextIsCurrent()) { invalidateDepartmentExport(); return; }
    const contentType = response.headers?.get('content-type') || '';
    if (!response.ok || response.redirected || !/application\/(?:vnd\.openxmlformats-officedocument\.spreadsheetml\.sheet|zip|octet-stream)/i.test(contentType)) {
      throw departmentResponseError(await readExamResponse(response));
    }
    const blob = await response.blob();
    if (token !== _departmentRequestToken) return;
    if (!departmentContextIsCurrent()) { invalidateDepartmentExport(); return; }
    const disposition = response.headers.get('content-disposition') || '';
    const filename = disposition.match(/filename="([^"]+)"/i)?.[1]
      || disposition.match(/filename=([^;\s]+)/i)?.[1]
      || (departments.length > 1 ? 'department-exams.zip' : 'department-exams.xlsx');
    objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename.replace(/[\\/]/g, '_');
    document.body.append(link);
    link.click();
    link.remove();
    departmentExportBusy(false, IS_AR ? 'بدأ تنزيل الملفات.' : 'File download started.');
  } catch (error) {
    if (token === _departmentRequestToken) {
      departmentExportBusy(false);
      departmentExportError(error instanceof TypeError ? (IS_AR ? 'تعذر تنزيل الملفات. تحقق من الاتصال ثم أعد المحاولة.' : 'Could not download files. Check your connection and retry.') : error.message);
    }
  } finally {
    // The browser has consumed the download click before releasing the blob URL.
    if (objectUrl) setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    if (token === _departmentRequestToken && _departmentBusy) departmentExportBusy(false);
  }
}

/* The student data dialog (static/js/exam-student-export.js) exports the
   saved run on the board, under the Department files rule, and reads the
   page only through this hook. */
window.examStudentExportHost = Object.freeze({
  blockReason: studentExportBlockReason,
  context: () => ({ runId: _currentRunId, signature: editorSignature(), revision: _editorRevision }),
  isCurrent: exportContextIsCurrent,
  savedRun: () => cloneData(_savedResultData),
  readResponse: readExamResponse,
  csrf: () => getCsrfToken() || CSRF,
  closeDepartmentFiles: closeDepartmentExport,
});

/* The course drawer (static/js/exam-roster-drawer.js) shows the saved run
   behind the board and says what the draft has moved; read-only. */
window.examRosterHost = Object.freeze({
  runId: () => _currentRunId,
  savedRun: () => _savedResultData,
  currentSchedule: () => _currentResultData?.schedule || [],
  unsavedMoves: movedSinceSave,
  hasUnsavedChanges: () => hasUnsavedEdits() || _scheduleHasDraftMoves,
  blockReason: studentExportBlockReason,
  csrf: () => getCsrfToken() || CSRF,
});

$('departmentFilesBtn').addEventListener('click', openDepartmentExport);
$('closeExamDepartment').addEventListener('click', closeDepartmentExport);
$('examDepartmentDialog').addEventListener('cancel', event => { event.preventDefault(); closeDepartmentExport(); });
$('examDepartmentForm').addEventListener('submit', downloadDepartmentFiles);
$('examDepartmentList').addEventListener('change', syncDepartmentSelection);
$('examDepartmentAll').addEventListener('change', event => {
  $('examDepartmentList').querySelectorAll('input').forEach(input => { input.checked = event.target.checked; });
  syncDepartmentSelection();
});

// Delegation also covers newly added period rows. Course bulk links call
// updateCourseCount, while pins and drag/drop use updatePinBar.
['input', 'change'].forEach(eventName => $('examSettingsControls').addEventListener(eventName, event => {
  if (event.target.matches('input, select') && !event.target.closest('#examPinEditor, #examLinkEditor')) updateLoadedRunActions();
}));

function setBuilderBusy(busy) {
  _builderBusy = busy;
  updateBuildSummary();
  // Keep the status live region readable while preventing edits to a request
  // already in flight. Inert also covers course-selection links and dragging.
  for (const id of ['examSettingsControls', 'schedGrid', 'pinBar', 'historyList', 'historyPages', 'examEditToolbar', 'examReviewPanel', 'examMoveDialog', 'examLinkDialog']) {
    const element = $(id);
    if (!element) continue;
    element.inert = busy;
    element.setAttribute('aria-busy', String(busy));
    element.classList.toggle('et-builder-busy', busy);
  }
  // Recompute action availability after either entering or leaving a request.
  // A loaded result is rendered while busy; its disabled buttons must be
  // released when the request finishes, even if no editor value changed.
  updateLoadedRunActions();
  // A Check left waiting for the solver lost its retry to the busy builder;
  // with the builder free, it is owed now.
  if (!busy && _checkState === 'waiting' && _checkWaitLive) scheduleDraftCheck({ delay: 0 });
}

function updateBuildSummary() {
  const selected = getCheckedValues('courseList').length;
  const days = generateDayLabels().length;
  const periods = readExamPeriods().periods.length;
  const pins = Object.values(_pinnedCourses).length;
  const links = _linkedExams.length;
  $('examBuildSummary').textContent = !_coursesLoaded
    ? (IS_AR ? 'حمّل المقررات للمتابعة.' : 'Load courses to continue.')
    : (IS_AR ? `${selected} مقرر · ${days} أيام · ${periods} فترات يومياً · ${pins} مثبت`
      : `${selected} courses · ${days} days · ${periods} periods/day · ${pins} fixed`)
      + (links ? (IS_AR ? ` · ${links} مرتبط` : ` · ${links} linked`) : '')
      + (_examLocks.length ? LOCK_TEXT.summary(_examLocks.length) : '');
}

function updateLoadedRunActions() {
  updateBuildSummary();
  if (_currentResultData && _savedEditorSignature !== null) {
    const signature = editorSignature();
    _scheduleHasDraftMoves = signature !== _savedEditorSignature || _evaluatedReportDirty;
    _loadedRunForRebuild = true;
    if (!_renderingResults && signature !== _observedEditorSignature) {
      _observedEditorSignature = signature;
      _editorRevision++;
      _checkState = signature === _evaluatedEditorSignature ? 'checked' : 'stale';
      _checkError = '';
      if (signature === _savedEditorSignature && _savedResultData && !_evaluatedInputsChanged && signature !== _evaluatedEditorSignature) {
        cancelDraftChecks();
        renderResults(_savedResultData, { evaluation: true });
        return;
      }
      scheduleDraftCheck();
    }
  }
  const hasLoadedSchedule = Boolean(
    _loadedRunForRebuild
    && _currentResultData
    && Array.isArray(_currentResultData.schedule)
    && _currentResultData.schedule.length
  );
  const saveBtn = $('saveLoadedBtn');
  const optimizeBtn = $('optimizeLoadedBtn');
  // The panel says whose job it is and that these wait for it.
  const laneHeld = jobLaneHeldByOther();
  if (saveBtn) {
    saveBtn.classList.toggle('d-none', !hasLoadedSchedule);
    saveBtn.disabled = _builderBusy || laneHeld || needsExamSourceRebuild() || !hasLoadedSchedule || !_scheduleHasDraftMoves;
  }
  if (optimizeBtn) {
    optimizeBtn.classList.toggle('d-none', !hasLoadedSchedule);
    optimizeBtn.disabled = _builderBusy || laneHeld || needsExamSourceRebuild() || !hasLoadedSchedule;
  }
  // Once the board is edited again, the last report describes a board that
  // no longer exists. Unsaved drags alone are not that: a repair that moved
  // nothing leaves them in place, and its report is about exactly that board.
  if (_editorRevision !== _repairReportRevision || !hasLoadedSchedule) clearRepairReport();
  const minChangeBtn = $('minChangeBtn');
  if (minChangeBtn) {
    minChangeBtn.classList.toggle('d-none', !hasLoadedSchedule);
    minChangeBtn.disabled = _builderBusy || laneHeld || needsExamSourceRebuild() || !hasLoadedSchedule;
  }
  const addBtn = $('addCoursesBtn');
  if (addBtn) {
    addBtn.classList.toggle('d-none', !hasLoadedSchedule);
    // Not disabled for unsaved edits: pressing it says why and offers Save.
    addBtn.disabled = _builderBusy || laneHeld || needsExamSourceRebuild() || !hasLoadedSchedule;
  }
  // The hidden Build button's tooltip explains nothing: the note does.
  const note = $('loadedRunBuildNote');
  if (note) {
    note.hidden = !hasLoadedSchedule;
    if (hasLoadedSchedule) $('loadCoursesBtn').setAttribute('aria-describedby', 'loadedRunBuildNote');
    else $('loadCoursesBtn').removeAttribute('aria-describedby');
  }
  $('buildBtn').classList.toggle('d-none', hasLoadedSchedule);
  if (hasLoadedSchedule) {
    $('buildBtn').disabled = true;
    $('buildBtn').title = T.loadedRunAction;
  } else {
    $('buildBtn').disabled = _builderBusy || laneHeld || !_coursesLoaded || !getCheckedValues('courseList').length;
    $('buildBtn').title = '';
  }
  updateExportState();
  updateEditingStatus();
}

function enterFreshBuildMode() {
  clearOutsideCourses();
  clearExamCourseSourceError();
  clearJobNotice();
  cancelDraftChecks();
  clearLockRefusal();
  // Leaving a saved run for a new Build: its SAVED locks are what a Build can
  // keep (the draft was discarded), so they come along, with the run they are
  // kept from. Leaving no saved run keeps what the builder already holds.
  if (_savedResultData) {
    const saved = savedLocks();
    _lockSource = saved.length ? { runId: _currentRunId ?? _savedResultData.run_id, run: _savedResultData } : null;
    restoreLocksFromRun(saved.length ? _savedResultData : null);
  }
  _roomsForLocks = false;
  _currentResultData = null;
  _loadedRunForRebuild = false;
  _scheduleHasDraftMoves = false;
  _currentRunId = null;
  _savedEditorSignature = null;
  _observedEditorSignature = null;
  _evaluatedEditorSignature = null;
  _savedResultData = null;
  _evaluatedReportDirty = false;
  _evaluatedInputsChanged = false;
  _reviewedInputFingerprint = null;
  _foundExamIdentity = null;
  _drillCourseMetadata = new Map();
  _undoCommands = [];
  _redoCommands = [];
  if ($('examSetupDetails')) $('examSetupDetails').open = true;
  $('examSetupSummary')?.querySelector('.et-details-meta')?.remove();
  $('etResults').classList.add('d-none');
  _checkState = 'checked';
  updatePinBar();
  updateLoadedRunActions();
}

function closeDrill(restoreFocus = false) {
  const fullCard = document.querySelector('.kpi-click.active');
  const quickCard = document.querySelector('[data-open-drill][aria-expanded="true"]');
  const activeCard = $('examSummaryDetails')?.open ? (fullCard || quickCard) : (quickCard || fullCard);
  document.querySelectorAll('[data-open-drill]').forEach(button => button.setAttribute('aria-expanded', 'false'));
  $('kpiDrill').classList.add('d-none');
  document.querySelectorAll('.kpi-click.active').forEach(el => {
    el.classList.remove('active');
    el.setAttribute('aria-expanded', 'false');
  });
  if (restoreFocus === true) {
    const mappingNoticeClosed = activeCard?.id === 'examSectionMappingReview' && activeCard.closest('[hidden], .d-none');
    const target = mappingNoticeClosed ? $('examEditHeading') : activeCard;
    target?.focus({ preventScroll: true });
  }
}

// Render helpers — each returns {title, head, body, colspan} for openDrill
function hideLegacyQaWarnings() {
  const panel = $('qaWarnings');
  if (!panel) return;
  panel.hidden = true;
  panel.classList.add('d-none');
  const body = $('qaBody');
  if (body) body.innerHTML = '';
}

function cloneData(data) {
  return JSON.parse(JSON.stringify(data || {}));
}

// A count a saved run never measured is null, shown as "—": never a zero.
const recordedCount = value => (Number.isFinite(value) ? value : null);
function showRecordedCount(element, value) {
  const count = recordedCount(value);
  if (count != null) { element.textContent = count; return; }
  element.innerHTML = `<span aria-hidden="true">—</span><span class="visually-hidden">${IS_AR ? 'لم يُحسب' : 'Not calculated'}</span>`;
}

const EXAM_METRICS = [
  { id: 'kCourses', value: data => data.courses_count ?? data.qa?.total_courses ?? 0 },
  { id: 'kStudents', value: data => data.students_count ?? data.qa?.total_students ?? 0 },
  { id: 'kSlots', value: data => data.qa?.slots_used ?? 0 },
  { id: 'kMaxDay', value: data => data.qa?.max_exams_per_day_per_student ?? 0, lower: true },
  { id: 'kMultiExamDay', value: data => recordedCount(data.qa?.multi_exam_day_students), lower: true },
  { id: 'kOver2', value: data => data.qa?.students_over_limit_per_day ?? data.qa?.students_over_2_per_day ?? 0, lower: true,
    quick: IS_AR ? 'تجاوز الحد اليومي' : 'Over daily limit', drill: 'overload' },
  { id: 'kConflicts', value: data => data.qa?.conflict_count ?? 0, lower: true,
    quick: IS_AR ? 'التعارضات' : 'Conflicts', drill: 'conflicts' },
  { id: 'kMaxCredit', value: data => data.qa?.max_credit_load_per_day ?? 0, lower: true },
  { id: 'kHeavyDay', value: data => data.qa?.heavy_day_students ?? 0, lower: true },
  { id: 'kRoomsUsed', value: data => data.qa?.rooms?.rooms_used ?? 0 },
  { id: 'kRoomUtil', value: data => Math.round(Number(data.qa?.rooms?.avg_utilization ?? 0) * 100), suffix: '%' },
  { id: 'kRoomUnassigned', value: data => data.qa?.rooms?.unassigned_room_sections?.length ?? 0, lower: true,
    quick: IS_AR ? 'مجموعات بلا قاعة' : 'Unassigned groups', drill: 'room-unassigned' },
  { id: 'kRoomDouble', value: data => data.qa?.rooms?.room_double_bookings?.length ?? 0, lower: true },
  { id: 'kMultiSittingCount', value: data => data.qa?.multi_sitting_sections ?? 0 },
  { id: 'kThinCount', value: data => data.qa?.thin_courses?.length ?? 0 },
  { id: 'kThinClash', value: data => data.qa?.thin_clash_risk?.length ?? 0, lower: true },
];

function hasCurrentEvaluation() {
  return Boolean(_currentResultData && !needsExamSourceRebuild() && !_sourceCoursesRejected && _evaluatedEditorSignature === editorSignature()
    && ['checked', 'review'].includes(_checkState));
}

function metricComparison(metric, fresh) {
  if (needsExamSourceRebuild()) return { text: IS_AR ? 'تقرير سابق — مصدر المقررات يحتاج مراجعة' : 'Previous report — course source needs review', modifier: 'is-stale', compact: '' };
  if (!fresh) return { text: IS_AR ? 'آخر تحقق — تغييرات لم تُفحص' : 'Last checked — changes pending', modifier: 'is-stale', compact: '' };
  const baseline = _savedResultData?.input_fingerprint;
  const current = _currentResultData?.input_fingerprint;
  if (!baseline || !current) return { text: IS_AR ? 'تحقق واحفظ لبدء المقارنات' : 'Check and save to start comparisons', modifier: 'is-neutral' };
  if (baseline !== current) {
    const text = lockChangesSinceSave().length ? LOCK_TEXT.comparisonPaused : IS_AR ? 'المقارنة متوقفة: تغيّرت بيانات الجدول' : 'Comparison paused: timetable data changed';
    return { text, modifier: 'is-neutral' };
  }
  const suffix = metric.suffix || '';
  const saved = metric.value(_savedResultData);
  const checked = metric.value(_currentResultData);
  if (saved == null || checked == null) {
    // One side never measured this metric (a run saved before it existed).
    const shown = value => (value == null ? '—' : `${value}${suffix}`);
    return {
      text: IS_AR ? `المحفوظ ${shown(saved)} ← المتحقق ${shown(checked)}` : `Saved ${shown(saved)} → Checked ${shown(checked)}`,
      modifier: saved == null && checked == null ? 'is-neutral is-unchanged' : 'is-neutral',
      compact: `${shown(saved)} → ${shown(checked)}`,
    };
  }
  const before = Number(saved);
  const after = Number(checked);
  const difference = after - before;
  const change = difference ? `${difference > 0 ? '+' : '−'}${Math.abs(difference)}${metric.suffix ? (IS_AR ? ' نقطة' : ' pp') : ''}` : (IS_AR ? 'دون تغيير' : 'unchanged');
  return {
    text: IS_AR ? `المحفوظ ${before}${suffix} ← المتحقق ${after}${suffix} (${change})` : `Saved ${before}${suffix} → Checked ${after}${suffix} (${change})`,
    modifier: before === after ? 'is-neutral is-unchanged' : difference && metric.lower ? (difference < 0 ? 'is-better' : 'is-worse') : 'is-neutral',
    compact: `${before}${suffix} → ${after}${suffix}${difference ? ` (${change})` : ''}`,
  };
}

function updateMetricComparisons() {
  if (!_currentResultData || !_savedResultData) return;
  const fresh = hasCurrentEvaluation();
  for (const metric of EXAM_METRICS) {
    const value = $(metric.id);
    if (!value) continue;
    let delta = value.parentElement.querySelector('.et-kpi-delta');
    if (!delta) {
      delta = document.createElement('small');
      value.insertAdjacentElement('afterend', delta);
    }
    const comparison = metricComparison(metric, fresh);
    delta.className = `et-kpi-delta ${comparison.modifier}`;
    delta.textContent = comparison.text;
  }
  if ($('examQuickMetrics')) {
    const focused = document.activeElement?.closest?.('[data-open-drill]')?.dataset.openDrill;
    const metrics = ['kConflicts', 'kOver2', 'kRoomUnassigned'].map(id => EXAM_METRICS.find(metric => metric.id === id));
    $('examQuickMetrics').innerHTML = metrics.map(metric => {
      const comparison = metricComparison(metric, fresh);
      return `<button type="button" class="et-quick-metric" data-open-drill="${metric.drill}" aria-controls="kpiDrill" aria-expanded="${!$('kpiDrill').classList.contains('d-none') && $('kpiDrill').dataset.type === metric.drill}" ${_builderBusy ? 'disabled' : ''}><span class="et-quick-label">${metric.quick}${fresh ? '' : (IS_AR ? ' · سابق' : ' · previous')}</span><strong class="et-quick-value">${escapeAttr(metric.value(_currentResultData))}</strong><small class="et-quick-delta ${comparison.modifier}" dir="ltr" title="${escapeAttr(comparison.text)}">${escapeAttr(comparison.compact || '—')}</small></button>`;
    }).join('');
    const context = metricComparison(metrics[0], fresh);
    const contextText = context.compact ? (IS_AR ? 'مقارنة بالجدول المحفوظ' : 'Saved → latest check') : context.text;
    $('examQuickMetrics').insertAdjacentHTML('beforeend', `<small class="et-quick-context">${escapeAttr(contextText)}</small>`);
    if (focused) [...$('examQuickMetrics').querySelectorAll('[data-open-drill]')].find(button => button.dataset.openDrill === focused)?.focus({ preventScroll: true });
  }
}

function currentExamByIdentity(identity) {
  return _currentResultData?.schedule?.find(entry => (entry.course_identity || entry.course_code) === identity) || null;
}

function currentExamIsSelected(entry) {
  return Boolean(entry && getCheckedValues('courseList').includes(entry.course_code));
}

/* Say exactly what the minimum-change repair did. The move list is the point
   of the button: a registrar who pressed "fewest moves" needs to see which
   exams moved and where, not just that a run was saved. */
const AR_PLURAL = typeof Intl !== 'undefined' && Intl.PluralRules ? new Intl.PluralRules('ar') : null;
// Arabic agrees in five forms (one, two, few 3-10, many 11-99, other 100+);
// a 1-versus-more test gets every count from two upward wrong.
function arabicCount(count, forms) {
  const category = AR_PLURAL ? AR_PLURAL.select(count) : (count === 1 ? 'one' : 'other');
  return (forms[category] || forms.other).replace('{n}', String(count));
}

const AR_SECONDS = { one: 'ثانية واحدة', two: 'ثانيتين', few: '{n} ثوانٍ', many: '{n} ثانيةً', other: '{n} ثانية' };
const AR_EXAMS = { one: 'اختبار واحد', two: 'اختبارين', few: '{n} اختبارات', many: '{n} اختباراً', other: '{n} اختبار' };
const AR_REMAINING = { one: 'بقيت مخالفة واحدة', two: 'بقيت مخالفتان', few: 'بقيت {n} مخالفات', many: 'بقيت {n} مخالفةً', other: 'بقيت {n} مخالفة' };
const AR_UNSEATED = {
  one: 'تعذّر إيجاد موعد لاختبار واحد، فنُقل',
  two: 'تعذّر إيجاد موعد لاختبارين، فنُقلا',
  few: 'تعذّر إيجاد موعد لـ{n} اختبارات، فنُقلت',
  many: 'تعذّر إيجاد موعد لـ{n} اختباراً، فنُقلت',
  other: 'تعذّر إيجاد موعد لـ{n} اختبار، فنُقلت',
};

const REPAIR_TEXT = {
  moved: count => IS_AR ? `تم نقل ${arabicCount(count, AR_EXAMS)}` : `Moved ${count} exam${count === 1 ? '' : 's'}`,
  proven: () => IS_AR ? '، وهذا أقل عدد ممكن' : ', the fewest possible',
  showAll: count => IS_AR ? `عرض جميع التنقلات (${count})` : `Show all ${count} moves`,
  remainingCount: count => IS_AR
    ? arabicCount(count, AR_REMAINING)
    : `${count} rule break${count === 1 ? ' remains' : 's remain'}`,
  remaining: count => IS_AR
    ? `${REPAIR_TEXT.remainingCount(count)} بين اختبارات مثبّتة أو نقلتَها منذ آخر حفظ، ولم يُغيَّر موعد أيٍّ منها.`
    : `${REPAIR_TEXT.remainingCount(count)} between pinned exams or exams you moved since the last save; those exams were left where they are.`,
  unseated: count => IS_AR
    ? `${arabicCount(count, AR_UNSEATED)} إلى «${T.overflow}»:`
    : `${count} exam${count === 1 ? '' : 's'} could not be placed, so ${count === 1 ? 'it was' : 'they were'} moved to the ${T.overflow}:`,
  widened: () => IS_AR
    ? 'بعض هذه الاختبارات لم يكن في أي تعارض، ونُقل لإفساح المجال.'
    : 'Some of these exams were not in any clash; they were moved to make room.',
  stillOverflow: count => IS_AR
    ? `لا توجد تعارضات تحتاج إصلاحاً، لكن عدد الاختبارات في «${T.overflow}»: ${count}. استخدم «تحسين الجدول الحالي» لإعادة جدولتها.`
    : `No clashes to repair, but ${count} exam${count === 1 ? ' is' : 's are'} still in the ${T.overflow}. Use Optimize current timetable to place ${count === 1 ? 'it' : 'them'}.`,
  nothing: () => IS_AR
    ? 'لا يوجد ما يحتاج إصلاحاً: لا تعارض بين الاختبارات، ولا يجتمع اختباران من الفصل الدراسي نفسه في يوم واحد.'
    : 'Nothing to repair: no exams clash, and no study term has two exams on the same day.',
  failed: count => IS_AR
    ? `تعذّر إكمال الإصلاح، فلم يُنقل أي اختبار${count ? `، و${REPAIR_TEXT.remainingCount(count)}` : ''}. أعد المحاولة أو استخدم «تحسين الجدول الحالي».`
    : `The repair could not finish, so no exam was moved.${count ? ` ${REPAIR_TEXT.remainingCount(count)}.` : ''} Try again, or use Optimize current timetable.`,
  saved: () => IS_AR ? 'حُفظت النتيجة جدولاً جديداً في «الجداول المحفوظة».' : 'Saved as a new timetable in Saved timetables.',
  linkedClash: count => IS_AR
    ? `${arabicCount(count, AR_LINK_CLASH_REPAIR)} لا يفصل الإصلاح المقررات المرتبطة أبداً؛ ألغِ الربط أو راجع التسجيل.`
    : `${count} student${count === 1 ? ' is' : 's are'} registered in two linked courses: each sits two papers at one time. The repair never separates linked courses; unlink them or review the registration.`,
};

function repairMoveItem(move) {
  const arrow = IS_AR ? '←' : '→';
  // The arrow is decorative; the hidden word is what a screen reader hears.
  return `<li><strong><bdi dir="ltr">${escapeAttr(move.course_code)}</bdi></strong> `
    + `<bdi dir="ltr">${escapeAttr(examLocation(move.from))}</bdi> `
    + `<span aria-hidden="true">${arrow}</span><span class="visually-hidden">${IS_AR ? ' إلى ' : ' to '}</span> `
    + `<bdi dir="ltr">${escapeAttr(examLocation(move.to))}</bdi></li>`;
}

function describeMinimumChange(report, { saved = true } = {}) {
  // Whatever the server did is already done when this renders, so a malformed
  // report must degrade to a plainer message rather than throw a red "Error".
  const moves = (Array.isArray(report?.moves) ? report.moves : [])
    .filter(move => move && typeof move.course_code === 'string' && move.from && move.to);
  const unseated = (Array.isArray(report?.unseated) ? report.unseated : [])
    .filter(code => typeof code === 'string');
  const remaining = Number(report?.violations_after) || 0;
  // Exams already parked in Overflow are not clashes this repair can see, but
  // the board is not finished while they are there.
  const alreadyOverflow = Number(report?.already_overflow) || 0;
  // Students in two linked courses: a clash no repair may separate.
  const linkedClash = Number(report?.linked_clash_students) || 0;
  // Exams in locked cells, never moved, and breaches among them alone.
  const lockedCount = Number(report?.locked_count) || 0;
  const lockedViolations = Math.min(remaining, Number(report?.locked_violations) || 0);
  const solved = ['OPTIMAL', 'FEASIBLE'].includes(report?.status);

  if (!solved) {
    // Never blame the registrar for a solver that did not finish.
    return { html: `<p>${escapeAttr(REPAIR_TEXT.failed(remaining))}</p>`, clean: false };
  }
  const parts = [];
  // "No exams clash" would be untrue while students sit two linked papers at once.
  if (!moves.length && !unseated.length && !remaining && (alreadyOverflow || !linkedClash)) {
    parts.push(`<p>${escapeAttr(alreadyOverflow ? REPAIR_TEXT.stillOverflow(alreadyOverflow) : REPAIR_TEXT.nothing())}</p>`);
  }
  if (moves.length) {
    const lead = `${REPAIR_TEXT.moved(moves.length)}${report?.proven_minimal ? REPAIR_TEXT.proven() : ''}:`;
    const shown = moves.slice(0, 6).map(repairMoveItem).join('');
    const rest = moves.length > 6
      ? `<details><summary>${escapeAttr(REPAIR_TEXT.showAll(moves.length))}</summary><ul>${moves.slice(6).map(repairMoveItem).join('')}</ul></details>`
      : '';
    parts.push(`<p>${escapeAttr(lead)}</p><ul>${shown}</ul>${rest}`);
    if (report?.widened) parts.push(`<p>${escapeAttr(REPAIR_TEXT.widened())}</p>`);
  }
  if (unseated.length) {
    const codes = unseated.map(code => `<bdi dir="ltr">${escapeAttr(code)}</bdi>`).join(IS_AR ? '، ' : ', ');
    parts.push(`<p>${escapeAttr(REPAIR_TEXT.unseated(unseated.length))} ${codes}.</p>`);
  }
  if (remaining - lockedViolations) parts.push(`<p>${escapeAttr(REPAIR_TEXT.remaining(remaining - lockedViolations))}</p>`);
  if (lockedViolations) parts.push(`<p class="et-repair-locked">${escapeAttr(LOCK_TEXT.fixLockedRemain(lockedViolations))}</p>`);
  if (lockedCount) parts.push(`<p class="et-repair-locked">${escapeAttr(LOCK_TEXT.fixKept(lockedCount))}</p>`);
  if (linkedClash) parts.push(`<p class="et-repair-linked">${escapeAttr(REPAIR_TEXT.linkedClash(linkedClash))}</p>`);
  if (saved) parts.push(`<p>${escapeAttr(REPAIR_TEXT.saved())}</p>`);
  return { html: parts.join(''), clean: !remaining && !unseated.length && !alreadyOverflow && !linkedClash };
}

function showRepairReport(report, options) {
  const region = $('examRepairReport');
  if (!region) return;
  _repairReportRevision = _editorRevision;
  const summary = describeMinimumChange(report, options);
  region.classList.toggle('is-clean', summary.clean);
  region.classList.toggle('is-partial', !summary.clean);
  region.innerHTML = summary.html;
}

/* ── Optimize: what the search changed on the board ── */
// Optimize looks for a better timetable starting from the one on screen. It
// saves one only when it finds it; the numbers below are the server's, said in
// the registrar's words. The internal spacing score is never shown.
const AR_FIXED_KEPT = {
  one: 'بقي اختبار واحد مثبّت أو مقفل في موعده',
  two: 'بقي اختباران مثبّتان أو مقفلان في موعديهما',
  few: 'بقيت {n} اختبارات مثبّتة أو مقفلة في مواعيدها',
  many: 'بقي {n} اختباراً مثبّتاً أو مقفلاً في موعده',
  other: 'بقي {n} اختبار مثبّت أو مقفل في موعده',
};
const AR_SENT_TO_OVERFLOW = {
  one: 'نُقل اختبار واحد',
  two: 'نُقل اختباران',
  few: 'نُقلت {n} اختبارات',
  many: 'نُقل {n} اختباراً',
  other: 'نُقل {n} اختبار',
};
const AR_PLACE_BY_HAND = { one: 'حدّد موعده يدوياً.', two: 'حدّد موعديهما يدوياً.', other: 'حدّد مواعيدها يدوياً.' };
const OPTIMISE_TEXT = {
  found: () => IS_AR ? 'وجد التحسين جدولاً أفضل.' : 'Optimize found a better timetable.',
  // Saved although no better timetable was found: a pin or lock put an exam
  // back in its place (moved > 0), or the request changed pins, links, locks or
  // settings and those were saved with nothing moved.
  notBetter: moved => IS_AR
    ? (moved
      ? 'لم يجد التحسين جدولاً أفضل. أُعيدت الاختبارات المثبّتة أو المقفلة إلى مواعيدها، وحُفظ الجدول.'
      : 'لم يجد التحسين جدولاً أفضل. لم يُنقل أي اختبار، وحُفظ الجدول بالتثبيتات والروابط والأقفال والإعدادات الحالية.')
    : (moved
      ? 'Optimize found no better timetable. Pinned or locked exams were put back in their places, and the timetable was saved.'
      : 'Optimize found no better timetable. No exam was moved; the timetable was saved with your current pins, links, locks and settings.'),
  // The sentence a job panel has already said; the report then starts here.
  screenStays: () => IS_AR
    ? 'يبقى الجدول المعروض، بما فيه تغييراتك غير المحفوظة، كما هو.'
    : 'The timetable on screen, including any changes you have not saved, stays as it is.',
  noBetter: () => `${T.optimizeNoBetter} ${OPTIMISE_TEXT.screenStays()}`,
  ruleBreaks: () => IS_AR ? 'مخالفات القواعد بين الاختبارات القابلة للنقل' : 'Rule breaks among movable exams',
  sentToOverflow: count => IS_AR
    ? `لإزالة المخالفات ${arabicCount(count, AR_SENT_TO_OVERFLOW)} إلى «${T.overflow}». ${arabicCount(count, AR_PLACE_BY_HAND)}`
    : `To clear rule breaks, ${count} exam${count === 1 ? ' was' : 's were'} sent to the ${T.overflow}. Place ${count === 1 ? 'it' : 'them'} by hand.`,
  multiExamDay: () => IS_AR ? 'طلاب لديهم اختباران أو أكثر في يوم واحد' : 'Students with 2+ exams in a day',
  overLimit: () => IS_AR ? 'طلاب تجاوزوا الحد اليومي للاختبارات' : 'Students over the daily exam limit',
  heavyDay: () => IS_AR ? 'طلاب لديهم يوم اختبارات مرتفع الساعات المعتمدة' : 'Students with a heavy-credit day',
  unseated: () => IS_AR ? `اختبارات في «${T.overflow}»` : `Exams in the ${T.overflow}`,
  // The busiest day's invigilator total as the rooms really came out.
  invigilatorPeak: () => IS_AR ? 'أكبر عدد مراقبين في يوم واحد' : 'Most invigilators needed in one day',
  moved: (moved, fixed) => {
    if (IS_AR) {
      const kept = fixed ? `، و${arabicCount(fixed, AR_FIXED_KEPT)}` : '';
      return moved ? `تم نقل ${arabicCount(moved, AR_EXAMS)}${kept}.` : `لم يُنقل أي اختبار${kept}.`;
    }
    const kept = fixed ? `${fixed} pinned or locked exam${fixed === 1 ? '' : 's'} stayed where ${fixed === 1 ? 'it was' : 'they were'}` : '';
    if (moved) return `${moved} exam${moved === 1 ? '' : 's'} moved${kept ? `; ${kept}` : ''}.`;
    return kept ? `No exam was moved; ${kept}.` : 'No exam was moved.';
  },
};

const isPlainObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);

function describeOptimisation(report, { saved = true, panelShown = false } = {}) {
  // Absent or malformed numbers count as none: the save is already done. A
  // number that is not finite, or absurdly large, is not a count either.
  const count = value => {
    const n = Math.max(0, Math.round(Number(value))) || 0;
    return Number.isFinite(n) ? Math.min(n, 999999) : 0;
  };
  const before = isPlainObject(report?.before) ? report.before : {};
  const known = isPlainObject(report?.after) && Object.keys(report.after).length > 0;
  const after = known ? report.after : {};
  const improved = report?.improved === true;
  const arrow = IS_AR ? '←' : '→';
  // Before then after, as the language reads; the arrow is decorative and the
  // hidden words are what a screen reader hears: "from 443 to 85".
  const pair = (from, to) => `<span class="visually-hidden">${IS_AR ? 'من ' : 'from '}</span><bdi dir="ltr">${escapeAttr(from)}</bdi> `
    + `<span aria-hidden="true">${arrow}</span><span class="visually-hidden">${IS_AR ? ' إلى ' : ' to '}</span> <bdi dir="ltr">${escapeAttr(to)}</bdi>`;
  const items = [];
  const line = (label, key) => {
    const was = count(before[key]);
    const now = count(after[key]);
    if (!was && !now) return;
    items.push(`<li>${escapeAttr(label)}: ${saved ? pair(was, now) : `<bdi dir="ltr">${escapeAttr(now)}</bdi>`}</li>`);
  };
  // A saved timetable that is not better has no student numbers to compare:
  // its "before" is the board as submitted, not a worse timetable.
  if (!saved || improved) {
    line(OPTIMISE_TEXT.ruleBreaks(), 'rule_breaks');
    line(OPTIMISE_TEXT.multiExamDay(), 'multi_exam_day');
    line(OPTIMISE_TEXT.overLimit(), 'over_limit');
    line(OPTIMISE_TEXT.heavyDay(), 'heavy_day');
    line(OPTIMISE_TEXT.unseated(), 'unseated');
  }
  // The invigilator peak: only when the server measured both timetables. Shown
  // even when equal, and a rise is a caution.
  const peakNumber = value => (typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.min(Math.round(value), 999999) : null);
  const peak = saved && isPlainObject(report?.invigilator_peak) ? report.invigilator_peak : {};
  const peakBefore = peakNumber(peak.before);
  const peakAfter = peakNumber(peak.after);
  const peakShown = peakBefore !== null && peakAfter !== null;
  if (peakShown) items.push(`<li>${escapeAttr(OPTIMISE_TEXT.invigilatorPeak())}: ${pair(peakBefore, peakAfter)}</li>`);
  const parts = [];
  if (!saved) {
    // A job panel has already said the first sentence; a screen reader must not hear it twice.
    parts.push(`<p>${escapeAttr(panelShown ? OPTIMISE_TEXT.screenStays() : OPTIMISE_TEXT.noBetter())}</p>`);
  } else {
    parts.push(`<p>${escapeAttr(improved ? OPTIMISE_TEXT.found() : OPTIMISE_TEXT.notBetter(count(report?.moved)))}</p>`);
  }
  if (items.length) parts.push(`<ul>${items.join('')}</ul>`);
  if (saved) {
    if (improved) {
      parts.push(`<p>${escapeAttr(OPTIMISE_TEXT.moved(count(report?.moved), count(report?.fixed)))}</p>`);
      // Rule breaks come first, even at the price of an exam in the Overflow.
      const sent = count(after.unseated) - count(before.unseated);
      if (sent > 0) parts.push(`<p>${escapeAttr(OPTIMISE_TEXT.sentToOverflow(sent))}</p>`);
    }
    parts.push(`<p>${escapeAttr(REPAIR_TEXT.saved())}</p>`);
  }
  // Clean only when the server gave the numbers of the board and none is left.
  const clean = known && !count(after.rule_breaks) && !count(after.unseated) && !count(after.over_limit) && !count(after.staff_excess)
    && !(peakShown && peakAfter > peakBefore);
  return { html: parts.join(''), clean };
}

function showOptimisationReport(report, options) {
  // No report to read (the flag is off, or the answer is not an object): say nothing.
  if (!isPlainObject(report)) return;
  const region = $('examRepairReport');
  if (!region) return;
  _repairReportRevision = _editorRevision;
  let summary;
  try {
    summary = describeOptimisation(report, options);
  } catch (_) {
    // The answer is already applied when this renders: a report it cannot
    // read must never turn that into an error.
    summary = { html: `<p>${escapeAttr(options?.saved === false ? T.optimizeNoBetter : T.optimized)}</p>`, clean: false };
  }
  region.classList.toggle('is-clean', summary.clean);
  region.classList.toggle('is-partial', !summary.clean);
  region.innerHTML = summary.html;
}

function clearRepairReport() {
  const region = $('examRepairReport');
  if (!region || !region.innerHTML) return;
  region.innerHTML = '';
  region.classList.remove('is-clean', 'is-partial');
}

/* ── Add courses: new courses into the saved timetable on the board ── */
// A committee builds part of its courses, perfects the board, then adds the
// rest to THAT board. The list shows every course of the timetable's own
// programmes and sections - no Load Courses - with the courses already in it
// marked and never selectable; only the new ones can be ticked. The server
// places them, moving an existing exam only when that is the one way to seat
// a new one, and saves a new timetable beside this one.
const AR_COURSES_GEN = { one: 'مقرر واحد', two: 'مقررين', few: '{n} مقررات', many: '{n} مقرراً', other: '{n} مقرر' };
const AR_ADD_N = { one: 'إضافة مقرر واحد', two: 'إضافة مقررين', few: 'إضافة {n} مقررات', many: 'إضافة {n} مقرراً', other: 'إضافة {n} مقرر' };
const AR_ADDED_N = { one: 'أُضيف مقرر واحد', two: 'أُضيف مقرران', few: 'أُضيفت {n} مقررات', many: 'أُضيف {n} مقرراً', other: 'أُضيف {n} مقرر' };
const AR_MOVED_N = { one: 'نُقل اختبار واحد موجود', two: 'نُقل اختباران موجودان', few: 'نُقلت {n} اختبارات موجودة', many: 'نُقل {n} اختباراً موجوداً', other: 'نُقل {n} اختبار موجود' };
const AR_PERIODS_N = { one: 'فترة واحدة', two: 'فترتين', few: '{n} فترات', many: '{n} فترةً', other: '{n} فترة' };
const AR_GROUPS_N = { one: 'مجموعة واحدة', two: 'مجموعتين', few: '{n} مجموعات', many: '{n} مجموعةً', other: '{n} مجموعة' };
const AR_BREAKS_N = { one: 'مخالفة واحدة موجودة', two: 'مخالفتان موجودتان', few: '{n} مخالفات موجودة', many: '{n} مخالفةً موجودة', other: '{n} مخالفة موجودة' };
const AR_CHOSEN_N = { one: 'مقرر واحد مختار للإضافة', two: 'مقرران مختاران للإضافة', few: '{n} مقررات مختارة للإضافة', many: '{n} مقرراً مختاراً للإضافة', other: '{n} مقرر مختار للإضافة' };

// A course code, a day or a period reads left to right inside Arabic.
const addCode = code => (IS_AR ? isolateLtr(String(code)) : String(code));
const addCodes = codes => (codes || []).map(addCode).join(IS_AR ? '، ' : ', ');
const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

const ADD_TEXT = {
  scope: (programs, sections) => {
    const all = IS_AR ? 'الكل' : 'all';
    const p = programs.length ? programs.join(', ') : all;
    const s = sections.length ? sections.join(', ') : all;
    return IS_AR ? `البرامج: ${isolateLtr(p)} · الشعب: ${isolateLtr(s)}` : `Programmes: ${p} · Sections: ${s}`;
  },
  inTimetable: where => (IS_AR ? `في الجدول · ${isolateLtr(where)}` : `In timetable · ${where}`),
  inTimetableOverflow: IS_AR ? `في الجدول · غير موزَّع (${T.overflow})` : `In timetable · not placed (${T.overflow})`,
  alreadyThere: IS_AR ? 'موجود في هذا الجدول' : 'already in this timetable',
  noRegistrations: IS_AR ? 'لا توجد تسجيلات حالية' : 'no current registrations',
  addsAs: code => (IS_AR ? `يُضاف باسم ${isolateLtr(code)}` : `Added as ${code}`),
  sameCode: codes => (IS_AR ? `الرمز نفسه لـ${addCodes(codes)} في هذا الجدول` : `Same code as ${codes.join(', ')} in this timetable`),
  rowName: (code, name) => (IS_AR ? `إضافة ${isolateLtr(code)} — ${name}` : `Add ${code} — ${name}`),
  count: (shown, total, chosen) => (IS_AR
    ? `عرض ${shown} من ${arabicCount(total, AR_COURSES_GEN)} · ${arabicCount(chosen, AR_CHOSEN_N)}`
    : `Showing ${shown} of ${plural(total, 'course', 'courses')} · ${chosen} chosen to add`),
  submit: count => {
    if (!count) return IS_AR ? 'إضافة المقررات' : 'Add courses';
    return IS_AR ? arabicCount(count, AR_ADD_N) : `Add ${plural(count, 'course', 'courses')}`;
  },
  noneTicked: IS_AR ? 'حدّد مقرراً واحداً على الأقل لإضافته.' : 'Tick at least one course to add.',
  loading: IS_AR ? 'جارٍ تحميل مقررات برامج هذا الجدول وشعبه…' : "Loading the courses of this timetable's programmes and sections…",
  loadFailed: IS_AR ? 'تعذّر تحميل قائمة المقررات.' : 'The course list could not be loaded.',
  nothingToAdd: IS_AR ? 'جميع مقررات هذه البرامج والشعب موجودة في الجدول بالفعل.' : 'Every course of these programmes and sections is already in this timetable.',
  termChanged: IS_AR ? 'بُني هذا الجدول لفصل دراسي آخر. ابنِ جدولاً جديداً لهذا الفصل.' : 'This timetable was built for another academic term. Build a new timetable for this term.',
  missing: codes => (IS_AR
    ? `لم تعد لـ${addCodes(codes)} تسجيلات حالية. ${codes.length === 1 ? 'احذفه' : 'احذفها'} من هذا الجدول واحفظه أولاً، ثم أضف المقررات.`
    : `${codes.join(', ')} ${codes.length === 1 ? 'has' : 'have'} no current registrations. Remove ${codes.length === 1 ? 'it' : 'them'} from this timetable and save it first, then add courses.`),
  badName: IS_AR ? 'أدخل اسماً للجدول الجديد من سطر واحد، من 1 إلى 120 حرفاً.' : 'Enter a single-line name for the new timetable, 1 to 120 characters.',
  // The reason itself, where the registrar is looking, and where to fix it:
  // the status line that also says it sits in the setup section, behind this list.
  cannotSend: reason => (IS_AR
    ? `تعذّر إرسال إعدادات الجدول.${reason ? ` ${reason}` : ''} أغلق القائمة وصحّحها في «إعدادات الجدول والمقررات والمواعيد المثبتة».`
    : `The timetable settings could not be sent.${reason ? ` ${reason}` : ''} Close this list and correct them under Timetable setup, courses and fixed times.`),
  unsaved: IS_AR
    ? 'احفظ تغييراتك، أو افتح الجدول المحفوظ مجدداً من «الجداول المحفوظة» لتجاهلها. تعمل إضافة المقررات على الجدول المحفوظ.'
    : 'Save your changes, or open the saved timetable again from Saved timetables to discard them. Adding courses works on the saved timetable.',
  defaultName: label => {
    const suffix = IS_AR ? ' + مقررات مضافة' : ' + added courses';
    let prefix = '';
    for (const character of String(label || '').trim()) {
      if (prefix.length + character.length > 120 - suffix.length) break;
      prefix += character;
    }
    return prefix.trimEnd() + suffix;
  },
  setupHeading: count => (IS_AR ? `مقررات أخرى في هذه البرامج والشعب (${count})` : `Other courses in these programmes and sections (${count})`),
  setupNone: IS_AR ? 'جميع مقررات هذه البرامج والشعب موجودة في هذا الجدول.' : 'Every course of these programmes and sections is in this timetable.',
  setupLoading: IS_AR ? 'جارٍ تحميل بقية مقررات هذه البرامج والشعب…' : 'Loading the other courses of these programmes and sections…',
  setupFailed: IS_AR ? 'تعذّر تحميل بقية المقررات. أغلق هذا القسم وافتحه مجدداً للمحاولة.' : 'The other courses could not be loaded. Close this section and open it again to retry.',
  inBadge: IS_AR ? 'في الجدول' : 'In timetable',
  outBadge: IS_AR ? 'ليس في هذا الجدول' : 'Not in this timetable',
  setupCount: (shown, total, inRun) => (IS_AR
    ? `عرض ${shown} من ${arabicCount(total, AR_COURSES_GEN)} · ${inRun} في هذا الجدول`
    : `Showing ${shown} of ${plural(total, 'course', 'courses')} · ${inRun} in this timetable`),
  // The report.
  headline: (placed, requested, moved, proven) => {
    if (!moved) {
      if (placed === requested) return IS_AR ? `${arabicCount(placed, AR_ADDED_N)} دون نقل أي اختبار موجود.` : `Added ${plural(placed, 'course', 'courses')}. No existing exam moved.`;
      return IS_AR
        ? `أُضيف ${placed} من ${arabicCount(requested, AR_COURSES_GEN)} دون نقل أي اختبار موجود.`
        : `Added ${placed} of ${plural(requested, 'course', 'courses')}. No existing exam moved.`;
    }
    const added = placed === requested
      ? (IS_AR ? arabicCount(placed, AR_ADDED_N) : `Added ${plural(placed, 'course', 'courses')}`)
      : (IS_AR ? `أُضيف ${placed} من ${arabicCount(requested, AR_COURSES_GEN)}` : `Added ${placed} of ${plural(requested, 'course', 'courses')}`);
    return IS_AR
      ? `${added}. ${arabicCount(moved, AR_MOVED_N)} لإفساح المجال، وهو ${proven ? 'أقل عدد ممكن' : 'أقل عدد وُجد'}.`
      : `${added}. ${plural(moved, 'existing exam', 'existing exams')} moved to make room: the fewest ${proven ? 'possible' : 'found'}.`;
  },
  placedTitle: IS_AR ? 'مواعيد المقررات المضافة' : 'Where the new courses were placed',
  movedTitle: IS_AR ? 'الاختبارات الموجودة التي نُقلت' : 'Existing exams moved',
  madeRoomFor: codes => (IS_AR ? `لإفساح المجال لـ${addCodes(codes)}` : `to make room for ${codes.join(', ')}`),
  notPlacedTitle: IS_AR ? `لم تُوزَّع (${T.overflow})` : `Not placed (${T.overflow})`,
  showAll: count => (IS_AR ? `عرض الكل (${count})` : `Show all ${count}`),
  reason: row => {
    const blocked = row?.blocked_by || {};
    switch (row?.reason) {
      case 'bucket_days_full': {
        const bucket = Array.isArray(blocked.bucket) ? blocked.bucket[0] : null;
        if (bucket?.program) {
          return IS_AR
            ? `لبرنامج ${isolateLtr(bucket.program)} في المستوى ${bucket.programme_term} اختبار في كل يوم متاح.`
            : `${bucket.program} term ${bucket.programme_term} already has an exam on every open day.`;
        }
        return IS_AR ? 'لخطته الدراسية اختبار في كل يوم متاح.' : 'Its study term already has an exam on every open day.';
      }
      case 'blocked_by_fixed_exams': {
        const fixed = Array.isArray(blocked.fixed) ? blocked.fixed : [];
        return IS_AR
          ? `كل فترة متاحة تتعارض مع اختبارات لطلابه لا يمكن نقلها (مثبتة أو مقفلة أو نقلتها أنت${fixed.length ? `: ${addCodes(fixed)}` : ''}).`
          : `Every open period clashes with exams its students sit that cannot move (pinned, locked or moved by you${fixed.length ? `: ${fixed.join(', ')}` : ''}).`;
      }
      case 'only_locked_periods_free':
        return IS_AR ? 'الفترات الوحيدة المتاحة له مقفلة.' : 'The only periods free for it are locked.';
      case 'search_limit':
        return IS_AR ? 'توقف البحث عند حد العمل. استخدم «إصلاح بأقل تغيير» أو ضعه يدوياً.' : 'The search stopped at its work limit. Use Fix with fewest moves or place it by hand.';
      default:
        return IS_AR ? 'تعذّر إفساح مكان له دون نقل مزيد من الاختبارات. استخدم «إصلاح بأقل تغيير» أو ضعه يدوياً.' : 'No place could be made without moving more exams. Use Fix with fewest moves or place it by hand.';
    }
  },
  untouched: count => (IS_AR
    ? `تُركت ${arabicCount(count, AR_BREAKS_N)} في الجدول المحفوظ كما هي. استخدم «إصلاح بأقل تغيير» لإزالتها.`
    : `${plural(count, 'rule break', 'rule breaks')} already in the saved timetable ${count === 1 ? 'was' : 'were'} left as ${count === 1 ? 'it was' : 'they were'}. Use Fix with fewest moves to clear ${count === 1 ? 'it' : 'them'}.`),
  roomsChanged: count => (IS_AR
    ? `أُعيد توزيع القاعات في ${arabicCount(count, AR_PERIODS_N)}، وبقيت بقية القاعات كما حُفظت.`
    : `Rooms were reassigned in ${plural(count, 'period', 'periods')}; all other rooms are as saved.`),
  sectionsChanged: count => (IS_AR
    ? `تغيّرت التسجيلات منذ حفظ هذا الجدول في ${arabicCount(count, AR_GROUPS_N)} من شعب الاختبارات الموجودة.`
    : `Registrations changed since this timetable was saved for ${plural(count, 'section group', 'section groups')} of existing exams.`),
  unassigned: count => (IS_AR
    ? `لم تُخصَّص قاعة بعد لـ${arabicCount(count, AR_GROUPS_N)} من مجموعات المقررات المضافة.`
    : `${plural(count, 'section group', 'section groups')} of the new courses ${count === 1 ? 'has' : 'have'} no room yet.`),
};

// Why the server refused to add courses, in the page's words.
const ADD_REFUSALS = {
  add_courses_invalid: ['The courses to add could not be read. Close the list and open Add courses… again.', 'تعذّرت قراءة المقررات المطلوب إضافتها. أغلق القائمة وافتح «إضافة مقررات…» من جديد.'],
  add_courses_none: ['Tick at least one course to add.', 'حدّد مقرراً واحداً على الأقل لإضافته.'],
  add_courses_source_required: ['Open the saved timetable, then add courses to it.', 'افتح الجدول المحفوظ، ثم أضف المقررات إليه.'],
  add_courses_unsaved_changes: [
    'Save your changes, or open the saved timetable again from Saved timetables to discard them. Adding courses works on the saved timetable.',
    'احفظ تغييراتك، أو افتح الجدول المحفوظ مجدداً من «الجداول المحفوظة» لتجاهلها. تعمل إضافة المقررات على الجدول المحفوظ.',
  ],
  add_courses_term_changed: ['This timetable was built for another academic term. Build a new timetable for this term.', 'بُني هذا الجدول لفصل دراسي آخر. ابنِ جدولاً جديداً لهذا الفصل.'],
  add_courses_already_in_timetable: ['This course is already in the timetable.', 'هذا المقرر موجود في الجدول بالفعل.'],
  add_courses_outside_scope: ["This course has no students in this timetable's programmes and sections.", 'لا يوجد طلاب لهذا المقرر في برامج هذا الجدول وشعبه.'],
  add_courses_unavailable: ['This course has no registrations in the imported student timetables any more. Open Add courses… again to see the current list.', 'لم يعد لهذا المقرر تسجيلات في الجداول الدراسية المستوردة. افتح «إضافة مقررات…» مجدداً لرؤية القائمة الحالية.'],
};

function addCoursesRefusal(data) {
  const message = (ADD_REFUSALS[data.code] || ADD_REFUSALS.add_courses_invalid)[IS_AR ? 1 : 0];
  const courses = Array.isArray(data.courses) ? data.courses.map(String).filter(Boolean) : [];
  const error = new Error(courses.length ? `${message} (${addCodes(courses)})` : message);
  error.examRequestKind = 'add-courses';
  error.examAddCode = data.code;
  return error;
}

// Adding works on the saved board. A name or the shuffle setting is not a
// board edit; anything else Save would save is.
function addCoursesBlockedReason() {
  if (!_currentResultData || _savedEditorSignature === null) return '';
  const board = text => {
    try {
      const value = JSON.parse(text);
      delete value.label;
      delete value.randomize;
      return JSON.stringify(value);
    } catch (_) {
      return text;
    }
  };
  return board(editorSignature()) !== board(_savedEditorSignature) ? ADD_TEXT.unsaved : '';
}

function loadedScheduleShown() {
  return Boolean(_loadedRunForRebuild && _currentResultData && Array.isArray(_currentResultData.schedule) && _currentResultData.schedule.length);
}

async function fetchScopeCourses(runId) {
  const res = await fetch(`/ops/exam-timetable/${encodeURIComponent(runId)}/scope-courses/`, {
    headers: { 'X-CSRFToken': getCsrfToken() || CSRF },
  });
  const data = await readExamResponse(res);
  if (!res.ok || !data.ok) throw examResponseError(data);
  return data;
}

function addSearchText(course) {
  return [course.course_code, course.add_code, course.timetable_course_code, course.source_course_code, course.course_name || '', ...(course.programs || [])]
    .filter(Boolean).join(' ').toLocaleLowerCase();
}

function addPlacementText(course) {
  if (course.placement?.overflow) return ADD_TEXT.inTimetableOverflow;
  const place = course.placement || {};
  return ADD_TEXT.inTimetable([place.day, place.period].filter(Boolean).join(' '));
}

let _addDialog = null;   // { runId, opener, data } while the list is open
let _addDialogSeq = 0;
// What was sent last from the list: { runId, identities, name }. A refusal
// (the server's, or a colleague's job holding the queue) must not cost the
// registrar their ticks - the re-selection the owner asked never to need. Kept
// until a new timetable is saved, or the list is dismissed.
let _addSent = null;

function addCoursesError(message, focus = null) {
  const error = $('examAddCoursesError');
  if (!error) return;
  error.textContent = message;
  error.hidden = !message;
  if (message && focus) focus.focus();
}

function openAddCoursesDialog(opener, { search = '' } = {}) {
  const dialog = $('examAddCoursesDialog');
  // The dialog is rendered only for those who may save: no dialog, no Add.
  if (!dialog || _builderBusy || !loadedScheduleShown() || !_currentRunId) return;
  if (needsExamSourceRebuild()) return;
  const blocked = addCoursesBlockedReason();
  if (blocked) {
    // Said where the registrar is looking, and Save offered: a disabled
    // button would have hidden why.
    const refusal = new Error(blocked);
    refusal.examRequestKind = 'add-courses';
    showExamRequestError(refusal, 'add-courses');
    const save = $('saveLoadedBtn');
    if (save && !save.disabled && !save.classList.contains('d-none')) save.focus();
    return;
  }
  clearExamRequestError('add-courses');
  const seq = ++_addDialogSeq;
  _addDialog = { runId: _currentRunId, opener: opener || $('addCoursesBtn'), data: null };
  const scope = _currentResultData.enrollment_scope || {};
  $('examAddCoursesScope').textContent = ADD_TEXT.scope(scope.programs || [], scope.sections || []);
  $('examAddCoursesSearch').value = search;
  $('examAddCoursesShow').value = 'all';
  const again = _addSent?.runId === _currentRunId ? _addSent : null;
  $('examAddCoursesName').value = again?.name || ADD_TEXT.defaultName($('etLabel').value || _currentResultData.label || '');
  $('examAddCoursesMissing').hidden = true;
  addCoursesError('');
  if (dialog.showModal) dialog.showModal();
  else dialog.setAttribute('open', '');
  $('examAddCoursesSearch').focus();
  loadAddCourses(seq);
}

async function loadAddCourses(seq) {
  const list = $('examAddCoursesList');
  $('retryAddCourses').hidden = true;
  list.setAttribute('aria-busy', 'true');
  list.innerHTML = `<p class="et-add-empty">${escapeAttr(ADD_TEXT.loading)}</p>`;
  $('examAddCoursesCount').textContent = '';
  $('confirmAddCourses').textContent = ADD_TEXT.submit(0);
  let data;
  try {
    // Asked every time the list opens: a course can gain or lose its
    // registrations while the page stays open.
    data = await fetchScopeCourses(_addDialog.runId);
  } catch (err) {
    if (seq !== _addDialogSeq) return;
    list.setAttribute('aria-busy', 'false');
    list.innerHTML = `<p class="et-add-empty">${escapeAttr(ADD_TEXT.loadFailed)}</p>`;
    addCoursesError(err?.examRequestKind === 'auth' ? err.message : ADD_TEXT.loadFailed);
    $('retryAddCourses').hidden = false;
    return;
  }
  if (seq !== _addDialogSeq || !_addDialog) return;
  _addDialog.data = data;
  list.setAttribute('aria-busy', 'false');
  renderAddCourses();
}

function renderAddCourses() {
  const data = _addDialog?.data;
  const list = $('examAddCoursesList');
  if (!data) return;
  const missing = (Array.isArray(data.missing) ? data.missing : []).map(row => String(row.course_code || '')).filter(Boolean);
  $('examAddCoursesMissing').hidden = !missing.length;
  $('examAddCoursesMissing').textContent = missing.length ? ADD_TEXT.missing(missing) : '';
  if (data.term_changed) {
    list.innerHTML = `<p class="et-add-empty">${escapeAttr(ADD_TEXT.termChanged)}</p>`;
    $('examAddCoursesCount').textContent = '';
    return;
  }
  const courses = [...(Array.isArray(data.courses) ? data.courses : [])]
    .sort((a, b) => String(a.in_timetable ? a.timetable_course_code : a.add_code || a.course_code)
      .localeCompare(String(b.in_timetable ? b.timetable_course_code : b.add_code || b.course_code), 'en', { numeric: true }));
  const missingRows = (Array.isArray(data.missing) ? data.missing : []).map(row => ({
    course_code: row.course_code, timetable_course_code: row.course_code, course_identity: row.course_identity, in_timetable: true, missing: true,
  }));
  const addable = courses.filter(course => !course.in_timetable);
  const rows = [...courses, ...missingRows].map(course => {
    const online = course.is_online ? `<span class="et-online-badge">${IS_AR ? 'إلكتروني' : 'Online'}</span>` : '';
    const plans = escapeAttr((course.programs || []).join(' · ') || '—');
    const students = `<span class="et-course-enrollment"><span class="et-mobile-label">${IS_AR ? 'الطلاب' : 'Students'} </span>${escapeAttr(course.enrolled_count ?? '—')}</span>`;
    if (course.in_timetable) {
      const code = course.timetable_course_code || course.course_code;
      const badge = course.missing ? ADD_TEXT.noRegistrations : addPlacementText(course);
      return `<div class="et-add-row is-in" data-search="${escapeAttr(addSearchText(course))}" data-state="in"><span class="et-add-mark" aria-hidden="true">✓</span><span class="et-course-main"><span class="et-course-code" dir="ltr">${escapeAttr(code)}</span>${online}<span class="et-course-name">${escapeAttr(course.course_name || '')}</span><span class="et-add-badge">${escapeAttr(badge)}</span><span class="visually-hidden">${escapeAttr(ADD_TEXT.alreadyThere)}</span></span><span class="et-course-plans">${plans}</span>${students}</div>`;
    }
    const code = course.add_code || course.course_code;
    const renumbered = code !== course.course_code ? `<span class="et-add-note">${escapeAttr(ADD_TEXT.addsAs(code))}</span>` : '';
    const shared = Array.isArray(course.same_code_as) && course.same_code_as.length ? `<span class="et-add-note">${escapeAttr(ADD_TEXT.sameCode(course.same_code_as))}</span>` : '';
    return `<label class="et-add-row" data-search="${escapeAttr(addSearchText(course))}" data-state="out"><input type="checkbox" value="${escapeAttr(course.course_identity)}" data-code="${escapeAttr(code)}" aria-label="${escapeAttr(ADD_TEXT.rowName(code, course.course_name || code))}"><span class="et-course-main"><span class="et-course-code" dir="ltr">${escapeAttr(code)}</span>${online}<span class="et-course-name">${escapeAttr(course.course_name || '')}</span>${renumbered}${shared}</span><span class="et-course-plans">${plans}</span>${students}</label>`;
  });
  list.innerHTML = rows.join('') + (addable.length ? '' : `<p class="et-add-empty">${escapeAttr(ADD_TEXT.nothingToAdd)}</p>`);
  // Sent before and refused: ticked again - only those still addable.
  const again = _addSent?.runId === _addDialog?.runId ? new Set(_addSent.identities) : null;
  list.querySelectorAll('input[type=checkbox]').forEach(box => {
    if (again?.has(box.value)) box.checked = true;
  });
  list.querySelectorAll('input[type=checkbox]').forEach(box => box.addEventListener('change', () => {
    addCoursesError('');
    filterAddCourses();
  }));
  filterAddCourses();
}

function filterAddCourses() {
  const list = $('examAddCoursesList');
  const rows = [...list.querySelectorAll('.et-add-row')];
  const terms = $('examAddCoursesSearch').value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const show = $('examAddCoursesShow').value;
  let shown = 0;
  for (const row of rows) {
    const box = row.querySelector('input[type=checkbox]');
    const state = show === 'all' || (show === 'addable' && box) || (show === 'chosen' && box?.checked);
    row.hidden = !state || !terms.every(term => row.dataset.search.includes(term));
    if (!row.hidden) shown += 1;
  }
  const chosen = list.querySelectorAll('input[type=checkbox]:checked').length;
  $('examAddCoursesCount').textContent = _addDialog?.data?.term_changed ? '' : ADD_TEXT.count(shown, rows.length, chosen);
  $('confirmAddCourses').textContent = ADD_TEXT.submit(chosen);
}

function closeAddCoursesDialog({ restoreFocus = true, dismissed = false } = {}) {
  const dialog = $('examAddCoursesDialog');
  if (!dialog) return;
  if (dismissed) _addSent = null;
  const opener = _addDialog?.opener;
  _addDialogSeq += 1;
  _addDialog = null;
  if (dialog.close) dialog.close();
  else dialog.removeAttribute('open');
  $('examAddCoursesList').innerHTML = '';
  if (restoreFocus && opener?.isConnected) opener.focus({ preventScroll: true });
}

function submitAddCourses() {
  const data = _addDialog?.data;
  if (!data) return;
  if (data.term_changed) { addCoursesError(ADD_TEXT.termChanged); return; }
  const missing = (Array.isArray(data.missing) ? data.missing : []).map(row => String(row.course_code || '')).filter(Boolean);
  if (missing.length) { addCoursesError(ADD_TEXT.missing(missing)); return; }
  const ticked = [...$('examAddCoursesList').querySelectorAll('input[type=checkbox]:checked')];
  if (!ticked.length) { addCoursesError(ADD_TEXT.noneTicked, $('examAddCoursesSearch')); return; }
  const name = $('examAddCoursesName').value.trim();
  if (!name || name.length > 120 || [...name].some(character => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)) { addCoursesError(ADD_TEXT.badName, $('examAddCoursesName')); return; }
  // Built and checked before the list closes: a refusal here keeps the ticks.
  // The name is the list's own; the setup's name field is not asked for.
  $('etStatus').textContent = '';
  const payload = collectLoadedRunPayload('add_courses', { label: name });
  if (!payload) { addCoursesError(ADD_TEXT.cannotSend($('etStatus').textContent.trim())); return; }
  // Only what was ticked: a course already in the timetable is never sent.
  payload.added_courses = ticked.map(box => ({ course_identity: box.value, course_code: box.dataset.code }));
  _addSent = { runId: _addDialog.runId, identities: ticked.map(box => box.value), name };
  closeAddCoursesDialog({ restoreFocus: false });
  runLoadedRunAction('add_courses', $('addCoursesBtn'), T.addingCourses, T.coursesAdded, payload);
}

$('addCoursesBtn')?.addEventListener('click', event => openAddCoursesDialog(event.currentTarget));
$('courseOutsideAdd')?.addEventListener('click', event => openAddCoursesDialog(event.currentTarget, { search: $('courseSearch').value.trim() }));
$('examAddCoursesSearch')?.addEventListener('input', filterAddCourses);
$('examAddCoursesShow')?.addEventListener('change', filterAddCourses);
$('confirmAddCourses')?.addEventListener('click', submitAddCourses);
$('cancelAddCourses')?.addEventListener('click', () => closeAddCoursesDialog({ dismissed: true }));
$('retryAddCourses')?.addEventListener('click', () => {
  if (!_addDialog) return;
  addCoursesError('');
  loadAddCourses(++_addDialogSeq);
});
$('examAddCoursesDialog')?.addEventListener('cancel', event => { event.preventDefault(); closeAddCoursesDialog({ dismissed: true }); });

/* The setup list of a saved timetable: its own courses, then the rest of its
   scope, found by the same search. Fetched when the registrar opens the setup
   section on that timetable, or searches it - never on load - and forgotten
   when the run changes. */
let _outsideRunId = null;
let _outsideSeq = 0;

function clearOutsideCourses() {
  _outsideRunId = null;
  _outsideSeq += 1;
  const group = $('courseOutsideGroup');
  if (!group) return;
  group.hidden = true;
  $('courseOutsideList').innerHTML = '';
  $('courseOutsideStatus').textContent = '';
  $('courseOutsideHeading').textContent = '';
}

async function ensureOutsideCourses() {
  const group = $('courseOutsideGroup');
  if (!group || !$('examSetupDetails')?.open) return;
  if (!loadedScheduleShown() || !_currentRunId || _builderBusy || needsExamSourceRebuild() || _sourceCoursesRejected) return;
  if (_outsideRunId === _currentRunId) return;
  const runId = _currentRunId;
  _outsideRunId = runId;
  const seq = ++_outsideSeq;
  group.hidden = false;
  $('courseOutsideHeading').textContent = ADD_TEXT.setupHeading('…');
  $('courseOutsideStatus').textContent = ADD_TEXT.setupLoading;
  $('courseOutsideList').innerHTML = '';
  let data;
  try {
    data = await fetchScopeCourses(runId);
  } catch (_) {
    if (seq !== _outsideSeq) return;
    _outsideRunId = null;
    $('courseOutsideStatus').textContent = ADD_TEXT.setupFailed;
    return;
  }
  if (seq !== _outsideSeq || runId !== _currentRunId) return;
  const outside = (Array.isArray(data.courses) ? data.courses : []).filter(course => !course.in_timetable)
    .sort((a, b) => String(a.add_code || a.course_code).localeCompare(String(b.add_code || b.course_code), 'en', { numeric: true }));
  $('courseOutsideHeading').textContent = ADD_TEXT.setupHeading(outside.length);
  $('courseOutsideStatus').textContent = outside.length ? '' : ADD_TEXT.setupNone;
  $('courseOutsideList').innerHTML = outside.map(course => {
    const code = course.add_code || course.course_code;
    const online = course.is_online ? `<span class="et-online-badge">${IS_AR ? 'إلكتروني' : 'Online'}</span>` : '';
    return `<div class="et-course-option et-course-outside-row" role="listitem" data-search="${escapeAttr(addSearchText(course))}" data-online="${course.is_online ? '1' : '0'}">
      <span class="et-add-mark" aria-hidden="true">+</span>
      <span class="et-course-main"><span class="et-course-code" dir="ltr">${escapeAttr(code)}</span>${online}<span class="et-add-badge is-out">${escapeAttr(ADD_TEXT.outBadge)}</span><span class="et-course-name">${escapeAttr(course.course_name || code)}</span></span>
      <span class="et-course-plans">${escapeAttr((course.programs || []).join(' · ') || '—')}</span>
      <span class="et-course-level"><span class="et-mobile-label">${IS_AR ? 'الساعات' : 'Credits'} </span>${escapeAttr(course.credit_hours || '—')}</span>
      <span class="et-course-enrollment"><span class="et-mobile-label">${IS_AR ? 'الطلاب' : 'Students'} </span>${escapeAttr(course.enrolled_count ?? 0)}</span>
    </div>`;
  }).join('');
  filterCourseList();
}

// When the registrar opens the setup section, or searches its courses. Not
// when the page opens it to point at a field: that is not a request to read
// anything. The section toggles after the click, so it is read afterwards.
$('examSetupSummary')?.addEventListener('click', () => {
  setTimeout(() => { if ($('examSetupDetails').open) ensureOutsideCourses(); }, 0);
});
$('courseSearch')?.addEventListener('focus', () => ensureOutsideCourses());

/* The report of an Add, where Fix's report goes. */
function addReportItems(items, render) {
  const shown = items.slice(0, 6).map(render).join('');
  const rest = items.length > 6
    ? `<details><summary>${escapeAttr(ADD_TEXT.showAll(items.length))}</summary><ul>${items.slice(6).map(render).join('')}</ul></details>`
    : '';
  return `<ul>${shown}</ul>${rest}`;
}

function describeAddCourses(report) {
  const added = (Array.isArray(report?.added) ? report.added : []).filter(row => row && typeof row.course_code === 'string');
  const moves = (Array.isArray(report?.moves) ? report.moves : []).filter(move => move && typeof move.course_code === 'string' && move.from && move.to);
  const notPlaced = (Array.isArray(report?.not_placed) ? report.not_placed : []).filter(row => row && typeof row.course_code === 'string');
  const requested = Number(report?.requested_count) || added.length;
  const placed = Number.isFinite(Number(report?.placed_count)) ? Number(report.placed_count) : added.filter(row => row.placed).length;
  const parts = [`<p><strong>${escapeAttr(ADD_TEXT.headline(placed, requested, moves.length, Boolean(report?.proven_minimal)))}</strong></p>`];
  const onBoard = added.filter(row => row.placed);
  if (onBoard.length) {
    const entries = new Map((_currentResultData?.schedule || []).map(entry => [entry.course_code, entry]));
    parts.push(`<p class="et-add-report-title">${escapeAttr(ADD_TEXT.placedTitle)}</p>`);
    parts.push(addReportItems(onBoard, row => {
      const entry = entries.get(row.course_code);
      const label = `${IS_AR ? 'إظهار في الجدول' : 'Find in timetable'}: ${row.course_code} — ${entry?.course_name || ''}`;
      const find = entry
        ? ` <button type="button" class="et-add-find" data-find-exam="${escapeAttr(entry.course_identity || entry.course_code)}" aria-label="${escapeAttr(label)}">${IS_AR ? 'إظهار' : 'Find'}</button>`
        : '';
      return `<li><strong><bdi dir="ltr">${escapeAttr(row.course_code)}</bdi></strong> <bdi dir="ltr">${escapeAttr(examLocation(row.placed))}</bdi>${find}</li>`;
    }));
  }
  if (moves.length) {
    parts.push(`<p class="et-add-report-title">${escapeAttr(ADD_TEXT.movedTitle)}</p>`);
    parts.push(addReportItems(moves, move => {
      const why = Array.isArray(move.made_room_for) && move.made_room_for.length
        ? ` <span class="et-add-why">${escapeAttr(ADD_TEXT.madeRoomFor(move.made_room_for))}</span>` : '';
      return repairMoveItem(move).replace(/<\/li>$/, `${why}</li>`);
    }));
  }
  if (notPlaced.length) {
    parts.push(`<p class="et-add-report-title">${escapeAttr(ADD_TEXT.notPlacedTitle)}</p>`);
    parts.push(addReportItems(notPlaced, row => `<li><strong><bdi dir="ltr">${escapeAttr(row.course_code)}</bdi></strong> — ${escapeAttr(ADD_TEXT.reason(row))}</li>`));
  }
  const untouched = Number(report?.untouched_violations) || 0;
  if (untouched) parts.push(`<p>${escapeAttr(ADD_TEXT.untouched(untouched))}</p>`);
  const locked = Number(report?.locked_count) || 0;
  if (locked) parts.push(`<p class="et-repair-locked">${escapeAttr(LOCK_TEXT.fixKept(locked))}</p>`);
  const rooms = Array.isArray(report?.rooms_changed) ? report.rooms_changed : [];
  const periods = new Set(rooms.map(row => `${row?.day}|${row?.period}`)).size;
  if (periods) parts.push(`<p>${escapeAttr(ADD_TEXT.roomsChanged(periods))}</p>`);
  const sections = Number(report?.sections_changed) || 0;
  if (sections) parts.push(`<p>${escapeAttr(ADD_TEXT.sectionsChanged(sections))}</p>`);
  const unassigned = Number(report?.unassigned_added) || 0;
  if (unassigned) parts.push(`<p>${escapeAttr(ADD_TEXT.unassigned(unassigned))}</p>`);
  const linkedClash = Number(report?.linked_clash_students) || 0;
  if (linkedClash) parts.push(`<p class="et-repair-linked">${escapeAttr(REPAIR_TEXT.linkedClash(linkedClash))}</p>`);
  parts.push(`<p>${escapeAttr(REPAIR_TEXT.saved())}</p>`);
  return { html: parts.join(''), clean: !notPlaced.length && !untouched && !unassigned };
}

// The status line of a finished Add: never "added" when some were not - a
// screen reader hears it beside the report, and the two must agree.
function addCoursesStatus(report) {
  const requested = Number(report?.requested_count);
  const placed = Number(report?.placed_count);
  if (Number.isFinite(requested) && Number.isFinite(placed) && placed < requested) return T.coursesSavedNotAll;
  return T.coursesAdded;
}

function showAddCoursesReport(report) {
  const region = $('examRepairReport');
  if (!region) return;
  _repairReportRevision = _editorRevision;
  let summary;
  try {
    summary = describeAddCourses(report);
  } catch (_) {
    // The courses are already added when this renders: a report it cannot
    // read must never turn that into an error.
    summary = { html: `<p>${escapeAttr(T.coursesAdded)}</p>`, clean: false };
  }
  region.classList.toggle('is-clean', summary.clean);
  region.classList.toggle('is-partial', !summary.clean);
  region.innerHTML = summary.html;
  updateDrillActionAvailability();
}

function examLocation(entry) {
  if (!entry) return IS_AR ? 'غير مثبت' : 'Not pinned';
  if (entry.day === 'OVERFLOW') return T.overflow;
  return `${entry.day} · ${entry.period}`;
}

function examActionMarkup(entry, { allowStaleMove = false, allowMove = true, compact = false, courseLabel = '' } = {}) {
  const identity = entry?.course_identity || entry?.course_code || '';
  const label = courseLabel || `${entry?.course_code || ''} — ${entry?.course_name || ''}`;
  const findLabel = IS_AR ? 'إظهار في الجدول' : 'Find in timetable';
  const moveLabel = IS_AR ? 'نقل' : 'Move';
  const description = action => escapeAttr(`${action}: ${label}`);
  const note = allowMove ? '<small class="et-drill-action-note"></small>' : '';
  return `<span class="et-drill-actions${compact ? ' et-course-card-actions' : ''}"><button type="button" class="btn btn-outline-secondary" data-find-exam="${escapeAttr(identity)}" aria-label="${description(findLabel)}" title="${description(findLabel)}">${compact ? examCourseIcon('find') : findLabel}</button>${allowMove ? `<button type="button" class="btn btn-outline-secondary" data-move-exam="${escapeAttr(identity)}"${allowStaleMove ? ' data-allow-unchecked="true"' : ''} aria-label="${description(moveLabel)}" title="${description(moveLabel)}">${compact ? examCourseIcon('move') : moveLabel}</button>` : ''}${compact ? '' : note}</span>${compact ? note : ''}`;
}

// Exams the draft has placed elsewhere than the saved run (selected courses).
function movedSinceSave() {
  if (!_currentResultData || !_savedResultData) return [];
  const selected = new Set(getCheckedValues('courseList'));
  const baseline = new Map((_savedResultData.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
  return (_currentResultData.schedule || []).filter(entry => {
    const before = baseline.get(entry.course_identity || entry.course_code);
    return selected.has(entry.course_code) && before && (before.day !== entry.day || before.period !== entry.period);
  });
}

function updateChangeReview() {
  if (!$('examChangesContent') || !_currentResultData || !_savedResultData) return;
  const focused = document.activeElement;
  const focusedAction = focused?.closest?.('#examChangesContent') && (focused.hasAttribute('data-find-exam') ? 'data-find-exam' : focused.hasAttribute('data-move-exam') ? 'data-move-exam' : null);
  const focusedIdentity = focusedAction ? focused.getAttribute(focusedAction) : null;
  const focusedKind = focused?.closest?.('[data-change-kind]')?.dataset.changeKind;
  const baseline = new Map((_savedResultData.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
  const moved = movedSinceSave();
  const savedPins = new Map((_savedResultData.pinned || []).map(pin => {
    const course = (_savedResultData.schedule || []).find(entry => entry.course_code === pin.course_code);
    return [pin.course_identity || course?.course_identity || pin.course_code, pin];
  }));
  const pinChanges = [...new Set([...savedPins.keys(), ...Object.keys(_pinnedCourses)])].filter(identity => {
    const before = savedPins.get(identity);
    const after = _pinnedCourses[identity];
    return (!before !== !after) || (before && after && (before.day !== after.day || before.period !== after.period));
  });
  const linkChanges = linkChangesSinceSave();
  const lockChanges = lockChangesSinceSave();
  $('examChangeCount').textContent = (IS_AR ? `${moved.length} منقول · ${pinChanges.length} تغيير تثبيت` : `${moved.length} moved · ${pinChanges.length} pin changes`)
    + (linkChanges.length ? (IS_AR ? ` · ${linkChanges.length} تغيير ربط` : ` · ${linkChanges.length} link changes`) : '')
    + (lockChanges.length ? LOCK_TEXT.changeCount(lockChanges.length) : '');
  const changeTotal = moved.length + pinChanges.length + linkChanges.length + lockChanges.length;
  $('examChangesSummary').textContent = IS_AR ? `مراجعة التغييرات (${changeTotal})` : `Review changes (${changeTotal})`;
  const row = (entry, before, after, kind) => `<li class="et-change-item" data-change-identity="${escapeAttr(entry.course_identity || entry.course_code)}" data-change-kind="${kind}"><span class="et-change-course"><strong>${escapeAttr(entry.course_code)}</strong><small class="et-course-name">${escapeAttr(entry.course_name || '')}</small></span><span class="et-change-times"><span>${IS_AR ? 'المحفوظ: ' : 'Saved: '}<bdi dir="ltr">${escapeAttr(examLocation(before))}</bdi></span><span aria-hidden="true"> ${IS_AR ? '←' : '→'} </span><span>${IS_AR ? 'الحالي: ' : 'Current: '}<bdi dir="ltr">${escapeAttr(examLocation(after))}</bdi></span></span>${examActionMarkup(entry, { allowStaleMove: true })}</li>`;
  const movedRows = moved.map(entry => row(entry, baseline.get(entry.course_identity || entry.course_code), entry, 'placement')).join('');
  const pinRows = pinChanges.map(identity => {
    const entry = currentExamByIdentity(identity) || baseline.get(identity) || _pinnedCourses[identity] || savedPins.get(identity);
    return row(entry, savedPins.get(identity), _pinnedCourses[identity], 'pin');
  }).join('');
  const linkRows = linkChanges.map(change => {
    const codes = change.members.map(member => `<bdi dir="ltr">${escapeAttr(member.course_code)}</bdi>`).join(' + ');
    const names = change.members.map(member => member.course_name).filter(Boolean).join(IS_AR ? '، ' : ', ');
    const state = linked => linked ? (IS_AR ? 'مرتبطة' : 'Linked') : (IS_AR ? 'غير مرتبطة' : 'Not linked');
    const first = currentExamByIdentity(change.members[0].course_identity) || change.members[0];
    // Find and Move act on the first course: on the whole link only while it is one.
    const actions = examActionMarkup(first, { allowStaleMove: true, courseLabel: change.saved ? '' : change.members.map(member => member.course_code).join(' + ') });
    return `<li class="et-change-item" data-change-identity="${escapeAttr(change.members[0].course_identity)}" data-change-kind="link" data-change-link="${escapeAttr(change.key)}"><span class="et-change-course"><strong>${codes}</strong><small class="et-course-name">${escapeAttr(names)}</small></span><span class="et-change-times"><span>${IS_AR ? 'المحفوظ: ' : 'Saved: '}${state(change.saved)}</span><span aria-hidden="true"> ${IS_AR ? '←' : '→'} </span><span>${IS_AR ? 'الحالي: ' : 'Current: '}${state(!change.saved)}</span></span>${actions}</li>`;
  }).join('');
  // A lock is a day or a period of it: listed by where it is, never by course.
  const lockRows = lockChanges.map(change => {
    const where = change.period
      ? `<bdi dir="ltr">${escapeAttr(`${change.day} · ${change.period}`)}</bdi>`
      : `<bdi dir="ltr">${escapeAttr(change.day)}</bdi> <small>${escapeAttr(LOCK_TEXT.wholeDay)}</small>`;
    return `<li class="et-change-item et-change-lock" data-change-kind="lock" data-change-lock="${escapeAttr(lockItemKey(change))}"><span class="et-change-course"><strong class="et-lock-where">${lockIcon(!change.saved)}${where}</strong></span><span class="et-change-times"><span>${IS_AR ? 'المحفوظ: ' : 'Saved: '}${LOCK_TEXT.lockedState(change.saved)}</span><span aria-hidden="true"> ${IS_AR ? '←' : '→'} </span><span>${IS_AR ? 'الحالي: ' : 'Current: '}${LOCK_TEXT.lockedState(!change.saved)}</span></span></li>`;
  }).join('');
  const anyLinks = _linkedExams.length || (_savedResultData.linked_exams || []).length;
  $('examChangesContent').innerHTML = (movedRows ? `<section class="et-change-group"><h4>${IS_AR ? 'مواعيد الاختبارات' : 'Exam placements'}</h4><ul class="et-change-list">${movedRows}</ul></section>` : '')
    + (pinRows ? `<section class="et-change-group"><h4>${IS_AR ? 'تغييرات التثبيت' : 'Pin changes'}</h4><ul class="et-change-list">${pinRows}</ul></section>` : '')
    + (linkRows ? `<section class="et-change-group" data-change-group="links"><h4>${IS_AR ? 'الاختبارات المرتبطة' : 'Linked exams'}</h4><ul class="et-change-list">${linkRows}</ul></section>` : '')
    + (lockRows ? `<section class="et-change-group" data-change-group="locks"><h4>${escapeAttr(LOCK_TEXT.changesGroup)}</h4><ul class="et-change-list">${lockRows}</ul></section>` : '')
    + (!movedRows && !pinRows && !linkRows && !lockRows ? `<p class="et-change-empty">${anyLinks
      ? (IS_AR ? 'لم تتغير مواعيد الاختبارات أو تثبيتاتها أو روابطها عن الجدول المحفوظ.' : 'Exam placements, pins and linked exams match the saved timetable.')
      : (IS_AR ? 'لم تتغير مواعيد الاختبارات أو تثبيتاتها عن الجدول المحفوظ.' : 'Exam placements and pins match the saved timetable.')}</p>` : '');
  if (focusedAction) [...$('examChangesContent').querySelectorAll(`[${focusedAction}]`)].find(button => button.getAttribute(focusedAction) === focusedIdentity && button.closest('[data-change-kind]')?.dataset.changeKind === focusedKind)?.focus({ preventScroll: true });
}

// Calculation freshness and persistence are independent. Only the saved
// signature controls export; a successful check never marks a draft saved.
function updateEditingStatus() {
  const hasSchedule = Boolean(_currentResultData?.schedule?.length);
  const sourceRebuild = needsExamSourceRebuild();
  const fresh = hasSchedule && hasCurrentEvaluation();
  const state = sourceRebuild ? 'source' : _checkState;
  if ($('examEditToolbar')) $('examEditToolbar').classList.toggle('d-none', !hasSchedule);
  if ($('checkDraftBtn')) $('checkDraftBtn').disabled = !hasSchedule || _builderBusy || sourceRebuild;
  $('examSourceReviewNotice').hidden = !sourceRebuild;
  if ($('undoExamBtn')) $('undoExamBtn').disabled = _builderBusy || !_undoCommands.length;
  if ($('redoExamBtn')) $('redoExamBtn').disabled = _builderBusy || !_redoCommands.length;
  const dirtyLabel = _scheduleHasDraftMoves
    ? (IS_AR ? ' · تغييرات غير محفوظة' : ' · Unsaved changes')
    : (IS_AR ? ' · محفوظ' : ' · Saved');
  const labels = {
    checked: IS_AR ? 'تم التحقق' : 'Checked',
    stale: IS_AR ? 'تغييرات لم يتم التحقق منها' : 'Changes not checked',
    loading: IS_AR ? 'جارٍ التحقق…' : 'Checking…',
    error: IS_AR ? 'تعذر التحقق — أعد المحاولة' : 'Check failed — retry',
    waiting: IS_AR ? 'في انتظار الخادم' : 'Waiting for the server',
    review: IS_AR ? 'بيانات المصدر تحتاج مراجعة' : 'Source data needs review',
    source: IS_AR ? 'مراجعة مصدر المقررات مطلوبة' : 'Course source review required',
  };
  // A lock change moves the input fingerprint on purpose (the locks are part
  // of what Save keeps): that review is worded as the lock change it is.
  const lockReview = _currentResultData && lockChangesSinceSave().length > 0;
  if (lockReview) labels.review = LOCK_TEXT.reviewLabel;
  const checkStatus = $('examCheckStatus');
  const checkText = hasSchedule ? (labels[state] || labels.stale) + dirtyLabel : '';
  if (checkStatus && checkStatus.textContent !== checkText) checkStatus.textContent = checkText;
  $('etResults').dataset.calculationState = hasSchedule ? state : '';
  for (const id of ['examSummaryCards', 'kStatusBanner', 'kpiDrill']) {
    $(id)?.classList.toggle('et-calculation-stale', hasSchedule && !fresh);
  }
  const banner = $('draftImpactBanner');
  if (banner) {
    banner.classList.toggle('d-none', !hasSchedule || sourceRebuild || (fresh && state !== 'review'));
    const bannerText = state === 'review'
      ? (lockReview ? LOCK_TEXT.reviewBanner : IS_AR ? 'انقر «تحقق من التغييرات» لمراجعة بيانات المصدر المحدثة قبل الحفظ.' : 'Click Check changes to review the updated source data before saving.')
      : (state === 'error' || state === 'waiting') && _checkError ? _checkError : (IS_AR
      ? 'الأرقام والتفاصيل المعروضة تخص آخر تحقق. تحقق من التغييرات لتحديثها؛ لن يتم نقل أي اختبار.'
      : 'Cards and details show the last checked arrangement. Check changes to update them; no exam will be moved.');
    if (banner.textContent !== bannerText) banner.textContent = bannerText;
  }
  if ($('examLiveMode')) $('examLiveMode').textContent = $('examLiveUpdate')?.checked ? (IS_AR ? 'التحديث المباشر: مفعّل' : 'Live update: on') : (IS_AR ? 'التحديث المباشر: متوقف' : 'Live update: off');
  if ($('examRunSummary') && _currentResultData) {
    const courseCount = _currentResultData.courses_count ?? _currentResultData.schedule?.length ?? 0;
    const studentCount = _currentResultData.students_count ?? 0;
    $('examRunSummary').textContent = `${$('etLabel').value.trim()} · ` + (IS_AR ? `${courseCount} مقرر · ${studentCount} طالب` : `${courseCount} courses · ${studentCount} students`);
    if ($('examSetupSummary')) {
      let meta = $('examSetupSummary').querySelector('.et-details-meta');
      if (!meta) { meta = document.createElement('span'); meta.className = 'et-details-meta'; $('examSetupSummary').appendChild(meta); }
      meta.textContent = `${$('etLabel').value.trim()} · ` + (IS_AR ? `${courseCount} مقرر` : `${courseCount} courses`);
    }
  }
  updateMetricComparisons();
  updateChangeReview();
  updateSectionMappingNotice();
  updateDrillActionAvailability();
  updateExportState();
  // The links are the editor's, as the board draws them: a Link or Unlink is
  // shown in Shared students before any Check.
  examReview?.update(_currentResultData && { ..._currentResultData, linked_exams: linkPayload() }, {
    stale: !fresh, blocked: sourceRebuild || _sourceCoursesRejected,
    visibleCodes: _coursesLoaded ? getCheckedValues('courseList') : null,
  });
  window.ExamRosterDrawer?.paintLinks();
  renderLinkWarnings();
  updateLinkBar();
  renderLockLine();
  renderLockBar();
}

function cancelDraftChecks() {
  clearTimeout(_checkTimer);
  _checkTimer = null;
  _queuedCheck = false;
  _draftImpactSeq++;
}

function scheduleDraftCheck({ delay = LIVE_UPDATE_DELAY } = {}) {
  clearTimeout(_checkTimer);
  _checkTimer = null;
  if (!_currentResultData || needsExamSourceRebuild() || _builderBusy || !_scheduleHasDraftMoves
      || _evaluatedEditorSignature === editorSignature() || !$('examLiveUpdate')?.checked) return;
  _checkTimer = setTimeout(() => {
    _checkTimer = null;
    refreshDraftImpact();
  }, delay);
}

function updateCurrentScheduleMove(courseCode, day, period) {
  const entry = _currentResultData?.schedule?.find(item => item.course_code === courseCode);
  if (!entry) return;
  const slot = _currentResultData.slots.find(item => item.day === day && item.period === period);
  if (!slot) return;
  entry.day = day;
  entry.period = period;
  entry.slot_index = slot.index;
}

async function refreshDraftImpact({ force = false, review = false } = {}) {
  clearTimeout(_checkTimer);
  _checkTimer = null;
  if (!_currentResultData?.schedule?.length || needsExamSourceRebuild()) return false;
  if (_checkPromise) {
    _queuedCheck = true;
    await _checkPromise;
    // A waiting manual Check/Save must review the latest revision itself.
    if (force && (_evaluatedEditorSignature !== editorSignature() || (review && _currentResultData?.input_fingerprint !== _reviewedInputFingerprint))) return refreshDraftImpact({ force: true, review });
    return _evaluatedEditorSignature === editorSignature();
  }
  _checkRequestError = null;
  const payload = collectLoadedRunPayload('check_draft');
  if (!payload) {
    const workspaceAnchor = captureExamWorkspaceAnchor();
    _checkState = 'error';
    _checkError = $('etStatus').textContent;
    updateEditingStatus();
    restoreExamWorkspaceAnchor(workspaceAnchor);
    return false;
  }
  const revision = _editorRevision;
  const runId = _currentRunId;
  const signature = editorSignature();
  const seq = ++_draftImpactSeq;
  payload.editor_revision = revision;
  // A live retry while the solver is busy keeps saying it is waiting: it does
  // not flash "Checking…" and back on every attempt.
  if (force || _checkState !== 'waiting') {
    const loadingAnchor = captureExamWorkspaceAnchor();
    _checkState = 'loading';
    _checkError = '';
    updateEditingStatus();
    restoreExamWorkspaceAnchor(loadingAnchor);
  }
  const task = (async () => {
    try {
      const res = await fetch('/ops/exam-timetable/draft-impact/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() || CSRF },
        body: JSON.stringify(payload),
      });
      const data = await readExamResponse(res);
      if (seq !== _draftImpactSeq || runId !== _currentRunId || revision !== _editorRevision || signature !== editorSignature()) return false;
      if (!res.ok || !data.ok) {
        const error = examResponseError(data);
        const retryAfter = Number(res.headers?.get?.('Retry-After'));
        if (retryAfter > 0) error.examRetryAfter = retryAfter * 1000;
        throw error;
      }
      if (data.editor_revision !== revision) throw new Error(IS_AR ? 'استجابة تحقق قديمة. أعد التحقق.' : 'The check response does not match this edit. Check again.');
      const checkedPlacements = new Map((data.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
      if (checkedPlacements.size !== payload.base_schedule.length || payload.base_schedule.some(entry => {
        const checked = checkedPlacements.get(entry.course_identity || entry.course_code);
        return !checked || checked.day !== entry.day || checked.period !== entry.period;
      })) throw new Error(IS_AR ? 'لا تطابق نتيجة التحقق المواعيد الحالية. أعد المحاولة.' : 'The check result does not match the current placements. Retry.');
      const workspaceAnchor = captureExamWorkspaceAnchor();
      clearExamRequestError('check');
      clearExamCourseSourceError();
      _evaluatedEditorSignature = signature;
      _checkState = 'checked';
      renderResults(data, { evaluation: true, preserveViewport: false });
      if (review) _reviewedInputFingerprint = data.input_fingerprint;
      else if (!_reviewedInputFingerprint || data.input_fingerprint !== _reviewedInputFingerprint) _checkState = 'review';
      updateLoadedRunActions();
      restoreExamWorkspaceAnchor(workspaceAnchor);
      return true;
    } catch (err) {
      if (seq === _draftImpactSeq && runId === _currentRunId && revision === _editorRevision) {
        const workspaceAnchor = captureExamWorkspaceAnchor();
        _checkRequestError = err;
        let changed = true;
        if (err.examRequestKind === 'solver-busy') {
          // Not a failure: the solver is someone else's for now. Say so where
          // the board is, show the job holding it, and check again after.
          // Live, it tries again by itself; Check and Save were asked for, so
          // the registrar is told to ask again - each in its own words.
          let waitText = JOB_TEXT.checkWaitLive(err.examHolder);
          if (force) waitText = review ? JOB_TEXT.checkWaitManual(err.examHolder) : JOB_TEXT.saveWait(err.examHolder);
          changed = _checkState !== 'waiting' || _checkError !== waitText;
          _checkState = 'waiting';
          _checkError = waitText;
          _checkWaitLive = !force;
          if (!force) scheduleDraftCheck({ delay: err.examRetryAfter || JOB_POLL.checkRetry });
          // An ended panel still on screen is not the job holding the solver.
          if (err.examHolder?.kind === 'exam_job' && (!_jobFollow || _jobFollow.outcome)) resumeExamJob();
        } else {
          _checkState = 'error';
          _checkError = showExamRequestError(err, 'check');
        }
        // Unchanged, repainting would only make the board's live lines speak again.
        if (changed) updateLoadedRunActions();
        restoreExamWorkspaceAnchor(workspaceAnchor);
      }
      return false;
    }
  })();
  _checkPromise = task;
  try { return await task; }
  finally {
    if (_checkPromise === task) _checkPromise = null;
    if (_queuedCheck) {
      _queuedCheck = false;
      scheduleDraftCheck();
    }
  }
}

$('examLiveUpdate')?.addEventListener('change', () => {
  try { localStorage.setItem(LIVE_UPDATE_KEY, $('examLiveUpdate').checked ? 'on' : 'off'); } catch (_) { /* Storage may be disabled. */ }
  cancelDraftChecks();
  if (!['error', 'review'].includes(_checkState)) {
    _checkState = _evaluatedEditorSignature === editorSignature() ? 'checked' : 'stale';
  }
  scheduleDraftCheck();
  updateEditingStatus();
});
$('checkDraftBtn')?.addEventListener('click', () => {
  if (!_builderBusy) refreshDraftImpact({ force: true, review: true });
});
$('reviewExamCourseSource')?.addEventListener('click', reviewExamCourseSource);

function uniqueByOrder(items) {
  const seen = new Set();
  const out = [];
  for (const item of items) {
    if (item == null || item === '' || seen.has(item)) continue;
    seen.add(item);
    out.push(item);
  }
  return out;
}

// The programme and section chips of a saved run's scope (an empty list: all).
function applyScopeChips(scope) {
  for (const [key, containerId, countId] of [
    ['programs', 'progList', 'progCount'],
    ['sections', 'secList', 'secCount'],
  ]) {
    const selected = scope?.[key];
    if (!Array.isArray(selected)) continue;
    const inputs = [...$(containerId).querySelectorAll('input')];
    for (const cb of inputs) {
      cb.checked = !selected.length || selected.includes(cb.value);
      cb.closest('.et-chip').classList.toggle('active', cb.checked);
    }
    updateChipCount(containerId, countId, inputs.length);
  }
}

function hydrateHeaderFromRun(data) {
  restorePinsFromRun(data);
  restoreLinksFromRun(data);
  $('etLabel').value = data.label ?? '';
  applyScopeChips(data.enrollment_scope);

  const slots = Array.isArray(data.slots) ? data.slots : [];
  const days = uniqueByOrder(slots.map(s => s.day).filter(Boolean));
  const periods = uniqueByOrder(slots.map(s => s.period).filter(Boolean));

  if (days.length) {
    _loadedRunDaysOverride = days;
    const firstDayToken = String(days[0]).split('-').at(-1);
    if (WORK_DAYS.includes(firstDayToken)) $('etStartDay').value = firstDayToken;
    $('etNumDays').value = String(days.length);
    updateDayPreview();
  }

  if (periods.length) {
    setPeriodRows(periods);
  }

  $('etMaxPerDay').value = String(data.qa?.max_per_day ?? 2);

  const thinThreshold = Number(data.qa?.thin_threshold ?? 0);
  $('etRelaxThin').checked = thinThreshold > 0;
  $('etThinThreshold').disabled = thinThreshold <= 0;
  $('etThinThreshold').value = String(thinThreshold > 0 ? thinThreshold : 4);

  const schedule = Array.isArray(data.schedule) ? data.schedule : [];
  const enrollmentCount = (code) => {
    const rows = data.section_enrollment?.[code];
    if (!Array.isArray(rows)) return '';
    return rows.reduce((sum, row) => sum + (parseInt(row.student_count, 10) || 0), 0);
  };
  const courseRows = [];
  const seenCourses = new Set();
  for (const entry of schedule) {
    const code = String(entry.course_code || '').trim();
    if (!code || seenCourses.has(code)) continue;
    seenCourses.add(code);
    const sourceCode = sourceCodeFromDisplay(code, entry.source_course_code);
    courseRows.push({
      course_code: code,
      source_course_code: sourceCode,
      course_name: entry.course_name || '',
      course_identity: entry.course_identity || '',
      course_label: entry.course_label || '',
      programs: entry.programs || [],
      enrolled_count: entry.enrolled_count ?? enrollmentCount(code),
      credit_hours: data.credit_map?.[code] ?? data.credit_map?.[sourceCode] ?? '',
      is_online: !!entry.is_online,
    });
  }
  const storedCourses = Array.isArray(data.courses) ? data.courses : [];
  for (const rawCode of storedCourses) {
    const code = String(rawCode || '').trim();
    if (!code || seenCourses.has(code)) continue;
    seenCourses.add(code);
    const sourceCode = sourceCodeFromDisplay(code);
    courseRows.push({
      course_code: code,
      source_course_code: sourceCode,
      course_name: '',
      course_identity: '',
      enrolled_count: enrollmentCount(code),
      credit_hours: data.credit_map?.[code] ?? data.credit_map?.[sourceCode] ?? '',
      is_online: false,
    });
  }
  if (courseRows.length) {
    $('coursePreview').classList.remove('d-none');
    renderCourseList(courseRows, { inTimetable: true });
  } else {
    clearCoursePreview();
  }
}

function checkedExamReference(reference) {
  const key = typeof reference === 'string' ? reference : reference?.course_identity || reference?.course_code || reference?.code;
  return _drillCourseMetadata.get(key) || null;
}

function drillCourseMarkup(reference, options = {}) {
  const entry = checkedExamReference(reference);
  const code = entry?.course_code || (typeof reference === 'string' ? reference : reference?.course_code || reference?.code || '');
  const course = entry || { course_code: code, course_name: reference?.course_name || '' };
  const identity = entry?.course_identity || entry?.course_code || '';
  return examCourseCardMarkup(course, {
    classes: 'et-drill-course',
    attributes: `data-course-identity="${escapeAttr(identity)}"`,
    actions: examActionMarkup(entry, { ...options, compact: true, courseLabel: `${code} — ${course.course_name || code}` }),
  });
}

function drillCourseListMarkup(references) {
  return `<span class="et-drill-course-list">${(references || []).map(reference => drillCourseMarkup(reference)).join('')}</span>`;
}

function studentGroupLabel(gender) {
  return gender === 'F' ? (IS_AR ? 'طالبات (F)' : 'Female students (F)')
    : gender === 'M' ? (IS_AR ? 'طلاب (M)' : 'Male students (M)') : String(gender || '—');
}

function sectionMappingLabel(status) {
  return status === 'ambiguous' ? (IS_AR ? 'عدة شعب محتملة' : 'Multiple possible sections')
    : status === 'missing' ? (IS_AR ? 'الشعبة غير مسجلة' : 'Section not recorded')
      : (IS_AR ? 'بيانات الشعبة غير متاحة' : 'Section information unavailable');
}

function sectionMappingReason(reason) {
  if (reason === 'no_recorded_section') return IS_AR ? 'لا توجد شعبة مقرر مسجلة لهذه التسجيلات.' : 'No teaching section is recorded for these enrollments.';
  if (reason === 'multiple_recorded_sections') return IS_AR ? 'تطابق هذه التسجيلات أكثر من شعبة مقرر مسجلة.' : 'These enrollments match more than one recorded teaching section.';
  return String(reason || '');
}

function examSectionMarkup(section, { showStudents = false, showGender = false } = {}) {
  // Official labels are data, including literal slashes and punctuation.
  // Room grouping is separate metadata, never a suffix parsed from the name.
  const official = String(section.section || '');
  const name = official.trim() ? official : sectionMappingLabel(section.mapping_status);
  const index = Number(section.room_group_index), count = Number(section.room_group_count);
  const group = Number.isInteger(index) && Number.isInteger(count) && index > 0 && count >= index && count > 1
    ? (IS_AR ? `مجموعة قاعة ${index} من ${count}` : `Room group ${index} of ${count}`) : '';
  const gap = official.trim() && ['missing', 'ambiguous'].includes(section.mapping_status) ? sectionMappingLabel(section.mapping_status) : '';
  return `<div class="et-section-detail"><strong class="et-official-section"><bdi>${escapeAttr(name)}</bdi></strong>${showGender && section.gender ? `<small>${escapeAttr(studentGroupLabel(section.gender))}</small>` : ''}${group ? `<small class="et-room-group">${escapeAttr(group)}</small>` : ''}${gap ? `<small class="et-section-gap">${escapeAttr(gap)}</small>` : ''}${showStudents ? `<small>${escapeAttr(section.student_count ?? 0)} ${T.students}</small>` : ''}</div>`;
}

function roomSectionsMarkup(row) {
  if (Array.isArray(row.section_parts) && row.section_parts.length) {
    return row.section_parts.map(part => examSectionMarkup(part, { showStudents: row.section_parts.length > 1 })).join('');
  }
  const group = row.room_group ? `<small class="et-room-group">${IS_AR ? 'مجموعة القاعة: ' : 'Room group: '}<bdi>${escapeAttr(row.room_group)}</bdi></small>` : '';
  return examSectionMarkup(row) + group;
}

function updateSectionMappingNotice() {
  const notice = $('examSectionMappingNotice');
  if (!notice) return;
  const mapping = _currentResultData?.qa?.section_mapping;
  const missing = Number(mapping?.missing_enrollments || 0);
  const ambiguous = Number(mapping?.ambiguous_enrollments || 0);
  notice.hidden = !missing && !ambiguous;
  const previous = hasCurrentEvaluation() ? '' : (IS_AR ? 'آخر تحقق: ' : 'Last checked: ');
  const message = previous + (IS_AR
    ? `${missing} تسجيل مقرر دون شعبة مسجلة · ${ambiguous} تسجيل مقرر له عدة شعب محتملة. راجع سجلات التسجيل لتأكيد الشعب.`
    : `${missing} course enrollments without a recorded section · ${ambiguous} with multiple possible sections. Review the enrollment records to confirm their sections.`);
  if ($('examSectionMappingText').textContent !== message) $('examSectionMappingText').textContent = message;
  const button = $('examSectionMappingReview');
  button.hidden = !mapping?.details?.length;
  button.disabled = _builderBusy;
}

function updateDrillActionAvailability() {
  const fresh = hasCurrentEvaluation();
  const selected = new Set(getCheckedValues('courseList'));
  const entries = new Map((_currentResultData?.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
  document.querySelectorAll('[data-find-exam], [data-move-exam]').forEach(button => {
    const entry = entries.get(button.dataset.findExam ?? button.dataset.moveExam);
    const selectable = entry && selected.has(entry.course_code);
    let reason = '';
    if (_builderBusy) reason = IS_AR ? 'انتظر اكتمال العملية الحالية.' : 'Wait for the current request to finish.';
    else if (!selectable) reason = IS_AR ? 'المقرر غير متاح ضمن التحديد الحالي.' : 'Course is unavailable in the current selection.';
    else if (button.hasAttribute('data-move-exam')) {
      const locked = lockedPlaceOf(entry.course_code);
      if (locked) reason = LOCK_TEXT.lockedOut(entry.course_code, locked.day, locked.period);
      else if (groupPin(entry.course_code)) reason = IS_AR ? 'ألغِ التثبيت للنقل.' : 'Unpin to move.';
      else if (needsExamSourceRebuild() && !button.hasAttribute('data-allow-unchecked')) reason = IS_AR ? 'راجع مصدر المقررات وأعد البناء قبل النقل من هذه التفاصيل السابقة.' : 'Review the course source and rebuild before moving from these previous details.';
      else if (!fresh && !button.hasAttribute('data-allow-unchecked')) reason = IS_AR ? 'تحقق من التغييرات قبل النقل من هذه التفاصيل السابقة.' : 'Check changes before moving from these previous details.';
    }
    button.disabled = Boolean(reason);
    button.title = reason || button.getAttribute('aria-label') || '';
    if (button.hasAttribute('data-move-exam')) {
      const note = button.closest('.et-drill-course')?.querySelector('.et-drill-action-note') || button.parentElement.querySelector('.et-drill-action-note');
      if (note) { note.textContent = reason; note.hidden = !reason; }
    }
  });
  const notice = $('examDrillNotice');
  if (notice) {
    notice.hidden = fresh || !_currentResultData;
    notice.textContent = fresh ? '' : needsExamSourceRebuild()
      ? (IS_AR ? 'هذه تفاصيل الجدول السابق. يمكنك إظهار المقرر في موضعه الحالي؛ راجع مصدر المقررات وأعد البناء لتحديث التفاصيل.' : 'These are details from the earlier timetable. Find shows the course at its current position; review the course source and rebuild to refresh these details.')
      : (IS_AR ? 'هذه تفاصيل آخر تحقق. يمكنك إظهار المقرر في موضعه الحالي؛ تحقق من التغييرات لتحديث التفاصيل وإتاحة النقل منها.' : 'These are the last checked details. Find shows the course at its current position; Check changes to refresh the details and enable Move here.');
  }
}

function revealExamChip(chip) {
  // The page owns vertical navigation. Only the day column stays fixed when
  // scrolling sideways; period headings scroll away with the timetable.
  chip.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });
  const grid = $('schedGrid');
  const viewport = grid.getBoundingClientRect();
  if (!viewport.width || !viewport.height) return;
  const rowHeader = chip.closest('tr')?.querySelector('th[scope="row"]')?.getBoundingClientRect();
  const target = chip.getBoundingClientRect();
  const { scroller, top, bottom } = examWorkspaceViewport(grid);
  if (bottom > top && target.height <= bottom - top - 16) {
    if (target.top < top + 8) scroller.scrollTop += target.top - top - 8;
    else if (target.bottom > bottom - 8) scroller.scrollTop += target.bottom - bottom + 8;
  }
  const left = viewport.left + (IS_AR ? 8 : (rowHeader?.width || 0) + 8);
  const right = viewport.right - (IS_AR ? (rowHeader?.width || 0) + 8 : 8);
  if (target.left < left) grid.scrollLeft += target.left - left;
  else if (target.right > right) grid.scrollLeft += target.right - right;
}

function findExamInTimetable(identity) {
  if (_builderBusy) return;
  const entry = currentExamByIdentity(identity);
  if (!currentExamIsSelected(entry)) return;
  const chip = [...$('schedGrid').querySelectorAll('.et-course')].find(item => item.dataset.course === entry.course_code);
  if (!chip) return;
  _foundExamIdentity = identity;
  $('schedGrid').querySelectorAll('.et-found-exam').forEach(item => item.classList.remove('et-found-exam'));
  chip.classList.add('et-found-exam');
  chip.focus({ preventScroll: true });
  revealExamChip(chip);
  if ($('examFindStatus')) $('examFindStatus').textContent = `${entry.course_code} — ${entry.course_name || ''} · ${examLocation(entry)}`;
}

document.addEventListener('click', event => {
  const find = event.target.closest('[data-find-exam]');
  if (find && !find.disabled) { findExamInTimetable(find.dataset.findExam); return; }
  const move = event.target.closest('[data-move-exam]');
  if (!move || move.disabled || _builderBusy) return;
  const entry = currentExamByIdentity(move.dataset.moveExam);
  if (!currentExamIsSelected(entry) || (!hasCurrentEvaluation() && !move.hasAttribute('data-allow-unchecked'))) return;
  openExamMoveDialog(entry.course_code);
});

const _drillRenderers = {
  'section-mapping'(rows) {
    return {
      title: IS_AR ? 'شعب المقررات التي تحتاج مراجعة' : 'Teaching sections requiring review',
      head: `<tr><th>${IS_AR ? 'المقرر' : 'Course'}</th><th>${IS_AR ? 'فئة الطلاب' : 'Student group'}</th><th>${IS_AR ? 'الطلاب' : 'Students'}</th><th>${IS_AR ? 'المشكلة' : 'Issue'}</th><th>${IS_AR ? 'التفاصيل' : 'Details'}</th></tr>`,
      body: rows.map(row => `<tr><td>${drillCourseMarkup(row, { allowMove: false })}</td><td>${escapeAttr(studentGroupLabel(row.gender))}</td><td>${escapeAttr(row.student_count ?? 0)}</td><td>${escapeAttr(sectionMappingLabel(row.mapping_status))}</td><td>${escapeAttr(sectionMappingReason(row.reason))}</td></tr>`).join(''),
      colspan: 5,
    };
  },
  'room-unassigned'(rows) {
    return {
      title: IS_AR ? 'مجموعات دون قاعة' : 'Unassigned room groups',
      head: `<tr><th>${IS_AR ? 'المقرر' : 'Course'}</th><th>${IS_AR ? 'الموعد' : 'Time'}</th><th>${IS_AR ? 'الشعبة ومجموعة القاعة' : 'Teaching section and room group'}</th><th>${IS_AR ? 'فئة الطلاب' : 'Student group'}</th><th>${IS_AR ? 'الطلاب' : 'Students'}</th></tr>`,
      body: rows.map(row => `<tr><td>${rowLockMark(row)}${drillCourseMarkup(row)}</td><td><bdi dir="ltr">${escapeAttr(`${row.day || ''} · ${row.period || ''}`)}</bdi></td><td>${roomSectionsMarkup(row)}</td><td>${escapeAttr(studentGroupLabel(row.gender))}</td><td>${escapeAttr(row.student_count ?? 0)}</td></tr>`).join(''),
      colspan: 5,
    };
  },
  'room-double'(rows) {
    return {
      title: IS_AR ? 'تعارضات حجز القاعات' : 'Room double bookings',
      head: `<tr><th>${IS_AR ? 'القاعة' : 'Room'}</th><th>${IS_AR ? 'الموعد' : 'Time'}</th><th>${IS_AR ? 'المقررات' : 'Courses'}</th></tr>`,
      body: rows.map(row => {
        const slot = _slotsByIndex[row.slot_index];
        return `<tr><td>${rowLockMark(row, row.courses)}${escapeAttr(row.room_code || '')}</td><td>${escapeAttr(slot ? `${slot.day} · ${slot.period}` : `#${row.slot_index}`)}</td><td>${drillCourseListMarkup(row.courses)}</td></tr>`;
      }).join(''),
      colspan: 3,
    };
  },
  conflicts(rows) {
    const slotLabel = (si) => {
      const s = _slotsByIndex[si];
      return s ? `${s.day} ${s.period}` : `#${si}`;
    };
    return {
      title: IS_AR ? 'تفاصيل التعارضات' : 'Conflict Detail',
      head: `<tr>
        <th>${IS_AR ? 'النوع' : 'Type'}</th>
        <th>${IS_AR ? 'الطالب / المجموعة' : 'Student / Bucket'}</th>
        <th>${IS_AR ? 'الفترة / اليوم' : 'Slot / Day'}</th>
        <th>${IS_AR ? 'المقررات' : 'Courses'}</th>
      </tr>`,
      // A student in two linked courses sits both papers at one time: a real
      // clash that no tool may separate, so it says what it is.
      note: rows.some(r => r.kind === 'linked-same-slot') ? (IS_AR
        ? 'تعارض الاختبار المرتبط: طالب مسجل في مقررين مرتبطين يؤدي ورقتين في وقت واحد. لا يفصل «إصلاح بأقل تغيير» المقررات المرتبطة؛ ألغِ الربط أو راجع التسجيل.'
        : 'A linked-exam clash is a student registered in two linked courses: two papers at one time. Fix with fewest moves never separates linked courses; unlink them or review the registration.') : '',
      body: rows.map(r => {
        const isBucket = r.kind === 'bucket-day';
        const typeBadge = isBucket
          ? `<span class="badge bg-warning text-dark">${IS_AR ? 'مجموعة' : 'Bucket'}</span>`
          : r.kind === 'linked-same-slot'
            ? `<span class="badge bg-danger et-linked-clash">${IS_AR ? 'تعارض اختبار مرتبط' : 'Linked-exam clash'}</span>`
            : `<span class="badge bg-danger">${IS_AR ? 'طالب' : 'Student'}</span>`;
        const who = isBucket
          ? `${r.program || ''}/Term${r.programme_term ?? ''}`
          : `<strong>${r.student_id}</strong>`;
        const when = isBucket ? (r.day || '') : slotLabel(r.slot_index);
        const courses = drillCourseListMarkup(r.courses);
        return `<tr>
          <td>${rowLockMark(r, r.courses)}${typeBadge}</td>
          <td>${who}</td>
          <td>${when}</td>
          <td>${courses}</td>
        </tr>`;
      }).join(''),
      colspan: 4,
    };
  },
  overload(rows) {
    return {
      title: IS_AR ? 'تفاصيل الطلاب المتجاوزين' : 'Overloaded Students Detail',
      head: `<tr>
        <th>${IS_AR ? 'الطالب' : 'Student ID'}</th>
        <th>${IS_AR ? 'اليوم' : 'Day'}</th>
        <th>${IS_AR ? 'العدد' : 'Count'}</th>
        <th>${IS_AR ? 'المقررات' : 'Courses'}</th>
      </tr>`,
      body: rows.map(r => `<tr>
        <td><strong>${r.student_id}</strong></td>
        <td>${r.day}</td>
        <td><span class="badge bg-danger">${r.count}</span></td>
        <td>${drillCourseListMarkup(r.courses)}</td>
      </tr>`).join(''),
      colspan: 4,
    };
  },
  heavy(rows) {
    return {
      title: IS_AR ? 'تفاصيل الأيام الثقيلة' : 'Heavy Day Students Detail',
      head: `<tr>
        <th>${IS_AR ? 'الطالب' : 'Student ID'}</th>
        <th>${IS_AR ? 'اليوم' : 'Day'}</th>
        <th>${IS_AR ? 'مجموع الساعات' : 'Total Cr.'}</th>
        <th>${IS_AR ? 'شدة' : 'Severity'}</th>
        <th>${IS_AR ? 'المقررات' : 'Courses'}</th>
      </tr>`,
      body: rows.map(r => {
        const sev = r.penalty >= 100
          ? `<span class="badge bg-danger">${IS_AR ? 'حرج' : 'Critical'}</span>`
          : `<span class="badge bg-warning text-dark">${IS_AR ? 'مرتفع' : 'High'}</span>`;
        return `<tr>
          <td><strong>${r.student_id}</strong></td>
          <td>${r.day}</td>
          <td>${r.total_credits}</td>
          <td>${sev}</td>
          <td>${drillCourseListMarkup(r.courses)}</td>
        </tr>`;
      }).join(''),
      colspan: 5,
    };
  },
  // Exam pairs and how many students sit both on that day. Never students.
  // The count comes second so a phone shows it without scrolling past cards.
  'same-day-pairs'(rows) {
    const examCell = exam => `${drillCourseMarkup(exam?.code || '')}<small class="et-pair-period"><bdi dir="ltr">${escapeAttr(exam?.period || '')}</bdi></small>`;
    const clash = `<span class="et-pair-clash" title="${escapeAttr(IS_AR ? 'في الفترة نفسها، ومحسوب أيضاً ضمن التعارضات' : 'Same period, also counted under Conflicts')}">${IS_AR ? 'تعارض' : 'Clash'}<span class="visually-hidden">${IS_AR ? ': في الفترة نفسها' : ': same period'}</span></span>`;
    return {
      title: IS_AR ? 'أزواج الاختبارات في اليوم نفسه' : 'Exam pairs on the same day',
      // A student is in every pair they sit on a day, so pairs outnumber the card.
      note: IS_AR
        ? 'يُحسب الطالب في كل زوج يؤديه في اليوم نفسه (ثلاثة اختبارات تكوّن ثلاثة أزواج)، لذا قد يزيد مجموع الأعداد على رقم البطاقة.'
        : 'A student is counted in every pair they sit on the same day (three exams make three pairs), so these counts can add up to more than the card.',
      head: `<tr><th>${IS_AR ? 'اليوم' : 'Day'}</th><th>${IS_AR ? 'طلاب يؤدون الاختبارين' : 'Students sitting both'}</th><th>${IS_AR ? 'الاختبار الأول' : 'First exam'}</th><th>${IS_AR ? 'الاختبار الثاني' : 'Second exam'}</th></tr>`,
      body: rows.map(row => {
        const [first, second] = row.courses || [];
        return `<tr><td class="et-pair-day"><bdi dir="ltr">${escapeAttr(row.day || '')}</bdi></td><td><strong class="et-pair-count">${escapeAttr(row.student_count ?? 0)}</strong>${row.clash ? ` ${clash}` : ''}</td><td>${examCell(first)}</td><td>${examCell(second)}</td></tr>`;
      }).join(''),
      colspan: 4,
      empty: Array.isArray(_drillData['same-day-pairs'])
        ? (IS_AR ? 'لا يوجد طالب لديه أكثر من اختبار في يوم واحد.' : 'No student has more than one exam on a day.')
        : (IS_AR ? 'لم يُحسب هذا المؤشر لهذا الجدول المحفوظ. افحص التغييرات لحسابه.' : 'Not calculated for this saved timetable. Check changes to calculate it.'),
    };
  },
  'thin-courses'(rows) {
    return {
      title: IS_AR ? 'مقررات صغيرة (مخففة)' : 'Thin Courses Relaxed',
      head: `<tr>
        <th>${IS_AR ? 'المقرر' : 'Course'}</th>
        <th>${IS_AR ? 'الطلاب' : 'Students'}</th>
        <th>${IS_AR ? 'تعارضات أُسقطت' : 'Edges dropped'}</th>
        <th>${IS_AR ? 'المقررات المتعارضة' : 'Neighbours dropped'}</th>
      </tr>`,
      body: rows.map(r => `<tr>
        <td>${drillCourseMarkup(r.course_code)}</td>
        <td>${r.total_students}</td>
        <td>${r.dropped_edges}</td>
        <td>${drillCourseListMarkup(r.neighbours)}</td>
      </tr>`).join(''),
      colspan: 4,
    };
  },
  'multi-sitting'(rows) {
    return {
      title: IS_AR ? 'شعب موزعة على قاعات' : 'Sections split across rooms',
      head: `<tr>
        <th>${IS_AR ? 'المقرر' : 'Course'}</th>
        <th>${IS_AR ? 'شعبة المقرر' : 'Teaching section'}</th>
        <th>${IS_AR ? 'الطلاب' : 'Students'}</th>
        <th>${IS_AR ? 'أكبر سعة قاعة' : 'Max room cap'}</th>
        <th>${IS_AR ? 'مجموعات القاعات' : 'Room groups'}</th>
        <th>${IS_AR ? 'الفترات' : 'Slots'}</th>
        <th>${IS_AR ? 'القاعات' : 'Rooms'}</th>
        <th>${IS_AR ? 'الحالة' : 'State'}</th>
      </tr>`,
      body: rows.map(r => {
        const incompleteBadge = r.incomplete
          ? `<span class="badge bg-warning text-dark">${IS_AR ? 'غير مكتمل' : 'Incomplete'}</span>`
          : `<span class="badge bg-success">${IS_AR ? 'مكتمل' : 'Complete'}</span>`;
        return `<tr>
          <td>${rowLockMark(r, [r.course_code])}${drillCourseMarkup(r.course_identity || r.course_code)}</td>
          <td>${examSectionMarkup(r, { showGender: true })}</td>
          <td>${r.enrolment || 0}</td>
          <td>${r.max_room_cap || 0}</td>
          <td>${r.sittings || 0}</td>
          <td>${[...new Set(r.slots || [])].map(s => { const slot = _slotsByIndex[s]; return `<span class="badge bg-secondary me-1"><bdi dir="ltr">${escapeAttr(slot ? `${slot.day} ${slot.period}` : String(s))}</bdi></span>`; }).join('')}</td>
          <td>${(r.rooms || []).map(rm => `<code>${escapeAttr(!rm || rm === 'UNASSIGNED' ? (IS_AR ? 'دون قاعة' : 'Unassigned') : rm)}</code>`).join(' ')}</td>
          <td>${incompleteBadge}</td>
        </tr>`;
      }).join(''),
      colspan: 8,
    };
  },
  // What the report found inside locked cells, grouped by kind: reported, never moved.
  'exam-locks'(rows) {
    const capacity = rows.some(row => row.kind === 'room_over_capacity');
    return {
      title: LOCK_TEXT.drillTitle,
      note: [LOCK_TEXT.drillNote, capacity ? LOCK_TEXT.capacityNote : ''].filter(Boolean).join(' '),
      head: `<tr>${LOCK_TEXT.drillHead.map(text => `<th>${escapeAttr(text)}</th>`).join('')}</tr>`,
      body: rows.map(issue => {
        const kind = LOCK_ISSUE[issue.kind];
        const where = issue.period ? `${issue.day || ''} · ${issue.period}` : String(issue.day || '');
        return `<tr class="et-lock-issue" data-lock-issue="${escapeAttr(issue.kind)}"><td><span class="et-lock-kind">${escapeAttr(kind?.label || String(issue.kind || ''))}</span></td><td><bdi dir="ltr">${escapeAttr(where)}</bdi></td><td>${escapeAttr(kind ? kind.detail(issue) : '')}</td></tr>`;
      }).join(''),
      colspan: 3,
      empty: Array.isArray(_drillData['exam-locks']) ? LOCK_TEXT.drillEmpty : LOCK_TEXT.barUnchecked,
    };
  },
  'thin-clash'(rows) {
    const slotLabel = (si) => {
      const s = _slotsByIndex[si];
      return s ? `${s.day} ${s.period}` : `#${si}`;
    };
    return {
      title: IS_AR ? 'تعارضات فعلية بسبب التخفيف' : 'Realised Relaxation Clashes',
      head: `<tr>
        <th>${IS_AR ? 'الطالب' : 'Student'}</th>
        <th>${IS_AR ? 'الفترة' : 'Slot'}</th>
        <th>${IS_AR ? 'المقررات المتعارضة' : 'Courses in collision'}</th>
      </tr>`,
      body: rows.map(r => `<tr>
        <td><strong>${r.student_id}</strong></td>
        <td><span class="badge bg-secondary">${slotLabel(r.slot_index)}</span></td>
        <td>${drillCourseListMarkup(r.courses)}</td>
      </tr>`).join(''),
      colspan: 3,
    };
  },
};

function openDrill(type, { navigate = false } = {}) {
  const panel = $('kpiDrill');
  const rows = _drillData[type] || [];
  const renderer = _drillRenderers[type];
  if (!renderer) {
    console.warn('Unknown drill type:', type);
    return;
  }

  // Toggle off if already open on same type
  const isOpen = !panel.classList.contains('d-none');
  if (isOpen && panel.dataset.type === type) { closeDrill(); return; }

  // Mark active card
  document.querySelectorAll('.kpi-click.active').forEach(el => {
    el.classList.remove('active');
    el.setAttribute('aria-expanded', 'false');
  });
  const card = document.querySelector(`.kpi-click[data-drill="${type}"]`);
  if (card) {
    card.classList.add('active');
    card.setAttribute('aria-expanded', 'true');
  }
  panel.dataset.type = type;

  const r = renderer(rows);
  $('kpiDrillTitle').textContent = r.title;
  $('kpiDrillNote').textContent = rows.length ? (r.note || '') : '';
  $('kpiDrillNote').hidden = !$('kpiDrillNote').textContent;
  $('kpiDrillHead').innerHTML = r.head;
  $('kpiDrillBody').innerHTML = rows.length
    ? r.body
    : `<tr><td colspan="${r.colspan}" class="text-center text-secondary py-3">${r.empty || (IS_AR ? 'لا توجد بيانات' : 'No records')}</td></tr>`;
  panel.classList.remove('d-none');
  updateDrillActionAvailability();
  examReview?.refresh();
  document.querySelectorAll('[data-open-drill]').forEach(button => button.setAttribute('aria-expanded', String(button.dataset.openDrill === type)));
  if (navigate) {
    const title = $('kpiDrillTitle');
    title.setAttribute('tabindex', '-1');
    title.focus({ preventScroll: true });
    title.scrollIntoView({ block: 'start', inline: 'nearest', behavior: 'auto' });
  }
}

// Click handlers for KPI cards
document.addEventListener('click', (e) => {
  const card = e.target.closest('.kpi-click, [data-open-drill]');
  if (card && !_builderBusy) {
    const type = card.dataset.drill || card.dataset.openDrill;
    if (type) openDrill(type, { navigate: true });
    return;
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const card = event.target.closest?.('.kpi-click[role="button"]');
  if (!card || _builderBusy) return;
  event.preventDefault();
  if (!event.repeat && card.dataset.drill) openDrill(card.dataset.drill, { navigate: true });
});

$('kpiDrillClose').addEventListener('click', () => closeDrill(true));

// Rebuild lookup from buckets_summary each time results arrive.
function buildProgramCoursesMap(bucketsSummary) {
  _programCourses = {};
  if (!Array.isArray(bucketsSummary)) return;
  for (const b of bucketsSummary) {
    const prog = (b.program || '').toLowerCase();
    if (!prog) continue;
    if (!_programCourses[prog]) _programCourses[prog] = new Set();
    for (const c of (b.courses || [])) _programCourses[prog].add(c.toLowerCase());
  }
}

/* ── Render Results ── */
function renderResults(data, { evaluation = false, preserveViewport = true } = {}) {
  // A new board: a notice about the one before no longer applies. A Check
  // re-renders the same board, and leaves it.
  if (!evaluation) clearJobNotice();
  const workspaceAnchor = evaluation && preserveViewport ? captureExamWorkspaceAnchor() : null;
  const openDrillType = !$('kpiDrill').classList.contains('d-none') ? $('kpiDrill').dataset.type : null;
  const matrixOpen = !$('conflictMatrix').classList.contains('d-none');
  const matrixScroll = { left: $('etMatrixViewport').scrollLeft, top: $('etMatrixViewport').scrollTop };
  const matrixFocusKey = document.activeElement?.closest('[data-matrix-key]')?.dataset.matrixKey;
  _renderingResults = true;
  if (evaluation) {
    const inputChanged = Boolean(_savedResultData?.input_fingerprint && data.input_fingerprint
      && _savedResultData.input_fingerprint !== data.input_fingerprint);
    const unrecorded = QA_ADDED_AFTER_SAVED_RUNS.filter(key => !Object.hasOwn(_savedResultData?.qa || {}, key));
    _evaluatedReportDirty = inputChanged || reportSignature(data, unrecorded) !== reportSignature(_savedResultData || {}, unrecorded);
    _evaluatedInputsChanged = inputChanged || (editorSignature() === _savedEditorSignature && _evaluatedReportDirty);
    if (!_evaluatedReportDirty && data.input_fingerprint && _savedResultData && !_savedResultData.input_fingerprint) {
      _savedResultData.input_fingerprint = data.input_fingerprint;
    }
    const calculated = new Map((data.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
    const placements = _currentResultData.schedule.map(entry => {
      const checked = calculated.get(entry.course_identity || entry.course_code);
      return { ...entry, ...(checked || {}), course_code: entry.course_code, course_identity: entry.course_identity,
        day: entry.day, period: entry.period, slot_index: entry.slot_index };
    });
    _currentResultData = { ..._currentResultData, ...cloneData(data), schedule: placements,
      run_id: _currentRunId, rebuild_mode: _currentResultData.rebuild_mode,
      pinned: Object.values(_pinnedCourses).map(pin => ({ ...pin })),
      linked_exams: linkPayload(), exam_locks: lockPayload() };
    data = _currentResultData;
  } else {
    cancelDraftChecks();
    restorePinsFromRun(data);
    // Before the saved signature is taken below: restored later, the page
    // would send no links, and the first Check would say the inputs changed.
    restoreLinksFromRun(data);
    // The same for the saved locks. A new board is its own source of locks.
    restoreLocksFromRun(data);
    _lockSource = null;
    _roomsForLocks = false;
    clearLockRefusal();
    _currentResultData = cloneData(data);
    _undoCommands = [];
    _redoCommands = [];
    _editorRevision = 0;
    _foundExamIdentity = null;
  }
  hideLegacyQaWarnings();
  _checkState = 'checked';
  _checkError = '';
  _linkError = null;
  buildProgramCoursesMap(data.buckets_summary);
  $('etResults').classList.remove('d-none');

  // Track run_id for Excel export link
  _currentRunId = data.run_id ?? null;
  const exportBtn = $('exportXlsx');
  if (_currentRunId) {
    exportBtn.href = `/ops/exam-timetable/${_currentRunId}/export.xlsx`;
    exportBtn.classList.remove('d-none');
  } else {
    exportBtn.classList.add('d-none');
  }

  // Show seed info if the timetable was built with randomised tie-breaking
  const seedInfo = $('etSeedInfo');
  if (data.seed != null) {
    seedInfo.textContent = IS_AR ? `بذرة: ${data.seed}` : `Seed: ${data.seed}`;
    seedInfo.classList.remove('d-none');
  } else {
    seedInfo.classList.add('d-none');
  }

  // KPIs
  $('kCourses').textContent  = data.courses_count ?? data.qa?.total_courses ?? 0;
  $('kStudents').textContent = data.students_count ?? data.qa?.total_students ?? 0;
  $('kSlots').textContent    = data.qa?.slots_used ?? 0;
  $('kMaxDay').textContent   = data.qa?.max_exams_per_day_per_student ?? 0;
  showRecordedCount($('kMultiExamDay'), data.qa?.multi_exam_day_students);

  const mpd = data.qa?.max_per_day ?? 2;
  $('kOverLabel').textContent = IS_AR
    ? `طلاب بأكثر من ${mpd} اختبارات/يوم`
    : `Students >${mpd} exams/day`;

  const overLimit = data.qa?.students_over_limit_per_day ?? data.qa?.students_over_2_per_day ?? 0;
  $('kOver2').textContent = overLimit;
  $('kOver2').className   = 'v' + (overLimit > 0 ? ' warn' : '');

  const cc = data.qa?.conflict_count ?? 0;
  $('kConflicts').textContent = cc;
  $('kConflicts').className   = 'v' + (cc > 0 ? ' warn' : '');

  // Credit KPIs
  const maxCr = data.qa?.max_credit_load_per_day ?? 0;
  $('kMaxCredit').textContent = maxCr;
  $('kMaxCredit').className   = 'v' + (maxCr > 8 ? ' warn' : '');
  const hd = data.qa?.heavy_day_students ?? 0;
  $('kHeavyDay').textContent = hd;
  $('kHeavyDay').className   = 'v' + (hd > 0 ? ' warn' : '');

  // Room KPIs
  const rqa = data.qa?.rooms ?? {};
  $('kRoomsUsed').textContent = rqa.rooms_used ?? 0;
  const util = Number(rqa.avg_utilization ?? 0);
  $('kRoomUtil').textContent = `${(util * 100).toFixed(0)}%`;
  const unassigned = (rqa.unassigned_room_sections ?? []).length;
  $('kRoomUnassigned').textContent = unassigned;
  $('kRoomUnassigned').className = 'v' + (unassigned > 0 ? ' warn' : '');
  const doubleB = (rqa.room_double_bookings ?? []).length;
  $('kRoomDouble').textContent = doubleB;
  $('kRoomDouble').className = 'v' + (doubleB > 0 ? ' warn' : '');

  // Thin-relaxation KPIs — only shown when threshold > 0
  const thinThreshold = data.qa?.thin_threshold ?? 0;
  const thinCourses = data.qa?.thin_courses ?? [];
  const thinClash = data.qa?.thin_clash_risk ?? [];
  if (thinThreshold > 0) {
    $('kThinRow').classList.remove('d-none');
    $('kThinCount').textContent = thinCourses.length;
    $('kThinClash').textContent = thinClash.length;
    $('kThinClash').className = 'v' + (thinClash.length > 0 ? ' warn' : '');
  } else {
    $('kThinRow').classList.add('d-none');
  }

  // v2 honest status surface: primary_status + status_flags banner.
  // Always shown for v2+ payloads (the data is universally present);
  // hidden only when the payload pre-dates v2 entirely.
  const primaryStatus = data.primary_status;
  const statusFlags = Array.isArray(data.status_flags) ? data.status_flags : [];
  const banner = $('kStatusBanner');
  const copyMap = $('statusCopyMap');
  if (primaryStatus && copyMap) {
    banner.classList.remove('d-none');
    const labelKey = primaryStatus.replace(/-/g, '_');
    // Defensive fallback: an unknown primary_status value (a future
    // backend enum we don't yet know about) renders the raw key as
    // its label and the secondary badge style — never throws, never
    // silently disappears. The registrar sees an honest "I don't
    // recognise this status" signal rather than a missing card.
    const primaryLabel = copyMap.dataset[labelKey] || primaryStatus;
    const primaryEl = $('kStatusPrimary');
    primaryEl.textContent = primaryLabel;
    const severityColour = ({
      clean: 'bg-success',
      clean_with_approved_thin_conflicts: 'bg-success',
      requires_room_action: 'bg-warning text-dark',
      requires_section_review: 'bg-warning text-dark',
      contains_overflow: 'bg-warning text-dark',
      contains_manual_override: 'bg-warning text-dark',
      contains_workload_warnings: 'bg-warning text-dark',
      infeasible: 'bg-danger',
      unrenderable: 'bg-secondary',
      future_version_unrenderable: 'bg-secondary',
    })[labelKey] || 'bg-secondary';
    primaryEl.className = 'badge ' + severityColour;
    // Render flags as smaller pills, deduplicated and ordered by
    // registrar-action severity (most-actionable first), with unknown
    // flags appended alphabetically so a future backend addition is
    // visible (with the raw key) rather than swallowed.
    //
    // Severity order (peer-review confirmed): room action requires
    // physical-world fix > overflow is a registrar-visible scheduling
    // failure > manual override is a deliberate registrar bypass >
    // workload warnings need review before policy-approved thin conflicts >
    // multi-sitting required is a logistics fact > legacy_incomplete_qa
    // is a metadata caveat last.
    const FLAG_DISPLAY_ORDER = [
      'room_action_required',
      'overflow',
      'manual_override',
      'section_mapping_incomplete',
      'daily_limit_exceeded',
      'heavy_credit_day',
      'approved_thin_conflicts',
      'multi_sitting_required',
      'legacy_incomplete_qa',
    ];
    const flagsEl = $('kStatusFlags');
    flagsEl.innerHTML = '';
    const uniqueFlags = [...new Set(statusFlags)];
    uniqueFlags.sort((a, b) => {
      const ai = FLAG_DISPLAY_ORDER.indexOf(a.replace(/-/g, '_'));
      const bi = FLAG_DISPLAY_ORDER.indexOf(b.replace(/-/g, '_'));
      // Known flags ordered by severity; unknowns appended alphabetically.
      if (ai === -1 && bi === -1) return a.localeCompare(b);
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    });
    uniqueFlags.forEach(flag => {
      const flagKey = flag.replace(/-/g, '_');
      // Same defensive fallback as primary_status: unknown flag renders
      // the raw key, never disappears.
      const flagLabel = copyMap.getAttribute(`data-flag-${flagKey}`) || flag;
      const pill = document.createElement('span');
      pill.className = 'badge bg-light text-dark border';
      pill.style.fontSize = '0.75rem';
      pill.textContent = flagLabel;
      flagsEl.appendChild(pill);
    });
  } else {
    banner.classList.add('d-none');
  }

  // This tile counts teaching sections split across room groups.
  // Hide it when no sections require more than one room group.
  const msCount = data.qa?.multi_sitting_sections ?? 0;
  if (msCount > 0) {
    $('kMultiSittingRow').classList.remove('d-none');
    $('kMultiSittingCount').textContent = msCount;
    $('kMultiSittingCount').className = 'v warn';
  } else {
    $('kMultiSittingRow').classList.add('d-none');
  }

  // Build slot lookup so the thin-clash drill can show day/period for
  // each slot_index instead of a bare integer.
  _slotsByIndex = {};
  for (const s of (data.slots || [])) {
    _slotsByIndex[s.index] = s;
  }

  // Freeze identity resolution with this checked report, never with a later draft.
  _drillCourseMetadata = new Map();
  for (const entry of data.schedule || []) {
    _drillCourseMetadata.set(entry.course_code, { ...entry });
    _drillCourseMetadata.set(entry.course_identity || entry.course_code, { ...entry });
  }
  // The exams this report says its locked cells keep.
  _drillLockedCodes = new Set((data.qa?.exam_locks?.cells || []).flatMap(cell => (cell?.courses || [])
    .map(course => (typeof course === 'string' ? course : course?.course_code)).filter(Boolean)));
  // Store drilldown data + close any open panel
  // The server says which clashes are inside a link (linked_same_slot).
  const linkedClashes = new Set((data.qa?.manual_override_details ?? [])
    .filter(row => row?.kind === 'linked_same_slot').map(row => `${row.student_id}|${row.slot_index}`));
  _drillData = {
    conflicts:       [
      ...(data.qa?.same_slot_conflicts ?? []).map(r => ({ ...r, kind: linkedClashes.has(`${r.student_id}|${r.slot_index}`) ? 'linked-same-slot' : 'same-slot' })),
      ...(data.qa?.bucket_day_violations ?? []).map(r => ({ ...r, kind: 'bucket-day' })),
    ],
    overload:        data.qa?.overload_details ?? [],
    heavy:           data.qa?.heavy_day_details ?? [],
    'same-day-pairs': Array.isArray(data.qa?.same_day_exam_pairs) ? data.qa.same_day_exam_pairs : null,
    'thin-courses':  data.qa?.thin_courses ?? [],
    'thin-clash':    data.qa?.thin_clash_risk ?? [],
    'multi-sitting': data.qa?.multi_sitting_details ?? [],
    'section-mapping': data.qa?.section_mapping?.details ?? [],
    'room-unassigned': data.qa?.rooms?.unassigned_room_sections ?? [],
    'room-double': data.qa?.rooms?.room_double_bookings ?? [],
    // What the report found inside locked cells; null before any report.
    'exam-locks': Array.isArray(data.qa?.exam_locks?.issues) ? lockIssueRows(data.qa.exam_locks.issues) : null,
  };
  closeDrill();
  hideLegacyQaWarnings();

  // Schedule grid
  const schedule = data.schedule ?? [];
  const slots    = data.slots ?? [];
  renderScheduleGrid(schedule, slots);

  // Conflict matrix
  renderConflictMatrix(data.conflicts ?? [], data.courses ?? []);
  if (evaluation && matrixOpen) {
    $('conflictMatrix').classList.remove('d-none');
    $('toggleMatrix').textContent = T.hide;
    $('toggleMatrix').setAttribute('aria-expanded', 'true');
    $('etMatrixViewport').scrollLeft = matrixScroll.left;
    $('etMatrixViewport').scrollTop = matrixScroll.top;
  }
  if (evaluation && matrixFocusKey) {
    const matrixTarget = [...$('matrixGrid').querySelectorAll('[data-matrix-key]')]
      .find(element => element.dataset.matrixKey === matrixFocusKey);
    (matrixTarget || $('etMatrixViewport')).focus({ preventScroll: true });
  }
  _observedEditorSignature = editorSignature();
  _evaluatedEditorSignature = _observedEditorSignature;
  if (!evaluation) {
    _savedEditorSignature = _observedEditorSignature;
    _savedResultData = cloneData(_currentResultData);
    _evaluatedReportDirty = false;
    _evaluatedInputsChanged = false;
    _reviewedInputFingerprint = data.input_fingerprint || null;
    _scheduleHasDraftMoves = false;
    if ($('examSetupDetails')) $('examSetupDetails').open = false;
    if ($('examSummaryDetails')) $('examSummaryDetails').open = false;
    if ($('examChangesDetails')) $('examChangesDetails').open = false;
  }
  _renderingResults = false;
  updateLoadedRunActions();
  if (evaluation && openDrillType) openDrill(openDrillType);
  restoreExamWorkspaceAnchor(workspaceAnchor);
}

function compactExamCourseName(name) {
  const words = String(name || '').trim().split(/\s+/).filter(Boolean);
  // Some catalog names start with a copied course number or punctuation.
  // Omit that prefix only in this compact view; retain letter-bearing 3D/C++ tokens.
  const firstWord = words.findIndex(word => /\p{L}/u.test(word));
  if (firstWord < 0) return '';
  const nameWords = words.slice(firstWord);
  return nameWords.slice(0, 2).join(' ') + (nameWords.length > 2 ? '…' : '');
}

// The timetable and all QA details share one compact card and icon vocabulary.
function examCourseIcon(name) {
  const paths = {
    pin: '<path d="M16 3 9 3 10 9 6 13 11 13 11 21 13 18 13 13 18 13 14 9Z"/>',
    move: '<path d="M12 3v18M3 12h18M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/>',
    related: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6M21 21v-2a6 6 0 0 0-4-5"/>',
    lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    online: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a17 17 0 0 1 0 18 17 17 0 0 1 0-18"/>',
    find: '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
  };
  return `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths[name] || ''}</svg>`;
}

function examCourseCardMarkup(course, { classes, attributes = '', actions = '', extra = '', pinned = false, note = '' }) {
  const code = String(course?.course_code || '');
  const fullName = String(course?.course_name || '').trim();
  const shortName = compactExamCourseName(fullName);
  // `note`: a state the card's name carries, as "locked" in a locked cell.
  const label = `${code} — ${fullName || code}${note ? ` (${note})` : ''}`;
  const onlineLabel = IS_AR ? 'مقرر عن بُعد' : 'Online course';
  return `<span class="et-course-card ${classes}" role="group" tabindex="-1" aria-label="${escapeAttr(label)}" title="${escapeAttr(label)}"${note ? ` data-exam-note="${escapeAttr(note)}"` : ''} ${attributes}><strong class="et-course-code">${pinned ? `<span class="et-pin-lock" aria-hidden="true">${examCourseIcon('pin')}</span> ` : ''}${escapeAttr(code)}${course?.is_online ? ` <span class="et-course-online" role="img" aria-label="${onlineLabel}" title="${onlineLabel}">${examCourseIcon('online')}</span>` : ''}</strong>${shortName ? `<span class="et-course-short-name">${escapeAttr(shortName)}</span><span class="et-course-full-name" aria-hidden="true">${escapeAttr(fullName)}</span>` : ''}${actions}${extra}<span class="et-review-badges"></span></span>`;
}

/* ── Render schedule as day×period grid ── */
// Builds an HTML table: rows = days, columns = periods, cells = course chips.
// Dragging moves an unpinned exam; pinning is a separate explicit action.
// Courses that couldn't be placed appear in a red OVERFLOW row at the bottom.
function renderScheduleGrid(schedule, slots) {
  const selected = new Set(getCheckedValues('courseList'));
  if (_coursesLoaded) schedule = schedule.filter(entry => selected.has(entry.course_code));
  const container = $('schedGrid');
  const active = document.activeElement;
  const focusedCourse = active?.closest?.('.et-course')?.dataset.course;
  const focusedAction = active?.matches?.('[data-exam-pin]') ? 'data-exam-pin' : active?.matches?.('[data-exam-move]') ? 'data-exam-move' : active?.matches?.('[data-exam-related]') ? 'data-exam-related' : null;
  // A lock button is found again by its day and period after the re-render.
  const focusedLock = active?.matches?.('[data-lock-day]') ? [active.dataset.lockDay, active.dataset.lockPeriod || ''] : null;
  const focusedColumn = active?.matches?.('[data-lock-column]') ? active.dataset.lockColumn : null;
  const scrollLeft = container.scrollLeft;
  const coursesByCode = new Map(schedule.map(entry => [entry.course_code, entry]));
  const linkByCode = linkedCodeIndex();
  const locks = lockSets();
  const courseChip = code => {
    const pinned = Boolean(groupPin(code));
    const course = coursesByCode.get(code);
    // In a locked cell nothing moves or pins in or out: the card says so.
    const locked = Boolean(course && course.day !== 'OVERFLOW' && isCellLocked(course.day, course.period, locks));
    const lockedHint = locked ? LOCK_TEXT.lockedOut(code, course.day, course.period) : '';
    const fullName = String(course?.course_name || '').trim();
    const label = `${code} — ${fullName || code}`;
    const link = linkByCode.get(code);
    // A linked card's Pin and Move act on the whole link, and say so.
    const pinLabel = link
      ? (pinned ? (IS_AR ? 'إلغاء تثبيت الاختبارات المرتبطة' : 'Unpin linked exams') : (IS_AR ? 'تثبيت الاختبارات المرتبطة' : 'Pin linked exams'))
      : pinned ? T.pinRemove : (IS_AR ? 'تثبيت' : 'Pin');
    const moveLabel = link ? (IS_AR ? 'نقل الاختبارات المرتبطة' : 'Move linked exams') : (IS_AR ? 'نقل' : 'Move');
    const lockHint = IS_AR ? 'ألغِ التثبيت للنقل' : 'Unpin to move';
    const relatedLabel = IS_AR ? 'عرض الطلاب المشتركين' : 'Show shared students';
    // Locked, Pin and Move stay reachable and say why they do nothing.
    const refused = locked ? ` aria-disabled="true" data-locked-hint="${escapeAttr(lockedHint)}"` : '';
    return examCourseCardMarkup(course, {
      classes: `et-course${pinned ? ' et-pinned' : ''}${locked ? ' et-in-locked' : ''}`, pinned,
      note: locked ? LOCK_TEXT.locked : '',
      attributes: `data-course="${escapeAttr(code)}" draggable="${!pinned && !locked}"${locked ? ' data-locked="true"' : ''}`,
      actions: `<span class="et-schedule-course-actions et-course-card-actions"><button type="button" data-exam-pin="${escapeAttr(code)}" aria-pressed="${pinned}" aria-label="${escapeAttr(`${pinLabel}: ${label}`)}" title="${escapeAttr(locked ? lockedHint : `${pinLabel}: ${label}`)}" ${course?.day === 'OVERFLOW' ? 'disabled' : ''}${refused}>${examCourseIcon('pin')}</button><button type="button" data-exam-move="${escapeAttr(code)}" aria-label="${escapeAttr(`${moveLabel}: ${label}`)}" title="${escapeAttr(locked ? lockedHint : `${pinned ? lockHint : moveLabel}: ${label}`)}" ${pinned && !locked ? 'disabled' : ''}${refused}>${examCourseIcon('move')}</button></span>`,
      extra: `<button type="button" class="et-related-action" data-exam-related="${escapeAttr(course?.course_identity || code)}" aria-label="${escapeAttr(`${relatedLabel}: ${label}`)}" title="${escapeAttr(`${relatedLabel}: ${label}`)}">${examCourseIcon('related')}</button>`
        + (link ? linkLabelMarkup(link.filter(other => other !== code)) : ''),
    });
  };
  // Members sharing a cell are drawn together, in one labelled group.
  const cellChips = codes => {
    const drawn = new Set();
    return codes.map(code => {
      if (drawn.has(code)) return '';
      const link = linkByCode.get(code);
      const together = link ? codes.filter(other => link.includes(other)) : [code];
      together.forEach(other => drawn.add(other));
      if (together.length < 2) return courseChip(code);
      return `<div class="et-link-group" role="group" aria-label="${escapeAttr(linkGroupLabel(together))}" data-link-group="${escapeAttr(together.join('+'))}">${together.map(courseChip).join(' ')}</div>`;
    }).filter(Boolean).join(' ');
  };
  if (!schedule.length) {
    container.style.setProperty('--exam-period-count', '1');
    container.innerHTML = `<p class="text-center text-secondary py-3">${IS_AR ? 'لا توجد بيانات' : 'No data'}</p>`;
    return;
  }

  // Extract ordered unique days and periods from the slots array (preserves creation order)
  const dayOrder = [];
  const periodOrder = [];
  const daySet = new Set();
  const periodSet = new Set();
  for (const s of slots) {
    if (!daySet.has(s.day))    { daySet.add(s.day);       dayOrder.push(s.day); }
    if (!periodSet.has(s.period)) { periodSet.add(s.period); periodOrder.push(s.period); }
  }

  // Build lookup: grid[day][period] = [course_code, ...]
  const grid = {};
  const overflowCourses = [];
  for (const e of schedule) {
    if (e.day === 'OVERFLOW') {
      overflowCourses.push(e.course_code);
      continue;
    }
    if (!grid[e.day]) grid[e.day] = {};
    if (!grid[e.day][e.period]) grid[e.day][e.period] = [];
    grid[e.day][e.period].push(e.course_code);
  }

  // If there are days in schedule not in slots (shouldn't happen, but defensive)
  for (const e of schedule) {
    if (e.day !== 'OVERFLOW' && !daySet.has(e.day)) {
      daySet.add(e.day);
      dayOrder.push(e.day);
    }
    if (e.day !== 'OVERFLOW' && !periodSet.has(e.period)) {
      periodSet.add(e.period);
      periodOrder.push(e.period);
    }
  }

  // Build HTML table
  container.style.setProperty('--exam-period-count', String(Math.max(1, periodOrder.length)));
  let html = `<table class="et-grid"><caption class="visually-hidden">${IS_AR ? 'جدول الاختبارات: الصفوف للأيام والأعمدة لفترات الاختبار.' : 'Exam timetable: rows are days and columns are exam periods.'}</caption>`;

  // Header: corner + periods
  html += '<thead><tr>';
  html += `<th scope="col">${IS_AR ? 'اليوم' : 'Day'}</th>`;
  for (const p of periodOrder) {
    const columnLocked = isColumnLocked(p, locks);
    html += `<th scope="col"${columnLocked ? ' class="et-locked"' : ''}><span class="et-grid-period-label"><bdi dir="ltr">${escapeAttr(p)}</bdi></span>${columnLockMarkup(p, columnLocked)}</th>`;
  }
  html += '</tr></thead>';

  // Body: one row per day
  html += '<tbody>';
  for (const day of dayOrder) {
    const dayLocked = locks.days.has(day);
    html += dayLocked ? '<tr class="et-locked-day">' : '<tr>';
    html += `<th scope="row"${dayLocked ? ' class="et-locked"' : ''}><span class="et-grid-day-label"><bdi dir="ltr">${escapeAttr(day)}</bdi></span>${dayLockMarkup(day, dayLocked)}</th>`;
    for (const period of periodOrder) {
      const courses = (grid[day] && grid[day][period]) ? grid[day][period] : [];
      const locked = isCellLocked(day, period, locks);
      const lockClass = locked ? ' et-locked' : '';
      const lockAttributes = locked ? ' data-locked="true"' : '';
      const lockBar = cellLockMarkup(day, period, locked, dayLocked);
      if (courses.length === 0) {
        html += `<td class="et-empty${lockClass}" data-day="${escapeAttr(day)}" data-period="${escapeAttr(period)}"${lockAttributes}>${lockBar}—</td>`;
      } else {
        html += `<td${locked ? ' class="et-locked"' : ''} data-day="${escapeAttr(day)}" data-period="${escapeAttr(period)}"${lockAttributes}>${lockBar}<div class="et-slot-courses">${cellChips(courses)}</div></td>`;
      }
    }
    html += '</tr>';
  }

  // Overflow row (if any)
  if (overflowCourses.length > 0) {
    html += `<tr class="et-overflow-row">`;
    html += `<th scope="row" class="et-overflow-label"><span class="et-grid-day-label">${T.overflow}</span></th>`;
    html += `<td colspan="${periodOrder.length}"><div class="et-slot-courses">${cellChips(overflowCourses)}</div></td>`;
    html += '</tr>';
  }

  html += '</tbody></table>';
  container.innerHTML = html;
  updateExamGridGeometry();
  if (_foundExamIdentity) {
    const found = currentExamByIdentity(_foundExamIdentity);
    [...container.querySelectorAll('.et-course')].find(chip => chip.dataset.course === found?.course_code)?.classList.add('et-found-exam');
  }
  container.scrollLeft = scrollLeft;
  if (focusedCourse) {
    const chip = [...container.querySelectorAll('.et-course')].find(item => item.dataset.course === focusedCourse);
    (focusedAction ? chip?.querySelector(`[${focusedAction}]`) : chip)?.focus({ preventScroll: true });
  }
  if (focusedLock) {
    [...container.querySelectorAll('[data-lock-day]')].find(button => button.dataset.lockDay === focusedLock[0]
      && (button.dataset.lockPeriod || '') === focusedLock[1])?.focus({ preventScroll: true });
  }
  if (focusedColumn !== null) {
    [...container.querySelectorAll('[data-lock-column]')].find(button => button.dataset.lockColumn === focusedColumn)?.focus({ preventScroll: true });
  }
  $('schedFilter').dispatchEvent(new Event('input'));
  updateEditingStatus();
}

/* ── Schedule Filter ── */
// Three filter modes (auto-detected from input):
//   "AI"   → programme plan: highlight all courses in that programme's study plan
//   "CS*"  → prefix match: highlight courses whose code starts with "CS"
//   "101"  → substring fallback: highlight courses whose code contains "101"
// Multiple space-separated terms are OR'd: "CS101 MATH201" highlights both.
// Matching chips get .et-highlight; non-matching get .et-dim.
$('schedFilter').addEventListener('input', function() {
  const raw = this.value.trim().toLowerCase();
  const allChips = $('schedGrid').querySelectorAll('.et-course');
  const allCells = $('schedGrid').querySelectorAll('td');

  if (!raw) {
    allChips.forEach(c => { c.classList.remove('et-highlight', 'et-dim'); });
    allCells.forEach(c => { c.classList.remove('et-dim'); });
    return;
  }

  // Build a match function for a single term
  function buildMatchFn(term) {
    if (term.endsWith('*')) {
      const prefix = term.slice(0, -1);
      return (code) => code.startsWith(prefix);
    } else if (_programCourses[term]) {
      const progCourses = _programCourses[term];
      return (code) => progCourses.has(code);
    } else {
      if ([...allChips].some(chip => chip.dataset.course.toLowerCase() === term)) return code => code === term;
      return (code) => code.includes(term);
    }
  }

  // Support multiple space-separated terms (OR logic)
  const tokens = raw.match(/\S+(?:\s+\(\d+\))?/g) || [];
  const fns = tokens.map(buildMatchFn);
  const matchFn = (code) => fns.some(fn => fn(code));

  allChips.forEach(chip => {
    const code = (chip.dataset.course || '').toLowerCase();
    if (matchFn(code)) {
      chip.classList.add('et-highlight');
      chip.classList.remove('et-dim');
    } else {
      chip.classList.remove('et-highlight');
      chip.classList.add('et-dim');
    }
  });

  allCells.forEach(cell => {
    if (cell.classList.contains('et-empty')) {
      cell.classList.add('et-dim');
    }
  });
});

/* ── Fixed exam editor and shared pin state ── */
function pinIdentity(course) {
  return course.course_identity || course.course_code;
}

function pinForCourse(code) {
  const course = _courseMetadata[code];
  return course ? _pinnedCourses[pinIdentity(course)] : null;
}

function reconcilePinsWithCourses() {
  const byIdentity = new Map(Object.values(_courseMetadata).map(course => [pinIdentity(course), course]));
  for (const [identity, pin] of Object.entries(_pinnedCourses)) {
    const course = byIdentity.get(identity);
    if (course) {
      pin.course_code = course.course_code;
      pin.course_name = course.course_name || '';
    }
  }
}

function restorePinsFromRun(data) {
  const courses = new Map((data.schedule || []).map(course => [course.course_code, course]));
  const startDay = data.slots?.[0]?.day || $('etStartDay').value;
  _pinnedCourses = {};
  for (const pin of data.pinned || []) {
    const course = courses.get(pin.course_code);
    if (!course) continue;
    const identity = pinIdentity(course);
    _pinnedCourses[identity] = { ...pin, course_identity: identity, course_name: course.course_name || '', day_identity: examDayIdentity(pin.day, startDay) };
  }
}

function pinContext(header) {
  const periodState = header ? { periods: header.periods, errors: [] } : readExamPeriods();
  const days = header?.days || generateDayLabels();
  return {
    days, periods: periodState.periods, selected: new Set(getCheckedValues('courseList')),
    error: periodState.errors[0]?.message || (!days.length ? T.invalidDays : ''),
    errorField: periodState.errors[0]?.field || $('etNumDays'),
  };
}

function pinProblem(pin, context) {
  if (!_coursesLoaded) return T.pinNeedsCourses;
  const course = _courseMetadata[pin.course_code];
  if (!course || pinIdentity(course) !== pin.course_identity) return T.pinUnavailable;
  if (!context.selected.has(pin.course_code)) return T.pinNotSelected;
  if (!context.days.some(day => day === pin.day && examDayIdentity(day) === pin.day_identity)
    || !context.periods.includes(pin.period)) return T.pinSlotUnavailable;
  return lockPinProblem(pin.course_code, pin.day, pin.period);
}

function pinOptions(id, choices, placeholder) {
  const select = $(id);
  const selected = select.value;
  const selectedIdentity = select.selectedOptions[0]?.dataset.identity;
  const retained = choices.find(([value, , identity]) => selectedIdentity ? identity === selectedIdentity : value === selected);
  select.innerHTML = `<option value="">${escapeAttr(placeholder)}</option>` + choices.map(([value, label, identity]) =>
    `<option value="${escapeAttr(value)}"${identity ? ` data-identity="${escapeAttr(identity)}"` : ''}>${escapeAttr(label)}</option>`).join('');
  select.value = retained ? retained[0] : '';
  select.disabled = !_coursesLoaded || !choices.length;
}

function renderPinDayOptions(days) {
  const select = $('examPinDay');
  const selectedIdentity = select.selectedOptions[0]?.dataset.identity;
  if (selectedIdentity) select.dataset.dayIdentity = selectedIdentity;
  const retainedIdentity = select.dataset.dayIdentity;
  const choices = days.map(day => [day, day, examDayIdentity(day)]);
  pinOptions('examPinDay', choices, T.choosePinDay);
  if (retainedIdentity) {
    select.value = choices.find(([, , identity]) => identity === retainedIdentity)?.[0] || '';
  }
}

function selectPinnedDay(pin) {
  const select = $('examPinDay');
  select.dataset.dayIdentity = pin.day_identity;
  select.value = [...select.options].find(option => option.dataset.identity === pin.day_identity)?.value || '';
}

// The native select remains the canonical value/identity authority. The visible
// combobox only searches its options and commits through the existing change path.
// The Fixed exam times and Linked exams pickers share it; `prefix` names their
// elements (examPinCourse, examPinCourseSearch, ...), `onSearch` re-renders
// their editor while the registrar types.
function createCoursePicker(prefix, onSearch) {
  const select = $(`${prefix}Course`), input = $(`${prefix}CourseSearch`);
  const root = $(`${prefix}CoursePicker`), toggle = $(`${prefix}CourseToggle`);
  const popup = $(`${prefix}CoursePopup`), list = $(`${prefix}CourseResults`);
  let searching = false, previousIdentity = '', active = -1, matches = [];
  const compactCode = value => String(value).replace(/[\s\-–—]/g, '').toLocaleLowerCase();
  const options = () => [...select.options].filter(option => option.value);

  function positionPopup() {
    if (popup.hidden) return;
    const rect = root.getBoundingClientRect();
    const below = window.innerHeight - rect.bottom - 12, above = rect.top - 12;
    const upwards = below < 180 && above > below;
    root.dataset.placement = upwards ? 'above' : 'below';
    list.style.maxHeight = `${Math.max(80, Math.min(300, upwards ? above : below))}px`;
  }

  function highlight(index, scroll = false) {
    active = matches.length ? Math.max(0, Math.min(index, matches.length - 1)) : -1;
    [...list.children].forEach((option, i) => {
      option.classList.toggle('is-active', i === active);
      option.setAttribute('aria-selected', String(i === active));
    });
    const option = list.children[active];
    if (!option) input.removeAttribute('aria-activedescendant');
    else {
      input.setAttribute('aria-activedescendant', option.id);
      if (scroll) {
        const top = option.offsetTop, bottom = top + option.offsetHeight;
        if (top < list.scrollTop) list.scrollTop = top;
        else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
      }
    }
  }

  function render() {
    const query = searching ? input.value.trim().toLocaleLowerCase() : '';
    const codeQuery = compactCode(query);
    const availableOptions = options();
    const looksLikeCode = /^[a-z]+\d+(?:\(\d+\))?$/.test(codeQuery);
    const terms = query.split(/\s+/).filter(Boolean);
    matches = availableOptions.filter(option => {
      const course = _courseMetadata[option.value];
      const text = `${option.textContent} ${(course?.programs || []).join(' ')}`.toLocaleLowerCase();
      // Do not assemble "CS 111" from plan CS and course GS111. Names such
      // as "Calculus 1" can match within the name itself, independently of codes.
      if (looksLikeCode) return compactCode(option.value).includes(codeQuery)
        || compactCode(course?.source_course_code || '').includes(codeQuery)
        || compactCode(course?.course_name || '').includes(codeQuery);
      return terms.every(term => text.includes(term))
        || (codeQuery && compactCode(course?.source_course_code || option.value).includes(codeQuery));
    });
    // Code matches lead name/plan matches when a short prefix is entered.
    if (codeQuery) matches.sort((a, b) => Number(compactCode(b.value).startsWith(codeQuery))
      - Number(compactCode(a.value).startsWith(codeQuery)));
    list.innerHTML = matches.map((option, index) => {
      const course = _courseMetadata[option.value];
      const plans = course?.programs?.join(' · ') || '';
      return `<div id="${prefix}CourseOption-${index}" role="option" data-value="${escapeAttr(option.value)}" aria-selected="false" class="et-course-picker-option"><strong dir="ltr">${escapeAttr(option.value)}</strong><span>${escapeAttr(course?.course_name || option.textContent)}</span>${plans ? `<small>${escapeAttr(plans)}</small>` : ''}</div>`;
    }).join('');
    $(`${prefix}CourseEmpty`).hidden = matches.length > 0;
    $(`${prefix}CourseSearchStatus`).textContent = IS_AR ? `${matches.length} مقرر مطابق` : `${matches.length} matching courses`;
    const selectedIndex = matches.findIndex(option => option.value === select.value);
    highlight(selectedIndex < 0 ? 0 : selectedIndex);
    positionPopup();
  }

  function close() {
    popup.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }

  function open() {
    if (input.disabled) return;
    popup.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    toggle.setAttribute('aria-expanded', 'true');
    render();
  }

  function commit(value) {
    if (!options().some(option => option.value === value)) return;
    searching = false;
    previousIdentity = '';
    select.value = value;
    close();
    select.dispatchEvent(new Event('change', { bubbles: true }));
    input.focus();
    close();
  }

  input.addEventListener('focus', () => {
    open();
    if (!searching && select.value) input.select();
  });
  input.addEventListener('click', open);
  input.addEventListener('input', () => {
    if (!searching) previousIdentity = select.selectedOptions[0]?.dataset.identity || '';
    searching = true;
    select.value = '';
    onSearch();
    open();
  });
  input.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (popup.hidden) { open(); highlight(event.key === 'ArrowUp' ? matches.length - 1 : 0, true); }
      else highlight(active + (event.key === 'ArrowDown' ? 1 : -1), true);
    } else if (event.key === 'Enter' && !popup.hidden) {
      event.preventDefault();
      if (matches[active]) commit(matches[active].value);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      if (searching) {
        select.value = options().find(option => option.dataset.identity === previousIdentity)?.value || '';
        searching = false;
        previousIdentity = '';
        select.dispatchEvent(new Event('change', { bubbles: true }));
      }
      close();
    } else if (event.key === 'Tab') close();
  });
  input.addEventListener('blur', close);
  // Keep focus in the input while clicking/tapping a result; do not select on
  // pointerdown, which would mistake a touch-scroll gesture for a selection.
  list.addEventListener('pointerdown', event => {
    if (event.target.closest('[role="option"]')) event.preventDefault();
  });
  list.addEventListener('click', event => {
    const option = event.target.closest('[role="option"]');
    if (option) commit(option.dataset.value);
  });
  toggle.addEventListener('pointerdown', event => event.preventDefault());
  toggle.addEventListener('click', () => {
    const wasOpen = !popup.hidden;
    input.focus();
    if (wasOpen) close(); else open();
  });
  window.addEventListener('resize', positionPopup);
  document.addEventListener('scroll', positionPopup, true);

  return {
    sync() {
      input.disabled = select.disabled;
      toggle.disabled = select.disabled;
      if (input.disabled) { searching = false; previousIdentity = ''; close(); }
      if (!searching) input.value = select.value ? select.selectedOptions[0].textContent : '';
      if (!popup.hidden) render();
    },
    selected() { searching = false; previousIdentity = ''; },
    close,
  };
}
const pinCoursePicker = createCoursePicker('examPin', () => renderPinEditor());
const linkCoursePicker = createCoursePicker('examLink', () => renderLinkEditor());

function renderPinEditor() {
  const pins = Object.entries(_pinnedCourses);
  $('examPinEditor').classList.toggle('d-none', !_coursesLoaded && !pins.length);
  const context = pinContext();
  const courses = Object.values(_courseMetadata).filter(course => context.selected.has(course.course_code));
  pinOptions('examPinCourse', courses.map(course => [course.course_code, `${course.course_code} — ${course.course_name || course.course_code}`, pinIdentity(course)]), T.choosePinCourse);
  pinCoursePicker.sync();
  renderPinDayOptions(context.days);
  pinOptions('examPinPeriod', context.periods.map(period => [period, period]), T.choosePinPeriod);
  $('applyExamPin').disabled = !_coursesLoaded || Boolean(context.error) || !$('examPinCourse').value || !$('examPinDay').value || !$('examPinPeriod').value;
  $('applyExamPin').textContent = groupPin($('examPinCourse').value) ? T.updatePin : T.pinCourse;
  $('examPinCount').textContent = pins.length ? `(${pins.length})` : '';
  $('clearExamPins').classList.toggle('d-none', !pins.length);
  $('examPinTable').classList.toggle('d-none', !pins.length);
  let invalid = false;
  $('examPinRows').innerHTML = pins.sort((a, b) => a[1].course_code.localeCompare(b[1].course_code)).map(([identity, pin]) => {
    const problem = pinProblem(pin, context);
    invalid ||= Boolean(problem);
    return `<tr class="${problem ? 'et-pin-invalid' : ''}" data-pin-identity="${escapeAttr(identity)}">
      <td><strong>${escapeAttr(pin.course_code)}</strong><small class="et-course-name">${escapeAttr(pin.course_name)}</small>${problem ? `<small class="et-pin-problem">${escapeAttr(problem)}</small>` : ''}</td>
      <td>${escapeAttr(pin.day)}</td><td>${escapeAttr(pin.period)}</td>
      <td><div class="et-pin-actions"><button type="button" class="btn btn-sm btn-outline-secondary" data-pin-edit="${escapeAttr(identity)}" aria-label="${escapeAttr(`${T.pinEdit}: ${pin.course_code} ${pin.course_name}`)}">${T.pinEdit}</button><button type="button" class="btn btn-sm btn-outline-secondary" data-pin-remove="${escapeAttr(identity)}" aria-label="${escapeAttr(`${T.pinRemove}: ${pin.course_code} ${pin.course_name}`)}">${T.pinRemove}</button></div></td></tr>`;
  }).join('');
  const selectedCourse = $('examPinCourse').value;
  const selectedDay = $('examPinDay').value;
  const selectedPeriod = $('examPinPeriod').value;
  const selectionStarted = selectedCourse || selectedDay || selectedPeriod;
  const selectionHint = selectionStarted
    ? (!selectedCourse ? T.choosePinCourse : !selectedDay ? T.choosePinDay : !selectedPeriod ? T.choosePinPeriod : '') : '';
  $('examPinNotice').textContent = context.error || (invalid ? T.pinNeedsReview : selectionHint || (pins.length ? T.pinsReady : T.pinsEmpty));
  $('examPinNotice').className = `small mt-2 mb-0 ${invalid || context.error ? 'text-danger' : 'text-secondary'}`;
  // Pins, days, periods and the selection all shape the linked exams too,
  // and whether each lock can be kept.
  renderLinkEditor();
  renderLockEditor();
}

function validatedPinPayload(header) {
  const context = pinContext(header);
  if (Object.values(_pinnedCourses).some(pin => pinProblem(pin, context))) {
    renderPinEditor();
    return inputError(T.pinNeedsReview, $('examPinCourse'));
  }
  return Object.values(_pinnedCourses).map(({ course_code, day, period }) => ({ course_code, day, period }));
}

function manualSnapshot() {
  return { pins: cloneData(_pinnedCourses), links: cloneData(_linkedExams), locks: cloneData(_examLocks), rooms: _roomsForLocks,
    placements: (_currentResultData?.schedule || []).map(entry => ({
      identity: entry.course_identity || entry.course_code, day: entry.day, period: entry.period, slot_index: entry.slot_index,
    })) };
}

function refreshManualEditor() {
  updatePinBar();
  if (_currentResultData) renderScheduleGrid(_currentResultData.schedule, _currentResultData.slots);
}

function runManualCommand(mutate) {
  if (_builderBusy) return false;
  const workspaceAnchor = captureExamWorkspaceAnchor();
  const before = manualSnapshot();
  mutate();
  const after = manualSnapshot();
  if (JSON.stringify(before) === JSON.stringify(after)) return false;
  if (_currentResultData) {
    _undoCommands.push({ before, after });
    if (_undoCommands.length > 100) _undoCommands.shift();
    _redoCommands = [];
  }
  // An edit that went through: a refusal said before it no longer applies.
  clearLockRefusal();
  refreshManualEditor();
  restoreExamWorkspaceAnchor(workspaceAnchor);
  return true;
}

function restoreManualSnapshot(snapshot) {
  const workspaceAnchor = captureExamWorkspaceAnchor();
  _pinnedCourses = cloneData(snapshot.pins);
  _linkedExams = cloneData(snapshot.links || []);
  _examLocks = cloneData(snapshot.locks || []);
  _roomsForLocks = Boolean(snapshot.rooms);
  clearLockRefusal();
  const placements = new Map(snapshot.placements.map(entry => [entry.identity, entry]));
  for (const entry of _currentResultData?.schedule || []) {
    const placement = placements.get(entry.course_identity || entry.course_code);
    if (placement) Object.assign(entry, { day: placement.day, period: placement.period, slot_index: placement.slot_index });
  }
  refreshManualEditor();
  restoreExamWorkspaceAnchor(workspaceAnchor);
}

function undoManualEdit() {
  if (_builderBusy || !_undoCommands.length) return;
  const command = _undoCommands.pop();
  _redoCommands.push(command);
  restoreManualSnapshot(command.before);
}

function redoManualEdit() {
  if (_builderBusy || !_redoCommands.length) return;
  const command = _redoCommands.pop();
  _undoCommands.push(command);
  restoreManualSnapshot(command.after);
}

function setCoursePin(code, day, period) {
  const course = _courseMetadata[code];
  if (!course) return false;
  // Never into or out of a locked cell (rule 6).
  const locked = lockPinProblem(code, day, period);
  if (locked) return refuseLockedEdit(locked);
  // A linked course is pinned with its link: every member, at one time.
  const codes = linkedCodes(code).filter(member => _courseMetadata[member]);
  return runManualCommand(() => {
    for (const member of codes) {
      const meta = _courseMetadata[member];
      _pinnedCourses[pinIdentity(meta)] = { course_code: member, course_identity: pinIdentity(meta), course_name: meta.course_name || '', day, day_identity: examDayIdentity(day), period };
      if (_currentResultData) updateCurrentScheduleMove(member, day, period);
    }
  });
}

function removeCoursePin(identity) {
  return runManualCommand(() => { for (const member of linkIdentities(identity)) delete _pinnedCourses[member]; });
}

function clearCoursePins() {
  return runManualCommand(() => { _pinnedCourses = {}; });
}

// A linked exam moves as one: every member, in one Undo step.
function moveExamCourse(code, day, period) {
  if (_builderBusy) return false;
  const entry = _currentResultData?.schedule?.find(item => item.course_code === code);
  const members = linkedCodes(code).map(member => _currentResultData?.schedule?.find(item => item.course_code === member)).filter(Boolean);
  if (!entry || members.every(item => item.day === day && item.period === period)) return false;
  // Nothing moves into or out of a locked cell (rule 6), before any request.
  const locked = lockMoveProblem(code, day, period);
  if (locked) return refuseLockedEdit(locked);
  if (groupPin(code)) return inputError(IS_AR ? 'ألغِ تثبيت الاختبار قبل نقله، أو عدّل موعده في المواعيد المثبتة.' : 'Unpin to move, or deliberately change its time in Fixed exam times.');
  if (!_currentResultData.slots.some(slot => slot.day === day && slot.period === period)) return false;
  return runManualCommand(() => members.forEach(item => updateCurrentScheduleMove(item.course_code, day, period)));
}

function toggleExamPin(code) {
  const entry = _currentResultData?.schedule?.find(item => item.course_code === code);
  if (!entry || entry.day === 'OVERFLOW' || _builderBusy) return;
  // A card in a locked cell is closed to pins too, until the cell is unlocked.
  const place = lockedPlaceOf(code);
  if (place) { refuseLockedEdit(LOCK_TEXT.lockedOut(code, place.day, place.period)); return; }
  const pin = groupPin(code);
  if (pin) removeCoursePin(pin.course_identity);
  else setCoursePin(code, entry.day, entry.period);
}

$('undoExamBtn')?.addEventListener('click', undoManualEdit);
$('redoExamBtn')?.addEventListener('click', redoManualEdit);
document.addEventListener('keydown', event => {
  if (!event.target.closest?.('#etResults') || event.target.closest('input, textarea, select, [contenteditable="true"]')
      || !(event.ctrlKey || event.metaKey) || event.altKey) return;
  const key = event.key.toLowerCase();
  if (key !== 'z' && key !== 'y') return;
  event.preventDefault();
  if (key === 'y' || event.shiftKey) redoManualEdit();
  else undoManualEdit();
});

$('examPinCourse').addEventListener('change', () => {
  pinCoursePicker.selected();
  const pin = groupPin($('examPinCourse').value);
  if (pin) {
    selectPinnedDay(pin);
    $('examPinPeriod').value = pin.period;
  }
  renderPinEditor();
});
$('examPinDay').addEventListener('change', () => {
  $('examPinDay').dataset.dayIdentity = $('examPinDay').selectedOptions[0]?.dataset.identity || '';
  renderPinEditor();
});
$('examPinPeriod').addEventListener('change', renderPinEditor);
$('clearExamPins').addEventListener('click', clearCoursePins);
$('examPinRows').addEventListener('click', event => {
  const remove = event.target.closest('[data-pin-remove]');
  if (remove) return removeCoursePin(remove.dataset.pinRemove);
  const edit = event.target.closest('[data-pin-edit]');
  if (!edit) return;
  const pin = _pinnedCourses[edit.dataset.pinEdit];
  if (!pin) return;
  const course = _courseMetadata[pin.course_code];
  if (!_coursesLoaded || !course || pinIdentity(course) !== pin.course_identity) {
    return inputError(T.pinUnavailable, $('examPinCourse'));
  }
  if (!pinContext().selected.has(pin.course_code)) {
    return inputError(T.pinNotSelected, $('examPinCourse'));
  }
  $('examPinCourse').value = pin.course_code;
  pinCoursePicker.selected();
  selectPinnedDay(pin);
  $('examPinPeriod').value = pin.period;
  renderPinEditor();
  $('examPinCourseSearch').focus();
});
$('applyExamPin').addEventListener('click', () => {
  const code = $('examPinCourse').value;
  const day = $('examPinDay').value;
  const period = $('examPinPeriod').value;
  const context = pinContext();
  if (context.error) return inputError(context.error, context.errorField);
  if (!_coursesLoaded || !context.selected.has(code)) return inputError(T.choosePinCourse, $('examPinCourse'));
  if (!context.days.includes(day) || !context.periods.includes(period)) return inputError(T.pinSlotUnavailable);
  if (_currentResultData) {
    const slots = context.days.flatMap(d => context.periods.map(p => ({ day: d, period: p })));
    slots.forEach((slot, index) => { slot.index = index; });
    const otherEntries = _currentResultData.schedule.filter(entry => entry.course_code !== code && entry.day !== 'OVERFLOW');
    if (otherEntries.some(entry => !slots.some(slot => slot.day === entry.day && slot.period === entry.period))) {
      return inputError(T.pinSettingsChanged);
    }
    _currentResultData.slots = slots;
    let overflowIndex = slots.length;
    for (const entry of _currentResultData.schedule) {
      const slot = slots.find(candidate => candidate.day === entry.day && candidate.period === entry.period);
      entry.slot_index = slot ? slot.index : overflowIndex++;
    }
  }
  setCoursePin(code, day, period);
});

/* ── Manual placement and independent pin controls ── */
function updatePinBar() {
  const n = Object.keys(_pinnedCourses).length;
  $('pinBar').classList.toggle('d-none', n === 0);
  $('pinCount').textContent = T.pinCount.replace('{n}', n);
  // Update build button text
  $('buildBtn').textContent = n > 0
    ? T.buildPinned.replace('{n}', n)
    : (IS_AR ? 'بناء الجدول' : 'Build Timetable');
  updateLoadedRunActions();
  renderPinEditor();
  updateLinkBar();
}

$('schedGrid').addEventListener('dragstart', event => {
  const chip = event.target.closest('.et-course');
  if (!chip || event.target.closest('button') || _builderBusy || groupPin(chip.dataset.course) || chip.dataset.locked === 'true') {
    event.preventDefault();
    return;
  }
  event.dataTransfer.setData('text/plain', chip.dataset.course);
  event.dataTransfer.effectAllowed = 'move';
  _dragCode = chip.dataset.course;
  _dragRefusedCell = null;
});
// A browser fires no drop on a cell that refused the drag (a locked one), so
// the refusal is said when the drag ends there: the card being dragged, and the
// locked cell the pointer was last over. Tracked page-wide, so a drag that
// left the board for anywhere else ends without a word.
let _dragCode = null;
let _dragRefusedCell = null;
document.addEventListener('dragover', event => {
  if (_dragCode) _dragRefusedCell = event.target?.closest?.('#schedGrid td[data-day][data-locked="true"]') || null;
});
$('schedGrid').addEventListener('dragover', event => {
  const cell = event.target.closest('td[data-day]');
  if (!cell || _builderBusy) return;
  // A locked cell takes no drop: the browser shows it cannot, and so does the cell.
  if (cell.dataset.locked === 'true') {
    $('schedGrid').querySelectorAll('.et-drag-over').forEach(item => item.classList.remove('et-drag-over'));
    cell.classList.add('et-drag-refused');
    return;
  }
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  $('schedGrid').querySelectorAll('.et-drag-over').forEach(item => item.classList.remove('et-drag-over'));
  cell.classList.add('et-drag-over');
});
$('schedGrid').addEventListener('dragleave', event => event.target.closest('td')?.classList.remove('et-drag-over', 'et-drag-refused'));
$('schedGrid').addEventListener('dragend', () => {
  $('schedGrid').querySelectorAll('.et-drag-over, .et-drag-refused').forEach(item => item.classList.remove('et-drag-over', 'et-drag-refused'));
  const code = _dragCode;
  const refused = _dragRefusedCell;
  _dragCode = null;
  _dragRefusedCell = null;
  // Released over a locked cell: nothing moved, and the page says why. A drag
  // that ended anywhere else was last over something that cleared the cell.
  if (!code || !refused?.isConnected || _builderBusy) return;
  const { day, period } = refused.dataset;
  refuseLockedEdit(lockMoveProblem(code, day, period) || LOCK_TEXT.lockedIn(day, period));
});
$('schedGrid').addEventListener('drop', event => {
  event.preventDefault();
  $('schedGrid').querySelectorAll('.et-drag-over, .et-drag-refused').forEach(item => item.classList.remove('et-drag-over', 'et-drag-refused'));
  const cell = event.target.closest('td[data-day]');
  if (!cell) return;
  moveExamCourse(event.dataTransfer.getData('text/plain'), cell.dataset.day, cell.dataset.period);
});
$('schedGrid').addEventListener('dblclick', event => {
  if (event.target.closest('button')) return;
  const chip = event.target.closest('.et-course');
  if (chip) toggleExamPin(chip.dataset.course);
});
$('schedGrid').addEventListener('click', event => {
  const column = event.target.closest('[data-lock-column]');
  if (column) { toggleColumnLock(column.dataset.lockColumn); return; }
  const lock = event.target.closest('[data-lock-day]');
  if (lock) { toggleLock(lock.dataset.lockDay, lock.dataset.lockPeriod); return; }
  const pin = event.target.closest('[data-exam-pin]');
  if (pin) { toggleExamPin(pin.dataset.examPin); return; }
  const move = event.target.closest('[data-exam-move]');
  if (!move || _builderBusy) return;
  // Locked: the button stays reachable, and a press says why it does nothing.
  if (move.dataset.lockedHint) { refuseLockedEdit(move.dataset.lockedHint); return; }
  if (groupPin(move.dataset.examMove)) return;
  openExamMoveDialog(move.dataset.examMove);
});
function openExamMoveDialog(code) {
  const entry = _currentResultData?.schedule?.find(item => item.course_code === code);
  if (!entry || _builderBusy || groupPin(code) || !currentExamIsSelected(entry)) return;
  const place = lockedPlaceOf(code);
  if (place) { refuseLockedEdit(LOCK_TEXT.lockedOut(code, place.day, place.period)); return; }
  _moveCourseCode = entry.course_code;
  showMoveError('');
  const members = linkedCodes(code);
  const help = $('examMoveHelp');
  help.dataset.defaultText ??= help.textContent;
  if (members.length > 1) {
    $('examMoveTitle').textContent = (IS_AR ? 'نقل الاختبارات المرتبطة: ' : 'Move linked exams: ') + members.join(IS_AR ? '، ' : ', ');
    help.textContent = IS_AR ? 'تُنقل المقررات المرتبطة معاً إلى اليوم والفترة المختارين.' : 'Linked courses move together to the day and period you choose.';
  } else {
    $('examMoveTitle').textContent = (IS_AR ? 'نقل الاختبار: ' : 'Move exam: ') + `${entry.course_code} — ${entry.course_name || entry.course_code}`;
    help.textContent = help.dataset.defaultText;
  }
  pinOptions('examMoveDay', uniqueByOrder(_currentResultData.slots.map(slot => slot.day)).map(day => [day, day]), T.choosePinDay);
  pinOptions('examMovePeriod', uniqueByOrder(_currentResultData.slots.map(slot => slot.period)).map(period => [period, period]), T.choosePinPeriod);
  $('examMoveDay').value = entry.day === 'OVERFLOW' ? '' : entry.day;
  $('examMovePeriod').value = entry.day === 'OVERFLOW' ? '' : entry.period;
  const dialog = $('examMoveDialog');
  if (dialog.showModal) dialog.showModal();
  else dialog.setAttribute('open', '');
  $('examMoveDay').focus();
}
function closeExamMoveDialog({ reveal = false } = {}) {
  const dialog = $('examMoveDialog');
  if (dialog?.close) dialog.close();
  else dialog?.removeAttribute('open');
  const chip = [...$('schedGrid').querySelectorAll('.et-course')].find(item => item.dataset.course === _moveCourseCode);
  chip?.querySelector('[data-exam-move]')?.focus({ preventScroll: true });
  if (reveal && chip) revealExamChip(chip);
  _moveCourseCode = null;
}
// A choice the dialog cannot make is said inside it, which stays open.
function showMoveError(message) {
  const error = $('examMoveError');
  if (!error) return;
  error.textContent = message;
  error.hidden = !message;
}
['examMoveDay', 'examMovePeriod'].forEach(id => $(id)?.addEventListener('change', () => showMoveError('')));
$('confirmExamMove')?.addEventListener('click', () => {
  const day = $('examMoveDay').value;
  const period = $('examMovePeriod').value;
  if (!day || !period) {
    (!day ? $('examMoveDay') : $('examMovePeriod')).focus();
    return;
  }
  const locked = lockMoveProblem(_moveCourseCode, day, period);
  if (locked) {
    showMoveError(locked);
    $('examMoveDay').focus();
    return;
  }
  const moved = moveExamCourse(_moveCourseCode, day, period);
  closeExamMoveDialog({ reveal: moved === true });
});
$('cancelExamMove')?.addEventListener('click', closeExamMoveDialog);
$('examMoveDialog')?.addEventListener('cancel', event => { event.preventDefault(); closeExamMoveDialog(); });

// Clear all pins
$('clearPins').addEventListener('click', (e) => {
  e.preventDefault();
  clearCoursePins();
});

/* ── Linked exams: the builder section, the alignment dialog and the board ── */
// A link is courses the committee examines as one exam: always the same day
// and period, moved, pinned and sent to the Overflow slot together. Each
// course keeps its own card, sections and student lists; the board draws the
// members of a link as one group. The server refuses any request that would
// split a link, so every edit here keeps the members together.
const AR_LINK_COURSES = { one: 'ربط مقرر واحد', two: 'ربط مقررين', few: 'ربط {n} مقررات', many: 'ربط {n} مقرراً', other: 'ربط {n} مقرر' };
const AR_LINKED_EXAMS = { one: 'اختبار مرتبط واحد', two: 'اختباران مرتبطان', few: '{n} اختبارات مرتبطة', many: '{n} اختباراً مرتبطاً', other: '{n} اختبار مرتبط' };
// Whole sentences per plural form, so the adjective and the verb agree with the count.
const AR_LINK_CLASH_WARNING = {
  one: 'طالب واحد مسجل في مقررين مرتبطين: يؤدي ورقتين في وقت واحد (تعارض اختبار مرتبط).',
  two: 'طالبان مسجلان في مقررين مرتبطين: يؤدي كلٌّ منهما ورقتين في وقت واحد (تعارض اختبار مرتبط).',
  few: '{n} طلاب مسجلون في مقررين مرتبطين: يؤدي كلٌّ منهم ورقتين في وقت واحد (تعارض اختبار مرتبط).',
  many: '{n} طالباً مسجلاً في مقررين مرتبطين: يؤدي كلٌّ منهم ورقتين في وقت واحد (تعارض اختبار مرتبط).',
  other: '{n} طالب مسجل في مقررين مرتبطين: يؤدي كلٌّ منهم ورقتين في وقت واحد (تعارض اختبار مرتبط).',
};
const AR_LINK_CLASH_REPAIR = {
  one: 'طالب واحد مسجل في مقررين مرتبطين، فيؤدي ورقتين في وقت واحد.',
  two: 'طالبان مسجلان في مقررين مرتبطين، فيؤديان ورقتين في وقت واحد.',
  few: '{n} طلاب مسجلون في مقررين مرتبطين، فيؤدون ورقتين في وقت واحد.',
  many: '{n} طالباً مسجلاً في مقررين مرتبطين، فيؤدون ورقتين في وقت واحد.',
  other: '{n} طالب مسجل في مقررين مرتبطين، فيؤدون ورقتين في وقت واحد.',
};
const AR_MIXED_LINKS = { one: 'ربط واحد يجمع', two: 'ربطان يجمعان', few: '{n} روابط تجمع', many: '{n} ربطاً تجمع', other: '{n} ربط تجمع' };
const AR_ONLINE_LINKED = { one: 'مقرر مرتبط واحد يُدرَّس', two: 'مقرران مرتبطان يُدرَّسان', few: '{n} مقررات مرتبطة تُدرَّس', many: '{n} مقرراً مرتبطاً يُدرَّس', other: '{n} مقرر مرتبط يُدرَّس' };
const LINK_TEXT = {
  chooseCourse: IS_AR ? 'اختر مقرراً لإضافته' : 'Choose a course to add',
  link: IS_AR ? 'ربط المقررات' : 'Link courses',
  linkN: n => (IS_AR ? arabicCount(n, AR_LINK_COURSES) : `Link ${n} courses`),
  unlink: IS_AR ? 'إلغاء الربط' : 'Unlink',
  removePending: IS_AR ? 'إزالة من الربط الجديد' : 'Remove from the new link',
  empty: IS_AR ? 'لا توجد اختبارات مرتبطة. يُجدول كل مقرر محدد بمفرده.' : 'No linked exams. Each selected course is scheduled on its own.',
  ready: IS_AR ? 'تُعقد المقررات المرتبطة دائماً في اليوم والفترة نفسيهما، وتُنقل وتُثبَّت معاً.' : 'Linked courses always sit at the same day and period, and move and pin together.',
  addAnother: IS_AR ? 'أضف مقرراً آخر على الأقل للربط.' : 'Add at least one more course to link.',
  // Names the button as it reads with `n` courses chosen.
  readyToLink: n => (IS_AR ? `انقر «${LINK_TEXT.linkN(n)}» لتُختبر هذه المقررات اختباراً واحداً.` : `Select ${LINK_TEXT.linkN(n)} to examine these courses as one exam.`),
  needsReview: IS_AR ? 'راجع الاختبارات المرتبطة المعلّمة قبل المتابعة.' : 'Resolve the highlighted linked exams before continuing.',
  needsCourses: IS_AR ? 'حمّل المقررات للتحقق من الاختبارات المرتبطة.' : 'Load courses to check the linked exams.',
  tooFew: IS_AR ? 'يحتاج الاختبار المرتبط إلى مقررين على الأقل.' : 'A linked exam needs at least two courses.',
  unavailable: IS_AR ? 'مقرر مرتبط غير موجود ضمن الفلاتر الحالية. أعد تحميله أو ألغِ الربط.' : 'A linked course is unavailable in the current filters. Reload it or unlink.',
  notSelected: IS_AR ? 'أعد تحديد المقررات المرتبطة أو ألغِ ربطها.' : 'Reselect the linked courses or unlink them.',
  pinsDisagree: IS_AR ? 'المقررات المرتبطة مثبتة في مواعيد مختلفة. ثبّتها في موعد واحد أو ألغِ تثبيتها.' : 'The linked courses are pinned to different times. Pin them to one time, or unpin them.',
  split: IS_AR ? 'تُعقد المقررات المرتبطة في مواعيد مختلفة. انقلها معاً أو ألغِ ربطها.' : 'The linked courses sit at different times. Move them together, or unlink them.',
  pinnedApart: codes => (IS_AR
    ? `المقررات ${codes.join('، ')} مثبتة في مواعيد مختلفة. ألغِ تثبيت أحدها أو ثبّتها في موعد واحد قبل ربطها.`
    : `${codes.join(', ')} are pinned to different times. Unpin one, or pin them to one time, before linking them.`),
  linked: codes => (IS_AR ? `تم ربط ${codes.join('، ')}.` : `Linked ${codes.join(', ')}.`),
  unlinked: codes => (IS_AR ? `أُلغي ربط ${codes.join('، ')}.` : `Unlinked ${codes.join(', ')}.`),
  fixed: IS_AR ? 'موعد ثابت' : 'Fixed time',
  atBuild: IS_AR ? 'يُحدد عند البناء' : 'Chosen when you build',
  lastChecked: IS_AR ? 'آخر تحقق: ' : 'Last checked: ',
};
// The server's refusals, by code: [English, Arabic].
const LINK_REFUSALS = {
  linked_exams_invalid: ['This link could not be read. Unlink it and link the courses again.', 'تعذرت قراءة هذا الربط. ألغِ الربط ثم اربط المقررات من جديد.'],
  linked_exams_too_few_members: ['A linked exam needs at least two courses.', 'يحتاج الاختبار المرتبط إلى مقررين على الأقل.'],
  linked_exams_course_repeated: ['A course can be in only one linked exam.', 'لا يمكن أن يكون المقرر في أكثر من اختبار مرتبط واحد.'],
  linked_exams_course_not_selected: ['A linked course is not selected for this timetable. Reselect it or unlink it.', 'مقرر مرتبط غير محدد لهذا الجدول. أعد تحديده أو ألغِ الربط.'],
  linked_exams_pins_disagree: ['Linked courses are pinned to different times. Pin them to one time, or unpin them.', 'المقررات المرتبطة مثبتة في مواعيد مختلفة. ثبّتها في موعد واحد أو ألغِ تثبيتها.'],
  linked_exams_split: ['Linked courses must sit at the same day and period. Move them together, then check again.', 'يجب أن تُعقد المقررات المرتبطة في اليوم والفترة نفسيهما. انقلها معاً ثم تحقق مجدداً.'],
};

// Why the server refused a locked day or period: the page words every refusal
// itself, then names the cell and the exams the server named. The lock buttons
// come next; until then only a refusal of a request can mention a lock.
const LOCK_REFUSALS = {
  exam_locks_invalid: ['This lock could not be read. Unlock it, then lock the day or period again.', 'تعذرت قراءة هذا القفل. ألغِ القفل ثم اقفل اليوم أو الفترة من جديد.'],
  exam_locks_outside_timetable: ['A locked day or period is not in this timetable any more. Unlock it, or add the day and period back.', 'يوم أو فترة مقفلة لم تعد في هذا الجدول. ألغِ القفل، أو أعد إضافة اليوم والفترة.'],
  exam_locks_repeated: ['This day or period is locked twice.', 'هذا اليوم أو هذه الفترة مقفلة مرتين.'],
  exam_locks_source_required: ['A lock keeps a saved timetable as it is. Load the saved timetable, then lock it.', 'يُبقي القفل الجدول المحفوظ كما هو. حمّل الجدول المحفوظ ثم اقفله.'],
  exam_locks_source_incomplete: ['This saved timetable has no complete rooms for its locked exams. Check changes, then Save Changes, to assign its rooms again. Then lock it.', 'لا تتوفر في هذا الجدول المحفوظ قاعات كاملة للاختبارات المقفلة. افحص التغييرات ثم احفظها لإعادة توزيع قاعاته، ثم اقفله.'],
  exam_locks_scope_changed: ['The locked days were saved for other programs or sections. Choose the saved programs and sections, or unlock the days.', 'حُفظت الأيام المقفلة لبرامج أو شعب أخرى. اختر البرامج والشعب المحفوظة، أو ألغِ قفل الأيام.'],
  exam_locks_term_changed: ['The locked exams were saved for another academic term. Unlock them to rebuild the timetable for this term.', 'حُفظت الاختبارات المقفلة لفصل دراسي آخر. ألغِ قفلها لإعادة بناء الجدول لهذا الفصل.'],
  exam_locks_course_not_selected: ['An exam of a locked day or period is not selected for this timetable. Select it again, or unlock it.', 'اختبار في يوم أو فترة مقفلة غير محدد لهذا الجدول. أعد تحديده، أو ألغِ القفل.'],
  exam_locks_link_outside: ['A locked exam is linked to an exam outside its locked period. Unlock it, or unlink the courses.', 'اختبار مقفل مرتبط باختبار خارج فترته المقفلة. ألغِ القفل، أو ألغِ ربط المقررات.'],
  exam_locks_link_room_shared: ['These linked courses share a room in a locked period. Unlock it before unlinking them.', 'تتشارك هذه المقررات المرتبطة قاعة في فترة مقفلة. ألغِ القفل قبل إلغاء ربطها.'],
  exam_locks_pinned_elsewhere: ['This exam is in a locked period. Unlock it to pin the exam elsewhere.', 'هذا الاختبار في فترة مقفلة. ألغِ القفل لتثبيته في موعد آخر.'],
  exam_locks_pin_in_locked_cell: ['An exam cannot be pinned to a locked period. Unlock it first.', 'لا يمكن تثبيت اختبار في فترة مقفلة. ألغِ القفل أولاً.'],
  exam_locks_moved_out: ['This exam is in a locked period. Unlock it to move the exam.', 'هذا الاختبار في فترة مقفلة. ألغِ القفل لنقل الاختبار.'],
  exam_locks_moved_in: ['An exam cannot be placed in a locked period. Unlock it first.', 'لا يمكن وضع اختبار في فترة مقفلة. ألغِ القفل أولاً.'],
  exam_locks_cell_unsaved: ['This day or period has unsaved changes. Save the timetable, then lock it.', 'في هذا اليوم أو هذه الفترة تغييرات غير محفوظة. احفظ الجدول ثم اقفله.'],
};

function examLocksRefusal(data) {
  const message = (LOCK_REFUSALS[data.code] || LOCK_REFUSALS.exam_locks_invalid)[IS_AR ? 1 : 0];
  const cell = data.cell && typeof data.cell === 'object'
    ? [data.cell.day, data.cell.period].filter(part => typeof part === 'string' && part).join(' ')
    : '';
  const courses = Array.isArray(data.courses) ? data.courses.map(String).filter(Boolean).join(', ') : '';
  // Day labels, periods and course codes read left to right inside Arabic.
  const named = [cell, courses].filter(Boolean).map(text => (IS_AR ? isolateLtr(text) : text));
  const error = new Error(named.length ? `${message} (${named.join(IS_AR ? '، ' : '; ')})` : message);
  error.examRequestKind = 'exam-locks';
  error.examLockCode = data.code;
  return error;
}

// Order-free: the server saves links in code order, the page in the order made.
function linkSignatureOf(links) {
  return JSON.stringify((Array.isArray(links) ? links : [])
    .map(link => JSON.stringify((link?.members || []).map(member => String(member?.course_identity || '')).sort()))
    .sort());
}
function linkSignature() {
  return linkSignatureOf(_linkedExams);
}

// What a notice in the section was said of: the links, the courses chosen and
// the pins. Once any of them changes, it no longer describes the page.
function linkNoticeKey() {
  const pins = Object.entries(_pinnedCourses).map(([identity, pin]) => [identity, pin.day, pin.period]).sort();
  return JSON.stringify([linkSignature(), _pendingLinkMembers, pins]);
}
function sayLinkNotice(text, error = false) {
  _linkNotice = { text, error, key: linkNoticeKey() };
}

// Links made or undone since the save, each once: `saved` says it was the
// saved run's (and is undone now), else it is new.
function linkChangesSinceSave() {
  if (!_savedResultData) return [];
  const savedEntries = new Map((_savedResultData.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
  const saved = new Map();
  for (const link of Array.isArray(_savedResultData.linked_exams) ? _savedResultData.linked_exams : []) {
    const members = (Array.isArray(link?.members) ? link.members : []).map(member => {
      const identity = String(member?.course_identity || '');
      const entry = currentExamByIdentity(identity) || savedEntries.get(identity);
      return { course_identity: identity, course_code: entry?.course_code || String(member?.course_code || identity), course_name: entry?.course_name || '' };
    }).filter(member => member.course_identity);
    if (members.length) saved.set(linkSignatureOf([{ members }]), members);
  }
  const current = new Map(_linkedExams.map(link => [linkSignatureOf([link]), link.members]));
  return [
    ...[...current].filter(([key]) => !saved.has(key)).map(([key, members]) => ({ key, members, saved: false })),
    ...[...saved].filter(([key]) => !current.has(key)).map(([key, members]) => ({ key, members, saved: true })),
  ];
}

// What a request carries: identity is the authority, the code a courtesy.
function linkPayload() {
  return _linkedExams.map(link => ({
    members: link.members.map(({ course_identity, course_code }) => ({ course_identity, course_code })),
  }));
}

function scheduleEntryFor(code) {
  return _currentResultData?.schedule?.find(entry => entry.course_code === code) || null;
}

function courseForIdentity(identity) {
  return Object.values(_courseMetadata).find(course => pinIdentity(course) === identity)
    || _currentResultData?.schedule?.find(entry => (entry.course_identity || entry.course_code) === identity) || null;
}

function identityOfCode(code) {
  const course = _courseMetadata[code] || scheduleEntryFor(code);
  return course ? pinIdentity(course) : code;
}

function linkForIdentity(identity) {
  return _linkedExams.find(link => link.members.some(member => member.course_identity === identity)) || null;
}

// The identities pinned, unpinned or moved together with `identity`.
function linkIdentities(identity) {
  const link = linkForIdentity(identity);
  return link ? link.members.map(member => member.course_identity) : [identity];
}

// The codes that move and pin as one with `code`: its link's, or itself.
function linkedCodes(code) {
  const link = linkForIdentity(identityOfCode(code));
  return link ? link.members.map(member => member.course_code) : [code];
}

// A link is pinned when any member is: it cannot move without all of them.
function groupPin(code) {
  for (const member of linkedCodes(code)) {
    const pin = pinForCourse(member);
    if (pin) return pin;
  }
  return null;
}

// Each linked code to its link's codes, for one render of the board.
function linkedCodeIndex() {
  const index = new Map();
  for (const link of _linkedExams) {
    const codes = link.members.map(member => member.course_code);
    for (const code of codes) index.set(code, codes);
  }
  return index;
}

function linkLabelMarkup(partners) {
  if (!partners.length) return '';
  const codes = partners.map(code => `<bdi dir="ltr">${escapeAttr(code)}</bdi>`).join(IS_AR ? '، ' : ', ');
  return `<span class="et-link-label"><span class="et-link-icon" aria-hidden="true">${examCourseIcon('link')}</span><span>${IS_AR ? 'مع' : 'with'} ${codes}</span></span>`;
}

function linkGroupLabel(codes) {
  return (IS_AR ? 'اختبار مرتبط: ' : 'Linked exam: ') + codes.join(IS_AR ? '، ' : ', ');
}

function restoreLinksFromRun(data) {
  const entries = new Map((data.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
  _linkedExams = (Array.isArray(data.linked_exams) ? data.linked_exams : []).map(link => ({
    members: (Array.isArray(link?.members) ? link.members : []).map(member => {
      const identity = String(member?.course_identity || '');
      const entry = entries.get(identity);
      return { course_identity: identity, course_code: entry?.course_code || String(member?.course_code || identity), course_name: entry?.course_name || '' };
    }).filter(member => member.course_identity),
  })).filter(link => link.members.length);
  _pendingLinkMembers = [];
  _linkError = null;
  _linkNotice = null;
}

// Display codes are renumbered with the population; identities are not.
function reconcileLinksWithCourses() {
  const byIdentity = new Map(Object.values(_courseMetadata).map(course => [pinIdentity(course), course]));
  for (const link of _linkedExams) {
    for (const member of link.members) {
      const course = byIdentity.get(member.course_identity);
      if (course) {
        member.course_code = course.course_code;
        member.course_name = course.course_name || '';
      }
    }
  }
  _pendingLinkMembers = _pendingLinkMembers.filter(identity => byIdentity.has(identity));
}

const linkPlaceKey = entry => (!entry ? '' : entry.day === 'OVERFLOW' ? 'OVERFLOW' : JSON.stringify([entry.day, entry.period]));

// Why a link cannot go to the server as it stands, or ''.
function linkProblem(link, context) {
  if (!_coursesLoaded) return LINK_TEXT.needsCourses;
  if (link.members.length < 2) return LINK_TEXT.tooFew;
  for (const member of link.members) {
    const course = _courseMetadata[member.course_code];
    if (!course || pinIdentity(course) !== member.course_identity) return LINK_TEXT.unavailable;
    if (!context.selected.has(member.course_code)) return LINK_TEXT.notSelected;
  }
  const pinTimes = new Set(link.members.map(member => pinForCourse(member.course_code)).filter(Boolean)
    .map(pin => JSON.stringify([pin.day, pin.period])));
  if (pinTimes.size > 1) return LINK_TEXT.pinsDisagree;
  if (_currentResultData?.schedule?.length
    && new Set(link.members.map(member => linkPlaceKey(scheduleEntryFor(member.course_code)))).size > 1) return LINK_TEXT.split;
  return '';
}

function validatedLinkPayload() {
  const context = pinContext();
  if (_linkedExams.some(link => linkProblem(link, context))) {
    renderLinkEditor();
    return inputError(LINK_TEXT.needsReview, $('examLinkRows').querySelector('.et-link-invalid [data-link-remove]') || $('examLinkCourse'));
  }
  return linkPayload();
}

// A refusal from the server, worded for the registrar and kept for its row.
function linkedExamsRefusal(code, field) {
  const match = /^linked_exams\[(\d+)\](?:\.members\[(\d+)\])?/.exec(String(field || ''));
  const link = match ? _linkedExams[Number(match[1])] : null;
  const message = (LINK_REFUSALS[code] || LINK_REFUSALS.linked_exams_invalid)[IS_AR ? 1 : 0];
  const codes = link ? link.members.map(member => member.course_code).join(' + ') : '';
  const error = new Error(codes ? (IS_AR ? `الاختبار المرتبط ${codes}: ${message}` : `Linked exam ${codes}: ${message}`) : message);
  error.examRequestKind = 'linked-exams';
  error.examLinkIndex = link ? Number(match[1]) : null;
  error.examLinkMember = link && match[2] !== undefined ? Number(match[2]) : null;
  error.examLinkMessage = message;
  return error;
}

function recordLinkError(error) {
  _linkError = { signature: linkSignature(), index: error.examLinkIndex, member: error.examLinkMember, message: error.examLinkMessage };
  _linkNotice = null;
  renderLinkEditor();
}

function reviewLinkedExams() {
  if (_builderBusy) return;
  if ($('examSetupDetails')) $('examSetupDetails').open = true;
  renderLinkEditor();
  const target = $('examLinkRows').querySelector('.et-link-invalid [data-link-remove]');
  if (target) target.focus({ preventScroll: true });
  else {
    $('examLinkHeading').setAttribute('tabindex', '-1');
    $('examLinkHeading').focus({ preventScroll: true });
  }
  $('examLinkEditor').scrollIntoView({ block: 'center', behavior: 'smooth' });
}

function linkWarnings(counts) {
  const items = [];
  const students = Number(counts?.students_in_two_linked_courses) || 0;
  const mixed = Number(counts?.mixed_credit_links) || 0;
  const online = Number(counts?.online_courses) || 0;
  if (students) {
    items.push(IS_AR
      ? arabicCount(students, AR_LINK_CLASH_WARNING)
      : `${students} student${students === 1 ? ' is' : 's are'} registered in two linked courses: each sits two papers at one time (a linked-exam clash).`);
  }
  if (mixed) {
    items.push(IS_AR
      ? `${arabicCount(mixed, AR_MIXED_LINKS)} مقررات بساعات معتمدة مختلفة.`
      : `${mixed} link${mixed === 1 ? ' joins' : 's join'} courses with different credit hours.`);
  }
  if (online) {
    items.push(IS_AR
      ? `${arabicCount(online, AR_ONLINE_LINKED)} عن بُعد.`
      : `${online} linked course${online === 1 ? ' is' : 's are'} taught online.`);
  }
  return items;
}

// What the last check or build counted about the links: never a reason to
// refuse a save, and labelled as the last check's while edits are unchecked.
function currentLinkWarnings() {
  const counts = _currentResultData?.qa?.linked_exams;
  if (!_linkedExams.length || !counts) return [];
  const previous = hasCurrentEvaluation() ? '' : LINK_TEXT.lastChecked;
  return linkWarnings(counts).map(text => previous + text);
}

function renderLinkWarnings() {
  const warnings = currentLinkWarnings();
  const html = warnings.map(text => `<li>${escapeAttr(text)}</li>`).join('');
  if ($('examLinkWarningList').innerHTML !== html) $('examLinkWarningList').innerHTML = html;
  $('examLinkWarnings').hidden = !warnings.length;
}

function updateLinkBar() {
  const bar = $('linkBar');
  if (!bar) return;
  const count = _linkedExams.length;
  bar.classList.toggle('d-none', !count || !_currentResultData);
  $('linkCount').textContent = IS_AR ? arabicCount(count, AR_LINKED_EXAMS) : `${count} linked exam${count === 1 ? '' : 's'}`;
  const warnings = currentLinkWarnings().join(' ');
  if ($('linkBarWarnings').textContent !== warnings) $('linkBarWarnings').textContent = warnings;
}

function linkPlaceMarkup(entry) {
  if (!entry) return '—';
  return entry.day === 'OVERFLOW' ? escapeAttr(T.overflow) : `<bdi dir="ltr">${escapeAttr(`${entry.day} · ${entry.period}`)}</bdi>`;
}

function linkTimeMarkup(link) {
  const pin = link.members.map(member => pinForCourse(member.course_code)).find(Boolean);
  if (pin) return `${linkPlaceMarkup(pin)}<small>${LINK_TEXT.fixed}</small>`;
  const entry = link.members.map(member => scheduleEntryFor(member.course_code)).find(Boolean);
  return entry ? linkPlaceMarkup(entry) : `<small>${LINK_TEXT.atBuild}</small>`;
}

let _linkRowsMarkup = '';
let _linkPendingMarkup = '';
function renderLinkEditor() {
  const editor = $('examLinkEditor');
  if (!editor) return;
  editor.classList.toggle('d-none', !_coursesLoaded && !_linkedExams.length);
  if (_linkError && _linkError.signature !== linkSignature()) _linkError = null;
  const context = pinContext();
  const linked = new Set(_linkedExams.flatMap(link => link.members.map(member => member.course_identity)));
  const selectedCourses = Object.values(_courseMetadata).filter(course => context.selected.has(course.course_code));
  const selectedIdentities = new Set(selectedCourses.map(pinIdentity));
  _pendingLinkMembers = _pendingLinkMembers.filter(identity => !linked.has(identity) && selectedIdentities.has(identity));
  if (_linkNotice && _linkNotice.key !== linkNoticeKey()) _linkNotice = null;
  const choices = selectedCourses
    .filter(course => !linked.has(pinIdentity(course)) && !_pendingLinkMembers.includes(pinIdentity(course)))
    .sort((a, b) => a.course_code.localeCompare(b.course_code, 'en', { numeric: true }))
    .map(course => [course.course_code, `${course.course_code} — ${course.course_name || course.course_code}`, pinIdentity(course)]);
  pinOptions('examLinkCourse', choices, LINK_TEXT.chooseCourse);
  // Kept usable when every course is linked or chosen: it then says so, and
  // the search a registrar is typing in never goes disabled under them.
  $('examLinkCourse').disabled = !_coursesLoaded;
  linkCoursePicker.sync();

  const pendingHtml = _pendingLinkMembers.map(identity => {
    const course = courseForIdentity(identity);
    const code = course?.course_code || identity;
    const label = `${LINK_TEXT.removePending}: ${code} — ${course?.course_name || code}`;
    return `<li class="et-link-chip" data-link-pending="${escapeAttr(identity)}"><bdi dir="ltr" class="et-link-code">${escapeAttr(code)}</bdi><span class="et-link-chip-name">${escapeAttr(course?.course_name || '')}</span><button type="button" class="et-link-chip-remove" data-link-pending-remove="${escapeAttr(identity)}" aria-label="${escapeAttr(label)}" title="${escapeAttr(label)}">&times;</button></li>`;
  }).join('');
  if (pendingHtml !== _linkPendingMarkup) {
    $('examLinkPending').innerHTML = pendingHtml;
    _linkPendingMarkup = pendingHtml;
  }
  $('examLinkPending').hidden = !_pendingLinkMembers.length;
  const apply = $('applyExamLink');
  apply.disabled = !_coursesLoaded || _pendingLinkMembers.length < 2;
  apply.textContent = _pendingLinkMembers.length >= 2 ? LINK_TEXT.linkN(_pendingLinkMembers.length) : LINK_TEXT.link;

  $('examLinkCount').textContent = _linkedExams.length ? `(${_linkedExams.length})` : '';
  $('examLinkTable').classList.toggle('d-none', !_linkedExams.length);
  let invalid = false;
  const rowsHtml = _linkedExams.map((link, index) => {
    const refused = _linkError?.index === index ? _linkError : null;
    const problem = refused?.message || linkProblem(link, context);
    invalid ||= Boolean(problem);
    const codes = link.members.map(member => member.course_code).join(IS_AR ? '، ' : ', ');
    const members = link.members.map((member, position) => `<span class="et-link-member${refused?.member === position ? ' is-invalid' : ''}"><bdi dir="ltr" class="et-link-code">${escapeAttr(member.course_code)}</bdi><small class="et-course-name">${escapeAttr(member.course_name || '')}</small></span>`).join('');
    return `<tr class="${problem ? 'et-link-invalid' : ''}" data-link-index="${index}"><td><div class="et-link-members">${members}</div>${problem ? `<small class="et-link-problem">${escapeAttr(problem)}</small>` : ''}</td><td class="et-link-time">${linkTimeMarkup(link)}</td><td><button type="button" class="btn btn-sm btn-outline-secondary" data-link-remove="${index}" aria-label="${escapeAttr(`${LINK_TEXT.unlink}: ${codes}`)}">${LINK_TEXT.unlink}</button></td></tr>`;
  }).join('');
  if (rowsHtml !== _linkRowsMarkup) {
    // Rebuilt only when it changed, so a focused Unlink is not dropped.
    const focused = document.activeElement?.closest?.('[data-link-remove]')?.dataset.linkRemove;
    $('examLinkRows').innerHTML = rowsHtml;
    _linkRowsMarkup = rowsHtml;
    if (focused !== undefined) $('examLinkRows').querySelector(`[data-link-remove="${focused}"]`)?.focus({ preventScroll: true });
  }
  renderLinkWarnings();

  let text = '', error = false;
  // The refusal of the click just made, then any problem with the links as
  // they stand: a success message never covers either.
  if (_linkNotice?.error) ({ text, error } = _linkNotice);
  else if (invalid) { text = LINK_TEXT.needsReview; error = true; }
  else if (_linkError) { text = _linkError.message; error = true; }
  else if (_linkNotice) text = _linkNotice.text;
  else if (_pendingLinkMembers.length === 1) text = LINK_TEXT.addAnother;
  else if (_pendingLinkMembers.length > 1) text = LINK_TEXT.readyToLink(_pendingLinkMembers.length);
  else text = _linkedExams.length ? LINK_TEXT.ready : LINK_TEXT.empty;
  if ($('examLinkNotice').textContent !== text) $('examLinkNotice').textContent = text;
  $('examLinkNotice').className = `small mt-2 mb-0 ${error ? 'text-danger' : 'text-secondary'}`;
}

// How the chosen courses come to share one time. On a board they may sit
// apart: the registrar then chooses among their current times, unless one is
// pinned - its time is the only one. Courses pinned to two times cannot link.
function linkAlignment(identities) {
  const codes = identities.map(identity => courseForIdentity(identity)?.course_code || identity);
  const pins = codes.map(code => [code, pinForCourse(code)]).filter(([, pin]) => pin);
  const pinTimes = [...new Map(pins.map(([, pin]) => [JSON.stringify([pin.day, pin.period]), pin])).values()];
  if (pinTimes.length > 1) return { error: LINK_TEXT.pinnedApart(pins.map(([code]) => code)) };
  const pin = pinTimes[0] || null;
  if (!_currentResultData?.schedule?.length) return { place: pin ? { day: pin.day, period: pin.period } : null };
  const entries = codes.map(scheduleEntryFor);
  if (pin) {
    const at = entry => entry && entry.day === pin.day && entry.period === pin.period;
    if (entries.every(at)) return { place: null };
    return { options: [{ day: pin.day, period: pin.period, codes: codes.filter((code, index) => at(entries[index])) }], preferred: 0, pinned: pins.map(([code]) => code) };
  }
  const placed = codes.map((code, index) => ({ code, entry: entries[index] })).filter(({ entry }) => entry && entry.day !== 'OVERFLOW');
  if (!placed.length) return { place: null };
  const options = [];
  for (const { code, entry } of [...placed].sort((a, b) => a.entry.slot_index - b.entry.slot_index)) {
    const option = options.find(item => item.day === entry.day && item.period === entry.period);
    if (option) option.codes.push(code);
    else options.push({ day: entry.day, period: entry.period, codes: [code] });
  }
  if (options.length === 1 && placed.length === entries.length) return { place: null };
  // The largest course's time first, as the server prefers it; lowest code on a tie.
  const size = code => Number(_courseMetadata[code]?.enrolled_count ?? scheduleEntryFor(code)?.enrolled_count) || 0;
  const leader = [...placed].sort((a, b) => (size(b.code) - size(a.code)) || a.code.localeCompare(b.code, 'en', { numeric: true }))[0];
  return { options, preferred: options.findIndex(option => option.codes.includes(leader.code)) };
}

// One Undo step: the link, its members moved to `place`, and - when one of
// them is pinned - every member pinned at that time, as the link is.
function linkCourses(identities, place) {
  const members = identities.map(identity => {
    const course = courseForIdentity(identity);
    return { course_identity: identity, course_code: course?.course_code || identity, course_name: course?.course_name || '' };
  });
  const pin = members.map(member => pinForCourse(member.course_code)).find(Boolean);
  return runManualCommand(() => {
    _linkedExams.push({ members });
    for (const member of members) {
      if (pin && _courseMetadata[member.course_code]) {
        _pinnedCourses[member.course_identity] = { course_code: member.course_code, course_identity: member.course_identity, course_name: member.course_name, day: pin.day, day_identity: pin.day_identity, period: pin.period };
      }
      if (place && _currentResultData) updateCurrentScheduleMove(member.course_code, place.day, place.period);
    }
  });
}

function finishLink(identities, place) {
  const codes = identities.map(identity => courseForIdentity(identity)?.course_code || identity);
  if (!linkCourses(identities, place)) return false;
  _pendingLinkMembers = [];
  sayLinkNotice(LINK_TEXT.linked(codes));
  renderLinkEditor();
  $('examLinkCourseSearch').focus({ preventScroll: true });
  linkCoursePicker.close();
  return true;
}

function openLinkDialog(identities, plan) {
  _linkDialog = { identities, options: plan.options };
  const codes = identities.map(identity => courseForIdentity(identity)?.course_code || identity);
  const list = items => items.map(code => `<bdi dir="ltr">${escapeAttr(code)}</bdi>`).join(IS_AR ? '، ' : ', ');
  $('examLinkDialogHelp').innerHTML = plan.pinned
    ? (IS_AR
      ? `${list(plan.pinned)} مثبّت، لذا تأخذ الاختبارات المرتبطة موعده وتُنقل البقية إليه.`
      : `${list(plan.pinned)} ${plan.pinned.length === 1 ? 'is' : 'are'} pinned, so the linked exams take that time and the others move to it.`)
    : (IS_AR
      ? 'تُعقد هذه المقررات الآن في مواعيد مختلفة. اختر الموعد الذي ستشترك فيه، فتُنقل البقية إليه. يمكنك التراجع عن ذلك.'
      : 'These courses sit at different times now. Choose the time they will share; the others move to it. Undo reverses it.');
  $('examLinkDialogMembers').innerHTML = codes.map(code => `<li><bdi dir="ltr" class="et-link-code">${escapeAttr(code)}</bdi><span>${linkPlaceMarkup(scheduleEntryFor(code))}</span></li>`).join('');
  $('examLinkOptions').innerHTML = plan.options.map((option, index) => {
    const where = option.codes.length
      ? (IS_AR ? `الموعد الحالي لـ${list(option.codes)}` : `where ${list(option.codes)} ${option.codes.length === 1 ? 'sits' : 'sit'} now`)
      : '';
    return `<label class="et-link-option"><input type="radio" name="examLinkOption" value="${index}"${index === plan.preferred ? ' checked' : ''}><span><bdi dir="ltr">${escapeAttr(`${option.day} · ${option.period}`)}</bdi>${where ? `<small>${where}</small>` : ''}</span></label>`;
  }).join('');
  const dialog = $('examLinkDialog');
  if (dialog.showModal) dialog.showModal();
  else dialog.setAttribute('open', '');
  ($('examLinkOptions').querySelector('input:checked') || $('examLinkOptions').querySelector('input'))?.focus();
}

function closeLinkDialog({ restoreFocus = false } = {}) {
  const dialog = $('examLinkDialog');
  if (dialog?.close) dialog.close();
  else dialog?.removeAttribute('open');
  _linkDialog = null;
  if (restoreFocus) ($('applyExamLink').disabled ? $('examLinkCourseSearch') : $('applyExamLink')).focus({ preventScroll: true });
}

$('examLinkCourse').addEventListener('change', () => {
  linkCoursePicker.selected();
  const course = _courseMetadata[$('examLinkCourse').value];
  if (course && !_pendingLinkMembers.includes(pinIdentity(course))) {
    _pendingLinkMembers.push(pinIdentity(course));
    _linkNotice = null;
  }
  $('examLinkCourse').value = '';
  renderLinkEditor();
});
$('examLinkPending').addEventListener('click', event => {
  const remove = event.target.closest('[data-link-pending-remove]');
  if (!remove || _builderBusy) return;
  const index = _pendingLinkMembers.indexOf(remove.dataset.linkPendingRemove);
  _pendingLinkMembers = _pendingLinkMembers.filter(identity => identity !== remove.dataset.linkPendingRemove);
  _linkNotice = null;
  renderLinkEditor();
  const buttons = [...$('examLinkPending').querySelectorAll('[data-link-pending-remove]')];
  (buttons[Math.min(index, buttons.length - 1)] || $('examLinkCourseSearch')).focus({ preventScroll: true });
});
$('applyExamLink').addEventListener('click', () => {
  if (_builderBusy) return;
  const identities = [..._pendingLinkMembers];
  if (!_coursesLoaded || identities.length < 2) return inputError(LINK_TEXT.addAnother, $('examLinkCourse'));
  const plan = linkAlignment(identities);
  const lockedMember = identities.map(identity => courseForIdentity(identity)?.course_code).filter(Boolean)
    .map(code => [code, lockedPlaceOf(code)]).find(([, place]) => place);
  if (!plan.error && lockedMember) {
    const [code, place] = lockedMember;
    const together = identities.every(identity => {
      const entry = currentExamByIdentity(identity);
      return entry && entry.day === place.day && entry.period === place.period;
    });
    // Two courses already in one locked cell may link: nothing moves.
    if (!together) plan.error = LOCK_TEXT.linkLocked(code, place.day, place.period);
  }
  if (plan.error) {
    // Said beside the button, which keeps focus: focusing the course search
    // would open its list over the reason.
    sayLinkNotice(plan.error, true);
    renderLinkEditor();
    $('applyExamLink').focus({ preventScroll: true });
    return null;
  }
  if (plan.options?.length) return openLinkDialog(identities, plan);
  return finishLink(identities, plan.place);
});
$('examLinkRows').addEventListener('click', event => {
  const remove = event.target.closest('[data-link-remove]');
  if (!remove || _builderBusy) return;
  const index = Number(remove.dataset.linkRemove);
  const link = _linkedExams[index];
  if (!link) return;
  const codes = link.members.map(member => member.course_code);
  if (linkSharesLockedRoom(link)) {
    sayLinkNotice(LOCK_REFUSALS.exam_locks_link_room_shared[IS_AR ? 1 : 0], true);
    renderLinkEditor();
    remove.focus({ preventScroll: true });
    return;
  }
  if (!runManualCommand(() => { _linkedExams.splice(index, 1); })) return;
  sayLinkNotice(LINK_TEXT.unlinked(codes));
  renderLinkEditor();
  const buttons = [...$('examLinkRows').querySelectorAll('[data-link-remove]')];
  (buttons[Math.min(index, buttons.length - 1)] || $('examLinkCourseSearch')).focus({ preventScroll: true });
});
// Linked courses whose saved room is shared inside a locked cell: unlinking
// them would leave that saved room double booked (the server refuses it too).
function linkSharesLockedRoom(link) {
  if (!_savedResultData || !_examLocks.length) return false;
  const saved = new Map((_savedResultData.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
  const savedCodes = new Set(link.members.map(member => saved.get(member.course_identity)?.course_code).filter(Boolean));
  return link.members.some(member => {
    const entry = saved.get(member.course_identity);
    if (!entry || !lockedPlaceOf(member.course_code)) return false;
    return (entry.rooms || []).some(room => (room?.room_shared_with || []).some(partner => savedCodes.has(String(partner)) && String(partner) !== entry.course_code));
  });
}

$('confirmExamLink')?.addEventListener('click', () => {
  if (!_linkDialog || _builderBusy) return;
  const chosen = $('examLinkOptions').querySelector('input:checked');
  if (!chosen) {
    $('examLinkOptions').querySelector('input')?.focus();
    return;
  }
  const option = _linkDialog.options[Number(chosen.value)];
  const identities = _linkDialog.identities;
  closeLinkDialog();
  finishLink(identities, { day: option.day, period: option.period });
});
$('cancelExamLink')?.addEventListener('click', () => closeLinkDialog({ restoreFocus: true }));
$('examLinkDialog')?.addEventListener('cancel', event => { event.preventDefault(); closeLinkDialog({ restoreFocus: true }); });

/* ── Locked days and periods: the board's lock buttons, the rules they keep, the report ── */
// A locked day or period is closed: no exam moves into or out of it, its rooms
// and invigilators stay exactly as saved, and its exams still count for their
// students. The server keeps every one of those rules; the page refuses what
// it can see before sending anything, and says why. Lock state is placed by
// day and period, never by course code: codes renumber when the population does.
const AR_LOCK_CHANGES = {
  one: 'تغيير واحد في الأقفال لم يُحفظ',
  two: 'تغييران في الأقفال لم يُحفظا',
  few: '{n} تغييرات في الأقفال لم تُحفظ',
  many: '{n} تغييراً في الأقفال لم يُحفظ',
  other: '{n} تغيير في الأقفال لم يُحفظ',
};
const AR_LOCKED_CELLS = { one: 'فترة مقفلة واحدة', two: 'فترتان مقفلتان', few: '{n} فترات مقفلة', many: '{n} فترةً مقفلة', other: '{n} فترة مقفلة' };
// The same, as the object of a verb: "يُبقي البناء فترتين مقفلتين".
const AR_LOCKED_CELLS_KEPT = { one: 'فترة مقفلة واحدة', two: 'فترتين مقفلتين', few: '{n} فترات مقفلة', many: '{n} فترةً مقفلة', other: '{n} فترة مقفلة' };
const AR_LOCK_ISSUES = { one: 'مشكلة واحدة', two: 'مشكلتان', few: '{n} مشكلات', many: '{n} مشكلةً', other: '{n} مشكلة' };
const AR_STUDENTS_SIT = { one: 'طالب واحد يؤدي', two: 'طالبان يؤديان', few: '{n} طلاب يؤدون', many: '{n} طالباً يؤدون', other: '{n} طالب يؤدون' };
const AR_STUDENT_COUNT = { one: 'طالب واحد', two: 'طالبان', few: '{n} طلاب', many: '{n} طالباً', other: '{n} طالب' };

// A day and period as text: left to right inside Arabic.
function lockWhereText(day, period) {
  const text = period ? `${day} ${period}` : String(day);
  return IS_AR ? isolateLtr(text) : text;
}
function lockCode(code) { return IS_AR ? isolateLtr(String(code)) : String(code); }
function lockCodes(codes) { return (codes || []).map(lockCode).join(IS_AR ? '، ' : ', '); }
function lockPlural(count, one, many) { return `${count} ${count === 1 ? one : many}`; }

const LOCK_TEXT = {
  locked: IS_AR ? 'مقفل' : 'Locked',
  lockDayName: day => (IS_AR ? `قفل اليوم ${lockWhereText(day)}` : `Lock day ${day}`),
  lockPeriodName: (day, period) => (IS_AR ? `قفل الفترة ${lockWhereText(day, period)}` : `Lock period ${day} ${period}`),
  lockColumnName: period => (IS_AR ? `قفل الفترة ${lockWhereText(period)} في كل الأيام` : `Lock period ${period} on every day`),
  unlockColumnHint: period => (IS_AR ? `${lockWhereText(period)} مقفلة في كل الأيام. انقر لإلغاء قفلها في كل الأيام.` : `${period} is locked on every day. Press to unlock it on every day.`),
  columnLocked: period => (IS_AR ? `${lockWhereText(period)} مقفلة في كل الأيام` : `${period} is locked on every day`),
  unlockDayHint: day => (IS_AR ? `${lockWhereText(day)} مقفل. انقر لإلغاء قفله.` : `${day} is locked. Press to unlock it.`),
  unlockPeriodHint: (day, period) => (IS_AR ? `${lockWhereText(day, period)} مقفلة. انقر لإلغاء قفلها.` : `${day} ${period} is locked. Press to unlock it.`),
  unlockPeriodOfDayHint: (day, period) => (IS_AR
    ? `${lockWhereText(day, period)} مقفلة مع يومها. انقر لإلغاء قفل هذه الفترة وحدها؛ تبقى بقية فترات ${lockWhereText(day)} مقفلة.`
    : `${day} ${period} is locked with its day. Press to unlock this period only; the rest of ${day} stays locked.`),
  dayLocked: day => (IS_AR ? `${lockWhereText(day)} مقفل` : `${day} is locked`),
  lockedIn: (day, period) => (IS_AR ? `${lockWhereText(day, period)} مقفلة — ألغِ قفلها أولاً.` : `${day} ${period} is locked — unlock it first.`),
  lockedOut: (code, day, period) => (IS_AR
    ? `${lockCode(code)} في ${lockWhereText(day, period)} المقفلة — ألغِ قفلها أولاً.`
    : `${code} is in locked ${day} ${period} — unlock it first.`),
  unsaved: (day, period) => (IS_AR
    ? `احفظ تغييراتك أولاً: في ${lockWhereText(day, period)} تغييرات لم تُحفظ بعد. احفظ الجدول ثم اقفلها.`
    : `Save your changes first: ${day} ${period} has changes that are not saved yet. Save the timetable, then lock it.`),
  noRooms: IS_AR
    ? 'حُفظ هذا الجدول دون قاعات، والقفل يُبقي القاعات المحفوظة. ستُوزَّع القاعات عند «فحص التغييرات» ثم «حفظ التغييرات»؛ اقفل بعد ذلك.'
    : 'This timetable was saved without rooms, and a lock keeps the saved rooms. Rooms will be assigned when you Check changes, then Save Changes; lock it after that.',
  roomsPending: IS_AR
    ? 'ستُوزَّع القاعات عند «فحص التغييرات» ثم «حفظ التغييرات»، ثم يمكنك قفل الأيام والفترات.'
    : 'Rooms will be assigned when you Check changes, then Save Changes. Lock days and periods after that.',
  unsavedChanges: count => (IS_AR
    ? `${arabicCount(count, AR_LOCK_CHANGES)} — انقر «فحص التغييرات» ثم «حفظ التغييرات».`
    : `${lockPlural(count, 'lock change', 'lock changes')} not saved — Check changes, then Save Changes.`),
  saveLocksFirst: action => (IS_AR
    ? `تغييرات الأقفال لم تُحفظ بعد. انقر «فحص التغييرات» ثم «حفظ التغييرات»، ثم استخدم «${action}».`
    : `Lock changes are not saved yet. Check changes, then Save Changes, then use ${action}.`),
  reviewBeforeSave: IS_AR
    ? 'افحص تغييرات الأقفال قبل الحفظ: انقر «فحص التغييرات» ثم «حفظ التغييرات».'
    : 'Review the lock changes before saving: Check changes, then Save Changes.',
  reviewLabel: IS_AR ? 'تغييرات الأقفال تحتاج فحصاً' : 'Lock changes need a Check',
  reviewBanner: IS_AR
    ? 'تغيّر ما يُبقيه الحفظ. انقر «فحص التغييرات» لمراجعة الفترات المقفلة، ثم «حفظ التغييرات».'
    : 'Locks change what Save keeps. Click Check changes to review the locked cells, then Save Changes.',
  comparisonPaused: IS_AR ? 'المقارنة متوقفة: تغيّرت الأقفال' : 'Comparison paused: locks changed',
  outside: (day, period) => (IS_AR
    ? `${lockWhereText(day, period)} لم يعد ضمن إعدادات الجدول. ألغِ القفل، أو أعد الإعدادات.`
    : `${period ? `${day} ${period}` : day} is not in the timetable settings any more. Unlock it, or change the settings back.`),
  notSelected: (code, day, period) => (IS_AR
    ? `${lockCode(code)} في ${lockWhereText(day, period)} المقفلة غير محدد لهذا الجدول. أعد تحديده، أو ألغِ القفل.`
    : `${code} in locked ${day} ${period} is not selected for this timetable. Select it again, or unlock it.`),
  wholeDay: IS_AR ? 'اليوم كاملاً' : 'whole day',
  introLoaded: IS_AR
    ? 'لا يدخل أي اختبار إلى يوم أو فترة مقفلة ولا يخرج منها، وتبقى قاعاتها ومراقبوها كما حُفظت. يعمل البناء والتحسين والإصلاح حولها.'
    : 'Nothing moves into or out of a locked day or period, and its rooms and invigilators stay as saved. Build, Optimize and Fix work around it.',
  introBuild: label => (IS_AR
    ? `يُبقي البناء هذه كما حُفظت في «${isolate(label)}» بقاعاتها، ويعيد بناء ما عداها حولها. ألغِ قفل أيٍّ منها ليتمكن البناء من تغييره.`
    : `Build keeps these exactly as saved in “${label}”, rooms included, and rebuilds everything else around them. Unlock one to let Build change it.`),
  unlock: IS_AR ? 'إلغاء القفل' : 'Unlock',
  unlockName: (day, period) => (IS_AR ? `إلغاء قفل ${lockWhereText(day, period)}` : `Unlock ${period ? `${day} ${period}` : day}`),
  exams: count => (IS_AR ? arabicCount(count, AR_EXAMS) : lockPlural(count, 'exam', 'exams')),
  needsReview: IS_AR ? 'راجع الأقفال المعلّمة قبل المتابعة.' : 'Resolve the highlighted locks before continuing.',
  buildKeeps: (cells, exams) => (IS_AR
    ? `يُبقي البناء ${arabicCount(cells, AR_LOCKED_CELLS_KEPT)} (تضم ${arabicCount(exams, AR_EXAMS)}) ويعيد بناء كل ما عداها حولها.`
    : `Build keeps ${lockPlural(cells, 'locked cell', 'locked cells')} (${lockPlural(exams, 'exam', 'exams')}) and rebuilds everything else around them.`),
  buildTitle: IS_AR ? 'البناء حول الفترات المقفلة؟' : 'Build around the locked cells?',
  buildHow: IS_AR
    ? 'لتسمح للبناء بتغيير فترة مقفلة، ألغِ قفلها أولاً في «الأيام والفترات المقفلة».'
    : 'To let Build change a locked cell, unlock it first under Locked days and periods.',
  buildConfirm: IS_AR ? 'بناء' : 'Build',
  cancel: IS_AR ? 'إلغاء' : 'Cancel',
  barCount: (cells, exams) => (IS_AR
    ? `${arabicCount(cells, AR_LOCKED_CELLS)} · ${arabicCount(exams, AR_EXAMS)}`
    : `${lockPlural(cells, 'locked cell', 'locked cells')} · ${lockPlural(exams, 'exam', 'exams')}`),
  barIssues: count => (IS_AR
    ? (count ? `${arabicCount(count, AR_LOCK_ISSUES)} في الفترات المقفلة` : 'لا مشكلات في الفترات المقفلة')
    : (count ? `${lockPlural(count, 'issue', 'issues')} in locked cells` : 'No issues in locked cells')),
  barUnchecked: IS_AR ? 'افحص التغييرات لمعرفة حال الفترات المقفلة.' : 'Check changes to report on the locked cells.',
  lastChecked: IS_AR ? 'آخر تحقق: ' : 'Last checked: ',
  drillTitle: IS_AR ? 'الفترات المقفلة' : 'Locked cells',
  drillNote: IS_AR ? 'مقفل — لم يُنقل؛ ألغِ القفل للإصلاح.' : 'Locked — not moved; unlock to fix.',
  capacityNote: IS_AR
    ? 'المقاعد المحفوظة تتجاوز سعة الاختبار الحالية: وُزِّعت قاعات هذا الجدول قبل أن تعتمد القاعات سعة الاختبار. تبقى كما حُفظت ما دامت مقفلة.'
    : "Saved seats exceed today's exam capacity: this timetable was roomed before rooms used their exam capacity. They stay as saved while locked.",
  drillEmpty: IS_AR ? 'لا مشكلات في الفترات المقفلة.' : 'No issues in the locked cells.',
  drillHead: IS_AR ? ['المشكلة', 'الموعد', 'التفاصيل'] : ['Issue', 'Time', 'Details'],
  rowLock: IS_AR ? 'مقفل — لم يُنقل؛ ألغِ القفل للإصلاح' : 'Locked — not moved; unlock to fix',
  fixKept: count => (IS_AR
    ? `بقيت الاختبارات في الفترات المقفلة في مواضعها (${arabicCount(count, AR_EXAMS)}).`
    : `${lockPlural(count, 'exam', 'exams')} in locked cells ${count === 1 ? 'was' : 'were'} not moved.`),
  fixLockedRemain: count => (IS_AR
    ? `${arabicCount(count, AR_REMAINING)} بين اختبارات مقفلة فقط: مقفلة — لم تُنقل؛ ألغِ القفل لإصلاحها.`
    : `${lockPlural(count, 'rule break is', 'rule breaks are')} among locked exams only: locked — not moved; unlock to fix.`),
  linkLocked: (code, day, period) => (IS_AR
    ? `${lockCode(code)} في ${lockWhereText(day, period)} المقفلة، فلا يُنقل أي مقرر إليها أو منها لربطه. ألغِ القفل أولاً.`
    : `${code} is in locked ${day} ${period}: no course can move into or out of it to link. Unlock it first.`),
  changesGroup: IS_AR ? 'الأيام والفترات المقفلة' : 'Locked days and periods',
  changeCount: count => (IS_AR ? ` · ${count} تغيير قفل` : ` · ${lockPlural(count, 'lock change', 'lock changes')}`),
  lockedState: locked => (locked ? (IS_AR ? 'مقفل' : 'Locked') : (IS_AR ? 'غير مقفل' : 'Not locked')),
  summary: count => (IS_AR ? ` · ${count} مقفل` : ` · ${count} locked`),
};

// Each kind the server reports inside a locked cell: its name and its sentence.
const LOCK_ISSUE_ORDER = ['clash', 'bucket_day', 'registrations_changed', 'unassigned', 'room_unavailable', 'room_over_capacity', 'room_cohort_changed', 'double_booking'];
const LOCK_ISSUE = {
  clash: {
    label: IS_AR ? 'تعارض طلاب' : 'Student clash',
    detail: issue => {
      const count = Number(issue.student_count) || 0;
      return IS_AR
        ? `${arabicCount(count, AR_STUDENTS_SIT)} ${lockCodes(issue.courses)} في الوقت نفسه.`
        : `${lockPlural(count, 'student sits', 'students sit')} ${lockCodes(issue.courses)} at the same time.`;
    },
  },
  bucket_day: {
    label: IS_AR ? 'فصل دراسي واحد في يوم واحد' : 'Same study term, same day',
    detail: issue => (IS_AR
      ? `${lockCode(issue.program || '')}، الفصل الدراسي ${issue.programme_term ?? ''}: ${lockCodes(issue.courses)} في اليوم نفسه.`
      : `${issue.program || ''}, study term ${issue.programme_term ?? ''}: ${lockCodes(issue.courses)} on the same day.`),
  },
  registrations_changed: {
    label: IS_AR ? 'تغيّرت التسجيلات' : 'Registrations changed',
    detail: issue => {
      const who = `${lockCode(issue.course_code || '')} ${lockCode(issue.section || '')} (${studentGroupLabel(issue.gender)})`;
      const saved = Number(issue.saved_count) || 0;
      const live = Number(issue.live_count) || 0;
      const change = issue.change;
      if (IS_AR) {
        if (change === 'new') return `${who}: مجموعة جديدة منذ الحفظ (${arabicCount(live, AR_STUDENT_COUNT)}) دون قاعة محفوظة.`;
        if (change === 'gone') return `${who}: لم تعد موجودة منذ الحفظ، وتبقى مقاعدها المحفوظة (${saved}) محجوزة.`;
        if (change === 'swapped') return `${who}: العدد نفسه (${live}) بطلاب مختلفين.`;
        return `${who}: تغيّر عدد الطلاب من ${saved} إلى ${live}${live > saved ? '؛ لا مقاعد محفوظة للطلاب الجدد' : ''}.`;
      }
      if (change === 'new') return `${who}: a new group since the save (${lockPlural(live, 'student', 'students')}), with no saved room.`;
      if (change === 'gone') return `${who}: gone since the save; its ${saved} saved seats stay booked.`;
      if (change === 'swapped') return `${who}: the same ${live} seats, different students.`;
      return `${who}: ${saved} → ${live} students${live > saved ? '; the new students have no saved seat' : ''}.`;
    },
  },
  unassigned: {
    label: IS_AR ? 'طلاب دون قاعة' : 'Students without a room',
    detail: issue => {
      const count = Number(issue.student_count) || 0;
      return IS_AR
        ? `${lockCode(issue.course_code || '')} ${lockCode(issue.section || '')}: ${arabicCount(count, AR_STUDENT_COUNT)} دون قاعة.`
        : `${issue.course_code || ''} ${issue.section || ''}: ${lockPlural(count, 'student', 'students')} without a room.`;
    },
  },
  room_unavailable: {
    label: IS_AR ? 'قاعة لم تعد متاحة' : 'Room no longer available',
    detail: issue => (IS_AR
      ? `${lockCode(issue.room_code || '')} لم تعد ضمن قاعات الاختبار الحالية (${lockCodes(issue.courses)}).`
      : `${issue.room_code || ''} is not among today's exam rooms (${lockCodes(issue.courses)}).`),
  },
  room_over_capacity: {
    label: IS_AR ? 'تجاوز سعة الاختبار' : 'Over exam capacity',
    detail: issue => (IS_AR
      ? `${lockCode(issue.room_code || '')}: المقاعد المحفوظة ${Number(issue.seated) || 0}، وسعة الاختبار الحالية ${Number(issue.room_capacity) || 0}. المقاعد المحفوظة تتجاوز سعة الاختبار الحالية.`
      : `${issue.room_code || ''}: ${Number(issue.seated) || 0} saved seats, today's exam capacity ${Number(issue.room_capacity) || 0}. The saved seats exceed today's exam capacity.`),
  },
  room_cohort_changed: {
    label: IS_AR ? 'تغيّرت فئة طلاب القاعة' : 'Room student group changed',
    detail: issue => (IS_AR
      ? `حُفظت ${lockCode(issue.room_code || '')} لـ${studentGroupLabel(issue.saved_gender)}، وهي الآن لـ${studentGroupLabel(issue.gender)}.`
      : `${issue.room_code || ''} was saved for ${studentGroupLabel(issue.saved_gender)}; it now serves ${studentGroupLabel(issue.gender)}.`),
  },
  double_booking: {
    label: IS_AR ? 'قاعة محجوزة مرتين' : 'Room double booked',
    detail: issue => (IS_AR
      ? `${lockCode(issue.room_code || '')} محجوزة لـ${lockCodes(issue.courses)} في الوقت نفسه.`
      : `${issue.room_code || ''} holds ${lockCodes(issue.courses)} at the same time.`),
  },
};

function lockIcon(locked) {
  // The design system's icon: a closed or an open padlock.
  const shackle = locked ? 'M8 10V7a4 4 0 0 1 8 0v3' : 'M8 10V7a4 4 0 0 1 7.6-1.8';
  return `<span class="i i-sm" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="${shackle}"/></svg></span>`;
}

function lockCellKey(day, period) { return JSON.stringify([String(day), String(period)]); }
function lockItemKey(item) { return JSON.stringify([String(item.day), item.period ? String(item.period) : '']); }

// A saved or sent list, read leniently: a day, or a day and one period.
function lockItems(raw) {
  return (Array.isArray(raw) ? raw : []).filter(item => item && typeof item.day === 'string' && item.day)
    .map(item => (typeof item.period === 'string' && item.period ? { day: item.day, period: item.period } : { day: item.day }));
}

function lockSignatureOf(items) {
  return JSON.stringify(lockItems(items).map(lockItemKey).sort());
}

function savedLocks() {
  return lockItems(_savedResultData?.exam_locks);
}

function restoreLocksFromRun(data) {
  const startDay = data?.slots?.[0]?.day || $('etStartDay').value;
  _examLocks = lockItems(data?.exam_locks).map(item => ({ ...item, day_identity: examDayIdentity(item.day, startDay) }));
}

// The days and periods of the board on screen, in its own order.
function boardDays() { return uniqueByOrder((_currentResultData?.slots || []).map(slot => slot.day)); }
function boardPeriods() { return uniqueByOrder((_currentResultData?.slots || []).map(slot => slot.period)); }

// In header order: a whole day before its periods, as the server saves them.
function sortLocks(items, days, periods) {
  const at = (list, value) => { const index = list.indexOf(value); return index < 0 ? list.length : index; };
  return [...items].sort((a, b) => (at(days, a.day) - at(days, b.day))
    || ((a.period ? at(periods, a.period) : -1) - (b.period ? at(periods, b.period) : -1)));
}

function lockPayload() {
  const days = _currentResultData ? boardDays() : generateDayLabels();
  const periods = _currentResultData ? boardPeriods() : readExamPeriods().periods;
  return sortLocks(_examLocks, days, periods).map(item => (item.period ? { day: item.day, period: item.period } : { day: item.day }));
}

function lockSets(items = _examLocks) {
  const days = new Set();
  const cells = new Set();
  for (const item of items) {
    if (item.period) cells.add(lockCellKey(item.day, item.period));
    else days.add(item.day);
  }
  return { days, cells };
}

function isCellLocked(day, period, sets = lockSets()) {
  return sets.days.has(day) || sets.cells.has(lockCellKey(day, period));
}

// Where a locked exam sits: on the board, or - before a Build - where the saved
// run it is kept from has it. Null for an exam in no locked cell.
function lockedPlaceOf(code, sets = lockSets()) {
  if (!_examLocks.length) return null;
  if (_currentResultData) {
    const entry = scheduleEntryFor(code);
    return entry && entry.day !== 'OVERFLOW' && isCellLocked(entry.day, entry.period, sets) ? entry : null;
  }
  const identity = identityOfCode(code);
  const entry = (_lockSource?.run?.schedule || []).find(item => (item.course_identity || item.course_code) === identity);
  return entry && entry.day !== 'OVERFLOW' && isCellLocked(entry.day, entry.period, sets) ? entry : null;
}

// Locks added or taken away since the save, each once: `saved` says it was the
// saved run's and is undone now.
function lockChangesSinceSave() {
  if (!_savedResultData) return [];
  const saved = new Map(savedLocks().map(item => [lockItemKey(item), item]));
  const current = new Map(_examLocks.map(item => [lockItemKey(item), item]));
  const changes = [
    ...[...current].filter(([key]) => !saved.has(key)).map(([, item]) => ({ day: item.day, period: item.period, saved: false })),
    ...[...saved].filter(([key]) => !current.has(key)).map(([, item]) => ({ day: item.day, period: item.period, saved: true })),
  ];
  return sortLocks(changes, boardDays(), boardPeriods());
}

// The exams in one cell of a schedule, by identity: what a lock would keep.
function cellIdentities(schedule, day, period) {
  return (schedule || []).filter(entry => entry.day === day && entry.period === period)
    .map(entry => entry.course_identity || entry.course_code).sort();
}

// Why a lock cannot be added here now, or ''. A lock keeps what the SAVED run
// holds, so a cell must match it; the server says the same (exam_locks_cell_unsaved).
function newLockProblem(day, period) {
  if (!_currentResultData || !_savedResultData) return LOCK_REFUSALS.exam_locks_source_required[IS_AR ? 1 : 0];
  for (const each of period ? [period] : boardPeriods()) {
    const now = cellIdentities(_currentResultData.schedule, day, each);
    const before = cellIdentities(_savedResultData.schedule, day, each);
    if (JSON.stringify(now) !== JSON.stringify(before)) return LOCK_TEXT.unsaved(day, each);
  }
  return '';
}

// Why moving `code` (with its link) to (day, period) would break a lock, or ''.
function lockMoveProblem(code, day, period) {
  if (!_examLocks.length) return '';
  const sets = lockSets();
  for (const member of linkedCodes(code)) {
    const place = lockedPlaceOf(member, sets);
    if (place) return LOCK_TEXT.lockedOut(member, place.day, place.period);
  }
  return isCellLocked(day, period, sets) ? LOCK_TEXT.lockedIn(day, period) : '';
}

// Why pinning `code` to (day, period) would pin into or out of a locked cell, or ''.
function lockPinProblem(code, day, period) {
  if (!_examLocks.length) return '';
  const sets = lockSets();
  let inside = true;
  for (const member of linkedCodes(code)) {
    const place = lockedPlaceOf(member, sets);
    if (place && (place.day !== day || place.period !== period)) return LOCK_TEXT.lockedOut(member, place.day, place.period);
    if (!place) inside = false;
  }
  return !inside && isCellLocked(day, period, sets) ? LOCK_TEXT.lockedIn(day, period) : '';
}

// A lock whose day or period the settings no longer have, by label and by day
// identity (a changed start day relabels the days), or ''.
function lockHeaderProblem(header) {
  for (const item of _examLocks) {
    const dayKept = header.days.some(day => day === item.day && examDayIdentity(day) === item.day_identity);
    if (!dayKept || (item.period && !header.periods.includes(item.period))) return LOCK_TEXT.outside(item.day, item.period);
  }
  return '';
}

// Said at the board, and in the setup's status line: a refusal made before any
// request. The board's alert keeps to the foot of the viewport while the board
// is in view, so a refusal far down the board is seen where it happened.
let _refusalReturnFocus = null;
function refuseLockedEdit(message) {
  const alert = $('examLockRefusal');
  if (alert && !$('etResults').classList.contains('d-none')) {
    $('examLockRefusalText').textContent = message;
    alert.hidden = false;
    // The control that was refused, for the keyboard to return to on Close.
    const active = document.activeElement;
    if (active !== $('examLockRefusalClose')) _refusalReturnFocus = active && $('etResults').contains(active) ? active : null;
  }
  $('etStatus').textContent = message;
  $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
  return null;
}

function clearLockRefusal() {
  const alert = $('examLockRefusal');
  if (alert && !alert.hidden) {
    alert.hidden = true;
    $('examLockRefusalText').textContent = '';
  }
}
$('examLockRefusalClose')?.addEventListener('click', () => {
  // Closed from the keyboard: focus goes back to the refused control, or the
  // board - never to the top of the page.
  const refocus = document.activeElement === $('examLockRefusalClose');
  const back = _refusalReturnFocus?.isConnected ? _refusalReturnFocus : $('schedGrid');
  _refusalReturnFocus = null;
  clearLockRefusal();
  if (refocus) back?.focus({ preventScroll: true });
});

// Lock or unlock a whole day (no period) or one period. Locking one period of
// a locked day is not offered: its button unlocks just that period, and the
// day's other periods stay locked one by one.
function toggleLock(day, period) {
  if (_builderBusy || !CAN_EDIT_EXAM_TIMETABLE || !_currentResultData) return false;
  const sets = lockSets();
  const locking = period ? !isCellLocked(day, period, sets) : !sets.days.has(day);
  if (locking) {
    if (_savedResultData && _savedResultData.assign_rooms !== true) {
      // A lock keeps saved rooms; this run has none yet. Its next Check and
      // Save assign them - one Undo step - and the lock comes after that.
      runManualCommand(() => { _roomsForLocks = true; });
      refuseLockedEdit(LOCK_TEXT.noRooms);
      return false;
    }
    const problem = newLockProblem(day, period);
    if (problem) {
      refuseLockedEdit(problem);
      return false;
    }
  }
  const startDay = _currentResultData.slots?.[0]?.day;
  const identity = examDayIdentity(day, startDay);
  return runManualCommand(() => {
    if (!period) {
      _examLocks = _examLocks.filter(item => item.day !== day);
      if (locking) _examLocks.push({ day, day_identity: identity });
    } else if (locking) {
      _examLocks.push({ day, period, day_identity: identity });
    } else if (sets.days.has(day)) {
      _examLocks = _examLocks.filter(item => item.day !== day);
      for (const other of boardPeriods()) if (other !== period) _examLocks.push({ day, period: other, day_identity: identity });
    } else {
      _examLocks = _examLocks.filter(item => item.day !== day || item.period !== period);
    }
    _examLocks = sortLocks(_examLocks, boardDays(), boardPeriods());
  });
}

// A period is locked on every day when each of the board's days holds it
// locked, by its day's lock or its own.
function isColumnLocked(period, sets = lockSets()) {
  const days = boardDays();
  return days.length > 0 && days.every(day => isCellLocked(day, period, sets));
}

// Lock or unlock one period on every day, as one Undo step. Locking adds the
// period lock of each day not yet locked there, and needs each of those cells
// to match the saved run: one unsaved cell refuses the whole column, so a
// column is never half locked. Unlocking takes the period out of every day,
// keeping the other periods of a locked day locked (as toggleLock does).
function toggleColumnLock(period) {
  if (_builderBusy || !CAN_EDIT_EXAM_TIMETABLE || !_currentResultData) return false;
  const sets = lockSets();
  const days = boardDays();
  const locking = !isColumnLocked(period, sets);
  if (locking) {
    if (_savedResultData && _savedResultData.assign_rooms !== true) {
      runManualCommand(() => { _roomsForLocks = true; });
      refuseLockedEdit(LOCK_TEXT.noRooms);
      return false;
    }
    for (const day of days) {
      if (isCellLocked(day, period, sets)) continue;
      const problem = newLockProblem(day, period);
      if (problem) {
        refuseLockedEdit(problem);
        return false;
      }
    }
  }
  const startDay = _currentResultData.slots?.[0]?.day;
  return runManualCommand(() => {
    for (const day of days) {
      const identity = examDayIdentity(day, startDay);
      if (locking) {
        if (!isCellLocked(day, period, sets)) _examLocks.push({ day, period, day_identity: identity });
      } else if (sets.days.has(day)) {
        _examLocks = _examLocks.filter(item => item.day !== day);
        for (const other of boardPeriods()) if (other !== period) _examLocks.push({ day, period: other, day_identity: identity });
      } else {
        _examLocks = _examLocks.filter(item => item.day !== day || item.period !== period);
      }
    }
    _examLocks = sortLocks(_examLocks, boardDays(), boardPeriods());
  });
}

// What the board shows as locked: cells and the exams in them.
function boardLockFacts() {
  const sets = lockSets();
  let cells = 0;
  for (const day of boardDays()) for (const period of boardPeriods()) if (isCellLocked(day, period, sets)) cells += 1;
  const selected = _coursesLoaded ? new Set(getCheckedValues('courseList')) : null;
  const exams = (_currentResultData?.schedule || []).filter(entry => entry.day !== 'OVERFLOW'
    && (!selected || selected.has(entry.course_code)) && isCellLocked(entry.day, entry.period, sets)).length;
  return { cells, exams };
}

// Markup for a day's lock control in its row header, and a period's in its cell.
function dayLockMarkup(day, locked) {
  if (!CAN_EDIT_EXAM_TIMETABLE) {
    return locked ? `<span class="et-lock-mark" role="img" aria-label="${escapeAttr(LOCK_TEXT.dayLocked(day))}" title="${escapeAttr(LOCK_TEXT.dayLocked(day))}">${lockIcon(true)}</span>` : '';
  }
  const name = LOCK_TEXT.lockDayName(day);
  return `<button type="button" class="et-lock-toggle et-lock-day" data-lock-day="${escapeAttr(day)}" aria-pressed="${locked}" aria-label="${escapeAttr(name)}" title="${escapeAttr(locked ? LOCK_TEXT.unlockDayHint(day) : name)}">${lockIcon(locked)}</button>`;
}

function cellLockMarkup(day, period, locked, dayLocked) {
  if (!CAN_EDIT_EXAM_TIMETABLE && !locked) return '';
  const badge = locked ? `<span class="et-lock-badge">${lockIcon(true)}<span>${escapeAttr(LOCK_TEXT.locked)}</span></span>` : '';
  let button = '';
  if (CAN_EDIT_EXAM_TIMETABLE) {
    const name = LOCK_TEXT.lockPeriodName(day, period);
    const hint = !locked ? name : dayLocked ? LOCK_TEXT.unlockPeriodOfDayHint(day, period) : LOCK_TEXT.unlockPeriodHint(day, period);
    button = `<button type="button" class="et-lock-toggle et-lock-period" data-lock-day="${escapeAttr(day)}" data-lock-period="${escapeAttr(period)}" aria-pressed="${locked}" aria-label="${escapeAttr(name)}" title="${escapeAttr(hint)}">${lockIcon(locked)}</button>`;
  }
  return `<div class="et-cell-lockbar">${badge}${button}</div>`;
}

// A period's control in its column header: locks it on every day.
function columnLockMarkup(period, locked) {
  if (!CAN_EDIT_EXAM_TIMETABLE) {
    return locked ? `<span class="et-lock-mark" role="img" aria-label="${escapeAttr(LOCK_TEXT.columnLocked(period))}" title="${escapeAttr(LOCK_TEXT.columnLocked(period))}">${lockIcon(true)}</span>` : '';
  }
  const name = LOCK_TEXT.lockColumnName(period);
  return `<button type="button" class="et-lock-toggle et-lock-column" data-lock-column="${escapeAttr(period)}" aria-pressed="${locked}" aria-label="${escapeAttr(name)}" title="${escapeAttr(locked ? LOCK_TEXT.unlockColumnHint(period) : name)}">${lockIcon(locked)}</button>`;
}

// "N lock changes not saved", or that rooms come first.
function renderLockLine() {
  const line = $('examLockLine');
  if (!line) return;
  const changes = _currentResultData ? lockChangesSinceSave().length : 0;
  const text = changes ? LOCK_TEXT.unsavedChanges(changes) : (_roomsForLocks && _currentResultData ? LOCK_TEXT.roomsPending : '');
  if (line.textContent !== text) line.textContent = text;
  line.hidden = !text;
}

// The board's lock summary, with the last check's issue count and the way to them.
function renderLockBar() {
  const bar = $('lockBar');
  if (!bar) return;
  const shown = Boolean(_currentResultData && _examLocks.length);
  bar.classList.toggle('d-none', !shown);
  if (!shown) return;
  const { cells, exams } = boardLockFacts();
  const count = LOCK_TEXT.barCount(cells, exams);
  if ($('lockCount').textContent !== count) $('lockCount').textContent = count;
  const report = _currentResultData.qa?.exam_locks;
  const unchecked = !report || lockChangesSinceSave().length > 0 && !hasCurrentEvaluation();
  const issues = unchecked ? LOCK_TEXT.barUnchecked
    : `${hasCurrentEvaluation() ? '' : LOCK_TEXT.lastChecked}${LOCK_TEXT.barIssues(Number(report.issue_count) || (report.issues || []).length)}`;
  if ($('lockBarIssues').textContent !== issues) $('lockBarIssues').textContent = issues;
  const review = $('lockBarReview');
  review.hidden = !report;
  review.disabled = _builderBusy;
}

// The builder's list of locks: on a loaded board, and - after Load Courses -
// what a Build keeps, each with the reason it cannot be kept, if any.
function lockListProblem(item) {
  if (_currentResultData) return '';
  const header = { days: generateDayLabels(), periods: readExamPeriods().periods };
  const outside = () => LOCK_TEXT.outside(item.day, item.period);
  if (!header.days.some(day => day === item.day && examDayIdentity(day) === item.day_identity)) return outside();
  if (item.period && !header.periods.includes(item.period)) return outside();
  const selected = _coursesLoaded ? new Set(getCheckedCourseEntries().map(entry => entry.course_identity || entry.course_code)) : null;
  for (const entry of lockSourceEntries(item)) {
    if (!header.periods.includes(entry.period)) return LOCK_TEXT.outside(entry.day, entry.period);
    if (selected && !selected.has(entry.course_identity || entry.course_code)) return LOCK_TEXT.notSelected(entry.course_code, entry.day, entry.period);
  }
  return '';
}

function lockSourceEntries(item) {
  return (_lockSource?.run?.schedule || []).filter(entry => entry.day === item.day && entry.day !== 'OVERFLOW'
    && (!item.period || entry.period === item.period));
}

function lockItemExamCount(item) {
  if (_currentResultData) {
    return (_currentResultData.schedule || []).filter(entry => entry.day === item.day && entry.day !== 'OVERFLOW' && (!item.period || entry.period === item.period)).length;
  }
  return lockSourceEntries(item).length;
}

let _lockRowsMarkup = '';
function renderLockEditor() {
  const editor = $('examLockEditor');
  if (!editor) return;
  editor.classList.toggle('d-none', !_examLocks.length);
  if (!_examLocks.length && !_lockRowsMarkup) return;
  $('examLockCount').textContent = _examLocks.length ? `(${_examLocks.length})` : '';
  $('examLockTable').classList.toggle('d-none', !_examLocks.length);
  const intro = _currentResultData ? LOCK_TEXT.introLoaded : _lockSource ? LOCK_TEXT.introBuild(_lockSource.run?.label || '') : '';
  if ($('examLockIntro').textContent !== intro) $('examLockIntro').textContent = intro;
  let invalid = false;
  const rows = _examLocks.map(item => {
    const problem = lockListProblem(item);
    invalid ||= Boolean(problem);
    const where = item.period
      ? `<bdi dir="ltr">${escapeAttr(`${item.day} · ${item.period}`)}</bdi>`
      : `<bdi dir="ltr">${escapeAttr(item.day)}</bdi> <small>${escapeAttr(LOCK_TEXT.wholeDay)}</small>`;
    const unlock = CAN_EDIT_EXAM_TIMETABLE
      ? `<button type="button" class="btn btn-sm btn-outline-secondary" data-lock-remove="${escapeAttr(lockItemKey(item))}" aria-label="${escapeAttr(LOCK_TEXT.unlockName(item.day, item.period))}">${escapeAttr(LOCK_TEXT.unlock)}</button>`
      : '';
    return `<tr class="${problem ? 'et-lock-invalid' : ''}" data-lock-item="${escapeAttr(lockItemKey(item))}"><td><span class="et-lock-where">${lockIcon(true)}${where}</span>${problem ? `<small class="et-lock-problem">${escapeAttr(problem)}</small>` : ''}</td><td>${escapeAttr(LOCK_TEXT.exams(lockItemExamCount(item)))}</td><td>${unlock}</td></tr>`;
  }).join('');
  if (rows !== _lockRowsMarkup) {
    // Rebuilt only when it changed, so a focused Unlock is not dropped.
    const focused = document.activeElement?.closest?.('[data-lock-remove]')?.dataset.lockRemove;
    $('examLockRows').innerHTML = rows;
    _lockRowsMarkup = rows;
    if (focused) [...$('examLockRows').querySelectorAll('[data-lock-remove]')].find(button => button.dataset.lockRemove === focused)?.focus({ preventScroll: true });
  }
  let text = '';
  if (invalid) text = LOCK_TEXT.needsReview;
  else if (!_currentResultData && _lockSource && _examLocks.length) {
    const facts = buildLockFacts({ days: generateDayLabels(), periods: readExamPeriods().periods });
    text = LOCK_TEXT.buildKeeps(facts.cells, facts.exams);
  }
  if ($('examLockNotice').textContent !== text) $('examLockNotice').textContent = text;
  $('examLockNotice').className = `small mt-2 mb-0 ${invalid ? 'text-danger' : 'text-secondary'}`;
}

// A Build's locks: the cells it keeps in this header and the saved exams in them.
function buildLockFacts(header) {
  const sets = lockSets();
  let cells = 0;
  for (const day of header.days) for (const period of header.periods) if (isCellLocked(day, period, sets)) cells += 1;
  const exams = (_lockSource?.run?.schedule || []).filter(entry => entry.day !== 'OVERFLOW' && isCellLocked(entry.day, entry.period, sets)).length;
  return { cells, exams };
}

// What a Build sends for its locks, or null (refused, with the reason said).
// None kept: exactly the Build without locks.
function validatedBuildLocks(header, scope) {
  if (!_examLocks.length || !_lockSource) return { keep: false };
  const saved = _lockSource.run?.enrollment_scope || {};
  const same = (a, b) => JSON.stringify([...(a || [])].map(String).sort()) === JSON.stringify([...(b || [])].map(String).sort());
  if (!same(scope.programs, saved.programs) || !same(scope.sections, saved.sections)) {
    renderLockEditor();
    return refuseLockedEdit(LOCK_REFUSALS.exam_locks_scope_changed[IS_AR ? 1 : 0]);
  }
  for (const item of _examLocks) {
    const problem = lockListProblem(item);
    if (problem) {
      renderLockEditor();
      if ($('examSetupDetails')) $('examSetupDetails').open = true;
      return refuseLockedEdit(problem);
    }
  }
  const facts = buildLockFacts(header);
  return { keep: true, runId: _lockSource.runId, locks: lockPayload(), ...facts };
}

// The review list's rows: the report kept inside locked cells, by kind.
function lockIssueRows(issues) {
  const rank = kind => { const index = LOCK_ISSUE_ORDER.indexOf(kind); return index < 0 ? LOCK_ISSUE_ORDER.length : index; };
  return [...(issues || [])].filter(issue => issue && typeof issue === 'object')
    .map((issue, index) => ({ issue, index }))
    .sort((a, b) => (rank(a.issue.kind) - rank(b.issue.kind)) || (a.index - b.index))
    .map(({ issue }) => issue);
}

// A small lock on a report row the server says sits inside locked cells.
function rowLockMark(row, codes = []) {
  const all = Array.isArray(codes) ? codes.map(code => (typeof code === 'string' ? code : code?.course_code || code?.code)).filter(Boolean) : [];
  const locked = row?.locked === true || (_drillLockedCodes.size > 0 && all.length > 0 && all.every(code => _drillLockedCodes.has(code)));
  return locked ? `<span class="et-row-lock" role="img" aria-label="${escapeAttr(LOCK_TEXT.rowLock)}" title="${escapeAttr(LOCK_TEXT.rowLock)}">${lockIcon(true)}</span>` : '';
}

$('examLockRows')?.addEventListener('click', event => {
  const remove = event.target.closest('[data-lock-remove]');
  if (!remove || _builderBusy || !CAN_EDIT_EXAM_TIMETABLE) return;
  const item = _examLocks.find(lock => lockItemKey(lock) === remove.dataset.lockRemove);
  if (!item) return;
  const index = _examLocks.indexOf(item);
  if (_currentResultData) toggleLock(item.day, item.period);
  else runManualCommand(() => { _examLocks = _examLocks.filter(lock => lock !== item); });
  const buttons = [...$('examLockRows').querySelectorAll('[data-lock-remove]')];
  (buttons[Math.min(index, buttons.length - 1)] || $('examLockHeading')).focus({ preventScroll: true });
});

/* ── Conflict Matrix ── */
// Renders an N×N heatmap of shared students between every course pair.
// Color scale: 0 (transparent) → 1-2 (yellow) → 3-5 (orange) → 6-10 (red) → 11+ (dark red).
// Interactive features:
//   • Crosshair hover: row highlight via CSS, column via JS .cm-col-hl class
//   • Click header → fill schedule filter with that course
//   • Click conflict cell → fill schedule filter with BOTH row & column courses
function renderConflictMatrix(conflicts, courses) {
  const container = $('matrixGrid');
  const emptyEl = $('etMatrixEmpty');
  const viewportEl = $('etMatrixViewport');

  // Reset toggle state
  $('conflictMatrix').classList.add('d-none');
  $('toggleMatrix').textContent = T.show;
  $('toggleMatrix').setAttribute('aria-expanded', 'false');

  if (!conflicts.length || courses.length < 2) {
    container.innerHTML = '';
    if (emptyEl) emptyEl.classList.add('visible');
    if (viewportEl) viewportEl.style.display = 'none';
    return;
  }

  // Has data: hide empty, show viewport
  if (emptyEl) emptyEl.classList.remove('visible');
  if (viewportEl) viewportEl.style.display = '';

  // Build adjacency map from edge list
  const adj = new Map(courses.map(code => [code, new Map()]));
  for (const e of conflicts) {
    if (!adj.has(e.course_a) || !adj.has(e.course_b)) continue;
    adj.get(e.course_a).set(e.course_b, e.shared);
    adj.get(e.course_b).set(e.course_a, e.shared);
  }

  // Color class by shared-student count
  function cmClass(n) {
    if (n === 0) return 'cm-0';
    if (n <= 2) return 'cm-1';
    if (n <= 5) return 'cm-2';
    if (n <= 10) return 'cm-3';
    return 'cm-4';
  }

  // Build HTML table
  let html = '<table class="et-matrix">';
  const matrixKey = (...parts) => escapeAttr(JSON.stringify(parts));

  // Header row: empty corner + rotated course labels
  html += '<thead><tr><th></th>';
  for (const c of courses) {
    html += `<th scope="col" role="button" tabindex="0" data-matrix-key="${matrixKey('column', c)}" aria-label="${escapeAttr(`${IS_AR ? 'تمييز الاختبار' : 'Highlight exam'}: ${c}`)}"><span>${escapeAttr(c)}</span></th>`;
  }
  html += '</tr></thead>';

  // Body: one row per course
  html += '<tbody>';
  for (let i = 0; i < courses.length; i++) {
    const rowCourse = courses[i];
    html += `<tr><th scope="row" role="button" tabindex="0" data-matrix-key="${matrixKey('row', rowCourse)}" aria-label="${escapeAttr(`${IS_AR ? 'تمييز الاختبار' : 'Highlight exam'}: ${rowCourse}`)}">${escapeAttr(rowCourse)}</th>`;
    for (let j = 0; j < courses.length; j++) {
      const colCourse = courses[j];
      if (i === j) {
        html += '<td class="cm-diag">\u00b7</td>';
      } else {
        const n = adj.get(rowCourse)?.get(colCourse) ?? 0;
        const cls = cmClass(n);
        const label = n > 0 ? n : '';
        const tooltip = n > 0
          ? `${rowCourse} \u2194 ${colCourse}: ${n} ${T.students}`
          : `${rowCourse} \u2194 ${colCourse}: 0`;
        html += `<td class="${cls}" title="${escapeAttr(tooltip)}"${n > 0 ? ` role="button" tabindex="0" data-matrix-key="${matrixKey('pair', rowCourse, colCourse)}" aria-label="${escapeAttr(tooltip)}"` : ''}>${label}</td>`;
      }
    }
    html += '</tr>';
  }
  html += '</tbody></table>';

  // Legend
  html += '<div class="et-legend">';
  html += `<span><span class="et-legend-box" style="background:transparent"></span> 0</span>`;
  html += `<span><span class="et-legend-box" style="background:hsl(45 90% 88%)"></span> 1-2</span>`;
  html += `<span><span class="et-legend-box" style="background:hsl(33 90% 78%)"></span> 3-5</span>`;
  html += `<span><span class="et-legend-box" style="background:hsl(15 85% 68%)"></span> 6-10</span>`;
  html += `<span><span class="et-legend-box" style="background:hsl(0 80% 55%)"></span> 11+</span>`;
  html += '</div>';

  container.innerHTML = html;

  // ── Crosshair hover (column highlight via JS, row via CSS tr:hover) ──
  const tbl = container.querySelector('.et-matrix');
  if (tbl) {
    tbl.addEventListener('mouseover', (e) => {
      const cell = e.target.closest('td, th');
      if (!cell) return;
      const ci = [...cell.parentElement.children].indexOf(cell);
      tbl.querySelectorAll('.cm-col-hl').forEach(el => el.classList.remove('cm-col-hl'));
      if (ci > 0) {
        tbl.querySelectorAll('tr').forEach(r => {
          if (r.children[ci]) r.children[ci].classList.add('cm-col-hl');
        });
      }
    });
    tbl.addEventListener('mouseleave', () => {
      tbl.querySelectorAll('.cm-col-hl').forEach(el => el.classList.remove('cm-col-hl'));
    });

    // ── Click-to-filter: click a course header or conflict cell → filter schedule grid ──
    tbl.addEventListener('click', (e) => {
      const target = e.target.closest('th, td');
      if (!target) return;
      let courseCode = '';
      if (target.tagName === 'TH') {
        const span = target.querySelector('span');
        courseCode = span ? span.textContent.trim() : target.textContent.trim();
      } else if (target.tagName === 'TD' && !target.classList.contains('cm-diag') && !target.classList.contains('cm-0')) {
        // Conflict cell → both row and column courses
        const th = target.parentElement.querySelector('th');
        const rowCourse = th ? th.textContent.trim() : '';
        const ci = [...target.parentElement.children].indexOf(target);
        const headerCells = tbl.querySelector('thead tr').children;
        const colSpan = headerCells[ci] ? headerCells[ci].querySelector('span') : null;
        const colCourse = colSpan ? colSpan.textContent.trim() : '';
        courseCode = rowCourse && colCourse ? `${rowCourse} ${colCourse}` : rowCourse || colCourse;
      }
      if (courseCode) {
        if ($('matrixPanel').classList.contains('matrix-fullscreen')) $('matrixFullscreen').click();
        const filter = $('schedFilter');
        filter.value = courseCode;
        filter.dispatchEvent(new Event('input'));
        const matched = $('schedGrid').querySelector('.et-highlight');
        matched?.focus({ preventScroll: true });
        if (matched) revealExamChip(matched);
      }
    });
    tbl.addEventListener('keydown', event => {
      if (!['Enter', ' '].includes(event.key) || !event.target.matches('[role="button"]')) return;
      event.preventDefault();
      if (!event.repeat) event.target.click();
    });
  }
}

/* ── Toggle conflict matrix ── */
$('toggleMatrix').addEventListener('click', () => {
  const el = $('conflictMatrix');
  const show = el.classList.contains('d-none');
  el.classList.toggle('d-none', !show);
  $('toggleMatrix').textContent = show ? T.hide : T.show;
  $('toggleMatrix').setAttribute('aria-expanded', String(show));
});

/* ── History ── */
let _historyPage = 1;
let _historyRequest = 0;

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function loadHistory(page) {
  const requestedPage = page === undefined ? _historyPage : Math.max(1, Number(page) || 1);
  const request = ++_historyRequest;
  $('historyList').setAttribute('aria-busy', 'true');
  try {
    const res = await fetch(`/ops/exam-timetable/list/?page=${requestedPage}`, {
      headers: { 'X-CSRFToken': getCsrfToken() || CSRF },
    });
    const data = await readExamResponse(res);
    if (request !== _historyRequest) return;
    if (!res.ok || !data.ok) throw examResponseError(data);
    clearExamRequestError('history');

    const runs = data.runs ?? [];
    const totalPages = data.total_pages ?? 1;
    const total = data.total ?? 0;
    _historyPage = data.page ?? 1;
    _savedRunsTotal = data.total ?? runs.length;
    updateRosterTab();
    const kept = captureHistoryFocus();

    if (!runs.length) {
      $('historyList').innerHTML = `<small class="text-secondary">${T.noHistory}</small>`;
      $('historyPagination').classList.add('d-none');
      restoreHistoryFocus(kept);
      return;
    }

    $('historyList').innerHTML = runs.map(r => {
      const dt = fmtDate(r.created_at);
      return `<div class="et-history-item${String(r.id) === String(_currentRunId) ? ' active' : ''}" data-id="${escapeAttr(r.id)}">
        <button type="button" class="et-run-info" aria-label="${escapeAttr(`${IS_AR ? 'تحميل الجدول' : 'Load run'}: ${r.label}`)}">
          <strong>${escapeAttr(r.label)}</strong> <small class="text-secondary">${escapeAttr(dt)}</small>
        </button>
        <button type="button" class="et-copy-btn" data-id="${escapeAttr(r.id)}" data-label="${escapeAttr(r.label)}" title="${IS_AR ? 'إنشاء نسخة من الجدول المحفوظ' : 'Copy saved timetable'}" aria-label="${escapeAttr(`${IS_AR ? 'نسخ الجدول' : 'Copy run'}: ${r.label}`)}" aria-haspopup="dialog">
          <svg aria-hidden="true" focusable="false" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></svg>
        </button>
        ${CAN_DELETE_EXAM_TIMETABLE ? `<button type="button" class="et-del-btn" data-id="${escapeAttr(r.id)}" data-label="${escapeAttr(r.label)}" title="${IS_AR ? 'حذف' : 'Delete'}" aria-label="${escapeAttr(`${IS_AR ? 'حذف الجدول' : 'Delete run'}: ${r.label}`)}">
          <svg aria-hidden="true" focusable="false" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        </button>` : ''}
      </div>`;
    }).join('');

    // Click on run info → load run
    $('historyList').querySelectorAll('.et-run-info').forEach(el => {
      el.addEventListener('click', () => loadRun(el.closest('.et-history-item').dataset.id));
    });

    $('historyList').querySelectorAll('.et-copy-btn').forEach(btn => {
      btn.addEventListener('click', event => { event.stopPropagation(); copyRun(btn.dataset.id, btn.dataset.label, btn); });
    });

    // Click on delete button → confirm & delete
    $('historyList').querySelectorAll('.et-del-btn').forEach(btn => {
      btn.addEventListener('click', (e) => { e.stopPropagation(); deleteRun(btn.dataset.id, btn.dataset.label); });
    });

    // Pagination
    if (totalPages > 1) {
      const from = (_historyPage - 1) * 10 + 1;
      const to = Math.min(_historyPage * 10, total);
      $('historyShowing').textContent = T.showingRuns.replace('{from}', from).replace('{to}', to).replace('{total}', total);
      renderHistoryPagination(totalPages);
      $('historyPagination').classList.remove('d-none');
    } else {
      $('historyPagination').classList.add('d-none');
    }
    restoreHistoryFocus(kept);
  } catch (err) {
    if (request === _historyRequest) showExamRequestError(err, 'history');
  } finally {
    if (request === _historyRequest) $('historyList').removeAttribute('aria-busy');
  }
}

// Reloaded while someone reads it - a job ending in the background, or their
// own page change - the list and its pager keep the keyboard's place: the same
// control, or its nearest stand-in, and never the page body.
function captureHistoryFocus() {
  const active = document.activeElement;
  if (!active || active === document.body) return null;
  if ($('historyList').contains(active)) {
    const rows = Array.from($('historyList').querySelectorAll('.et-history-item'));
    const row = active.closest('.et-history-item');
    return {
      region: 'list',
      id: row?.dataset.id,
      index: Math.max(0, rows.indexOf(row)),
      kind: ['et-run-info', 'et-copy-btn', 'et-del-btn'].find(name => active.classList.contains(name)),
    };
  }
  if ($('historyPages').contains(active)) {
    return { region: 'pages', nav: active.dataset.historyNav || '', page: active.dataset.historyPage };
  }
  return null;
}

function restoreHistoryFocus(kept) {
  // Only focus that the re-render dropped - or left on a control now hidden -
  // is put back; never taken from elsewhere.
  const active = document.activeElement;
  if (!kept || (active && active !== document.body && !active.closest('.d-none, [hidden]'))) return;
  let target = null;
  if (kept.region === 'list') {
    const rows = Array.from($('historyList').querySelectorAll('.et-history-item'));
    const row = rows.find(item => item.dataset.id === String(kept.id)) || rows[Math.min(kept.index, rows.length - 1)];
    target = row?.querySelector(`.${kept.kind || 'et-run-info'}`) || row?.querySelector('.et-run-info');
  } else {
    const buttons = Array.from($('historyPages').querySelectorAll('[data-history-page]'));
    target = buttons.find(button => (kept.nav
      ? button.dataset.historyNav === kept.nav
      : !button.dataset.historyNav && button.dataset.historyPage === kept.page));
    if (!target || target.disabled) target = buttons.find(button => button.getAttribute('aria-current') === 'page');
  }
  if (!target || target.disabled || target.closest('.d-none, [hidden]')) target = $('examHistorySummary');
  target?.focus({ preventScroll: true });
}

function renderHistoryPagination(pages) {
  const wrap = $('historyPages');
  let html = `<button type="button" class="pg-btn" data-history-page="${_historyPage-1}" data-history-nav="prev" aria-label="${IS_AR ? 'الصفحة السابقة' : 'Previous page'}" ${_historyPage<=1?'disabled':''}>‹</button>`;
  for (let i = 1; i <= pages; i++) {
    if (pages > 7 && i > 2 && i < pages - 1 && Math.abs(i - _historyPage) > 1) {
      if (i === 3 || i === pages - 2) html += '<span class="et-pagination-ellipsis">…</span>';
      continue;
    }
    html += `<button type="button" class="pg-btn ${i===_historyPage?'active':''}" data-history-page="${i}" aria-label="${IS_AR ? 'صفحة' : 'Page'} ${i}"${i === _historyPage ? ' aria-current="page"' : ''}>${i}</button>`;
  }
  html += `<button type="button" class="pg-btn" data-history-page="${_historyPage+1}" data-history-nav="next" aria-label="${IS_AR ? 'الصفحة التالية' : 'Next page'}" ${_historyPage>=pages?'disabled':''}>›</button>`;
  wrap.innerHTML = html;
}

$('historyPages').addEventListener('click', event => {
  const button = event.target.closest('[data-history-page]');
  if (button && !button.disabled && !_builderBusy) loadHistory(Number(button.dataset.historyPage));
});

async function deleteRun(runId, label) {
  if (!CAN_DELETE_EXAM_TIMETABLE || _builderBusy) return;
  const ok = await dlg.confirm({
    title: T.deleteRun,
    body: T.deleteRunBody + `<p style="margin-top:6px;"><strong>${escapeAttr(label)}</strong></p>`,
    icon: 'danger',
    confirmLabel: T.deleteConfirm,
    confirmClass: 'danger',
  });
  if (!ok || _builderBusy) return;
  setBuilderBusy(true);

  try {
    const res = await fetch(`/ops/exam-timetable/${runId}/delete/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() || CSRF },
      body: JSON.stringify({ confirm: 'DELETE' }),
    });
    const data = await readExamResponse(res);
    if (!res.ok || !data.ok) throw examResponseError(data);
    clearExamRequestError('delete');

    notify.success(T.deleted);
    jobRunGone(runId);

    // If the deleted run was the currently loaded one, hide the results panel.
    // We hide (not clear innerHTML) so the DOM elements remain for the next build.
    if (String(_currentRunId) === String(runId)) {
      enterFreshBuildMode();
      $('etStatus').textContent = T.ready;
      $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';
      const exportBtn = $('exportXlsx');
      if (exportBtn) { exportBtn.classList.add('d-none'); exportBtn.removeAttribute('href'); }
    }
    // A deleted run can no longer be where a Build keeps its locks from.
    if (_lockSource && String(_lockSource.runId) === String(runId)) {
      _lockSource = null;
      _examLocks = [];
      updatePinBar();
    }

    loadHistory();
  } catch (err) {
    showExamRequestError(err, 'delete');
  } finally {
    setBuilderBusy(false);
  }
}

// Load a previously saved run from the database and render it (read-only view).
async function confirmDiscardDraft({ waitForClose = false } = {}) {
  if (!hasUnsavedEdits()) return true;
  return dlg.confirm({
    title: IS_AR ? 'تجاهل التغييرات غير المحفوظة؟' : 'Discard unsaved changes?',
    body: IS_AR ? 'لم تُحفظ التغييرات اليدوية. المتابعة ستستبدل المسودة الحالية.' : 'Your manual changes have not been saved. Continuing will replace the current draft.',
    icon: 'warning',
    confirmLabel: IS_AR ? 'تجاهل التغييرات' : 'Discard changes',
    confirmClass: 'danger',
    waitForClose,
  });
}

function defaultCopyRunLabel(label) {
  const suffix = IS_AR ? ' — نسخة' : ' — Copy';
  let prefix = '';
  for (const character of String(label || '').trim()) {
    if (prefix.length + character.length > 120 - suffix.length) break;
    prefix += character;
  }
  return prefix.trimEnd() + suffix;
}

async function copyRun(runId, label, trigger) {
  if (_builderBusy) return;
  const context = { runId: _currentRunId, signature: editorSignature(), revision: _editorRevision };
  const contextIsCurrent = () => context.runId === _currentRunId
    && context.signature === editorSignature() && context.revision === _editorRevision;
  const changedMessage = IS_AR ? 'تغيّر الجدول أثناء تجهيز النسخة. أعد المحاولة.' : 'The timetable changed while preparing the copy. Try again.';
  let navigateToResult = false;
  // Where the keyboard goes back to if the list is re-rendered meanwhile - a
  // job ending in the background reloads it - and the button is gone.
  const rows = Array.from($('historyList').querySelectorAll('.et-history-item'));
  const kept = { region: 'list', id: String(runId), index: Math.max(0, rows.indexOf(trigger?.closest('.et-history-item'))), kind: 'et-copy-btn' };
  setBuilderBusy(true);
  try {
    const name = await dlg.prompt({
      title: IS_AR ? 'نسخ الجدول المحفوظ' : 'Copy saved timetable',
      body: `<p><strong>${escapeAttr(label)}</strong></p><p>${IS_AR ? 'ستُنشأ نسخة مستقلة من الإصدار المحفوظ. يبقى الجدول الأصلي كما هو.' : 'Create a separate copy of the saved version. The original timetable stays unchanged.'}</p>`,
      inputLabel: IS_AR ? 'اسم النسخة' : 'Copy name',
      inputHint: IS_AR ? 'حتى 120 حرفاً.' : 'Up to 120 characters.',
      defaultValue: defaultCopyRunLabel(label), maxLength: 120, required: true, waitForClose: true,
      confirmLabel: IS_AR ? 'نسخ' : 'Copy', cancelLabel: IS_AR ? 'إلغاء' : 'Cancel',
    });
    if (name === false || name == null) return;
    if (typeof name !== 'string' || !name.trim() || name.trim().length > 120) {
      throw new Error(IS_AR ? 'أدخل اسماً للنسخة من 1 إلى 120 حرفاً.' : 'Enter a copy name of 1 to 120 characters.');
    }
    if (!contextIsCurrent()) throw new Error(changedMessage);
    if (!await confirmDiscardDraft({ waitForClose: true })) return;
    if (!contextIsCurrent()) throw new Error(changedMessage);
    $('etStatus').textContent = IS_AR ? 'جارٍ نسخ الجدول المحفوظ...' : 'Copying saved timetable...';
    $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';
    const res = await fetch(`/ops/exam-timetable/${encodeURIComponent(runId)}/copy/`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() || CSRF },
      body: JSON.stringify({ label: name.trim() }),
    });
    const data = await readExamResponse(res);
    if (!res.ok || !data.ok) throw examResponseError(data);
    clearExamRequestError('copy');
    // Keep newer editor state even if a non-UI update bypassed the lock.
    const message = contextIsCurrent()
      ? (IS_AR ? 'تم إنشاء النسخة وفتحها.' : 'Copy created and opened.')
      : (IS_AR ? 'حُفظت النسخة في السجل. احتُفظ بالجدول الحالي لأنه تغيّر أثناء النسخ.' : 'Copy saved in history. The current timetable was kept because it changed while copying.');
    if (contextIsCurrent()) {
      cancelDraftChecks();
      clearExamCourseSourceError();
      hydrateHeaderFromRun(data);
      renderResults(data);
      _loadedRunForRebuild = true;
      _scheduleHasDraftMoves = false;
      updatePinBar();
      navigateToResult = true;
      settleJobPanel();
    }
    $('etStatus').textContent = message;
    $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';
    notify.success(message);
    await loadHistory(1);
  } catch (error) {
    $('etStatus').textContent = T.error + ': ' + showExamRequestError(error, 'copy');
    $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
  } finally {
    setBuilderBusy(false);
    if (navigateToResult) focusExamEditor();
    else if (trigger?.isConnected) trigger.focus({ preventScroll: true });
    else restoreHistoryFocus(kept);
    // A queued live check may have reached its timer while the dialog was open.
    scheduleDraftCheck();
  }
}

window.addEventListener('beforeunload', event => {
  if (!hasUnsavedEdits()) return;
  event.preventDefault();
  event.returnValue = '';
});

// Resolves to 'loaded', 'gone' (deleted meanwhile), 'failed', or nothing when
// it did not try (busy, or the registrar kept their draft).
// ``addReport``: an Add courses result opened from its job, whose report the
// registrar has not seen yet.
// ``optimiseReport``: the same for an Optimize result.
async function loadRun(runId, { addReport = false, optimiseReport = false } = {}) {
  if (_builderBusy) return undefined;
  if (!await confirmDiscardDraft() || _builderBusy) return undefined;
  cancelDraftChecks();
  setBuilderBusy(true);
  $('etStatus').textContent = T.loadingRun;
  $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';

  let navigateToResult = false;
  let outcome = 'failed';
  try {
    const res = await fetch(`/ops/exam-timetable/${runId}/`, {
      headers: { 'X-CSRFToken': getCsrfToken() || CSRF },
    });
    const data = await readExamResponse(res);
    if (!res.ok || !data.ok) throw examResponseError(data);
    clearExamRequestError('load');
    clearExamCourseSourceError();

    hydrateHeaderFromRun(data);
    renderResults(data);
    navigateToResult = true;
    _loadedRunForRebuild = true;
    _scheduleHasDraftMoves = false;
    updatePinBar();
    if (addReport && data.add_courses) showAddCoursesReport(data.add_courses);
    if (optimiseReport && data.optimisation) showOptimisationReport(data.optimisation);
    $('etStatus').textContent = T.done;
    $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';

    // Highlight active
    $('historyList').querySelectorAll('.et-history-item').forEach(el => {
      el.classList.toggle('active', el.dataset.id === String(runId));
    });
    outcome = 'loaded';
    settleJobPanel();
  } catch (err) {
    if (err.examRunGone) {
      outcome = 'gone';
      // However it was asked for - Open, or its row - a panel naming it says
      // so, and the list stops showing it.
      jobRunGone(runId, { announce: false });
      loadHistory();
    }
    $('etStatus').textContent = T.error + ': ' + showExamRequestError(err, 'load');
    $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
  } finally {
    setBuilderBusy(false);
    if (navigateToResult) focusExamEditor();
  }
  return outcome;
}

// Show in timetable (Student lists): ?run=<id>[&focus=<course>] loads that
// saved run and finds the exam. The address is then cleaned, so a reload
// starts fresh; it never carries more than a run and a course.
async function openRunFromAddress() {
  const params = new URLSearchParams(window.location.search);
  const run = params.get('run');
  if (!run || !/^[0-9]{1,18}$/.test(run)) return;
  const focus = params.get('focus');
  try { window.history.replaceState(window.history.state, '', window.location.pathname); } catch (_) { /* a sandboxed frame */ }
  if (await loadRun(run) !== 'loaded' || !focus) return;
  const entry = _currentResultData?.schedule?.find(item => item.course_code === focus);
  if (entry) findExamInTimetable(entry.course_identity || entry.course_code);
}

// Load history on page load
loadHistory();
resumeExamJob();
openRunFromAddress();

/* ── Export Excel click feedback ── */
$('exportXlsx')?.addEventListener('click', function(event) {
  updateLoadedRunActions();
  if (_builderBusy || needsExamSourceRebuild() || _sourceCoursesRejected || _scheduleHasDraftMoves || !_currentRunId || ['loading', 'error', 'review'].includes(_checkState)) {
    event.preventDefault();
    if (_scheduleHasDraftMoves) inputError(T.saveBeforeExport, $('saveLoadedBtn'));
    return;
  }
  if (this.href && this.href !== '#') {
    notify.success(IS_AR ? 'جارٍ تحميل ملف إكسل...' : 'Downloading Excel file...');
  }
});

/* ── Matrix Zoom Controls ── */
(function() {
  let zoom = 1.0;
  const scaler = $('etMatrixScaler');
  const level  = $('etZoomLevel');
  const viewport = $('etMatrixViewport');
  if (!scaler || !level) return;
  const minimumZoom = () => viewport?.clientWidth && scaler.scrollWidth
    ? Math.min(0.5, viewport.clientWidth / scaler.scrollWidth) : 0.5;

  function updateZoom() {
    scaler.style.transform = 'scale(' + zoom + ')';
    level.textContent = Math.round(zoom * 100) + '%';
    $('etZoomIn').disabled = zoom >= 2;
    $('etZoomOut').disabled = zoom <= minimumZoom();
  }

  $('etZoomIn')?.addEventListener('click', () => {
    zoom = Math.min(2.0, +(zoom + 0.1).toFixed(2));
    updateZoom();
  });

  $('etZoomOut')?.addEventListener('click', () => {
    zoom = Math.max(minimumZoom(), +(zoom - 0.1).toFixed(2));
    updateZoom();
  });

  $('etZoomFit')?.addEventListener('click', () => {
    const viewport = $('etMatrixViewport');
    if (!viewport || !scaler) return;
    const vw = viewport.clientWidth;
    const sw = scaler.scrollWidth;
    if (!vw || !sw) return;
    zoom = Math.min(1.5, vw / sw);
    updateZoom();
  });
})();

/* ── Matrix Fullscreen Toggle ── */
(function() {
  let originalParent = null;
  let originalNext = null;
  let originalFocus = null;
  let originalOverflow = '';
  let background = [];
  const panel = $('matrixPanel');
  const btn = $('matrixFullscreen');
  if (!panel || !btn) return;

  function setFullscreen(entering) {
    if (entering) {
      originalParent = panel.parentElement;
      originalNext = panel.nextElementSibling;
      originalFocus = document.activeElement;
      originalOverflow = document.body.style.overflow;
      // Escape the app shell's transformed stacking context.
      document.body.appendChild(panel);
      background = [...document.body.children].filter(element => element !== panel)
        .map(element => ({ element, inert: element.inert }));
      background.forEach(({ element }) => { element.inert = true; });
      panel.classList.add('matrix-fullscreen');
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'true');
      panel.setAttribute('aria-labelledby', 'examMatrixHeading');
      $('conflictMatrix').classList.remove('d-none');
      $('toggleMatrix').textContent = T.hide;
      $('toggleMatrix').setAttribute('aria-expanded', 'true');
      document.body.style.overflow = 'hidden';
      document.addEventListener('keydown', onKeyDown);
      btn.focus({ preventScroll: true });
    } else {
      panel.classList.remove('matrix-fullscreen');
      panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
      panel.removeAttribute('aria-labelledby');
      background.forEach(({ element, inert }) => { element.inert = inert; });
      background = [];
      pageExposedAgain();
      document.body.style.overflow = originalOverflow;
      document.removeEventListener('keydown', onKeyDown);
      if (originalParent) {
        if (originalNext?.parentElement === originalParent) originalParent.insertBefore(panel, originalNext);
        else originalParent.appendChild(panel);
      }
      if (originalFocus?.isConnected) originalFocus.focus({ preventScroll: true });
    }
    btn.innerHTML = entering ? '&#x2716;' : '&#x26F6;';
    btn.title = entering ? (IS_AR ? 'إنهاء ملء الشاشة' : 'Exit fullscreen') : (IS_AR ? 'ملء الشاشة' : 'Fullscreen');
    btn.setAttribute('aria-label', btn.title);
    btn.setAttribute('aria-expanded', String(entering));
  }
  function onKeyDown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      setFullscreen(false);
    } else if (event.key === 'Tab') {
      const focusable = [...panel.querySelectorAll('button:not(:disabled), [tabindex="0"]')]
        .filter(element => !element.closest('.d-none, [hidden]'));
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
        event.preventDefault(); first?.focus();
      }
    }
  }
  btn.addEventListener('click', () => setFullscreen(!panel.classList.contains('matrix-fullscreen')));
})();

// Only explicit loading/building navigates to the workspace. Automatic checks
// preserve the visible course even when earlier rows change height.
function examWorkspaceViewport(grid) {
  let scroller = grid.parentElement;
  while (scroller && !(scroller.scrollHeight > scroller.clientHeight
    && /^(auto|scroll|overlay)$/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
  scroller ||= document.scrollingElement || document.documentElement;
  const documentScroller = scroller === document.scrollingElement || scroller === document.documentElement || scroller === document.body;
  const bounds = documentScroller ? { top: 0, bottom: window.innerHeight } : scroller.getBoundingClientRect();
  let top = Math.max(0, bounds.top);
  const bottom = Math.min(window.innerHeight, bounds.bottom);
  const toolbar = $('examEditToolbar');
  if (toolbar?.classList.contains('et-toolbar-sticky')) {
    const toolbarRect = toolbar.getBoundingClientRect();
    if (toolbarRect.top < bottom && toolbarRect.bottom > top) top = toolbarRect.bottom;
  }
  return { scroller, top, bottom };
}

function captureExamWorkspaceAnchor() {
  const grid = $('schedGrid');
  const rect = grid?.getBoundingClientRect();
  if (!rect?.width || !rect.height) return null;
  const { scroller, top, bottom } = examWorkspaceViewport(grid);
  if (rect.bottom <= top || rect.top >= bottom || bottom <= top) return null;
  const anchor = { grid, scroller, top: rect.top };
  const dayWidth = grid.querySelector('tbody th[scope="row"]')?.getBoundingClientRect().width || 0;
  const left = rect.left + (IS_AR ? 0 : dayWidth);
  const right = rect.right - (IS_AR ? dayWidth : 0);
  for (const chip of grid.querySelectorAll('.et-course')) {
    const box = chip.getBoundingClientRect();
    if (!box.width || box.bottom <= top || box.top >= bottom || box.right <= left || box.left >= right) continue;
    const cell = chip.closest('td');
    const row = chip.closest('tr');
    anchor.course = chip.dataset.course;
    anchor.day = cell?.dataset.day;
    anchor.period = cell?.dataset.period;
    anchor.courseTop = box.top;
    anchor.rowLabel = row?.querySelector('th[scope="row"]')?.textContent;
    anchor.rowTop = row?.getBoundingClientRect().top;
    break;
  }
  if (!anchor.course) {
    // An empty period still has a meaningful visible day to preserve.
    const row = [...grid.querySelectorAll('tbody tr')].find(item => {
      const box = item.getBoundingClientRect();
      return box.height > 0 && box.bottom > top && box.top < bottom;
    });
    if (row) {
      anchor.rowLabel = row.querySelector('th[scope="row"]')?.textContent;
      anchor.rowTop = row.getBoundingClientRect().top;
    }
  }
  return anchor;
}

function restoreExamWorkspaceAnchor(anchor) {
  if (!anchor?.grid.isConnected || !anchor.scroller.isConnected) return;
  // Reading the new box accounts for the browser's own scroll anchoring.
  // Correct only the remaining displacement, without navigating an offscreen
  // workspace or changing its independently preserved horizontal position.
  let difference = anchor.grid.getBoundingClientRect().top - anchor.top;
  if (anchor.course || anchor.rowLabel) {
    const chip = [...anchor.grid.querySelectorAll('.et-course')].find(item => item.dataset.course === anchor.course);
    const cell = chip?.closest('td');
    if (chip && cell?.dataset.day === anchor.day && cell?.dataset.period === anchor.period) {
      difference = chip.getBoundingClientRect().top - anchor.courseTop;
    } else {
      // A moved course must not take the viewport with it. Keep its old day
      // in view; an explicit Move dialog separately reveals the destination.
      const row = [...anchor.grid.querySelectorAll('tbody tr')].find(item => item.querySelector('th[scope="row"]')?.textContent === anchor.rowLabel);
      if (row) difference = row.getBoundingClientRect().top - anchor.rowTop;
    }
  }
  if (Number.isFinite(difference) && Math.abs(difference) >= 0.5) anchor.scroller.scrollTop += difference;
}

function focusExamEditor() {
  const heading = $('examEditHeading');
  if (!heading || _builderBusy) return;
  heading.setAttribute('tabindex', '-1');
  ($('examScheduleWorkspace') || $('etResults')).scrollIntoView({ block: 'start', inline: 'nearest', behavior: 'auto' });
  heading.focus({ preventScroll: true });
}

function updateExamGridGeometry() {
  const grid = $('schedGrid');
  const width = grid?.querySelector('tbody th[scope="row"]')?.getBoundingClientRect().width;
  if (width > 0) grid.style.setProperty('--exam-grid-row-header-width', `${Math.ceil(width)}px`);
  const toolbar = $('examEditToolbar');
  if (grid && toolbar) {
    const { scroller } = examWorkspaceViewport(grid);
    const height = toolbar.getBoundingClientRect().height;
    const pagePadding = parseFloat(getComputedStyle(scroller).paddingTop) || 0;
    toolbar.style.setProperty('--exam-toolbar-top', `${-pagePadding}px`);
    // Expanded help, zoom, and small screens must not leave a tall panel
    // covering the main working area.
    toolbar.classList.toggle('et-toolbar-sticky', window.innerWidth >= 768
      && height > 0 && height <= Math.min(160, scroller.clientHeight / 4));
  }
}
if (typeof ResizeObserver !== 'undefined' && $('schedGrid')) {
  const gridObserver = new ResizeObserver(updateExamGridGeometry);
  gridObserver.observe($('schedGrid'));
  if ($('examEditToolbar')) gridObserver.observe($('examEditToolbar'));
}
window.addEventListener('resize', updateExamGridGeometry);
