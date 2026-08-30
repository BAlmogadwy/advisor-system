/* ═══════════════════════════════════════════════════════════════
   STATE
   ═══════════════════════════════════════════════════════════════ */
let allStudents = [];
let summaryCache = {};
let filteredStudents = [];
let currentAdvisorId = '';
let currentFocus = 'all';
let currentPage = 1;
const PAGE_SIZE = 50;
//: How many rows to ASK the server for. Distinct from PAGE_SIZE, which is how
//: many to SHOW at a time — they were accidentally equal, which is what made the
//: truncation invisible. 500 is the ceiling in advisor_views.py.
const SERVER_PAGE_SIZE = 500;
//: The server's own total, which is not the same as the number of rows received.
let rosterTotal = 0;
let batchSelected = new Set();
let selectedSid = null;
let advisorLoadGeneration = 0;
let graduationRequestController = null;
let graduationRequestGeneration = 0;
let graduationBaseline = 'registered_timetable';
let graduationGraphMode = 'term';
let graduationRenderedGraph = null;
let graduationBandLabels = {};

const IS_AR = document.documentElement.lang === 'ar';

const T = {
  // ── Clipboard ──
  copied:             IS_AR ? 'تم النسخ'                            : 'Copied to clipboard',
  copyFailed:         IS_AR ? 'فشل النسخ — استخدم Ctrl+C'           : 'Copy failed — use Ctrl+C',

  // ── Advisors dropdown ──
  selectAdvisor:      IS_AR ? 'اختر مرشدًا…'                        : 'Select advisor…',
  failedLoadAdvisors: IS_AR ? 'تعذّر تحميل قائمة المرشدين'          : 'Failed to load advisors list',

  // ── Load students ──
  loadingStudents:    IS_AR ? 'جارٍ تحميل الطلاب…'                  : 'Loading students…',
  failedLoadStudents: IS_AR ? 'تعذّر تحميل الطلاب'                  : 'Failed to load students',
  failedLoadAdvisors: IS_AR ? 'تعذّر تحميل قائمة المرشدين'          : 'Failed to load the advisor list',
  showingFirst:    (n, total) => IS_AR
    ? `يُعرض أول ${n} من ${total} طالبًا في هذا الجدول. تصدير CSV يشمل القائمة كاملة.`
    : `This table holds the first ${n} of ${total} students. The CSV export covers the full list.`,
  mappingNotReady:    IS_AR ? 'ربط الطلاب بالمرشدين غير جاهز بعد.'  : 'Student-advisor mapping is not ready yet.',
  mappingNotReadyShort: IS_AR ? 'الربط غير جاهز'                    : 'Mapping not ready',
  nStudents:       (n) => IS_AR ? `${n} طالب`                       : `${n} students`,
  loadedStudents:  (n) => IS_AR ? `تم تحميل ${n} طالب`              : `Loaded ${n} students`,
  networkFailure:     IS_AR ? 'خطأ في الاتصال'                       : 'Network failure',

  // ── Clear state ──
  noAdvisor:          IS_AR ? 'لا يوجد مرشد'                        : 'No advisor',
  zeroStudents:       IS_AR ? '0 طالب'                               : '0 students',
  noAdvisorSelected:  IS_AR ? 'لم يتم اختيار مرشد'                  : 'No advisor selected',
  chooseAdvisorHint:  IS_AR ? 'اختر مرشدًا أعلاه لتحميل قائمة طلابه' : 'Choose an advisor above to load their roster',

  // ── Insights ──
  veryHighRisk:    (n) => IS_AR ? `${n} خطورة عالية جدًا`           : `${n} very high risk`,
  lowGpaInsight:   (n) => IS_AR ? `${n} معدل أقل من 2.0`            : `${n} GPA < 2.0`,
  hpMissing2Plus:  (n) => IS_AR ? `${n} بنقص ≥2 مقررات ذات أولوية`  : `${n} with 2+ HP missing`,
  zeroTermHours:   (n) => IS_AR ? `${n} بدون ساعات هذا الفصل`       : `${n} zero current-term hours`,
  allPrograms:        IS_AR ? 'كل البرامج'                          : 'All programs',

  // ── Table status ──
  noStudentsMatch:    IS_AR ? 'لم يُعثر على طلاب مطابقين'           : 'No students match filters',
  showingRange:   (s,e,t) => IS_AR ? `عرض <strong>${s}–${e}</strong> من <strong>${t}</strong> طالب` : `Showing <strong>${s}–${e}</strong> of <strong>${t}</strong> students`,
  attention:          IS_AR ? 'يحتاج متابعة'                         : 'Attention',
  ok:                 IS_AR ? 'جيد'                                   : 'OK',
  expandDetails:      IS_AR ? 'عرض التفاصيل'                        : 'Expand details',

  // ── Reason map ──
  lowGpa:             IS_AR ? 'معدل منخفض'                          : 'Low GPA',
  hpMissing:          IS_AR ? 'نقص أولوية عالية'                     : 'HP missing',
  zeroHours:          IS_AR ? 'بدون ساعات'                           : 'Zero hours',

  // ── Detail row labels ──
  section:            IS_AR ? 'الشعبة'                               : 'Section',
  status:             IS_AR ? 'الحالة'                               : 'Status',
  termHours:          IS_AR ? 'ساعات الفصل'                          : 'Term Hours',
  earnedReg:          IS_AR ? 'مكتسبة / مسجلة'                      : 'Earned / Reg',
  regNo:              IS_AR ? 'رقم القيد'                            : 'Reg No',
  reasons:            IS_AR ? 'الأسباب'                              : 'Reasons',

  // ── Drawer ──
  close:              IS_AR ? 'إغلاق'                                : 'Close',
  studentDetails:     IS_AR ? 'تفاصيل الطالب'                       : 'Student details',
  none:               IS_AR ? 'لا يوجد'                              : 'None',
  noHpMissing:        IS_AR ? 'لا توجد مقررات ذات أولوية عالية ناقصة.' : 'No high-priority missing courses.',
  needsAttention:     IS_AR ? 'يحتاج متابعة — '                      : 'Needs attention — ',
  academicInfo:       IS_AR ? 'المعلومات الأكاديمية'                 : 'Academic Info',
  gpa:                IS_AR ? 'المعدل التراكمي'                      : 'GPA',
  riskScore:          IS_AR ? 'درجة الخطورة'                        : 'Risk Score',
  registrationNo:     IS_AR ? 'رقم القيد'                            : 'Registration No',
  credits:            IS_AR ? 'الساعات'                              : 'Credits',
  earned:             IS_AR ? 'المكتسبة'                             : 'Earned',
  registered:         IS_AR ? 'المسجلة'                              : 'Registered',
  highPriorityMissing:IS_AR ? 'مقررات ذات أولوية عالية ناقصة'        : 'High Priority Missing',
  openPlanner:        IS_AR ? 'فتح مخطط الجدول'                      : 'Open Timetable Builder',
  copyId:             IS_AR ? 'نسخ المعرّف'                          : 'Copy ID',
  copyHpCourses:      IS_AR ? 'نسخ المقررات ذات الأولوية'            : 'Copy HP courses',

  // ── Graduation plan panel ──
  graduationPlan:     IS_AR ? 'خطة إكمال التخرج'                    : 'Graduation Plan',
  graduationPanelTitle: IS_AR ? 'مسار إكمال الخطة الدراسية'          : 'Degree-plan completion path',
  graduationTabsLabel: IS_AR ? 'مصدر مقررات فصل البداية'             : 'Starting-term course source',
  registeredBaseline: IS_AR ? 'المسجّل'                              : 'Registered',
  recommendedBaseline: IS_AR ? 'الموصى به'                           : 'Recommended',
  registeredExplanation: IS_AR
    ? 'يبدأ التقدير من جدول الطالب المسجّل فعليًا.'
    : "Starts from the student's actual registered timetable.",
  recommendedExplanation: IS_AR
    ? 'يبدأ التقدير من مقررات فصل البداية التي يوصي بها النظام.'
    : 'Starts from the courses the system recommends for the starting term.',
  graduationAdvisory: IS_AR
    ? 'تقدير إرشادي للانتهاء من متطلبات الخطة، وليس قرار تخرج رسميًا. لا يغيّر أي سجل.'
    : 'Advisory estimate for completing the degree plan, not an official graduation decision. Nothing is saved.',
  loadingGraduation:  IS_AR ? 'جارٍ حساب مسار إكمال الخطة…'          : 'Calculating the degree-plan path…',
  failedGraduation:   IS_AR ? 'تعذّر تحميل خطة إكمال التخرج.'        : 'Could not load the graduation plan.',
  retry:              IS_AR ? 'إعادة المحاولة'                       : 'Retry',
  noGraduationData:   IS_AR ? 'لا تتوفر بيانات خطة دراسية لهذا الطالب.' : 'No degree-plan data is available for this student.',
  progressSummary:    IS_AR ? 'ملخص التقدم'                          : 'Progress summary',
  program:            IS_AR ? 'البرنامج'                             : 'Program',
  planProgress:       IS_AR ? 'مقررات الخطة المجتازة'                : 'Plan courses completed',
  coursesRemaining:   IS_AR ? 'المقررات المتبقية'                    : 'Courses remaining',
  creditsRemaining:   IS_AR ? 'الساعات المتبقية'                    : 'Credits remaining',
  projectedTerms:     IS_AR ? 'الفصول التقديرية'                    : 'Projected terms',
  lowerBound:         IS_AR ? 'الحد الأدنى'                          : 'Lower bound',
  exactEstimate:      IS_AR ? 'تقدير مكتمل'                          : 'Complete estimate',
  incompleteEstimate: IS_AR ? 'تعذّر بناء تقدير كامل'               : 'Full estimate unavailable',
  planningBaseline:   IS_AR ? 'فصل البداية ومصدره'                   : 'Starting term and provenance',
  registeredProvenance: IS_AR ? 'الجدول المسجّل فعليًا'              : 'Actual registered timetable',
  recommendedProvenance: IS_AR ? 'توصيات النظام لفصل البداية'        : 'System recommendations for the starting term',
  planningTerm:       IS_AR ? 'فصل البداية'                          : 'Starting term',
  baselineCredits:    IS_AR ? 'ساعات فصل البداية'                    : 'Starting-term credits',
  baselineCourses:    IS_AR ? 'مقررات فصل البداية المفترض اجتيازها' : 'Starting-term courses assumed passed',
  noBaselineCourses:  IS_AR ? 'لم تُرجع الخدمة مقررات مفصّلة لفصل البداية.' : 'No detailed starting-term courses were returned.',
  fullTermPlan:       IS_AR ? 'الخطة الكاملة حسب الفصل'              : 'Full term-by-term plan',
  sequence:           IS_AR ? 'الترتيب'                              : 'Step',
  academicTerm:       IS_AR ? 'الفصل الأكاديمي'                      : 'Academic term',
  termState:          IS_AR ? 'حالة الفصل'                           : 'Term state',
  plannedCourses:     IS_AR ? 'المقررات'                             : 'Courses',
  termCredits:        IS_AR ? 'الساعات'                              : 'Credits',
  planned:            IS_AR ? 'مقررات مخططة'                         : 'Courses planned',
  waiting:            IS_AR ? 'فصل انتظار'                           : 'Waiting term',
  waitingForPrereqs:  IS_AR ? 'لا مقررات — انتظار إتاحة المتطلبات السابقة' : 'No courses — waiting for prerequisites to unlock',
  noFutureTerms:      IS_AR ? 'لا توجد فصول مستقبلية مطلوبة في هذا السيناريو.' : 'No future terms are needed in this scenario.',
  unresolvedBlockers: IS_AR ? 'العوائق غير المحلولة'                 : 'Unresolved blockers',
  noBlockers:         IS_AR ? 'لا توجد عوائق غير محلولة.'            : 'No unresolved blockers.',
  blockerDetailsMissing: IS_AR ? 'لم تُرجع الخدمة تفاصيل للعوائق المتبقية.' : 'No details were returned for the remaining blockers.',
  missingPrereqs:     IS_AR ? 'متطلبات سابقة ناقصة'                  : 'Missing prerequisites',
  creditGate:         IS_AR ? 'شرط الساعات'                          : 'Credit-hour gate',
  required:           IS_AR ? 'المطلوب'                              : 'required',
  effective:          IS_AR ? 'المحتسب'                              : 'effective',
  remaining:          IS_AR ? 'المتبقي'                              : 'remaining',
  prerequisiteTree:   IS_AR ? 'شجرة المتطلبات السابقة'              : 'Prerequisite tree',
  byProjectedTerm:    IS_AR ? 'حسب الفصل التقديري'                  : 'By projected term',
  byPrerequisiteChain: IS_AR ? 'حسب سلسلة المتطلبات'                 : 'By prerequisite chain',
  noPrerequisiteTree: IS_AR ? 'لا تتوفر خريطة متطلبات لهذا السيناريو.' : 'No prerequisite map is available for this scenario.',
  graphLabel:         IS_AR ? 'خريطة تفاعلية لمسار إكمال الخطة'     : 'Interactive map of the degree-plan completion path',
  readOnly:           IS_AR ? 'للقراءة فقط'                          : 'Read only',

  // ── HP table headers ──
  course:             IS_AR ? 'المقرر'                               : 'Course',
  score:              IS_AR ? 'الدرجة'                               : 'Score',
  planTermPattern:    IS_AR ? 'نمط فصول الخطة'                    : 'Plan-term pattern',
  oddPlanTerms:       IS_AR ? 'فردي (1، 3، 5، 7)'                  : 'Odd (1, 3, 5, 7)',
  evenPlanTerms:      IS_AR ? 'زوجي (2، 4، 6، 8)'                  : 'Even (2, 4, 6, 8)',
  currentTermPattern: IS_AR ? 'مطابق لنمط الفصل الحالي'             : 'Matches current term',

  // ── GPA chart ──
  nStudentsTitle:  (n) => IS_AR ? `${n} طالب`                       : `${n} students`,

  // ── Batch & export ──
  noStudentsSelected: IS_AR ? 'لم يتم اختيار طلاب'                  : 'No students selected',
  exportedStudents:(n) => IS_AR ? `تم تصدير ${n} طالب`               : `Exported ${n} students`,
  noStudentsCopy:     IS_AR ? 'لا يوجد طلاب للنسخ'                   : 'No students to copy',
  noHighRisk:         IS_AR ? 'لا يوجد طلاب بخطورة عالية'            : 'No high-risk students',
};

/* q, esc, getCookie, csrfToken, csrfHeaders — provided by shared-utils.js */

function normalizeProgram(value) {
  return String(value || '').trim().toUpperCase();
}

function selectedProgram() {
  return normalizeProgram(q('apProgramFilter')?.value);
}

function resetProgramFilterControl() {
  const select = q('apProgramFilter');
  if (!select) return;
  select.innerHTML = `<option value="">${T.allPrograms}</option>`;
  select.value = '';
}

function syncProgramPills() {
  const selected = selectedProgram();
  document.querySelectorAll('#apPrograms .ap-prog-pill').forEach(btn => {
    const active = normalizeProgram(btn.dataset.program) === selected && selected !== '';
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function renderProgramControls(programBreakdown) {
  const entries = Object.entries(programBreakdown || {})
    .filter(([program]) => normalizeProgram(program))
    .sort((a, b) => Number(b[1]) - Number(a[1]) || String(a[0]).localeCompare(String(b[0])));

  const select = q('apProgramFilter');
  const previous = selectedProgram();
  if (select) {
    select.innerHTML = `<option value="">${T.allPrograms}</option>` + entries
      .map(([program, count]) => `<option value="${esc(normalizeProgram(program))}">${esc(program)} (${Number(count) || 0})</option>`)
      .join('');
    select.value = entries.some(([program]) => normalizeProgram(program) === previous)
      ? previous
      : '';
  }

  q('apPrograms').innerHTML = entries.length
    ? entries.map(([program, count]) => `
        <button type="button" class="ap-prog-pill" data-program="${esc(normalizeProgram(program))}"
                aria-pressed="false" onclick="setProgramFilter(this.dataset.program)">
          ${esc(program)} <span class="ap-prog-n">${Number(count) || 0}</span>
        </button>`).join('')
    : '<span style="font-size:0.78rem;color:var(--muted);">—</span>';
  syncProgramPills();
}

function setProgramFilter(program) {
  const select = q('apProgramFilter');
  if (!select) return;
  const requested = normalizeProgram(program);
  const next = selectedProgram() === requested ? '' : requested;
  const valid = Array.from(select.options).some(option => normalizeProgram(option.value) === next);
  select.value = valid ? next : '';
  currentPage = 1;
  apFilter();
}

async function copyText(text, triggerBtn) {
  try {
    await navigator.clipboard.writeText(text);
    notify.success(T.copied);
    if (triggerBtn) {
      const orig = triggerBtn.textContent;
      triggerBtn.textContent = IS_AR ? 'تم النسخ!' : 'Copied!';
      triggerBtn.disabled = true;
      setTimeout(() => { triggerBtn.textContent = orig; triggerBtn.disabled = false; }, 2000);
    }
  }
  catch { notify.warning(T.copyFailed); }
}

/* notify — provided by notify.js */

/* ═══════════════════════════════════════════════════════════════
   LOAD ADVISORS DROPDOWN
   ═══════════════════════════════════════════════════════════════ */
async function loadAdvisors() {
  try {
    const res = await fetch('/report/advisors/');
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !Array.isArray(data.items)) {
      /* A bare `return` here is what made the dead end silent: no toast, no
         console error, and a tbody still reading "choose an advisor above".
         `loadStudents` below already surfaces its failures, and
         page-dashboard.js surfaces this exact endpoint's. */
      const msg = data?.error || data?.message || `HTTP ${res.status}`;
      const sel = q('apAdvisorSelect');
      if (sel) sel.innerHTML = `<option value="">${T.selectAdvisor}</option>`;
      q('apTable').querySelector('tbody').innerHTML =
        `<tr><td colspan="10" class="text-danger small">${esc(msg)}</td></tr>`;
      notify.error(T.failedLoadAdvisors, msg.slice(0, 120));
      return;
    }
    const sel = q('apAdvisorSelect');
    sel.innerHTML = `<option value="">${T.selectAdvisor}</option>` +
      data.items.map(a => `<option value="${a.advisor_id}">${a.advisor_id} — ${a.full_name} (${a.department})</option>`).join('');

    // Auto-select if advisor_id is in URL
    const params = new URLSearchParams(location.search);
    const urlAdvisor = params.get('advisor_id') || params.get('advisor');
    if (urlAdvisor) { sel.value = urlAdvisor; loadStudents(urlAdvisor); }
  } catch { notify.error(T.failedLoadAdvisors); }
}

/* ═══════════════════════════════════════════════════════════════
   LOAD STUDENTS (auto-load on advisor selection) — Fix #3
   ═══════════════════════════════════════════════════════════════ */
q('apAdvisorSelect').addEventListener('change', () => {
  const id = q('apAdvisorSelect').value.trim();
  if (id) loadStudents(id);
  else clearPortfolio();
});

async function loadStudents(advisorId) {
  const loadGeneration = ++advisorLoadGeneration;
  currentAdvisorId = advisorId;
  currentPage = 1;
  batchSelected.clear();
  updateBatchBar();
  resetProgramFilterControl();
  q('apPrograms').innerHTML = '';

  const tbody = q('apTable').querySelector('tbody');
  tbody.innerHTML = `<tr><td colspan="10"><div class="ap-empty"><span class="ap-empty-icon"><span class="i i-xl" aria-hidden="true"><svg viewBox="0 0 24 24"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg></span></span><div class="ap-empty-title">${T.loadingStudents}</div></div></td></tr>`;

  try {
    /* `page_size` explicitly. Omitting it took the server default of 50, and
       the client's own PAGE_SIZE is also 50 — so its pager saw exactly one page
       and hid itself, and ten of sixty advisees were unreachable with no control
       to reach them.

       500 is the server's ceiling. The slice happens after the whole roster is
       built, so a larger page issues NO ADDITIONAL SQL QUERIES — measured, 8 at
       page_size=50 and 8 at 500. That is the whole of the measurement, and it is
       not the whole of the cost: Python enrichment, risk and recommendation
       computation, JSON serialisation, response bytes, browser memory and DOM
       filtering all still scale with the roster. 500 rows measured ~284 KB.

       THE CONTRACT this establishes, which is not arbitrary pagination:
         <= 500 advisees  fully interactive here — every row loaded, filtered,
                          sorted and paged client-side.
         >  500 advisees  the totals stay truthful (`data.count`), the screen says
                          plainly that it holds a prefix, and the COMPLETE roster
                          is reachable through the CSV export, which passes no
                          page_size and so returns everything.
       Genuine server-side paging would be needed to interact with row 501 in this
       table. No adviser is near that today (largest measured roster: 27). */
    const res = await fetch(
      `/report/students-by-advisor/?advisor_id=${encodeURIComponent(advisorId)}&page=1&page_size=${SERVER_PAGE_SIZE}`
    );
    const data = await res.json();
    if (loadGeneration !== advisorLoadGeneration) return;
    if (!res.ok || !Array.isArray(data?.items)) {
      const msg = data?.error || data?.message || `HTTP ${res.status}`;
      tbody.innerHTML = `<tr><td colspan="10" class="text-danger small"><span class="i i-xs" aria-hidden="true" style="vertical-align:-2px"><svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span> ${esc(msg)}</td></tr>`;
      notify.error(T.failedLoadStudents, msg.slice(0,80));
      return;
    }
    if (data.mapping_ready === false) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty-note">${T.mappingNotReady}</td></tr>`;
      notify.warning(T.mappingNotReadyShort);
      return;
    }

    allStudents = data.items;
    summaryCache = data.summary || {};
    /* The server's count, NOT allStudents.length. The chips used to report the
       size of the slice, so a 60-student adviser read "50 students" beside an
       attention count of 60 — the page contradicting itself. */
    rosterTotal = Number.isFinite(data.count) ? data.count : allStudents.length;

    q('apAdvisorChip').textContent = advisorId;
    q('apCountChip').textContent = T.nStudents(rosterTotal);
    /* And if the roster genuinely exceeds what one request returns, say so
       rather than quietly showing a prefix. */
    const truncated = q('apTruncatedNote');
    if (truncated) {
      truncated.classList.toggle('d-none', rosterTotal <= allStudents.length);
      truncated.textContent = T.showingFirst(allStudents.length, rosterTotal);
    }
    q('apLoadedLabel').classList.remove('d-none');
    q('apLoadedTime').textContent = new Date().toLocaleTimeString();
    q('apMetricsWrap').classList.remove('d-none');

    renderProgramControls(summaryCache.program_breakdown);
    updateCsvLink();
    apFilter();
    notify.success(T.loadedStudents(allStudents.length), advisorId);
  } catch (err) {
    if (loadGeneration !== advisorLoadGeneration) return;
    tbody.innerHTML = `<tr><td colspan="10" class="text-danger small"><span class="i i-xs" aria-hidden="true" style="vertical-align:-2px"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg></span> Network failure — ${esc(String(err))}</td></tr>`;
    notify.error(T.networkFailure);
  }
}

function clearPortfolio() {
  advisorLoadGeneration++;
  currentAdvisorId = '';
  allStudents = [];
  summaryCache = {};
  filteredStudents = [];
  q('apMetricsWrap').classList.add('d-none');
  q('apLoadedLabel').classList.add('d-none');
  q('apAdvisorChip').textContent = T.noAdvisor;
  q('apCountChip').textContent = T.zeroStudents;
  const tbody = q('apTable').querySelector('tbody');
  tbody.innerHTML = `<tr><td colspan="10"><div class="ap-empty"><span class="ap-empty-icon"><span class="i i-xl" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></span></span><div class="ap-empty-title">${T.noAdvisorSelected}</div><div class="ap-empty-hint">${T.chooseAdvisorHint}</div></div></td></tr>`;
  q('apShowing').innerHTML = '—';
  q('apPagination').innerHTML = '';
  q('apInsights').innerHTML = '';
  q('apPrograms').innerHTML = '';
  q('apGpaChart').innerHTML = '';
  resetProgramFilterControl();
}

/* ═══════════════════════════════════════════════════════════════
   METRICS — Fix #6, #7, #11, #12, #14
   ═══════════════════════════════════════════════════════════════ */
function updateMetrics() {
  const program = selectedProgram();
  const scopedStudents = program
    ? allStudents.filter(student => normalizeProgram(student.program) === program)
    : allStudents;
  const serverProgramSummary = program ? summaryCache.program_summaries?.[program] : null;
  const s = program ? (serverProgramSummary || summarizeStudents(scopedStudents)) : summaryCache;
  const scopedTotal = program ? Number(s.student_count ?? scopedStudents.length) : rosterTotal;

  q('mAttention').textContent = s.needs_attention_count || 0;
  q('mHighRisk').textContent = s.very_high_risk_count || 0;
  q('mStudents').textContent = scopedTotal;
  q('mAvgGpa').textContent = s.avg_gpa != null ? String(s.avg_gpa) : '—';
  q('mTermHours').textContent = s.current_term_registered_hours_total || 0;
  q('mHpMissing').textContent = s.high_priority_missing_count || 0;

  // Insights
  const vhr = Number(s.very_high_risk_count || 0);
  const twoPlus = Number(s.two_plus_high_priority_missing_count || 0);
  const zero = Number(s.zero_current_term_hours_count || 0);
  const lowGpa = Number(s.low_gpa_count || 0);
  const chips = [];
  if (vhr > 0) chips.push(`<span class="ap-insight ap-insight-danger"><span class="i i-xxs" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="currentColor"/></svg></span> ${T.veryHighRisk(vhr)}</span>`);
  if (lowGpa > 0) chips.push(`<span class="ap-insight ap-insight-danger"><span class="i i-xxs" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="22 17 13.5 8.5 8.5 13.5 2 7"/><polyline points="16 17 22 17 22 11"/></svg></span> ${T.lowGpaInsight(lowGpa)}</span>`);
  if (twoPlus > 0) chips.push(`<span class="ap-insight ap-insight-warn"><span class="i i-xxs" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span> ${T.hpMissing2Plus(twoPlus)}</span>`);
  if (zero > 0) chips.push(`<span class="ap-insight ap-insight-warn"><span class="i i-xxs" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></span> ${T.zeroTermHours(zero)}</span>`);
  q('apInsights').innerHTML = chips.join('');

  // GPA mini-chart
  buildGpaChart(scopedStudents, s.gpa_distribution);
}

function summarizeStudents(students) {
  const gpas = students
    .filter(student => student.gpa != null)
    .map(student => Number(student.gpa))
    .filter(Number.isFinite);
  const avgGpa = gpas.length
    ? Math.round((gpas.reduce((sum, gpa) => sum + gpa, 0) / gpas.length) * 1000) / 1000
    : null;

  return {
    avg_gpa: avgGpa,
    low_gpa_count: gpas.filter(gpa => gpa < 2).length,
    gpa_distribution: [
      gpas.filter(gpa => gpa < 2).length,
      gpas.filter(gpa => gpa >= 2 && gpa < 3).length,
      gpas.filter(gpa => gpa >= 3 && gpa < 3.5).length,
      gpas.filter(gpa => gpa >= 3.5).length,
    ],
    student_count: students.length,
    needs_attention_count: students.filter(student => student.needs_attention).length,
    very_high_risk_count: students.filter(student => Number(student.risk_score || 0) >= 8).length,
    high_priority_missing_count: students.filter(student => student.has_high_priority_missing).length,
    zero_current_term_hours_count: students.filter(
      student => Number(student.current_term_registered_hours || 0) === 0
    ).length,
    two_plus_high_priority_missing_count: students.filter(student =>
      Array.isArray(student.high_priority_missing_courses)
      && student.high_priority_missing_courses.length >= 2
    ).length,
    current_term_registered_hours_total: students.reduce(
      (sum, student) => sum + Number(student.current_term_registered_hours || 0),
      0
    ),
  };
}

function buildGpaChart(students = allStudents, serverBuckets = null) {
  const buckets = Array.isArray(serverBuckets) && serverBuckets.length === 4
    ? serverBuckets.map(value => Number(value) || 0)
    : [0, 0, 0, 0]; // <2, 2-3, 3-3.5, 3.5+
  if (!Array.isArray(serverBuckets) || serverBuckets.length !== 4) {
    students.forEach(s => {
      if (s.gpa == null) return;
      const g = Number(s.gpa);
      if (g < 2) buckets[0]++;
      else if (g < 3) buckets[1]++;
      else if (g < 3.5) buckets[2]++;
      else buckets[3]++;
    });
  }
  const max = Math.max(...buckets, 1);
  const colors = ['ap-gpa-bar-danger', 'ap-gpa-bar-warn', 'ap-gpa-bar-ok', 'ap-gpa-bar-great'];
  q('apGpaChart').innerHTML = buckets.map((n, i) =>
    `<div class="ap-gpa-bar ${colors[i]}" style="height:${Math.max(n/max*100, 4)}%" title="${T.nStudentsTitle(n)}"></div>`
  ).join('');
}

/* ═══════════════════════════════════════════════════════════════
   FILTER + RENDER — Fix #1, #4, #8, #10
   ═══════════════════════════════════════════════════════════════ */
function setFocus(btn) {
  currentFocus = btn.dataset.focus;
  currentPage = 1;
  document.querySelectorAll('#apFilters .fb-dd').forEach(p => {
    p.classList.remove('active');
    p.setAttribute('aria-pressed', 'false');
  });
  btn.classList.add('active');
  btn.setAttribute('aria-pressed', 'true');
  apFilter();
}

function apFilter() {
  const search = (q('apSearch')?.value || '').trim().toLowerCase();
  const progFilter = selectedProgram();

  let rows = [...allStudents];

  if (search) {
    rows = rows.filter(s => String(s.student_id||'').toLowerCase().includes(search) || String(s.name||'').toLowerCase().includes(search));
  }
  if (progFilter) {
    rows = rows.filter(s => normalizeProgram(s.program) === progFilter);
  }
  if (currentFocus === 'attention') rows = rows.filter(s => s.needs_attention);
  else if (currentFocus === 'risk') rows = rows.filter(s => s.gpa != null && Number(s.gpa) < 2.0);
  else if (currentFocus === 'missing') rows = rows.filter(s => s.has_high_priority_missing);
  else if (currentFocus === 'zerohours') rows = rows.filter(s => Number(s.current_term_registered_hours || 0) === 0);

  // Sort: attention first, then lowest GPA
  rows.sort((a, b) => {
    const attA = a.needs_attention ? 1 : 0, attB = b.needs_attention ? 1 : 0;
    if (attA !== attB) return attB - attA;
    const gA = a.gpa == null ? 99 : Number(a.gpa), gB = b.gpa == null ? 99 : Number(b.gpa);
    if (gA !== gB) return gA - gB;
    return Number(a.student_id || 0) - Number(b.student_id || 0);
  });

  filteredStudents = rows;
  syncProgramPills();
  updateMetrics();
  updateCsvLink();
  renderPage();
  updatePillCounts();
}

function updatePillCounts() {
  const search = (q('apSearch')?.value || '').trim().toLowerCase();
  const progFilter = selectedProgram();
  let base = [...allStudents];
  if (search) base = base.filter(s => String(s.student_id||'').toLowerCase().includes(search) || String(s.name||'').toLowerCase().includes(search));
  if (progFilter) base = base.filter(s => normalizeProgram(s.program) === progFilter);

  const counts = {
    all:       base.length,
    attention: base.filter(s => s.needs_attention).length,
    risk:      base.filter(s => s.gpa != null && Number(s.gpa) < 2.0).length,
    missing:   base.filter(s => s.has_high_priority_missing).length,
    zerohours: base.filter(s => Number(s.current_term_registered_hours || 0) === 0).length,
  };
  document.querySelectorAll('#apFilters .fb-dd').forEach(btn => {
    const key = btn.dataset.focus;
    let badge = btn.querySelector('.fb-dd-count');
    if (!badge) { badge = document.createElement('span'); badge.className = 'fb-dd-count'; btn.appendChild(badge); }
    badge.textContent = counts[key] ?? '';
  });
}

function renderPage() {
  const total = filteredStudents.length;
  const pages = Math.ceil(total / PAGE_SIZE) || 1;
  if (currentPage > pages) currentPage = pages;

  const start = (currentPage - 1) * PAGE_SIZE;
  const pageRows = filteredStudents.slice(start, start + PAGE_SIZE);

  q('apShowing').innerHTML = total === 0
    ? T.noStudentsMatch
    : T.showingRange(start + 1, Math.min(start + PAGE_SIZE, total), total);

  renderPagination(pages);
  renderTable(pageRows);
}

function renderPagination(pages) {
  const wrap = q('apPagination');
  if (pages <= 1) { wrap.innerHTML = ''; return; }
  let html = `<button class="pg-btn" onclick="goPage(${currentPage-1})" ${currentPage<=1?'disabled':''}>‹</button>`;
  for (let i = 1; i <= pages; i++) {
    if (pages > 7 && i > 2 && i < pages - 1 && Math.abs(i - currentPage) > 1) {
      if (i === 3 || i === pages - 2) html += '<span class="text-t3" style="padding:0 0.2rem">…</span>';
      continue;
    }
    html += `<button class="pg-btn ${i===currentPage?'active':''}" onclick="goPage(${i})">${i}</button>`;
  }
  html += `<button class="pg-btn" onclick="goPage(${currentPage+1})" ${currentPage>=pages?'disabled':''}>›</button>`;
  wrap.innerHTML = html;
}

function goPage(p) { currentPage = p; renderPage(); q('apTable').scrollIntoView({ behavior:'smooth', block:'start' }); }

function renderTable(rows) {
  const tbody = q('apTable').querySelector('tbody');
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="10"><div class="ap-empty"><span class="ap-empty-icon"><span class="i i-xl" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></span></span><div class="ap-empty-title">${T.noStudentsMatch}</div></div></td></tr>`;
    return;
  }

  /* Shorten long names → 1st 2nd Last (drop middle parts) */
  function shortName(n) {
    if (!n) return '—';
    const parts = n.trim().split(/\s+/);
    if (parts.length > 3) return `${parts[0]} ${parts[1]} ${parts[parts.length - 1]}`;
    return n;
  }

  tbody.innerHTML = rows.map(s => {
    const sid = s.student_id;
    const gpaVal = s.gpa == null ? '—' : Number(s.gpa).toFixed(2);
    const gpaNum = s.gpa != null ? Number(s.gpa) : null;
    const gpaCls = gpaNum == null ? '' : gpaNum < 2.0 ? 'cr-gpa-lo' : gpaNum < 3.0 ? 'cr-gpa-md' : 'cr-gpa-hi';
    const rs = Number(s.risk_score || 0);

    const riskPill = rs >= 8
      ? `<span class="pill-status pill-r"><span class="pill-dot"></span>${rs.toFixed(1)}</span>`
      : rs >= 4
      ? `<span class="pill-status pill-a"><span class="pill-dot"></span>${rs.toFixed(1)}</span>`
      : `<span class="pill-status pill-g"><span class="pill-dot"></span>${rs.toFixed(1)}</span>`;

    const hpList = Array.isArray(s.high_priority_missing_courses) ? s.high_priority_missing_courses : [];
    const hpCell = s.has_high_priority_missing
      ? `<button type="button" class="fb-dd ap-hp-btn fs-11 text-warning" data-sid="${sid}" data-courses='${esc(JSON.stringify(hpList))}' style="padding:4px 10px">View (${hpList.length})</button>`
      : '<span class="text-t3 fs-11">—</span>';

    const attCell = s.needs_attention
      ? `<span class="pill-status pill-r"><span class="pill-dot"></span>${T.attention}</span>`
      : `<span class="text-t3 fs-11">${T.ok}</span>`;

    const trCls = [
      'cr-row',
      sid == selectedSid ? 'selected' : '',
    ].filter(Boolean).join(' ');

    const checked = batchSelected.has(String(sid)) ? 'checked' : '';
    const detailId = `apd-${sid}`;
    const plannerHref = `/planner/?student=${encodeURIComponent(sid)}`;

    const reasonMap = { low_gpa: T.lowGpa, high_priority_missing: T.hpMissing, zero_current_term_hours: T.zeroHours };
    const reasons = (Array.isArray(s.attention_reasons) ? s.attention_reasons : [])
      .map(r => `<span class="pill-status pill-a fs-10" style="padding:2px 7px"><span class="pill-dot"></span>${esc(reasonMap[r]||r)}</span>`).join(' ') || '<span class="text-t3">—</span>';

    return `<tr class="${trCls}" data-sid="${sid}" onclick="onRowClick(event, '${sid}')">
      <td onclick="event.stopPropagation()" style="padding-inline-start:14px;"><input type="checkbox" class="ap-check" ${checked} onchange="toggleBatch('${sid}',this.checked)" style="accent-color:var(--teal);"></td>
      <td><span class="cr-id">${sid}</span></td>
      <td><div class="cr-nm">${esc(shortName(s.name))}</div><div class="cr-sub">${esc(s.program||'')} · ${esc(s.section||'')}</div></td>
      <td class="cr-prog">${esc(s.program||'—')}</td>
      <td>${attCell}</td>
      <td><span class="cr-gpa ${gpaCls}">${gpaVal}</span></td>
      <td>${riskPill}</td>
      <td><span class="fw-semibold" style="font-size:12px">${s.current_term_registered_hours||0}</span></td>
      <td>${hpCell}</td>
      <td class="cr-actions" onclick="event.stopPropagation()">
        <button class="btn-circle ap-expand" data-detail="${detailId}" aria-expanded="false" onclick="toggleDetail(this)" title="${T.expandDetails}"><span class="i i-sm" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></span></button>
      </td>
    </tr>
    <tr id="${detailId}" class="ap-detail-row d-none">
      <td colspan="10">
        <div class="ap-detail-grid">
          <div><span class="ap-detail-label">${T.section}</span>${esc(s.section||'—')}</div>
          <div><span class="ap-detail-label">${T.status}</span>${esc(s.status||'—')}</div>
          <div><span class="ap-detail-label">${T.termHours}</span>${s.current_term_registered_hours||0}</div>
          <div><span class="ap-detail-label">${T.earnedReg}</span>${s.total_earned_credits||0} / ${s.total_registered_credits||0}</div>
          <div><span class="ap-detail-label">${T.regNo}</span>${esc(s.registration_no||'—')}</div>
          <div class="ap-detail-full"><span class="ap-detail-label">${T.reasons}</span>${reasons}</div>
        </div>
      </td>
    </tr>`;
  }).join('');
}

/* ═══════════════════════════════════════════════════════════════
   ROW CLICK → DRAWER — Fix #2
   ═══════════════════════════════════════════════════════════════ */
function onRowClick(e, sid) {
  if (e.target.closest('button, a, input')) return;
  openDrawer(sid);
}

function openDrawer(sid) {
  const s = allStudents.find(x => String(x.student_id) === String(sid));
  if (!s) return;

  cancelGraduationRequest();
  graduationBaseline = 'registered_timetable';
  graduationGraphMode = 'term';
  graduationRenderedGraph = null;
  graduationBandLabels = {};
  selectedSid = sid;
  // Highlight row
  document.querySelectorAll('#apTable tbody tr.cr-row').forEach(r => r.classList.toggle('selected', r.dataset.sid == sid));

  const gpa = s.gpa == null ? '—' : Number(s.gpa).toFixed(2);
  const rs = Number(s.risk_score || 0);
  const riskCls = rs >= 8 ? 'ap-risk-high' : rs >= 4 ? 'ap-risk-mid' : 'ap-risk-low';
  const reasonMap = { low_gpa: T.lowGpa, high_priority_missing: T.hpMissing, zero_current_term_hours: T.zeroHours };
  const reasons = (Array.isArray(s.attention_reasons) ? s.attention_reasons : []).map(r => esc(reasonMap[r]||r)).join(', ') || T.none;
  const hpList = Array.isArray(s.high_priority_missing_courses) ? s.high_priority_missing_courses : [];
  const termPatternLabel = c => c.term_pattern === 'odd'
    ? T.oddPlanTerms
    : c.term_pattern === 'even'
      ? T.evenPlanTerms
      : T.currentTermPattern;
  const hpHtml = hpList.length
    ? `<div class="table-wrap" style="max-height:200px;margin-top:0.4rem;"><table class="table table-sm mb-0"><thead><tr><th scope="col">${T.course}</th><th scope="col">${T.score}</th><th scope="col">${T.planTermPattern}</th></tr></thead><tbody>${hpList.map(c=>`<tr><td>${esc(c.course_code||'—')}</td><td>${Number(c.score||0).toFixed(2)}</td><td>${termPatternLabel(c)}</td></tr>`).join('')}</tbody></table></div>`
    : `<span style="color:var(--muted-light);font-size:0.82rem;">${T.noHpMissing}</span>`;

  const plannerHref = `/planner/?student=${encodeURIComponent(sid)}`;
  const graduationHref = `/advisor-portfolio/students/${encodeURIComponent(sid)}/graduation/`;

  q('apDrawerWrap').innerHTML = `
    <div class="ap-drawer-backdrop" onclick="closeDrawer()"></div>
    <div class="ap-drawer" role="dialog" aria-modal="true" aria-label="${T.studentDetails}">
      <button class="ap-drawer-close" onclick="closeDrawer()" aria-label="${T.close}"><span class="i i-xs" aria-hidden="true"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></span></button>
      <div class="ap-drawer-name">${esc(s.name || '—')}</div>
      <div class="ap-drawer-id"><span class="ap-drawer-id-pill">ID: ${sid}</span> · ${esc(s.program||'—')} · ${esc(s.section||'—')}</div>

      ${s.needs_attention ? '<div class="meta-banner meta-warn mb-3" style="font-size:0.82rem;"><span class="i i-xs" aria-hidden="true" style="vertical-align:-2px"><svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span> ' + T.needsAttention + esc(reasons) + '</div>' : ''}

      <div class="ap-drawer-section">
        <div class="ap-drawer-section-title">${T.academicInfo}</div>
        <div class="ap-drawer-kv">
          <div class="ap-drawer-kv-item"><div class="ap-drawer-kv-label">${T.gpa}</div><strong>${gpa}</strong></div>
          <div class="ap-drawer-kv-item"><div class="ap-drawer-kv-label">${T.riskScore}</div><span class="risk-pill ${riskCls}">${rs.toFixed(2)}</span></div>
          <div class="ap-drawer-kv-item"><div class="ap-drawer-kv-label">${T.status}</div>${esc(s.status||'—')}</div>
          <div class="ap-drawer-kv-item"><div class="ap-drawer-kv-label">${T.registrationNo}</div>${esc(s.registration_no||'—')}</div>
        </div>
      </div>

      <div class="ap-drawer-section">
        <div class="ap-drawer-section-title">${T.credits}</div>
        <div class="ap-drawer-kv">
          <div class="ap-drawer-kv-item"><div class="ap-drawer-kv-label">${T.termHours}</div><strong>${s.current_term_registered_hours||0}</strong></div>
          <div class="ap-drawer-kv-item"><div class="ap-drawer-kv-label">${T.earned}</div>${s.total_earned_credits||0}</div>
          <div class="ap-drawer-kv-item"><div class="ap-drawer-kv-label">${T.registered}</div>${s.total_registered_credits||0}</div>
        </div>
      </div>

      <div class="ap-drawer-section">
        <div class="ap-drawer-section-title">${T.highPriorityMissing}</div>
        ${hpHtml}
      </div>

      <div class="ap-drawer-actions">
        <a href="${plannerHref}" target="_blank" class="btn btn-sm btn-outline-primary"><span class="i i-xs" aria-hidden="true" style="vertical-align:-2px"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg></span> ${T.openPlanner}</a>
        <a class="btn btn-sm btn-outline-primary" id="apGraduationAction"
           href="${graduationHref}" target="_blank" rel="noopener">
          <span class="i i-xs" aria-hidden="true" style="vertical-align:-2px"><svg viewBox="0 0 24 24"><path d="M3 3h18v18H3z"/><path d="M7 15l3-3 2 2 5-6"/></svg></span>
          ${T.graduationPlan}
        </a>
        <button class="btn btn-sm btn-export" onclick="copyText('${sid}', this)" title="${IS_AR ? 'نسخ معرّف الطالب إلى الحافظة' : 'Copy student ID to clipboard'}">${T.copyId}</button>
        ${hpList.length ? `<button class="btn btn-sm btn-export" onclick="copyHpCourses('${sid}', this)" title="${IS_AR ? 'نسخ المقررات ذات الأولوية إلى الحافظة' : 'Copy high-priority missing courses to clipboard'}">${T.copyHpCourses}</button>` : ''}
      </div>

    </div>`;

  // Focus trap + Escape key
  drawerPreviousFocus = document.activeElement;
  document.addEventListener('keydown', drawerKeyHandler);
  // Focus close button after animation settles
  requestAnimationFrame(() => {
    const closeBtn = q('apDrawerWrap').querySelector('.ap-drawer-close');
    if (closeBtn) closeBtn.focus();
  });
}

let drawerPreviousFocus = null;

function closeDrawer() {
  const wrap = q('apDrawerWrap');
  const drawer = wrap.querySelector('.ap-drawer');
  const backdrop = wrap.querySelector('.ap-drawer-backdrop');
  cancelGraduationRequest();
  graduationRenderedGraph = null;
  graduationBandLabels = {};
  if (drawer) drawer.classList.add('closing');
  if (backdrop) backdrop.classList.add('closing');
  setTimeout(() => {
    wrap.innerHTML = '';
    selectedSid = null;
    document.querySelectorAll('#apTable tbody tr.selected').forEach(r => r.classList.remove('selected'));
  }, 200);
  document.removeEventListener('keydown', drawerKeyHandler);
  if (drawerPreviousFocus) { drawerPreviousFocus.focus(); drawerPreviousFocus = null; }
}

function drawerKeyHandler(e) {
  if (e.key === 'Escape') { closeDrawer(); return; }
  if (e.key !== 'Tab') return;
  const drawer = q('apDrawerWrap').querySelector('.ap-drawer');
  if (!drawer) return;
  const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
  const els = Array.from(drawer.querySelectorAll(FOCUSABLE));
  if (!els.length) return;
  const first = els[0], last = els[els.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

/* ═══════════════════════════════════════════════════════════════
   GRADUATION PLAN — read-only, per-student drawer panel
   ═══════════════════════════════════════════════════════════════ */
function cancelGraduationRequest() {
  graduationRequestGeneration += 1;
  if (graduationRequestController) graduationRequestController.abort();
  graduationRequestController = null;
}

function openGraduationPlan(sid) {
  if (String(selectedSid) !== String(sid)) return;
  const panel = q('apGraduationPanel');
  const action = q('apGraduationAction');
  const drawer = q('apDrawerWrap')?.querySelector('.ap-drawer');
  if (!panel || !action || !drawer) return;

  if (!panel.hidden) {
    q(graduationBaseline === 'registered_timetable' ? 'apGradRegisteredTab' : 'apGradRecommendedTab')?.focus();
    return;
  }

  panel.hidden = false;
  action.setAttribute('aria-expanded', 'true');
  drawer.classList.add('ap-drawer--graduation');
  graduationBaseline = 'registered_timetable';
  selectGraduationBaseline('registered_timetable', { force: true, focus: false });
  requestAnimationFrame(() => q('apGradRegisteredTab')?.focus());
}

function graduationBaselineKeydown(event) {
  const tabs = Array.from(q('apGraduationPanel')?.querySelectorAll('[role="tab"]') || []);
  if (!tabs.length || !tabs.includes(event.target)) return;
  const current = tabs.indexOf(event.target);
  let next = null;
  if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % tabs.length;
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current - 1 + tabs.length) % tabs.length;
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = tabs.length - 1;
  if (next === null) return;
  event.preventDefault();
  const baseline = tabs[next].dataset.baseline;
  selectGraduationBaseline(baseline, { focus: true });
}

function selectGraduationBaseline(mode, options = {}) {
  if (!['registered_timetable', 'recommended_current_term'].includes(mode)) return;
  const panel = q('apGraduationPanel');
  const content = q('apGraduationContent');
  if (!panel || !content || panel.hidden) return;

  graduationBaseline = mode;
  const activeTab = mode === 'registered_timetable' ? q('apGradRegisteredTab') : q('apGradRecommendedTab');
  panel.querySelectorAll('[role="tab"]').forEach(tab => {
    const active = tab.dataset.baseline === mode;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  q('apGraduationTabPanel')?.setAttribute('aria-labelledby', activeTab?.id || 'apGradRegisteredTab');
  if (options.focus) activeTab?.focus();

  const needsLoad = options.force || content.dataset.baseline !== mode;
  if (needsLoad) loadGraduationPlan(String(selectedSid), mode);
}

function retryGraduationPlan() {
  if (selectedSid == null) return;
  loadGraduationPlan(String(selectedSid), graduationBaseline);
}

function graduationApiMessage(payload, fallback) {
  if (!payload || typeof payload !== 'object') return fallback;
  const direct = payload.error || payload.message || payload.detail;
  if (typeof direct === 'string' && direct.trim()) return direct.trim();
  if (direct && typeof direct === 'object' && typeof direct.message === 'string') return direct.message;
  return fallback;
}

function unpackGraduationPayload(payload) {
  if (!payload || typeof payload !== 'object') return { report: null, presentation: {} };
  const containers = [payload];
  if (payload.data && typeof payload.data === 'object') containers.push(payload.data);
  if (payload.graduation && typeof payload.graduation === 'object') containers.push(payload.graduation);

  let report = null;
  const reportKeys = ['report', 'graduation_report', 'graduation'];
  containers.some(container => {
    for (const key of reportKeys) {
      const candidate = container[key];
      if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
        if ('term_plan' in candidate || 'planning_baseline_kind' in candidate || 'plan_courses_total' in candidate) {
          report = candidate;
          return true;
        }
      }
    }
    if ('term_plan' in container || 'planning_baseline_kind' in container || 'plan_courses_total' in container) {
      report = container;
      return true;
    }
    return false;
  });

  let presentation = {};
  for (const container of containers) {
    const candidate = container.presentation || container.graduation_presentation;
    if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
      presentation = candidate;
      break;
    }
  }
  if (!Object.keys(presentation).length && report?.presentation && typeof report.presentation === 'object') {
    presentation = report.presentation;
  }
  return { report, presentation };
}

function renderGraduationState(kind, message = '') {
  const content = q('apGraduationContent');
  if (!content) return;
  graduationRenderedGraph = null;
  graduationBandLabels = {};
  if (kind === 'loading') {
    content.innerHTML = `<div class="ap-grad-state ap-grad-state-loading" role="status">
      <span class="ap-grad-spinner" aria-hidden="true"></span><span>${T.loadingGraduation}</span>
    </div>`;
    return;
  }
  if (kind === 'error') {
    content.innerHTML = `<div class="ap-grad-state ap-grad-state-error" role="alert">
      <strong>${T.failedGraduation}</strong>
      ${message ? `<span>${esc(message)}</span>` : ''}
      <button type="button" class="ap-grad-retry" onclick="retryGraduationPlan()">${T.retry}</button>
    </div>`;
    return;
  }
  content.innerHTML = `<div class="ap-grad-state ap-grad-state-empty" role="status">${T.noGraduationData}</div>`;
}

async function loadGraduationPlan(sid, mode) {
  const content = q('apGraduationContent');
  if (!content || String(selectedSid) !== String(sid) || graduationBaseline !== mode) return;

  if (graduationRequestController) graduationRequestController.abort();
  graduationRequestGeneration += 1;
  const generation = graduationRequestGeneration;
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  graduationRequestController = controller;
  content.dataset.baseline = mode;
  renderGraduationState('loading');

  const endpoint = `/api/advisor-portfolio/students/${encodeURIComponent(sid)}/graduation/?baseline=${encodeURIComponent(mode)}`;
  try {
    const options = { headers: { Accept: 'application/json' }, credentials: 'same-origin' };
    if (controller) options.signal = controller.signal;
    const response = await fetch(endpoint, options);
    const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (
      generation !== graduationRequestGeneration
      || String(selectedSid) !== String(sid)
      || graduationBaseline !== mode
    ) return;
    if (payload?.code === 'GRADUATION_REPORT_UNAVAILABLE') {
      renderGraduationState('empty');
      return;
    }
    if (!response.ok || payload?.ok === false) {
      throw new Error(graduationApiMessage(payload, `HTTP ${response.status}`));
    }
    const { report, presentation } = unpackGraduationPayload(payload);
    if (!report) {
      renderGraduationState('empty');
      return;
    }
    renderGraduationReport(report, presentation);
  } catch (error) {
    if (error?.name === 'AbortError') return;
    if (
      generation !== graduationRequestGeneration
      || String(selectedSid) !== String(sid)
      || graduationBaseline !== mode
    ) return;
    renderGraduationState('error', error?.message || T.networkFailure);
  } finally {
    if (generation === graduationRequestGeneration) graduationRequestController = null;
  }
}

function graduationNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function graduationCourseRows(value) {
  return Array.isArray(value) ? value.filter(row => row && typeof row === 'object') : [];
}

function graduationCourseCode(row) {
  return String(row?.code || row?.course_code || '').trim().toUpperCase();
}

function renderGraduationCourseList(rows, emptyText) {
  if (!rows.length) return `<p class="ap-grad-muted">${emptyText}</p>`;
  return `<ul class="ap-grad-course-list">${rows.map(row => {
    const code = graduationCourseCode(row) || '—';
    const name = String(row.name || row.course_name || '').trim();
    const credits = graduationNumber(row.credits ?? row.credit_hours);
    return `<li>
      <bdi class="ap-grad-course-code" dir="ltr">${esc(code)}</bdi>
      ${name ? `<span class="ap-grad-course-name">${esc(name)}</span>` : ''}
      ${credits !== null ? `<span class="ap-grad-course-credits">${credits} ${T.termCredits}</span>` : ''}
    </li>`;
  }).join('')}</ul>`;
}

function renderGraduationTermCourses(term) {
  let rows = graduationCourseRows(term?.courses);
  if (!rows.length && Array.isArray(term?.course_codes)) {
    rows = term.course_codes.map(code => ({ code }));
  }
  if (!rows.length) return `<span class="ap-grad-wait-text">${T.waitingForPrereqs}</span>`;
  return `<ul class="ap-grad-term-courses">${rows.map(row => {
    const code = graduationCourseCode(row) || '—';
    const name = String(row.name || row.course_name || '').trim();
    const credits = graduationNumber(row.credits ?? row.credit_hours);
    return `<li><bdi dir="ltr">${esc(code)}</bdi>${name ? `<span>${esc(name)}</span>` : ''}${credits !== null ? `<small>${credits}</small>` : ''}</li>`;
  }).join('')}</ul>`;
}

function renderGraduationTermTable(report) {
  const terms = Array.isArray(report.term_plan) ? report.term_plan : [];
  if (!terms.length) return `<div class="ap-grad-state ap-grad-state-empty">${T.noFutureTerms}</div>`;
  const rows = terms.map((term, index) => {
    const courses = graduationCourseRows(term?.courses);
    const codes = Array.isArray(term?.course_codes) ? term.course_codes : [];
    const waiting = term?.waiting_term === true || (!courses.length && !codes.length);
    const sequence = graduationNumber(term?.sequence) ?? index + 1;
    const year = graduationNumber(term?.academic_year);
    const academicTerm = graduationNumber(term?.term);
    const credits = graduationNumber(term?.credits) ?? 0;
    return `<tr class="${waiting ? 'ap-grad-waiting-row' : ''}">
      <td>${sequence}</td>
      <td><bdi dir="ltr">${year ?? '—'}/${academicTerm ?? '—'}</bdi></td>
      <td><span class="ap-grad-term-state ${waiting ? 'is-waiting' : 'is-planned'}">${waiting ? T.waiting : T.planned}</span></td>
      <td>${renderGraduationTermCourses(term)}</td>
      <td>${credits}</td>
    </tr>`;
  }).join('');
  return `<div class="ap-grad-table-scroll" role="region" tabindex="0" aria-label="${T.fullTermPlan}">
    <table class="ap-grad-term-table">
      <caption class="visually-hidden">${T.fullTermPlan}</caption>
      <thead><tr>
        <th scope="col">${T.sequence}</th>
        <th scope="col">${T.academicTerm}</th>
        <th scope="col">${T.termState}</th>
        <th scope="col">${T.plannedCourses}</th>
        <th scope="col">${T.termCredits}</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

function renderGraduationBlockers(report, presentation) {
  const source = Array.isArray(report.unresolved_requirements)
    ? report.unresolved_requirements
    : (Array.isArray(presentation?.unresolved_requirements) ? presentation.unresolved_requirements : []);
  if (!source.length) {
    return `<div class="ap-grad-blocker-empty">${report.simulation_completed === false ? T.blockerDetailsMissing : T.noBlockers}</div>`;
  }
  return `<ul class="ap-grad-blockers">${source.map(row => {
    const code = graduationCourseCode(row) || '—';
    const name = String(row?.name || row?.course_name || '').trim();
    const prerequisites = Array.isArray(row?.missing_course_prerequisites)
      ? row.missing_course_prerequisites
      : (Array.isArray(row?.missing_prerequisites) ? row.missing_prerequisites : []);
    const gate = row?.credit_hour_gate && typeof row.credit_hour_gate === 'object' ? row.credit_hour_gate : null;
    const required = graduationNumber(gate?.required);
    const effective = graduationNumber(gate?.effective_in_scenario ?? gate?.effective);
    const remaining = graduationNumber(gate?.remaining);
    const details = [];
    if (prerequisites.length) {
      details.push(`${T.missingPrereqs}: ${prerequisites.map(value => esc(String(value).toUpperCase())).join(', ')}`);
    }
    if (gate && (required !== null || effective !== null || remaining !== null)) {
      details.push(`${T.creditGate}: ${T.required} ${required ?? '—'}, ${T.effective} ${effective ?? '—'}, ${T.remaining} ${remaining ?? '—'}`);
    }
    return `<li>
      <div><bdi class="ap-grad-course-code" dir="ltr">${esc(code)}</bdi>${name ? `<span>${esc(name)}</span>` : ''}</div>
      ${details.length ? `<p>${details.join(' · ')}</p>` : ''}
    </li>`;
  }).join('')}</ul>`;
}

function graduationPresentationGraph(report, presentation) {
  if (presentation?.graph && typeof presentation.graph === 'object') {
    return { graph: presentation.graph, labels: presentation.band_labels || {} };
  }
  if (report?.graduation_presentation?.graph && typeof report.graduation_presentation.graph === 'object') {
    return {
      graph: report.graduation_presentation.graph,
      labels: report.graduation_presentation.band_labels || {},
    };
  }
  if (report?.scenario_graph && typeof report.scenario_graph === 'object') {
    return { graph: report.scenario_graph, labels: {} };
  }
  return { graph: null, labels: {} };
}

function normalizeGraduationGraph(source) {
  if (!source || typeof source !== 'object') return null;
  const items = (Array.isArray(source.items) ? source.items : []).filter(row => (
    row && typeof row === 'object' && row.course_code && row.prerequisite_course_code
  ));
  const extraNodes = Array.from(new Set((Array.isArray(source.extraNodes) ? source.extraNodes : [])
    .map(code => String(code || '').trim().toUpperCase()).filter(Boolean)));
  items.forEach(row => {
    const course = String(row.course_code || '').trim().toUpperCase();
    const prerequisite = String(row.prerequisite_course_code || '').trim().toUpperCase();
    if (course && !extraNodes.includes(course)) extraNodes.push(course);
    if (prerequisite && !extraNodes.includes(prerequisite)) extraNodes.push(prerequisite);
  });
  if (!extraNodes.length) return null;

  const pickMap = (values, transform = value => value) => Object.fromEntries(
    Object.entries(values && typeof values === 'object' ? values : {})
      .filter(([code]) => extraNodes.includes(String(code).toUpperCase()))
      .map(([code, value]) => [String(code).toUpperCase(), transform(value)])
  );
  return {
    items: items.map(row => ({
      course_code: String(row.course_code).trim().toUpperCase(),
      prerequisite_course_code: String(row.prerequisite_course_code).trim().toUpperCase(),
    })),
    termOf: pickMap(source.termOf, value => {
      const number = graduationNumber(value);
      return number === null ? value : number;
    }),
    nameOf: pickMap(source.nameOf, value => String(value || '')),
    statusOf: pickMap(source.statusOf, value => String(value || '').toLowerCase()),
    extraNodes,
  };
}

/* Keep the unfinished path and its immediate completed prerequisites legible.
   If the plan is already complete, retain the full graph rather than showing an
   unexplained empty map. */
function focusGraduationGraph(source) {
  const graph = normalizeGraduationGraph(source);
  if (!graph) return null;
  const kept = new Set(graph.extraNodes.filter(code => graph.statusOf[code] !== 'passed'));
  if (!kept.size) return graph;
  graph.items.forEach(edge => {
    if (kept.has(edge.course_code) && graph.statusOf[edge.prerequisite_course_code] === 'passed') {
      kept.add(edge.prerequisite_course_code);
    }
  });
  const filterMap = values => Object.fromEntries(Object.entries(values || {}).filter(([code]) => kept.has(code)));
  return {
    items: graph.items.filter(edge => kept.has(edge.course_code) && kept.has(edge.prerequisite_course_code)),
    termOf: filterMap(graph.termOf),
    nameOf: filterMap(graph.nameOf),
    statusOf: filterMap(graph.statusOf),
    extraNodes: Array.from(kept),
  };
}

function localizeGraduationBand(value) {
  const label = String(value ?? '');
  if (label === 'Completed before the scenario') return IS_AR ? 'مجتاز قبل فصل البداية' : label;
  if (label.startsWith('Recommended starting term ')) {
    const term = label.slice('Recommended starting term '.length);
    return IS_AR ? `مقررات فصل البداية الموصى بها: ${term}` : `Recommended starting-term courses ${term}`;
  }
  if (label.startsWith('Registered timetable ')) {
    const term = label.slice('Registered timetable '.length);
    return IS_AR ? `الجدول المسجّل لفصل البداية: ${term}` : label;
  }
  if (label.startsWith('Planning baseline ')) {
    const term = label.slice('Planning baseline '.length);
    return IS_AR ? `فصل البداية: ${term}` : `Starting term ${term}`;
  }
  if (label.startsWith('Projected ')) {
    const term = label.slice('Projected '.length);
    return IS_AR ? `فصل تقديري: ${term}` : `Projected term ${term}`;
  }
  return label;
}

function graduationBandLabel(number) {
  const label = graduationBandLabels[String(number)];
  if (label !== undefined) return localizeGraduationBand(label);
  return IS_AR ? `الفصل ${number}` : `Term ${number}`;
}

function graduationGraphStrings() {
  return {
    termHeading: graduationBandLabel,
    pgNoTermBand: IS_AR ? 'خارج الفصول المرتبة' : 'Outside the projected terms',
    pgGateTip: hours => IS_AR ? `شرط الساعات المعتمدة: ${hours}` : `Credit-hour requirement: ${hours}`,
    pgInferredTip: IS_AR ? 'موضع تقديري' : 'Position inferred',
    pgTermTip: graduationBandLabel,
    pgGate: T.creditGate,
    pgInferred: IS_AR ? 'موضع تقديري' : 'inferred position',
    pgFoundation: IS_AR ? 'بداية السلسلة' : 'chain start',
    pgIntermediate: IS_AR ? 'وسط السلسلة' : 'chain middle',
    pgTerminal: IS_AR ? 'نهاية السلسلة' : 'chain end',
    pgHoverHint: IS_AR ? 'مرّر على مقرر لإبراز سلسلته' : 'hover to highlight a chain',
    pgPassed: IS_AR ? 'مجتاز قبل البداية' : 'completed before the scenario',
    pgStudying: IS_AR ? 'مسجّل ويُفترض اجتيازه' : 'registered; assumed passed',
    pgOpen: graduationBaseline === 'recommended_current_term'
      ? (IS_AR ? 'موصى به أو مخطط' : 'recommended or planned')
      : (IS_AR ? 'مخطط في السيناريو' : 'planned in the scenario'),
    pgLocked: IS_AR ? 'غير محلول' : 'unresolved',
    pgSameTermWarn: count => IS_AR
      ? `${count} علاقة متطلب داخل الفصل نفسه.`
      : `${count} prerequisite relation(s) within one projected term.`,
    pgBackwardWarn: count => IS_AR
      ? `${count} علاقة يظهر فيها المتطلب بعد المقرر.`
      : `${count} prerequisite relation(s) scheduled after their course.`,
  };
}

function renderGraduationGraphFallback(host) {
  if (!graduationRenderedGraph) return;
  const byTerm = new Map();
  graduationRenderedGraph.extraNodes.forEach(code => {
    const term = graduationNumber(graduationRenderedGraph.termOf[code]);
    const key = term === null ? Number.MAX_SAFE_INTEGER : term;
    if (!byTerm.has(key)) byTerm.set(key, []);
    byTerm.get(key).push(code);
  });
  const prerequisites = {};
  graduationRenderedGraph.items.forEach(edge => {
    if (!prerequisites[edge.course_code]) prerequisites[edge.course_code] = [];
    prerequisites[edge.course_code].push(edge.prerequisite_course_code);
  });
  host.innerHTML = `<div class="ap-grad-tree-fallback">${Array.from(byTerm.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([term, codes]) => `<section>
      <h4>${term === Number.MAX_SAFE_INTEGER ? T.prerequisiteTree : graduationBandLabel(term)}</h4>
      <ul>${codes.sort().map(code => `<li>
        <bdi dir="ltr">${esc(code)}</bdi>
        ${prerequisites[code]?.length ? `<span>← ${prerequisites[code].map(item => esc(item)).join(', ')}</span>` : ''}
      </li>`).join('')}</ul>
    </section>`).join('')}</div>`;
}

function drawGraduationGraph() {
  const host = q('apGraduationGraph');
  if (!host || !graduationRenderedGraph) return;
  host.innerHTML = '';
  host.setAttribute('dir', 'ltr');
  if (!window.PrereqGraph?.render) {
    renderGraduationGraphFallback(host);
    return;
  }
  window.PrereqGraph.render(graduationRenderedGraph.items, host, {
    termOf: graduationRenderedGraph.termOf,
    nameOf: graduationRenderedGraph.nameOf,
    statusOf: graduationRenderedGraph.statusOf,
    extraNodes: graduationRenderedGraph.extraNodes,
    mode: graduationGraphMode,
    t: graduationGraphStrings(),
  });
}

function setGraduationGraphMode(mode) {
  if (!['term', 'depth'].includes(mode) || !graduationRenderedGraph) return;
  graduationGraphMode = mode;
  document.querySelectorAll('#apGraduationGraphModes [data-graph-mode]').forEach(button => {
    const active = button.dataset.graphMode === mode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  drawGraduationGraph();
}

function renderGraduationReport(report, presentation) {
  const content = q('apGraduationContent');
  if (!content) return;

  const total = graduationNumber(report.plan_courses_total);
  const passed = graduationNumber(report.plan_courses_passed);
  const percent = Math.max(0, Math.min(100, graduationNumber(report.percent_courses) ?? 0));
  const remainingCourses = graduationNumber(report.remaining_courses);
  const remainingCredits = graduationNumber(report.remaining_credits);
  const exactTerms = graduationNumber(
    report.estimated_terms_including_planning_baseline ?? report.estimated_terms_including_current
  );
  const lowerTerms = graduationNumber(
    report.lower_bound_terms_including_planning_baseline ?? report.lower_bound_terms_including_current
  );
  const exactAvailable = report.simulation_completed === true && exactTerms !== null;
  const displayedTerms = exactAvailable ? exactTerms : (lowerTerms !== null ? `≥ ${lowerTerms}` : '—');
  const baselineKind = report.planning_baseline_kind || graduationBaseline;
  const baselineCourses = graduationCourseRows(
    report.planning_baseline_courses_assumed_passed
      || report.planning_baseline?.courses_assumed_passed
      || report.current_courses_assumed_passed
  );
  const baselineYear = graduationNumber(
    report.planning_baseline_academic_year ?? report.planning_baseline?.academic_year
  );
  const baselineTerm = graduationNumber(report.planning_baseline_term ?? report.planning_baseline?.term);
  const baselineCredits = graduationNumber(
    report.planning_baseline_credits ?? report.planning_baseline?.credits ?? report.registered_credits_at_planning_baseline
  ) ?? 0;
  const provenanceLabel = baselineKind === 'recommended_current_term'
    ? T.recommendedProvenance
    : T.registeredProvenance;
  const provenanceDescription = baselineKind === 'recommended_current_term'
    ? T.recommendedExplanation
    : T.registeredExplanation;
  const graphData = graduationPresentationGraph(report, presentation);
  graduationRenderedGraph = focusGraduationGraph(graphData.graph);
  graduationBandLabels = graphData.labels && typeof graphData.labels === 'object' ? graphData.labels : {};
  graduationGraphMode = 'term';

  content.innerHTML = `
    <section class="ap-grad-section" aria-labelledby="apGradSummaryTitle">
      <h3 id="apGradSummaryTitle">${T.progressSummary}</h3>
      <div class="ap-grad-summary-grid">
        <div class="ap-grad-metric"><span>${T.program}</span><strong>${esc(report.program || '—')}</strong></div>
        <div class="ap-grad-metric"><span>${T.planProgress}</span><strong>${passed ?? '—'} / ${total ?? '—'}</strong><small>${percent}%</small></div>
        <div class="ap-grad-metric"><span>${T.coursesRemaining}</span><strong>${remainingCourses ?? '—'}</strong></div>
        <div class="ap-grad-metric"><span>${T.creditsRemaining}</span><strong>${remainingCredits ?? '—'}</strong></div>
        <div class="ap-grad-metric ap-grad-metric-emphasis"><span>${T.projectedTerms}</span><strong>${displayedTerms}</strong><small>${exactAvailable ? T.exactEstimate : T.incompleteEstimate}</small></div>
      </div>
      <div class="ap-grad-progress" role="progressbar" aria-label="${T.planProgress}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}">
        <span style="width:${percent}%"></span>
      </div>
    </section>

    <section class="ap-grad-section" aria-labelledby="apGradBaselineTitle">
      <h3 id="apGradBaselineTitle">${T.planningBaseline}</h3>
      <div class="ap-grad-provenance">
        <div><strong>${provenanceLabel}</strong><p>${provenanceDescription}</p></div>
        <dl>
          <div><dt>${T.planningTerm}</dt><dd><bdi dir="ltr">${baselineYear ?? '—'}/${baselineTerm ?? '—'}</bdi></dd></div>
          <div><dt>${T.baselineCredits}</dt><dd>${baselineCredits}</dd></div>
        </dl>
      </div>
      <h4>${T.baselineCourses}</h4>
      ${renderGraduationCourseList(baselineCourses, T.noBaselineCourses)}
    </section>

    <section class="ap-grad-section" aria-labelledby="apGradTermsTitle">
      <h3 id="apGradTermsTitle">${T.fullTermPlan}</h3>
      ${renderGraduationTermTable(report)}
    </section>

    <section class="ap-grad-section" aria-labelledby="apGradBlockersTitle">
      <h3 id="apGradBlockersTitle">${T.unresolvedBlockers}</h3>
      ${renderGraduationBlockers(report, presentation)}
    </section>

    <section class="ap-grad-section" aria-labelledby="apGradGraphTitle">
      <div class="ap-grad-graph-head">
        <h3 id="apGradGraphTitle">${T.prerequisiteTree}</h3>
        ${graduationRenderedGraph ? `<div class="ap-grad-graph-modes" id="apGraduationGraphModes" role="group" aria-label="${T.prerequisiteTree}">
          <button type="button" class="is-active" data-graph-mode="term" aria-pressed="true" onclick="setGraduationGraphMode('term')">${T.byProjectedTerm}</button>
          <button type="button" data-graph-mode="depth" aria-pressed="false" onclick="setGraduationGraphMode('depth')">${T.byPrerequisiteChain}</button>
        </div>` : ''}
      </div>
      ${graduationRenderedGraph
        ? `<div class="ap-grad-graph-scroll" role="region" tabindex="0" aria-label="${T.graphLabel}"><div class="ap-grad-graph" id="apGraduationGraph" role="img" aria-label="${T.graphLabel}"></div></div>`
        : `<div class="ap-grad-state ap-grad-state-empty">${T.noPrerequisiteTree}</div>`}
    </section>`;

  if (graduationRenderedGraph) requestAnimationFrame(drawGraduationGraph);
}

function copyHpCourses(sid, triggerBtn) {
  const s = allStudents.find(x => String(x.student_id) === String(sid));
  if (!s) return;
  const courses = Array.isArray(s.high_priority_missing_courses) ? s.high_priority_missing_courses : [];
  const txt = courses.map(c => `${c.course_code}(${Number(c.score||0).toFixed(2)})`).join(', ');
  copyText(txt, triggerBtn);
}

/* ═══════════════════════════════════════════════════════════════
   EXPAND DETAIL ROW — Fix #9
   ═══════════════════════════════════════════════════════════════ */
function toggleDetail(btn) {
  const id = btn.dataset.detail;
  const row = document.getElementById(id);
  if (!row) return;
  const open = btn.getAttribute('aria-expanded') === 'true';
  btn.setAttribute('aria-expanded', open ? 'false' : 'true');
  row.classList.toggle('d-none', open);
}

/* ═══════════════════════════════════════════════════════════════
   HP BUTTON CLICK (in table) — shows drawer
   ═══════════════════════════════════════════════════════════════ */
document.addEventListener('click', e => {
  const hpBtn = e.target.closest('.ap-hp-btn');
  if (!hpBtn) return;
  e.stopPropagation();
  const sid = hpBtn.dataset.sid;
  if (sid) openDrawer(sid);
});

/* ═══════════════════════════════════════════════════════════════
   BATCH SELECTION — Fix #15
   ═══════════════════════════════════════════════════════════════ */
function toggleBatch(sid, checked) {
  if (checked) batchSelected.add(String(sid));
  else batchSelected.delete(String(sid));
  updateBatchBar();
}

function toggleAllChecks(master) {
  const cbs = document.querySelectorAll('#apTable tbody .ap-check');
  cbs.forEach(cb => {
    const tr = cb.closest('tr');
    const sid = tr?.dataset?.sid;
    if (sid) { cb.checked = master.checked; if (master.checked) batchSelected.add(sid); else batchSelected.delete(sid); }
  });
  updateBatchBar();
}

function clearBatch() {
  batchSelected.clear();
  document.querySelectorAll('#apTable tbody .ap-check').forEach(cb => cb.checked = false);
  q('apCheckAll').checked = false;
  updateBatchBar();
}

function updateBatchBar() {
  q('apBatchCount').textContent = batchSelected.size;
  q('apBatchBar').classList.toggle('active', batchSelected.size > 0);
}

function copySelectedIds(triggerBtn) {
  if (!batchSelected.size) return;
  copyText(Array.from(batchSelected).join(','), triggerBtn);
}

function exportSelectedCsv() {
  if (!batchSelected.size) { notify.warning(T.noStudentsSelected); return; }
  const selected = allStudents.filter(s => batchSelected.has(String(s.student_id)));
  const header = 'student_id,name,program,section,gpa,risk_score,needs_attention,has_hp_missing,term_hours';
  const rows = selected.map(s => [s.student_id,`"${s.name||''}"`,s.program||'',s.section||'',s.gpa??'',s.risk_score||0,s.needs_attention?1:0,s.has_high_priority_missing?1:0,s.current_term_registered_hours||0].join(','));
  const csv = '\ufeff' + header + '\n' + rows.join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `portfolio_selected_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
  notify.success(T.exportedStudents(selected.length));
}

/* ═══════════════════════════════════════════════════════════════
   COPY IDS + CSV LINK — Fix #5
   ═══════════════════════════════════════════════════════════════ */
function copyFilteredIds(triggerBtn) {
  if (!filteredStudents.length) { notify.warning(T.noStudentsCopy); return; }
  copyText(filteredStudents.map(s => s.student_id).join(','), triggerBtn);
}

function copyHighRiskIds(triggerBtn) {
  const ids = allStudents.filter(s => Number(s.risk_score || 0) >= 8).map(s => s.student_id);
  if (!ids.length) { notify.warning(T.noHighRisk); return; }
  copyText(ids.join(','), triggerBtn);
}

function updateCsvLink() {
  if (!currentAdvisorId) { q('apCsvLink').href = '#'; return; }
  const search = (q('apSearch')?.value || '').trim();
  const prog = (q('apProgramFilter')?.value || '').trim();
  q('apCsvLink').href = `/export/students-by-advisor.csv?advisor_id=${encodeURIComponent(currentAdvisorId)}&search=${encodeURIComponent(search)}&focus=${encodeURIComponent(currentFocus)}&program_filter=${encodeURIComponent(prog)}`;
}

/* wireSortableTable — provided by shared-ux.js */

/* ═══════════════════════════════════════════════════════════════
   KEYBOARD NAVIGATION — Fix #13
   ═══════════════════════════════════════════════════════════════ */
document.addEventListener('keydown', e => {
  // Nested widgets (notably the graduation baseline tabs) own their arrow keys.
  if (e.defaultPrevented) return;
  // Ignore if typing in input
  if (['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)) return;

  const rows = Array.from(document.querySelectorAll('#apTable tbody tr[data-sid]:not(.ap-detail-row)'));
  if (!rows.length) return;

  const currentIdx = rows.findIndex(r => r.dataset.sid == selectedSid);

  if (e.key === 'ArrowDown' || e.key === 'j') {
    e.preventDefault();
    const next = currentIdx < rows.length - 1 ? currentIdx + 1 : 0;
    openDrawer(rows[next].dataset.sid);
    rows[next].scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'ArrowUp' || e.key === 'k') {
    e.preventDefault();
    const prev = currentIdx > 0 ? currentIdx - 1 : rows.length - 1;
    openDrawer(rows[prev].dataset.sid);
    rows[prev].scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'Escape') {
    closeDrawer();
  } else if (e.key === 'Enter' && currentIdx >= 0) {
    const btn = rows[currentIdx].querySelector('.ap-expand');
    if (btn) toggleDetail(btn);
  }
});

/* Debounced filter wrappers — debounce() provided by shared-ux.js */
const debouncedApFilter = debounce(() => { currentPage = 1; apFilter(); }, 250);

/* The options come from this roster, so an exact program can be selected without
   guessing whether AI and AI2 (or DS and DS2) are separate curricula. */
q('apProgramFilter').addEventListener('change', () => {
  currentPage = 1;
  apFilter();
});

/* ═══════════════════════════════════════════════════════════════
   INIT
   ═══════════════════════════════════════════════════════════════ */
wireSortableTable('apTable');

/* Mobile card layout for ≤768px */
wireMobileCards('apTable', {
  labels: IS_AR
    ? ['', 'المعرف', 'الاسم', 'البرنامج', 'الحالة', 'المعدل', 'المخاطر', 'س.م. مسجلة', 'أولوية مفقودة', '']
    : ['', 'ID', 'Name', 'Program', 'Status', 'GPA', 'Risk', 'Reg. Cr.', 'HP Missing', ''],
  primaryCols: [1, 2],
  hideCols: [0, 7, 8],
  actionCol: 9,
});

/* The values the template writes, NOT their names.
 *
 * These were `'userRole'` and `'userAdvisorId'` — the identifiers as string
 * literals — so the test below was `'userRole' === 'ADVISOR'`, false for
 * everyone, and no adviser has ever taken the own-portfolio branch.
 *
 * It was a DEAD END, not a degraded path. `advisor_portfolio.html` puts `d-none`
 * on the adviser bar for exactly `role == 'ADVISOR'`, so the else branch fell
 * through to a picker the same user could not see: an empty table telling them
 * to "choose an advisor above", pointing at nothing.
 *
 * `typeof` rather than a bare reference: these are `const` in a separate inline
 * <script>, so a template edit that drops that block would throw a ReferenceError
 * at the top level of this file and take the rest of the page's JavaScript with
 * it. Degrading to the picker is the safe failure.
 */
const USER_ROLE = typeof userRole === 'string' ? userRole : '';
const USER_ADVISOR_ID = typeof userAdvisorId === 'string' ? userAdvisorId : '';

const HIDE_ADVISOR_PICKER =
  typeof hideAdvisorPicker === 'boolean' ? hideAdvisorPicker : false;

if (HIDE_ADVISOR_PICKER) {
  // Advisor role: skip dropdown, load own students immediately
  loadStudents(USER_ADVISOR_ID);
} else {
  // Super admin / general advisor: show dropdown
  loadAdvisors();
}

/* Mark sidebar link active */
document.querySelectorAll('.sidebar .nav-link').forEach(link => {
  if (link.getAttribute('href') === '/advisor-portfolio/') link.classList.add('active');
});
