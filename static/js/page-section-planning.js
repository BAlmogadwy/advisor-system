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
  male:        IS_AR ? 'ذكور' : 'M',
  female:      IS_AR ? 'إناث' : 'F',
  noGender:    (n, seats, courses) => IS_AR
    ? `${n} طالب بلا جنس مسجّل: يحتاجون ${seats} مقعداً في ${courses} مقرر، وهم خارج كل الأعداد. سجّل جنسهم ثم أعد الحساب.`
    : `${n} student${n === 1 ? ' has' : 's have'} no recorded gender: ${seats} seat${seats === 1 ? '' : 's'} across ${courses} course${courses === 1 ? '' : 's'}, left out of every count. Record their gender, then generate again.`,
  noGenderRow: n => IS_AR ? `+${n} بلا جنس` : `+${n} no gender`,
  noGenderOnly: IS_AR ? 'بلا جنس مسجّل' : 'No gender recorded',
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

/* A disclosure chevron: CSS points it along the reading direction when
 * closed (right in English, left in Arabic) and down when open. */
const CHEVRON = '<svg class="sp-chev" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

/* M · F split for a KPI, a row or a summary card. */
function splitText(male, female) {
  return `${T.male} ${male ?? 0} · ${T.female} ${female ?? 0}`;
}

const CSRF = document.querySelector('[name=csrfmiddlewaretoken]')?.value
  || 'djCsrfToken';

const $ = id => document.getElementById(id);

/* ── Toggle capacity settings ── */
$('spToggleCaps').onclick = () => {
  const hidden = $('spCapsWrap').classList.toggle('d-none');
  $('spToggleCaps').setAttribute('aria-expanded', hidden ? 'false' : 'true');
};

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
const _drafts = new Map(); // course code -> { raw, all, typed }

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
};
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
   * value only if the user typed it: a row changed only in scope ("All
   * programmes") keeps the limit saved NOW, so a limit someone else saved
   * in the meantime is never turned back to the one pre-filled before. */
  tbody.querySelectorAll('tr[data-code]').forEach(tr => {
    const inp = tr.querySelector('.adv-input');
    const all = tr.querySelector('.adv-all');
    const prior = _drafts.get(tr.dataset.code);
    if (prior) {
      if (prior.typed) inp.value = prior.raw;
      all.checked = prior.all;
    }
    inp.addEventListener('input', () => { refreshAdvRow(tr); rememberDraft(tr); updateAdvState(); });
    inp.addEventListener('blur', () => {
      /* An emptied saved limit is not a removal: that has its own button. */
      if (!inp.value.trim() && tr.dataset.saved !== '') inp.value = tr.dataset.saved;
      refreshAdvRow(tr); rememberDraft(tr); updateAdvState();
    });
    all.addEventListener('change', () => { refreshAdvRow(tr); rememberDraft(tr); updateAdvState(); });
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
 * the saved limit the row was pre-filled with. */
function rememberDraft(tr) {
  const d = draftOf(tr);
  if (d.draft || d.invalid) _drafts.set(d.code, { raw: d.raw, all: d.all, typed: d.changed || d.invalid });
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

async function postLimits(body) {
  const res = await fetch(LIMITS_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { res, data };
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

/* ── Collect payload ── */
function getPayload() {
  const overrides = collectOverrides();
  const payload = {
    year:            parseInt($('spYear').value, 10) || 0,
    semester:        parseInt($('spSemester').value, 10) || 0,
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

function renderResults(data) {
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
 * card: demand, max, fill, then M | F | Total sections, then the status. */
const CARD_ROLE = ['mc-hide', 'mc-hide', 'mc-primary', 'mc-primary', 'mc-hide', 'sp-c-demand',
  'sp-sec-m', 'sp-sec-f', 'sp-sec-total', 'sp-c-max', 'mc-hide', 'sp-c-fill', 'sp-c-status'];
wireSortOnce($('spTable'));   // after the header is read: the copies start unsorted

/* ── Build table rows HTML from a plan array ── */
function buildPlanRows(plan) {
  if (!plan.length) {
    return `<tr><td colspan="13" class="empty-note">${T.noRecs}</td></tr>`;
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
    } else if (row.status === 'no_gender') {
      statusHtml = `<span class="sp-pill sp-pill-under">${T.noGenderOnly}</span>`;
    }
    const slots = (row.slots || []).join(', ');
    const slotTag = slots
      ? ` <span class="sp-slot" title="${esc(T.fillsTitle(slots))}">${T.fills} <bdi>${esc(slots)}</bdi></span>` : '';
    const source = T.source[row.limit_source] ? T.source[row.limit_source](slots) : '';
    const noGender = row.unknown_students
      ? `<div class="sp-cell-sub sp-no-gender">${esc(T.noGenderRow(row.unknown_students))}</div>` : '';
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
      [`<strong>${esc(row.total_students)}</strong>
        <div class="sp-cell-sub">${esc(splitText(row.male_students, row.female_students))}</div>${noGender}`, 'text-center'],
      [esc(row.male_sections ?? 0), 'text-center'],
      [esc(row.female_sections ?? 0), 'text-center'],
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
 * whose sections are requested from them), each with M, F and Total
 * sections. The total row adds the departments up; the server's summary is
 * the same plan, so it equals the result's KPIs. ── */
const TS = {
  dept:       IS_AR ? 'القسم' : 'Department',
  courses:    IS_AR ? 'المقررات' : 'Courses',
  seats:      IS_AR ? 'المقاعد المطلوبة' : 'Seat demand',
  male:       IS_AR ? 'شعب الذكور' : 'M sections',
  female:     IS_AR ? 'شعب الإناث' : 'F sections',
  sections:   IS_AR ? 'مجموع الشعب' : 'Total sections',
  hours:      IS_AR ? 'ساعات التدريس' : 'Teaching hours',
  hoursTitle: IS_AR ? 'ساعات المقرر × عدد الشعب' : 'Credit hours × sections',
  ours:       IS_AR ? 'أقسامنا' : 'Our departments',
  service:    IS_AR ? 'الأقسام الخدمية (شعب تُطلب منها)' : 'Service departments (sections to request)',
  subtotal:   IS_AR ? 'المجموع الفرعي' : 'Subtotal',
  total:      IS_AR ? 'المجموع' : 'Total',
};
/* Sections first: on a phone they show before the table scrolls sideways. */
const SUM_COLS = ['male_sections', 'female_sections', 'sections', 'courses', 'students', 'total_credits'];

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
  const head = [TS.dept, TS.male, TS.female, TS.sections, TS.courses, TS.seats]
    .map(text => `<th scope="col">${esc(text)}</th>`).join('')
    + `<th scope="col" title="${esc(TS.hoursTitle)}">${esc(TS.hours)}</th>`;
  return `<table class="sp-sum-table">
      <thead><tr>${head}</tr></thead>${body}
      <tfoot><tr data-sum-total><th scope="row">${esc(TS.total)}</th>${sumCells(sumDepartments(depts))}</tr></tfoot>
    </table>`;
}

/* ── KPIs: Sections = M + F; no-gender students stated, never pooled ── */
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

function renderKpis(studentCount, cohorts, summary, electives) {
  const c = cohorts || {};
  const s = summary || {};
  $('spKpiStudents').textContent = String(studentCount ?? 0);
  $('spKpiStudentsSplit').textContent = splitText(c.M, c.F);
  $('spKpiCourses').textContent = String(s.total_courses || 0);
  $('spKpiSections').textContent = String(s.total_sections || 0);
  $('spKpiSectionsSplit').textContent = splitText(s.male_sections, s.female_sections);
  $('spKpiFill').textContent = (s.avg_fill_percent || 0) + '%';
  renderDroppedSlots(electives);
  const ng = s.no_gender || {};
  const note = $('spGenderNote');
  if (ng.students) {
    note.textContent = T.noGender(ng.students, ng.seat_demand || 0, ng.courses || 0);
    note.classList.remove('d-none');
  } else {
    note.textContent = '';
    note.classList.add('d-none');
  }
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
  renderKpis(data.student_count, data.cohorts, data.summary, data.electives);

  /* Timestamp */
  $('spTimestamp').textContent = T.lastUpdate + ': ' + new Date().toLocaleTimeString();

  /* Table */
  const tbody = $('spTable').querySelector('tbody');
  const plan = data.plan || [];

  if (!plan.length) {
    tbody.innerHTML = `<tr><td colspan="13" class="empty-note">${T.noRecs}</td></tr>`;
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
  renderKpis(data.student_count, data.cohorts, cs, data.electives);

  /* Timestamp */
  $('spTimestamp').textContent = T.lastUpdate + ': ' + new Date().toLocaleTimeString();

  /* ── Union table (main table) ── */
  const combinedPlan = data.combined_plan || [];
  const tbody = $('spTable').querySelector('tbody');
  if (!combinedPlan.length) {
    tbody.innerHTML = `<tr><td colspan="13" class="empty-note">${T.noRecs}</td></tr>`;
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
        · ${countHtml(summary.total_sections, 'section')}: ${esc(splitText(summary.male_sections, summary.female_sections))})</span></button>`;
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
 * limits' Save lives outside the form, so Enter never saves. ── */
async function runGenerate() {
  const btn = $('spGenerate');
  if (btn.disabled) return;   // one Generate at a time
  const payload = getPayload();
  if (!payload.year || !payload.semester) {
    showStatus(T.fillAll, 'err');
    return;
  }

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
      showStatus(requestError(data, res.status), 'err');
      return;
    }

    _lastPayload = payload;
    renderResults(data);
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

/* ── Export click ── */
$('spExport').onclick = async () => {
  if (!_lastPayload) return;

  const btn = $('spExport');
  btn.disabled = true;
  btn.textContent = T.exporting;

  try {
    const exportPayload = { ..._lastPayload };
    const deptFilter = ($('spDeptFilter').value || '').trim().toUpperCase();
    if (deptFilter) exportPayload.dept_filter = deptFilter;

    const res = await fetch('/ops/section-planning/export/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify(exportPayload),
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      showStatus(requestError(errData, res.status), 'err');
      return;
    }

    /* Download the blob as .xlsx */
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `section_plan_${_lastPayload.year}_${_lastPayload.semester}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

    if (typeof notify !== 'undefined') notify.success(T.exported);

  } catch (err) {
    showStatus(T.reqFailed + ': ' + err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = IS_AR ? '📥 تصدير XLSX' : '📥 Export XLSX';
  }
};

/* ── Reset click ── */
$('spReset').onclick = () => {
  $('spProgram').value = '';
  $('spResults').classList.add('d-none');
  hideStatus();
  _lastPayload = null;

  /* Restore single-mode table visibility */
  $('spTable').style.display = '';
  $('spPager').style.display = '';
  $('spTable').querySelector('tbody').innerHTML =
    `<tr><td colspan="13" class="empty-note">${IS_AR ? 'حدد السنة والفصل ثم انقر حساب.' : 'Set Year & Semester, then click Generate.'}</td></tr>`;
  $('spDeptSummary').innerHTML = '';

  /* Clear multi-program container */
  $('spMultiPrograms').innerHTML = '';
  $('spMultiPrograms').classList.add('d-none');

  /* Drop drafts (never saved limits), and show the panel for the cleared scope. */
  discardDrafts();
  if (_advState !== 'idle') loadAdvancedCourses();
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
