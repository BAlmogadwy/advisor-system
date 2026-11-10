/* ── Section Planning — client-side logic ── */
const IS_AR = document.documentElement.lang === 'ar';
const T = {
  generating:  IS_AR ? 'جارٍ الحساب...' : 'Generating...',
  done:        IS_AR ? 'تم حساب خطة الشعب بنجاح.' : 'Section plan generated successfully.',
  error:       IS_AR ? 'خطأ' : 'Error',
  fillAll:     IS_AR ? 'يرجى إدخال السنة والفصل.' : 'Please enter Year and Semester.',
  reqFailed:   IS_AR ? 'فشل الطلب' : 'Request failed',
  exporting:   IS_AR ? 'جارٍ التصدير...' : 'Exporting...',
  exported:    IS_AR ? 'تم تصدير الملف.' : 'File exported successfully.',
  full:        IS_AR ? 'ممتلئ' : 'Full',
  underfilled: IS_AR ? 'ناقص' : 'Underfilled',
  lastUpdate:  IS_AR ? 'آخر تحديث' : 'Last update',
  deptSummary: IS_AR ? 'ملخص الأقسام' : 'Department Summary',
  noRecs:      IS_AR ? 'لا توجد توصيات.' : 'No recommendations found.',
  emptyScope:  IS_AR ? 'حدد السنة والفصل والشطر ثم انقر حساب.' : 'Set Year, Semester & Section, then click Generate.',
  allProgs:    IS_AR ? 'كل البرامج' : 'All programmes',
  /* "1448 T1": the year, then the term. */
  term:        IS_AR ? 'ف' : 'T',
  /* The section a plan is for: a Room's and a student's section are M or F. */
  sections:    { M: IS_AR ? 'طلاب' : 'Male', F: IS_AR ? 'طالبات' : 'Female' },
  chooseSection: IS_AR ? 'اختر شطر الطلاب (M) أو الطالبات (F).' : 'Choose Male (M) or Female (F).',
  badSection:  IS_AR ? 'الشطر غير صالح: اختر شطر الطلاب (M) أو الطالبات (F).' : 'That section is not valid: choose Male (M) or Female (F).',
  /* A resolved elective fills a slot: "AI463 ← AI1". In a right-to-left line
   * the arrow is mirrored so it still points from the slot to the course. */
  fills:       IS_AR ? '→' : '←',
  fillsTitle:  slot => IS_AR ? `يملأ الخانة الاختيارية ${slot}` : `Fills elective slot ${slot}`,
  source: {
    rule:      () => IS_AR ? 'القاعدة' : 'rule',
    programme: () => IS_AR ? 'محفوظ' : 'saved',
    slot:      slots => IS_AR ? `حد ${slots}` : `${slots} limit`,
    draft:     () => IS_AR ? 'مسودة' : 'draft',
  },
  dropReason: {
    not_published:      IS_AR ? 'غير منشورة لهذا الفصل' : 'not published for this term',
    invalid_mapping:    IS_AR ? 'ربطها غير صالح' : 'its mapping is invalid',
    no_eligible_course: IS_AR ? 'لا مقرر مؤهَّل له الطالب' : 'no course the student is eligible for',
    no_term_scope:      IS_AR ? 'بلا فصل لتحديدها' : 'no term to resolve it in',
  },
  dropped: (n, parts) => IS_AR
    ? `${n} طلب لخانة اختيارية لم يصبح مقرراً وليس في الخطة: ${parts}.`
    : `${n} elective-slot request${n === 1 ? '' : 's'} became no course and ${n === 1 ? 'is' : 'are'} not in the plan: ${parts}.`,
  slotUses:    IS_AR ? 'بحد هذه الخانة' : "uses this slot's limit",
  slotNone:    status => IS_AR ? `لا مقرر لهذا الفصل (${status})` : `no course this term (${status})`,
};

/* Counts in words: the noun agrees with its number, through the CLDR plural
 * rules (English one/other; Arabic zero/one/two/few/many/other), never
 * "1 courses". In Arabic text the number is a left-to-right island. */
const PLURAL = new Intl.PluralRules(IS_AR ? 'ar' : 'en');
const NOUNS = {
  course:  IS_AR ? { zero: 'مقرر', one: 'مقرر', two: 'مقرران', few: 'مقررات', many: 'مقرراً', other: 'مقرر' }
                 : { one: 'course', other: 'courses' },
  section: IS_AR ? { zero: 'شعبة', one: 'شعبة', two: 'شعبتان', few: 'شعب', many: 'شعبة', other: 'شعبة' }
                 : { one: 'section', other: 'sections' },
  student: IS_AR ? { zero: 'طالب', one: 'طالب', two: 'طالبان', few: 'طلاب', many: 'طالباً', other: 'طالب' }
                 : { one: 'student', other: 'students' },
};
function nounFor(n, noun) {
  const forms = NOUNS[noun];
  return forms[PLURAL.select(n)] || forms.other;
}
function countText(n, noun) {
  const value = Number(n) || 0;
  return `${value} ${nounFor(value, noun)}`;
}
function countHtml(n, noun) {
  const value = Number(n) || 0;
  return `<bdi>${value}</bdi> ${esc(nounFor(value, noun))}`;
}

/* Students with no recorded section are in neither plan: said, never pooled.
 * The verb (and in Arabic the pronoun) agrees with the number. */
function noSectionText(n) {
  const one = PLURAL.select(n) === 'one';
  if (!IS_AR) return `${countText(n, 'student')} ${one ? 'has' : 'have'} no recorded section and ${one ? 'is' : 'are'} not in this plan.`;
  const them = { one: 'فهو', two: 'فهما' }[PLURAL.select(n)] || 'فهم';
  return `${countText(n, 'student')} بلا شطر مسجَّل، ${them} خارج هذه الخطة.`;
}

/* A disclosure chevron: CSS points it along the reading direction when
 * closed (right in English, left in Arabic) and down when open. */
const CHEVRON = '<svg class="sp-chev" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

const CSRF = document.querySelector('[name=csrfmiddlewaretoken]')?.value
  || 'djCsrfToken';

const $ = id => document.getElementById(id);

/* ── Toggle capacity settings ── */
$('spToggleCaps').onclick = () => {
  const hidden = $('spCapsWrap').classList.toggle('d-none');
  $('spToggleCaps').setAttribute('aria-expanded', hidden ? 'false' : 'true');
};

/* A course code in plain text (a status line) of an Arabic sentence: a
 * left-to-right island (FSI ... PDI), so a list of codes keeps its order. */
const ltrIsland = code => String.fromCharCode(0x2068) + code + String.fromCharCode(0x2069);

/* ── Per-course seat limits ──
 * A limit belongs to a programme. Editing a row makes a DRAFT: it is used as a
 * what-if on Generate, marked "Modified", and never written until the user
 * presses Save, reviews the exact rows (course · programme · old → new) and
 * confirms. Discard drops drafts only; removing a saved limit is its own
 * confirmed action. Nothing here writes on input, change or blur.
 *
 * Drafts live in `_drafts`, by course, apart from the rows: a programme change
 * that hides a course keeps its draft, and the course gets it back when shown.
 * The rows on screen belong to `_advProgram`, which changes only once the new
 * programme's rows are rendered; while a list is loading, or after it failed,
 * nothing can be saved, so a save always names the programmes whose rows are
 * on screen. */
const LIMIT_MIN = 1, LIMIT_MAX = 500;
const LIMITS_URL = '/ops/section-planning/limits/';
let _advCourses = [];     // cached course list from server
let _advLoaded = false;   // rendered at least once?
let _advProgram = '';     // the programme(s) whose rows are on screen
let _advSeq = 0;          // the newest course-list request; older answers are dropped
let _advState = 'idle';   // 'idle' | 'loading' | 'ready' | 'failed'
let _limitBusy = false;   // a review or save in flight: one at a time
const _drafts = new Map(); // course code -> { raw, all, typed, value }
let _scopeCleared = [];    // rows whose "All programmes" a reload cleared: the limit shown changed

const TL = {
  modified:     IS_AR ? 'معدَّل' : 'Modified',
  invalid:      IS_AR ? `رقم صحيح من ${LIMIT_MIN} إلى ${LIMIT_MAX}` : `Whole number ${LIMIT_MIN}–${LIMIT_MAX}`,
  mixed:        IS_AR ? 'مختلف' : 'mixed',
  lowest:       IS_AR ? 'أدنى حد معلن' : 'lowest declared',
  allProgs:     IS_AR ? 'كل البرامج' : 'All programmes',
  allAria:      code => IS_AR ? `تطبيق حد ${code} على كل البرامج التي تدرّسه` : `Apply the ${code} limit to every programme that teaches it`,
  limitAria:    code => IS_AR ? `الحد الأقصى لشعبة ${code}` : `Seat limit for ${code}`,
  remove:       IS_AR ? 'إزالة' : 'Remove',
  removeAria:   code => IS_AR ? `إزالة الحد المحفوظ لـ ${code}` : `Remove the saved limit for ${code}`,
  drafts:       n => IS_AR ? `${n} تعديل غير محفوظ` : `${n} unsaved change${n === 1 ? '' : 's'}`,
  whatIfs:      n => IS_AR ? `${n} تعديل للحساب فقط — اختر برنامجاً لحفظه` : `${n} what-if change${n === 1 ? '' : 's'} for Generate — choose a programme to save`,
  toFix:        n => IS_AR ? `${n} قيمة تحتاج تصحيحاً قبل الحفظ` : `${n} value${n === 1 ? '' : 's'} to fix before saving`,
  hidden:       n => IS_AR ? `${n} تعديل غير محفوظ لمقررات غير معروضة` : `${n} unsaved change${n === 1 ? '' : 's'} for courses not on screen`,
  scopeCleared: codes => IS_AR
    ? `${codes.map(ltrIsland).join('، ')}: ${codes.length === 1 ? 'تغيّر الحد المعروض' : 'تغيّرت الحدود المعروضة'} فأُلغي تحديد «كل البرامج»؛ حدّده مرة أخرى لتطبيق الحد المعروض الآن`
    : `${codes.join(', ')}: the limit${codes.length === 1 ? '' : 's'} shown changed, so All programmes was cleared; tick it again to apply the limit shown now`,
  noDrafts:     IS_AR ? 'لا توجد تعديلات غير محفوظة' : 'No unsaved changes',
  needProgram:  IS_AR ? 'اختر برنامجاً أولاً: الحدود تُحفظ لكل برنامج.' : 'Choose a programme first: limits are saved per programme.',
  fixInvalid:   IS_AR ? 'صحّح القيم غير الصالحة قبل الحفظ.' : 'Fix the invalid values before saving.',
  listLoading:  IS_AR ? 'جارٍ تحميل قائمة المقررات…' : 'The course list is loading…',
  listFailed:   IS_AR ? 'لم تُحمَّل قائمة المقررات، فلا يمكن الحفظ. غيّر البرنامج أو حاول مرة أخرى.' : "The course list didn't load, so nothing can be saved. Change the programme or try again.",
  nothing:      IS_AR ? 'لا شيء يتغيّر: الحدود المحفوظة مطابقة.' : 'Nothing to change: the saved limits already match.',
  saveTitle:    n => IS_AR ? `حفظ ${n} من حدود الشعب؟` : `Save ${n} seat limit${n === 1 ? '' : 's'}?`,
  removeTitle:  code => IS_AR ? `إزالة الحد المحفوظ لـ ${code}؟` : `Remove the saved limit for ${code}?`,
  saveIntro:    IS_AR ? 'ستُحفظ هذه الحدود للبرامج المذكورة فقط، ويُسجَّل كل تغيير في سجل التدقيق.' : 'Only the rows below change, for the programmes named. Each change is recorded in the audit log.',
  removeIntro:  IS_AR ? 'بعد الإزالة تُطبَّق القاعدة العامة على هذا المقرر.' : 'After removal the general rule applies to this course.',
  unchanged:    n => IS_AR ? `${n} صف مطابق أصلاً ولن يتغيّر.` : `${n} row${n === 1 ? ' already matches' : 's already match'} and will not change.`,
  thCourse:     IS_AR ? 'المقرر' : 'Course',
  thProgram:    IS_AR ? 'البرنامج' : 'Programme',
  thOld:        IS_AR ? 'المحفوظ الآن' : 'Saved now',
  thNew:        IS_AR ? 'الجديد' : 'New',
  thScope:      IS_AR ? 'النطاق' : 'Scope',
  scopeProgs:   IS_AR ? 'البرامج المعروضة' : 'Programmes on screen',
  scopeAll:     IS_AR ? 'كل البرامج' : 'All programmes',
  none:         IS_AR ? '— (القاعدة)' : '— (rule)',
  removed:      IS_AR ? 'يُزال (القاعدة)' : 'removed (rule)',
  confirmSave:  n => IS_AR ? `حفظ ${n} تغيير` : `Save ${n} change${n === 1 ? '' : 's'}`,
  confirmRemove: IS_AR ? 'إزالة الحد' : 'Remove limit',
  keepEditing:  IS_AR ? 'متابعة التعديل' : 'Keep editing',
  checking:     IS_AR ? 'جارٍ التحقق…' : 'Checking…',
  saving:       IS_AR ? 'جارٍ الحفظ...' : 'Saving...',
  saveBtn:      IS_AR ? 'حفظ الحدود…' : 'Save limits…',
  saved:        n => IS_AR ? `حُفظ ${n} حد وسُجّل في سجل التدقيق.` : `Saved ${n} seat limit${n === 1 ? '' : 's'}; each change is in the audit log.`,
  leave:        IS_AR ? 'لديك حدود غير محفوظة.' : 'You have unsaved seat limits.',
};

/* Server refusals carry a stable code; the words are the page's. */
const TOO_MANY = () => IS_AR ? 'طلبات كثيرة. انتظر قليلاً ثم حاول.' : 'Too many requests. Wait a moment and try again.';
const LIMIT_ERRORS = {
  invalid_json:             () => IS_AR ? 'تعذّر قراءة الطلب.' : 'The request could not be read.',
  programs_required:        () => TL.needProgram,
  unknown_program:          d => IS_AR ? `برنامج غير معروف: ${d.program || ''}` : `Unknown programme: ${d.program || ''}`,
  changes_required:         () => TL.nothing,
  too_many_changes:         d => IS_AR ? `احفظ ${d.max || ''} مقرراً كحد أقصى في المرة الواحدة.` : `Save at most ${d.max || ''} courses at a time.`,
  invalid_course:           () => IS_AR ? 'تغيير بلا رمز مقرر.' : 'A change is missing its course code.',
  duplicate_course:         d => IS_AR ? `${d.course_code || ''} مكرر في الحفظ نفسه.` : `${d.course_code || ''} appears twice in one save.`,
  invalid_limit:            d => IS_AR ? `حد ${d.course_code || ''} يجب أن يكون رقماً صحيحاً من ${LIMIT_MIN} إلى ${LIMIT_MAX}.` : `The limit for ${d.course_code || ''} must be a whole number from ${LIMIT_MIN} to ${LIMIT_MAX}.`,
  invalid_scope:            d => IS_AR ? `نطاق ${d.course_code || ''} غير صالح.` : `The scope for ${d.course_code || ''} is not valid.`,
  course_not_in_programmes: d => IS_AR ? `${d.course_code || ''} لا يُدرَّس في البرامج المعروضة.` : `${d.course_code || ''} is not taught by the programmes on screen.`,
  preview_stale:            () => IS_AR ? 'تغيّرت الحدود المحفوظة منذ المراجعة. اضغط حفظ مرة أخرى لمراجعة القائمة الجديدة.' : 'The saved limits changed since you reviewed them. Press Save again to review the new list.',
  limit_changed:            () => IS_AR ? 'تغيّر حد أثناء الحفظ فلم يُحفظ شيء. اضغط حفظ مرة أخرى.' : 'A limit changed while saving, so nothing was saved. Press Save again.',
  audit_unavailable:        () => IS_AR ? 'تعذّر تسجيل التغيير في سجل التدقيق، لذلك لم يُحفظ شيء. حاول مرة أخرى.' : "Couldn't record the change in the audit log, so nothing was saved. Try again.",
};
/* The saved limits moved under the page: reload them before the next review. */
const STALE_LIMIT_CODES = new Set(['preview_stale', 'limit_changed']);
function limitError(data, status) {
  const make = data && LIMIT_ERRORS[data.code];
  if (make) return make(data);
  if (status === 429) return TOO_MANY();
  if (status === 401 || status === 403) return IS_AR ? 'لا تملك صلاحية حفظ الحدود.' : 'You are not allowed to save limits.';
  return T.reqFailed;
}

/* Generate and Export refusals, in the page's words too. */
const REQUEST_ERRORS = {
  generate_failed:  () => IS_AR ? 'تعذّر حساب الخطة.' : 'The plan could not be computed.',
  export_failed:    () => IS_AR ? 'تعذّر إنشاء الملف.' : 'The file could not be made.',
  invalid_json:     () => LIMIT_ERRORS.invalid_json(),
  invalid_term:     () => IS_AR ? 'السنة أو الفصل غير صالح.' : 'The year or semester is not valid.',
  invalid_capacity: () => IS_AR ? 'حدود الشعب يجب أن تكون أرقاماً صحيحة.' : 'Section limits must be whole numbers.',
  section_required: () => T.chooseSection,
  section_invalid:  () => T.badSection,
};
/* Refusals about the section are said at the Section control, not the status line. */
const SECTION_ERRORS = new Set(['section_required', 'section_invalid']);
function requestError(data, status) {
  const make = data && REQUEST_ERRORS[data.code];
  if (make) return make(data);
  if (status === 429) return TOO_MANY();
  if (status === 401 || status === 403) return IS_AR ? 'لا تملك صلاحية هذا الإجراء.' : 'You are not allowed to do this.';
  return T.reqFailed;
}

$('spToggleAdv').onclick = () => {
  const panel = $('spAdvPanel');
  const isHidden = panel.classList.contains('d-none');
  panel.classList.toggle('d-none');
  $('spToggleAdv').setAttribute('aria-expanded', isHidden ? 'true' : 'false');
  if (isHidden) {
    const prog = $('spProgram').value.trim().toUpperCase();
    if (!_advLoaded || prog !== _advProgram || _advState === 'failed') loadAdvancedCourses();
  }
};

function advPrograms() {
  return _advProgram ? _advProgram.split(',').map(p => p.trim()).filter(Boolean) : [];
}

/* Latin digits whatever the keyboard: Arabic-Indic and Persian 30 are 30. */
function latinDigits(text) {
  return String(text).replace(/[٠-٩]/g, d => String(d.charCodeAt(0) - 0x0660))
                     .replace(/[۰-۹]/g, d => String(d.charCodeAt(0) - 0x06F0));
}

/* The limit a row starts from. With programmes on screen: their saved limit
 * when every programme that teaches the course agrees, "mixed" otherwise.
 * With none: the lowest limit any programme declares, which is what Generate
 * applies to every student (nothing can be saved or removed from there). */
function savedLimitOf(course) {
  if (course.limit_scope === 'lowest_declared') {
    const value = Number.isInteger(course.programme_max) ? course.programme_max : null;
    return { value, mixed: false, any: false, lowest: value != null };
  }
  const limits = course.programme_limits || {};
  const values = Object.values(limits).filter(v => Number.isInteger(v));
  const taught = course.programmes || [];
  if (!values.length) return { value: null, mixed: false, any: false, lowest: false };
  const uniq = [...new Set(values)];
  if (uniq.length === 1 && values.length === taught.length) return { value: uniq[0], mixed: false, any: true, lowest: false };
  return { value: null, mixed: true, any: true, lowest: false };
}

async function loadAdvancedCourses() {
  const seq = ++_advSeq;
  const local4  = parseInt($('spCapLocal4').value, 10) || 25;
  const localO  = parseInt($('spCapLocalOther').value, 10) || 40;
  const ext     = parseInt($('spCapExternal').value, 10) || 50;
  const prog    = $('spProgram').value.trim().toUpperCase();
  let url = `/ops/section-planning/courses/?max_local_4cr=${local4}&max_local_other=${localO}&max_external=${ext}`;
  if (prog) url += `&program=${encodeURIComponent(prog)}`;
  const year = parseInt($('spYear').value, 10), term = parseInt($('spSemester').value, 10);
  if (year && term) url += `&year=${year}&semester=${term}`;
  _advState = 'loading';
  updateAdvState();
  const failed = () => {
    _advState = 'failed';
    showStatus(TL.listFailed, 'err');
    updateAdvState();
  };
  try {
    const res = await fetch(url, { headers: { 'X-CSRFToken': CSRF } });
    const data = await res.json().catch(() => ({}));
    if (seq !== _advSeq) return;   // a newer request owns the panel
    if (!res.ok || !data.ok) { failed(); return; }
    _advCourses = data.courses || [];
    _advProgram = prog;
    _advLoaded = true;
    _advState = 'ready';
    renderAdvancedTable(_advCourses);
    $('spAdvCount').textContent = countText(_advCourses.length, 'course');
  } catch (err) {
    if (seq === _advSeq) failed();
  }
}

function renderAdvancedTable(courses) {
  const tbody = $('spAdvBody');
  _scopeCleared = [];
  if (!courses.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-note text-center" style="padding:20px">${
      IS_AR ? 'لا توجد مقررات — أدخل البرنامج أولاً.' : 'No courses found — enter a Program first.'}</td></tr>`;
    updateAdvState();
    return;
  }
  tbody.innerHTML = courses.map(c => {
    const saved = savedLimitOf(c);
    const code = esc(c.course_code);
    const dispVal = saved.value != null ? saved.value : '';
    const placeholder = saved.mixed ? TL.mixed : c.default_max;
    const lowestNote = saved.lowest ? `<span class="sp-cell-sub sp-adv-src">${esc(TL.lowest)}</span>` : '';
    return `<tr data-code="${code}" data-saved="${saved.value ?? ''}" data-mixed="${saved.mixed ? '1' : ''}">
    <td><span class="cr-id">${code}</span></td>
    <td>${esc(c.department)}</td>
    <td class="text-center">${esc(c.credit_hours)}</td>
    <td class="text-center">${c.is_external ? '✓' : ''}</td>
    <td class="adv-default text-center">${esc(c.default_max)}</td>
    <td class="text-center"><input type="text" inputmode="numeric"
        class="form-control form-control-compact adv-input"
        value="${esc(dispVal)}"
        placeholder="${esc(placeholder)}"
        aria-label="${esc(TL.limitAria(c.course_code))}"
        data-code="${code}" data-default="${esc(c.default_max)}">${lowestNote}
      <span class="sp-adv-state" aria-live="polite"></span></td>
    <td class="text-center"><label class="sp-adv-all"><input type="checkbox" class="adv-all"
        aria-label="${esc(TL.allAria(c.course_code))}"><span aria-hidden="true">${esc(TL.allProgs)}</span></label></td>
    <td class="text-center">${saved.any
      ? `<button type="button" class="sp-adv-reset adv-remove" aria-label="${esc(TL.removeAria(c.course_code))}">${esc(TL.remove)}</button>`
      : ''}</td>
  </tr>${slotElectiveRows(c)}`;
  }).join('');
  /* Drafts only: inputs mark the row, nothing is sent until Save. A reload
   * (after a refused save, or another programme) gives a draft back its
   * value only if the user typed it. A row changed only in scope ("All
   * programmes" ticked on the limit it showed) is kept only while the row
   * still shows that limit: a limit someone else saved in the meantime is
   * never turned back, and a ticked box always means a pending change. When
   * the limit shown changed (or is gone, or mixed), the box is cleared and the
   * panel says so, for the user to tick it again on the value now shown. */
  tbody.querySelectorAll('tr[data-code]').forEach(tr => {
    const inp = tr.querySelector('.adv-input');
    const all = tr.querySelector('.adv-all');
    const prior = _drafts.get(tr.dataset.code);
    if (prior?.typed) {
      inp.value = prior.raw;
      all.checked = prior.all;
    } else if (prior?.all) {
      if (draftOf(tr).value === prior.value) all.checked = true;
      else _scopeCleared.push(tr.dataset.code);
    }
    /* The user acts on the row again: the note about it has been read. */
    const edited = () => {
      _scopeCleared = _scopeCleared.filter(code => code !== tr.dataset.code);
      refreshAdvRow(tr); rememberDraft(tr); updateAdvState();
    };
    inp.addEventListener('input', edited);
    inp.addEventListener('blur', () => {
      /* An emptied saved limit is not a removal: that has its own button. */
      if (!inp.value.trim() && tr.dataset.saved !== '') inp.value = tr.dataset.saved;
      refreshAdvRow(tr); rememberDraft(tr); updateAdvState();
    });
    all.addEventListener('change', edited);
    tr.querySelector('.adv-remove')?.addEventListener('click', () => removeSavedLimit(tr));
    refreshAdvRow(tr);
    rememberDraft(tr);
  });
  updateAdvState();
}

/* Under an elective slot, the courses it resolves to this term ("AI463 ← AI1"). */
function slotElectiveRows(course) {
  return (course.slot_electives || []).map(entry => {
    const prog = (course.programmes || []).length > 1 ? ` <span class="sp-pill sp-pill-ext">${esc(entry.program)}</span>` : '';
    const text = entry.status === 'ready' && entry.courses.length
      ? entry.courses.map(code => `<bdi class="cr-id">${esc(code)}</bdi> ${T.fills} <bdi>${esc(course.course_code)}</bdi>`).join(' · ')
        + ` <span class="text-t3">${esc(T.slotUses)}</span>`
      : `<span class="text-t3">${esc(T.slotNone(entry.status))}</span>`;
    return `<tr class="sp-adv-sub" data-slot-of="${esc(course.course_code)}" data-electives="${esc((entry.courses || []).join(' '))}">
      <td colspan="8">${text}${prog}</td></tr>`;
  }).join('');
}

/* What one row asks for, read from its fields. */
function draftOf(tr) {
  const inp = tr.querySelector('.adv-input');
  const all = Boolean(tr.querySelector('.adv-all')?.checked);
  const raw = latinDigits(inp.value.trim());
  const saved = tr.dataset.saved === '' ? null : Number(tr.dataset.saved);
  const mixed = tr.dataset.mixed === '1';
  const base = { code: tr.dataset.code, raw: inp.value, all, value: null, invalid: false, changed: false, draft: false };
  if (!raw) return base;
  const value = /^\d+$/.test(raw) ? Number(raw) : NaN;
  if (!Number.isInteger(value) || value < LIMIT_MIN || value > LIMIT_MAX) return { ...base, invalid: true };
  const changed = mixed || value !== saved;
  return { ...base, value, changed, draft: changed || all };
}

/* Keep a row's draft (or forget it once it matches the saved limit again).
 * `typed`: the value is the user's own (a change, or one still to fix), not
 * the saved limit the row was pre-filled with. `value`: the limit the row
 * showed, which an untyped ("All programmes" only) draft applies. */
function rememberDraft(tr) {
  const d = draftOf(tr);
  if (d.draft || d.invalid) _drafts.set(d.code, { raw: d.raw, all: d.all, typed: d.changed || d.invalid, value: d.value });
  else _drafts.delete(d.code);
}

function advRows() { return [...$('spAdvBody').querySelectorAll('tr[data-code]')]; }
function advRow(code) { return advRows().find(tr => tr.dataset.code === code) || null; }
function currentDrafts() { return advRows().map(draftOf).filter(d => d.draft && !d.invalid); }
function invalidDrafts() { return advRows().map(draftOf).filter(d => d.invalid); }
function hiddenDraftCount() {
  const shown = new Set(advRows().map(tr => tr.dataset.code));
  return [..._drafts.keys()].filter(code => !shown.has(code)).length;
}

function refreshAdvRow(tr) {
  const d = draftOf(tr);
  const state = tr.querySelector('.sp-adv-state');
  tr.classList.toggle('sp-adv-draft', d.draft && !d.invalid);
  tr.classList.toggle('sp-adv-invalid', d.invalid);
  tr.querySelector('.adv-input').setAttribute('aria-invalid', d.invalid ? 'true' : 'false');
  state.textContent = d.invalid ? TL.invalid : (d.draft ? TL.modified : '');
}

/* Why Save cannot run now ('' when it can, given drafts). */
function saveBlockReason(invalid) {
  if (_advState === 'loading') return TL.listLoading;
  if (_advState === 'failed') return TL.listFailed;
  if (!advPrograms().length) return TL.needProgram;
  if (invalid.length) return TL.fixInvalid;
  return '';
}

/* The panel's status line says what is pending and, when Save is off, why:
 * Save stays focusable (aria-disabled), and describes itself with this line. */
function updateAdvState() {
  const drafts = currentDrafts();
  const invalid = invalidDrafts();
  const hidden = hiddenDraftCount();
  const scoped = advPrograms().length > 0;
  const badge = $('spAdvBadge');
  const pending = drafts.length + hidden;
  if (pending > 0) {
    badge.textContent = pending;
    badge.classList.remove('d-none');
  } else {
    badge.classList.add('d-none');
  }
  const reason = saveBlockReason(invalid);
  const parts = [];
  if (drafts.length) parts.push(scoped || _advState !== 'ready' ? TL.drafts(drafts.length) : TL.whatIfs(drafts.length));
  if (invalid.length) parts.push(TL.toFix(invalid.length));
  if (hidden) parts.push(TL.hidden(hidden));
  if (_scopeCleared.length) parts.push(TL.scopeCleared(_scopeCleared));
  if (drafts.length && (reason === TL.listLoading || reason === TL.listFailed)) parts.push(reason);
  $('spAdvDrafts').textContent = parts.length ? parts.join(' · ') : TL.noDrafts;
  const btn = $('spAdvSaveDb');
  btn.setAttribute('aria-disabled', _limitBusy || reason || !drafts.length ? 'true' : 'false');
  btn.title = reason;
  $('spAdvReset').disabled = _limitBusy || (!drafts.length && !invalid.length && !hidden);
}

/* What-if for Generate: only values the user CHANGED, never pre-filled saved ones. */
function collectOverrides() {
  const overrides = {};
  currentDrafts().filter(d => d.changed).forEach(d => { overrides[d.code] = d.value; });
  return overrides;
}

/* Search filter for advanced table */
$('spAdvSearch').addEventListener('input', function() {
  const q = this.value.trim().toUpperCase();
  $('spAdvBody').querySelectorAll('tr[data-code], tr[data-slot-of]').forEach(tr => {
    const code = [tr.dataset.code, tr.dataset.slotOf, tr.dataset.electives].filter(Boolean).join(' ');
    tr.style.display = (!q || code.toUpperCase().includes(q)) ? '' : 'none';
  });
});

/* Discard drafts, on screen or not: back to the saved values. Never a request. */
function discardDrafts() {
  _drafts.clear();
  _scopeCleared = [];
  advRows().forEach(tr => {
    tr.querySelector('.adv-input').value = tr.dataset.saved;
    tr.querySelector('.adv-all').checked = false;
    refreshAdvRow(tr);
  });
  updateAdvState();
}
$('spAdvReset').onclick = () => {
  discardDrafts();
  /* The button is now off: keep the keyboard in the panel. */
  $('spAdvSearch').focus();
};

/* A JSON POST to one of the panels' save endpoints: the answer and its body. */
async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { res, data };
}

function postLimits(body) {
  return postJson(LIMITS_URL, body);
}

function limitTableHtml(changes) {
  const val = (v, isNew) => v == null ? (isNew ? TL.removed : TL.none) : String(v);
  const rows = changes.map(c => `<tr>
      <td><bdi class="cr-id">${esc(c.course_code)}</bdi>${c.course_name ? `<div class="sp-cell-sub"><bdi>${esc(c.course_name)}</bdi></div>` : ''}</td>
      <td><bdi>${esc(c.program)}</bdi></td>
      <td class="text-center">${esc(val(c.old, false))}</td>
      <td class="text-center"><strong>${esc(val(c.new, true))}</strong></td>
      <td>${esc(c.scope === 'all_programmes' ? TL.scopeAll : TL.scopeProgs)}</td>
    </tr>`).join('');
  return `<table class="sp-adv-table sp-limit-review">
      <thead><tr><th>${esc(TL.thCourse)}</th><th>${esc(TL.thProgram)}</th><th>${esc(TL.thOld)}</th><th>${esc(TL.thNew)}</th><th>${esc(TL.thScope)}</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
}

/* Preview, confirm, commit. Says what happened:
 *   saved  – the server's answer to the commit
 *   noop   – the preview changed nothing (the saved limits already match)
 *   reload – the saved limits on screen may be out of date: reload them */
async function reviewAndSave(changes, { title, intro, confirmText, onCommit }) {
  const programs = advPrograms();
  if (!programs.length) { showStatus(TL.needProgram, 'warn'); return {}; }
  const preview = await postLimits({ programs, changes, dry_run: true });
  if (!preview.res.ok || !preview.data.ok) {
    showStatus(limitError(preview.data, preview.res.status), 'err');
    return { reload: STALE_LIMIT_CODES.has(preview.data.code) };
  }
  const planned = preview.data.changes || [];
  if (!planned.length) { showStatus(TL.nothing, 'warn'); return { noop: true, reload: true }; }
  const unchangedNote = preview.data.unchanged ? `<p class="fs-sm text-t3">${esc(TL.unchanged(preview.data.unchanged))}</p>` : '';
  const ok = await dlg.confirm({
    title: title(planned.length),
    body: `<p>${esc(intro)}</p>${limitTableHtml(planned)}${unchangedNote}`,
    kind: 'warning',
    confirmText: confirmText(planned.length),
    cancelText: TL.keepEditing,
    /* A review: the keyboard starts on "Keep editing", never on the write. */
    initialFocus: 'cancel',
    waitForClose: true,
  });
  if (!ok) return {};
  if (onCommit) onCommit();
  const commit = await postLimits({ programs, changes, dry_run: false, preview_token: preview.data.preview_token });
  if (!commit.res.ok || !commit.data.ok) {
    showStatus(limitError(commit.data, commit.res.status), 'err');
    return { reload: STALE_LIMIT_CODES.has(commit.data.code) };
  }
  return { saved: commit.data, reload: true };
}

/* After the panel is reloaded its rows are new: put the keyboard back. */
function focusAfterLimits(code = null) {
  const field = code && advRow(code)?.querySelector('.adv-input');
  if (field) { field.focus(); return; }
  const btn = $('spAdvSaveDb');
  (btn.getAttribute('aria-disabled') === 'true' ? $('spAdvSearch') : btn).focus();
}

/* Save drafts (the confirmed, audited write). */
$('spAdvSaveDb').onclick = async () => {
  if (_limitBusy) return;
  const invalid = invalidDrafts();
  const reason = saveBlockReason(invalid);
  if (reason) { showStatus(reason, invalid.length ? 'err' : 'warn'); return; }
  const drafts = currentDrafts();
  if (!drafts.length) { showStatus(TL.nothing, 'warn'); return; }
  const btn = $('spAdvSaveDb');
  /* Not `disabled`: the focused button would drop the keyboard to the page. */
  _limitBusy = true;
  btn.setAttribute('aria-busy', 'true');
  btn.textContent = TL.checking;
  updateAdvState();
  let outcome = {};
  try {
    const changes = drafts.map(d => ({ course_code: d.code, max_capacity: d.value, all_programmes: d.all }));
    outcome = await reviewAndSave(changes, {
      title: TL.saveTitle, intro: TL.saveIntro, confirmText: TL.confirmSave,
      onCommit: () => { btn.textContent = TL.saving; },
    });
    if (outcome.saved) showStatus(TL.saved(outcome.saved.changed_count), 'ok');
    /* Saved, or nothing to save: those drafts are done with. */
    if (outcome.saved || outcome.noop) changes.forEach(c => _drafts.delete(c.course_code));
    if (outcome.reload) await loadAdvancedCourses();
  } catch (e) {
    showStatus(T.reqFailed + ': ' + e.message, 'err');
  } finally {
    _limitBusy = false;
    btn.removeAttribute('aria-busy');
    btn.textContent = TL.saveBtn;
    updateAdvState();
    if (outcome.reload) focusAfterLimits();
  }
};

/* Remove one saved limit: explicit, confirmed, audited. Other drafts are kept. */
async function removeSavedLimit(tr) {
  if (_limitBusy) return;
  const reason = saveBlockReason([]);
  if (reason) { showStatus(reason, 'warn'); return; }
  const code = tr.dataset.code;
  const all = Boolean(tr.querySelector('.adv-all')?.checked);
  _limitBusy = true;
  updateAdvState();
  let outcome = {};
  try {
    outcome = await reviewAndSave(
      [{ course_code: code, max_capacity: null, all_programmes: all }],
      { title: () => TL.removeTitle(code), intro: TL.removeIntro, confirmText: () => TL.confirmRemove },
    );
    if (outcome.saved) {
      showStatus(TL.saved(outcome.saved.changed_count), 'ok');
      _drafts.delete(code);
    }
    if (outcome.reload) await loadAdvancedCourses();
  } catch (e) {
    showStatus(T.reqFailed + ': ' + e.message, 'err');
  } finally {
    _limitBusy = false;
    updateAdvState();
    if (outcome.reload) focusAfterLimits(code);
  }
}

window.addEventListener('beforeunload', e => {
  /* What-ifs with no programme on screen cannot be saved: nothing to lose. */
  const unsaved = (advPrograms().length ? currentDrafts().length : 0) + hiddenDraftCount();
  if (!unsaved) return;
  e.preventDefault();
  e.returnValue = TL.leave;
});

/* Reload defaults when global capacity settings change (drafts are kept) */
['spCapLocal4', 'spCapLocalOther', 'spCapExternal'].forEach(id => {
  $(id).addEventListener('change', () => {
    if (_advState !== 'idle') loadAdvancedCourses();
  });
});

/* Reload when program input changes (courses are program-specific; drafts are kept) */
$('spProgram').addEventListener('change', () => {
  if (_advState !== 'idle') loadAdvancedCourses();
});

/* ── A programme's weekly meetings (its 3-credit courses) ──
 * How each 3-credit course of a programme meets each week: not set (the
 * programme leaves it to the others), Standard (2 × 75 min) or 2 × 100 min.
 * It is saved on the programme's study plan, and a timetable GENERATED from
 * then on copies what its programmes agree on; saved scenarios keep their own.
 * As with the seat limits, a choice is a DRAFT until Save, a review (course ·
 * programme · now → new) and a confirm; nothing is sent on change.
 *
 * It is no what-if: this page sizes sections and never reads weekly meetings,
 * so these drafts never reach getPayload(). A row is a course IDENTITY
 * (CODE::NAME, `data-key`), never a bare code: MATH243 is two courses. Drafts
 * belong to the programmes on screen (`_meetProgram`, which changes only once
 * the new programmes' rows are rendered): when the programmes change they are
 * dropped, and the panel says so, so a choice is never saved for programmes it
 * was not made for. The panel asks the server only once it is opened, and
 * never with no programme on screen. The words of the three choices, and the
 * minutes in them, are the server's. */
const MEET_URL = '/ops/section-planning/meeting-patterns/';
const MEET_SAVE_URL = '/ops/section-planning/meeting-patterns/save/';
/* A mixed row's own option: its programmes keep their different values. Never sent. */
const MEET_KEEP = 'keep';
let _meetChoices = [];        // the three votes in the server's words
let _meetProgram = '';        // the programmes whose rows are on screen ('' none)
let _meetSeq = 0;             // the newest list request; older answers are dropped
let _meetState = 'idle';      // 'idle' | 'loading' | 'ready' | 'failed' | 'none'
let _meetBusy = false;        // a review or save in flight: one at a time
const _meetDrafts = new Map(); // course key -> { value, all, typed, saved }
let _meetDropNote = '';       // drafts a programme change dropped, said until the next edit
let _meetReloadNotes = [];    // what the last reload did to drafts, said until the next edit
let _meetFailWords = '';      // why the list failed when trying again cannot help ('' otherwise)

const TM = {
  chooseProgram: IS_AR ? 'اختر برنامجاً أولاً: اللقاءات الأسبوعية تُحدَّد لكل برنامج.' : 'Choose a programme first: weekly meetings are set per programme.',
  noCourses:     IS_AR ? 'لا مقرر من ٣ ساعات في هذه البرامج تُختار له اللقاءات الأسبوعية.' : 'These programmes have no 3-credit course whose weekly meetings can be chosen.',
  listLoading:   IS_AR ? 'جارٍ تحميل اللقاءات الأسبوعية…' : 'The weekly meetings are loading…',
  listFailed:    IS_AR ? 'لم تُحمَّل اللقاءات الأسبوعية، فلا يمكن الحفظ. غيّر البرنامج أو حاول مرة أخرى.' : "The weekly meetings didn't load, so nothing can be saved. Change the programme or try again.",
  listNotAllowed: IS_AR ? 'لا تملك صلاحية عرض اللقاءات الأسبوعية أو حفظها.' : 'You are not allowed to see or save weekly meetings.',
  mixed:         IS_AR ? 'البرامج مختلفة' : 'Programmes differ',
  partly:        IS_AR ? 'محدد لبعض البرامج فقط' : 'Set for some programmes only',
  selectAria:    label => IS_AR ? `اللقاءات الأسبوعية لـ ${label}` : `Weekly meetings for ${label}`,
  allAria:       (label, others) => IS_AR ? `تطبيق اختيار ${label} على كل البرامج التي تدرّسه، ومنها: ${others}` : `Apply the ${label} choice to every programme that teaches it, also: ${others}`,
  alsoTaught:    IS_AR ? 'يدرّسه أيضاً:' : 'Also taught by:',
  clearOnly:     reason => IS_AR ? `لم يعد يُختار له (${reason})، ويمكن مسحه فقط.` : `No longer offered (${reason}); it can only be cleared.`,
  /* The rule counts every programme's row of the course: at different credit
     hours the programmes never agree, whatever they set. */
  creditsDiffer: IS_AR
    ? 'تدرّسه برامجه بساعات معتمدة مختلفة، فتبقى الجداول التي تُنشأ من الآن على لقاءاته الأسبوعية القياسية.'
    : 'Its programmes teach it at different credit hours, so timetables generated from now on keep its standard weekly meetings.',
  /* Why a row may not take a value (programme_meeting_patterns.ineligibility). */
  reasons: {
    not_three_credits: IS_AR ? 'ليس من ٣ ساعات' : 'not 3 credits',
    not_named:         IS_AR ? 'بلا اسم مقرر في خطة الدراسة' : 'no course name in the study plan',
    placeholder:       IS_AR ? 'خانة اختيارية' : 'an elective slot',
    excluded:          IS_AR ? 'لا يدخل الجدول' : 'not on the timetable',
    policy:            IS_AR ? 'مقرر إنجليزي بلقاءات ثابتة' : 'an English course with fixed meetings',
  },
  dropped:       (n, progs) => IS_AR
    ? `أُسقط ${n} تعديل غير محفوظ لـ ${ltrIsland(progs)}: تغيّرت البرامج المعروضة`
    : `${n} unsaved change${n === 1 ? '' : 's'} for ${progs} ${n === 1 ? 'was' : 'were'} dropped: the programmes on screen changed`,
  gone:          n => IS_AR
    ? `أُسقط ${n} تعديل غير محفوظ: لم يعد المقرر يقبل هذا الاختيار لهذه البرامج`
    : `${n} unsaved change${n === 1 ? '' : 's'} dropped: the course no longer takes that choice for these programmes`,
  scopeCleared:  codes => IS_AR
    ? `${codes.map(ltrIsland).join('، ')}: تغيّرت القيمة المحفوظة فأُلغي تحديد «كل البرامج»؛ حدّده مرة أخرى لتطبيق القيمة المعروضة الآن`
    : `${codes.join(', ')}: the saved value changed, so All programmes was cleared; tick it again to apply the value shown now`,
  saveBtn:       IS_AR ? 'حفظ اللقاءات الأسبوعية…' : 'Save weekly meetings…',
  saveTitle:     n => IS_AR ? `حفظ ${n} من اختيارات اللقاءات الأسبوعية؟` : `Save ${n} weekly-meetings choice${n === 1 ? '' : 's'}?`,
  saveIntro:     IS_AR
    ? 'تتغيّر الصفوف أدناه فقط، للبرامج المذكورة، ويُسجَّل كل تغيير في سجل التدقيق. تُطبَّق على الجداول التي تُنشأ من الآن، للطلاب والطالبات؛ وتحتفظ السيناريوهات المحفوظة بلقاءاتها.'
    : 'Only the rows below change, for the programmes named, and each change is recorded in the audit log. It applies to timetables generated from now on, Male and Female alike; saved scenarios keep their own.',
  saved:         n => IS_AR ? `حُفظ ${n} من اختيارات اللقاءات الأسبوعية وسُجّل في سجل التدقيق.` : `Saved ${n} weekly-meetings choice${n === 1 ? '' : 's'}; each change is in the audit log.`,
  nothing:       IS_AR ? 'لا شيء يتغيّر: القيم المحفوظة مطابقة.' : 'Nothing to change: the saved values already match.',
  skippedCount:  n => IS_AR ? `${n} صف لا يقبل هذا الاختيار.` : `${n} row${n === 1 ? '' : 's'} cannot take that choice.`,
  skippedHead:   IS_AR ? 'لا يُطبَّق على:' : 'Not set on:',
  coOwners:      IS_AR ? 'يدرّسه أيضاً (يحتفظ كل برنامج بقيمته):' : 'Also taught by (each keeps its own value):',
  otherName:     IS_AR ? 'مخطط باسم آخر — حدّد صفه الخاص.' : 'is planned under another name — set its own row.',
  scenariosHead: IS_AR ? 'جداول مسودة محفوظة تدرّسه (تحتفظ بلقاءاتها الأسبوعية):' : 'Saved draft timetables that teach it (they keep their own weekly meetings):',
  scenarioDiffers: (now, wanted) => IS_AR
    ? `الآن ${now}، وافتراضي البرامج ${wanted}: افتحه واضغط «استخدام افتراضي البرامج»`
    : `now ${now}; the programme default is ${wanted}: open it and press “Use programme defaults”`,
  scenarioMatches: IS_AR ? 'مطابق لافتراضي البرامج' : 'already matches the programme default',
  scenarioMixed:   IS_AR ? 'برامجه مختلفة: يبقى كما هو' : 'its programmes differ: it stays as it is',
  scenarioNone:    IS_AR ? 'لا قيمة لبرامجه: يبقى كما هو' : 'no programme default there: it stays as it is',
  scenariosMore:   n => IS_AR ? `وجداول مسودة أخرى عددها ${n} لم تُذكر.` : `and ${n} more draft timetable${n === 1 ? '' : 's'}, not listed.`,
  notAllowed:    IS_AR ? 'لا تملك صلاحية حفظ اللقاءات الأسبوعية.' : 'You are not allowed to save weekly meetings.',
  leave:         IS_AR ? 'لديك اختيارات لقاءات أسبوعية غير محفوظة.' : 'You have unsaved weekly-meetings choices.',
};

/* A course in a status line: its code, an LTR island in Arabic. */
const courseOf = d => {
  const code = d.course_code || String(d.course_key || '').split('::')[0];
  return IS_AR ? ltrIsland(code) : code;
};
const MEET_ERRORS = {
  invalid_json:             () => LIMIT_ERRORS.invalid_json(),
  programs_required:        () => TM.chooseProgram,
  unknown_program:          d => IS_AR ? `برنامج غير معروف: ${d.program || ''}` : `Unknown programme: ${d.program || ''}`,
  changes_required:         () => TM.nothing,
  too_many_changes:         d => IS_AR ? `احفظ ${d.max || ''} مقرراً كحد أقصى في المرة الواحدة.` : `Save at most ${d.max || ''} courses at a time.`,
  invalid_course:           () => IS_AR ? 'تغيير بلا مقرر.' : 'A change is missing its course.',
  duplicate_course:         d => IS_AR ? `${courseOf(d)} مكرر في الحفظ نفسه.` : `${courseOf(d)} appears twice in one save.`,
  invalid_pattern:          d => IS_AR ? `اختيار اللقاءات الأسبوعية لـ ${courseOf(d)} غير صالح.` : `The weekly meetings chosen for ${courseOf(d)} are not a valid choice.`,
  invalid_scope:            d => IS_AR ? `نطاق ${courseOf(d)} غير صالح.` : `The scope for ${courseOf(d)} is not valid.`,
  course_not_in_programmes: d => IS_AR ? `${courseOf(d)} لا يُدرَّس في البرامج المعروضة.` : `${courseOf(d)} is not taught by the programmes on screen.`,
  preview_stale:            () => IS_AR ? 'تغيّرت اللقاءات الأسبوعية المحفوظة منذ المراجعة. اضغط حفظ مرة أخرى لمراجعة القائمة الجديدة.' : 'The saved weekly meetings changed since you reviewed them. Press Save again to review the new list.',
  row_changed:              () => IS_AR ? 'تغيّر مقرر أثناء الحفظ فلم يُحفظ شيء. اضغط حفظ مرة أخرى.' : 'A course changed while saving, so nothing was saved. Press Save again.',
  audit_unavailable:        () => LIMIT_ERRORS.audit_unavailable(),
};
/* The saved values moved under the page: reload them before the next review. */
const STALE_MEET_CODES = new Set(['preview_stale', 'row_changed']);
function meetError(data, status) {
  const make = data && MEET_ERRORS[data.code];
  if (make) return make(data);
  if (status === 429) return TOO_MANY();
  if (status === 401 || status === 403) return TM.notAllowed;
  return T.reqFailed;
}

/* The programmes typed, as one key: upper-case, each once, sorted ("AI2,DS2"),
 * so "AI2, DS2" and "DS2,AI2" are the same programmes on screen. */
function typedProgrammes() {
  const codes = $('spProgram').value.split(',').map(p => p.trim().toUpperCase()).filter(Boolean);
  return [...new Set(codes)].sort().join(',');
}
function meetPrograms() {
  return _meetProgram ? _meetProgram.split(',') : [];
}

/* One of the three votes in the page's language, in the server's words. */
function choiceLabel(value) {
  const choice = _meetChoices.find(c => c.value === (value || ''));
  if (!choice) return String(value || '');
  return IS_AR ? (choice.label_ar || choice.label) : choice.label;
}
/* "AI2 2 × 100 min · DS2 Not set": each programme with its vote. */
function programmeValues(list) {
  return list.map((p, i) => safeHtml`${i ? ' · ' : ''}<bdi>${p.program}</bdi> ${choiceLabel(p.meeting_pattern)}`);
}
const programmeValuesText = list => list.map(p => `${p.program} ${choiceLabel(p.meeting_pattern)}`).join(IS_AR ? '، ' : ', ');

$('spToggleMeet').onclick = () => {
  const panel = $('spMeetPanel');
  const opening = panel.classList.contains('d-none');
  panel.classList.toggle('d-none');
  $('spToggleMeet').setAttribute('aria-expanded', opening ? 'true' : 'false');
  if (opening && (_meetState === 'idle' || _meetState === 'failed' || typedProgrammes() !== _meetProgram)) {
    loadMeetingPatterns();
  }
};

/* The rows on screen become `prog`'s. Drafts made for other programmes are
 * dropped, and the panel says so: a choice is saved only for the programmes it
 * was made for. */
function adoptMeetProgramme(prog) {
  if (prog === _meetProgram) return;
  const dropped = _meetDrafts.size;
  _meetDropNote = dropped ? TM.dropped(dropped, meetPrograms().join(', ')) : '';
  _meetReloadNotes = [];
  _meetDrafts.clear();
  _meetProgram = prog;
}

function meetEmptyRow(words) {
  return safeHtml`<tr><td colspan="4" class="empty-note text-center">${words}</td></tr>`;
}

async function loadMeetingPatterns() {
  const seq = ++_meetSeq;
  const prog = typedProgrammes();
  if (!prog) {
    adoptMeetProgramme('');
    _meetState = 'none';
    $('spMeetBody').innerHTML = meetEmptyRow(TM.chooseProgram);
    $('spMeetCount').textContent = '';
    updateMeetState();
    return;
  }
  _meetState = 'loading';
  updateMeetState();
  const failed = (data, status) => {
    _meetState = 'failed';
    /* Refused for the user's role: trying again cannot help, so say why. */
    _meetFailWords = status === 401 || status === 403 ? TM.listNotAllowed : '';
    const words = _meetFailWords || (data && MEET_ERRORS[data.code] ? meetError(data) : TM.listFailed);
    if (!meetRows().length) $('spMeetBody').innerHTML = meetEmptyRow(words);
    showStatus(words, 'err');
    updateMeetState();
  };
  try {
    const res = await fetch(`${MEET_URL}?program=${encodeURIComponent(prog)}`, { headers: { 'X-CSRFToken': CSRF } });
    const data = await res.json().catch(() => ({}));
    if (seq !== _meetSeq) return;   // a newer request owns the panel
    if (!res.ok || !data.ok) { failed(data, res.status); return; }
    const rows = Array.isArray(data.courses) ? data.courses : [];
    _meetChoices = Array.isArray(data.choices) ? data.choices : [];
    adoptMeetProgramme(prog);
    _meetState = 'ready';
    renderMeetTable(rows);
    $('spMeetCount').textContent = countText(rows.length, 'course');
  } catch (err) {
    if (seq === _meetSeq) failed(null);
  }
}

/* What the programmes on screen HOLD for a row, which is not what the rule
 * decides: a programme that has not set a value abstains from the rule, yet a
 * row shows one value only when every programme on screen stores that same
 * value ("" not set included). Otherwise the row is drawn as mixed, so any
 * value chosen is a change sent for every programme on screen (the server
 * writes only the rows that differ): with AI2 2 × 100 and DS2 not set,
 * choosing 2 × 100 gives it to DS2 too. `partly`: one value, and programmes
 * that have not set it. */
function meetSavedOf(row) {
  const values = [...new Set((row.programmes || []).map(p => p.meeting_pattern || ''))];
  if (values.length <= 1) return { saved: values.length ? values[0] : (row.meeting_pattern || ''), mixed: false, partly: false };
  return { saved: '', mixed: true, partly: values.filter(Boolean).length === 1 };
}

function meetRowHtml(row) {
  const { saved, mixed, partly } = meetSavedOf(row);
  const label = [row.course_code, row.course_name].filter(Boolean).join(' ');
  const owners = row.co_owners || [];
  const taught = row.programmes || [];
  /* A row that may no longer vote offers only clearing, beside what it holds. */
  const choices = row.clear_only
    ? _meetChoices.filter(c => c.value === '' || taught.some(p => p.meeting_pattern === c.value))
    : _meetChoices;
  return safeHtml`<tr data-key="${row.course_key}" data-course="${row.course_code}" data-saved="${saved}" data-mixed="${mixed ? '1' : ''}">
    <td><bdi class="cr-id">${row.course_code}</bdi>${row.course_name ? safeHtml`<div class="sp-cell-sub"><bdi>${row.course_name}</bdi></div>` : ''}</td>
    <td>${taught.map(p => safeHtml`<div class="sp-meet-prog"><bdi class="fw-semibold">${p.program}</bdi> ${choiceLabel(p.meeting_pattern)}</div>`)}</td>
    <td><select class="form-select form-select-sm meet-select" aria-label="${TM.selectAria(label)}">${mixed ? safeHtml`<option value="${MEET_KEEP}">${partly ? TM.partly : TM.mixed}</option>` : ''}${choices.map(c => safeHtml`<option value="${c.value}">${IS_AR ? (c.label_ar || c.label) : c.label}</option>`)}</select>${row.clear_only ? safeHtml`<div class="fs-sm text-t3 sp-meet-clear-only">${TM.clearOnly(TM.reasons[row.reason] || row.reason || '')}</div>` : ''}${row.credit_hours_differ ? safeHtml`<div class="fs-sm text-t3 sp-meet-credits">${TM.creditsDiffer}</div>` : ''}<span class="sp-adv-state text-t3" aria-live="polite"></span></td>
    <td>${owners.length ? safeHtml`<label class="sp-adv-all"><input type="checkbox" class="meet-all" aria-label="${TM.allAria(label, programmeValuesText(owners))}"><span aria-hidden="true">${TL.allProgs}</span></label><div class="fs-sm text-t3 sp-meet-owners">${TM.alsoTaught} ${programmeValues(owners)}</div>` : ''}</td>
  </tr>`;
}

/* The rows shown, each starting from its saved value. A reload for the same
 * programmes (after a save, or one refused) gives a draft back its value if
 * the user chose it; a row changed only in scope ("All programmes" ticked on
 * the value it showed) keeps it only while the row still shows that value, so
 * a value someone else saved meanwhile is never applied everywhere unasked.
 * A draft the rows can no longer take is dropped. Each is said. */
function renderMeetTable(rows) {
  const tbody = $('spMeetBody');
  tbody.innerHTML = rows.length ? safeHtml`${rows.map(meetRowHtml)}` : meetEmptyRow(TM.noCourses);
  const shown = new Set();
  const cleared = [];
  let gone = 0;
  meetRows().forEach(tr => {
    const key = tr.dataset.key;
    const select = tr.querySelector('.meet-select');
    const all = tr.querySelector('.meet-all');
    shown.add(key);
    select.value = tr.dataset.mixed === '1' ? MEET_KEEP : tr.dataset.saved;
    const prior = _meetDrafts.get(key);
    if (prior?.typed && [...select.options].some(o => o.value === prior.value)) {
      select.value = prior.value;
      if (all) all.checked = prior.all;
    } else if (prior?.all && all && tr.dataset.mixed !== '1' && tr.dataset.saved === prior.saved) {
      all.checked = true;
    } else if (prior) {
      if (prior.typed) gone++;
      else cleared.push(tr.dataset.course);
    }
    const edited = () => {
      _meetDropNote = '';
      _meetReloadNotes = [];
      syncMeetAll(tr); refreshMeetRow(tr); rememberMeetDraft(tr); updateMeetState();
    };
    select.addEventListener('change', edited);
    all?.addEventListener('change', edited);
    syncMeetAll(tr);
    refreshMeetRow(tr);
    rememberMeetDraft(tr);
  });
  for (const key of [..._meetDrafts.keys()]) {
    if (!shown.has(key)) { _meetDrafts.delete(key); gone++; }
  }
  _meetReloadNotes = [];
  if (gone) _meetReloadNotes.push(TM.gone(gone));
  if (cleared.length) _meetReloadNotes.push(TM.scopeCleared(cleared));
  updateMeetState();
}

/* "All programmes" widens a VALUE: on a mixed row it waits until one is chosen. */
function syncMeetAll(tr) {
  const all = tr.querySelector('.meet-all');
  if (!all) return;
  const waiting = tr.querySelector('.meet-select').value === MEET_KEEP;
  if (waiting) all.checked = false;
  all.disabled = waiting;
}

/* What one row asks for. A mixed row asks nothing until a value is chosen. */
function meetDraftOf(tr) {
  const select = tr.querySelector('.meet-select');
  const value = select.value === MEET_KEEP ? null : select.value;
  const all = value !== null && Boolean(tr.querySelector('.meet-all')?.checked);
  const changed = value !== null && (tr.dataset.mixed === '1' || value !== tr.dataset.saved);
  return { key: tr.dataset.key, code: tr.dataset.course, value, all, changed, draft: changed || all };
}

/* Keep a row's draft, or forget it once it asks for nothing. `typed`: the user
 * chose the value; `saved`: the value the row showed (what a scope-only draft
 * applies everywhere). */
function rememberMeetDraft(tr) {
  const d = meetDraftOf(tr);
  if (d.draft) _meetDrafts.set(d.key, { value: d.value, all: d.all, typed: d.changed, saved: tr.dataset.saved });
  else _meetDrafts.delete(d.key);
}

function meetRows() { return [...$('spMeetBody').querySelectorAll('tr[data-key]')]; }
function currentMeetDrafts() { return meetRows().map(meetDraftOf).filter(d => d.draft); }

function refreshMeetRow(tr) {
  const d = meetDraftOf(tr);
  tr.classList.toggle('sp-adv-draft', d.draft);
  tr.querySelector('.sp-adv-state').textContent = d.draft ? TL.modified : '';
}

/* Why Save cannot run now ('' when it can, given drafts). */
function meetBlockReason() {
  if (_meetState === 'loading') return TM.listLoading;
  if (_meetState === 'failed') return _meetFailWords || TM.listFailed;
  if (!_meetProgram) return TM.chooseProgram;
  return '';
}

/* The panel's status line says what is pending, what a reload or a programme
 * change did to drafts, and, when Save is off, why: Save stays focusable
 * (aria-disabled) and describes itself with this line. */
function updateMeetState() {
  const drafts = currentMeetDrafts();
  const badge = $('spMeetBadge');
  badge.textContent = String(drafts.length);
  badge.classList.toggle('d-none', !drafts.length);
  const reason = meetBlockReason();
  const parts = [];
  if (drafts.length) parts.push(TL.drafts(drafts.length));
  if (_meetDropNote) parts.push(_meetDropNote);
  parts.push(..._meetReloadNotes);
  if (drafts.length && (_meetState === 'loading' || _meetState === 'failed')) parts.push(reason);
  $('spMeetDrafts').textContent = parts.length ? parts.join(' · ') : TL.noDrafts;
  const btn = $('spMeetSaveDb');
  btn.setAttribute('aria-disabled', _meetBusy || reason || !drafts.length ? 'true' : 'false');
  btn.title = reason;
  $('spMeetReset').disabled = _meetBusy || !drafts.length;
}

/* Discard drafts: back to the saved values. Never a request. */
function discardMeetDrafts() {
  _meetDrafts.clear();
  _meetDropNote = '';
  _meetReloadNotes = [];
  meetRows().forEach(tr => {
    tr.querySelector('.meet-select').value = tr.dataset.mixed === '1' ? MEET_KEEP : tr.dataset.saved;
    const all = tr.querySelector('.meet-all');
    if (all) all.checked = false;
    syncMeetAll(tr);
    refreshMeetRow(tr);
  });
  updateMeetState();
}
$('spMeetReset').onclick = () => {
  discardMeetDrafts();
  /* The button is now off: keep the keyboard in the panel. */
  (meetRows()[0]?.querySelector('.meet-select') || $('spMeetSaveDb')).focus();
};

const scenarioUrl = id => `/timetable-workspace/?scenario=${encodeURIComponent(id)}`;
/* A saved draft timetable the change reaches: it keeps its own weekly meetings,
 * so the review says whether it now differs from its programmes' default. */
function scenarioWords(s) {
  const now = choiceLabel(s.budget_pattern || 'standard');
  if (s.differs) return TM.scenarioDiffers(now, choiceLabel(s.programme_pattern));
  if (s.status === 'agree') return TM.scenarioMatches;
  if (s.status === 'mixed') return TM.scenarioMixed;
  return TM.scenarioNone;
}

/* What a changed course means elsewhere: the programmes that keep their own
 * value, the course's other names, and the draft timetables it reaches. */
function meetAfterHtml(course) {
  const owners = course.co_owners || [];
  const others = course.same_code_elsewhere || [];
  const scenarios = course.scenarios || [];
  /* The server names the newest drafts it reaches and counts the rest. */
  const more = Number(course.scenarios_more) || 0;
  if (!owners.length && !others.length && !scenarios.length) return '';
  return safeHtml`<div class="sp-meet-after" data-key="${course.course_key}">
    <p class="fs-sm mb-0"><bdi class="cr-id">${course.course_code}</bdi> <bdi>${course.course_name}</bdi></p>
    ${owners.length ? safeHtml`<p class="fs-sm text-t3 mb-0 sp-meet-owners">${TM.coOwners} ${programmeValues(owners)}</p>` : ''}
    ${others.map(o => safeHtml`<p class="fs-sm text-t3 mb-0 sp-meet-other"><bdi class="cr-id">${course.course_code}</bdi> <bdi>${o.course_name}</bdi> (<bdi>${(o.programmes || []).join(', ')}</bdi>) ${TM.otherName}</p>`)}
    ${scenarios.length ? safeHtml`<p class="fs-sm text-t3 mb-0">${TM.scenariosHead}</p>
    <ul class="fs-sm sp-meet-scenarios">${scenarios.map(s => safeHtml`<li data-scenario="${s.id}"><a href="${scenarioUrl(s.id)}"><bdi>${s.name}</bdi></a> <bdi>(#${s.id})</bdi> — ${scenarioWords(s)}</li>`)}</ul>
    ${more ? safeHtml`<p class="fs-sm text-t3 mb-0 sp-meet-scenarios-more">${TM.scenariosMore(more)}</p>` : ''}` : ''}
  </div>`;
}

function meetReviewHtml(data) {
  const changes = data.changes || [];
  const skipped = data.skipped || [];
  return safeHtml`<p>${TM.saveIntro}</p>
    <table class="sp-adv-table sp-limit-review sp-meet-review">
      <thead><tr><th>${TL.thCourse}</th><th>${TL.thProgram}</th><th>${TL.thOld}</th><th>${TL.thNew}</th><th>${TL.thScope}</th></tr></thead>
      <tbody>${changes.map(c => safeHtml`<tr>
        <td><bdi class="cr-id">${c.course_code}</bdi>${c.course_name ? safeHtml`<div class="sp-cell-sub"><bdi>${c.course_name}</bdi></div>` : ''}</td>
        <td><bdi>${c.program}</bdi></td>
        <td>${choiceLabel(c.old)}</td>
        <td><strong>${choiceLabel(c.new)}</strong></td>
        <td>${c.scope === 'all_programmes' ? TL.scopeAll : TL.scopeProgs}</td>
      </tr>`)}</tbody></table>
    ${skipped.length ? safeHtml`<p class="fs-sm text-t3 sp-meet-skipped">${TM.skippedHead} ${skipped.map((s, i) => safeHtml`${i ? ' · ' : ''}<bdi class="cr-id">${s.course_code}</bdi> (<bdi>${s.program}</bdi>): ${TM.reasons[s.reason] || s.reason}`)}</p>` : ''}
    ${data.unchanged ? safeHtml`<p class="fs-sm text-t3">${TL.unchanged(data.unchanged)}</p>` : ''}
    ${(data.courses || []).map(meetAfterHtml)}`;
}

/* Preview, confirm, commit (as the seat limits' review). Says what happened:
 * saved, noop (the preview changed nothing), reload (the rows may be stale). */
async function reviewMeetings(programs, changes, onCommit) {
  const preview = await postJson(MEET_SAVE_URL, { programs, changes, dry_run: true });
  if (!preview.res.ok || !preview.data.ok) {
    showStatus(meetError(preview.data, preview.res.status), 'err');
    return { reload: STALE_MEET_CODES.has(preview.data.code) };
  }
  const planned = preview.data.changes || [];
  if (!planned.length) {
    const skipped = (preview.data.skipped || []).length;
    showStatus(skipped ? `${TM.nothing} ${TM.skippedCount(skipped)}` : TM.nothing, 'warn');
    return { noop: true, reload: true };
  }
  const ok = await dlg.confirm({
    title: TM.saveTitle(planned.length),
    body: String(meetReviewHtml(preview.data)),
    kind: 'warning',
    confirmText: TL.confirmSave(planned.length),
    cancelText: TL.keepEditing,
    /* A review: the keyboard starts on "Keep editing", never on the write. */
    initialFocus: 'cancel',
    waitForClose: true,
  });
  if (!ok) return {};
  onCommit();
  const commit = await postJson(MEET_SAVE_URL, { programs, changes, dry_run: false, preview_token: preview.data.preview_token });
  if (!commit.res.ok || !commit.data.ok) {
    showStatus(meetError(commit.data, commit.res.status), 'err');
    return { reload: STALE_MEET_CODES.has(commit.data.code) };
  }
  return { saved: commit.data, reload: true };
}

/* Save drafts (the confirmed, audited write), for the programmes on screen. */
$('spMeetSaveDb').onclick = async () => {
  if (_meetBusy) return;
  const reason = meetBlockReason();
  if (reason) { showStatus(reason, 'warn'); return; }
  const drafts = currentMeetDrafts();
  if (!drafts.length) { showStatus(TM.nothing, 'warn'); return; }
  const btn = $('spMeetSaveDb');
  /* Not `disabled`: the focused button would drop the keyboard to the page. */
  _meetBusy = true;
  btn.setAttribute('aria-busy', 'true');
  btn.textContent = TL.checking;
  updateMeetState();
  const changes = drafts.map(d => ({ course_key: d.key, meeting_pattern: d.value, all_programmes: d.all }));
  try {
    const outcome = await reviewMeetings(meetPrograms(), changes, () => { btn.textContent = TL.saving; });
    if (outcome.saved) showStatus(TM.saved(outcome.saved.changed_count), 'ok');
    /* Saved, or nothing to save: those drafts are done with. */
    if (outcome.saved || outcome.noop) changes.forEach(c => _meetDrafts.delete(c.course_key));
    if (outcome.reload) await loadMeetingPatterns();
  } catch (e) {
    showStatus(T.reqFailed + ': ' + e.message, 'err');
  } finally {
    _meetBusy = false;
    btn.removeAttribute('aria-busy');
    btn.textContent = TM.saveBtn;
    updateMeetState();
  }
};

window.addEventListener('beforeunload', e => {
  if (!currentMeetDrafts().length) return;
  e.preventDefault();
  e.returnValue = TM.leave;
});

/* The programmes on screen changed: their rows, and drafts only for them. */
$('spProgram').addEventListener('change', () => {
  if (_meetState !== 'idle') loadMeetingPatterns();
});

/* ── Section: one per plan, M or F ──
 * Native radios in the scope form. Nothing is chosen on a first visit; the
 * last choice is remembered in this browser (a preference: Reset keeps it).
 * Storage is a convenience only: the page works when it is missing or throws. */
const SECTION_KEY = 'sectionPlanning.section';
const sectionRadios = () => [...document.querySelectorAll('#spSectionGroup input[name="spSection"]')];
function chosenSection() {
  return sectionRadios().find(radio => radio.checked)?.value || '';
}
function clearSectionMessage() {
  $('spSectionMsg').textContent = '';
  $('spSectionGroup').removeAttribute('aria-invalid');
}
/* Said at the control (an alert), and the keyboard is taken there. */
function showSectionMessage(text) {
  hideStatus();
  $('spSectionGroup').setAttribute('aria-invalid', 'true');
  $('spSectionMsg').textContent = text;
  const radios = sectionRadios();
  (radios.find(radio => radio.checked) || radios[0]).focus();
}
(function rememberSection() {
  let stored = null;
  try { stored = window.localStorage.getItem(SECTION_KEY); } catch (_) { stored = null; }
  const radio = sectionRadios().find(r => r.value === stored);
  if (radio) radio.checked = true;
  sectionRadios().forEach(r => r.addEventListener('change', () => {
    if (!r.checked) return;
    clearSectionMessage();
    try { window.localStorage.setItem(SECTION_KEY, r.value); } catch (_) { /* not remembered: still chosen */ }
  }));
})();

/* ── Collect payload ── */
function getPayload() {
  const overrides = collectOverrides();
  const payload = {
    year:            parseInt($('spYear').value, 10) || 0,
    semester:        parseInt($('spSemester').value, 10) || 0,
    section:         chosenSection(),
    program:         $('spProgram').value.trim().toUpperCase(),
    max_local_4cr:   parseInt($('spCapLocal4').value, 10) || 25,
    max_local_other: parseInt($('spCapLocalOther').value, 10) || 40,
    max_external:    parseInt($('spCapExternal').value, 10) || 50,
  };
  if (Object.keys(overrides).length) payload.course_overrides = overrides;
  return payload;
}

/* ── Progress bar ── */
let _progressInterval = null;
function showProgress() {
  const wrap = $('spProgressWrap');
  const bar  = $('spProgressBar');
  wrap.classList.remove('d-none');
  bar.style.width = '0%';
  let pct = 0;
  _progressInterval = setInterval(() => {
    pct += Math.random() * 10 + 3;
    if (pct > 92) pct = 92;
    bar.style.width = pct + '%';
  }, 300);
}

function hideProgress() {
  clearInterval(_progressInterval);
  const bar = $('spProgressBar');
  bar.style.width = '100%';
  setTimeout(() => {
    $('spProgressWrap').classList.add('d-none');
    bar.style.width = '0%';
  }, 500);
}

/* ── Status messages ── */
function showStatus(msg, type) {
  const el = $('spStatus');
  el.className = type === 'ok' ? 'sp-alert sp-alert-ok'
               : type === 'warn' ? 'sp-alert sp-alert-warn' : 'sp-alert sp-alert-err';
  /* An error interrupts (an alert); anything else waits its turn. */
  el.setAttribute('role', type === 'err' ? 'alert' : 'status');
  el.setAttribute('aria-live', type === 'err' ? 'assertive' : 'polite');
  el.textContent = msg;
  el.classList.remove('d-none');
}
/* Emptied, never display:none: a live region has to be in the page before its
 * words change, or a screen reader may not read them. Empty and classless, it
 * takes no room. */
function hideStatus() {
  const el = $('spStatus');
  el.textContent = '';
  el.className = '';
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
}

/* ── Render results ── */
let _lastPayload = null;
/* "Our" departments come from the server (section_planning.LOCAL_DEPARTMENTS). */
const LOCAL_DEPTS = new Set((() => {
  try { return JSON.parse($('spLocalDepartments')?.textContent || '[]'); } catch (_) { return []; }
})());

/* Sorting is wired once per table (the shared sorter adds listeners on each
 * call, so wiring on every render sorted a column once per render). A new
 * result keeps the column the user sorted by: the sorter keeps each header's
 * next direction, so sorting that header twice lands on the one it shows. */
function wireSortOnce(table) {
  if (!table || table.dataset.sortWired || typeof wireSortableTable !== 'function') return;
  table.dataset.sortWired = '1';
  wireSortableTable(table.id);
}
function keepSort(table) {
  const th = table.querySelector('thead th[data-dir]');
  if (th) { th.click(); th.click(); }
}

/* A results table scrolled sideways keeps its course code in view (a sticky
 * column, global.css), opaque only while scrolled. One listener for every
 * results box, present or rendered later: scroll does not bubble, so it
 * listens in the capture phase. scrollLeft is negative when Arabic scrolls. */
document.addEventListener('scroll', event => {
  const box = event.target;
  if (box instanceof Element && box.classList.contains('sp-table-scroll')) {
    box.classList.toggle('sp-scrolled', box.scrollLeft !== 0);
  }
}, { capture: true, passive: true });

/* The result's scope as a heading: "AI · 1448 T1 · Female (F)". It names the
 * section the result on screen was generated for (the server's answer), not
 * the radio's state now: Export exports exactly this. Codes are LTR islands. */
function resultScopeHtml(payload, data) {
  const programs = [...new Set(String(payload.program || '').split(',').map(p => p.trim()).filter(Boolean))];
  const scope = programs.length ? programs.map(p => `<bdi>${esc(p)}</bdi>`).join(', ') : esc(T.allProgs);
  const section = data.section || payload.section;
  const term = `<bdi>${esc(data.year ?? payload.year)}</bdi> ${T.term}<bdi>${esc(data.semester ?? payload.semester)}</bdi>`;
  return `${scope} · ${term} · ${esc(T.sections[section] || '')} <bdi>(${esc(section)})</bdi>`;
}

function renderResults(data, payload) {
  $('spResultsScope').innerHTML = resultScopeHtml(payload, data);
  if (data.mode === 'multi') {
    renderMultiProgramResults(data);
  } else {
    renderSingleProgramResults(data);
  }
}

/* The results tables' header is the server's (the template's #spTable), read
 * before anything wires it: per-programme tables reuse it, and every cell
 * carries its column's header as the label a phone card shows. */
const PLAN_HEAD_HTML = $('spTable').tHead.innerHTML;
const PLAN_LABELS = [...$('spTable').tHead.querySelectorAll('th')].map(th => th.textContent.trim());
/* Each column's part in a phone card (<=768px, .mobile-cards): hidden, the
 * title line (code, name), or a number; the number classes also order the
 * card: demand, sections, max, then the fill bar, then the status. */
const CARD_ROLE = ['mc-hide', 'mc-hide', 'mc-primary', 'mc-primary', 'mc-hide', 'sp-c-demand',
  'sp-c-sections', 'sp-c-max', 'mc-hide', 'sp-c-fill', 'sp-c-status'];
const PLAN_COLUMNS = CARD_ROLE.length;
wireSortOnce($('spTable'));   // after the header is read: the copies start unsorted

/* ── Build table rows HTML from a plan array ── */
function buildPlanRows(plan) {
  if (!plan.length) {
    return `<tr><td colspan="${PLAN_COLUMNS}" class="empty-note">${T.noRecs}</td></tr>`;
  }
  return plan.map((row, idx) => {
    const fillCls = row.fill_percent >= 80 ? 'sp-fill-hi'
                  : row.fill_percent >= 40 ? 'sp-fill-md'
                  : 'sp-fill-lo';
    let statusHtml = '';
    if (row.status === 'full') {
      statusHtml = `<span class="sp-pill sp-pill-full">${T.full}</span>`;
    } else if (row.status === 'underfilled') {
      statusHtml = `<span class="sp-pill sp-pill-under">${T.underfilled}</span>`;
    }
    const slots = (row.slots || []).join(', ');
    const slotTag = slots
      ? ` <span class="sp-slot" title="${esc(T.fillsTitle(slots))}">${T.fills} <bdi>${esc(slots)}</bdi></span>` : '';
    const source = T.source[row.limit_source] ? T.source[row.limit_source](slots) : '';
    const extBadge = row.is_external ? ` <span class="sp-pill sp-pill-ext">EXT</span>` : '';
    const programs = Array.isArray(row.programs) ? row.programs.filter(Boolean) : [];
    const programTags = programs.length
      ? `<span style="display:inline-flex;flex-wrap:wrap;gap:4px;margin-inline-end:6px;vertical-align:middle">${programs.map(p => `<span class="sp-pill sp-pill-ext">${esc(p)}</span>`).join('')}</span>`
      : '';
    const courseName = row.course_name || '';
    const cells = [
      [idx + 1],
      [`<strong>${esc(row.department)}</strong>`],
      [`<span class="cr-id">${esc(row.course_code)}</span>${slotTag}${extBadge}`],
      [`${programTags}<span>${esc(courseName)}</span>`],
      [esc(row.credit_hours), 'text-center'],
      [`<strong>${esc(row.total_students)}</strong>`, 'text-center'],
      [`<strong>${esc(row.num_sections)}</strong>`, 'text-center'],
      [`${esc(row.max_per_section)}${source ? `<div class="sp-cell-sub sp-limit-src">${esc(source)}</div>` : ''}`, 'text-center'],
      [esc(row.avg_per_section), 'text-center'],
      [`<div class="d-flex align-items-center gap-1">
          <div class="sp-fill-wrap"><div class="sp-fill ${fillCls}" style="width:${row.fill_percent}%"></div></div>
          <span class="fs-sm text-t3" style="min-width:30px">${row.fill_percent}%</span>
        </div>`, 'sp-fill-cell'],
      [statusHtml],
    ];
    /* No whitespace inside a cell's tags: an empty status cell is :empty. */
    const tds = cells.map(([html, cls], i) =>
      `<td class="${CARD_ROLE[i]}${cls ? ` ${cls}` : ''}" data-label="${esc(PLAN_LABELS[i] || '')}">${html}</td>`);
    return `<tr data-code="${esc(row.course_code)}">${tds.join('')}</tr>`;
  }).join('');
}

/* ── Department summary: every department in the result, in two groups by
 * the server's list of our departments (ours; then the service departments,
 * whose sections are requested from them), with the section's sections.
 * The total row adds the departments up; the server's summary is the same
 * plan, so it equals the result's KPIs. ── */
const TS = {
  dept:       IS_AR ? 'القسم' : 'Department',
  courses:    IS_AR ? 'المقررات' : 'Courses',
  seats:      IS_AR ? 'المقاعد المطلوبة' : 'Seat demand',
  sections:   IS_AR ? 'الشعب' : 'Sections',
  hours:      IS_AR ? 'ساعات التدريس' : 'Teaching hours',
  hoursTitle: IS_AR ? 'ساعات المقرر × عدد الشعب' : 'Credit hours × sections',
  ours:       IS_AR ? 'أقسامنا' : 'Our departments',
  service:    IS_AR ? 'الأقسام الخدمية (شعب تُطلب منها)' : 'Service departments (sections to request)',
  subtotal:   IS_AR ? 'المجموع الفرعي' : 'Subtotal',
  total:      IS_AR ? 'المجموع' : 'Total',
};
/* Sections first: on a phone they show before the table scrolls sideways. */
const SUM_COLS = ['sections', 'courses', 'students', 'total_credits'];

function sumDepartments(depts) {
  const total = Object.fromEntries(SUM_COLS.map(key => [key, 0]));
  depts.forEach(d => SUM_COLS.forEach(key => { total[key] += Number(d[key]) || 0; }));
  return total;
}

function sumCells(d) {
  return SUM_COLS.map(key => `<td data-col="${key}"><bdi>${Number(d[key]) || 0}</bdi></td>`).join('');
}

function buildDeptSummaryHtml(summary) {
  const depts = (summary && summary.departments) || [];
  if (!depts.length) return '';
  const groups = [
    ['ours', TS.ours, depts.filter(d => LOCAL_DEPTS.has(d.department))],
    ['service', TS.service, depts.filter(d => !LOCAL_DEPTS.has(d.department))],
  ].filter(([, , rows]) => rows.length);
  const body = groups.map(([key, label, rows]) => `<tbody data-group="${key}">
      <tr class="sp-sum-group"><th scope="rowgroup" colspan="${SUM_COLS.length + 1}">${esc(label)}</th></tr>
      ${rows.map(d => `<tr data-dept="${esc(d.department)}"><th scope="row"><bdi>${esc(d.department)}</bdi></th>${sumCells(d)}</tr>`).join('')}
      ${groups.length > 1 ? `<tr class="sp-sum-sub" data-subtotal="${key}"><th scope="row">${esc(TS.subtotal)}</th>${sumCells(sumDepartments(rows))}</tr>` : ''}
    </tbody>`).join('');
  const head = [TS.dept, TS.sections, TS.courses, TS.seats]
    .map(text => `<th scope="col">${esc(text)}</th>`).join('')
    + `<th scope="col" title="${esc(TS.hoursTitle)}">${esc(TS.hours)}</th>`;
  return `<table class="sp-sum-table">
      <thead><tr>${head}</tr></thead>${body}
      <tfoot><tr data-sum-total><th scope="row">${esc(TS.total)}</th>${sumCells(sumDepartments(depts))}</tr></tfoot>
    </table>`;
}

/* ── KPIs: the section's students, courses, sections and fill; students
 * with no recorded section stated, never pooled ── */
function renderDroppedSlots(electives) {
  const note = $('spElectiveNote');
  const e = electives || {};
  if (!e.dropped_total) {
    note.textContent = '';
    note.classList.add('d-none');
    return;
  }
  const parts = (e.dropped || []).map(d =>
    `${d.program} ${d.slot} — ${T.dropReason[d.reason] || d.reason} (${d.students})`).join('; ');
  note.textContent = T.dropped(e.dropped_total, parts);
  note.classList.remove('d-none');
}

function renderKpis(data, summary) {
  const s = summary || {};
  $('spKpiStudents').textContent = String(data.student_count ?? 0);
  $('spKpiCourses').textContent = String(s.total_courses || 0);
  $('spKpiSections').textContent = String(s.total_sections || 0);
  $('spKpiFill').textContent = (s.avg_fill_percent || 0) + '%';
  renderDroppedSlots(data.electives);
  const missing = Number(data.no_section) || 0;
  const note = $('spNoSectionNote');
  note.textContent = missing ? noSectionText(missing) : '';
  note.classList.toggle('d-none', !missing);
}

/* ── Single-program mode (original behavior) ── */
function renderSingleProgramResults(data) {
  $('spResults').classList.remove('d-none');

  /* Show single table, hide multi container */
  $('spTable').style.display = '';
  $('spPager').style.display = '';
  $('spMultiPrograms').classList.add('d-none');
  $('spMultiPrograms').innerHTML = '';


  /* KPIs */
  renderKpis(data, data.summary);

  /* Timestamp */
  $('spTimestamp').textContent = T.lastUpdate + ': ' + new Date().toLocaleTimeString();

  /* Table */
  const tbody = $('spTable').querySelector('tbody');
  const plan = data.plan || [];

  if (!plan.length) {
    tbody.innerHTML = `<tr><td colspan="${PLAN_COLUMNS}" class="empty-note">${T.noRecs}</td></tr>`;
    $('spDeptSummary').innerHTML = '';
    return;
  }

  tbody.innerHTML = buildPlanRows(plan);
  keepSort($('spTable'));
  if (typeof paginateTable === 'function') paginateTable('spTable', 'spPager', 30);

  /* Department summary */
  $('spDeptSummary').innerHTML = buildDeptSummaryHtml(data.summary);
}

/* ── Multi-program mode ── */
let _multiTableCounter = 0;

function renderMultiProgramResults(data) {
  $('spResults').classList.remove('d-none');

  /* Show combined union in the main table, show multi container for per-program */
  $('spTable').style.display = '';
  $('spPager').style.display = '';
  const container = $('spMultiPrograms');
  container.classList.remove('d-none');
  container.innerHTML = '';

  /* KPIs from the pooled plan (programmes share sections, as the builder pools them) */
  const cs = data.combined_summary || {};
  renderKpis(data, cs);

  /* Timestamp */
  $('spTimestamp').textContent = T.lastUpdate + ': ' + new Date().toLocaleTimeString();

  /* ── Union table (main table) ── */
  const combinedPlan = data.combined_plan || [];
  const tbody = $('spTable').querySelector('tbody');
  if (!combinedPlan.length) {
    tbody.innerHTML = `<tr><td colspan="${PLAN_COLUMNS}" class="empty-note">${T.noRecs}</td></tr>`;
    $('spDeptSummary').innerHTML = '';
  } else {
    tbody.innerHTML = buildPlanRows(combinedPlan);
    keepSort($('spTable'));
    if (typeof paginateTable === 'function') paginateTable('spTable', 'spPager', 30);
    $('spDeptSummary').innerHTML = buildDeptSummaryHtml(cs);
  }

  /* ── Collapsible per-program blocks ── */
  (data.programs || []).forEach(prog => {
    _multiTableCounter++;
    const tableId = 'spMultiTable_' + _multiTableCounter;
    const bodyId  = 'spMultiBody_' + _multiTableCounter;
    const plan = prog.plan || [];
    const summary = prog.summary || {};

    const block = document.createElement('div');
    block.className = 'sp-prog-block';
    block.style.marginTop = '14px';

    /* Collapsible heading — starts collapsed. A button inside the heading,
     * so it opens and closes by keyboard and says whether it is open. */
    const heading = document.createElement('h5');
    heading.className = 'sp-prog-heading';
    heading.innerHTML = `<button type="button" class="sp-prog-toggle" aria-expanded="false" aria-controls="${bodyId}">
      ${CHEVRON}<bdi class="sp-prog-code">${esc(prog.program)}</bdi>
      <span class="sp-prog-count">(${countHtml(prog.student_count, 'student')}
        · ${countHtml(summary.total_courses, 'course')}
        · ${countHtml(summary.total_sections, 'section')})</span></button>`;
    block.appendChild(heading);
    const toggle = heading.querySelector('button');

    /* Collapsible body — hidden by default */
    const body = document.createElement('div');
    body.id = bodyId;
    body.className = 'd-none';

    /* Table: the main table's header and phone cards, in its own scroller */
    const scroller = document.createElement('div');
    scroller.className = 'sp-table-scroll';
    const table = document.createElement('table');
    table.className = 'tbl-card mobile-cards sp-plan-table';
    table.id = tableId;
    table.setAttribute('role', 'table');   // a table still, when a phone lays it out as cards
    table.innerHTML = `<thead>${PLAN_HEAD_HTML}</thead><tbody>${buildPlanRows(plan)}</tbody>`;
    scroller.appendChild(table);
    body.appendChild(scroller);

    /* Department summary for this program: its total is the heading's */
    const deptHtml = buildDeptSummaryHtml(summary);
    if (deptHtml) {
      const deptPanel = document.createElement('section');
      deptPanel.className = 'sp-panel sp-sum-panel';
      deptPanel.style.marginTop = '8px';
      deptPanel.setAttribute('aria-labelledby', `${bodyId}_sum`);
      deptPanel.innerHTML = `<h6 class="mb-2" style="font-size:.82rem" id="${bodyId}_sum">${T.deptSummary} · <bdi>${esc(prog.program)}</bdi></h6>
        <div class="sp-sum-scroll">${deptHtml}</div>`;
      body.appendChild(deptPanel);
    }

    block.appendChild(body);
    container.appendChild(block);
    if (plan.length) wireSortOnce(table);

    toggle.addEventListener('click', () => {
      const hidden = body.classList.toggle('d-none');
      toggle.setAttribute('aria-expanded', hidden ? 'false' : 'true');
    });
  });
}

/* ── Generate: the scope form's submit, so Enter in Year, Semester or
 * Program runs it too. Generate is the form's only submit button; the seat
 * limits' Save lives outside the form, so Enter never saves. A plan is for
 * one section: with none chosen, nothing is asked of the server. ── */
async function runGenerate() {
  const btn = $('spGenerate');
  if (btn.disabled) return;   // one Generate at a time
  const payload = getPayload();
  if (!payload.year || !payload.semester) {
    showStatus(T.fillAll, 'err');
    return;
  }
  if (!payload.section) {
    showSectionMessage(T.chooseSection);
    return;
  }
  clearSectionMessage();

  btn.disabled = true;
  btn.textContent = T.generating;
  hideStatus();
  showProgress();

  try {
    const res = await fetch('/ops/section-planning/generate/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify(payload),
    });

    const data = await res.json().catch(() => ({}));

    if (!res.ok || !data.ok) {
      reportRefusal(data, res.status);
      return;
    }

    _lastPayload = payload;
    renderResults(data, payload);
    showStatus(T.done, 'ok');   // the one success channel: the status line, not a toast as well

  } catch (err) {
    showStatus(T.reqFailed + ': ' + err.message, 'err');
  } finally {
    hideProgress();
    btn.disabled = false;
    btn.textContent = IS_AR ? 'حساب' : 'Generate';
  }
}
$('spScopeForm').addEventListener('submit', event => {
  event.preventDefault();   // never a page load: the plan comes back as JSON
  runGenerate();
});

/* A refusal in the page's words: about the section, at the Section control. */
function reportRefusal(data, status) {
  if (data && SECTION_ERRORS.has(data.code)) showSectionMessage(requestError(data, status));
  else showStatus(requestError(data, status), 'err');
}

/* The file name the server gave the download (it names the section), else
 * one made the same way from the export's own stem. */
function downloadName(res, payload, stem) {
  const header = (res.headers && typeof res.headers.get === 'function' && res.headers.get('Content-Disposition')) || '';
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try { return decodeURIComponent(encoded[1].trim()); } catch (_) { /* the plain name, then */ }
  }
  const plain = /filename\s*=\s*(?:"([^"]+)"|([^;]+))/i.exec(header);
  const name = plain ? (plain[1] || plain[2] || '').trim() : '';
  return name || `${stem || 'section_plan'}_${payload.year}_${payload.semester}_${payload.section}.xlsx`;
}

/* ── Export: exactly the result on screen (its payload, its section), even
 * if the scope fields or the Section radio changed since. Two files from the
 * one result — the plan, and the students the plan's counts are made of — so
 * both go through here and differ only in their endpoint. ── */
async function runExport(button, { url, stem, idleLabel }) {
  if (!_lastPayload) return;

  button.disabled = true;
  button.textContent = T.exporting;

  try {
    const exportPayload = { ..._lastPayload };
    const deptFilter = ($('spDeptFilter').value || '').trim().toUpperCase();
    if (deptFilter) exportPayload.dept_filter = deptFilter;

    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify(exportPayload),
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      reportRefusal(errData, res.status);
      return;
    }

    /* Download the blob as .xlsx */
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = downloadName(res, exportPayload, stem);
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objectUrl);

    if (typeof notify !== 'undefined') notify.success(T.exported);

  } catch (err) {
    showStatus(T.reqFailed + ': ' + err.message, 'err');
  } finally {
    button.disabled = false;
    button.textContent = idleLabel();
  }
}

/* The plan: a row per course, the sections it needs. */
$('spExport').onclick = () => runExport($('spExport'), {
  url: '/ops/section-planning/export/',
  stem: 'section_plan',
  idleLabel: () => IS_AR ? '📥 تصدير XLSX' : '📥 Export XLSX',
});

/* The students: a row per student per course, the roster behind every count.
 * It names who registers a course, never which numbered section they sit in. */
$('spExportRosters').onclick = () => runExport($('spExportRosters'), {
  url: '/ops/section-planning/export-rosters/',
  stem: 'section_rosters',
  idleLabel: () => IS_AR ? '👥 تصدير الطلاب (XLSX)' : '👥 Export Students XLSX',
});

/* ── Reset click: clears the scope and the result; the chosen section is a
 * preference, not a draft, so it stays chosen. ── */
$('spReset').onclick = () => {
  $('spProgram').value = '';
  $('spResults').classList.add('d-none');
  hideStatus();
  clearSectionMessage();
  _lastPayload = null;

  /* Restore single-mode table visibility */
  $('spTable').style.display = '';
  $('spPager').style.display = '';
  $('spTable').querySelector('tbody').innerHTML =
    `<tr><td colspan="${PLAN_COLUMNS}" class="empty-note">${esc(T.emptyScope)}</td></tr>`;
  $('spDeptSummary').innerHTML = '';

  /* Clear multi-program container */
  $('spMultiPrograms').innerHTML = '';
  $('spMultiPrograms').classList.add('d-none');

  /* Drop drafts (never saved limits), and show the panel for the cleared scope. */
  discardDrafts();
  if (_advState !== 'idle') loadAdvancedCourses();
  /* The same for the weekly meetings: with no programme it asks nothing. */
  discardMeetDrafts();
  if (_meetState !== 'idle') loadMeetingPatterns();
};

/* ── Department filter on results table ── */
(function(){
  const filterInput = $('spDeptFilter');
  const clearBtn = $('spDeptFilterClear');
  if (!filterInput) return;

  function applyDeptFilter() {
    const raw = filterInput.value.trim().toUpperCase();
    const prefixes = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];
    document.querySelectorAll('.tbl-card tbody').forEach(tbody => {
      const rows = tbody.querySelectorAll('tr');
      rows.forEach(row => {
        if (!prefixes.length) {
          row.style.display = '';
          return;
        }
        // Course code is in the 3rd column (index 2)
        const courseCell = row.querySelector('td:nth-child(3)');
        if (!courseCell) { row.style.display = ''; return; }
        const code = (courseCell.textContent || '').trim().toUpperCase();
        const match = prefixes.some(p => code.startsWith(p));
        row.style.display = match ? '' : 'none';
      });

      // Re-number visible rows per table.
      let num = 0;
      rows.forEach(row => {
        if (row.style.display !== 'none') {
          num++;
          const numCell = row.querySelector('td:first-child');
          if (numCell) numCell.textContent = num;
        }
      });
    });
  }

  filterInput.addEventListener('input', applyDeptFilter);
  clearBtn.addEventListener('click', () => {
    filterInput.value = '';
    applyDeptFilter();
  });
})();
