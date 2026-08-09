(function () {
  'use strict';

  const IS_AR = document.documentElement.lang === 'ar';
  const CONFIG = window.sectionsImportConfig || {};
  const TABLE_COLUMNS = 11;
  const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
  const ALLOWED_SUFFIXES = ['.html', '.htm'];

  const T = {
    noAction: IS_AR ? 'لا يوجد إجراء بعد.' : 'No action yet.',
    requestFailed: IS_AR ? 'تعذر الاتصال بالخادم. حاول مرة أخرى.' : 'Could not reach the server. Please try again.',
    chooseFileFirst: IS_AR ? 'اختر ملف Oracle HTML أولاً.' : 'Choose an Oracle HTML file first.',
    invalidFileType: IS_AR ? 'يجب أن يكون تصدير Oracle ملف HTML أو HTM.' : 'The Oracle export must be an HTML or HTM file.',
    emptyFile: IS_AR ? 'الملف المحدد فارغ.' : 'The selected file is empty.',
    fileTooLarge: IS_AR ? 'حجم الملف أكبر من الحد المسموح (10 ميجابايت).' : 'The file exceeds the 10 MB upload limit.',
    noFile: IS_AR ? 'لم يتم اختيار ملف' : 'No file selected',
    parsing: IS_AR ? 'جارٍ تحليل الملف وحساب الأثر على قاعدة البيانات…' : 'Parsing the file and calculating database impact…',
    parseButton: IS_AR ? 'تحليل ومعاينة الأثر' : 'Parse and preview impact',
    parseFailed: IS_AR ? 'تعذر إنشاء المعاينة.' : 'Could not create the preview.',
    stalePreview: IS_AR ? 'تغيّر الملف أو المصدر أو البرامج. شغّل المعاينة مرة أخرى.' : 'The file, source, or programme selection changed. Run Preview again.',
    chooseProgramme: IS_AR ? 'اختر برنامجًا واحدًا على الأقل، ثم شغّل المعاينة مجددًا. الإدخال محظور حاليًا.' : 'Choose at least one programme, then run Preview again. Insert is currently blocked.',
    selectionNone: IS_AR ? 'لم يتم اختيار أي برنامج — سيكون الإدخال محظورًا.' : 'No programme selected — Insert will remain blocked.',
    selectionCount: (count, values) => IS_AR
      ? `تم اختيار ${count}: ${values.join('، ')}`
      : `${count} selected: ${values.join(', ')}`,
    noPreview: IS_AR ? 'لا توجد معاينة بعد' : 'No preview yet',
    noRows: IS_AR ? 'لم يتم تحليل أي صف بعد.' : 'No rows parsed yet.',
    rowSummary: (shown, total) => IS_AR
      ? `عرض ${shown} من أصل ${total} صفًا محللًا.`
      : `Showing ${shown} of ${total} parsed rows.`,
    noFilterResults: IS_AR ? 'لا توجد صفوف مطابقة للتصفية.' : 'No rows match the current filters.',
    previewReady: (sections, meetings) => IS_AR
      ? `المعاينة جاهزة: ${sections} شعبة و${meetings} لقاء. راجع الأثر ثم أكّد الإدخال.`
      : `Preview ready: ${sections} sections and ${meetings} meetings. Review the impact, then confirm Insert.`,
    unassignedBlocked: (count) => IS_AR
      ? `الإدخال محظور لأن ${count} شعبة ستبقى بلا برنامج. حدّث اختيار البرامج وأعد المعاينة.`
      : `Insert is blocked because ${count} sections would remain unassigned. Update the programme selection and preview again.`,
    cannotImport: IS_AR ? 'لا يمكن إدخال هذه المعاينة بأمان. حدّث الخيارات وأعد المعاينة.' : 'This preview cannot be imported safely. Update the options and preview again.',
    impactInitial: IS_AR ? 'شغّل المعاينة لرؤية الشعب الجديدة والموجودة وتغييرات ارتباط البرامج.' : 'Run Preview to see new and existing sections and programme membership changes.',
    impactDetails: (adds, removes, promotions, assignments) => IS_AR
      ? `ارتباطات فعلية بعد الدمج: ${assignments} · إضافات: ${adds} · إزالة: ${removes} · ترقية إلى مصدر مستورد: ${promotions}`
      : `Effective assignments after merge: ${assignments} · adds: ${adds} · removals: ${removes} · promoted to imported source: ${promotions}`,
    confirmTitle: IS_AR ? 'تأكيد دمج الشعب' : 'Confirm section merge',
    confirmBody: (sections, programs, phrase) => IS_AR
      ? `سيتم دمج <strong>${sections}</strong> شعبة وربطها بالبرامج <strong>${programs}</strong>. ستُستبدل لقاءات الشعب المطابقة، لذلك ستُنشأ نسخة احتياطية أولاً.<br><br>اكتب <code dir="ltr">${phrase}</code> تمامًا للمتابعة.`
      : `This will merge <strong>${sections}</strong> sections for <strong>${programs}</strong>. Meetings on matching sections will be replaced, so a backup is created first.<br><br>Type <code>${phrase}</code> exactly to continue.`,
    exactConfirmation: (phrase) => IS_AR ? `اكتب ${phrase} بالحروف والمسافات نفسها.` : `Type ${phrase} with the exact letters and spacing.`,
    insert: IS_AR ? 'إدخال الشعب' : 'Insert sections',
    inserting: IS_AR ? 'جارٍ إنشاء النسخة الاحتياطية ودمج الشعب…' : 'Creating the backup and merging sections…',
    insertFailed: IS_AR ? 'تعذر إدخال الشعب.' : 'Could not insert the sections.',
    inserted: (sections, meetings, backup) => IS_AR
      ? `تم الدمج بنجاح: ${sections} شعبة و${meetings} لقاء.${backup ? ` النسخة الاحتياطية: ${backup}.` : ''}`
      : `Merge completed: ${sections} sections and ${meetings} meetings.${backup ? ` Backup: ${backup}.` : ''}`,
    department: IS_AR ? 'القسم' : 'Department',
    other: IS_AR ? 'أخرى' : 'Other',
    technicalShow: IS_AR ? 'التفاصيل التقنية' : 'Technical details',
    technicalHide: IS_AR ? 'إخفاء التفاصيل التقنية' : 'Hide technical details',
    unexpectedHtml: IS_AR
      ? 'أعاد الخادم صفحة HTML بدلاً من البيانات المطلوبة. قد تكون الجلسة منتهية؛ حدّث الصفحة وسجّل الدخول مجددًا.'
      : 'The server returned an HTML page instead of the requested data. Your session may have expired; refresh and sign in again.',
    unexpectedResponse: (status, statusText, details) => {
      const suffix = details ? `: ${details}` : '';
      return IS_AR
        ? `استجابة غير متوقعة من الخادم (HTTP ${status}${statusText ? ` ${statusText}` : ''})${suffix}`
        : `Unexpected server response (HTTP ${status}${statusText ? ` ${statusText}` : ''})${suffix}`;
    },
  };

  const state = {
    token: null,
    rows: [],
    totalRows: 0,
    sourceTag: '',
    defaultPrograms: [],
    isDepartment: false,
    canImport: false,
    confirmationPhrase: '',
    impact: {},
    requestVersion: 0,
    isParsing: false,
  };

  function safe(value) {
    return esc(value == null ? '' : value);
  }

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function displayValue(value) {
    return value == null || String(value).trim() === '' ? '—' : value;
  }

  async function readApiResponse(response) {
    const contentType = String(response.headers.get('content-type') || '').toLowerCase();
    const body = await response.text();
    if (contentType.includes('application/json') || contentType.includes('text/json')) {
      try {
        const data = JSON.parse(body);
        if (data && typeof data === 'object') return data;
      } catch (_error) {
        // Fall through to the safe diagnostic response below.
      }
    }

    const looksLikeHtml = contentType.includes('text/html') || /^\s*</.test(body);
    if (looksLikeHtml) {
      return {
        error: T.unexpectedHtml,
        code: 'unexpected_html_response',
        http_status: response.status,
      };
    }
    const details = body.replace(/\s+/g, ' ').trim().slice(0, 240);
    return {
      error: T.unexpectedResponse(response.status, response.statusText, details),
      code: 'unexpected_response',
      http_status: response.status,
    };
  }

  function setStatus(kind, text) {
    const el = q('status');
    el.className = `alert alert-${kind} si-status`;
    el.textContent = text;
  }

  function setInsertEnabled(enabled) {
    const button = q('insertBtn');
    button.disabled = !enabled;
    button.classList.toggle('btn-danger', enabled);
    button.classList.toggle('btn-outline-danger', !enabled);
  }

  function selectedPrograms() {
    return Array.from(document.querySelectorAll('.si-program-checkbox:checked'))
      .map((checkbox) => String(checkbox.value || '').trim().toUpperCase())
      .filter(Boolean)
      .sort();
  }

  function samePrograms(left, right) {
    return left.length === right.length && left.every((value, index) => value === right[index]);
  }

  function programmeTags(programs, emptyLabel) {
    if (!Array.isArray(programs) || !programs.length) {
      return `<span class="si-program-empty">${safe(emptyLabel || '—')}</span>`;
    }
    return programs.map((program) => `<span class="si-program-tag" dir="ltr">${safe(program)}</span>`).join('');
  }

  function updateSelectionStatus() {
    const programs = selectedPrograms();
    const status = q('programSelectionStatus');
    status.textContent = programs.length ? T.selectionCount(programs.length, programs) : T.selectionNone;
    status.classList.toggle('is-blocked', programs.length === 0);
  }

  function clearImpact() {
    ['kSections', 'kMeetings', 'kNew', 'kExisting', 'kMembership', 'kUnassigned']
      .forEach((id) => { q(id).textContent = '0'; });
    q('impactDetails').textContent = T.impactInitial;
    q('effectivePrograms').textContent = T.noPreview;
    q('previewPanel').classList.remove('has-preview', 'has-risk');
  }

  function emptyTable(title, hint) {
    q('tbody').innerHTML = `<tr><td colspan="${TABLE_COLUMNS}"><div class="empty-state"><div class="empty-title">${safe(title)}</div>${hint ? `<div class="empty-hint">${safe(hint)}</div>` : ''}</div></td></tr>`;
  }

  function clearPreviewVisuals() {
    state.rows = [];
    state.totalRows = 0;
    clearImpact();
    emptyTable(IS_AR ? 'لا توجد بيانات بعد' : 'No preview data yet', IS_AR ? 'شغّل المعاينة لعرض الصفوف.' : 'Run Preview to display rows.');
    q('rowSummary').textContent = T.noRows;
  }

  function invalidatePreview(announce) {
    const hadPreview = Boolean(state.token || state.rows.length);
    const wasParsing = state.isParsing;
    state.requestVersion += 1;
    state.isParsing = false;
    state.token = null;
    state.sourceTag = '';
    state.defaultPrograms = [];
    state.isDepartment = false;
    state.canImport = false;
    state.confirmationPhrase = '';
    state.impact = {};
    setInsertEnabled(false);
    clearPreviewVisuals();
    if (announce && (hadPreview || wasParsing)) setStatus('warning', T.stalePreview);
  }

  function rowKey(row) {
    return [row.course_key, row.section, row.day, row.start_time, row.end_time, row.room, row.instructor]
      .map((value) => String(value || '').trim().toUpperCase())
      .join('|');
  }

  function dedupeRows(rows) {
    const seen = new Set();
    return rows.filter((row) => {
      const key = rowKey(row);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function filteredRows(rows) {
    const code = String(q('fCode').value || '').trim().toUpperCase();
    const section = String(q('fSection').value || '').trim().toUpperCase();
    const day = String(q('fDay').value || '').trim().toUpperCase();
    return rows.filter((row) => {
      const course = String(row.course_key || `${row.course_code || ''}${row.course_number || ''}`).toUpperCase();
      return (!code || course.includes(code))
        && (!section || String(row.section || '').toUpperCase().includes(section))
        && (!day || String(row.day || '').toUpperCase().includes(day));
    });
  }

  function renderTable(rows) {
    const tbody = q('tbody');
    tbody.innerHTML = '';
    if (!rows.length) {
      if (state.rows.length) emptyTable(T.noFilterResults, '');
      else emptyTable(IS_AR ? 'لا توجد بيانات بعد' : 'No preview data yet', IS_AR ? 'شغّل المعاينة لعرض الصفوف.' : 'Run Preview to display rows.');
      return;
    }

    rows.forEach((row) => {
      const courseKey = row.course_key || `${row.course_code || ''}${row.course_number || ''}`;
      const effectivePrograms = Array.isArray(row.effective_programs) ? row.effective_programs : state.defaultPrograms;
      const sourceLabel = row.source_tag === 'department' ? T.department : T.other;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong class="si-course-code" dir="ltr">${safe(courseKey)}</strong></td>
        <td>${safe(row.course_name || '—')}</td>
        <td><span dir="ltr">${safe(row.section || '—')}</span></td>
        <td><div class="si-program-tags">${programmeTags(effectivePrograms, IS_AR ? 'بلا برنامج' : 'Unassigned')}</div></td>
        <td>${safe(row.day || '—')}</td>
        <td><span class="si-time" dir="ltr">${safe(row.start_time || '—')} – ${safe(row.end_time || '—')}</span></td>
        <td>${safe(displayValue(row.available_capacity))}</td>
        <td>${safe(displayValue(row.registered_count))}</td>
        <td><span dir="ltr">${safe(row.room || '—')}</span></td>
        <td>${safe(row.instructor || '—')}</td>
        <td><span class="si-source-tag">${safe(sourceLabel)}</span></td>`;
      tbody.appendChild(tr);
    });
  }

  function renderCurrentRows() {
    const rows = filteredRows(state.rows);
    renderTable(rows);
    q('rowSummary').textContent = state.rows.length
      ? T.rowSummary(rows.length, state.totalRows)
      : T.noRows;
  }

  function renderImpact(data) {
    const impact = data.impact || {};
    const sections = number(impact.sections_unique);
    const meetings = number(impact.meeting_rows_unique);
    const additions = number(impact.membership_adds);
    const removals = number(impact.membership_removes);
    const sourceChanges = number(impact.membership_source_changes || impact.membership_promotions);
    const promotions = number(impact.membership_promotions);
    const unassigned = number(impact.predicted_fully_unassigned_sections);
    const assignments = number(impact.programme_assignments_effective);

    q('kSections').textContent = String(sections);
    q('kMeetings').textContent = String(meetings);
    q('kNew').textContent = String(number(impact.sections_new));
    q('kExisting').textContent = String(number(impact.sections_existing));
    q('kMembership').textContent = String(additions + removals + sourceChanges);
    q('kUnassigned').textContent = String(unassigned);
    q('impactDetails').textContent = T.impactDetails(additions, removals, promotions, assignments);
    q('effectivePrograms').innerHTML = programmeTags(data.default_programs || [], IS_AR ? 'لم يُعيّن برنامج' : 'No programme assigned');
    q('previewPanel').classList.add('has-preview');
    q('previewPanel').classList.toggle('has-risk', unassigned > 0);
  }

  function applyPreview(data, captured) {
    const rows = dedupeRows(Array.isArray(data.preview_rows) ? data.preview_rows : []);
    const responsePrograms = Array.isArray(data.default_programs)
      ? data.default_programs.map((value) => String(value).trim().toUpperCase()).filter(Boolean).sort()
      : captured.programs;

    state.token = data.token || null;
    state.rows = rows;
    state.totalRows = number(data.total_rows);
    state.sourceTag = data.source_tag || (captured.isDepartment ? 'department' : 'other');
    state.defaultPrograms = responsePrograms;
    state.isDepartment = captured.isDepartment;
    state.canImport = data.can_import === true;
    state.confirmationPhrase = String(data.confirmation_phrase || data.expected_confirmation || '');
    state.impact = data.impact || {};

    renderImpact(data);
    renderCurrentRows();

    const unassigned = number(state.impact.predicted_fully_unassigned_sections);
    const sections = number(state.impact.sections_unique);
    const meetings = number(state.impact.meeting_rows_unique);
    const canInsert = Boolean(state.token && state.canImport && responsePrograms.length && sections > 0);
    setInsertEnabled(canInsert);
    if (!responsePrograms.length || data.program_selection_required) {
      setStatus('warning', T.chooseProgramme);
    } else if (unassigned > 0) {
      setStatus('danger', T.unassignedBlocked(unassigned));
    } else if (!canInsert) {
      setStatus('danger', T.cannotImport);
    } else {
      setStatus('success', T.previewReady(sections, meetings));
    }
  }

  function validateFile(file) {
    if (!file) return T.chooseFileFirst;
    const lowerName = String(file.name || '').toLowerCase();
    if (!ALLOWED_SUFFIXES.some((suffix) => lowerName.endsWith(suffix))) return T.invalidFileType;
    if (!file.size) return T.emptyFile;
    if (file.size > MAX_UPLOAD_BYTES) return T.fileTooLarge;
    return '';
  }

  function restoreParseButton() {
    q('parseBtn').disabled = false;
    q('parseBtn').textContent = T.parseButton;
  }

  async function parsePreview() {
    const file = q('oracleFile').files[0];
    const validationError = validateFile(file);
    if (validationError) {
      notify.warning(validationError);
      setStatus('warning', validationError);
      return;
    }

    invalidatePreview(false);
    const requestVersion = ++state.requestVersion;
    state.isParsing = true;
    const captured = {
      programs: selectedPrograms(),
      isDepartment: q('dept').checked,
    };
    q('parseBtn').disabled = true;
    q('parseBtn').textContent = IS_AR ? 'جارٍ التحليل…' : 'Parsing…';
    setStatus('info', T.parsing);

    const form = new FormData();
    form.append('oracle_file', file);
    form.append('is_department', captured.isDepartment ? '1' : '0');
    captured.programs.forEach((program) => form.append('default_programs', program));

    try {
      const response = await fetch(CONFIG.previewUrl || '/ops/sections-import/preview/', {
        method: 'POST',
        headers: { 'X-CSRFToken': getCsrfToken() },
        body: form,
      });
      const data = await readApiResponse(response);
      if (requestVersion !== state.requestVersion) return;
      state.isParsing = false;
      q('out').textContent = JSON.stringify(data, null, 2);
      if (!response.ok || data.error || !Array.isArray(data.preview_rows)) {
        setStatus('danger', data.error || T.parseFailed);
        return;
      }
      applyPreview(data, captured);
    } catch (_error) {
      if (requestVersion === state.requestVersion) {
        state.isParsing = false;
        setStatus('danger', T.requestFailed);
      }
    } finally {
      restoreParseButton();
    }
  }

  function backupLabel(backup) {
    if (!backup || typeof backup !== 'object') return '';
    if (backup.skipped) return IS_AR ? 'تُدار بواسطة مزود قاعدة البيانات' : 'managed by the database provider';
    return String(backup.backup_file || '');
  }

  async function insertPreview() {
    const currentPrograms = selectedPrograms();
    if (!state.token || !state.canImport) {
      notify.warning(T.chooseProgramme);
      return;
    }
    if (!samePrograms(currentPrograms, state.defaultPrograms) || q('dept').checked !== state.isDepartment) {
      invalidatePreview(true);
      notify.warning(T.stalePreview);
      return;
    }

    const sections = number(state.impact.sections_unique);
    const phrase = state.confirmationPhrase || `IMPORT ${sections}`;
    const programsHtml = state.defaultPrograms.map(safe).join(IS_AR ? '، ' : ', ');
    const confirmation = await dlg.confirm({
      title: T.confirmTitle,
      body: T.confirmBody(sections, programsHtml, safe(phrase)),
      kind: 'warning',
      confirmText: T.insert,
      cancelText: IS_AR ? 'إلغاء' : 'Cancel',
      typed: phrase,
    });
    if (!confirmation) return;
    if (confirmation !== phrase) {
      notify.warning(T.exactConfirmation(phrase));
      return;
    }

    q('insertBtn').disabled = true;
    q('parseBtn').disabled = true;
    q('insertBtn').textContent = IS_AR ? 'جارٍ الإدخال…' : 'Inserting…';
    setStatus('info', T.inserting);
    try {
      const response = await fetch(CONFIG.insertUrl || '/ops/sections-import/insert/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': getCsrfToken(),
        },
        body: JSON.stringify({
          token: state.token,
          is_department: state.isDepartment,
          default_programs: state.defaultPrograms,
          confirmation,
        }),
      });
      const data = await readApiResponse(response);
      q('out').textContent = JSON.stringify(data, null, 2);
      if (!response.ok || data.error) {
        setStatus('danger', data.error || T.insertFailed);
        if (['preview_changed', 'preview_stale', 'preview_expired'].includes(data.code)) invalidatePreview(false);
        else setInsertEnabled(state.canImport);
        return;
      }

      const finalImpact = data.impact || state.impact;
      const finalSections = number(finalImpact.sections_unique || data.rows_total);
      const finalMeetings = number(finalImpact.meeting_rows_unique || data.meetings_total);
      state.token = null;
      state.canImport = false;
      setInsertEnabled(false);
      setStatus('success', T.inserted(finalSections, finalMeetings, backupLabel(data.backup)));
    } catch (_error) {
      setStatus('danger', T.requestFailed);
      setInsertEnabled(state.canImport);
    } finally {
      q('insertBtn').textContent = T.insert;
      q('parseBtn').disabled = false;
    }
  }

  function initFilters() {
    ['fCode', 'fSection', 'fDay'].forEach((id) => {
      q(id).addEventListener('input', renderCurrentRows);
    });

    const ids = ['fCode', 'fSection', 'fDay'];
    function closeAll() {
      ids.forEach((id) => {
        q(`${id}Pop`).classList.remove('open');
        q(`${id}Icon`).setAttribute('aria-expanded', 'false');
      });
    }
    function syncIcon(id) {
      const active = q(id).value.trim().length > 0;
      q(`${id}Icon`).classList.toggle('active', active);
      q(`${id}Clear`).classList.toggle('visible', active);
    }
    ids.forEach((id) => {
      const header = q(`${id}Icon`).closest('.si-filterable');
      header.addEventListener('click', (event) => {
        if (event.target.closest('.si-filter-pop')) return;
        event.stopPropagation();
        const popover = q(`${id}Pop`);
        const open = popover.classList.contains('open');
        closeAll();
        if (!open) {
          popover.classList.add('open');
          q(`${id}Icon`).setAttribute('aria-expanded', 'true');
          q(id).focus();
        }
      });
      q(`${id}Pop`).addEventListener('click', (event) => event.stopPropagation());
      q(id).addEventListener('input', () => syncIcon(id));
      q(id).addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          closeAll();
          q(`${id}Icon`).focus();
        }
      });
      q(`${id}Clear`).addEventListener('click', () => {
        q(id).value = '';
        syncIcon(id);
        renderCurrentRows();
        closeAll();
      });
    });
    document.addEventListener('click', closeAll);
  }

  function initUpload() {
    const dropzone = q('siDropzone');
    const fileInput = q('oracleFile');
    const openPicker = () => fileInput.click();
    dropzone.addEventListener('click', openPicker);
    dropzone.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openPicker();
      }
    });
    dropzone.addEventListener('dragover', (event) => {
      event.preventDefault();
      dropzone.classList.add('dragover');
    });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
    dropzone.addEventListener('drop', (event) => {
      event.preventDefault();
      dropzone.classList.remove('dragover');
      if (!event.dataTransfer.files.length) return;
      fileInput.files = event.dataTransfer.files;
      fileInput.dispatchEvent(new Event('change'));
    });
    fileInput.addEventListener('change', () => {
      const file = fileInput.files[0];
      q('oracleFileName').textContent = file ? file.name : T.noFile;
      dropzone.classList.toggle('has-file', Boolean(file));
      invalidatePreview(true);
      if (file) {
        const error = validateFile(file);
        setStatus(error ? 'warning' : 'info', error || (IS_AR ? 'الملف جاهز. اختر البرامج ثم شغّل المعاينة.' : 'File ready. Choose programmes, then run Preview.'));
      }
    });
  }

  function initProgrammes() {
    const checkboxes = Array.from(document.querySelectorAll('.si-program-checkbox'));
    checkboxes.forEach((checkbox) => checkbox.addEventListener('change', () => {
      updateSelectionStatus();
      invalidatePreview(true);
    }));
    q('selectAllPrograms').disabled = checkboxes.length === 0;
    q('clearPrograms').disabled = checkboxes.length === 0;
    q('selectAllPrograms').addEventListener('click', () => {
      checkboxes.forEach((checkbox) => { checkbox.checked = true; });
      updateSelectionStatus();
      invalidatePreview(true);
    });
    q('clearPrograms').addEventListener('click', () => {
      checkboxes.forEach((checkbox) => { checkbox.checked = false; });
      updateSelectionStatus();
      invalidatePreview(true);
    });
    updateSelectionStatus();
  }

  q('dept').addEventListener('change', () => invalidatePreview(true));
  q('parseBtn').addEventListener('click', parsePreview);
  q('insertBtn').addEventListener('click', insertPreview);
  q('toggleTech').addEventListener('click', () => {
    const wrap = q('techWrap');
    const willOpen = wrap.classList.contains('d-none');
    wrap.classList.toggle('d-none', !willOpen);
    q('toggleTech').setAttribute('aria-expanded', String(willOpen));
    q('toggleTech').textContent = willOpen ? T.technicalHide : T.technicalShow;
  });

  initFilters();
  initUpload();
  initProgrammes();
  clearImpact();
  setInsertEnabled(false);
})();
