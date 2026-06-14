/* ═══════════════════════════════════════════════════════════════
   Instructor Management — Client-side logic
   ═══════════════════════════════════════════════════════════════ */

// ── STATE ──
let allInstructors = [];
let isEditMode = false;

const IS_AR = document.documentElement.lang === 'ar';

const T = {
  // ── Loading & errors ──
  loading:              IS_AR ? 'جارٍ التحميل…' : 'Loading…',
  failedLoadInstructors: IS_AR ? 'تعذّر تحميل هيئة التدريس' : 'Failed to load instructors',
  failedLoadSections:   IS_AR ? 'تعذّر تحميل الشعب' : 'Failed to load sections',
  failedLoadCourses:    IS_AR ? 'تعذّر تحميل المقررات' : 'Failed to load courses',
  failedLoadReport:     IS_AR ? 'تعذّر تحميل التقرير' : 'Failed to load report',
  networkError:         IS_AR ? 'خطأ في الاتصال' : 'Network error',

  // ── Success messages ──
  instructorSaved:      IS_AR ? 'تم حفظ بيانات عضو هيئة التدريس' : 'Instructor saved successfully',
  assignmentUpdated:    IS_AR ? 'تم تحديث التوزيع' : 'Assignment updated',
  bulkAssignCompleted:  IS_AR ? 'تم تطبيق التوزيع المجمع' : 'Bulk assignment completed',

  // ── Empty states ──
  noInstructors:        IS_AR ? 'لا يوجد أعضاء هيئة تدريس' : 'No instructors found',
  noSections:           IS_AR ? 'لا توجد شعب في هذا السيناريو' : 'No sections in this scenario',
  noCourses:            IS_AR ? 'لا توجد مقررات لهذا البرنامج' : 'No courses for this program',
  noReportData:         IS_AR ? 'لا توجد بيانات للتقرير' : 'No report data available',
  selectProgram:        IS_AR ? 'اختر برنامج وشعبة' : 'Select a program and section',
  noMatch:              IS_AR ? 'لا توجد مقررات مطابقة للبحث' : 'No courses match your search',
  startTyping:          IS_AR ? 'ابدأ بكتابة اسم المدرس…' : 'Start typing instructor name…',

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
  selectCourses:        IS_AR ? 'يرجى اختيار المقررات' : 'Please select courses',

  // ── Confirmation ──
  confirmDeactivate:    IS_AR ? 'هل تريد تعطيل هذا العضو؟' : 'Deactivate this instructor?',
  confirmUnassign:      IS_AR ? 'هل تريد إلغاء هذا التوزيع؟' : 'Remove this assignment?',

  // ── Scenario notices ──
  scenarioPublished:    IS_AR ? 'هذا السيناريو منشور - لا يمكن تعديل التوزيعات' : 'This scenario is published - assignments cannot be modified',
  selectScenario:       IS_AR ? 'يرجى اختيار سيناريو' : 'Please select a scenario',

  // ── Course assignment labels ──
  addAssign:            IS_AR ? '+ توزيع' : '+ Assign',

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
  term:                 (n) => IS_AR ? `ف${n}` : `T${n}`,

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
let currentTab = 'assignments';

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

  // Load data for specific tabs
  if (tab === 'assignments') {
    if (typeof ca !== 'undefined' && ca.init) ca.init();
  } else if (tab === 'roster') {
    imLoadInstructors();
  } else if (tab === 'report') {
    imLoadReport();
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
    notify.error(T.failedLoadInstructors + ': ' + error.message);
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
let imAdvisorMap = {};

function imCreateInstructor() {
  isEditMode = false;
  $('imInstructorModalLabel').textContent = IS_AR ? 'إضافة عضو هيئة تدريس' : 'Add Instructor';
  imClearInstructorForm();
  // The "seed from advisor" picker is only meaningful when creating.
  const seed = $('imAdvisorSeedGroup');
  if (seed) seed.style.display = '';
  imLoadAdvisors();
  imShowInstructorModal();
}

async function imLoadAdvisors() {
  const dl = $('imAdvisorList');
  if (!dl) return;
  try {
    const data = await imApiCall('/ops/instructors/advisors/');
    if (!data || !data.ok) return;
    imAdvisorMap = {};
    dl.innerHTML = '';
    (data.advisors || []).forEach(a => {
      const label = a.email ? `${a.full_name} · ${a.email}` : a.full_name;
      imAdvisorMap[label] = a;
      const opt = document.createElement('option');
      opt.value = label;
      if (a.already_instructor) opt.label = IS_AR ? 'مُضاف مسبقاً' : 'already an instructor';
      else if (a.department) opt.label = a.department;
      dl.appendChild(opt);
    });
  } catch (e) { /* advisor seeding is optional — fail silent */ }
}

function imOnAdvisorPicked() {
  const a = imAdvisorMap[$('imAdvisorPicker').value];
  if (!a) return;  // free typing of a new name — leave the form for manual entry
  $('imInstructorName').value = a.full_name || '';
  if (a.email) $('imInstructorEmail').value = a.email;
  if (a.department) $('imInstructorDepartment').value = a.department;
  $('imInstructorName').focus();
}

function imEditInstructor(instructorId) {
  const instructor = allInstructors.find(i => i.id === instructorId);
  if (!instructor) return;

  isEditMode = true;
  $('imInstructorModalLabel').textContent = IS_AR ? 'تعديل عضو هيئة تدريس' : 'Edit Instructor';
  const seed = $('imAdvisorSeedGroup');
  if (seed) seed.style.display = 'none';

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
  if ($('imAdvisorPicker')) $('imAdvisorPicker').value = '';
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

async function imSaveInstructor() {
  const name = $('imInstructorName').value.trim();
  if (!name) {
    notify.error(T.nameRequired);
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
      notify.success(T.instructorSaved);
      imHideInstructorModal();
      imLoadInstructors();
    } else {
      throw new Error(data.error?.message || 'Failed to save instructor');
    }
  } catch (error) {
    notify.error(error.message);
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
      notify.success(`${action} ${T.instructor}`);
      imLoadInstructors();
    } else {
      throw new Error(data.error?.message || 'Failed to update instructor');
    }
  } catch (error) {
    notify.error(error.message);
  }
}

// ── Load Report (scenario-independent: per-instructor over course assignments) ──
async function imLoadReport() {
  $('imReportContent').innerHTML = `<div class="im-assignment-notice"><span>${T.loading || 'Loading…'}</span></div>`;
  try {
    const data = await imApiCall('/ops/instructors/load-report/');
    if (data.ok) {
      imRenderReport(data);
    } else {
      throw new Error(data.error?.message || 'Failed to load report');
    }
  } catch (error) {
    notify.error(T.failedLoadReport + ': ' + error.message);
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
    <th>${T.programs || 'Programs'}</th>
    <th>${T.courses}</th>
    <th>${T.distinctCourses || 'Distinct'}</th>
    <th>${T.creditHours}</th>
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
      <td>${(row.programs || []).map(p => `<span class="cr-id">${escapeHtml(p)}</span>`).join(' ') || '—'}</td>
      <td class="text-center">${row.course_count}</td>
      <td class="text-center">${row.distinct_courses}</td>
      <td class="text-center">${row.total_credit_hours}</td>
      <td><span class="pill-status ${loadStatusClass}">${T[row.load_status] || row.load_status}</span></td>
    </tr>`;
  });

  // Add totals row
  if (data.totals) {
    html += `<tr class="im-report-totals">
      <td><strong>${T.totalRow}</strong></td>
      <td>—</td>
      <td>—</td>
      <td class="text-center"><strong>${data.totals.course_count}</strong></td>
      <td>—</td>
      <td class="text-center"><strong>${data.totals.total_credit_hours}</strong></td>
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
if ($('imAdvisorPicker')) $('imAdvisorPicker').addEventListener('input', imOnAdvisorPicked);

// ============================================================
// COURSE ASSIGNMENT MODULE (ca namespace)
// ============================================================

const ca = {
  // ── State ──
  courses: [],
  selectedCourses: new Set(),
  currentProgram: '',
  currentSection: 'M',
  popoverVisible: false,
  activePopoverCourse: null,
  popoverTrigger: null,   // element to restore focus to on close
  activeOptIndex: -1,     // keyboard-highlighted result index
  searchSeq: 0,           // request token: drop stale typeahead responses

  // ── Main loader ──
  async loadCourses() {
    const program = $('caProgram').value.trim();
    const section = this.currentSection;

    if (!program) {
      this.renderEmptyTable();
      return;
    }

    this.currentProgram = program;
    const tbody = $('caTable').querySelector('tbody');
    tbody.innerHTML = '<tr><td colspan="7" class="empty-note">' + T.loading + '</td></tr>';

    try {
      const params = new URLSearchParams({ program, section });
      const data = await imApiCall(`/ops/instructors/course-assignments/?${params}`);

      if (data.ok) {
        this.courses = data.courses || [];
        this.renderTable();
        this.updateStatusPills();
      } else {
        throw new Error(data.error?.message || T.failedLoadCourses);
      }
    } catch (error) {
      notify.error(T.failedLoadCourses + ': ' + error.message);
      this.renderEmptyTable();
    }
  },

  // ── Section toggle ──
  setSection(section) {
    // Update buttons
    $$('.ca-seg-btn').forEach(btn => {
      const isActive = btn.dataset.sec === section;
      btn.classList.toggle('ca-seg-active', isActive);
      btn.setAttribute('aria-checked', isActive);
    });

    this.currentSection = section;
    this.selectedCourses.clear();
    this.updateBulkBar();

    if (this.currentProgram) {
      this.loadCourses();
    }
  },

  // ── Table rendering ──
  renderTable() {
    const tbody = $('caTable').querySelector('tbody');

    if (this.courses.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-note">' + T.noCourses + '</td></tr>';
      return;
    }

    tbody.innerHTML = this.courses.map((course, idx) => {
      const checked = this.selectedCourses.has(course.course_code) ? 'checked' : '';
      const assignmentCell = course.instructor
        ? `<div class="ca-chip" onclick="ca.showAssignPopover('${course.course_code}', event)" title="${T.assign}">
             ${escapeHtml(course.instructor.full_name)}
             <button class="ca-chip-x" onclick="ca.clearAssignment('${course.course_code}', event)"
                     aria-label="${T.unassign} ${escapeHtml(course.instructor.full_name)} — ${course.course_code}">×</button>
           </div>`
        : `<button class="ca-assign-add" onclick="ca.showAssignPopover('${course.course_code}', event)"
                   aria-haspopup="dialog" aria-label="${T.assign} — ${course.course_code}">${T.addAssign}</button>`;

      return `<tr data-course="${course.course_code}">
        <td><input type="checkbox" ${checked} onchange="ca.toggleCourse('${course.course_code}', this)"></td>
        <td>${idx + 1}</td>
        <td><span class="cr-id">${course.course_code}</span></td>
        <td>${escapeHtml(course.course_name)}</td>
        <td class="text-center">${T.term(course.programme_term)}</td>
        <td class="text-center">${course.credit_hours}</td>
        <td class="ca-assign-cell">${assignmentCell}</td>
      </tr>`;
    }).join('');
  },

  renderEmptyTable() {
    const tbody = $('caTable').querySelector('tbody');
    tbody.innerHTML = '<tr><td colspan="7" class="empty-note">' + T.selectProgram + '</td></tr>';
    this.updateStatusPills(0, 0);
  },

  // ── Course filtering ──
  filterCourses() {
    const query = $('caSearch').value.toLowerCase().trim();
    let visible = 0;
    $$('#caTable tbody tr[data-course]').forEach(row => {
      const courseCode = row.dataset.course;
      const course = this.courses.find(c => c.course_code === courseCode);
      if (!course) return;

      const matches = !query ||
        course.course_code.toLowerCase().includes(query) ||
        course.course_name.toLowerCase().includes(query);

      row.style.display = matches ? '' : 'none';
      if (matches) visible++;
    });

    // Distinguish "search found nothing" from "no courses loaded"
    const existing = $('caNoMatch');
    if (visible === 0 && this.courses.length > 0) {
      if (!existing) {
        $('caTable').querySelector('tbody').insertAdjacentHTML(
          'beforeend',
          `<tr id="caNoMatch"><td colspan="7" class="empty-note">${T.noMatch}</td></tr>`
        );
      }
    } else if (existing) {
      existing.remove();
    }
  },

  // ── Selection management ──
  toggleCourse(courseCode, checkbox) {
    if (checkbox.checked) {
      this.selectedCourses.add(courseCode);
    } else {
      this.selectedCourses.delete(courseCode);
    }
    this.updateBulkBar();
  },

  toggleAllCourses(checkbox) {
    const visibleCheckboxes = $$('#caTable tbody tr[data-course]:not([style*="display: none"]) input[type="checkbox"]');
    visibleCheckboxes.forEach(cb => {
      cb.checked = checkbox.checked;
      const courseCode = cb.closest('tr').dataset.course;
      if (checkbox.checked) {
        this.selectedCourses.add(courseCode);
      } else {
        this.selectedCourses.delete(courseCode);
      }
    });
    this.updateBulkBar();
  },

  // ── Bulk assignment bar ──
  updateBulkBar() {
    const count = this.selectedCourses.size;
    const bulkbar = $('caBulkbar');
    const countSpan = $('caBulkCount');

    if (count > 0) {
      bulkbar.classList.remove('d-none');
      if (countSpan) countSpan.textContent = count;
    } else {
      bulkbar.classList.add('d-none');
    }
  },

  // ── Status pills ──
  updateStatusPills(assignedCount = null, unassignedCount = null) {
    if (assignedCount === null) {
      assignedCount = this.courses.filter(c => c.instructor).length;
      unassignedCount = this.courses.filter(c => !c.instructor).length;
    }

    const assignedPill = $('caAssigned');
    const unassignedPill = $('caUnassigned');

    if (assignedPill) {
      assignedPill.querySelector('.ca-pill-count').textContent = assignedCount;
    }
    if (unassignedPill) {
      unassignedPill.querySelector('.ca-pill-count').textContent = unassignedCount;
    }
  },

  // ── Assignment popover ──
  showAssignPopover(courseCode, event) {
    event.stopPropagation();
    this.hideAssignPopover();

    this.activePopoverCourse = courseCode;
    this.popoverTrigger = event.currentTarget || event.target;
    this.activeOptIndex = -1;
    const popover = $('caPop');
    const searchInput = $('caPopSearch');
    const trigger = this.popoverTrigger;

    // `position: fixed` only resolves against the viewport when no ancestor is
    // transformed. The page wrapper (.content-wrap) carries a transform, which
    // would otherwise re-anchor the popover to the wrapper and throw it
    // off-screen. Hoisting to <body> once makes fixed positioning correct.
    if (popover.parentElement !== document.body) document.body.appendChild(popover);

    const gap = 6;
    const margin = 8;
    const isRTL = document.documentElement.dir === 'rtl';
    const rect = trigger.getBoundingClientRect();

    // Reset results to a deterministic minimal state BEFORE measuring, so a
    // stale result list from a previous open can't inflate the measured height
    // (which would otherwise throw the flip-up math off-screen).
    $('caPopResults').innerHTML = '<div class="ca-pop-hint">' + T.startTyping + '</div>';

    // Render off-screen so we can measure the popover before placing it.
    popover.style.position = 'fixed';
    popover.style.visibility = 'hidden';
    popover.style.display = 'block';
    // Cap measured dims to the viewport so the clamp below can never produce
    // a negative coordinate (the failure mode when a box approaches the
    // viewport size).
    const pw = Math.min(popover.offsetWidth || 280, window.innerWidth - 2 * margin);
    const ph = Math.min(popover.offsetHeight || 240, window.innerHeight - 2 * margin);

    // Horizontal: anchor to the trigger's leading edge (right edge in RTL).
    let left = isRTL ? rect.right - pw : rect.left;

    // Vertical: open below if there's room, otherwise flip above.
    let top = rect.bottom + gap;
    const fitsBelow = top + ph <= window.innerHeight - margin;
    const fitsAbove = rect.top - ph - gap >= margin;
    if (!fitsBelow && fitsAbove) top = rect.top - ph - gap;

    // Robust clamp (inner Math.min first): always lands fully on-screen, even
    // when ph/pw are near the viewport size.
    left = Math.max(margin, Math.min(left, window.innerWidth - pw - margin));
    top = Math.max(margin, Math.min(top, window.innerHeight - ph - margin));

    popover.style.left = left + 'px';
    popover.style.top = top + 'px';
    popover.style.visibility = '';
    popover.setAttribute('aria-hidden', 'false');
    if (trigger.setAttribute) trigger.setAttribute('aria-expanded', 'true');

    this.popoverVisible = true;

    // Focus search
    setTimeout(() => searchInput.focus(), 50);

    // Load initial results
    this.searchInstructors('');
  },

  hideAssignPopover() {
    if (!this.popoverVisible) return;

    const popover = $('caPop');
    popover.style.display = 'none';
    popover.setAttribute('aria-hidden', 'true');
    $('caPopSearch').removeAttribute('aria-activedescendant');

    this.popoverVisible = false;
    this.activePopoverCourse = null;
    this.activeOptIndex = -1;

    // Clear search
    $('caPopSearch').value = '';

    // Return focus to the trigger if it still exists (it won't after a
    // re-render following a successful assign — guarded to avoid throwing).
    const trigger = this.popoverTrigger;
    if (trigger) {
      if (trigger.setAttribute) trigger.setAttribute('aria-expanded', 'false');
      if (document.contains(trigger) && trigger.focus) trigger.focus();
    }
    this.popoverTrigger = null;
  },

  // ── Keyboard navigation within the results list ──
  moveActiveOpt(delta) {
    const opts = $$('#caPopResults .ca-pop-opt');
    if (!opts.length) return;
    let i = this.activeOptIndex + delta;
    if (i < 0) i = opts.length - 1;
    if (i >= opts.length) i = 0;
    this.activeOptIndex = i;
    opts.forEach((o, idx) => o.classList.toggle('is-active', idx === i));
    opts[i].scrollIntoView({ block: 'nearest' });
    $('caPopSearch').setAttribute('aria-activedescendant', opts[i].id);
  },

  // ── Instructor search ──
  async searchInstructors(query) {
    const resultsDiv = $('caPopResults');
    this.activeOptIndex = -1;
    $('caPopSearch').removeAttribute('aria-activedescendant');

    if (!query.trim()) {
      resultsDiv.innerHTML = '<div class="ca-pop-hint">' + T.startTyping + '</div>';
      return;
    }

    const seq = ++this.searchSeq;   // request token
    resultsDiv.innerHTML = '<div class="ca-pop-loading">' + T.loading + '</div>';

    try {
      const params = new URLSearchParams({ q: query.trim() });
      const data = await imApiCall(`/ops/instructors/list/?${params}`);

      if (seq !== this.searchSeq) return;   // a newer search superseded this one
      if (data.ok) {
        this.renderInstructorResults(data.instructors || []);
      }
    } catch (error) {
      if (seq !== this.searchSeq) return;
      resultsDiv.innerHTML = '<div class="ca-pop-error">' + T.networkError + '</div>';
    }
  },

  renderInstructorResults(instructors) {
    const resultsDiv = $('caPopResults');
    this.activeOptIndex = -1;

    if (instructors.length === 0) {
      resultsDiv.innerHTML = '<div class="ca-pop-hint">' + T.noInstructors + '</div>';
      return;
    }

    resultsDiv.innerHTML = instructors.map((inst, idx) =>
      `<div class="ca-pop-opt" id="caPopOpt${idx}" role="option" aria-selected="false"
            onclick="ca.assignInstructor(${inst.id})" data-id="${inst.id}">
         <strong>${escapeHtml(inst.full_name)}</strong>
         ${inst.department ? `<div class="ca-pop-dept">${escapeHtml(inst.department)}</div>` : ''}
       </div>`
    ).join('');
  },

  // ── Assignment actions ──
  async assignInstructor(instructorId) {
    if (!this.activePopoverCourse) return;

    try {
      const data = await imApiCall('/ops/instructors/course-assignments/set/', {
        method: 'POST',
        body: JSON.stringify({
          program: this.currentProgram,
          course_code: this.activePopoverCourse,
          section: this.currentSection,
          instructor_ids: [instructorId]
        })
      });

      if (data.ok) {
        notify.success(T.assignmentUpdated);
        this.hideAssignPopover();
        this.loadCourses(); // Refresh table
      } else {
        throw new Error(data.error?.message || 'Failed to assign');
      }
    } catch (error) {
      notify.error(error.message);
    }
  },

  async clearAssignment(courseCode, event) {
    event.stopPropagation();

    try {
      const data = await imApiCall('/ops/instructors/course-assignments/clear/', {
        method: 'POST',
        body: JSON.stringify({
          program: this.currentProgram,
          course_code: courseCode,
          section: this.currentSection
        })
      });

      if (data.ok) {
        notify.success(T.assignmentUpdated);
        this.loadCourses(); // Refresh table
      } else {
        throw new Error(data.error?.message || 'Failed to clear assignment');
      }
    } catch (error) {
      notify.error(error.message);
    }
  },

  // ── Bulk assignment ──
  async executeBulkAssign() {
    if (this.selectedCourses.size === 0) {
      notify.error(T.selectCourses);
      return;
    }

    const typeahead = $('caBulkTypeahead');
    const query = typeahead.value.trim();

    if (!query) {
      notify.error(T.selectInstructor);
      return;
    }

    // Simple matching by name - could be enhanced with proper typeahead
    try {
      const searchData = await imApiCall(`/ops/instructors/list/?q=${encodeURIComponent(query)}`);
      if (!searchData.ok || !searchData.instructors.length) {
        notify.error(T.selectInstructor);
        return;
      }

      const instructor = searchData.instructors[0]; // Take first match
      const courseCodes = Array.from(this.selectedCourses);

      const data = await imApiCall('/ops/instructors/course-assignments/assign-bulk/', {
        method: 'POST',
        body: JSON.stringify({
          program: this.currentProgram,
          section: this.currentSection,
          course_codes: courseCodes,
          instructor_id: instructor.id
        })
      });

      if (data.ok) {
        notify.success(T.bulkAssignCompleted + ': ' + T.nAssigned(data.updated || courseCodes.length));
        this.selectedCourses.clear();
        this.updateBulkBar();
        typeahead.value = '';
        this.loadCourses();
      } else {
        throw new Error(data.error?.message || 'Failed to bulk assign');
      }
    } catch (error) {
      notify.error(error.message);
    }
  }
};

// ── Event handlers for search/keyboard ──
let caSearchTimeout;
if ($('caPopSearch')) {
  $('caPopSearch').addEventListener('input', function() {
    clearTimeout(caSearchTimeout);
    caSearchTimeout = setTimeout(() => {
      ca.searchInstructors(this.value);
    }, 300);
  });
}

// Hide popover on outside click
document.addEventListener('click', function(event) {
  if (ca.popoverVisible && !$('caPop').contains(event.target)) {
    ca.hideAssignPopover();
  }
});

// The popover is position:fixed, so it detaches from its row on scroll —
// close it instead of letting it float disconnected. Capture phase catches
// scrolls inside any container.
window.addEventListener('scroll', function() {
  if (ca.popoverVisible) ca.hideAssignPopover();
}, true);

// Keyboard navigation for popover: Arrow keys highlight, Enter selects the
// highlighted result (or the first if none), Escape closes. ArrowDown no
// longer assigns immediately — that was an accidental irreversible action.
if ($('caPopSearch')) {
  $('caPopSearch').addEventListener('keydown', function(event) {
    const opts = $$('#caPopResults .ca-pop-opt');
    if (event.key === 'Escape') {
      event.preventDefault();
      ca.hideAssignPopover();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      ca.moveActiveOpt(1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      ca.moveActiveOpt(-1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      const target = opts[ca.activeOptIndex] || opts[0];
      if (target) target.click();
    }
  });
}

// ── Initialization ──
document.addEventListener('DOMContentLoaded', function() {
  // Start with assignments tab active
  imSwitchTab('assignments');
});
