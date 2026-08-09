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
      const looksLikeHtml = ct.includes('text/html') || /^\s*</.test(body);
      const snippet = looksLikeHtml
        ? ''
        : body.replace(/\s+/g,' ').trim().slice(0,300);
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

/* ── Section: Clear current section snapshot ── */
const ssPreviewBtn = q('ssPreview');
const ssClearBtn = q('ssClear');
let ssPreviewState = null;

function snapshotNumber(data, keys) {
  for (const key of keys) {
    const value = Number(data?.[key]);
    if (Number.isFinite(value)) return value;
  }
  return 0;
}

function snapshotPreviewPayload(data) {
  if (data && data.preview && typeof data.preview === 'object') return data.preview;
  return data || {};
}

function setSnapshotStatus(kind, message) {
  const el = q('ssStatus');
  if (!el) return;
  el.classList.remove('d-none', 'is-error', 'is-success', 'is-info');
  el.classList.add(`is-${kind || 'info'}`);
  el.textContent = String(message || '');
}

function clearSnapshotStatus() {
  const el = q('ssStatus');
  if (!el) return;
  el.classList.add('d-none');
  el.textContent = '';
}

function invalidateSnapshotPreview({ hide = true, announce = false } = {}) {
  const hadPreview = Boolean(ssPreviewState?.token);
  ssPreviewState = null;
  if (ssClearBtn) disableDeleteBtn(ssClearBtn);
  if (hide) q('ssPreviewResult')?.classList.add('d-none');
  if (announce && hadPreview) {
    setSnapshotStatus('info', IS_AR
      ? 'تغيّر نطاق المسح. شغّل المعاينة مرة أخرى قبل المتابعة.'
      : 'The clear scope changed. Run Preview again before continuing.');
  }
}

function snapshotFilterPayload() {
  const program = String(q('ssProgram')?.value || '').trim().toUpperCase();
  const gender = String(q('ssGender')?.value || 'ALL').trim().toUpperCase();
  return { program, gender, all_programs: !program };
}

function snapshotScopeLabel(payload) {
  const programEl = q('ssProgram');
  const genderEl = q('ssGender');
  const programLabel = programEl?.selectedOptions?.[0]?.textContent?.trim() || payload.program;
  const genderLabel = genderEl?.selectedOptions?.[0]?.textContent?.trim() || payload.gender;
  return IS_AR
    ? `النطاق: ${programLabel} · ${genderLabel}`
    : `Scope: ${programLabel} · ${genderLabel}`;
}

function snapshotPrograms(value) {
  if (Array.isArray(value)) return value;
  if (value == null || value === '') return [];
  return String(value).split(',').map(item => item.trim()).filter(Boolean);
}

function renderSnapshotSample(rows, total) {
  const wrap = q('ssSampleWrap');
  const body = q('ssSampleBody');
  if (!wrap || !body) return;
  body.innerHTML = '';

  if (!Array.isArray(rows) || rows.length === 0) {
    wrap.classList.add('d-none');
    return;
  }

  rows.forEach(row => {
    const programs = snapshotPrograms(row.programs || row.program_codes || row.programme_codes);
    const programmeHtml = programs.length
      ? programs.map(program => `<span class="dba-program-chip">${esc(program)}</span>`).join('')
      : `<span class="text-secondary">${IS_AR ? 'غير محدد' : 'Unassigned'}</span>`;
    const meetings = Array.isArray(row.meetings)
      ? row.meetings.length
      : snapshotNumber(row, ['meetings_count', 'meeting_count']);
    const isRetained = row.action === 'retain';
    const actionLabel = isRetained
      ? (IS_AR ? 'سيُحتفظ بها' : 'Retained')
      : (IS_AR ? 'ستُمسح' : 'Will clear');
    const tr = document.createElement('tr');
    if (isRetained) tr.classList.add('is-retained');
    tr.innerHTML =
      `<td><bdi class="dba-snapshot-code">${esc(row.course_code || row.course_key || row.code || '—')}</bdi></td>` +
      `<td><bdi>${esc(row.section || row.section_code || '—')}</bdi></td>` +
      `<td><div class="dba-program-chips">${programmeHtml}</div></td>` +
      `<td>${esc(String(meetings))}</td>` +
      `<td>${esc(row.source_tag || row.source || '—')}</td>` +
      `<td><span class="dba-snapshot-action ${isRetained ? 'is-retained' : 'is-delete'}">${actionLabel}</span></td>`;
    body.appendChild(tr);
  });

  q('ssSampleCaption').textContent = IS_AR
    ? `عرض ${rows.length} من ${total}`
    : `Showing ${rows.length} of ${total}`;
  wrap.classList.remove('d-none');
}

function renderSnapshotPreview(data, filters) {
  const payload = snapshotPreviewPayload(data);
  const matchedSections = snapshotNumber(payload, ['sections_count', 'term_sections_count', 'section_count']);
  const physicalSections = payload.physical_sections_count != null
    ? Number(payload.physical_sections_count) || 0
    : matchedSections;
  const counts = {
    matched: matchedSections,
    sections: physicalSections,
    meetings: snapshotNumber(payload, ['meetings_count', 'meeting_count']),
    links: snapshotNumber(payload, ['student_links_count', 'student_term_sections_count', 'links_count']),
    students: snapshotNumber(payload, ['students_count', 'affected_students_count', 'distinct_students_count']),
    memberships: snapshotNumber(payload, ['memberships_count', 'program_memberships_count']),
  };
  const token = String(payload.preview_token || payload.token || data?.preview_token || '');
  const confirmationPhrase = String(
    payload.confirmation_phrase || payload.confirm_phrase || data?.confirmation_phrase || `CLEAR ${counts.sections}`
  );
  const rows = payload.sample_sections || payload.samples || payload.sections_preview ||
    (Array.isArray(payload.sections) ? payload.sections : []);

  q('ssSectionsCount').textContent = String(counts.sections);
  q('ssMeetingsCount').textContent = String(counts.meetings);
  q('ssLinksCount').textContent = String(counts.links);
  q('ssStudentsCount').textContent = String(counts.students);
  q('ssScope').textContent = payload.scope_label || snapshotScopeLabel(filters);
  const expiresSeconds = snapshotNumber(payload, ['preview_expires_in_seconds']);
  const expiresMinutes = expiresSeconds > 0 ? Math.max(1, Math.ceil(expiresSeconds / 60)) : 0;
  q('ssPreviewReady').textContent = expiresMinutes > 0
    ? (IS_AR ? `صالحة لمدة ${expiresMinutes} دقائق` : `Valid for ${expiresMinutes} min`)
    : (IS_AR ? 'معاينة جاهزة' : 'Preview ready');
  renderSnapshotSample(rows, matchedSections);

  const sharedCount = snapshotNumber(payload, ['shared_sections_count']);
  const retainedCount = snapshotNumber(payload, ['retained_sections_count', 'shared_retained_count', 'shared_sections_retained']);
  const protectedCount = snapshotNumber(payload, ['protected_sections_count']);
  const warningMessages = [];
  if (Array.isArray(payload.warnings)) {
    payload.warnings.filter(Boolean).forEach(warning => {
      if (typeof warning === 'string') {
        warningMessages.push(warning);
        return;
      }
      /* Shared and planner warnings are rendered from the exact counts below. */
      if (warning.code === 'shared_sections_retained' || warning.code === 'planner_sections_retained') return;
      if (warning.code === 'unassigned_sections') {
        warningMessages.push(warning.included
          ? (IS_AR
            ? `يتضمن هذا المسح ${warning.count || 0} شعبة غير مرتبطة بأي برنامج.`
            : `This clear includes ${warning.count || 0} section(s) not assigned to a programme.`)
          : (IS_AR
            ? `سيتم الاحتفاظ بـ ${warning.count || 0} شعبة غير مرتبطة ببرنامج لعدم إمكانية مطابقتها مع هذا النطاق.`
            : `${warning.count || 0} unassigned section(s) will be retained because they cannot be matched to this programme.`));
        return;
      }
      if (warning.message) warningMessages.push(String(warning.message));
    });
  }
  const sharedNote = q('ssSharedNote');
  if (sharedNote) {
    const notes = [];
    if (retainedCount > 0) {
      notes.push(IS_AR
        ? `سيتم الاحتفاظ بـ ${retainedCount} شعبة لأنها مشتركة مع برامج خارج النطاق أو محمية بمرجع في المخطط.`
        : `${retainedCount} section(s) will be retained because they are shared outside this scope or protected by a planner reference.`);
    } else if (sharedCount > 0) {
      notes.push(IS_AR
        ? `يتضمن النطاق ${sharedCount} شعبة مشتركة. راجع البرامج الظاهرة في الجدول قبل المتابعة.`
        : `This scope includes ${sharedCount} shared section(s). Review the programmes shown below before continuing.`);
    }
    if (protectedCount > 0) {
      notes.push(IS_AR
        ? `${protectedCount} شعبة محمية بمرجع في المخطط ولن تُحذف فعلياً.`
        : `${protectedCount} section(s) are protected by planner references and will not be physically deleted.`);
    }
    notes.push(...warningMessages);
    if (notes.length > 0) {
      sharedNote.textContent = notes.join('\n');
      sharedNote.classList.remove('d-none');
    } else {
      sharedNote.textContent = '';
      sharedNote.classList.add('d-none');
    }
  }

  q('ssPreviewResult').classList.remove('d-none');
  ssPreviewState = { token, confirmationPhrase, counts, filters };

  if (counts.matched > 0 && token) {
    enableDeleteBtn(ssClearBtn);
    setSnapshotStatus('success', IS_AR
      ? 'اكتملت المعاينة. راجع التأثير ثم أكد المسح.'
      : 'Preview complete. Review the impact, then confirm the clear.');
  } else {
    disableDeleteBtn(ssClearBtn);
    setSnapshotStatus('info', counts.matched === 0
      ? (IS_AR ? 'لا توجد شعب حالية تطابق هذا النطاق.' : 'No current sections match this scope.')
      : (IS_AR ? 'تعذر تفويض المسح. أعد تشغيل المعاينة.' : 'Clear authorization was not issued. Run Preview again.'));
  }
}

if (ssPreviewBtn) {
  ssPreviewBtn.addEventListener('click', async () => {
    invalidateSnapshotPreview({ hide: true });
    clearSnapshotStatus();
    const filters = snapshotFilterPayload();
    const data = await callJson('/ops/db/section-snapshot/preview/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(filters),
    }, null, ssPreviewBtn);

    if (!data || data.error || data.ok === false || data.scraper_running) {
      const message = data?.error || data?.message || data?.blocked_reason || (IS_AR ? 'فشلت معاينة النطاق.' : 'Scope preview failed.');
      setSnapshotStatus('error', message);
      return;
    }
    renderSnapshotPreview(data, filters);
  });

  ['ssProgram', 'ssGender'].forEach(id => {
    q(id)?.addEventListener('change', () => invalidateSnapshotPreview({ hide: true, announce: true }));
  });
}

if (ssClearBtn) {
  ssClearBtn.addEventListener('click', async () => {
    if (!ssPreviewState?.token) {
      invalidateSnapshotPreview({ hide: true });
      setSnapshotStatus('info', IS_AR ? 'شغّل المعاينة مرة أخرى قبل المسح.' : 'Run Preview again before clearing.');
      return;
    }

    const phrase = ssPreviewState.confirmationPhrase;
    const counts = ssPreviewState.counts;
    const ok = await dlg.confirm({
      title: IS_AR ? 'مسح لقطة الشعب الحالية؟' : 'Clear current section snapshot?',
      body: IS_AR
        ? `<p>سيتم حذف <strong>${counts.sections}</strong> شعبة فعلياً و<strong>${counts.meetings}</strong> موعد و<strong>${counts.links}</strong> رابط جدول، وإزالة <strong>${counts.memberships}</strong> ارتباط بالبرامج ضمن النطاق الذي عاينته.</p><p>لا يمكن التراجع عن هذا الإجراء من هذه الشاشة.</p>`
        : `<p>This will physically delete <strong>${counts.sections}</strong> section(s), <strong>${counts.meetings}</strong> meeting(s), <strong>${counts.links}</strong> timetable link(s), and remove <strong>${counts.memberships}</strong> programme membership(s) from the previewed scope.</p><p>This cannot be undone from this screen.</p>`,
      typed: phrase,
      confirmText: IS_AR ? 'مسح اللقطة' : 'Clear snapshot',
      kind: 'danger',
    });
    if (!ok) return;

    const state = ssPreviewState;
    const data = await callJson('/ops/db/section-snapshot/clear/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preview_token: state.token, confirm: phrase }),
    }, null, ssClearBtn);

    invalidateSnapshotPreview({ hide: false });
    if (!data || data.error || data.ok === false) {
      setSnapshotStatus('error', data?.error || data?.message || (IS_AR ? 'تعذر مسح لقطة الشعب.' : 'Could not clear the section snapshot.'));
      return;
    }

    const result = snapshotPreviewPayload(data);
    const deleted = result.deleted && typeof result.deleted === 'object' ? result.deleted : {};
    const deletedSections = snapshotNumber(deleted, ['sections']) || snapshotNumber(result, ['deleted_sections_count', 'sections_deleted']) || state.counts.sections;
    const deletedMeetings = snapshotNumber(deleted, ['meetings']) || snapshotNumber(result, ['deleted_meetings_count', 'meetings_deleted']) || state.counts.meetings;
    const deletedLinks = snapshotNumber(deleted, ['student_links']) || snapshotNumber(result, ['deleted_student_links_count', 'student_links_deleted', 'student_term_sections_deleted']) || state.counts.links;
    const backupFile = String(result.backup?.backup_file || result.backup_file || '');
    const backupNote = backupFile
      ? (IS_AR ? ` تم إنشاء نسخة احتياطية: ${backupFile}` : ` Backup created: ${backupFile}`)
      : '';
    setSnapshotStatus('success', IS_AR
      ? `تم المسح بنجاح: ${deletedSections} شعبة، ${deletedMeetings} موعد، ${deletedLinks} رابط جدول.${backupNote}`
      : `Snapshot cleared: ${deletedSections} section(s), ${deletedMeetings} meeting(s), and ${deletedLinks} timetable link(s).${backupNote}`);
  });
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
let termPreviewState = null;
let termPreviewRequestGeneration = 0;

function setTermStep(step) {
  ['tStep1','tStep2','tStep3'].forEach((id, i) => {
    const el = q(id);
    if (!el) return;
    el.classList.remove('active','done');
    if (i + 1 < step)     el.classList.add('done');
    else if (i + 1 === step) el.classList.add('active');
  });
}

function termImportNumber(data, keys) {
  for (const key of keys) {
    const value = Number(data?.[key]);
    if (Number.isFinite(value)) return value;
  }
  return 0;
}

function selectedTermDefaultPrograms() {
  return Array.from(document.querySelectorAll('.t-default-program:checked'))
    .map(input => String(input.value || '').trim().toUpperCase())
    .filter(Boolean)
    .sort();
}

function currentTermImportPayload() {
  const source = document.querySelector('input[name="tSourceTag"]:checked');
  return {
    csv_path: String(q('tCsvPath')?.value || '').trim(),
    source_tag: String(source?.value || 'other').trim().toLowerCase(),
    default_programs: selectedTermDefaultPrograms(),
  };
}

function sameTermImportPayload(left, right) {
  if (!left || !right) return false;
  const leftPrograms = Array.isArray(left.default_programs) ? left.default_programs : [];
  const rightPrograms = Array.isArray(right.default_programs) ? right.default_programs : [];
  return left.csv_path === right.csv_path
    && left.source_tag === right.source_tag
    && leftPrograms.length === rightPrograms.length
    && leftPrograms.every((value, index) => value === rightPrograms[index]);
}

function invalidateTermPreview({ hide = true, announce = false } = {}) {
  const hadPreview = termPreviewReady;
  termPreviewRequestGeneration += 1;
  termPreviewReady = false;
  termPreviewState = null;
  disableDeleteBtn(q('tImport'));
  if (hide) q('tPreviewWrap')?.classList.add('d-none');
  setTermStep(1);
  if (announce && hadPreview) {
    notify.warning(IS_AR
      ? 'تغيّرت إعدادات الاستيراد. شغّل المعاينة مرة أخرى.'
      : 'Import settings changed. Run Preview again.');
  }
}

function renderTermPrograms(programs) {
  const values = Array.isArray(programs) ? programs.filter(Boolean) : [];
  if (!values.length) {
    return `<span class="dba-programme-unassigned">${IS_AR ? 'غير معيّنة' : 'Unassigned'}</span>`;
  }
  return `<div class="dba-program-chips">${values.map(value => `<span class="dba-program-chip">${esc(value)}</span>`).join('')}</div>`;
}

function renderTermImportPreview(data, requestPayload) {
  const impact = data.impact && typeof data.impact === 'object' ? data.impact : data;
  const sections = termImportNumber(impact, ['sections_unique', 'sections_in_file', 'sections_count']);
  const meetings = termImportNumber(impact, ['meeting_rows_unique', 'meeting_rows_in_file', 'total_rows']);
  const sectionsNew = termImportNumber(impact, ['sections_new', 'new_sections_count']);
  const sectionsExisting = termImportNumber(impact, ['sections_existing', 'existing_sections_count']);
  const assignments = termImportNumber(impact, ['programme_assignments_effective', 'programme_assignments_count']);
  const membershipAdds = termImportNumber(impact, ['membership_adds', 'program_links_to_add']);
  const membershipRemoves = termImportNumber(impact, ['membership_removes', 'program_links_to_remove']);
  const membershipSourceChanges = termImportNumber(impact, ['membership_source_changes', 'membership_promotions']);
  const predictedUnassigned = termImportNumber(impact, ['predicted_fully_unassigned_sections', 'predicted_unassigned_count']);
  const previewRows = Array.isArray(data.preview_rows) ? data.preview_rows : [];
  const canImport = data.can_import === true || (
    data.can_import == null && sections > 0 && predictedUnassigned === 0
  );
  const confirmationPhrase = String(data.confirmation_phrase || `IMPORT ${sections}`);
  const previewFingerprint = String(data.preview_fingerprint || '');

  const metrics = {
    tMetricSections: sections,
    tMetricMeetings: meetings,
    tMetricNew: sectionsNew,
    tMetricExisting: sectionsExisting,
    tMetricAssignments: assignments,
    tMetricAdd: membershipAdds,
    tMetricRemove: membershipRemoves,
    tMetricSourceChanges: membershipSourceChanges,
    tMetricUnassigned: predictedUnassigned,
  };
  Object.entries(metrics).forEach(([id, value]) => { q(id).textContent = String(value); });

  const body = q('tTableBody');
  body.innerHTML = '';
  previewRows.forEach(row => {
    const rawPrograms = Array.isArray(row.programs) ? row.programs.filter(Boolean) : [];
    const effectivePrograms = Array.isArray(row.effective_programs)
      ? row.effective_programs.filter(Boolean)
      : rawPrograms;
    const assignmentSource = row.programme_source || row.program_source || row.assignment_source ||
      (rawPrograms.length ? 'csv' : effectivePrograms.length ? 'default' : 'unassigned');
    const assignmentKey = ['csv', 'default', 'preserved', 'mixed'].includes(assignmentSource)
      ? assignmentSource
      : 'unassigned';
    const assignmentLabels = {
      csv: IS_AR ? 'من CSV' : 'CSV',
      default: IS_AR ? 'افتراضي' : 'Default',
      preserved: IS_AR ? 'محفوظ من النظام' : 'Preserved',
      mixed: IS_AR ? 'مصادر مختلطة' : 'Mixed sources',
      unassigned: IS_AR ? 'غير معيّن' : 'Unassigned',
    };
    const assignmentLabel = assignmentLabels[assignmentKey];
    const code = row.course_key || `${row.course_code || ''}${row.course_number || ''}`;
    const time = [row.start_time, row.end_time].filter(Boolean).join('–') || '—';
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td><bdi class="dba-snapshot-code">${esc(code || '—')}</bdi><small>${esc(row.course_name || '')}</small></td>` +
      `<td><bdi>${esc(row.section || '—')}</bdi></td>` +
      `<td>${esc(row.day || '—')}</td>` +
      `<td><bdi>${esc(time)}</bdi></td>` +
      `<td>${esc(row.room || '—')}</td>` +
      `<td>${renderTermPrograms(effectivePrograms)}</td>` +
      `<td><span class="dba-assignment-source is-${assignmentKey}">${esc(assignmentLabel)}</span></td>`;
    body.appendChild(tr);
  });
  q('tTableWrap').classList.toggle('d-none', previewRows.length === 0);
  q('tSampleCaption').textContent = IS_AR
    ? `عرض ${previewRows.length} من ${meetings}`
    : `Showing ${previewRows.length} of ${meetings}`;

  const defaults = Array.isArray(data.default_programs)
    ? data.default_programs
    : requestPayload.default_programs;
  const statusParts = [];
  if (data.has_program_column) {
    statusParts.push(IS_AR
      ? 'عثر النظام على قيم برامج في CSV؛ لها الأولوية، وتُستخدم الاختيارات الافتراضية للصفوف غير الموسومة فقط.'
      : 'Programme values were found in the CSV; they take precedence, while selected defaults apply only to untagged rows.');
  } else if (defaults.length) {
    statusParts.push(IS_AR
      ? `لا يحتوي CSV على عمود برامج. ستُستخدم البرامج الافتراضية: ${defaults.join('، ')}.`
      : `The CSV has no programme column. Defaults will be used: ${defaults.join(', ')}.`);
  } else {
    statusParts.push(IS_AR
      ? 'لا يحتوي CSV على عمود برامج ولم تُحدد برامج افتراضية.'
      : 'The CSV has no programme column and no default programmes were selected.');
  }
  if (predictedUnassigned > 0) {
    statusParts.push(IS_AR
      ? `الاستيراد محجوب لأن ${predictedUnassigned} شعبة ستبقى بلا برنامج بعد الدمج.`
      : `${predictedUnassigned} section(s) would remain without a programme after the merge.`);
  } else if (data.program_membership_status === 'legacy_preserve') {
    statusParts.push(IS_AR
      ? 'سيحتفظ النظام بتعيينات البرامج الموجودة للشعب المطابقة.'
      : 'Existing programme assignments will be preserved for matching sections.');
  } else if (data.program_membership_warning) {
    statusParts.push(String(data.program_membership_warning));
  }
  q('tProgrammeStatus').textContent = statusParts.join(' ');

  q('tPreviewStatus').textContent = canImport
    ? (IS_AR ? `المعاينة جاهزة لدمج ${sections} شعبة.` : `Preview is ready to merge ${sections} section(s).`)
    : (IS_AR ? 'الاستيراد محجوب. عالج التحذير الظاهر ثم أعد المعاينة.' : 'Import is blocked. Resolve the warning, then preview again.');
  q('tCanImportBadge').textContent = canImport
    ? (IS_AR ? 'جاهز للاستيراد' : 'Ready to import')
    : (IS_AR ? 'الاستيراد محجوب' : 'Import blocked');
  q('tCanImportBadge').classList.toggle('is-blocked', !canImport);
  q('tPreviewWrap').classList.remove('d-none');

  termPreviewReady = canImport && /^[0-9a-f]{64}$/.test(previewFingerprint);
  termPreviewState = {
    requestPayload,
    sections,
    meetings,
    membershipAdds,
    membershipRemoves,
    membershipSourceChanges,
    confirmationPhrase,
    previewFingerprint,
  };
  if (termPreviewReady) {
    enableDeleteBtn(q('tImport'));
    setTermStep(3);
  } else {
    disableDeleteBtn(q('tImport'));
    setTermStep(2);
  }
}

q('tPreview').onclick = async () => {
  invalidateTermPreview({ hide: true });
  setTermStep(2);
  const requestPayload = currentTermImportPayload();
  const requestGeneration = termPreviewRequestGeneration;
  const data = await callJson('/ops/db/preview-term-sections/', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(requestPayload)
  }, null, q('tPreview'));

  if (
    requestGeneration !== termPreviewRequestGeneration
    || !sameTermImportPayload(requestPayload, currentTermImportPayload())
  ) {
    return;
  }
  writeOut('tOut', data);

  if (data && !data.error && Array.isArray(data.preview_rows)) {
    renderTermImportPreview(data, requestPayload);
  } else {
    invalidateTermPreview({ hide: true });
  }
};

q('tCsvPath').addEventListener('input', () => invalidateTermPreview({ announce: true }));
document.querySelectorAll('input[name="tSourceTag"], .t-default-program').forEach(input => {
  input.addEventListener('change', () => invalidateTermPreview({ announce: true }));
});

q('tImport').onclick = async () => {
  if (!termPreviewReady || !termPreviewState) { notify.warning(T.runPreviewFirst); return; }
  const state = termPreviewState;
  const confirmation = await dlg.confirm({
    title: IS_AR ? 'دمج الشعب الحالية؟' : 'Merge current sections?',
    body: IS_AR
      ? `<p>سيتم دمج <strong>${state.sections}</strong> شعبة و<strong>${state.meetings}</strong> صف موعد في اللقطة الحالية. أثر البرامج: ${state.membershipAdds} إضافة، ${state.membershipRemoves} إزالة، ${state.membershipSourceChanges} تغيير مصدر. لن تُحذف الشعب غير الموجودة في الملف.</p>`
      : `<p>This will merge <strong>${state.sections}</strong> section(s) and <strong>${state.meetings}</strong> meeting row(s) into the current snapshot. Programme impact: ${state.membershipAdds} add, ${state.membershipRemoves} remove, ${state.membershipSourceChanges} source change. Sections outside the file will not be deleted.</p>`,
    typed: state.confirmationPhrase,
    confirmText: IS_AR ? 'تأكيد الدمج' : 'Confirm merge',
    kind: 'info',
  });
  if (!confirmation) return;
  if (
    !termPreviewReady
    || termPreviewState !== state
    || !sameTermImportPayload(state.requestPayload, currentTermImportPayload())
  ) {
    invalidateTermPreview({ hide: true });
    notify.warning(IS_AR
      ? 'تغيّرت إعدادات الاستيراد. شغّل المعاينة مرة أخرى.'
      : 'Import settings changed. Run Preview again.');
    return;
  }
  if (confirmation !== state.confirmationPhrase) {
    notify.warning(IS_AR
      ? `اكتب ${state.confirmationPhrase} بالحروف والمسافات نفسها.`
      : `Type ${state.confirmationPhrase} with the exact letters and spacing.`);
    return;
  }
  const data = await callJson('/ops/db/import-term-sections/', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      ...state.requestPayload,
      confirm: confirmation,
      preview_fingerprint: state.previewFingerprint,
    })
  }, 'tOut', q('tImport'));
  if (data && ['preview_stale', 'preview_required'].includes(data.code)) {
    invalidateTermPreview({ hide: true });
    notify.warning(IS_AR
      ? 'تغيّر الملف أو بيانات الشعب. شغّل المعاينة مرة أخرى قبل الدمج.'
      : 'The file or section data changed. Run Preview again before merging.');
    return;
  }
  if (data && !data.error && data.ok !== false) {
    termPreviewReady = false;
    termPreviewState = null;
    disableDeleteBtn(q('tImport'));
    setTermStep(3);
    q('tPreviewStatus').textContent = IS_AR
      ? 'اكتمل الدمج. شغّل معاينة جديدة قبل أي استيراد آخر.'
      : 'Merge complete. Run a new preview before another import.';
    q('tCanImportBadge').textContent = IS_AR ? 'تم الاستيراد' : 'Imported';
    notify.success(IS_AR ? 'تم دمج الشعب الحالية بنجاح.' : 'Current sections were merged successfully.');
  } else {
    invalidateTermPreview({ hide: false });
    q('tPreviewStatus').textContent = IS_AR
      ? 'انتهت صلاحية المعاينة أو فشل الدمج. شغّل معاينة جديدة.'
      : 'The preview expired or the merge failed. Run a new preview.';
    q('tCanImportBadge').textContent = IS_AR ? 'المعاينة منتهية' : 'Preview expired';
    q('tCanImportBadge').classList.add('is-blocked');
  }
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
