/* ═══════════════════════════════════════════════════════════════
   Instructor Management — Client-side logic
   ═══════════════════════════════════════════════════════════════ */

// ── STATE ──
let allInstructors = [];
let allSections = [];
let currentScenario = null;
let currentReportScenario = null;
let selectedSections = new Set();
let isEditMode = false;

const IS_AR = document.documentElement.lang === 'ar';

const T = {
  // ── Loading & errors ──
  loading:              IS_AR ? 'جارٍ التحميل…' : 'Loading…',
  failedLoadInstructors: IS_AR ? 'تعذّر تحميل هيئة التدريس' : 'Failed to load instructors',
  failedLoadSections:   IS_AR ? 'تعذّر تحميل الشعب' : 'Failed to load sections',
  failedLoadReport:     IS_AR ? 'تعذّر تحميل التقرير' : 'Failed to load report',
  networkError:         IS_AR ? 'خطأ في الاتصال' : 'Network error',

  // ── Success messages ──
  instructorSaved:      IS_AR ? 'تم حفظ بيانات عضو هيئة التدريس' : 'Instructor saved successfully',
  assignmentUpdated:    IS_AR ? 'تم تحديث التوزيع' : 'Assignment updated',
  bulkAssignCompleted:  IS_AR ? 'تم تطبيق التوزيع المجمع' : 'Bulk assignment completed',

  // ── Empty states ──
  noInstructors:        IS_AR ? 'لا يوجد أعضاء هيئة تدريس' : 'No instructors found',
  noSections:           IS_AR ? 'لا توجد شعب في هذا السيناريو' : 'No sections in this scenario',
  noReportData:         IS_AR ? 'لا توجد بيانات للتقرير' : 'No report data available',

  // ── Status labels ──
  active:               IS_AR ? 'نشط' : 'Active',
  inactive:             IS_AR ? 'غير نشط' : 'Inactive',
  published:            IS_AR ? 'منشور' : 'Published',

  // ── Load status ──
  under:                IS_AR ? 'أقل من المطلوب' : 'Under',
  at:                   IS_AR ? 'في الحد المطلوب' : 'At Capacity',
  over:                 IS_AR ? 'فوق الحد المسموح' : 'Overloaded',
  na:                   IS_AR ? 'غير محدد' : 'N/A',

  // ── Actions ──
  edit:                 IS_AR ? 'تعديل' : 'Edit',
  activate:             IS_AR ? 'تفعيل' : 'Activate',
  deactivate:           IS_AR ? 'تعطيل' : 'Deactivate',
  assign:               IS_AR ? 'توزيع' : 'Assign',
  unassign:             IS_AR ? 'إلغاء التوزيع' : 'Unassign',
  bulkAssign:           IS_AR ? 'توزيع مجمع' : 'Bulk Assign',

  // ── Form validation ──
  nameRequired:         IS_AR ? 'الاسم مطلوب' : 'Name is required',
  selectInstructor:     IS_AR ? 'يرجى اختيار عضو هيئة تدريس' : 'Please select an instructor',
  selectSections:       IS_AR ? 'يرجى اختيار الشعب' : 'Please select sections',

  // ── Confirmation ──
  confirmDeactivate:    IS_AR ? 'هل تريد تعطيل هذا العضو؟' : 'Deactivate this instructor?',
  confirmUnassign:      IS_AR ? 'هل تريد إلغاء هذا التوزيع؟' : 'Remove this assignment?',

  // ── Scenario notices ──
  scenarioPublished:    IS_AR ? 'هذا السيناريو منشور - لا يمكن تعديل التوزيعات' : 'This scenario is published - assignments cannot be modified',
  selectScenario:       IS_AR ? 'يرجى اختيار سيناريو' : 'Please select a scenario',

  // ── Report labels ──
  instructor:           IS_AR ? 'عضو هيئة التدريس' : 'Instructor',
  department:           IS_AR ? 'القسم' : 'Department',
  sections:             IS_AR ? 'الشعب' : 'Sections',
  courses:              IS_AR ? 'المقررات' : 'Courses',
  creditHours:          IS_AR ? 'الساعات المعتمدة' : 'Credit Hours',
  contactHours:         IS_AR ? 'ساعات الاتصال' : 'Contact Hours',
  teachingDays:         IS_AR ? 'أيام التدريس' : 'Teaching Days',
  clashes:              IS_AR ? 'التعارضات' : 'Clashes',
  loadStatus:           IS_AR ? 'حالة العبء' : 'Load Status',
  totalRow:             IS_AR ? 'الإجمالي' : 'TOTAL',

  // ── Time labels ──
  sunday:               IS_AR ? 'الأحد' : 'Sun',
  monday:               IS_AR ? 'الإثنين' : 'Mon',
  tuesday:              IS_AR ? 'الثلاثاء' : 'Tue',
  wednesday:            IS_AR ? 'الأربعاء' : 'Wed',
  thursday:             IS_AR ? 'الخميس' : 'Thu',

  // ── Counts ──
  nInstructors:      (n) => IS_AR ? `${n} عضو` : `${n} instructors`,
  nSections:         (n) => IS_AR ? `${n} شعبة` : `${n} sections`,
  nSelected:         (n) => IS_AR ? `${n} محدد` : `${n} selected`,
  nAssigned:         (n) => IS_AR ? `تم توزيع ${n}` : `${n} assigned`,
  nSkipped:          (n) => IS_AR ? `تم تخطي ${n}` : `${n} skipped`,
};

// ── CSRF Token helper ──
function getCsrfToken() {
  return djCsrfToken || document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
}

// ── Notification helper ──
// notify is already available from notify.js loaded in base.html

// ── DOM helpers ──
const $ = id => document.getElementById(id);
const $$ = selector => document.querySelectorAll(selector);

// ── Tab Management ──
let currentTab = 'roster';

function imSwitchTab(tab) {
  // Update tab buttons
  $$('.im-tab').forEach(btn => {
    btn.classList.remove('im-tab-active');
    btn.setAttribute('aria-selected', 'false');
  });
  $(`imTabBtn${tab.charAt(0).toUpperCase() + tab.slice(1)}`).classList.add('im-tab-active');
  $(`imTabBtn${tab.charAt(0).toUpperCase() + tab.slice(1)}`).setAttribute('aria-selected', 'true');

  // Update tab content
  $$('.im-tab-content').forEach(content => {
    content.classList.remove('im-tab-content-active');
  });
  $(`imTab${tab.charAt(0).toUpperCase() + tab.slice(1)}`).classList.add('im-tab-content-active');

  currentTab = tab;

  // Load scenarios for assignment/report tabs
  if (tab === 'roster') {
    imLoadInstructors();
    imLoadScenarios();
  } else if (tab === 'report') {
    imLoadReportScenarios();
  }
}

// ── API Calls ──

async function imApiCall(endpoint, options = {}) {
  try {
    const response = await fetch(endpoint, {
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrfToken(),
        ...options.headers
      },
      ...options
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error('API call failed:', error);
    throw error;
  }
}

// ── Load Instructors ──
async function imLoadInstructors() {
  try {
    const includeInactive = $('imIncludeInactive').checked ? 1 : 0;
    const searchQ = $('imInstructorSearch').value.trim();

    const params = new URLSearchParams({
      q: searchQ,
      include_inactive: includeInactive
    });

    const data = await imApiCall(`/ops/instructors/list/?${params}`);

    if (data.ok) {
      allInstructors = data.instructors;
      imRenderInstructorTable();
      $('imInstructorCount').textContent = T.nInstructors(allInstructors.length);
    } else {
      throw new Error(data.error?.message || 'Failed to load instructors');
    }
  } catch (error) {
    notify(T.failedLoadInstructors + ': ' + error.message, 'error');
    imRenderInstructorTable([]); // Empty table
  }
}

function imRenderInstructorTable(instructors = null) {
  const tbody = $('imInstructorTable').getElementsByTagName('tbody')[0];
  const data = instructors || allInstructors;

  if (data.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-note">${T.noInstructors}</td></tr>`;
    return;
  }

  tbody.innerHTML = data.map(instructor => `
    <tr>
      <td>
        <div class="im-instructor-name">
          <strong>${escapeHtml(instructor.full_name)}</strong>
          ${instructor.full_name_ar ? `<div class="text-muted fs-sm">${escapeHtml(instructor.full_name_ar)}</div>` : ''}
        </div>
      </td>
      <td><span class="pill-neutral">${escapeHtml(instructor.department || '—')}</span></td>
      <td class="text-muted fs-sm">${escapeHtml(instructor.email || '—')}</td>
      <td class="text-muted fs-sm">${escapeHtml(instructor.employee_no || '—')}</td>
      <td class="text-center">${instructor.max_weekly_hours || '—'}</td>
      <td>
        <button class="pill-status ${instructor.is_active ? 'pill-status-success' : 'pill-status-muted'}"
                onclick="imToggleInstructorActive(${instructor.id}, ${!instructor.is_active})"
                title="${instructor.is_active ? T.clickToDisable : T.clickToEnable}">
          ${instructor.is_active ? T.active : T.inactive}
        </button>
      </td>
      <td>
        <div class="im-row-actions">
          <button class="im-row-btn" onclick="imEditInstructor(${instructor.id})"
                  title="${T.edit}" aria-label="${T.edit} ${escapeHtml(instructor.full_name)}">
            <svg viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          </button>
        </div>
      </td>
    </tr>
  `).join('');
}

function imFilterInstructors() {
  const search = $('imInstructorSearch').value.toLowerCase().trim();
  const includeInactive = $('imIncludeInactive').checked;

  let filtered = allInstructors;

  if (!includeInactive) {
    filtered = filtered.filter(i => i.is_active);
  }

  if (search) {
    filtered = filtered.filter(i =>
      i.full_name.toLowerCase().includes(search) ||
      (i.full_name_ar && i.full_name_ar.includes(search)) ||
      (i.email && i.email.toLowerCase().includes(search)) ||
      (i.department && i.department.toLowerCase().includes(search)) ||
      (i.employee_no && i.employee_no.toLowerCase().includes(search))
    );
  }

  imRenderInstructorTable(filtered);
}

// ── Instructor CRUD ──
function imCreateInstructor() {
  isEditMode = false;
  $('imInstructorModalLabel').textContent = IS_AR ? 'إضافة عضو هيئة تدريس' : 'Add Instructor';
  imClearInstructorForm();
  imShowInstructorModal();
}

function imEditInstructor(instructorId) {
  const instructor = allInstructors.find(i => i.id === instructorId);
  if (!instructor) return;

  isEditMode = true;
  $('imInstructorModalLabel').textContent = IS_AR ? 'تعديل عضو هيئة تدريس' : 'Edit Instructor';

  $('imInstructorId').value = instructor.id;
  $('imInstructorName').value = instructor.full_name;
  $('imInstructorNameAr').value = instructor.full_name_ar || '';
  $('imInstructorEmail').value = instructor.email || '';
  $('imInstructorEmployeeNo').value = instructor.employee_no || '';
  $('imInstructorDepartment').value = instructor.department || '';
  $('imInstructorMaxHours').value = instructor.max_weekly_hours || '';
  $('imInstructorActive').checked = instructor.is_active;

  imShowInstructorModal();
}

function imClearInstructorForm() {
  $('imInstructorId').value = '';
  $('imInstructorName').value = '';
  $('imInstructorNameAr').value = '';
  $('imInstructorEmail').value = '';
  $('imInstructorEmployeeNo').value = '';
  $('imInstructorDepartment').value = '';
  $('imInstructorMaxHours').value = '';
  $('imInstructorActive').checked = true;
}

function imShowInstructorModal() {
  const modal = $('imInstructorModal');
  modal.style.display = 'block';
  modal.setAttribute('aria-hidden', 'false');
  // Focus first input
  setTimeout(() => $('imInstructorName').focus(), 100);
}

function imHideInstructorModal() {
  const modal = $('imInstructorModal');
  modal.style.display = 'none';
  modal.setAttribute('aria-hidden', 'true');
}

function imShowBulkModal() {
  const modal = $('imBulkAssignModal');
  modal.style.display = 'block';
  modal.setAttribute('aria-hidden', 'false');
  // Focus first select
  setTimeout(() => $('imBulkInstructor').focus(), 100);
}

function imHideBulkModal() {
  const modal = $('imBulkAssignModal');
  modal.style.display = 'none';
  modal.setAttribute('aria-hidden', 'true');
}

async function imSaveInstructor() {
  const name = $('imInstructorName').value.trim();
  if (!name) {
    notify(T.nameRequired, 'error');
    return;
  }

  const payload = {
    full_name: name,
    full_name_ar: $('imInstructorNameAr').value.trim() || null,
    email: $('imInstructorEmail').value.trim() || null,
    employee_no: $('imInstructorEmployeeNo').value.trim() || null,
    department: $('imInstructorDepartment').value.trim() || null,
    max_weekly_hours: parseInt($('imInstructorMaxHours').value) || null
  };

  if (isEditMode) {
    payload.id = parseInt($('imInstructorId').value);
  }

  try {
    const endpoint = isEditMode ? '/ops/instructors/update/' : '/ops/instructors/create/';
    const data = await imApiCall(endpoint, {
      method: 'POST',
      body: JSON.stringify(payload)
    });

    if (data.ok) {
      notify(T.instructorSaved, 'success');
      imHideInstructorModal();
      imLoadInstructors();
    } else {
      throw new Error(data.error?.message || 'Failed to save instructor');
    }
  } catch (error) {
    notify(error.message, 'error');
  }
}

async function imToggleInstructorActive(instructorId, isActive) {
  try {
    const data = await imApiCall('/ops/instructors/set-active/', {
      method: 'POST',
      body: JSON.stringify({ id: instructorId, is_active: isActive })
    });

    if (data.ok) {
      const action = isActive ? T.activate : T.deactivate;
      notify(`${action} ${T.instructor}`, 'success');
      imLoadInstructors();
    } else {
      throw new Error(data.error?.message || 'Failed to update instructor');
    }
  } catch (error) {
    notify(error.message, 'error');
  }
}

// ── Load Scenarios ──
async function imLoadScenarios() {
  try {
    const params = new URLSearchParams({
      year: default_year,
      term: default_term
    });

    const data = await imApiCall(`/ops/tw/scenarios/?${params}`);

    if (data.ok) {
      const select = $('imScenarioSelect');
      select.innerHTML = '<option value="">' + T.selectScenario + '</option>' +
        data.scenarios.map(s =>
          `<option value="${s.id}">${escapeHtml(s.name)} (${s.status})</option>`
        ).join('');
    }
  } catch (error) {
    console.error('Failed to load scenarios:', error);
  }
}

async function imLoadReportScenarios() {
  try {
    const params = new URLSearchParams({
      year: default_year,
      term: default_term
    });

    const data = await imApiCall(`/ops/tw/scenarios/?${params}`);

    if (data.ok) {
      const select = $('imReportScenarioSelect');
      select.innerHTML = '<option value="">' + T.selectScenario + '</option>' +
        data.scenarios.map(s =>
          `<option value="${s.id}">${escapeHtml(s.name)} (${s.status})</option>`
        ).join('');
    }
  } catch (error) {
    console.error('Failed to load scenarios:', error);
  }
}

// ── Load Sections ──
async function imLoadSections() {
  const scenarioId = $('imScenarioSelect').value;
  if (!scenarioId) {
    $('imAssignmentContent').innerHTML = `
      <div class="im-assignment-notice">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="8"/></svg>
        <span>${T.selectScenario}</span>
      </div>`;
    return;
  }

  currentScenario = scenarioId;

  try {
    const params = new URLSearchParams({
      scenario_id: scenarioId,
      q: ''
    });

    const data = await imApiCall(`/ops/instructors/sections/?${params}`);

    if (data.ok) {
      allSections = data.sections;
      imRenderAssignments(data);
    } else {
      throw new Error(data.error?.message || 'Failed to load sections');
    }
  } catch (error) {
    notify(T.failedLoadSections + ': ' + error.message, 'error');
    $('imAssignmentContent').innerHTML = `
      <div class="im-assignment-notice">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="8"/></svg>
        <span>${T.failedLoadSections}</span>
      </div>`;
  }
}

function imRenderAssignments(data) {
  const isPublished = data.sections.length > 0 &&
    $$('#imScenarioSelect option:checked')[0]?.textContent.includes('published');

  if (data.sections.length === 0) {
    $('imAssignmentContent').innerHTML = `
      <div class="im-assignment-notice">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="8"/></svg>
        <span>${T.noSections}</span>
      </div>`;
    return;
  }

  let html = '';

  if (isPublished) {
    html += `<div class="im-publish-notice">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="8"/></svg>
      <span>${T.scenarioPublished}</span>
    </div>`;
  }

  if (!isPublished) {
    html += `<div class="im-bulk-actions">
      <button class="btn btn-primary" onclick="imShowBulkAssign()"
              ${selectedSections.size === 0 ? 'disabled' : ''}>
        ${T.bulkAssign} <span id="imSelectedCount">(${T.nSelected(selectedSections.size)})</span>
      </button>
    </div>`;
  }

  html += '<div class="table-wrap">';
  html += '<table class="tbl-card">';
  html += '<thead><tr>';
  if (!isPublished) {
    html += '<th style="width:40px"><input type="checkbox" onchange="imToggleAllSections(this)"></th>';
  }
  html += `
    <th>${T.courses}</th>
    <th>${IS_AR ? 'الشعبة' : 'Section'}</th>
    <th>${IS_AR ? 'اسم المقرر' : 'Course Name'}</th>
    <th>${T.instructor}</th>
  `;
  if (!isPublished) {
    html += `<th>${IS_AR ? 'الإجراءات' : 'Actions'}</th>`;
  }
  html += '</tr></thead><tbody>';

  data.sections.forEach(section => {
    html += '<tr>';
    if (!isPublished) {
      html += `<td><input type="checkbox" onchange="imToggleSection(${section.term_section_id}, this)" ${selectedSections.has(section.term_section_id) ? 'checked' : ''}></td>`;
    }
    html += `
      <td><span class="pill-neutral">${escapeHtml(section.course_code)}</span></td>
      <td class="text-center">${escapeHtml(section.section)}</td>
      <td class="text-muted">${escapeHtml(section.course_name)}</td>
      <td>
        <div class="im-instructor-chips">
          ${section.instructors.map(inst => `
            <span class="im-instructor-chip">
              ${escapeHtml(inst.full_name)}
              ${!isPublished ? `<button onclick="imUnassignInstructor(${section.term_section_id}, ${inst.id})"
                              title="${T.unassign}" aria-label="${T.unassign} ${escapeHtml(inst.full_name)}">×</button>` : ''}
            </span>
          `).join('')}
          ${!isPublished && section.instructors.length < 2 ? `
            <button class="im-add-instructor" onclick="imShowAssignInstructor(${section.term_section_id})"
                    title="${T.assign}" aria-label="${T.assign}">+</button>
          ` : ''}
        </div>
      </td>
    `;
    if (!isPublished) {
      html += `<td>
        <button class="im-row-btn" onclick="imShowAssignInstructor(${section.term_section_id})"
                title="${T.assign}">
          <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
        </button>
      </td>`;
    }
    html += '</tr>';
  });

  html += '</tbody></table></div>';

  $('imAssignmentContent').innerHTML = html;
}

function imToggleSection(termSectionId, checkbox) {
  if (checkbox.checked) {
    selectedSections.add(termSectionId);
  } else {
    selectedSections.delete(termSectionId);
  }
  imUpdateBulkActions();
}

function imToggleAllSections(checkbox) {
  const sectionCheckboxes = $$('#imAssignmentContent input[type="checkbox"]:not([onchange*="imToggleAllSections"])');
  sectionCheckboxes.forEach(cb => {
    cb.checked = checkbox.checked;
    const termSectionId = parseInt(cb.getAttribute('onchange').match(/\d+/)[0]);
    if (checkbox.checked) {
      selectedSections.add(termSectionId);
    } else {
      selectedSections.delete(termSectionId);
    }
  });
  imUpdateBulkActions();
}

function imUpdateBulkActions() {
  const count = selectedSections.size;
  const bulkBtn = document.querySelector('.im-bulk-actions button');
  const countSpan = $('imSelectedCount');

  if (bulkBtn) {
    bulkBtn.disabled = count === 0;
  }
  if (countSpan) {
    countSpan.textContent = `(${T.nSelected(count)})`;
  }
}

// ── Assignment Actions ──
function imShowAssignInstructor(termSectionId) {
  // Simple prompt for now - could be enhanced with a modal
  const activeInstructors = allInstructors.filter(i => i.is_active);
  const options = activeInstructors.map((inst, idx) => `${idx + 1}. ${inst.full_name}`).join('\n');
  const choice = prompt(`${T.selectInstructor}:\n\n${options}\n\n${IS_AR ? 'أدخل الرقم:' : 'Enter number:'}`);

  if (choice && !isNaN(choice)) {
    const instructor = activeInstructors[parseInt(choice) - 1];
    if (instructor) {
      imAssignInstructor(termSectionId, instructor.id);
    }
  }
}

async function imAssignInstructor(termSectionId, instructorId) {
  try {
    const data = await imApiCall('/ops/instructors/assign/', {
      method: 'POST',
      body: JSON.stringify({
        term_section_id: termSectionId,
        instructor_id: instructorId
      })
    });

    if (data.ok) {
      notify(T.assignmentUpdated, 'success');
      imLoadSections(); // Reload to show updated assignments
    } else {
      throw new Error(data.error?.message || 'Failed to assign instructor');
    }
  } catch (error) {
    notify(error.message, 'error');
  }
}

async function imUnassignInstructor(termSectionId, instructorId) {
  const confirmed = await dlg.confirm({
    title: T.unassign,
    body: T.confirmUnassign,
    kind: 'warning'
  });
  if (!confirmed) return;

  try {
    const data = await imApiCall('/ops/instructors/unassign/', {
      method: 'POST',
      body: JSON.stringify({
        term_section_id: termSectionId,
        instructor_id: instructorId
      })
    });

    if (data.ok) {
      notify(T.assignmentUpdated, 'success');
      imLoadSections();
    } else {
      throw new Error(data.error?.message || 'Failed to unassign instructor');
    }
  } catch (error) {
    notify(error.message, 'error');
  }
}

// ── Bulk Assignment ──
function imShowBulkAssign() {
  if (selectedSections.size === 0) {
    notify(T.selectSections, 'error');
    return;
  }

  // Populate instructor dropdown
  const select = $('imBulkInstructor');
  const activeInstructors = allInstructors.filter(i => i.is_active);
  select.innerHTML = '<option value="">' + T.selectInstructor + '</option>' +
    activeInstructors.map(inst =>
      `<option value="${inst.id}">${escapeHtml(inst.full_name)}</option>`
    ).join('');

  // Show selected sections
  const selectedSectionsList = Array.from(selectedSections).map(id => {
    const section = allSections.find(s => s.term_section_id === id);
    return section ? `${section.course_code}-${section.section}` : `ID ${id}`;
  });

  $('imBulkSectionsList').innerHTML = selectedSectionsList.map(s =>
    `<span class="im-bulk-section-tag">${escapeHtml(s)}</span>`
  ).join('');

  imShowBulkModal();
}

async function imExecuteBulkAssign() {
  const instructorId = $('imBulkInstructor').value;
  if (!instructorId) {
    notify(T.selectInstructor, 'error');
    return;
  }

  try {
    const data = await imApiCall('/ops/instructors/assign-bulk/', {
      method: 'POST',
      body: JSON.stringify({
        instructor_id: parseInt(instructorId),
        term_section_ids: Array.from(selectedSections)
      })
    });

    if (data.ok) {
      const message = `${T.bulkAssignCompleted}: ${T.nAssigned(data.assigned)}, ${T.nSkipped(data.skipped)}`;
      notify(message, 'success');
      imHideBulkModal();
      selectedSections.clear();
      imLoadSections();
    } else {
      throw new Error(data.error?.message || 'Failed to perform bulk assignment');
    }
  } catch (error) {
    notify(error.message, 'error');
  }
}

// ── Load Report ──
async function imLoadReport() {
  const scenarioId = $('imReportScenarioSelect').value;
  if (!scenarioId) {
    $('imReportContent').innerHTML = `
      <div class="im-assignment-notice">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="8"/></svg>
        <span>${T.selectScenario}</span>
      </div>`;
    return;
  }

  currentReportScenario = scenarioId;

  try {
    const params = new URLSearchParams({ scenario_id: scenarioId });
    const data = await imApiCall(`/ops/instructors/load-report/?${params}`);

    if (data.ok) {
      imRenderReport(data);
    } else {
      throw new Error(data.error?.message || 'Failed to load report');
    }
  } catch (error) {
    notify(T.failedLoadReport + ': ' + error.message, 'error');
    $('imReportContent').innerHTML = `
      <div class="im-assignment-notice">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="8"/></svg>
        <span>${T.noReportData}</span>
      </div>`;
  }
}

function imRenderReport(data) {
  if (!data.rows || data.rows.length === 0) {
    $('imReportContent').innerHTML = `
      <div class="im-assignment-notice">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="8"/></svg>
        <span>${T.noReportData}</span>
      </div>`;
    return;
  }

  let html = '<div class="table-wrap">';
  html += '<table class="tbl-card im-report-table">';
  html += '<thead><tr>';
  html += `
    <th>${T.instructor}</th>
    <th>${T.department}</th>
    <th>${T.sections}</th>
    <th>${T.courses}</th>
    <th>${T.creditHours}</th>
    <th>${T.contactHours}</th>
    <th>${T.teachingDays}</th>
    <th>${T.clashes}</th>
    <th>${T.loadStatus}</th>
  `;
  html += '</tr></thead><tbody>';

  data.rows.forEach(row => {
    const loadStatusClass = {
      under: 'pill-status-info',
      at: 'pill-status-success',
      over: 'pill-status-warning',
      na: 'pill-status-muted'
    }[row.load_status] || 'pill-status-muted';

    html += `<tr>
      <td>
        <div class="im-instructor-name">
          <strong>${escapeHtml(row.full_name)}</strong>
          ${row.full_name_ar ? `<div class="text-muted fs-sm">${escapeHtml(row.full_name_ar)}</div>` : ''}
        </div>
      </td>
      <td><span class="pill-neutral">${escapeHtml(row.department || '—')}</span></td>
      <td class="text-center">${row.section_count}</td>
      <td class="text-center">${row.distinct_courses}</td>
      <td class="text-center">${row.total_credit_hours}</td>
      <td class="text-center">${row.weekly_contact_hours}</td>
      <td class="text-center">${row.teaching_days.length}</td>
      <td class="text-center ${row.clash_count > 0 ? 'text-danger fw-bold' : ''}">${row.clash_count}</td>
      <td><span class="pill-status ${loadStatusClass}">${T[row.load_status] || row.load_status}</span></td>
    </tr>`;
  });

  // Add totals row
  if (data.totals) {
    html += `<tr class="im-report-totals">
      <td><strong>${T.totalRow}</strong></td>
      <td>—</td>
      <td class="text-center"><strong>${data.totals.section_count}</strong></td>
      <td>—</td>
      <td class="text-center"><strong>${data.totals.total_credit_hours}</strong></td>
      <td class="text-center"><strong>${data.totals.weekly_contact_hours}</strong></td>
      <td>—</td>
      <td>—</td>
      <td>—</td>
    </tr>`;
  }

  html += '</tbody></table></div>';

  $('imReportContent').innerHTML = html;
}

// ── Utility Functions ──
function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ── Event Handlers ──
$('imCreateInstructor').onclick = imCreateInstructor;

// ── Initialization ──
document.addEventListener('DOMContentLoaded', function() {
  // Start with roster tab active
  imSwitchTab('roster');
});
