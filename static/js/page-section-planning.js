/* ── Section Planning — client-side logic ── */
const IS_AR = 'LANGUAGE_CODE' === 'ar';
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

/* ── Advanced per-course overrides ── */
let _advCourses = [];     // cached course list from server
let _advLoaded = false;   // loaded at least once?

let _advProgram = '';  // program(s) when panel was last loaded

$('spToggleAdv').onclick = () => {
  const panel = $('spAdvPanel');
  const isHidden = panel.classList.contains('d-none');
  panel.classList.toggle('d-none');
  if (isHidden) {
    const prog = $('spProgram').value.trim().toUpperCase();
    if (!_advLoaded || prog !== _advProgram) loadAdvancedCourses();
  }
};

async function loadAdvancedCourses() {
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
    renderAdvancedTable(_advCourses);
    $('spAdvCount').textContent = _advCourses.length + (IS_AR ? ' مقرر' : ' courses');
  } catch (err) {
    showStatus(T.reqFailed + ': ' + err.message, 'err');
  }
}

function renderAdvancedTable(courses) {
  const tbody = $('spAdvBody');
  if (!courses.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-note text-center" style="padding:20px">${
      IS_AR ? 'لا توجد مقررات — أدخل البرنامج أولاً.' : 'No courses found — enter a Program first.'}</td></tr>`;
    return;
  }
  tbody.innerHTML = courses.map(c => {
    const saved = c.programme_max;
    const dispVal = saved != null ? saved : '';
    return `<tr data-code="${c.course_code}">
    <td><span class="cr-id">${c.course_code}</span></td>
    <td>${c.department}</td>
    <td class="text-center">${c.credit_hours}</td>
    <td class="text-center">${c.is_external ? '✓' : ''}</td>
    <td class="adv-default text-center">${c.default_max}</td>
    <td class="text-center"><input type="text" inputmode="numeric" pattern="[0-9]*"
        class="form-control form-control-compact adv-input"
        value="${dispVal}"
        placeholder="${c.default_max}"
        data-code="${c.course_code}" data-default="${c.default_max}"></td>
  </tr>`;
  }).join('');
  /* Wire input change → badge + auto-save to DB */
  tbody.querySelectorAll('.adv-input').forEach(inp => {
    inp.addEventListener('input', updateAdvBadge);
    inp.addEventListener('change', () => saveCapacityToDB(inp));
  });
}

/* Save a single course capacity to DB for all selected programs */
async function saveCapacityToDB(inp) {
  const prog = _advProgram;
  if (!prog) return;  // no program → nothing to save
  const programs = prog.includes(',')
    ? prog.split(',').map(p => p.trim()).filter(Boolean)
    : [prog];
  const code = inp.dataset.code;
  const raw = inp.value.trim();
  const cap = raw ? parseInt(raw, 10) : null;
  if (raw && (isNaN(cap) || cap < 1)) return;  // invalid → skip
  try {
    const res = await fetch('/ops/section-planning/save-capacity/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ programs, course_code: code, max_capacity: cap }),
    });
    const data = await res.json();
    if (data.ok) {
      /* Brief green flash to confirm save */
      inp.style.borderColor = 'var(--teal)';
      setTimeout(() => { inp.style.borderColor = ''; }, 800);
    }
  } catch (_) { /* silent — user will see no flash if save fails */ }
}

function updateAdvBadge() {
  const count = getOverrideCount();
  const badge = $('spAdvBadge');
  if (count > 0) {
    badge.textContent = count;
    badge.classList.remove('d-none');
  } else {
    badge.classList.add('d-none');
  }
}

function getOverrideCount() {
  let n = 0;
  $('spAdvBody').querySelectorAll('.adv-input').forEach(inp => {
    const val = inp.value.trim();
    if (val && parseInt(val, 10) > 0) n++;
  });
  return n;
}

function collectOverrides() {
  const overrides = {};
  $('spAdvBody').querySelectorAll('.adv-input').forEach(inp => {
    const val = inp.value.trim();
    const v = parseInt(val, 10);
    if (val && v > 0) {
      overrides[inp.dataset.code] = v;
    }
  });
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

/* Reset all overrides */
$('spAdvReset').onclick = () => {
  $('spAdvBody').querySelectorAll('.adv-input').forEach(inp => { inp.value = ''; });
  updateAdvBadge();
};

/* Save overrides to DB */
$('spAdvSaveDb').onclick = async () => {
  const inputs = $('spAdvBody').querySelectorAll('.adv-input');
  const overrides = {};
  inputs.forEach(inp => {
    const val = inp.value.trim();
    if (val && parseInt(val) > 0) {
      overrides[inp.dataset.code] = parseInt(val);
    }
  });

  const count = Object.keys(overrides).length;
  if (count === 0) {
    showStatus(IS_AR ? 'لا يوجد تخصيصات للحفظ' : 'No overrides to save', 'warn');
    return;
  }

  const btn = $('spAdvSaveDb');
  btn.disabled = true;
  btn.textContent = IS_AR ? 'جارٍ الحفظ...' : 'Saving...';

  try {
    const res = await fetch('/ops/section-planning/save-overrides-bulk/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF },
      body: JSON.stringify({ overrides }),
    });
    const data = await res.json();
    if (data.ok) {
      showStatus(IS_AR ? `تم حفظ ${data.courses} تخصيص في قاعدة البيانات` : `${data.courses} overrides saved to DB (${data.updated} rows updated)`, 'ok');
    } else {
      showStatus(data.error || 'Failed', 'err');
    }
  } catch (e) {
    showStatus(e.message, 'err');
  }

  btn.disabled = false;
  btn.textContent = IS_AR ? 'حفظ في قاعدة البيانات' : 'Save to DB';
};

/* Reload defaults when global capacity settings change */
['spCapLocal4', 'spCapLocalOther', 'spCapExternal'].forEach(id => {
  $(id).addEventListener('change', () => {
    if (_advLoaded) loadAdvancedCourses();
  });
});

/* Reload when program input changes (courses are program-specific) */
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
  el.className = type === 'ok' ? 'sp-alert sp-alert-ok' : 'sp-alert sp-alert-err';
  el.textContent = msg;
  el.classList.remove('d-none');
}
function hideStatus() {
  $('spStatus').classList.add('d-none');
}

/* ── Render results ── */
let _lastPayload = null;
const CS_DEPTS = new Set(['AI', 'DS', 'CS', 'IS', 'CYB', 'COE']);

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
  const depts = (departments || []).filter(d => CS_DEPTS.has(d.department));
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

  /* Reset advanced overrides */
  $('spAdvBody').querySelectorAll('.adv-input').forEach(inp => { inp.value = ''; });
  updateAdvBadge();
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
