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

/* ── Section: Programme Capacities ──
 * The seat limits (max_capacity) of ONE programme, saved through Section
 * Planning's write path (section_limits), never written on input or blur.
 * An edit is a draft, marked "Modified"; an emptied saved limit is a removal.
 * Save sends only the edited rows, for the programme the rows were LOADED for:
 * typing another programme in the box blocks Save until that programme is
 * loaded. The server previews the exact rows (course · name · old → new), the
 * user confirms them, and the commit carries the preview's token. */
const CAP_MIN = 1, CAP_MAX = 500;
const CAP_LIMITS_URL = '/ops/db/programme-capacities/limits/';
let capRows = [];
let capProgram = '';   // the programme whose rows are on screen
let capBusy = false;   // a review or save in flight: one at a time

const TC = {
  modified:     IS_AR ? 'معدَّل' : 'Modified',
  removing:     IS_AR ? 'معدَّل: يُزال الحد' : 'Modified: limit removed',
  invalid:      IS_AR ? `رقم صحيح من ${CAP_MIN} إلى ${CAP_MAX}` : `Whole number ${CAP_MIN}–${CAP_MAX}`,
  limitAria:    (code, name) => (IS_AR ? `الحد الجديد لـ ${code} ${name}` : `New seat limit for ${code} ${name}`).trim(),
  saveBtn:      n => IS_AR ? (n ? `حفظ التغييرات (${n})` : 'حفظ التغييرات') : (n ? `Save changes (${n})` : 'Save changes'),
  drafts:       n => IS_AR ? `${n} تعديل غير محفوظ` : `${n} unsaved change${n === 1 ? '' : 's'}`,
  toFix:        n => IS_AR ? `${n} قيمة تحتاج تصحيحاً قبل الحفظ` : `${n} value${n === 1 ? '' : 's'} to fix before saving`,
  noDrafts:     IS_AR ? 'لا توجد تعديلات غير محفوظة' : 'No unsaved changes',
  fixInvalid:   IS_AR ? 'صحّح القيم غير الصالحة قبل الحفظ.' : 'Fix the invalid values before saving.',
  otherProgram: (loaded, typed) => IS_AR
    ? `الصفوف المعروضة لبرنامج ${loaded}، والمكتوب في الحقل ${typed || '—'}. حمّل ذلك البرنامج أو أعد كتابة ${loaded} لتتمكن من الحفظ.`
    : `The rows on screen are for ${loaded}, but the box says ${typed || '—'}. Load that programme, or type ${loaded} again, to save.`,
  needProgram:  IS_AR ? 'رمز البرنامج مطلوب.' : 'Program code is required.',
  nothing:      IS_AR ? 'لا شيء يتغيّر: الحدود المحفوظة مطابقة.' : 'Nothing to change: the saved limits already match.',
  saveTitle:    (n, p) => IS_AR ? `حفظ ${n} من حدود الشعب لبرنامج ${p}؟` : `Save ${n} seat limit${n === 1 ? '' : 's'} for ${p}?`,
  saveIntro:    p => IS_AR ? `تتغيّر هذه الصفوف فقط، لبرنامج ${p} وحده، ويُسجَّل كل تغيير في سجل التدقيق.` : `Only these rows change, for ${p} only. Each change is recorded in the audit log.`,
  unchanged:    n => IS_AR ? `${n} صف مطابق أصلاً ولن يتغيّر.` : `${n} row${n === 1 ? ' already matches' : 's already match'} and will not change.`,
  thCourse:     IS_AR ? 'المقرر' : 'Course',
  thName:       IS_AR ? 'الاسم' : 'Name',
  thOld:        IS_AR ? 'المحفوظ الآن' : 'Saved now',
  thNew:        IS_AR ? 'الجديد' : 'New',
  none:         IS_AR ? '— (القاعدة)' : '— (rule)',
  remove:       IS_AR ? 'إزالة (تُطبَّق القاعدة)' : 'remove (the rule applies)',
  confirmSave:  n => IS_AR ? `حفظ ${n} تغيير` : `Save ${n} change${n === 1 ? '' : 's'}`,
  keepEditing:  IS_AR ? 'متابعة التعديل' : 'Keep editing',
  checking:     IS_AR ? 'جارٍ التحقق…' : 'Checking…',
  saving:       IS_AR ? 'جارٍ الحفظ...' : 'Saving...',
  saved:        (n, p) => IS_AR ? `حُفظ ${n} حد لبرنامج ${p} وسُجّل كل تغيير في سجل التدقيق.` : `Saved ${n} seat limit${n === 1 ? '' : 's'} for ${p}; each change is in the audit log.`,
  discardTitle: n => IS_AR ? `تجاهل ${n} تعديل غير محفوظ؟` : `Discard ${n} unsaved change${n === 1 ? '' : 's'}?`,
  discardBody:  (loaded, next) => IS_AR ? `تحميل ${next} يستبدل صفوف ${loaded} وتعديلاتها غير المحفوظة.` : `Loading ${next} replaces ${loaded}'s rows and their unsaved changes.`,
  discard:      IS_AR ? 'تجاهل وتحميل' : 'Discard and load',
  loaded:       (n, p) => IS_AR ? `تم تحميل ${n} مقرر للبرنامج "${p}".` : `Loaded ${n} course(s) for program "${p}".`,
  noRows:       p => IS_AR ? `لا توجد مقررات للبرنامج "${p}".` : `No courses found for program "${p}".`,
  count:        (n, p) => IS_AR ? `${n} مقرر · ${p}` : `${n} course(s) · ${p}`,
  leave:        IS_AR ? 'لديك حدود غير محفوظة.' : 'You have unsaved seat limits.',
};

/* Server refusals carry a stable code; the words are the page's. */
const CAP_ERRORS = {
  invalid_json:             () => IS_AR ? 'تعذّر قراءة الطلب.' : 'The request could not be read.',
  programs_required:        () => IS_AR ? 'حمّل برنامجاً واحداً أولاً: الحدود تُحفظ لكل برنامج.' : 'Load one programme first: limits are saved per programme.',
  unknown_program:          d => IS_AR ? `برنامج غير معروف: ${d.program || ''}` : `Unknown programme: ${d.program || ''}`,
  changes_required:         () => TC.nothing,
  too_many_changes:         d => IS_AR ? `احفظ ${d.max || ''} مقرراً كحد أقصى في المرة الواحدة.` : `Save at most ${d.max || ''} courses at a time.`,
  invalid_course:           () => IS_AR ? 'تغيير بلا رمز مقرر.' : 'A change is missing its course code.',
  duplicate_course:         d => IS_AR ? `${d.course_code || ''} مكرر في الحفظ نفسه.` : `${d.course_code || ''} appears twice in one save.`,
  invalid_limit:            d => IS_AR ? `حد ${d.course_code || ''} يجب أن يكون رقماً صحيحاً من ${CAP_MIN} إلى ${CAP_MAX}.` : `The limit for ${d.course_code || ''} must be a whole number from ${CAP_MIN} to ${CAP_MAX}.`,
  invalid_scope:            d => IS_AR ? `${d.course_code || ''} يُحفظ للبرنامج المحمَّل فقط.` : `${d.course_code || ''} can only be saved for the programme that was loaded.`,
  course_not_in_programmes: d => IS_AR ? `${d.course_code || ''} لا يُدرَّس في ${capProgram}.` : `${d.course_code || ''} is not taught by ${capProgram}.`,
  preview_stale:            () => IS_AR ? 'تغيّرت الحدود المحفوظة منذ المراجعة، فأُعيد تحميلها. اضغط حفظ التغييرات مرة أخرى لمراجعة القائمة الجديدة.' : 'The saved limits changed since you reviewed them, so they were reloaded. Press Save changes again to review the new list.',
  limit_changed:            () => IS_AR ? 'تغيّر حد أثناء الحفظ فلم يُحفظ شيء، وأُعيد تحميل الحدود. اضغط حفظ التغييرات مرة أخرى.' : 'A limit changed while saving, so nothing was saved and the limits were reloaded. Press Save changes again.',
  audit_unavailable:        () => IS_AR ? 'تعذّر تسجيل التغيير في سجل التدقيق، لذلك لم يُحفظ شيء. حاول مرة أخرى.' : "Couldn't record the change in the audit log, so nothing was saved. Try again.",
};
/* The saved limits moved under the panel: reload them, keeping the drafts. */
const CAP_STALE_CODES = new Set(['preview_stale', 'limit_changed']);
function capError(data, status) {
  const make = data && CAP_ERRORS[data.code];
  if (make) return make(data);
  if (status === 429) return IS_AR ? 'طلبات كثيرة. انتظر قليلاً ثم حاول.' : 'Too many requests. Wait a moment and try again.';
  if (status === 401 || status === 403) return IS_AR ? 'لا تملك صلاحية حفظ الحدود.' : 'You are not allowed to save limits.';
  return T.requestFailed;
}

/* A programme code as the server reads it: case and spaces aside. */
function capProgramOf(value) {
  return String(value || '').replace(/\s+/g, '').toUpperCase();
}

/* Latin digits whatever the keyboard: Arabic-Indic and Persian 30 are 30. */
function capLatinDigits(text) {
  return String(text).replace(/[٠-٩]/g, d => String(d.charCodeAt(0) - 0x0660))
                     .replace(/[۰-۹]/g, d => String(d.charCodeAt(0) - 0x06F0));
}

/* The panel's outcome, in words (not JSON): 'err', 'ok' or neutral. */
function capSay(text, kind = '') {
  const out = q('capOut');
  out.textContent = text;
  out.classList.toggle('has-error', kind === 'err');
  out.classList.toggle('has-success', kind === 'ok');
}

function capRowEls() { return [...q('capBody').querySelectorAll('tr[data-code]')]; }

/* What one row asks for, read from its field. */
function capDraftOf(tr) {
  const raw = capLatinDigits(tr.querySelector('.cap-input').value.trim());
  const saved = tr.dataset.saved === '' ? null : Number(tr.dataset.saved);
  const base = { code: tr.dataset.code, value: null, draft: false, invalid: false };
  if (!raw) return { ...base, draft: saved !== null };   // an emptied saved limit: remove it
  const value = /^\d+$/.test(raw) ? Number(raw) : NaN;
  if (!Number.isInteger(value) || value < CAP_MIN || value > CAP_MAX) return { ...base, invalid: true };
  return { ...base, value, draft: value !== saved };
}
function capDrafts() { return capRowEls().map(capDraftOf).filter(d => d.draft && !d.invalid); }
function capInvalid() { return capRowEls().map(capDraftOf).filter(d => d.invalid); }

/* Why Save cannot run now ('' when it can, given drafts). */
function capBlockReason() {
  if (!capProgram) return TC.needProgram;
  const typed = capProgramOf(q('capProgram').value);
  if (typed !== capProgram) return TC.otherProgram(capProgram, typed);
  if (capInvalid().length) return TC.fixInvalid;
  return '';
}

function capRefreshRow(tr) {
  const d = capDraftOf(tr);
  tr.classList.toggle('cap-draft', d.draft && !d.invalid);
  tr.classList.toggle('cap-invalid', d.invalid);
  tr.querySelector('.cap-input').setAttribute('aria-invalid', d.invalid ? 'true' : 'false');
  tr.querySelector('.cap-state').textContent = d.invalid ? TC.invalid
    : (d.draft ? (d.value === null ? TC.removing : TC.modified) : '');
}

/* Save is on only with edits it can send; when it is off, the status line
 * (its description) says why. It stays focusable (aria-disabled). */
function capUpdate() {
  const drafts = capDrafts();
  const invalid = capInvalid();
  const reason = capRows.length ? capBlockReason() : '';
  const parts = [];
  if (drafts.length) parts.push(TC.drafts(drafts.length));
  if (invalid.length) parts.push(TC.toFix(invalid.length));
  if (reason && reason !== TC.fixInvalid) parts.push(reason);
  q('capStatus').textContent = capRows.length ? (parts.join(' · ') || TC.noDrafts) : '';
  const btn = q('capSave');
  btn.classList.toggle('d-none', capRows.length === 0);
  btn.setAttribute('aria-disabled', capBusy || reason || !drafts.length ? 'true' : 'false');
  if (!capBusy) btn.textContent = TC.saveBtn(drafts.length);
}

/* `keep`: course code -> the raw text of a field, put back after a reload. */
function capRender(keep = new Map()) {
  const body = q('capBody');
  body.innerHTML = '';
  capRows.forEach((row, i) => {
    const tr = document.createElement('tr');
    const saved = row.max_capacity != null ? String(row.max_capacity) : '';
    const name = String(row.course_name || '').trim();
    tr.dataset.code = row.course_code;
    tr.dataset.saved = saved;
    tr.innerHTML =
      `<td><strong><bdi>${esc(row.course_code)}</bdi></strong></td>` +
      `<td><bdi>${esc(name || '—')}</bdi></td>` +
      `<td>${row.credit_hours != null ? esc(String(row.credit_hours)) : '--'}</td>` +
      `<td>${esc(saved || '--')}</td>` +
      `<td><input class="form-control form-control-sm cap-input" type="text" inputmode="numeric" autocomplete="off"` +
      ` value="${esc(saved)}" placeholder="--" aria-describedby="capState${i}"` +
      ` aria-label="${esc(TC.limitAria(row.course_code, name))}">` +
      `<span class="cap-state" id="capState${i}"></span></td>`;
    const inp = tr.querySelector('.cap-input');
    if (keep.has(row.course_code)) inp.value = keep.get(row.course_code);
    inp.addEventListener('input', () => { capRefreshRow(tr); capUpdate(); });
    body.appendChild(tr);
    capRefreshRow(tr);
  });
  q('capTableWrap').classList.toggle('d-none', capRows.length === 0);
  q('capCount').textContent = capRows.length ? TC.count(capRows.length, capProgram) : '';
  capUpdate();
}

/* Load a programme's rows; they become the programme Save writes to.
 * A failed load says why and leaves the rows (and their programme) as they were. */
async function capLoadRows(program, keep = new Map()) {
  const data = await callJson(
    `/ops/db/programme-capacities/?program=${encodeURIComponent(program)}`,
    {}, null, q('capLoad')
  );
  if (!data || data.error || !Array.isArray(data.rows)) {
    capSay((data && data.error) || T.requestFailed, 'err');
    return false;
  }
  capRows = data.rows;
  capProgram = capProgramOf(data.program || program);
  capRender(keep);
  return true;
}

q('capProgram').addEventListener('input', capUpdate);

q('capLoad').onclick = async () => {
  if (capBusy) return;
  const program = capProgramOf(q('capProgram').value);
  if (!program) {
    capSay(TC.needProgram, 'err');
    return;
  }
  const pending = capDrafts().length + capInvalid().length;
  if (pending) {
    const ok = await dlg.confirm({
      title: TC.discardTitle(pending),
      body: `<p>${esc(TC.discardBody(capProgram, program))}</p>`,
      kind: 'warning',
      confirmText: TC.discard,
      cancelText: TC.keepEditing,
      initialFocus: 'cancel',
      waitForClose: true,
    });
    if (!ok) return;
  }
  if (await capLoadRows(program)) {
    if (capRows.length) capSay(TC.loaded(capRows.length, capProgram), 'ok');
    else capSay(TC.noRows(capProgram), 'err');
  }
};

async function postCapLimits(body) {
  const res = await fetch(CAP_LIMITS_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok && data.ok === true, status: res.status, data };
}

function capReviewHtml(changes) {
  const val = (v, isNew) => v == null ? (isNew ? TC.remove : TC.none) : String(v);
  const rows = changes.map(c => `<tr>
      <td><bdi class="cr-id">${esc(c.course_code)}</bdi></td>
      <td><bdi>${esc(c.course_name || '—')}</bdi></td>
      <td class="text-center">${esc(val(c.old, false))}</td>
      <td class="text-center"><strong>${esc(val(c.new, true))}</strong></td>
    </tr>`).join('');
  return `<table class="sp-adv-table sp-limit-review">
      <thead><tr><th>${esc(TC.thCourse)}</th><th>${esc(TC.thName)}</th><th>${esc(TC.thOld)}</th><th>${esc(TC.thNew)}</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
}

/* Preview, confirm, commit. Says what to reload afterwards:
 *   'clean' – saved, or nothing to save: the saved limits, drafts dropped
 *   'keep'  – the saved limits moved under the panel: reload, drafts kept
 *   null    – nothing to reload (kept editing, or a refusal to fix) */
async function capReviewAndSave(program, changes, btn) {
  const preview = await postCapLimits({ programs: [program], changes, dry_run: true });
  if (!preview.ok) {
    capSay(capError(preview.data, preview.status), 'err');
    return CAP_STALE_CODES.has(preview.data.code) ? 'keep' : null;
  }
  const planned = preview.data.changes || [];
  if (!planned.length) { capSay(TC.nothing); return 'clean'; }
  const unchangedNote = preview.data.unchanged
    ? `<p class="fs-sm text-t3">${esc(TC.unchanged(preview.data.unchanged))}</p>` : '';
  const ok = await dlg.confirm({
    title: TC.saveTitle(planned.length, program),
    body: `<p>${esc(TC.saveIntro(program))}</p>${capReviewHtml(planned)}${unchangedNote}`,
    kind: 'warning',
    confirmText: TC.confirmSave(planned.length),
    cancelText: TC.keepEditing,
    /* A review: the keyboard starts on "Keep editing", never on the write. */
    initialFocus: 'cancel',
    waitForClose: true,
  });
  if (!ok) return null;
  btn.textContent = TC.saving;
  const commit = await postCapLimits({
    programs: [program], changes, dry_run: false, preview_token: preview.data.preview_token,
  });
  if (!commit.ok) {
    capSay(capError(commit.data, commit.status), 'err');
    return CAP_STALE_CODES.has(commit.data.code) ? 'keep' : null;
  }
  capSay(TC.saved(commit.data.changed_count, program), 'ok');
  return 'clean';
}

/* Save: only the edited rows, for the programme the rows were loaded for. */
q('capSave').onclick = async () => {
  if (capBusy) return;
  const reason = capBlockReason();
  if (reason) { capSay(reason, 'err'); return; }
  const drafts = capDrafts();
  if (!drafts.length) { capSay(TC.nothing); return; }
  const program = capProgram;
  const changes = drafts.map(d => ({ course_code: d.code, max_capacity: d.value }));
  const btn = q('capSave');
  /* Not `disabled`: the focused button would drop the keyboard to the page. */
  capBusy = true;
  btn.setAttribute('aria-busy', 'true');
  btn.textContent = TC.checking;
  capUpdate();
  capSay('');   // an earlier message is not about this save
  let reload = null;
  try {
    reload = await capReviewAndSave(program, changes, btn);
  } catch (e) {
    capSay(`${T.requestFailed}: ${e && e.message ? e.message : e}`, 'err');
  } finally {
    capBusy = false;
    btn.removeAttribute('aria-busy');
    capUpdate();
  }
  if (!reload) return;
  const keep = new Map();
  if (reload === 'keep') capRowEls().forEach(tr => keep.set(tr.dataset.code, tr.querySelector('.cap-input').value));
  await capLoadRows(program, keep);
};

window.addEventListener('beforeunload', e => {
  if (!capDrafts().length && !capInvalid().length) return;
  e.preventDefault();
  e.returnValue = TC.leave;
});

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
