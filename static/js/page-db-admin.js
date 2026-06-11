const IS_AR = document.documentElement.lang === 'ar';
const T = {
  runPreviewFirst: IS_AR ? 'يرجى تشغيل تحليل + معاينة أولاً.' : 'Please run Parse + Preview first.',
  requestFailed:  IS_AR ? 'فشل الطلب' : 'Request failed',
  confirmDelete:  IS_AR ? 'تأكيد الحذف؟' : 'Confirm Delete?',
};

/* ── Delete helper — delegates to typed dlg.confirm ── */
function handleDeleteWithConfirm(btn, deleteFn) {
  deleteFn();
}

/* ── Nav switching ── */
const navItems = document.querySelectorAll('.dba-nav-item[data-panel]');
const panels   = document.querySelectorAll('.dba-panel');

navItems.forEach(item => {
  item.addEventListener('click', () => {
    const target = item.dataset.panel;
    navItems.forEach(n => n.classList.remove('active'));
    panels.forEach(p => p.classList.remove('active'));
    item.classList.add('active');
    const panel = document.getElementById('panel-' + target);
    if (panel) panel.classList.add('active');
  });
});

/* ── Helpers ── */
const pretty = (data) => JSON.stringify(data, null, 2);

function writeOut(id, data) {
  const el = q(id);
  if (!el) return;
  el.textContent = pretty(data);
  el.classList.remove('has-error', 'has-success');
  if (data && data.error)   el.classList.add('has-error');
  if (data && data.message && !data.error) el.classList.add('has-success');
}

async function callJson(url, options = {}, outId = null, btn = null) {
  if (btn) { btn.disabled = true; btn._prevText = btn.textContent; btn.textContent = IS_AR ? 'جارٍ التحميل...' : 'Loading...'; }
  const method = (options.method || 'GET').toUpperCase();
  if (['POST','PUT','PATCH','DELETE'].includes(method)) {
    options.headers = Object.assign({ 'X-CSRFToken': getCsrfToken() }, options.headers || {});
  }
  try {
    const r = await fetch(url, options);
    let data;
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json') || ct.includes('text/json')) {
      data = await r.json();
    } else {
      const body = await r.text();
      const snippet = body.replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim().slice(0,300);
      data = { error: `HTTP ${r.status} ${r.statusText}${snippet ? ': ' + snippet : ''}` };
    }
    if (outId) writeOut(outId, data);
    return data;
  } catch (err) {
    const data = { error: T.requestFailed, details: String(err || '') };
    if (outId) writeOut(outId, data);
    return data;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btn._prevText || btn.textContent; }
  }
}

let termPreviewReady = false;

/* ── Helpers: enable / disable delete buttons ── */
function enableDeleteBtn(btn) {
  btn.disabled = false;
  btn.removeAttribute('aria-disabled');
}
function disableDeleteBtn(btn) {
  btn.disabled = true;
  btn.setAttribute('aria-disabled', 'true');
  /* also cancel any pending double-confirm state */
  if (btn.dataset.confirming === 'true') {
    btn.dataset.confirming = 'false';
    btn.classList.remove('dba-delete-confirm');
    if (btn._origHtml) btn.innerHTML = btn._origHtml;
    clearTimeout(btn._confirmTimer);
  }
}

/* ── Section: Delete students ── */
q('sPreview').onclick = async () => {
  const u = `/ops/db/preview-delete-students/?program=${encodeURIComponent(q('sProgram').value)}&section=${encodeURIComponent(q('sSection').value)}`;
  const data = await callJson(u, {}, 'sOut', q('sPreview'));
  /* Enable delete only when preview returns matching students */
  if (data && !data.error && data.students_count > 0) {
    enableDeleteBtn(q('sDelete'));
  } else {
    disableDeleteBtn(q('sDelete'));
  }
};

/* Re-disable delete when filter inputs change after a preview */
['sProgram', 'sSection'].forEach(id => {
  q(id).addEventListener('input', () => disableDeleteBtn(q('sDelete')));
});

q('sDelete').onclick = () => {
  handleDeleteWithConfirm(q('sDelete'), async () => {
    const okS = await dlg.confirm({
      title: IS_AR ? 'حذف الطلاب؟' : 'Delete students?',
      body: IS_AR
        ? '<p>سيحذف هذا جميع الطلاب المطابقين وسجلات <code>student_courses</code>.</p>'
        : '<p>This will permanently delete all matching students and their <code>student_courses</code> records.</p>',
      typed: 'DELETE',
      confirmText: IS_AR ? 'حذف نهائي' : 'Delete permanently',
      kind: 'danger',
    });
    if (!okS) return;
    await callJson('/ops/db/delete-students/', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ program: q('sProgram').value, section: q('sSection').value, confirm: 'DELETE' })
    }, 'sOut', q('sDelete'));
    /* Re-disable after delete — previewed data no longer valid */
    disableDeleteBtn(q('sDelete'));
  });
};

/* ── Section: Delete catalog ── */
q('pPreview').onclick = async () => {
  const u = `/ops/db/preview-delete-program-catalog/?program=${encodeURIComponent(q('pProgram').value)}`;
  const data = await callJson(u, {}, 'pOut', q('pPreview'));
  /* Enable delete only when preview returns matching results */
  if (data && !data.error && (data.requirements_count > 0 || data.prerequisites_count > 0)) {
    enableDeleteBtn(q('pDelete'));
  } else {
    disableDeleteBtn(q('pDelete'));
  }
};

/* Re-disable delete when filter input changes after a preview */
q('pProgram').addEventListener('input', () => disableDeleteBtn(q('pDelete')));

q('pDelete').onclick = () => {
  handleDeleteWithConfirm(q('pDelete'), async () => {
    const prog = (q('pProgram').value || '').trim().toUpperCase();
    const okP = await dlg.confirm({
      title: IS_AR ? `حذف كتالوج ${prog}؟` : `Delete program catalog?`,
      body: IS_AR
        ? `<p>سيحذف هذا كتالوج البرنامج بالكامل لـ <strong>${esc(prog)}</strong>. لا يمكن التراجع عن هذا.</p>`
        : `<p>This will permanently delete all requirements and prerequisites for <strong>${esc(prog)}</strong>.</p><p>This cannot be undone.</p>`,
      typed: `DELETE ${prog}`,
      confirmText: IS_AR ? 'حذف الكتالوج' : 'Delete catalog',
      kind: 'danger',
    });
    if (!okP) return;
    await callJson('/ops/db/delete-program-catalog/', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ program: q('pProgram').value, confirm: 'DELETE' })
    }, 'pOut', q('pDelete'));
    /* Re-disable after delete — previewed data no longer valid */
    disableDeleteBtn(q('pDelete'));
  });
};

/* ── Section: Import program plan ── */
q('iImport').onclick = async () => {
  await callJson('/ops/db/import-program-plan/', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ program: q('iProgram').value, csv_text: q('iCsv').value, replace_existing: q('iReplace').checked })
  }, 'iOut', q('iImport'));
};

/* ── Section: Term sections ── */
function setTermStep(step) {
  ['tStep1','tStep2','tStep3'].forEach((id, i) => {
    const el = q(id);
    if (!el) return;
    el.classList.remove('active','done');
    if (i + 1 < step)     el.classList.add('done');
    else if (i + 1 === step) el.classList.add('active');
  });
}

q('tPreview').onclick = async () => {
  setTermStep(2);
  const data = await callJson('/ops/db/preview-term-sections/', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      csv_path:     q('tCsvPath').value,
      academic_year: q('tYear').value,
      term:          q('tTerm').value,
      is_department: q('tDept').checked
    })
  }, 'tOut', q('tPreview'));

  const body = q('tTableBody');
  body.innerHTML = '';
  if (!data.error && Array.isArray(data.preview_rows)) {
    for (const row of data.preview_rows) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${esc(row.course_code||'')}</td><td>${esc(row.course_number||'')}</td><td>${esc(row.section||'')}</td><td>${esc(row.day||'')}</td><td>${esc(row.start_time||'')}</td><td>${esc(row.end_time||'')}</td><td>${esc(row.room||'')}</td><td>${esc(row.instructor||'')}</td><td><span class="badge text-bg-secondary">${esc(row.source_tag||'')}</span></td>`;
      body.appendChild(tr);
    }
    q('tTableWrap').classList.remove('d-none');
    termPreviewReady = true;
    q('tImport').disabled = false;
    setTermStep(3);
  } else {
    q('tTableWrap').classList.add('d-none');
    termPreviewReady = false;
    q('tImport').disabled = true;
    setTermStep(1);
  }
};

q('tImport').onclick = async () => {
  if (!termPreviewReady) { notify.warning(T.runPreviewFirst); return; }
  const ok = await dlg.confirm({
    title: IS_AR ? 'إدراج الصفوف المصفّاة؟' : 'Insert normalised rows?',
    body: IS_AR
      ? '<p>سيُدرج هذا جميع الصفوف المعروضة في المعاينة في قاعدة البيانات.</p>'
      : '<p>This will insert all rows shown in the preview into the database.</p>',
    confirmText: IS_AR ? 'إدراج الكل' : 'Insert all',
    kind: 'info',
  });
  if (!ok) return;
  await callJson('/ops/db/import-term-sections/', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      csv_path:         q('tCsvPath').value,
      academic_year:    q('tYear').value,
      term:             q('tTerm').value,
      is_department:    q('tDept').checked,
      truncate_existing: q('tTruncate').checked
    })
  }, 'tOut', q('tImport'));
};

/* ── Section: Oracle plan import ── */
let oraclePreviewReady = false;

const ORACLE_TYPE_OPTIONS = [
  'Mandatory', 'Free Elective', 'Program Elective', 'University Elective', 'Elective'
];

function setOracleStep(step) {
  ['oStep1','oStep2','oStep3'].forEach((id, i) => {
    const el = q(id);
    if (!el) return;
    el.classList.remove('active','done');
    if (i + 1 < step)     el.classList.add('done');
    else if (i + 1 === step) el.classList.add('active');
  });
}

function oracleTypeSelect(val) {
  let html = '<select class="form-select form-select-sm">';
  for (const opt of ORACLE_TYPE_OPTIONS) {
    html += `<option value="${esc(opt)}"${opt === val ? ' selected' : ''}>${esc(opt)}</option>`;
  }
  html += '</select>';
  return html;
}

function oracleAddRow(row, idx) {
  const tr = document.createElement('tr');
  const isOnline = parseInt(row.is_online || 0, 10);
  tr.innerHTML =
    `<td class="text-muted">${idx}</td>` +
    `<td><input class="form-control form-control-sm o-level" value="${esc(String(row.level_number || ''))}"></td>` +
    `<td><input class="form-control form-control-sm o-code" value="${esc(row.code || '')}"></td>` +
    `<td><input class="form-control form-control-sm o-name" value="${esc(row.en_name || '')}"></td>` +
    `<td><input class="form-control form-control-sm o-credits" value="${esc(String(row.credits || ''))}"></td>` +
    `<td class="o-type-cell">${oracleTypeSelect(row.type || 'Mandatory')}</td>` +
    `<td class="text-center"><input class="form-check-input o-online" type="checkbox"${isOnline ? ' checked' : ''}></td>` +
    `<td><input class="form-control form-control-sm o-prereqs" value="${esc(row.prereqs_str || '')}"></td>` +
    `<td><button class="btn btn-sm btn-outline-danger o-del-row" title="${IS_AR ? 'حذف' : 'Delete'}">&times;</button></td>`;
  return tr;
}

function collectOracleRows() {
  const rows = [];
  q('oTableBody').querySelectorAll('tr').forEach(tr => {
    rows.push({
      level_number: tr.querySelector('.o-level')?.value || '',
      code:         tr.querySelector('.o-code')?.value || '',
      en_name:      tr.querySelector('.o-name')?.value || '',
      credits:      tr.querySelector('.o-credits')?.value || '',
      type:         tr.querySelector('.o-type-cell select')?.value || 'Mandatory',
      is_online:    tr.querySelector('.o-online')?.checked ? 1 : 0,
      prereqs_str:  tr.querySelector('.o-prereqs')?.value || '',
    });
  });
  return rows;
}

function renderOracleSummary(data) {
  const s = data.summary || {};
  const m = data.metadata || {};
  const db = data.existing_db || {};
  const w = data.warnings || [];
  const kpis = [
    { label: IS_AR ? 'المقررات' : 'Courses',  value: s.total_courses || 0, color: '#0d9488' },
    { label: IS_AR ? 'الساعات'  : 'Credits',  value: s.total_credits || 0, color: '#6366f1' },
    { label: IS_AR ? 'المستويات': 'Levels',   value: s.total_levels  || 0, color: '#f59e0b' },
    { label: IS_AR ? 'التخصص'   : 'Major',    value: m.major_ar || '—',    color: '#8b5cf6' },
    { label: IS_AR ? 'صفوف موجودة' : 'Existing Reqs', value: db.requirements || 0, color: '#64748b' },
    { label: IS_AR ? 'متطلبات موجودة' : 'Existing Prereqs', value: db.prerequisites || 0, color: '#64748b' },
  ];
  let html = '';
  for (const k of kpis) {
    html += `<div class="col-md-2 col-4"><div class="border rounded p-2"><div class="fw-bold" style="font-size:1.3rem; color:${k.color}">${esc(String(k.value))}</div><div class="text-muted fs-sm">${esc(k.label)}</div></div></div>`;
  }
  if (w.length > 0) {
    html += `<div class="col-12"><div class="alert alert-warning py-1 px-2 mb-0 fs-md"><strong>${IS_AR ? 'تحذيرات:' : 'Warnings:'}</strong> ${esc(w.join(' | '))}</div></div>`;
  }
  q('oSummary').innerHTML = html;
  q('oSummaryWrap').classList.remove('d-none');
}

q('oPreview').onclick = async () => {
  const fileInput = q('oFile');
  if (!fileInput.files.length) { notify.warning(IS_AR ? 'يرجى اختيار ملف.' : 'Please select a file.'); return; }
  setOracleStep(2);
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  fd.append('program', q('oProgram').value);
  fd.append('encoding', q('oEncoding').value);
  const data = await callJson('/ops/db/preview-oracle-plan/', {
    method:'POST',
    body: fd,
  }, 'oOut', q('oPreview'));

  const body = q('oTableBody');
  body.innerHTML = '';

  if (!data.error && Array.isArray(data.preview_rows)) {
    renderOracleSummary(data);
    data.preview_rows.forEach((row, i) => {
      body.appendChild(oracleAddRow(row, i + 1));
    });
    q('oTableWrap').classList.remove('d-none');
    oraclePreviewReady = true;
    q('oImport').disabled = false;
    setOracleStep(3);
  } else {
    q('oTableWrap').classList.add('d-none');
    q('oSummaryWrap').classList.add('d-none');
    oraclePreviewReady = false;
    q('oImport').disabled = true;
    setOracleStep(1);
  }
};

/* Delegate delete row clicks */
q('oTableBody').addEventListener('click', (e) => {
  if (e.target.classList.contains('o-del-row')) {
    e.target.closest('tr').remove();
    /* re-number rows */
    q('oTableBody').querySelectorAll('tr').forEach((tr, i) => {
      tr.querySelector('td').textContent = i + 1;
    });
  }
});

/* Add empty row */
q('oAddRow').onclick = () => {
  const body = q('oTableBody');
  const idx = body.querySelectorAll('tr').length + 1;
  body.appendChild(oracleAddRow({level_number:'',code:'',en_name:'',credits:'',type:'Mandatory',is_online:0,prereqs_str:''}, idx));
};

q('oImport').onclick = async () => {
  if (!oraclePreviewReady) { notify.warning(T.runPreviewFirst); return; }
  const rows = collectOracleRows();
  if (rows.length === 0) { notify.warning(IS_AR ? 'لا توجد صفوف للإدراج.' : 'No rows to import.'); return; }
  const ok = await dlg.confirm({
    title: IS_AR ? 'إدراج خطة Oracle؟' : 'Import Oracle plan?',
    body: IS_AR
      ? `<p>سيُدرج هذا <strong>${rows.length}</strong> مقرر في قاعدة البيانات للبرنامج <strong>${esc(q('oProgram').value)}</strong>.</p>`
      : `<p>This will insert <strong>${rows.length}</strong> courses into the database for program <strong>${esc(q('oProgram').value)}</strong>.</p>`,
    confirmText: IS_AR ? 'إدراج الكل' : 'Import all',
    kind: 'info',
  });
  if (!ok) return;
  await callJson('/ops/db/import-oracle-plan/', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      program:          q('oProgram').value,
      rows:             rows,
      replace_existing: q('oReplace').checked,
    })
  }, 'oOut', q('oImport'));
};

/* ── Section: Legacy import ── */
q('lImport').onclick = async () => {
  const ok = await dlg.confirm({
    title: IS_AR ? 'تشغيل الاستيراد القديم؟' : 'Run legacy exact import?',
    body: IS_AR
      ? '<p>يعكس هذا المنطق القديم وقد يُدخل صفوف مكوّنات سابقة مكررة.</p>'
      : '<p>This mirrors the old loader logic and <strong>may insert duplicate prerequisite rows</strong>.</p><p>Run a preview first if unsure.</p>',
    confirmText: IS_AR ? 'تشغيل الاستيراد' : 'Run import',
    kind: 'warning',
  });
  if (!ok) return;
  await callJson('/ops/db/import-legacy-exact/', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ requirements_csv_path: q('lReqPath').value, prerequisites_csv_path: q('lPrePath').value })
  }, 'lOut', q('lImport'));
};

/* ── Section: System Defaults ── */
let defaultsLoaded = false;

async function loadDefaults() {
  const data = await callJson('/ops/settings/defaults/', {}, 'dOut');
  if (!data.error) {
    q('dYear').value = data.academic_year || '';
    q('dTerm').value = String(data.term || 1);
    q('dCurYear').value = data.currentYear || '';
    q('dCurTerm').value = String(data.currentTerm || 1);
    defaultsLoaded = true;
    writeOut('dOut', {
      message: IS_AR
        ? `الإعدادات الحالية: السنة = ${data.academic_year}, الفصل = ${data.term}, السنة الحالية = ${data.currentYear}, الفصل الحالي = ${data.currentTerm}`
        : `Current defaults: Year = ${data.academic_year}, Term = ${data.term}, Current Year = ${data.currentYear}, Current Term = ${data.currentTerm}`
    });
  }
}

/* Auto-load defaults when the panel becomes active */
navItems.forEach(item => {
  item.addEventListener('click', () => {
    if (item.dataset.panel === 'defaults' && !defaultsLoaded) loadDefaults();
  });
});

q('dSave').onclick = async () => {
  const yr = parseInt(q('dYear').value, 10);
  const tm = parseInt(q('dTerm').value, 10);
  const cYr = parseInt(q('dCurYear').value, 10);
  const cTm = parseInt(q('dCurTerm').value, 10);
  if (!yr || yr < 1400 || yr > 1600) {
    writeOut('dOut', { error: IS_AR ? 'السنة الأكاديمية يجب أن تكون بين 1400 و 1600.' : 'Academic year must be between 1400 and 1600.' });
    return;
  }
  if (!cYr || cYr < 1400 || cYr > 1600) {
    writeOut('dOut', { error: IS_AR ? 'السنة الحالية يجب أن تكون بين 1400 و 1600.' : 'Current year must be between 1400 and 1600.' });
    return;
  }
  await callJson('/ops/settings/defaults/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ academic_year: yr, term: tm, currentYear: cYr, currentTerm: cTm })
  }, 'dOut', q('dSave'));
};

/* ── Section: Maintenance ── */
q('bSnapshot').onclick = async () => {
  await callJson('/ops/db/backup-snapshot/', { method:'POST' }, 'bOut', q('bSnapshot'));
};
q('bIntegrity').onclick = async () => {
  await callJson('/ops/db/integrity-report/', {}, 'bOut', q('bIntegrity'));
};

/* ── Section: External Courses ── */
let extData = [];

function extRender() {
  const body = q('extBody');
  body.innerHTML = '';
  for (const c of extData) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><input type="checkbox" class="ext-chk" data-id="${c.course_id}"></td><td><strong>${esc(c.course_code||'')}</strong></td><td>${esc(c.department||'')}</td><td>${esc(c.description||'')}</td><td>${c.credit_hours||0}</td><td>${c.student_count||0}</td>`;
    body.appendChild(tr);
  }
  q('extTableWrap').classList.toggle('d-none', extData.length === 0);
  q('extDeleteAll').classList.toggle('d-none', extData.length === 0);
  q('extDeleteSel').classList.add('d-none');
  q('extCount').textContent = IS_AR ? `${extData.length} مادة خارجية` : `${extData.length} external course(s)`;
  q('extCheckAll').checked = false;
  body.querySelectorAll('.ext-chk').forEach(chk => {
    chk.addEventListener('change', () => {
      const anyChecked = body.querySelector('.ext-chk:checked');
      q('extDeleteSel').classList.toggle('d-none', !anyChecked);
    });
  });
}

q('extCheckAll').addEventListener('change', function() {
  q('extBody').querySelectorAll('.ext-chk').forEach(chk => { chk.checked = this.checked; });
  q('extDeleteSel').classList.toggle('d-none', !this.checked || extData.length === 0);
});

q('extLoad').onclick = async () => {
  const data = await callJson('/ops/db/external-courses/', {}, 'extOut', q('extLoad'));
  if (data && Array.isArray(data.items)) {
    extData = data.items;
    extRender();
  }
};

q('extDeleteAll').onclick = () => {
  handleDeleteWithConfirm(q('extDeleteAll'), async () => {
    const ok = await dlg.confirm({
      title: IS_AR ? 'حذف جميع المواد الخارجية؟' : 'Delete all external courses?',
      body: IS_AR
        ? '<p>سيحذف جميع المواد الخارجية وسجلات الطلاب المرتبطة بها. سيتم إنشاء نسخة احتياطية أولاً.</p>'
        : '<p>This will delete all external courses and their associated student records. A backup will be created first.</p>',
      typed: 'DELETE',
      confirmText: IS_AR ? 'حذف الكل' : 'Delete all',
      kind: 'danger',
    });
    if (!ok) return;
    const res = await callJson('/ops/db/delete-external-courses/', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ confirm: 'DELETE' })
    }, 'extOut', q('extDeleteAll'));
    if (res && res.ok) { extData = []; extRender(); }
  });
};

q('extDeleteSel').onclick = () => {
  handleDeleteWithConfirm(q('extDeleteSel'), async () => {
    const ids = [...q('extBody').querySelectorAll('.ext-chk:checked')].map(c => +c.dataset.id);
    if (!ids.length) return;
    const ok = await dlg.confirm({
      title: IS_AR ? `حذف ${ids.length} مادة؟` : `Delete ${ids.length} course(s)?`,
      body: IS_AR
        ? '<p>سيحذف المواد المحددة وسجلات الطلاب المرتبطة بها.</p>'
        : '<p>This will delete the selected external courses and their associated student records.</p>',
      typed: 'DELETE',
      confirmText: IS_AR ? 'حذف المحدد' : 'Delete selected',
      kind: 'danger',
    });
    if (!ok) return;
    const res = await callJson('/ops/db/delete-external-courses/', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ confirm: 'DELETE', course_ids: ids })
    }, 'extOut', q('extDeleteSel'));
    if (res && res.ok) {
      extData = extData.filter(c => !ids.includes(c.course_id));
      extRender();
    }
  });
};

/* ── Section: Programme Capacities ── */
let capRows = [];

function capRender() {
  const body = q('capBody');
  body.innerHTML = '';
  for (const row of capRows) {
    const tr = document.createElement('tr');
    const curCap = row.max_capacity != null ? row.max_capacity : '';
    const displayCap = row.max_capacity != null ? row.max_capacity : '--';
    tr.innerHTML =
      `<td><strong>${esc(row.course_code)}</strong></td>` +
      `<td>${row.credit_hours != null ? row.credit_hours : '--'}</td>` +
      `<td>${esc(String(displayCap))}</td>` +
      `<td><input class="form-control form-control-sm cap-input" type="number" min="1" data-code="${esc(row.course_code)}" value="${esc(String(curCap))}" placeholder="--"></td>`;
    body.appendChild(tr);
  }
  q('capTableWrap').classList.toggle('d-none', capRows.length === 0);
  q('capSave').classList.toggle('d-none', capRows.length === 0);
  q('capCount').textContent = IS_AR
    ? `${capRows.length} مقرر`
    : `${capRows.length} course(s)`;
}

q('capLoad').onclick = async () => {
  const program = (q('capProgram').value || '').trim().toUpperCase();
  if (!program) {
    writeOut('capOut', { error: IS_AR ? 'رمز البرنامج مطلوب.' : 'Program code is required.' });
    return;
  }
  const data = await callJson(
    `/ops/db/programme-capacities/?program=${encodeURIComponent(program)}`,
    {}, 'capOut', q('capLoad')
  );
  if (data && !data.error && Array.isArray(data.rows)) {
    capRows = data.rows;
    capRender();
    if (capRows.length === 0) {
      writeOut('capOut', {
        error: IS_AR
          ? `لا توجد مقررات للبرنامج "${program}".`
          : `No courses found for program "${program}".`
      });
    } else {
      writeOut('capOut', {
        message: IS_AR
          ? `تم تحميل ${capRows.length} مقرر للبرنامج "${program}".`
          : `Loaded ${capRows.length} course(s) for program "${program}".`
      });
    }
  }
};

q('capSave').onclick = async () => {
  const program = (q('capProgram').value || '').trim().toUpperCase();
  if (!program) {
    writeOut('capOut', { error: IS_AR ? 'رمز البرنامج مطلوب.' : 'Program code is required.' });
    return;
  }
  const capacities = {};
  q('capBody').querySelectorAll('.cap-input').forEach(inp => {
    const code = inp.dataset.code;
    const val = inp.value.trim();
    capacities[code] = val === '' ? null : parseInt(val, 10);
  });
  const data = await callJson('/ops/db/update-programme-capacities/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ program: program, capacities: capacities })
  }, 'capOut', q('capSave'));
  if (data && data.ok) {
    writeOut('capOut', {
      message: IS_AR
        ? `تم تحديث ${data.updated} صف بنجاح.`
        : `Successfully updated ${data.updated} row(s).`
    });
    /* Reload to reflect saved values */
    q('capLoad').click();
  }
};

/* ── Elective Catalogue ─────────────────────────────────────── */
const elecImport = q('elecImportBtn');
const elecRefresh = q('elecRefreshBtn');

if (elecImport) {
  elecImport.onclick = async () => {
    const programme = q('elecProgramme').value.trim().toUpperCase();
    const content = q('elecContent').value.trim();
    const result = q('elecImportResult');
    if (!programme || !content) { result.innerHTML = '<span class="text-danger">Programme and content required</span>'; return; }

    elecImport.disabled = true;
    try {
      const res = await fetch('/ops/electives/catalogue/import/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
        body: JSON.stringify({programme, content}),
      });
      const data = await res.json();
      if (data.ok) {
        result.innerHTML = `<span class="text-teal">✓ ${data.created} created, ${data.updated} updated (${data.total} total)</span>`;
        if (elecRefresh) elecRefresh.click();
      } else {
        result.innerHTML = `<span class="text-danger">✗ ${data.error || 'Import failed'}</span>`;
      }
    } catch (e) {
      result.innerHTML = `<span class="text-danger">✗ ${e.message}</span>`;
    }
    elecImport.disabled = false;
  };
}

async function loadElectiveCatalogue() {
  const container = q('elecCatalogueTable');
  if (!container) return;
  const programme = q('elecProgramme').value.trim().toUpperCase();
  const url = programme ? `/ops/electives/catalogue/?programme=${encodeURIComponent(programme)}` : '/ops/electives/catalogue/';
  try {
    const res = await fetch(url);
    const data = await res.json();
    if (!data.ok || !data.items.length) {
      container.innerHTML = '<div class="text-t4" style="padding:8px">No elective courses found</div>';
      return;
    }
    let html = '<table class="tbl-card w-100" style="border-spacing:0 3px"><thead><tr><th>Code</th><th>Name</th><th>Prereq</th><th>Cat</th><th>Cr</th></tr></thead><tbody>';
    data.items.forEach(c => {
      html += `<tr class="cr-row"><td><span class="cr-id">${c.course_code}</span></td><td>${c.course_name}</td><td class="font-mono fs-sm">${c.prerequisites_csv || '—'}</td><td>${c.category}</td><td>${c.credit_hours}</td></tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<div class="text-danger">${e.message}</div>`;
  }
}

if (elecRefresh) elecRefresh.onclick = loadElectiveCatalogue;
