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
let batchSelected = new Set();
let selectedSid = null;

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

  // ── HP table headers ──
  course:             IS_AR ? 'المقرر'                               : 'Course',
  score:              IS_AR ? 'الدرجة'                               : 'Score',
  bucket:             IS_AR ? 'الفئة'                                : 'Bucket',
  thisParity:         IS_AR ? 'هذا الطرف'                            : 'This parity',
  other:              IS_AR ? 'آخر'                                  : 'Other',

  // ── GPA chart ──
  nStudentsTitle:  (n) => IS_AR ? `${n} طالب`                       : `${n} students`,

  // ── Batch & export ──
  noStudentsSelected: IS_AR ? 'لم يتم اختيار طلاب'                  : 'No students selected',
  exportedStudents:(n) => IS_AR ? `تم تصدير ${n} طالب`               : `Exported ${n} students`,
  noStudentsCopy:     IS_AR ? 'لا يوجد طلاب للنسخ'                   : 'No students to copy',
  noHighRisk:         IS_AR ? 'لا يوجد طلاب بخطورة عالية'            : 'No high-risk students',
};

/* q, esc, getCookie, csrfToken, csrfHeaders — provided by shared-utils.js */

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
    const data = await res.json();
    if (!res.ok || !Array.isArray(data.items)) return;
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
  currentAdvisorId = advisorId;
  currentPage = 1;
  batchSelected.clear();
  updateBatchBar();

  const tbody = q('apTable').querySelector('tbody');
  tbody.innerHTML = `<tr><td colspan="10"><div class="ap-empty"><span class="ap-empty-icon"><span class="i i-xl" aria-hidden="true"><svg viewBox="0 0 24 24"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg></span></span><div class="ap-empty-title">${T.loadingStudents}</div></div></td></tr>`;

  try {
    const res = await fetch(`/report/students-by-advisor/?advisor_id=${encodeURIComponent(advisorId)}`);
    const data = await res.json();
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

    q('apAdvisorChip').textContent = advisorId;
    q('apCountChip').textContent = T.nStudents(allStudents.length);
    q('apLoadedLabel').classList.remove('d-none');
    q('apLoadedTime').textContent = new Date().toLocaleTimeString();
    q('apMetricsWrap').classList.remove('d-none');

    updateMetrics();
    updateCsvLink();
    apFilter();
    notify.success(T.loadedStudents(allStudents.length), advisorId);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="10" class="text-danger small"><span class="i i-xs" aria-hidden="true" style="vertical-align:-2px"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg></span> Network failure — ${esc(String(err))}</td></tr>`;
    notify.error(T.networkFailure);
  }
}

function clearPortfolio() {
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
}

/* ═══════════════════════════════════════════════════════════════
   METRICS — Fix #6, #7, #11, #12, #14
   ═══════════════════════════════════════════════════════════════ */
function updateMetrics() {
  const s = summaryCache;
  q('mAttention').textContent = s.needs_attention_count || 0;
  q('mHighRisk').textContent = s.very_high_risk_count || 0;
  q('mStudents').textContent = allStudents.length;
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

  // Program breakdown pills
  const progs = Object.entries(s.program_breakdown || {});
  q('apPrograms').innerHTML = progs.length
    ? progs.sort((a,b) => b[1]-a[1]).map(([k,v]) => `<span class="ap-prog-pill">${esc(k)} <span class="ap-prog-n">${v}</span></span>`).join('')
    : '<span style="font-size:0.78rem;color:var(--muted);">—</span>';

  // GPA mini-chart
  buildGpaChart();
}

function buildGpaChart() {
  const buckets = [0, 0, 0, 0]; // <2, 2-3, 3-3.5, 3.5+
  allStudents.forEach(s => {
    if (s.gpa == null) return;
    const g = Number(s.gpa);
    if (g < 2) buckets[0]++;
    else if (g < 3) buckets[1]++;
    else if (g < 3.5) buckets[2]++;
    else buckets[3]++;
  });
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
  const progFilter = (q('apProgramFilter')?.value || '').trim().toUpperCase();

  let rows = [...allStudents];

  if (search) {
    rows = rows.filter(s => String(s.student_id||'').toLowerCase().includes(search) || String(s.name||'').toLowerCase().includes(search));
  }
  if (progFilter) {
    rows = rows.filter(s => String(s.program||'').toUpperCase() === progFilter);
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
  updateCsvLink();
  renderPage();
  updatePillCounts();
}

function updatePillCounts() {
  const search = (q('apSearch')?.value || '').trim().toLowerCase();
  const progFilter = (q('apProgramFilter')?.value || '').trim().toUpperCase();
  let base = [...allStudents];
  if (search) base = base.filter(s => String(s.student_id||'').toLowerCase().includes(search) || String(s.name||'').toLowerCase().includes(search));
  if (progFilter) base = base.filter(s => String(s.program||'').toUpperCase() === progFilter);

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

  selectedSid = sid;
  // Highlight row
  document.querySelectorAll('#apTable tbody tr.cr-row').forEach(r => r.classList.toggle('selected', r.dataset.sid == sid));

  const gpa = s.gpa == null ? '—' : Number(s.gpa).toFixed(2);
  const rs = Number(s.risk_score || 0);
  const riskCls = rs >= 8 ? 'ap-risk-high' : rs >= 4 ? 'ap-risk-mid' : 'ap-risk-low';
  const reasonMap = { low_gpa: T.lowGpa, high_priority_missing: T.hpMissing, zero_current_term_hours: T.zeroHours };
  const reasons = (Array.isArray(s.attention_reasons) ? s.attention_reasons : []).map(r => esc(reasonMap[r]||r)).join(', ') || T.none;
  const hpList = Array.isArray(s.high_priority_missing_courses) ? s.high_priority_missing_courses : [];
  const hpHtml = hpList.length
    ? `<div class="table-wrap" style="max-height:200px;margin-top:0.4rem;"><table class="table table-sm mb-0"><thead><tr><th scope="col">${T.course}</th><th scope="col">${T.score}</th><th scope="col">${T.bucket}</th></tr></thead><tbody>${hpList.map(c=>`<tr><td>${esc(c.course_code||'—')}</td><td>${Number(c.score||0).toFixed(2)}</td><td>${c.bucket==='this_parity'?T.thisParity:T.other}</td></tr>`).join('')}</tbody></table></div>`
    : `<span style="color:var(--muted-light);font-size:0.82rem;">${T.noHpMissing}</span>`;

  const plannerHref = `/planner/?student=${encodeURIComponent(sid)}`;

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

/* Also listen for program filter changes */
q('apProgramFilter').addEventListener('input', debounce(() => { currentPage = 1; apFilter(); updateCsvLink(); }, 250));

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

const USER_ROLE = 'userRole';
const USER_ADVISOR_ID = 'userAdvisorId';

if (USER_ROLE === 'ADVISOR' && USER_ADVISOR_ID) {
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
