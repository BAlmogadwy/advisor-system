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
  courses:     IS_AR ? 'مقررات' : 'courses',
  sections:    IS_AR ? 'شعب' : 'sections',
  students:    IS_AR ? 'طلاب' : 'students',
  credits:     IS_AR ? 'ساعة' : 'cr',
  lastUpdate:  IS_AR ? 'آخر تحديث' : 'Last update',
  deptSummary: IS_AR ? 'ملخص الأقسام' : 'Department Summary',
  noRecs:      IS_AR ? 'لا توجد توصيات.' : 'No recommendations found.',
  progLabel:   IS_AR ? 'طالب' : 'students',
};

const CSRF = document.querySelector('[name=csrfmiddlewaretoken]')?.value
  || 'djCsrfToken';

const $ = id => document.getElementById(id);

/* ── Toggle capacity settings ── */
$('spToggleCaps').onclick = () => {
  $('spCapsWrap').classList.toggle('d-none');
};

/* ── Per-course seat limits ──
 * A limit belongs to a programme. Editing a row makes a DRAFT: it is used as a
 * what-if on Generate, marked "Modified", and never written until the user
 * presses Save, reviews the exact rows (course · programme · old → new) and
 * confirms. Discard drops drafts only; removing a saved limit is its own
 * confirmed action. Nothing here writes on input, change or blur. */
const LIMIT_MIN = 1, LIMIT_MAX = 500;
const LIMITS_URL = '/ops/section-planning/limits/';
let _advCourses = [];     // cached course list from server
let _advLoaded = false;   // loaded at least once?

let _advProgram = '';  // program(s) when panel was last loaded

const TL = {
  modified:     IS_AR ? 'معدَّل' : 'Modified',
  invalid:      IS_AR ? `رقم صحيح من ${LIMIT_MIN} إلى ${LIMIT_MAX}` : `Whole number ${LIMIT_MIN}–${LIMIT_MAX}`,
  mixed:        IS_AR ? 'مختلف' : 'mixed',
  allProgs:     IS_AR ? 'كل البرامج' : 'All programmes',
  allAria:      code => IS_AR ? `تطبيق حد ${code} على كل البرامج التي تدرّسه` : `Apply the ${code} limit to every programme that teaches it`,
  limitAria:    code => IS_AR ? `الحد الأقصى لشعبة ${code}` : `Seat limit for ${code}`,
  remove:       IS_AR ? 'إزالة' : 'Remove',
  removeAria:   code => IS_AR ? `إزالة الحد المحفوظ لـ ${code}` : `Remove the saved limit for ${code}`,
  drafts:       n => IS_AR ? `${n} تعديل غير محفوظ` : `${n} unsaved change${n === 1 ? '' : 's'}`,
  noDrafts:     IS_AR ? 'لا توجد تعديلات غير محفوظة' : 'No unsaved changes',
  needProgram:  IS_AR ? 'اختر برنامجاً أولاً: الحدود تُحفظ لكل برنامج.' : 'Choose a programme first: limits are saved per programme.',
  fixInvalid:   IS_AR ? 'صحّح القيم غير الصالحة قبل الحفظ.' : 'Fix the invalid values before saving.',
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
  saving:       IS_AR ? 'جارٍ الحفظ...' : 'Saving...',
  saveBtn:      IS_AR ? 'حفظ الحدود…' : 'Save limits…',
  saved:        n => IS_AR ? `حُفظ ${n} حد وسُجّل في سجل التدقيق.` : `Saved ${n} seat limit${n === 1 ? '' : 's'}; each change is in the audit log.`,
  leave:        IS_AR ? 'لديك حدود غير محفوظة.' : 'You have unsaved seat limits.',
};

/* Server refusals carry a stable code; the words are the page's. */
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
function limitError(data, status) {
  const make = data && LIMIT_ERRORS[data.code];
  if (make) return make(data);
  if (status === 429) return IS_AR ? 'طلبات كثيرة. انتظر قليلاً ثم حاول.' : 'Too many requests. Wait a moment and try again.';
  if (status === 401 || status === 403) return IS_AR ? 'لا تملك صلاحية حفظ الحدود.' : 'You are not allowed to save limits.';
  return T.reqFailed;
}

$('spToggleAdv').onclick = () => {
  const panel = $('spAdvPanel');
  const isHidden = panel.classList.contains('d-none');
  panel.classList.toggle('d-none');
  $('spToggleAdv').setAttribute('aria-expanded', isHidden ? 'true' : 'false');
  if (isHidden) {
    const prog = $('spProgram').value.trim().toUpperCase();
    if (!_advLoaded || prog !== _advProgram) loadAdvancedCourses();
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

/* The saved limit a row starts from: one value when every programme on screen
 * that teaches the course agrees, "mixed" otherwise. */
function savedLimitOf(course) {
  const limits = course.programme_limits || {};
  const values = Object.values(limits).filter(v => Number.isInteger(v));
  const taught = course.programmes || [];
  if (!values.length) return { value: null, mixed: false, any: false };
  const uniq = [...new Set(values)];
  if (uniq.length === 1 && values.length === taught.length) return { value: uniq[0], mixed: false, any: true };
  return { value: null, mixed: true, any: true };
}

async function loadAdvancedCourses(keep = null) {
  const kept = keep || currentDraftInputs();
  const local4  = parseInt($('spCapLocal4').value, 10) || 25;
  const localO  = parseInt($('spCapLocalOther').value, 10) || 40;
  const ext     = parseInt($('spCapExternal').value, 10) || 50;
  const prog    = $('spProgram').value.trim().toUpperCase();
  _advProgram   = prog;
  let url = `/ops/section-planning/courses/?max_local_4cr=${local4}&max_local_other=${localO}&max_external=${ext}`;
  if (prog) url += `&program=${encodeURIComponent(prog)}`;
  try {
    const res = await fetch(url, { headers: { 'X-CSRFToken': CSRF } });
    const data = await res.json();
    if (!data.ok) { showStatus(data.error || T.reqFailed, 'err'); return; }
    _advCourses = data.courses || [];
    _advLoaded = true;
    renderAdvancedTable(_advCourses, kept);
    $('spAdvCount').textContent = _advCourses.length + (IS_AR ? ' مقرر' : ' courses');
  } catch (err) {
    showStatus(T.reqFailed + ': ' + err.message, 'err');
  }
}

function renderAdvancedTable(courses, kept = new Map()) {
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
        data-code="${code}" data-default="${esc(c.default_max)}">
      <span class="sp-adv-state" aria-live="polite"></span></td>
    <td class="text-center"><label class="sp-adv-all"><input type="checkbox" class="adv-all"
        aria-label="${esc(TL.allAria(c.course_code))}"><span aria-hidden="true">${esc(TL.allProgs)}</span></label></td>
    <td class="text-center">${saved.any
      ? `<button type="button" class="sp-adv-reset adv-remove" aria-label="${esc(TL.removeAria(c.course_code))}">${esc(TL.remove)}</button>`
      : ''}</td>
  </tr>`;
  }).join('');
  /* Drafts only: inputs mark the row, nothing is sent until Save. */
  tbody.querySelectorAll('tr[data-code]').forEach(tr => {
    const inp = tr.querySelector('.adv-input');
    const all = tr.querySelector('.adv-all');
    const prior = kept.get(tr.dataset.code);
    if (prior) { inp.value = prior.raw; all.checked = prior.all; }
    inp.addEventListener('input', () => { refreshAdvRow(tr); updateAdvState(); });
    inp.addEventListener('blur', () => {
      /* An emptied saved limit is not a removal: that has its own button. */
      if (!inp.value.trim() && tr.dataset.saved !== '') inp.value = tr.dataset.saved;
      refreshAdvRow(tr); updateAdvState();
    });
    all.addEventListener('change', () => { refreshAdvRow(tr); updateAdvState(); });
    tr.querySelector('.adv-remove')?.addEventListener('click', () => removeSavedLimit(tr));
    refreshAdvRow(tr);
  });
  updateAdvState();
}

/* What one row asks for, read from the DOM (the only draft state there is). */
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

function advRows() { return [...$('spAdvBody').querySelectorAll('tr[data-code]')]; }
function currentDrafts() { return advRows().map(draftOf).filter(d => d.draft && !d.invalid); }
function invalidDrafts() { return advRows().map(draftOf).filter(d => d.invalid); }
function currentDraftInputs() {
  const kept = new Map();
  advRows().map(draftOf).filter(d => d.draft || d.invalid).forEach(d => kept.set(d.code, { raw: d.raw, all: d.all }));
  return kept;
}

function refreshAdvRow(tr) {
  const d = draftOf(tr);
  const state = tr.querySelector('.sp-adv-state');
  tr.classList.toggle('sp-adv-draft', d.draft && !d.invalid);
  tr.classList.toggle('sp-adv-invalid', d.invalid);
  tr.querySelector('.adv-input').setAttribute('aria-invalid', d.invalid ? 'true' : 'false');
  state.textContent = d.invalid ? TL.invalid : (d.draft ? TL.modified : '');
}

function updateAdvState() {
  const drafts = currentDrafts();
  const invalid = invalidDrafts();
  const badge = $('spAdvBadge');
  if (drafts.length > 0) {
    badge.textContent = drafts.length;
    badge.classList.remove('d-none');
  } else {
    badge.classList.add('d-none');
  }
  $('spAdvDrafts').textContent = drafts.length ? TL.drafts(drafts.length) : TL.noDrafts;
  const btn = $('spAdvSaveDb');
  const reason = !advPrograms().length ? TL.needProgram : (invalid.length ? TL.fixInvalid : '');
  btn.disabled = Boolean(reason) || !drafts.length;
  btn.title = reason;
  $('spAdvReset').disabled = !drafts.length && !invalid.length;
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
  $('spAdvBody').querySelectorAll('tr[data-code]').forEach(tr => {
    const code = tr.dataset.code || '';
    tr.style.display = (!q || code.toUpperCase().includes(q)) ? '' : 'none';
  });
});

/* Discard drafts: back to the saved values. Never a request. */
function discardDrafts() {
  advRows().forEach(tr => {
    tr.querySelector('.adv-input').value = tr.dataset.saved;
    tr.querySelector('.adv-all').checked = false;
    refreshAdvRow(tr);
  });
  updateAdvState();
}
$('spAdvReset').onclick = discardDrafts;

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
      <td><bdi class="cr-id">${esc(c.course_code)}</bdi></td>
      <td><bdi>${esc(c.program)}</bdi></td>
      <td class="text-center">${esc(val(c.old, false))}</td>
      <td class="text-center"><strong>${esc(val(c.new, true))}</strong></td>
      <td>${esc(c.scope === 'all_programmes' ? TL.scopeAll : TL.scopeProgs)}</td>
    </tr>`).join('');
  return `<table class="sp-adv-table sp-limit-review">
      <thead><tr><th>${esc(TL.thCourse)}</th><th>${esc(TL.thProgram)}</th><th>${esc(TL.thOld)}</th><th>${esc(TL.thNew)}</th><th>${esc(TL.thScope)}</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
}

/* Preview, confirm, commit. Returns the server's answer to the commit, or null. */
async function reviewAndSave(changes, { title, intro, confirmText }) {
  const programs = advPrograms();
  if (!programs.length) { showStatus(TL.needProgram, 'warn'); return null; }
  const preview = await postLimits({ programs, changes, dry_run: true });
  if (!preview.res.ok || !preview.data.ok) { showStatus(limitError(preview.data, preview.res.status), 'err'); return null; }
  const planned = preview.data.changes || [];
  if (!planned.length) { showStatus(TL.nothing, 'warn'); return null; }
  const unchangedNote = preview.data.unchanged ? `<p class="fs-sm text-t3">${esc(TL.unchanged(preview.data.unchanged))}</p>` : '';
  const ok = await dlg.confirm({
    title: title(planned.length),
    body: `<p>${esc(intro)}</p>${limitTableHtml(planned)}${unchangedNote}`,
    kind: 'warning',
    confirmText: confirmText(planned.length),
    cancelText: TL.keepEditing,
  });
  if (!ok) return null;
  const commit = await postLimits({ programs, changes, dry_run: false, preview_token: preview.data.preview_token });
  if (!commit.res.ok || !commit.data.ok) { showStatus(limitError(commit.data, commit.res.status), 'err'); return null; }
  return commit.data;
}

/* Save drafts (the confirmed, audited write). */
$('spAdvSaveDb').onclick = async () => {
  if (invalidDrafts().length) { showStatus(TL.fixInvalid, 'err'); return; }
  const drafts = currentDrafts();
  if (!drafts.length) { showStatus(TL.nothing, 'warn'); return; }
  const btn = $('spAdvSaveDb');
  btn.disabled = true;
  btn.textContent = TL.saving;
  try {
    const changes = drafts.map(d => ({ course_code: d.code, max_capacity: d.value, all_programmes: d.all }));
    const saved = await reviewAndSave(changes, { title: TL.saveTitle, intro: TL.saveIntro, confirmText: TL.confirmSave });
    if (saved) {
      showStatus(TL.saved(saved.changed_count), 'ok');
      const keep = currentDraftInputs();
      changes.forEach(c => keep.delete(c.course_code));
      await loadAdvancedCourses(keep);
    }
  } catch (e) {
    showStatus(T.reqFailed + ': ' + e.message, 'err');
  } finally {
    btn.textContent = TL.saveBtn;
    updateAdvState();
  }
};

/* Remove one saved limit: explicit, confirmed, audited. Other drafts are kept. */
async function removeSavedLimit(tr) {
  const code = tr.dataset.code;
  const all = Boolean(tr.querySelector('.adv-all')?.checked);
  try {
    const removed = await reviewAndSave(
      [{ course_code: code, max_capacity: null, all_programmes: all }],
      { title: () => TL.removeTitle(code), intro: TL.removeIntro, confirmText: () => TL.confirmRemove },
    );
    if (removed) {
      showStatus(TL.saved(removed.changed_count), 'ok');
      const keep = currentDraftInputs();
      keep.delete(code);
      await loadAdvancedCourses(keep);
    }
  } catch (e) {
    showStatus(T.reqFailed + ': ' + e.message, 'err');
  }
}

window.addEventListener('beforeunload', e => {
  if (!currentDrafts().length) return;
  e.preventDefault();
  e.returnValue = TL.leave;
});

/* Reload defaults when global capacity settings change (drafts are kept) */
['spCapLocal4', 'spCapLocalOther', 'spCapExternal'].forEach(id => {
  $(id).addEventListener('change', () => {
    if (_advLoaded) loadAdvancedCourses();
  });
});

/* Reload when program input changes (courses are program-specific; drafts are kept) */
$('spProgram').addEventListener('change', () => {
  if (_advLoaded) loadAdvancedCourses();
});

/* ── Collect payload ── */
function getPayload() {
  const overrides = collectOverrides();
  const payload = {
    year:            parseInt($('spYear').value, 10) || 0,
    semester:        parseInt($('spSemester').value, 10) || 0,
    program:         $('spProgram').value.trim().toUpperCase(),
    section:         $('spSection').value.trim(),
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
  el.setAttribute('role', type === 'err' ? 'alert' : 'status');
  el.textContent = msg;
  el.classList.remove('d-none');
}
function hideStatus() {
  $('spStatus').classList.add('d-none');
}

/* ── Render results ── */
let _lastPayload = null;
/* "Our" departments come from the server (section_planning.LOCAL_DEPARTMENTS). */
const LOCAL_DEPTS = new Set((() => {
  try { return JSON.parse($('spLocalDepartments')?.textContent || '[]'); } catch (_) { return []; }
})());

function renderResults(data) {
  if (data.mode === 'multi') {
    renderMultiProgramResults(data);
  } else {
    renderSingleProgramResults(data);
  }
}

/* ── Build table rows HTML from a plan array ── */
function buildPlanRows(plan) {
  if (!plan.length) {
    return `<tr><td colspan="11" class="empty-note">${T.noRecs}</td></tr>`;
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
    const extBadge = row.is_external ? ` <span class="sp-pill sp-pill-ext">EXT</span>` : '';
    const programs = Array.isArray(row.programs) ? row.programs.filter(Boolean) : [];
    const programTags = programs.length
      ? `<span style="display:inline-flex;flex-wrap:wrap;gap:4px;margin-inline-end:6px;vertical-align:middle">${programs.map(p => `<span class="sp-pill sp-pill-ext">${esc(p)}</span>`).join('')}</span>`
      : '';
    const courseName = row.course_name || '';
    return `<tr>
      <td>${idx + 1}</td>
      <td><strong>${row.department}</strong></td>
      <td><span class="cr-id">${row.course_code}</span>${extBadge}</td>
      <td>${programTags}<span>${courseName}</span></td>
      <td class="text-center">${row.credit_hours}</td>
      <td class="text-center"><strong>${row.total_students}</strong></td>
      <td class="text-center"><strong>${row.num_sections}</strong></td>
      <td class="text-center">${row.max_per_section}</td>
      <td class="text-center">${row.avg_per_section}</td>
      <td style="min-width:80px">
        <div class="d-flex align-items-center gap-1">
          <div class="sp-fill-wrap"><div class="sp-fill ${fillCls}" style="width:${row.fill_percent}%"></div></div>
          <span class="fs-sm text-t3" style="min-width:30px">${row.fill_percent}%</span>
        </div>
      </td>
      <td>${statusHtml}</td>
    </tr>`;
  }).join('');
}

/* ── Build department summary HTML ── */
function buildDeptSummaryHtml(departments) {
  const depts = (departments || []).filter(d => LOCAL_DEPTS.has(d.department));
  if (!depts.length) return '';
  return depts.map(d => `
    <div class="sp-dept-card">
      <div class="dept-name">${d.department}</div>
      <div class="dept-stat"><b>${d.courses}</b> ${T.courses} · <b>${d.sections}</b> ${T.sections} · <b>${d.students}</b> ${T.students} · <b>${d.total_credits}</b> ${T.credits}</div>
    </div>
  `).join('');
}

/* ── Table header HTML (shared between single and multi) ── */
function buildTableHeaderHtml() {
  return `<tr>
    <th data-sort="num">#</th>
    <th data-sort="text">${IS_AR ? 'القسم' : 'Dept'}</th>
    <th data-sort="text">${IS_AR ? 'المقرر' : 'Course'}</th>
    <th data-sort="text">${IS_AR ? 'اسم المقرر' : 'Course Name'}</th>
    <th data-sort="num">${IS_AR ? 'ساعات' : 'Cr'}</th>
    <th data-sort="num">${IS_AR ? 'الطلاب' : 'Students'}</th>
    <th data-sort="num">${IS_AR ? 'الشعب' : 'Sections'}</th>
    <th data-sort="num">${IS_AR ? 'الحد الأقصى' : 'Max'}</th>
    <th data-sort="num">${IS_AR ? 'المتوسط' : 'Avg'}</th>
    <th>${IS_AR ? 'الامتلاء' : 'Fill'}</th>
    <th data-sort="text">${IS_AR ? 'الحالة' : 'Status'}</th>
  </tr>`;
}

/* ── Single-program mode (original behavior) ── */
function renderSingleProgramResults(data) {
  $('spResults').classList.remove('d-none');

  /* Show single table, hide multi container */
  $('spTable').style.display = '';
  $('spPager').style.display = '';
  $('spMultiPrograms').classList.add('d-none');
  $('spMultiPrograms').innerHTML = '';

  /* Show the single-mode dept summary panel */
  $('spDeptGrid').parentElement.style.display = '';

  /* KPIs */
  $('spKpiStudents').textContent = String(data.student_count ?? 0);
  $('spKpiCourses').textContent = String(data.summary.total_courses);
  $('spKpiSections').textContent = String(data.summary.total_sections);
  $('spKpiFill').textContent = data.summary.avg_fill_percent + '%';

  /* Timestamp */
  $('spTimestamp').textContent = T.lastUpdate + ': ' + new Date().toLocaleTimeString();

  /* Table */
  const tbody = $('spTable').querySelector('tbody');
  const plan = data.plan || [];

  if (!plan.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="empty-note">${T.noRecs}</td></tr>`;
    $('spDeptGrid').innerHTML = '';
    return;
  }

  tbody.innerHTML = buildPlanRows(plan);

  /* Wire sorting + pagination */
  if (typeof wireSortableTable === 'function') wireSortableTable('spTable');
  if (typeof paginateTable === 'function') paginateTable('spTable', 'spPager', 30);

  /* Department summary */
  $('spDeptGrid').innerHTML = buildDeptSummaryHtml(data.summary.departments);
}

/* ── Multi-program mode ── */
let _multiTableCounter = 0;

function renderMultiProgramResults(data) {
  $('spResults').classList.remove('d-none');

  /* Show combined union in the main table, show multi container for per-program */
  $('spTable').style.display = '';
  $('spPager').style.display = '';
  $('spDeptGrid').parentElement.style.display = '';
  const container = $('spMultiPrograms');
  container.classList.remove('d-none');
  container.innerHTML = '';

  /* KPIs from combined summary */
  const cs = data.combined_summary || {};
  $('spKpiStudents').textContent = String(data.student_count ?? 0);
  $('spKpiCourses').textContent = String(cs.total_courses || 0);
  $('spKpiSections').textContent = String(cs.total_sections || 0);
  $('spKpiFill').textContent = (cs.avg_fill_percent || 0) + '%';

  /* Timestamp */
  $('spTimestamp').textContent = T.lastUpdate + ': ' + new Date().toLocaleTimeString();

  /* ── Union table (main table) ── */
  const combinedPlan = data.combined_plan || [];
  const tbody = $('spTable').querySelector('tbody');
  if (!combinedPlan.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="empty-note">${T.noRecs}</td></tr>`;
    $('spDeptGrid').innerHTML = '';
  } else {
    tbody.innerHTML = buildPlanRows(combinedPlan);
    if (typeof wireSortableTable === 'function') wireSortableTable('spTable');
    if (typeof paginateTable === 'function') paginateTable('spTable', 'spPager', 30);
    $('spDeptGrid').innerHTML = buildDeptSummaryHtml(cs.departments);
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

    /* Collapsible heading — starts collapsed */
    const heading = document.createElement('h5');
    heading.className = 'sp-prog-heading sp-collapsible';
    heading.style.cursor = 'pointer';
    heading.style.userSelect = 'none';
    heading.innerHTML = `<span class="sp-collapse-arrow">▶</span>
      <span>${esc(prog.program)}</span>
      <span class="sp-prog-count">(${prog.student_count ?? 0} ${T.progLabel}
        · ${summary.total_courses || 0} ${T.courses}
        · ${summary.total_sections || 0} ${T.sections})</span>`;
    block.appendChild(heading);

    /* Collapsible body — hidden by default */
    const body = document.createElement('div');
    body.id = bodyId;
    body.className = 'd-none';

    /* Table */
    const table = document.createElement('table');
    table.className = 'tbl-card';
    table.id = tableId;
    table.setAttribute('role', 'table');
    table.innerHTML = `<thead>${buildTableHeaderHtml()}</thead><tbody>${buildPlanRows(plan)}</tbody>`;
    body.appendChild(table);

    /* Department summary for this program */
    const deptHtml = buildDeptSummaryHtml(summary.departments);
    if (deptHtml) {
      const deptPanel = document.createElement('div');
      deptPanel.className = 'sp-panel';
      deptPanel.style.marginTop = '8px';
      deptPanel.innerHTML = `<h6 class="mb-2" style="font-size:.82rem">${T.deptSummary}</h6>
        <div class="sp-dept-grid">${deptHtml}</div>`;
      body.appendChild(deptPanel);
    }

    block.appendChild(body);
    container.appendChild(block);

    /* Toggle collapse on click */
    heading.addEventListener('click', () => {
      const hidden = body.classList.toggle('d-none');
      heading.querySelector('.sp-collapse-arrow').textContent = hidden ? '▶' : '▼';
      if (!hidden && plan.length && typeof wireSortableTable === 'function') {
        wireSortableTable(tableId);
      }
    });
  });
}

/* ── Generate click ── */
$('spGenerate').onclick = async () => {
  const payload = getPayload();
  if (!payload.year || !payload.semester) {
    showStatus(T.fillAll, 'err');
    return;
  }

  const btn = $('spGenerate');
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

    const data = await res.json();

    if (!res.ok || !data.ok) {
      showStatus(data.error || T.reqFailed, 'err');
      return;
    }

    _lastPayload = payload;
    renderResults(data);
    showStatus(T.done, 'ok');
    if (typeof notify !== 'undefined') notify.success(T.done);

  } catch (err) {
    showStatus(T.reqFailed + ': ' + err.message, 'err');
  } finally {
    hideProgress();
    btn.disabled = false;
    btn.textContent = IS_AR ? 'حساب' : 'Generate';
  }
};

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
      showStatus(errData.error || T.reqFailed, 'err');
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
  $('spSection').value = '';
  $('spResults').classList.add('d-none');
  hideStatus();
  _lastPayload = null;

  /* Restore single-mode table visibility */
  $('spTable').style.display = '';
  $('spPager').style.display = '';
  $('spTable').querySelector('tbody').innerHTML =
    `<tr><td colspan="11" class="empty-note">${IS_AR ? 'حدد السنة والفصل ثم انقر حساب.' : 'Set Year & Semester, then click Generate.'}</td></tr>`;
  $('spDeptGrid').innerHTML = '';
  $('spDeptGrid').parentElement.style.display = '';

  /* Clear multi-program container */
  $('spMultiPrograms').innerHTML = '';
  $('spMultiPrograms').classList.add('d-none');

  /* Drop drafts (never saved limits), and show the panel for the cleared scope. */
  discardDrafts();
  if (_advLoaded) loadAdvancedCourses(new Map());
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
