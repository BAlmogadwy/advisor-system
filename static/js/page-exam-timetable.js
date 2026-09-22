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
 *   _currentRunId    – ID of the currently viewed run (for export link)
 *   _drillData       – {overload: [], heavy: []} detail records for KPI drilldown
 *   _programCourses  – {programName: Set(course_codes)} for programme-based filtering
 */
const IS_AR = LANGUAGE_CODE === 'ar';
const CAN_DELETE_EXAM_TIMETABLE = document.querySelector('[data-exam-builder]')?.dataset.canDeleteExamTimetable === 'true';
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
  loadedRunAction: IS_AR ? 'احفظ التغييرات أو حسّن الجدول المحمّل. انقر تحميل المقررات لبناء جدول جديد.' : 'Use Save Changes or Optimize for the loaded run. Click Load Courses for a fresh build.',
  savingLoaded:   IS_AR ? 'جارٍ حفظ التغييرات...' : 'Saving loaded-run changes...',
  optimizing:     IS_AR ? 'جارٍ تحسين الجدول المحمّل...' : 'Optimizing from loaded run...',
  optimized:      IS_AR ? 'تم حفظ الجدول المحسّن.' : 'Optimized run saved.',
  repairing:      IS_AR ? 'جارٍ إصلاح الجدول بأقل تغيير...' : 'Repairing with the fewest moves...',
  repaired:       IS_AR ? 'تم حفظ الجدول بعد الإصلاح.' : 'Repaired run saved.',
  savedChanges:   IS_AR ? 'تم حفظ التغييرات.' : 'Loaded-run changes saved.',
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
  const target = field?.id === 'examPinCourse' ? $('examPinCourseSearch') : field;
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

function readExamHeader() {
  const label = $('etLabel').value.trim();
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
    renderChips('progList', data.programs ?? [], 'progCount');
    renderChips('secList', data.sections ?? [], 'secCount');
  } catch (err) {
    showExamRequestError(err, 'filters');
  }
}
loadFilters();

/* ── Step 1: Load Courses (preview) ── */
let _coursesLoaded = false;

const COMPUTER_PREFIXES = new Set(['AI', 'DS', 'CS', 'IS', 'COE', 'CYB']);

function clearCoursePreview() {
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

function renderCourseList(courses) {
  _courseMetadata = Object.fromEntries(courses.map(course => [course.course_code, course]));
  reconcilePinsWithCourses();
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
    return `<label class="et-chip et-course-option active" data-search="${escapeAttr(searchable)}">
      <input type="checkbox" value="${escapeAttr(code)}" checked data-online="${c.is_online ? '1' : '0'}" data-source-code="${escapeAttr(sourceCode)}" data-course-name="${escapeAttr(c.course_name || '')}" data-course-identity="${escapeAttr(c.course_identity || '')}">
      <span class="et-course-main"><span class="et-course-code" dir="ltr">${escapeAttr(code)}</span>${online}<span class="et-course-name">${escapeAttr(c.course_name || code)}</span></span>
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
  $('courseSearchCount').textContent = IS_AR ? `عرض ${shown} من ${rows.length} مقرر` : `Showing ${shown} of ${rows.length} courses`;
  $('courseNoMatches').hidden = shown > 0;
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
  const randomize = $('etRandomize').checked;
  const thinThreshold = readThinThresholdForPost();
  if (thinThreshold === null) return;

  $('buildBtn').disabled = true;
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
      randomize,
      thin_conflict_threshold: thinThreshold,
      previous_run_id: _currentResultData?.run_id,
      base_schedule: baseSchedule && baseSchedule.length ? baseSchedule : undefined,
    };
    const res = await fetch('/ops/exam-timetable/build/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() || CSRF },
      body: JSON.stringify(payload),
    });
    const data = await readExamResponse(res);
    if (!res.ok || !data.ok) {
      if (data.feasibility_error && data.violations) {
        let msg = T.infeasible + '\n';
        data.violations.forEach(v => {
          msg += '\n• ' + T.infeasItem
            .replace('{prog}', v.program)
            .replace('{term}', v.programme_term)
            .replace('{size}', v.bucket_size)
            .replace('{days}', v.num_days)
            .replace('{courses}', v.courses.join(', '));
        });
        throw new Error(msg);
      }
      throw examResponseError(data);
    }

    clearExamRequestError('build');
    clearExamCourseSourceError();
    renderResults(data);
    navigateToResult = true;
    _loadedRunForRebuild = data.rebuild_mode === 'loaded_schedule';
    _scheduleHasDraftMoves = false;
    updatePinBar();
    $('etStatus').textContent = T.done;
    $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';
    loadHistory();
  } catch (err) {
    $('etStatus').textContent = T.error + ': ' + showExamRequestError(err, 'build');
    $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
  } finally {
    setBuilderBusy(false);
    updateLoadedRunActions();
    if (navigateToResult) focusExamEditor();
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

function collectLoadedRunPayload(mode) {
  if (needsExamSourceRebuild()) return null;
  const selectedCourses = getCheckedValues('courseList');
  if (!selectedCourses.length) {
    $('etStatus').textContent = T.noSelected;
    $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    return null;
  }
  const header = readExamHeader();
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
  const pinned = validatedPinPayload(header);
  if (!pinned) return null;
  const thinThreshold = readThinThresholdForPost();
  if (thinThreshold === null) return null;
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
    randomize: $('etRandomize').checked,
    thin_conflict_threshold: thinThreshold,
    previous_run_id: _currentResultData?.run_id,
    assign_rooms: _currentResultData?.assign_rooms ?? true,
    update_run_id: mode === 'optimize_loaded' && _currentResultData?.rebuild_mode === 'optimized_from_loaded'
      ? _currentResultData.run_id
      : undefined,
    mode,
    base_schedule: baseSchedule,
  };
}

async function runLoadedRunAction(mode, button, busyText, successText) {
  if (_builderBusy) return;
  let payload = collectLoadedRunPayload(mode);
  if (!payload) return;
  cancelDraftChecks();
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
        throw new Error(IS_AR ? 'تغيّرت بيانات المصدر أو لم يتم التحقق منها. انقر «تحقق من التغييرات» لمراجعتها قبل الحفظ.' : 'Source data changed or has not been reviewed. Click Check changes to review it before saving.');
      }
      payload = collectLoadedRunPayload(mode);
      if (!payload) return;
      payload.expected_input_fingerprint = _currentResultData.input_fingerprint;
    }
    payload.editor_revision = _editorRevision;
    const res = await fetch('/ops/exam-timetable/build/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() || CSRF },
      body: JSON.stringify(payload),
    });
    const data = await readExamResponse(res);
    if (!res.ok || !data.ok) {
      if (data.error_code === 'inputs_changed') {
        _evaluatedEditorSignature = null;
        _checkState = 'error';
        _checkError = data.error;
      }
      throw examResponseError(data);
    }
    clearExamRequestError('save-optimize');
    clearExamCourseSourceError();
    hydrateHeaderFromRun({ ...data, label: payload.label });
    renderResults(data);
    _loadedRunForRebuild = true;
    _scheduleHasDraftMoves = false;
    updatePinBar();
    updateLoadedRunActions();
    if (data.minimum_change) {
      const summary = describeMinimumChange(data.minimum_change);
      $('etStatus').innerHTML = summary.html;
      $('etStatus').className = `alert ${summary.clean ? 'alert-success' : 'alert-warning'} mt-2 py-2 mb-0`;
    } else {
      $('etStatus').textContent = successText;
      $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';
    }
    loadHistory();
  } catch (err) {
    $('etStatus').textContent = T.error + ': ' + showExamRequestError(err, 'save-optimize');
    $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
  } finally {
    button.disabled = false;
    setBuilderBusy(false);
    updateLoadedRunActions();
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
let _renderingResults = false;
let _checkState = 'checked';
let _checkError = '';
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
let _departmentContext = null;
let _departmentRequestToken = 0;
let _departmentBusy = false;
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
  const placements = (_currentResultData?.schedule || []).map(entry =>
    [entry.course_identity || entry.course_code, entry.day, entry.period]).sort();
  return JSON.stringify({
    label: $('etLabel').value.trim(),
    days: generateDayLabels(), dayCount: $('etNumDays').value.trim(), periods,
    maxPerDay: $('etMaxPerDay').value.trim(),
    programs: getCheckedValues('progList').sort(), sections: getCheckedValues('secList').sort(),
    relaxThin: $('etRelaxThin').checked,
    thinThreshold: $('etRelaxThin').checked ? $('etThinThreshold').value.trim() : '',
    randomize: $('etRandomize').checked, courses, pins, placements,
  });
}

// Compare the saved report with a fresh evaluation, excluding timestamps.
// Inputs can change while placements stay identical; that still needs a save.
function reportSignature(data) {
  const qa = { ...(data.qa || {}) };
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
}

/* Department exports always use the saved run captured when the dialog opens. */
function departmentExportBlocked() {
  return !_currentRunId || _builderBusy || needsExamSourceRebuild() || _sourceCoursesRejected
    || hasUnsavedEdits() || _scheduleHasDraftMoves || ['loading', 'error', 'review'].includes(_checkState);
}

function departmentContextIsCurrent() {
  return _departmentContext && !departmentExportBlocked()
    && _departmentContext.runId === _currentRunId
    && _departmentContext.signature === editorSignature()
    && _departmentContext.revision === _editorRevision;
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
  if (event.target.matches('input, select') && !event.target.closest('#examPinEditor')) updateLoadedRunActions();
}));

function setBuilderBusy(busy) {
  _builderBusy = busy;
  updateBuildSummary();
  // Keep the status live region readable while preventing edits to a request
  // already in flight. Inert also covers course-selection links and dragging.
  for (const id of ['examSettingsControls', 'schedGrid', 'pinBar', 'historyList', 'historyPages', 'examEditToolbar', 'examReviewPanel', 'examMoveDialog']) {
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
}

function updateBuildSummary() {
  const selected = getCheckedValues('courseList').length;
  const days = generateDayLabels().length;
  const periods = readExamPeriods().periods.length;
  const pins = Object.values(_pinnedCourses).length;
  $('examBuildSummary').textContent = !_coursesLoaded
    ? (IS_AR ? 'حمّل المقررات للمتابعة.' : 'Load courses to continue.')
    : (IS_AR ? `${selected} مقرر · ${days} أيام · ${periods} فترات يومياً · ${pins} مثبت`
      : `${selected} courses · ${days} days · ${periods} periods/day · ${pins} fixed`);
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
  if (saveBtn) {
    saveBtn.classList.toggle('d-none', !hasLoadedSchedule);
    saveBtn.disabled = _builderBusy || needsExamSourceRebuild() || !hasLoadedSchedule || !_scheduleHasDraftMoves;
  }
  if (optimizeBtn) {
    optimizeBtn.classList.toggle('d-none', !hasLoadedSchedule);
    optimizeBtn.disabled = _builderBusy || needsExamSourceRebuild() || !hasLoadedSchedule;
  }
  const minChangeBtn = $('minChangeBtn');
  if (minChangeBtn) {
    minChangeBtn.classList.toggle('d-none', !hasLoadedSchedule);
    minChangeBtn.disabled = _builderBusy || needsExamSourceRebuild() || !hasLoadedSchedule;
  }
  $('buildBtn').classList.toggle('d-none', hasLoadedSchedule);
  if (hasLoadedSchedule) {
    $('buildBtn').disabled = true;
    $('buildBtn').title = T.loadedRunAction;
  } else {
    $('buildBtn').disabled = _builderBusy || !_coursesLoaded || !getCheckedValues('courseList').length;
    $('buildBtn').title = '';
  }
  updateExportState();
  updateEditingStatus();
}

function enterFreshBuildMode() {
  clearExamCourseSourceError();
  cancelDraftChecks();
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

const EXAM_METRICS = [
  { id: 'kCourses', value: data => data.courses_count ?? data.qa?.total_courses ?? 0 },
  { id: 'kStudents', value: data => data.students_count ?? data.qa?.total_students ?? 0 },
  { id: 'kSlots', value: data => data.qa?.slots_used ?? 0 },
  { id: 'kMaxDay', value: data => data.qa?.max_exams_per_day_per_student ?? 0, lower: true },
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
  if (baseline !== current) return { text: IS_AR ? 'المقارنة متوقفة: تغيّرت بيانات الجدول' : 'Comparison paused: timetable data changed', modifier: 'is-neutral' };
  const before = Number(metric.value(_savedResultData));
  const after = Number(metric.value(_currentResultData));
  const difference = after - before;
  const suffix = metric.suffix || '';
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
   exams moved and where, not just that the run was saved. */
function describeMinimumChange(report) {
  const moves = Array.isArray(report?.moves) ? report.moves : [];
  const unseated = Array.isArray(report?.unseated) ? report.unseated : [];
  const remaining = Number(report?.violations_after) || 0;
  const arrow = IS_AR ? '←' : '→';
  const parts = [];
  if (!moves.length && !unseated.length && !remaining) {
    parts.push(escapeAttr(IS_AR ? 'لا يوجد ما يحتاج إصلاحاً: لا يخالف أي اختبار القواعد.' : 'Nothing to repair: no exam breaks a rule.'));
  } else if (moves.length) {
    const count = moves.length;
    const lead = IS_AR
      ? `تم نقل ${count} ${count === 1 ? 'اختبار' : 'اختبارات'}${report?.proven_minimal ? ' وهو أقل عدد ممكن' : ''}:`
      : `Moved ${count} exam${count === 1 ? '' : 's'}${report?.proven_minimal ? ', the fewest possible' : ''}:`;
    const items = moves.slice(0, 6).map(move => `<li><strong><bdi dir="ltr">${escapeAttr(move.course_code)}</bdi></strong> <bdi dir="ltr">${escapeAttr(examLocation(move.from))}</bdi> <span aria-hidden="true">${arrow}</span> <bdi dir="ltr">${escapeAttr(examLocation(move.to))}</bdi></li>`).join('');
    const more = moves.length > 6 ? `<li>${escapeAttr(IS_AR ? `و${moves.length - 6} أخرى` : `and ${moves.length - 6} more`)}</li>` : '';
    parts.push(`${escapeAttr(lead)}<ul class="mb-0 mt-1">${items}${more}</ul>`);
  }
  if (unseated.length) {
    parts.push(escapeAttr(IS_AR
      ? `لم يوجد موعد نظامي لـ ${unseated.length} ${unseated.length === 1 ? 'اختبار' : 'اختبارات'} دون نقل اختبار ثبّته أو نقلته، فنُقلت إلى قائمة الفائض.`
      : `${unseated.length} exam${unseated.length === 1 ? '' : 's'} had no legal slot without moving one you pinned or moved, so ${unseated.length === 1 ? 'it was' : 'they were'} sent to Overflow.`));
  }
  if (remaining) {
    // Only exams the registrar froze can still clash: the repair never moves them.
    parts.push(escapeAttr(IS_AR
      ? `بقيت ${remaining} مخالفة بين اختبارات ثبّتها أو نقلتها؛ لم يتم تغييرها.`
      : `${remaining} rule break${remaining === 1 ? '' : 's'} remain between exams you pinned or moved; those were left as you placed them.`));
  }
  return { html: parts.join(' '), clean: !remaining && !unseated.length };
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

function updateChangeReview() {
  if (!$('examChangesContent') || !_currentResultData || !_savedResultData) return;
  const focused = document.activeElement;
  const focusedAction = focused?.closest?.('#examChangesContent') && (focused.hasAttribute('data-find-exam') ? 'data-find-exam' : focused.hasAttribute('data-move-exam') ? 'data-move-exam' : null);
  const focusedIdentity = focusedAction ? focused.getAttribute(focusedAction) : null;
  const focusedKind = focused?.closest?.('[data-change-kind]')?.dataset.changeKind;
  const selected = new Set(getCheckedValues('courseList'));
  const baseline = new Map((_savedResultData.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
  const moved = _currentResultData.schedule.filter(entry => {
    const before = baseline.get(entry.course_identity || entry.course_code);
    return selected.has(entry.course_code) && before && (before.day !== entry.day || before.period !== entry.period);
  });
  const savedPins = new Map((_savedResultData.pinned || []).map(pin => {
    const course = (_savedResultData.schedule || []).find(entry => entry.course_code === pin.course_code);
    return [pin.course_identity || course?.course_identity || pin.course_code, pin];
  }));
  const pinChanges = [...new Set([...savedPins.keys(), ...Object.keys(_pinnedCourses)])].filter(identity => {
    const before = savedPins.get(identity);
    const after = _pinnedCourses[identity];
    return (!before !== !after) || (before && after && (before.day !== after.day || before.period !== after.period));
  });
  $('examChangeCount').textContent = IS_AR ? `${moved.length} منقول · ${pinChanges.length} تغيير تثبيت` : `${moved.length} moved · ${pinChanges.length} pin changes`;
  $('examChangesSummary').textContent = IS_AR ? `مراجعة التغييرات (${moved.length + pinChanges.length})` : `Review changes (${moved.length + pinChanges.length})`;
  const row = (entry, before, after, kind) => `<li class="et-change-item" data-change-identity="${escapeAttr(entry.course_identity || entry.course_code)}" data-change-kind="${kind}"><span class="et-change-course"><strong>${escapeAttr(entry.course_code)}</strong><small class="et-course-name">${escapeAttr(entry.course_name || '')}</small></span><span class="et-change-times"><span>${IS_AR ? 'المحفوظ: ' : 'Saved: '}<bdi dir="ltr">${escapeAttr(examLocation(before))}</bdi></span><span aria-hidden="true"> ${IS_AR ? '←' : '→'} </span><span>${IS_AR ? 'الحالي: ' : 'Current: '}<bdi dir="ltr">${escapeAttr(examLocation(after))}</bdi></span></span>${examActionMarkup(entry, { allowStaleMove: true })}</li>`;
  const movedRows = moved.map(entry => row(entry, baseline.get(entry.course_identity || entry.course_code), entry, 'placement')).join('');
  const pinRows = pinChanges.map(identity => {
    const entry = currentExamByIdentity(identity) || baseline.get(identity) || _pinnedCourses[identity] || savedPins.get(identity);
    return row(entry, savedPins.get(identity), _pinnedCourses[identity], 'pin');
  }).join('');
  $('examChangesContent').innerHTML = (movedRows ? `<section class="et-change-group"><h4>${IS_AR ? 'مواعيد الاختبارات' : 'Exam placements'}</h4><ul class="et-change-list">${movedRows}</ul></section>` : '')
    + (pinRows ? `<section class="et-change-group"><h4>${IS_AR ? 'تغييرات التثبيت' : 'Pin changes'}</h4><ul class="et-change-list">${pinRows}</ul></section>` : '')
    + (!movedRows && !pinRows ? `<p class="et-change-empty">${IS_AR ? 'لم تتغير مواعيد الاختبارات أو تثبيتاتها عن الجدول المحفوظ.' : 'Exam placements and pins match the saved timetable.'}</p>` : '');
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
    review: IS_AR ? 'بيانات المصدر تحتاج مراجعة' : 'Source data needs review',
    source: IS_AR ? 'مراجعة مصدر المقررات مطلوبة' : 'Course source review required',
  };
  if ($('examCheckStatus')) $('examCheckStatus').textContent = hasSchedule ? (labels[state] || labels.stale) + dirtyLabel : '';
  $('etResults').dataset.calculationState = hasSchedule ? state : '';
  for (const id of ['examSummaryCards', 'kStatusBanner', 'kpiDrill']) {
    $(id)?.classList.toggle('et-calculation-stale', hasSchedule && !fresh);
  }
  const banner = $('draftImpactBanner');
  if (banner) {
    banner.classList.toggle('d-none', !hasSchedule || sourceRebuild || (fresh && state !== 'review'));
    banner.textContent = state === 'review'
      ? (IS_AR ? 'انقر «تحقق من التغييرات» لمراجعة بيانات المصدر المحدثة قبل الحفظ.' : 'Click Check changes to review the updated source data before saving.')
      : state === 'error' && _checkError ? _checkError : (IS_AR
      ? 'الأرقام والتفاصيل المعروضة تخص آخر تحقق. تحقق من التغييرات لتحديثها؛ لن يتم نقل أي اختبار.'
      : 'Cards and details show the last checked arrangement. Check changes to update them; no exam will be moved.');
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
  examReview?.update(_currentResultData, {
    stale: !fresh, blocked: sourceRebuild || _sourceCoursesRejected,
    visibleCodes: _coursesLoaded ? getCheckedValues('courseList') : null,
  });
}

function cancelDraftChecks() {
  clearTimeout(_checkTimer);
  _checkTimer = null;
  _queuedCheck = false;
  _draftImpactSeq++;
}

function scheduleDraftCheck() {
  clearTimeout(_checkTimer);
  _checkTimer = null;
  if (!_currentResultData || needsExamSourceRebuild() || _builderBusy || !_scheduleHasDraftMoves
      || _evaluatedEditorSignature === editorSignature() || !$('examLiveUpdate')?.checked) return;
  _checkTimer = setTimeout(() => {
    _checkTimer = null;
    refreshDraftImpact();
  }, LIVE_UPDATE_DELAY);
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
  const loadingAnchor = captureExamWorkspaceAnchor();
  _checkState = 'loading';
  _checkError = '';
  updateEditingStatus();
  restoreExamWorkspaceAnchor(loadingAnchor);
  const task = (async () => {
    try {
      const res = await fetch('/ops/exam-timetable/draft-impact/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() || CSRF },
        body: JSON.stringify(payload),
      });
      const data = await readExamResponse(res);
      if (seq !== _draftImpactSeq || runId !== _currentRunId || revision !== _editorRevision || signature !== editorSignature()) return false;
      if (!res.ok || !data.ok) throw examResponseError(data);
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
        _checkState = 'error';
        _checkRequestError = err;
        _checkError = showExamRequestError(err, 'check');
        updateLoadedRunActions();
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

function hydrateHeaderFromRun(data) {
  restorePinsFromRun(data);
  $('etLabel').value = data.label ?? '';
  for (const [key, containerId, countId] of [
    ['programs', 'progList', 'progCount'],
    ['sections', 'secList', 'secCount'],
  ]) {
    const selected = data.enrollment_scope?.[key];
    if (!Array.isArray(selected)) continue;
    const inputs = [...$(containerId).querySelectorAll('input')];
    for (const cb of inputs) {
      cb.checked = !selected.length || selected.includes(cb.value);
      cb.closest('.et-chip').classList.toggle('active', cb.checked);
    }
    updateChipCount(containerId, countId, inputs.length);
  }

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
    renderCourseList(courseRows);
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
      if (pinForCourse(entry.course_code)) reason = IS_AR ? 'ألغِ التثبيت للنقل.' : 'Unpin to move.';
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
      body: rows.map(row => `<tr><td>${drillCourseMarkup(row)}</td><td><bdi dir="ltr">${escapeAttr(`${row.day || ''} · ${row.period || ''}`)}</bdi></td><td>${roomSectionsMarkup(row)}</td><td>${escapeAttr(studentGroupLabel(row.gender))}</td><td>${escapeAttr(row.student_count ?? 0)}</td></tr>`).join(''),
      colspan: 5,
    };
  },
  'room-double'(rows) {
    return {
      title: IS_AR ? 'تعارضات حجز القاعات' : 'Room double bookings',
      head: `<tr><th>${IS_AR ? 'القاعة' : 'Room'}</th><th>${IS_AR ? 'الموعد' : 'Time'}</th><th>${IS_AR ? 'المقررات' : 'Courses'}</th></tr>`,
      body: rows.map(row => {
        const slot = _slotsByIndex[row.slot_index];
        return `<tr><td>${escapeAttr(row.room_code || '')}</td><td>${escapeAttr(slot ? `${slot.day} · ${slot.period}` : `#${row.slot_index}`)}</td><td>${drillCourseListMarkup(row.courses)}</td></tr>`;
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
      body: rows.map(r => {
        const isBucket = r.kind === 'bucket-day';
        const typeBadge = isBucket
          ? `<span class="badge bg-warning text-dark">${IS_AR ? 'مجموعة' : 'Bucket'}</span>`
          : `<span class="badge bg-danger">${IS_AR ? 'طالب' : 'Student'}</span>`;
        const who = isBucket
          ? `${r.program || ''}/Term${r.programme_term ?? ''}`
          : `<strong>${r.student_id}</strong>`;
        const when = isBucket ? (r.day || '') : slotLabel(r.slot_index);
        const courses = drillCourseListMarkup(r.courses);
        return `<tr>
          <td>${typeBadge}</td>
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
          <td>${drillCourseMarkup(r.course_identity || r.course_code)}</td>
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
  $('kpiDrillHead').innerHTML = r.head;
  $('kpiDrillBody').innerHTML = rows.length
    ? r.body
    : `<tr><td colspan="${r.colspan}" class="text-center text-secondary py-3">${IS_AR ? 'لا توجد بيانات' : 'No records'}</td></tr>`;
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
  const workspaceAnchor = evaluation && preserveViewport ? captureExamWorkspaceAnchor() : null;
  const openDrillType = !$('kpiDrill').classList.contains('d-none') ? $('kpiDrill').dataset.type : null;
  const matrixOpen = !$('conflictMatrix').classList.contains('d-none');
  const matrixScroll = { left: $('etMatrixViewport').scrollLeft, top: $('etMatrixViewport').scrollTop };
  const matrixFocusKey = document.activeElement?.closest('[data-matrix-key]')?.dataset.matrixKey;
  _renderingResults = true;
  if (evaluation) {
    const inputChanged = Boolean(_savedResultData?.input_fingerprint && data.input_fingerprint
      && _savedResultData.input_fingerprint !== data.input_fingerprint);
    _evaluatedReportDirty = inputChanged || reportSignature(data) !== reportSignature(_savedResultData || {});
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
      pinned: Object.values(_pinnedCourses).map(pin => ({ ...pin })) };
    data = _currentResultData;
  } else {
    cancelDraftChecks();
    restorePinsFromRun(data);
    _currentResultData = cloneData(data);
    _undoCommands = [];
    _redoCommands = [];
    _editorRevision = 0;
    _foundExamIdentity = null;
  }
  hideLegacyQaWarnings();
  _checkState = 'checked';
  _checkError = '';
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
  // Store drilldown data + close any open panel
  _drillData = {
    conflicts:       [
      ...(data.qa?.same_slot_conflicts ?? []).map(r => ({ ...r, kind: 'same-slot' })),
      ...(data.qa?.bucket_day_violations ?? []).map(r => ({ ...r, kind: 'bucket-day' })),
    ],
    overload:        data.qa?.overload_details ?? [],
    heavy:           data.qa?.heavy_day_details ?? [],
    'thin-courses':  data.qa?.thin_courses ?? [],
    'thin-clash':    data.qa?.thin_clash_risk ?? [],
    'multi-sitting': data.qa?.multi_sitting_details ?? [],
    'section-mapping': data.qa?.section_mapping?.details ?? [],
    'room-unassigned': data.qa?.rooms?.unassigned_room_sections ?? [],
    'room-double': data.qa?.rooms?.room_double_bookings ?? [],
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
  };
  return `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths[name] || ''}</svg>`;
}

function examCourseCardMarkup(course, { classes, attributes = '', actions = '', extra = '', pinned = false }) {
  const code = String(course?.course_code || '');
  const fullName = String(course?.course_name || '').trim();
  const shortName = compactExamCourseName(fullName);
  const label = `${code} — ${fullName || code}`;
  const onlineLabel = IS_AR ? 'مقرر عن بُعد' : 'Online course';
  return `<span class="et-course-card ${classes}" role="group" tabindex="-1" aria-label="${escapeAttr(label)}" title="${escapeAttr(label)}" ${attributes}><strong class="et-course-code">${pinned ? `<span class="et-pin-lock" aria-hidden="true">${examCourseIcon('lock')}</span> ` : ''}${escapeAttr(code)}${course?.is_online ? ` <span class="et-course-online" role="img" aria-label="${onlineLabel}" title="${onlineLabel}">${examCourseIcon('online')}</span>` : ''}</strong>${shortName ? `<span class="et-course-short-name">${escapeAttr(shortName)}</span><span class="et-course-full-name" aria-hidden="true">${escapeAttr(fullName)}</span>` : ''}${actions}${extra}<span class="et-review-badges"></span></span>`;
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
  const scrollLeft = container.scrollLeft;
  const coursesByCode = new Map(schedule.map(entry => [entry.course_code, entry]));
  const courseChip = code => {
    const pinned = Boolean(pinForCourse(code));
    const course = coursesByCode.get(code);
    const fullName = String(course?.course_name || '').trim();
    const label = `${code} — ${fullName || code}`;
    const pinLabel = pinned ? T.pinRemove : (IS_AR ? 'تثبيت' : 'Pin');
    const moveLabel = IS_AR ? 'نقل' : 'Move';
    const lockHint = IS_AR ? 'ألغِ التثبيت للنقل' : 'Unpin to move';
    const relatedLabel = IS_AR ? 'عرض الطلاب المشتركين' : 'Show shared students';
    return examCourseCardMarkup(course, {
      classes: `et-course${pinned ? ' et-pinned' : ''}`, pinned,
      attributes: `data-course="${escapeAttr(code)}" draggable="${!pinned}"`,
      actions: `<span class="et-schedule-course-actions et-course-card-actions"><button type="button" data-exam-pin="${escapeAttr(code)}" aria-pressed="${pinned}" aria-label="${escapeAttr(`${pinLabel}: ${label}`)}" title="${escapeAttr(`${pinLabel}: ${label}`)}" ${course?.day === 'OVERFLOW' ? 'disabled' : ''}>${examCourseIcon('pin')}</button><button type="button" data-exam-move="${escapeAttr(code)}" aria-label="${escapeAttr(`${moveLabel}: ${label}`)}" title="${escapeAttr(`${pinned ? lockHint : moveLabel}: ${label}`)}" ${pinned ? 'disabled' : ''}>${examCourseIcon('move')}</button></span>`,
      extra: `<button type="button" class="et-related-action" data-exam-related="${escapeAttr(course?.course_identity || code)}" aria-label="${escapeAttr(`${relatedLabel}: ${label}`)}" title="${escapeAttr(`${relatedLabel}: ${label}`)}">${examCourseIcon('related')}</button>`,
    });
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
    html += `<th scope="col"><bdi dir="ltr">${escapeAttr(p)}</bdi></th>`;
  }
  html += '</tr></thead>';

  // Body: one row per day
  html += '<tbody>';
  for (const day of dayOrder) {
    html += '<tr>';
    html += `<th scope="row"><span class="et-grid-day-label"><bdi dir="ltr">${escapeAttr(day)}</bdi></span></th>`;
    for (const period of periodOrder) {
      const courses = (grid[day] && grid[day][period]) ? grid[day][period] : [];
      if (courses.length === 0) {
        html += `<td class="et-empty" data-day="${escapeAttr(day)}" data-period="${escapeAttr(period)}">—</td>`;
      } else {
        const chips = courses.map(code => courseChip(code)).join(' ');
        html += `<td data-day="${escapeAttr(day)}" data-period="${escapeAttr(period)}"><div class="et-slot-courses">${chips}</div></td>`;
      }
    }
    html += '</tr>';
  }

  // Overflow row (if any)
  if (overflowCourses.length > 0) {
    html += `<tr class="et-overflow-row">`;
    html += `<th scope="row" class="et-overflow-label"><span class="et-grid-day-label">${T.overflow}</span></th>`;
    const chips = overflowCourses.map(code => courseChip(code)).join(' ');
    html += `<td colspan="${periodOrder.length}"><div class="et-slot-courses">${chips}</div></td>`;
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
  return '';
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
function createPinCoursePicker() {
  const select = $('examPinCourse'), input = $('examPinCourseSearch');
  const root = $('examPinCoursePicker'), toggle = $('examPinCourseToggle');
  const popup = $('examPinCoursePopup'), list = $('examPinCourseResults');
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
      return `<div id="examPinCourseOption-${index}" role="option" data-value="${escapeAttr(option.value)}" aria-selected="false" class="et-course-picker-option"><strong dir="ltr">${escapeAttr(option.value)}</strong><span>${escapeAttr(course?.course_name || option.textContent)}</span>${plans ? `<small>${escapeAttr(plans)}</small>` : ''}</div>`;
    }).join('');
    $('examPinCourseEmpty').hidden = matches.length > 0;
    $('examPinCourseSearchStatus').textContent = IS_AR ? `${matches.length} مقرر مطابق` : `${matches.length} matching courses`;
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
    renderPinEditor();
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
  };
}
const pinCoursePicker = createPinCoursePicker();

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
  $('applyExamPin').textContent = pinForCourse($('examPinCourse').value) ? T.updatePin : T.pinCourse;
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
  return { pins: cloneData(_pinnedCourses), placements: (_currentResultData?.schedule || []).map(entry => ({
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
  refreshManualEditor();
  restoreExamWorkspaceAnchor(workspaceAnchor);
  return true;
}

function restoreManualSnapshot(snapshot) {
  const workspaceAnchor = captureExamWorkspaceAnchor();
  _pinnedCourses = cloneData(snapshot.pins);
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
  return runManualCommand(() => {
    _pinnedCourses[pinIdentity(course)] = { course_code: code, course_identity: pinIdentity(course), course_name: course.course_name || '', day, day_identity: examDayIdentity(day), period };
    if (_currentResultData) updateCurrentScheduleMove(code, day, period);
  });
}

function removeCoursePin(identity) {
  return runManualCommand(() => { delete _pinnedCourses[identity]; });
}

function clearCoursePins() {
  return runManualCommand(() => { _pinnedCourses = {}; });
}

function moveExamCourse(code, day, period) {
  if (_builderBusy) return false;
  const entry = _currentResultData?.schedule?.find(item => item.course_code === code);
  if (!entry || (entry.day === day && entry.period === period)) return false;
  if (pinForCourse(code)) return inputError(IS_AR ? 'ألغِ تثبيت الاختبار قبل نقله، أو عدّل موعده في المواعيد المثبتة.' : 'Unpin to move, or deliberately change its time in Fixed exam times.');
  if (!_currentResultData.slots.some(slot => slot.day === day && slot.period === period)) return false;
  return runManualCommand(() => updateCurrentScheduleMove(code, day, period));
}

function toggleExamPin(code) {
  const entry = _currentResultData?.schedule?.find(item => item.course_code === code);
  if (!entry || entry.day === 'OVERFLOW' || _builderBusy) return;
  const pin = pinForCourse(code);
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
  const pin = pinForCourse($('examPinCourse').value);
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
}

$('schedGrid').addEventListener('dragstart', event => {
  const chip = event.target.closest('.et-course');
  if (!chip || event.target.closest('button') || _builderBusy || pinForCourse(chip.dataset.course)) {
    event.preventDefault();
    return;
  }
  event.dataTransfer.setData('text/plain', chip.dataset.course);
  event.dataTransfer.effectAllowed = 'move';
});
$('schedGrid').addEventListener('dragover', event => {
  const cell = event.target.closest('td[data-day]');
  if (!cell || _builderBusy) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  $('schedGrid').querySelectorAll('.et-drag-over').forEach(item => item.classList.remove('et-drag-over'));
  cell.classList.add('et-drag-over');
});
$('schedGrid').addEventListener('dragleave', event => event.target.closest('td')?.classList.remove('et-drag-over'));
$('schedGrid').addEventListener('dragend', () => {
  $('schedGrid').querySelectorAll('.et-drag-over').forEach(item => item.classList.remove('et-drag-over'));
});
$('schedGrid').addEventListener('drop', event => {
  event.preventDefault();
  $('schedGrid').querySelectorAll('.et-drag-over').forEach(item => item.classList.remove('et-drag-over'));
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
  const pin = event.target.closest('[data-exam-pin]');
  if (pin) { toggleExamPin(pin.dataset.examPin); return; }
  const move = event.target.closest('[data-exam-move]');
  if (!move || _builderBusy || pinForCourse(move.dataset.examMove)) return;
  openExamMoveDialog(move.dataset.examMove);
});
function openExamMoveDialog(code) {
  const entry = _currentResultData?.schedule?.find(item => item.course_code === code);
  if (!entry || _builderBusy || pinForCourse(code) || !currentExamIsSelected(entry)) return;
  _moveCourseCode = entry.course_code;
  $('examMoveTitle').textContent = (IS_AR ? 'نقل الاختبار: ' : 'Move exam: ') + `${entry.course_code} — ${entry.course_name || entry.course_code}`;
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
$('confirmExamMove')?.addEventListener('click', () => {
  const day = $('examMoveDay').value;
  const period = $('examMovePeriod').value;
  if (!day || !period) {
    (!day ? $('examMoveDay') : $('examMovePeriod')).focus();
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

    if (!runs.length) {
      $('historyList').innerHTML = `<small class="text-secondary">${T.noHistory}</small>`;
      $('historyPagination').classList.add('d-none');
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
  } catch (err) {
    if (request === _historyRequest) showExamRequestError(err, 'history');
  } finally {
    if (request === _historyRequest) $('historyList').removeAttribute('aria-busy');
  }
}

function renderHistoryPagination(pages) {
  const wrap = $('historyPages');
  let html = `<button type="button" class="pg-btn" data-history-page="${_historyPage-1}" aria-label="${IS_AR ? 'الصفحة السابقة' : 'Previous page'}" ${_historyPage<=1?'disabled':''}>‹</button>`;
  for (let i = 1; i <= pages; i++) {
    if (pages > 7 && i > 2 && i < pages - 1 && Math.abs(i - _historyPage) > 1) {
      if (i === 3 || i === pages - 2) html += '<span class="et-pagination-ellipsis">…</span>';
      continue;
    }
    html += `<button type="button" class="pg-btn ${i===_historyPage?'active':''}" data-history-page="${i}" aria-label="${IS_AR ? 'صفحة' : 'Page'} ${i}"${i === _historyPage ? ' aria-current="page"' : ''}>${i}</button>`;
  }
  html += `<button type="button" class="pg-btn" data-history-page="${_historyPage+1}" aria-label="${IS_AR ? 'الصفحة التالية' : 'Next page'}" ${_historyPage>=pages?'disabled':''}>›</button>`;
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

    // If the deleted run was the currently loaded one, hide the results panel.
    // We hide (not clear innerHTML) so the DOM elements remain for the next build.
    if (String(_currentRunId) === String(runId)) {
      enterFreshBuildMode();
      $('etStatus').textContent = T.ready;
      $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';
      const exportBtn = $('exportXlsx');
      if (exportBtn) { exportBtn.classList.add('d-none'); exportBtn.removeAttribute('href'); }
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
    else (trigger?.isConnected ? trigger : $('examHistorySummary')).focus({ preventScroll: true });
    // A queued live check may have reached its timer while the dialog was open.
    scheduleDraftCheck();
  }
}

window.addEventListener('beforeunload', event => {
  if (!hasUnsavedEdits()) return;
  event.preventDefault();
  event.returnValue = '';
});

async function loadRun(runId) {
  if (_builderBusy) return;
  if (!await confirmDiscardDraft() || _builderBusy) return;
  cancelDraftChecks();
  setBuilderBusy(true);
  $('etStatus').textContent = T.loadingRun;
  $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';

  let navigateToResult = false;
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
    $('etStatus').textContent = T.done;
    $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';

    // Highlight active
    $('historyList').querySelectorAll('.et-history-item').forEach(el => {
      el.classList.toggle('active', el.dataset.id === String(runId));
    });
  } catch (err) {
    $('etStatus').textContent = T.error + ': ' + showExamRequestError(err, 'load');
    $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
  } finally {
    setBuilderBusy(false);
    if (navigateToResult) focusExamEditor();
  }
}

// Load history on page load
loadHistory();

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
