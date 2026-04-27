/*
 * Exam Timetable Builder — client-side logic.
 *
 * Flow:
 *   1. Page load     → fetch filter chips (programs, sections) + history
 *   2. Load Courses  → POST filters → render course chips grouped by dept/level
 *   3. Build         → POST config → render KPI cards, schedule grid, conflict matrix
 *   4. Interact      → filter schedule, drag-pin courses, click KPI drilldowns
 *   5. History       → load/delete saved runs
 *
 * Global state:
 *   _coursesLoaded   – whether the course preview is populated
 *   _pinnedCourses   – {course_code: {course_code, day, period}} drag-pin overrides
 *   _currentRunId    – ID of the currently viewed run (for export link)
 *   _creditMap       – {course_code: credit_hours} for chip labels
 *   _drillData       – {overload: [], heavy: []} detail records for KPI drilldown
 *   _programCourses  – {programName: Set(course_codes)} for programme-based filtering
 */
const IS_AR = 'LANGUAGE_CODE' === 'ar';
const T = {
  ready:      IS_AR ? 'جاهز. حدد الفلاتر ثم انقر تحميل المقررات.' : 'Ready. Select filters then click Load Courses.',
  building:   IS_AR ? 'جارٍ بناء الجدول...' : 'Building timetable...',
  done:       IS_AR ? 'تم بناء الجدول بنجاح.' : 'Timetable built successfully.',
  error:      IS_AR ? 'خطأ' : 'Error',
  fillAll:    IS_AR ? 'يرجى ملء جميع الحقول.' : 'Please fill in all fields.',
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
  noCourses:      IS_AR ? 'لا توجد مقررات مطابقة للفلاتر المحددة.' : 'No courses match the selected filters.',
  selectAll:      IS_AR ? 'تحديد الكل' : 'Select all',
  deselectAll:    IS_AR ? 'إلغاء تحديد الكل' : 'Deselect all',
  selectComputer: IS_AR ? 'تحديد الحاسوبية' : 'Select computer',
  selectGeneral:  IS_AR ? 'تحديد العامة' : 'Select general',
  selectOnline:   IS_AR ? 'تحديد الإلكترونية' : 'Select online',
  deselectOnline: IS_AR ? 'إلغاء تحديد الإلكترونية' : 'Deselect online',
  loadFirst:      IS_AR ? 'يرجى تحميل المقررات أولاً.' : 'Please load courses first.',
  noSelected:     IS_AR ? 'يرجى اختيار مقرر واحد على الأقل.' : 'Please select at least one course.',
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

/* ── Day label generator ── */
const WORK_DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'];

function generateDayLabels() {
  const startDay = $('etStartDay').value;
  const count    = parseInt($('etNumDays').value, 10) || 5;
  const startIdx = WORK_DAYS.indexOf(startDay);
  if (startIdx === -1 || count < 1) return [];

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

function updateDayPreview() {
  $('dayPreview').textContent = generateDayLabels().join(', ');
}

$('etStartDay').addEventListener('change', updateDayPreview);
$('etNumDays').addEventListener('input', updateDayPreview);
updateDayPreview();

/* ── Structured period repeater ── */
function syncPeriodsHidden() {
  const rows = document.querySelectorAll('#etPeriodsRepeater .et-period-row');
  const vals = [];
  rows.forEach(r => {
    const s = r.querySelector('.et-period-start')?.value;
    const e = r.querySelector('.et-period-end')?.value;
    if (s && e) vals.push(`${s}-${e}`);
  });
  $('etPeriods').value = vals.join(',');
}

function addPeriodRow(startVal, endVal) {
  const row = document.createElement('div');
  row.className = 'et-period-row d-flex align-items-center gap-1';
  row.innerHTML =
    `<input type="text" inputmode="numeric" pattern="\\d{2}:\\d{2}" placeholder="HH:MM" class="form-control form-control-compact et-period-start" value="${startVal || ''}">` +
    `<span class="et-period-sep">–</span>` +
    `<input type="text" inputmode="numeric" pattern="\\d{2}:\\d{2}" placeholder="HH:MM" class="form-control form-control-compact et-period-end" value="${endVal || ''}">` +
    `<button type="button" class="btn btn-sm btn-outline-secondary et-period-remove et-period-remove-btn" title="${IS_AR ? 'إزالة' : 'Remove'}">&times;</button>`;
  $('etPeriodsRepeater').appendChild(row);
  row.querySelector('.et-period-remove').addEventListener('click', () => { row.remove(); syncPeriodsHidden(); });
  row.querySelectorAll('input').forEach(inp => inp.addEventListener('change', syncPeriodsHidden));
}

/* Wire existing remove buttons & inputs */
document.querySelectorAll('#etPeriodsRepeater .et-period-remove').forEach(btn => {
  btn.addEventListener('click', () => { btn.closest('.et-period-row').remove(); syncPeriodsHidden(); });
});
document.querySelectorAll('#etPeriodsRepeater input[type="text"]').forEach(inp => {
  inp.addEventListener('change', syncPeriodsHidden);
});

$('etAddPeriod')?.addEventListener('click', () => addPeriodRow('', ''));

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
    `<label class="et-chip active"><input type="checkbox" value="${v}" checked>${v}</label>`
  ).join('');
  box.querySelectorAll('.et-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const cb = chip.querySelector('input');
      cb.checked = !cb.checked;
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

async function loadFilters() {
  try {
    const res = await fetch('/ops/exam-timetable/filters/', {
      headers: { 'X-CSRFToken': CSRF },
    });
    const data = await res.json();
    if (!data.ok) return;
    renderChips('progList', data.programs ?? [], 'progCount');
    renderChips('secList', data.sections ?? [], 'secCount');
  } catch (err) {
    notify.error(IS_AR ? 'فشل تحميل الفلاتر' : 'Failed to load filters', err.message || String(err));
  }
}
loadFilters();

/* ── Step 1: Load Courses (preview) ── */
let _coursesLoaded = false;

const COMPUTER_PREFIXES = new Set(['AI', 'DS', 'CS', 'IS', 'COE', 'CYB']);

function clearCoursePreview() {
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
}

function renderCourseChips(courses) {
  const box = $('courseList');
  if (!courses.length) {
    box.innerHTML = `<small class="text-secondary">${T.noCourses}</small>`;
    $('courseCount').textContent = '';
    $('toggleAllCourses').textContent = '';
    $('buildBtn').disabled = true;
    _coursesLoaded = false;
    return;
  }

  // Group by department prefix, then sub-group by level (first digit)
  const deptMap = {};
  for (const c of courses) {
    const dept = c.course_code.match(/^[A-Za-z]+/)?.[0] || '?';
    if (!deptMap[dept]) deptMap[dept] = {};
    const level = c.course_code.match(/\d/)?.[0] || '0';
    if (!deptMap[dept][level]) deptMap[dept][level] = [];
    deptMap[dept][level].push(c);
  }

  const deptKeys = Object.keys(deptMap).sort();
  let html = '';
  for (const dept of deptKeys) {
    const levels = Object.keys(deptMap[dept]).sort();
    const deptTotal = levels.reduce((s, l) => s + deptMap[dept][l].length, 0);
    html += `<div class="et-dept-group" data-dept="${dept}">`;
    html += `<div class="et-dept-header">`;
    html += `<span class="dept-name">${dept}</span>`;
    html += `<span class="dept-count">(${deptTotal})</span>`;
    html += `<a href="#" class="dept-toggle">${T.deselectAll}</a>`;
    html += `</div><div class="et-dept-chips">`;
    for (const level of levels) {
      html += `<div class="et-level-row">`;
      html += `<span class="et-level-badge">L${level}</span>`;
      for (const c of deptMap[dept][level]) {
        const crLabel = c.credit_hours ? ` | ${c.credit_hours}cr` : '';
        const onlineMark = c.is_online ? ' <span class="et-online-dot" title="online" aria-label="online">●</span>' : '';
        html += `<label class="et-chip active"><input type="checkbox" value="${c.course_code}" checked data-online="${c.is_online ? '1' : '0'}">${c.course_code}${onlineMark} <small class="opacity-75">(${c.enrolled_count}${crLabel})</small></label>`;
      }
      html += `</div>`;
    }
    html += `</div></div>`;
  }

  box.innerHTML = html;

  // Chip click handlers
  box.querySelectorAll('.et-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const cb = chip.querySelector('input');
      cb.checked = !cb.checked;
      chip.classList.toggle('active', cb.checked);
      updateCourseCount(courses.length);
      updateDeptToggleLabel(chip.closest('.et-dept-group'));
    });
  });

  // Per-department toggle handlers
  box.querySelectorAll('.dept-toggle').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const group = link.closest('.et-dept-group');
      const cbs = group.querySelectorAll('input[type=checkbox]');
      const checked = group.querySelectorAll('input:checked');
      const newState = checked.length < cbs.length;
      cbs.forEach(cb => {
        cb.checked = newState;
        cb.closest('.et-chip').classList.toggle('active', newState);
      });
      updateCourseCount(courses.length);
      updateDeptToggleLabel(group);
    });
  });

  _coursesLoaded = true;
  $('buildBtn').disabled = false;
  $('selectComputerCourses').textContent = T.selectComputer;
  $('selectGeneralCourses').textContent = T.selectGeneral;
  // Online toggle only appears when at least one course is flagged online,
  // otherwise it'd be no-op confusion. Label flips between Select/Deselect
  // based on the current online-checked state.
  const hasOnline = courses.some(c => c.is_online);
  if (hasOnline) {
    updateOnlineToggleLabel();
  } else {
    $('toggleOnlineCourses').textContent = '';
  }
  document.querySelectorAll('.select-sep').forEach(s => s.classList.remove('d-none'));
  updateCourseCount(courses.length);
  updateToggleLabel();
  // Init per-dept labels
  box.querySelectorAll('.et-dept-group').forEach(updateDeptToggleLabel);
}

function updateDeptToggleLabel(group) {
  if (!group) return;
  const all = group.querySelectorAll('input[type=checkbox]');
  const checked = group.querySelectorAll('input:checked');
  const link = group.querySelector('.dept-toggle');
  if (link) link.textContent = checked.length === all.length ? T.deselectAll : T.selectAll;
  updateToggleLabel();
}

function updateCourseCount(total) {
  const checked = $('courseList').querySelectorAll('input:checked').length;
  $('courseCount').textContent = checked === total
    ? (IS_AR ? `(الكل ${total})` : `(all ${total})`)
    : `(${checked}/${total})`;
  updateToggleLabel();
  updateOnlineToggleLabel();
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
  $('courseList').querySelectorAll('.et-dept-group').forEach(updateDeptToggleLabel);
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
  $('courseList').querySelectorAll('.et-dept-group').forEach(updateDeptToggleLabel);
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
    link.textContent = '';
    return;
  }
  const online = $('courseList').querySelectorAll('input[type=checkbox][data-online="1"]');
  if (!online.length) {
    link.textContent = '';
    return;
  }
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
  $('courseList').querySelectorAll('.et-dept-group').forEach(updateDeptToggleLabel);
  updateOnlineToggleLabel();
});

// Enable / disable the thin-threshold input alongside its toggle
$('etRelaxThin').addEventListener('change', () => {
  $('etThinThreshold').disabled = !$('etRelaxThin').checked;
});

// Clear course list when filters change
['progList', 'secList'].forEach(id => {
  $(id).addEventListener('click', () => { if (_coursesLoaded) clearCoursePreview(); });
});

$('loadCoursesBtn').addEventListener('click', async () => {
  const programs = getCheckedValues('progList');
  const sections = getCheckedValues('secList');

  $('loadCoursesBtn').disabled = true;
  $('etStatus').textContent = T.loadingCourses;
  $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';

  try {
    const res = await fetch('/ops/exam-timetable/preview-courses/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ programs, sections }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || T.reqFailed);

    const courses = data.courses ?? [];
    $('coursePreview').classList.remove('d-none');
    renderCourseChips(courses);

    if (courses.length > 0) {
      $('etStatus').textContent = T.coursesLoaded.replace('{n}', courses.length);
      $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';
    } else {
      $('etStatus').textContent = T.noCourses;
      $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    }
  } catch (err) {
    $('etStatus').textContent = T.error + ': ' + err.message;
    $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
  } finally {
    $('loadCoursesBtn').disabled = false;
  }
});

/* ── Step 2: Build Timetable ── */
$('buildBtn').addEventListener('click', async () => {
  if (!_coursesLoaded) {
    $('etStatus').textContent = T.loadFirst;
    $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    return;
  }

  const selectedCourses = getCheckedValues('courseList');
  if (!selectedCourses.length) {
    $('etStatus').textContent = T.noSelected;
    $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    return;
  }

  const label      = $('etLabel').value.trim();
  const perStr     = $('etPeriods').value.trim();
  const maxPerDay  = parseInt($('etMaxPerDay').value, 10) || 2;
  const days       = generateDayLabels();
  const programs   = getCheckedValues('progList');
  const sections   = getCheckedValues('secList');

  if (!label || !perStr || !days.length) {
    $('etStatus').textContent = T.fillAll;
    $('etStatus').className = 'alert alert-warning mt-2 py-2 mb-0';
    return;
  }

  const periods = perStr.split(',').map(s => s.trim()).filter(Boolean);
  const randomize = $('etRandomize').checked;
  const relaxThin = $('etRelaxThin').checked;
  let thinThreshold = 0;
  if (relaxThin) {
    const raw = parseInt($('etThinThreshold').value, 10);
    let n = isNaN(raw) ? 0 : raw;
    n = Math.max(0, Math.min(10, n));
    if (n < 1) {
      // Toggle is ON but input is 0/empty/invalid — auto-correct to the
      // default rather than silently posting 0 (which would build with
      // no relaxation despite the toggle saying otherwise).
      n = 4;
      $('etThinThreshold').value = '4';
    }
    thinThreshold = n;
  }

  $('buildBtn').disabled = true;
  $('etStatus').textContent = T.building;
  $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';

  try {
    const res = await fetch('/ops/exam-timetable/build/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ label, days, periods, max_per_day: maxPerDay, programs, sections, selected_courses: selectedCourses, pinned: Object.keys(_pinnedCourses).length ? Object.values(_pinnedCourses) : undefined, randomize, thin_conflict_threshold: thinThreshold }),
    });
    const data = await res.json();
    if (!data.ok) {
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
      throw new Error(data.error || T.reqFailed);
    }

    renderResults(data);
    updatePinBar();
    $('etStatus').textContent = T.done;
    $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';
    loadHistory();
  } catch (err) {
    $('etStatus').textContent = T.error + ': ' + err.message;
    $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
  } finally {
    $('buildBtn').disabled = false;
  }
});

/* ── Program → courses mapping (for schedule filter) ── */
// Built from buckets_summary in each build result.  Allows the schedule
// filter to accept a programme name (e.g. "AI") and highlight all its courses.
let _programCourses = {};   // { programName: Set([course_code, ...]) }

/* ── Room assignments per grid cell (for renderScheduleGrid) ── */
// Indexed by "day||period||course" → array of room codes.  Populated in
// renderResult() from data.schedule[].rooms so the grid can show the
// rooms beneath each course chip without re-walking the schedule.
let _roomsByEntry = {};

/* ── Pinned courses (drag & drop) ── */
let _pinnedCourses = {};    // { course_code: { course_code, day, period } }

/* ── Current run ID (for export) ── */
let _currentRunId = null;

/* ── Credit map (for chip labels) ── */
let _creditMap = {};        // { course_code: credit_hours }

/* ── KPI drilldown data ── */
let _drillData = { overload: [], heavy: [], 'thin-courses': [], 'thin-clash': [] };
let _slotsByIndex = {};

function closeDrill() {
  $('kpiDrill').classList.add('d-none');
  document.querySelectorAll('.kpi-click.active').forEach(el => el.classList.remove('active'));
}

// Render helpers — each returns {title, head, body, colspan} for openDrill
const _drillRenderers = {
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
        <td>${r.courses.map(c =>
          `<span class="drill-course-chip" style="background:${colorForCourse(c.code)};border:1px solid ${colorForCourseBorder(c.code)}">${c.code}${c.credits != null ? ` <small class="et-credit-tag">${c.credits}cr</small>` : ''}</span>`
        ).join('')}</td>
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
          <td>${r.courses.map(c =>
            `<span class="drill-course-chip" style="background:${colorForCourse(c.code)};border:1px solid ${colorForCourseBorder(c.code)}">${c.code} <small class="et-credit-tag">${c.credits}cr</small></span>`
          ).join('')}</td>
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
        <td><span class="drill-course-chip" style="background:${colorForCourse(r.course_code)};border:1px solid ${colorForCourseBorder(r.course_code)}"><strong>${r.course_code}</strong></span></td>
        <td>${r.total_students}</td>
        <td>${r.dropped_edges}</td>
        <td>${(r.neighbours || []).map(c =>
          `<span class="drill-course-chip" style="background:${colorForCourse(c)};border:1px solid ${colorForCourseBorder(c)}">${c}</span>`
        ).join('')}</td>
      </tr>`).join(''),
      colspan: 4,
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
        <td>${(r.courses || []).map(c =>
          `<span class="drill-course-chip" style="background:${colorForCourse(c)};border:1px solid ${colorForCourseBorder(c)}">${c}</span>`
        ).join('')}</td>
      </tr>`).join(''),
      colspan: 3,
    };
  },
};

function openDrill(type) {
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
  document.querySelectorAll('.kpi-click.active').forEach(el => el.classList.remove('active'));
  const card = document.querySelector(`.kpi-click[data-drill="${type}"]`);
  if (card) card.classList.add('active');
  panel.dataset.type = type;

  const r = renderer(rows);
  $('kpiDrillTitle').textContent = r.title;
  $('kpiDrillHead').innerHTML = r.head;
  $('kpiDrillBody').innerHTML = rows.length
    ? r.body
    : `<tr><td colspan="${r.colspan}" class="text-center text-secondary py-3">${IS_AR ? 'لا توجد بيانات' : 'No records'}</td></tr>`;
  panel.classList.remove('d-none');
}

// Click handlers for KPI cards
document.addEventListener('click', (e) => {
  const card = e.target.closest('.kpi-click');
  if (card) {
    const type = card.dataset.drill;
    if (type) openDrill(type);
    return;
  }
});

$('kpiDrillClose').addEventListener('click', closeDrill);

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
function renderResults(data) {
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
  $('kEdges').textContent    = data.conflicts_count ?? 0;
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

  // Bucket KPIs
  $('kBuckets').textContent = data.qa?.bucket_count ?? data.bucket_count ?? 0;
  const bv = data.qa?.bucket_day_violations_count ?? 0;
  $('kBucketViol').textContent = bv;
  $('kBucketViol').className   = 'v' + (bv > 0 ? ' warn' : '');

  // Credit KPIs
  _creditMap = data.credit_map ?? {};
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

  // v3 telemetry: building-footprint card — display-only, no ranking effect.
  // Hidden when the payload lacks footprint data (legacy v1/v2 rows or
  // older v3 rows that didn't capture building info per room entry).
  const footprint = data.qa?.building_footprint ?? {};
  const footprintSummary = footprint.largest_slot_footprint_summary ?? '';
  const hasFootprint = !!footprintSummary;
  if (hasFootprint) {
    $('kFootprintRow').classList.remove('d-none');
    $('kFootprintLargest').textContent = footprintSummary;
  } else {
    $('kFootprintRow').classList.add('d-none');
  }

  // v3 telemetry: enrolment-snapshot integrity card — answers
  // "why does this exported schedule differ from what I expected?".
  // Hidden when the payload lacks snapshot data (pre-v3 builds).
  const snap = data.qa?.enrolment_snapshot ?? {};
  const snapHash = snap.source_hash ?? '';
  if (snapHash) {
    $('kSnapshotRow').classList.remove('d-none');
    // Render timestamp as YYYY-MM-DD HH:MM (no seconds) for readability.
    const tsRaw = String(snap.snapshot_timestamp ?? '');
    let tsDisplay = tsRaw;
    if (tsRaw) {
      const dt = new Date(tsRaw);
      if (!Number.isNaN(dt.getTime())) {
        const pad = (n) => String(n).padStart(2, '0');
        tsDisplay = `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} `
          + `${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
      }
    }
    $('kSnapshotTime').textContent = tsDisplay || '—';
    $('kSnapshotSections').textContent = snap.sections_count ?? 0;
    const fb = !!snap.fallback_used;
    $('kSnapshotFallback').textContent = fb
      ? (IS_AR ? 'نعم' : 'Yes')
      : (IS_AR ? 'لا' : 'No');
    $('kSnapshotFallback').className = 'v' + (fb ? ' warn' : '');
    // Show only the first 12 hex chars + ellipsis — full hash is in payload.
    $('kSnapshotHash').textContent = snapHash.slice(0, 12) + '…';
    $('kSnapshotHash').title = snapHash;
  } else {
    $('kSnapshotRow').classList.add('d-none');
  }

  // Index rooms-per-entry for the grid renderer: "day||period||course" → [codes]
  _roomsByEntry = {};
  for (const e of (data.schedule || [])) {
    if (e.day === 'OVERFLOW') continue;
    const codes = (e.rooms || [])
      .map(a => a.room_code)
      .filter(c => c && c !== 'UNASSIGNED');
    if (codes.length) {
      _roomsByEntry[`${e.day}||${e.period}||${e.course_code}`] = codes;
    }
  }

  // Build slot lookup so the thin-clash drill can show day/period for
  // each slot_index instead of a bare integer.
  _slotsByIndex = {};
  for (const s of (data.slots || [])) {
    _slotsByIndex[s.index] = s;
  }

  // Store drilldown data + close any open panel
  _drillData = {
    overload:       data.qa?.overload_details ?? [],
    heavy:          data.qa?.heavy_day_details ?? [],
    'thin-courses': data.qa?.thin_courses ?? [],
    'thin-clash':   data.qa?.thin_clash_risk ?? [],
  };
  closeDrill();

  // QA Warnings
  const conflicts = data.qa?.same_slot_conflicts ?? [];
  const bucketViols = data.qa?.bucket_day_violations ?? [];
  const hasWarnings = conflicts.length > 0 || bucketViols.length > 0;

  if (hasWarnings) {
    $('qaWarnings').classList.remove('d-none');
    let warningHtml = '';
    warningHtml += conflicts.map(c =>
      `<div class="alert alert-danger py-1 px-2 mb-1 small">${
        T.sameSlot
          .replace('{sid}', c.student_id)
          .replace('{n}', c.courses.length)
          .replace('{slot}', c.slot_index)
          .replace('{courses}', c.courses.join(', '))
      }</div>`
    ).join('');
    warningHtml += bucketViols.map(v =>
      `<div class="alert alert-warning py-1 px-2 mb-1 small">${
        T.bucketViol
          .replace('{prog}', v.program)
          .replace('{term}', v.programme_term)
          .replace('{courses}', v.courses.join(', '))
          .replace('{day}', v.day)
      }</div>`
    ).join('');
    $('qaBody').innerHTML = warningHtml;
  } else {
    $('qaWarnings').classList.add('d-none');
  }

  // Schedule grid
  const schedule = data.schedule ?? [];
  const slots    = data.slots ?? [];
  renderScheduleGrid(schedule, slots);

  // Conflict matrix
  renderConflictMatrix(data.conflicts ?? [], data.courses ?? []);
}

/* Color helpers (_isDark, colorForCourse, colorForCourseBorder) now in shared-utils.js */

/* ── Render schedule as day×period grid ── */
// Builds an HTML table: rows = days, columns = periods, cells = course chips.
// Each chip is draggable for pin-to-slot; pinned courses get .et-pinned class.
// Courses that couldn't be placed appear in a red OVERFLOW row at the bottom.
function renderScheduleGrid(schedule, slots) {
  const container = $('schedGrid');
  if (!schedule.length) {
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
  let html = '<table class="et-grid">';

  // Header: corner + periods
  html += '<thead><tr>';
  html += `<th>${IS_AR ? 'اليوم' : 'Day'}</th>`;
  for (const p of periodOrder) {
    html += `<th>${p}</th>`;
  }
  html += '</tr></thead>';

  // Body: one row per day
  html += '<tbody>';
  for (const day of dayOrder) {
    html += '<tr>';
    html += `<th>${day}</th>`;
    for (const period of periodOrder) {
      const courses = (grid[day] && grid[day][period]) ? grid[day][period] : [];
      if (courses.length === 0) {
        html += `<td class="et-empty" data-day="${day}" data-period="${period}">—</td>`;
      } else {
        const chips = courses.map(c => {
          const pinCls = _pinnedCourses[c] ? ' et-pinned' : '';
          const cr = _creditMap[c];
          const crTag = cr ? ` <small class="et-credit-tag">${cr}cr</small>` : '';
          const roomCodes = _roomsByEntry[`${day}||${period}||${c}`] || [];
          const roomTag = roomCodes.length
            ? `<div class="et-room-tag" title="${roomCodes.join(', ')}">${roomCodes.join(' · ')}</div>`
            : '';
          return `<span class="et-course${pinCls}" data-course="${c}" draggable="true" style="background:${colorForCourse(c)};border:1px solid ${colorForCourseBorder(c)}">${c}${crTag}${roomTag}</span>`;
        }).join(' ');
        html += `<td data-day="${day}" data-period="${period}">${chips}</td>`;
      }
    }
    html += '</tr>';
  }

  // Overflow row (if any)
  if (overflowCourses.length > 0) {
    html += `<tr class="et-overflow-row">`;
    html += `<th class="et-overflow-label">${T.overflow}</th>`;
    const chips = overflowCourses.map(c => {
      const cr = _creditMap[c];
      const crTag = cr ? ` <small class="et-credit-tag">${cr}cr</small>` : '';
      return `<span class="et-course" data-course="${c}" draggable="true" style="background:${colorForCourse(c)};border:1px solid ${colorForCourseBorder(c)}">${c}${crTag}</span>`;
    }).join(' ');
    html += `<td colspan="${periodOrder.length}">${chips}</td>`;
    html += '</tr>';
  }

  html += '</tbody></table>';
  container.innerHTML = html;
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
      return (code) => code.includes(term);
    }
  }

  // Support multiple space-separated terms (OR logic)
  const tokens = raw.split(/\s+/).filter(Boolean);
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

/* ── Drag & Drop Pin ── */
// Users can drag a course chip from one grid cell to another to "pin" it.
// Pinned courses are fixed to that slot on the next rebuild — the scheduler
// places them first, then schedules everything else around them.
// State is held in `_pinnedCourses` (course_code → {course_code, day, period}).
// • Drop    → records pin + moves chip visually (instant feedback)
// • Dblclick → unpins a single course
// • Clear   → removes all pins
function updatePinBar() {
  const n = Object.keys(_pinnedCourses).length;
  $('pinBar').classList.toggle('d-none', n === 0);
  $('pinCount').textContent = T.pinCount.replace('{n}', n);
  // Update build button text
  $('buildBtn').textContent = n > 0
    ? T.rebuild.replace('{n}', n)
    : (IS_AR ? 'بناء الجدول' : 'Build Timetable');
}

$('schedGrid').addEventListener('dragstart', (e) => {
  const chip = e.target.closest('.et-course');
  if (!chip) return;
  e.dataTransfer.setData('text/plain', chip.dataset.course);
  e.dataTransfer.effectAllowed = 'move';
});

$('schedGrid').addEventListener('dragover', (e) => {
  const td = e.target.closest('td');
  if (!td || !td.dataset.day) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  // Highlight target cell
  $('schedGrid').querySelectorAll('.et-drag-over').forEach(el => el.classList.remove('et-drag-over'));
  td.classList.add('et-drag-over');
});

$('schedGrid').addEventListener('dragleave', (e) => {
  const td = e.target.closest('td');
  if (td) td.classList.remove('et-drag-over');
});

$('schedGrid').addEventListener('drop', (e) => {
  e.preventDefault();
  $('schedGrid').querySelectorAll('.et-drag-over').forEach(el => el.classList.remove('et-drag-over'));
  const td = e.target.closest('td');
  if (!td || !td.dataset.day) return;
  const cc = e.dataTransfer.getData('text/plain');
  if (!cc) return;

  const day = td.dataset.day;
  const period = td.dataset.period;

  // Record pin
  _pinnedCourses[cc] = { course_code: cc, day, period };

  // Move chip visually
  const chip = $('schedGrid').querySelector(`.et-course[data-course="${cc}"]`);
  if (chip) {
    // Remove empty marker from source cell if it becomes empty
    const srcCell = chip.parentElement;
    chip.remove();
    if (srcCell && srcCell.tagName === 'TD' && !srcCell.querySelector('.et-course')) {
      srcCell.className = 'et-empty';
      srcCell.textContent = '\u2014';
    }
    // Add to target cell
    td.classList.remove('et-empty');
    if (td.textContent === '\u2014') td.textContent = '';
    chip.classList.add('et-pinned');
    td.appendChild(chip);
  }

  updatePinBar();
});

// Double-click to unpin
$('schedGrid').addEventListener('dblclick', (e) => {
  const chip = e.target.closest('.et-course.et-pinned');
  if (!chip) return;
  const cc = chip.dataset.course;
  delete _pinnedCourses[cc];
  chip.classList.remove('et-pinned');
  updatePinBar();
});

// Clear all pins
$('clearPins').addEventListener('click', (e) => {
  e.preventDefault();
  _pinnedCourses = {};
  $('schedGrid').querySelectorAll('.et-pinned').forEach(c => c.classList.remove('et-pinned'));
  updatePinBar();
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
  const adj = {};
  for (const c of courses) adj[c] = {};
  for (const e of conflicts) {
    adj[e.course_a][e.course_b] = e.shared;
    adj[e.course_b][e.course_a] = e.shared;
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

  // Header row: empty corner + rotated course labels
  html += '<thead><tr><th></th>';
  for (const c of courses) {
    html += `<th><span>${c}</span></th>`;
  }
  html += '</tr></thead>';

  // Body: one row per course
  html += '<tbody>';
  for (let i = 0; i < courses.length; i++) {
    const rowCourse = courses[i];
    html += `<tr><th>${rowCourse}</th>`;
    for (let j = 0; j < courses.length; j++) {
      const colCourse = courses[j];
      if (i === j) {
        html += '<td class="cm-diag">\u00b7</td>';
      } else {
        const n = adj[rowCourse]?.[colCourse] ?? 0;
        const cls = cmClass(n);
        const label = n > 0 ? n : '';
        const tooltip = n > 0
          ? `${rowCourse} \u2194 ${colCourse}: ${n} ${T.students}`
          : `${rowCourse} \u2194 ${colCourse}: 0`;
        html += `<td class="${cls}" title="${tooltip}">${label}</td>`;
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
        const filter = $('schedFilter');
        filter.value = courseCode;
        filter.dispatchEvent(new Event('input'));
        $('schedGrid').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    });
  }
}

/* ── Toggle conflict matrix ── */
$('toggleMatrix').addEventListener('click', () => {
  const el = $('conflictMatrix');
  const show = el.classList.contains('d-none');
  el.classList.toggle('d-none', !show);
  $('toggleMatrix').textContent = show ? T.hide : T.show;
});

/* ── History ── */
let _historyPage = 1;

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function loadHistory(page) {
  if (page !== undefined) _historyPage = page;
  try {
    const res = await fetch(`/ops/exam-timetable/list/?page=${_historyPage}`, {
      headers: { 'X-CSRFToken': CSRF },
    });
    const data = await res.json();
    if (!data.ok) return;

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
      return `<div class="et-history-item" data-id="${r.id}">
        <div class="et-run-info">
          <strong>${r.label}</strong> <small class="text-secondary">${dt}</small>
        </div>
        <button class="et-del-btn" data-id="${r.id}" data-label="${r.label}" title="${IS_AR ? 'حذف' : 'Delete'}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        </button>
      </div>`;
    }).join('');

    // Click on run info → load run
    $('historyList').querySelectorAll('.et-run-info').forEach(el => {
      el.addEventListener('click', () => loadRun(el.closest('.et-history-item').dataset.id));
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
    notify.error(IS_AR ? 'فشل تحميل السجل' : 'Failed to load history', err.message || String(err));
  }
}

function renderHistoryPagination(pages) {
  const wrap = $('historyPages');
  let html = `<button class="pg-btn" onclick="loadHistory(${_historyPage-1})" ${_historyPage<=1?'disabled':''}>‹</button>`;
  for (let i = 1; i <= pages; i++) {
    if (pages > 7 && i > 2 && i < pages - 1 && Math.abs(i - _historyPage) > 1) {
      if (i === 3 || i === pages - 2) html += '<span class="et-pagination-ellipsis">…</span>';
      continue;
    }
    html += `<button class="pg-btn ${i===_historyPage?'active':''}" onclick="loadHistory(${i})">${i}</button>`;
  }
  html += `<button class="pg-btn" onclick="loadHistory(${_historyPage+1})" ${_historyPage>=pages?'disabled':''}>›</button>`;
  wrap.innerHTML = html;
}

async function deleteRun(runId, label) {
  const ok = await dlg.confirm({
    title: T.deleteRun,
    body: T.deleteRunBody + `<p style="margin-top:6px;"><strong>${label}</strong></p>`,
    icon: 'danger',
    confirmLabel: T.deleteConfirm,
    confirmClass: 'danger',
  });
  if (!ok) return;

  try {
    const res = await fetch(`/ops/exam-timetable/${runId}/delete/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ confirm: 'DELETE' }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || T.reqFailed);

    notify.success(T.deleted);

    // If the deleted run was the currently loaded one, hide the results panel.
    // We hide (not clear innerHTML) so the DOM elements remain for the next build.
    if (String(_currentRunId) === String(runId)) {
      _currentRunId = null;
      $('etResults').classList.add('d-none');
      $('etStatus').textContent = T.ready;
      $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';
      const exportBtn = $('exportXlsx');
      if (exportBtn) { exportBtn.classList.add('d-none'); exportBtn.removeAttribute('href'); }
    }

    loadHistory();
  } catch (err) {
    notify.error(T.deleteFailed, err.message);
  }
}

// Load a previously saved run from the database and render it (read-only view).
async function loadRun(runId) {
  $('etStatus').textContent = T.loadingRun;
  $('etStatus').className = 'alert alert-info mt-2 py-2 mb-0';

  try {
    const res = await fetch(`/ops/exam-timetable/${runId}/`, {
      headers: { 'X-CSRFToken': CSRF },
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || T.reqFailed);

    _pinnedCourses = {};        // clear pins when viewing a saved run
    renderResults(data);
    updatePinBar();
    $('etLabel').value = data.label ?? '';
    $('etStatus').textContent = T.done;
    $('etStatus').className = 'alert alert-success mt-2 py-2 mb-0';

    // Highlight active
    $('historyList').querySelectorAll('.et-history-item').forEach(el => {
      el.classList.toggle('active', el.dataset.id === String(runId));
    });
  } catch (err) {
    $('etStatus').textContent = T.error + ': ' + err.message;
    $('etStatus').className = 'alert alert-danger mt-2 py-2 mb-0';
  }
}

// Load history on page load
loadHistory();

/* ── Export Excel click feedback ── */
$('exportXlsx')?.addEventListener('click', function() {
  if (this.href && this.href !== '#') {
    notify.success(IS_AR ? 'جارٍ تحميل ملف إكسل...' : 'Downloading Excel file...');
  }
});

/* ── Matrix Zoom Controls ── */
(function() {
  let zoom = 1.0;
  const scaler = $('etMatrixScaler');
  const level  = $('etZoomLevel');
  if (!scaler || !level) return;

  function updateZoom() {
    scaler.style.transform = 'scale(' + zoom + ')';
    level.textContent = Math.round(zoom * 100) + '%';
  }

  $('etZoomIn')?.addEventListener('click', () => {
    zoom = Math.min(2.0, +(zoom + 0.1).toFixed(2));
    updateZoom();
  });

  $('etZoomOut')?.addEventListener('click', () => {
    zoom = Math.max(0.5, +(zoom - 0.1).toFixed(2));
    updateZoom();
  });

  $('etZoomFit')?.addEventListener('click', () => {
    const viewport = $('etMatrixViewport');
    if (!viewport || !scaler) return;
    const vw = viewport.clientWidth;
    const sw = scaler.scrollWidth / zoom;
    zoom = Math.min(1.5, Math.max(0.5, +(vw / sw).toFixed(2)));
    updateZoom();
  });
})();

/* ── Matrix Fullscreen Toggle ── */
/* ── Matrix Fullscreen Toggle ── */
(function() {
  let originalParent = null;
  let originalNext = null;

  $('matrixFullscreen')?.addEventListener('click', () => {
    const panel = $('matrixPanel');
    const matrix = $('conflictMatrix');
    const btn = $('matrixFullscreen');
    if (!panel) return;
    const entering = !panel.classList.contains('matrix-fullscreen');

    if (entering) {
      // Save original position in DOM
      originalParent = panel.parentElement;
      originalNext = panel.nextElementSibling;
      // Move to body to escape stacking contexts (content-wrap has transform)
      document.body.appendChild(panel);
      panel.classList.add('matrix-fullscreen');
      matrix?.classList.remove('d-none');
      document.body.style.overflow = 'hidden';
    } else {
      panel.classList.remove('matrix-fullscreen');
      document.body.style.overflow = '';
      // Restore to original DOM position
      if (originalParent) {
        if (originalNext) originalParent.insertBefore(panel, originalNext);
        else originalParent.appendChild(panel);
      }
    }

    btn.innerHTML = entering ? '&#x2716;' : '&#x26F6;';
    btn.title = entering ? 'Exit fullscreen' : 'Fullscreen';

    if (entering) {
      const onEsc = (e) => {
        if (e.key === 'Escape') { btn.click(); document.removeEventListener('keydown', onEsc); }
      };
      document.addEventListener('keydown', onEsc);
    }
  });
})();
