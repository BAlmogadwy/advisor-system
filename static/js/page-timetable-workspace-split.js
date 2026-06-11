/* ══════════════════════════════════════════════════════════════════
   Timetable Workspace — Split-Pane Host (direct render)
   ────────────────────────────────────────────────────────────────
   Renders four independent compact workspace panes into one screen.
   Each pane shows a lecture grid + lab grid for the currently
   selected term + group, sized to fit without scroll. Talks to the
   same /ops/tw/ API surface the main page uses for placement
   mutation, board fetch, scenario publish/export/optimise.
   ══════════════════════════════════════════════════════════════════ */

const IS_AR = LANGUAGE_CODE === 'ar';
const CSRF = document.querySelector('[name=csrfmiddlewaretoken]')?.value || djCsrfToken;
const $ = id => document.getElementById(id);

const T = {
  selectScenario: IS_AR ? 'اختر سيناريو' : 'Select scenario',
  selectBoard: IS_AR ? 'اختر لوحة' : 'Select a board',
  publishConfirm: IS_AR ? 'نشر هذا السيناريو؟' : 'Publish this scenario?',
  optimiseConfirm: IS_AR ? 'تشغيل التحسين (بدون إعادة توليد)؟' : 'Run Optimise Current (no regenerate)?',
  noScenario: IS_AR ? 'لا يوجد سيناريو' : 'No scenario selected',
  group: IS_AR ? 'مج' : 'G',
  courses: IS_AR ? 'مقررات' : 'courses',
  placed: IS_AR ? 'موضوعة' : 'Placed',
  clashShort: IS_AR ? 'تعارضات' : 'clash',
  noClash: IS_AR ? 'نظيفة' : 'Clean',
  labs: IS_AR ? 'المعامل' : 'Labs',
  noLab: IS_AR ? 'لا معامل لهذه المجموعة' : 'No labs for this group',
  moveFail: IS_AR ? 'تعذّر النقل' : 'Move failed',
  moveOk: IS_AR ? 'تم النقل' : 'Moved',
};

const DAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU'];
const DAY_LABELS = IS_AR
  ? ['الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس']
  : ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'];

const DEFAULT_LECTURE_SLOTS = [
  { label: '1', start: '09:00', end: '10:15' },
  { label: '2', start: '10:30', end: '11:45' },
  { label: '3', start: '13:00', end: '14:15' },
  { label: '4', start: '14:30', end: '15:45' },
  { label: '5', start: '16:00', end: '17:15' },
];
const DEFAULT_LAB_SLOTS = [
  { label: 'L1', start: '09:00', end: '10:40' },
  { label: 'L2', start: '10:45', end: '12:25' },
  { label: 'L3', start: '13:00', end: '14:40' },
  { label: 'L4', start: '14:45', end: '16:25' },
  { label: 'L5', start: '16:30', end: '18:10' },
];

/* ── State ── */
const S = {
  scenarios: [],
  scenarioId: null,
  scenarioMeta: null,
  scenarioSummary: null,
  boards: [],
  crossBoardClashCount: 0,
  // Up to 16 pane slots (a 4×4 matrix is the max the picker exposes).
  // The current layout (cols × rows) decides how many are visible; the
  // rest carry `hidden` on their DOM node and no boardId.
  panes: Array.from({ length: 16 }, () => ({ boardId: null, group: 0, boardData: null })),
  // Global undo/redo stacks shared across panes, matching the main page's
  // single-stack model. Each action has enough state to fully revert.
  undoStack: [],
  redoStack: [],
  selectedPaneIdx: null,
  selectedPlacementId: null,
  search: '',
  protections: {
    instructors: new Set(),
    rooms: new Set(),
    times: new Set(),
  },
  // Data-driven grid shape. Anything from 1×1 up to 4×4 is allowed;
  // the actual upper bound at runtime is also gated by viewport fit.
  cols: 2,
  rows: 2,
  maximised: null,
  dragSource: null,
  slotAssist: {
    active: false,
    paneIdx: null,
    placementId: null,
    kind: null,
    candidates: new Map(),
    requestToken: 0,
    pendingMove: null,
    deepRepairRun: null,
    deepRepairLoading: false,
  },
  createAssist: {
    active: false,
    payload: null,
    targetPaneIdxs: [],
    candidates: new Map(),
    requestToken: 0,
    loading: false,
  },
  planLens: {
    plans: [],
    courses: {},
    sections: {},
    active: 'ALL',
    loaded: false,
  },
};

// 16 pane labels (A..P) so every visible pane has a stable, readable name
// in the board-bar and status ribbon.
const POS_LABELS = [
  'Pane A', 'Pane B', 'Pane C', 'Pane D',
  'Pane E', 'Pane F', 'Pane G', 'Pane H',
  'Pane I', 'Pane J', 'Pane K', 'Pane L',
  'Pane M', 'Pane N', 'Pane O', 'Pane P',
];

/* Number of panes currently visible = cols × rows. */
function paneCount() {
  return S.cols * S.rows;
}

// Minimum pane footprint for readable cell content. Matches the CSS
// grid measurements (5-day ruler + min 80px cell + padding).
const MIN_PANE_W = 440;
const MIN_PANE_H = 240;
// Chrome reserved for .tws-body side panels + .tws-quad gaps/padding.
const QUAD_CHROME_X = 16; // 6px padding × 2 + a few px gap buffer
const QUAD_CHROME_Y = 16;
// Vertical space consumed by topbar + boards-bar + ctrls + status below .tws-quad.
const TWS_CHROME_Y = 112;

/* Maximum cols/rows that fit the current viewport without squishing cells. */
function viewportMaxCols() {
  const body = document.getElementById('twsBody');
  const sb = body && body.classList.contains('sb-open') ? 240 : 0;
  const rp = body && body.classList.contains('rp-open') ? 300 : 0;
  const avail = window.innerWidth - sb - rp - QUAD_CHROME_X;
  return Math.max(1, Math.min(4, Math.floor(avail / MIN_PANE_W)));
}
function viewportMaxRows() {
  const avail = window.innerHeight - TWS_CHROME_Y - QUAD_CHROME_Y;
  return Math.max(1, Math.min(4, Math.floor(avail / MIN_PANE_H)));
}

// Right inspector panel state
const RP = {
  open: false,
  tab: 'issues',
  capacity: {}, // boardId -> capacity response
  studentBlockers: {
    token: 0,
    scenarioId: null,
    data: null,
    activeCourse: null,
  },
  fixQueue: {
    token: 0,
    cache: {},
    items: [],
  },
  builder: {
    token: 0,
    resolverToken: 0,
    readiness: null,
    actions: [],
    activeAction: null,
    resolver: null,
    roomCache: {},
    studentCache: {},
  },
  repair: {
    token: 0,
    placementId: null,
    run: null,
    mode: 'conservative',
    moveScope: 'single_session',
    blockedIdsText: '',
    busy: false,
  },
  globalRepair: {
    plan: null,
    busy: false,
    mode: 'conservative',
    maxPlacements: 8,
    maxSolverSeconds: 5,
    courseKeysText: '',
  },
};

// Bottom panel state
const BP = {
  open: false,
  tab: 'demand',
};

// Left sidebar state
const SB = {
  open: false,
  budget: [],
  search: '',
};

/* ── API helpers ── */
async function api(url, opts = {}) {
  const o = Object.assign({ credentials: 'same-origin', headers: {} }, opts);
  if (opts.method && opts.method !== 'GET') {
    o.headers['X-CSRFToken'] = CSRF;
  }
  try {
    const r = await fetch(url, o);
    if (!r.ok) {
      let msg = `${r.status}`;
      try { const d = await r.json(); msg = d.error?.message || d.error || d.message || msg; } catch {}
      console.error('api error', url, msg);
      return null;
    }
    return await r.json();
  } catch (e) {
    console.error(e);
    return null;
  }
}

/* ── Utilities ── */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function normaliseCourseCode(code) {
  return String(code || '').replace(/\u00a0/g, ' ').trim().toUpperCase().replace(/\s+/g, '');
}

function courseKeyOf(item) {
  return String(item?.course_key || item?.course_code || '').trim();
}

function activePlanFilter() {
  return S.planLens.active || 'ALL';
}

function sectionLensForPlacement(placement) {
  const sid = placement?.term_section_id == null ? '' : String(placement.term_section_id);
  return S.planLens.sections[sid] || null;
}

function courseLensForItem(item) {
  return S.planLens.courses[courseKeyOf(item)] || null;
}

function itemMatchesPlanLens(item) {
  const filter = activePlanFilter();
  if (filter === 'ALL') return true;
  const sectionLens = sectionLensForPlacement(item);
  if (sectionLens) {
    if (filter === 'SHARED') return !!sectionLens.shared || sectionLens.role === 'shared';
    return (sectionLens.filter_plans || []).includes(filter);
  }
  const courseLens = courseLensForItem(item);
  if (!courseLens) return true;
  if (filter === 'SHARED') return !!courseLens.shared_overflow;
  const plans = courseLens.plans || {};
  const allocation = courseLens.allocation || {};
  return Number(plans[filter] || 0) > 0 || Number(allocation[filter] || 0) > 0;
}

function activeSplitSearch() {
  return (S.search || '').trim().toUpperCase();
}

function splitPlacementSearchText(placement) {
  const lens = sectionLensForPlacement(placement);
  const lensBits = lens
    ? [lens.owner, lens.owner_label, lens.role, ...(lens.filter_plans || [])].filter(Boolean).join(' ')
    : '';
  const meetingBits = (placement?.meetings || [])
    .map(m => [m.instructor, m.room, m.day, m.start_time, m.end_time].filter(Boolean).join(' '))
    .join(' ');
  return [
    placement?.course_code,
    placement?.course_key,
    placement?.course_name,
    placement?.section,
    placement?.department,
    placement?.day,
    placement?.start_time,
    placement?.end_time,
    placement?.room,
    placement?.instructor,
    meetingBits,
    lensBits,
  ].filter(Boolean).join(' ').toUpperCase();
}

function itemMatchesSplitSearch(item) {
  const q = activeSplitSearch();
  return !q || splitPlacementSearchText(item).includes(q);
}

function budgetMatchesSplitSearch(item, search) {
  if (!search) return true;
  const courseLens = courseLensForItem(item);
  const lensBits = courseLens
    ? Object.entries(courseLens.plans || {})
        .filter(([, count]) => Number(count) > 0)
        .map(([plan, count]) => `${plan} ${count}`)
        .join(' ')
    : '';
  return [
    item?.course_code,
    item?.course_key,
    item?.course_name,
    item?.department,
    item?.programme_term,
    lensBits,
  ].filter(v => v != null).join(' ').toUpperCase().includes(search);
}

function refreshSplitSearch() {
  const q = activeSplitSearch();
  let total = 0;
  let matched = 0;
  S.panes.slice(0, paneCount()).forEach(p => {
    const placements = p.boardData?.placements || [];
    total += placements.length;
    matched += placements.filter(itemMatchesSplitSearch).length;
  });
  const input = $('twsSearch');
  if (input) {
    input.title = q ? `${matched}/${total} timetable matches` : (IS_AR ? 'Ø¨Ø­Ø« ÙÙŠ Ø§Ù„Ø¬Ø¯ÙˆÙ„' : 'Search timetable');
  }
  const status = $('twsStatusHover');
  if (status) {
    status.textContent = q
      ? `Search ${matched}/${total}: ${S.search}`
      : 'Search cleared';
  }
}

function planLensLabelForPlacement(placement) {
  const lens = sectionLensForPlacement(placement);
  if (!lens) return '';
  if (lens.shared) return 'Shared';
  if (lens.owner && lens.owner !== 'UNALLOCATED') return lens.owner;
  return '';
}

function planLensActualEntries(placement) {
  const lens = sectionLensForPlacement(placement);
  if (!lens) return [];
  const counts = lens.actual_plans || {};
  return Object.entries(counts)
    .filter(([, count]) => Number(count) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]) || String(a[0]).localeCompare(String(b[0])));
}

function planLensPlanLabels(placement) {
  const labels = planLensActualEntries(placement).map(([plan]) => String(plan));
  if (labels.length) return labels;
  const label = planLensLabelForPlacement(placement);
  return label ? [label] : [];
}

function planLensCountsText(placement) {
  return planLensActualEntries(placement)
    .map(([plan, count]) => `${plan} ${count}`)
    .join(' · ');
}

function planLensDetailsFieldsHtml(placement, fieldClass = 'tws-sel-field') {
  const lens = sectionLensForPlacement(placement);
  if (!lens) return '';
  const labels = planLensPlanLabels(placement);
  const plans = labels.length ? labels.join(' · ') : '—';
  const counts = planLensCountsText(placement) || '—';
  const total = Number(lens.actual_total || 0);
  const countText = total > 0 && counts !== '—' ? `${counts} · total ${total}` : counts;
  return `
    <div class="${fieldClass}"><span class="label">Plans</span><span class="value">${esc(plans)}</span></div>
    <div class="${fieldClass}"><span class="label">Plan counts</span><span class="value mono">${esc(countText)}</span></div>
  `;
}

function planLensBadgesHtml(placement) {
  const lens = sectionLensForPlacement(placement);
  if (!lens) return '';
  const labels = planLensPlanLabels(placement);
  const label = labels.length ? labels.slice(0, 3).join(' ') : planLensLabelForPlacement(placement);
  if (!label) return '';
  const cls = lens.shared ? ' shared' : '';
  const title = `${labels.length ? labels.join(', ') : label}${lens.role ? ` | ${lens.role}` : ''}`;
  return `<span class="plan-badges${cls}" title="${esc(title)}"><span>${esc(label)}</span></span>`;
}

function renderPlanLensControls() {
  const wrap = $('twsPlanLens');
  if (!wrap) return;
  const plans = S.planLens.plans || [];
  const active = activePlanFilter();
  const buttons = ['ALL']
    .concat(plans)
    .concat(['SHARED'])
    .map(plan => {
      const label = plan === 'ALL' ? 'All' : plan === 'SHARED' ? 'Shared' : plan;
      return `<button type="button" class="tws-plan-chip${active === plan ? ' on' : ''}" data-plan="${esc(plan)}">${esc(label)}</button>`;
    })
    .join('');
  wrap.innerHTML = buttons;
  wrap.querySelectorAll('[data-plan]').forEach(btn => {
    btn.addEventListener('click', () => {
      S.planLens.active = btn.dataset.plan || 'ALL';
      renderPlanLensControls();
      for (let i = 0; i < paneCount(); i++) renderPane(i);
      if (SB.open) renderSidebar();
      const label = btn.textContent || 'All';
      $('twsStatusHover').textContent = `Plan lens ${label}`;
    });
  });
}

function resetPlanLens() {
  S.planLens = { plans: [], courses: {}, sections: {}, active: 'ALL', loaded: false };
  renderPlanLensControls();
}

async function loadPlanLens({ rerender = false } = {}) {
  if (!S.scenarioId) {
    resetPlanLens();
    return;
  }
  const data = await api(`/ops/tw/scenarios/${S.scenarioId}/plan-lens/`);
  if (!data || !data.plan_lens) {
    S.planLens.loaded = false;
    renderPlanLensControls();
    return;
  }
  const active = S.planLens.active || 'ALL';
  S.planLens = Object.assign({ plans: [], courses: {}, sections: {}, active, loaded: true }, data.plan_lens);
  if (S.planLens.active !== 'ALL' && !S.planLens.plans.includes(S.planLens.active) && S.planLens.active !== 'SHARED') {
    S.planLens.active = 'ALL';
  }
  renderPlanLensControls();
  if (rerender) {
    for (let i = 0; i < paneCount(); i++) renderPane(i);
    if (SB.open) renderSidebar();
  }
}

// 20 distinct mid-dark course colours — tuned for a dark workspace so
// they distinguish courses at a glance without looking washed-out like
// the main page's light pastels. Each is ~35 % saturation / 40 % lightness
// with a complementary brighter accent for the left border stripe.
const COURSE_COLORS = [
  ['#3D5A80', '#6D94C4'],  // slate blue
  ['#456B52', '#72A687'],  // forest
  ['#8C4A4A', '#C47A7A'],  // dusty red
  ['#8C7A3D', '#C4A86D'],  // mustard
  ['#5C3D73', '#8C6DA6'],  // plum
  ['#3D6B52', '#6DA687'],  // emerald
  ['#8C6B3D', '#C4A06D'],  // bronze
  ['#3D7385', '#6DA8C0'],  // teal blue
  ['#8A5F3F', '#C49070'],  // terracotta
  ['#3D7368', '#6DA89C'],  // dark teal
  ['#6B3D73', '#A86DB3'],  // violet
  ['#8A4A66', '#C47A96'],  // rose pink
  ['#3D734A', '#6DA87A'],  // pine
  ['#73583D', '#A88A70'],  // olive brown
  ['#3D4F73', '#6D85A8'],  // dark navy
  ['#734A3D', '#A87A70'],  // brown
  ['#3D7352', '#6DA887'],  // emerald alt
  ['#73513D', '#A88570'],  // sienna
  ['#553D73', '#8A6DA8'],  // deep purple
  ['#4C5F73', '#7E93A8'],  // slate
];
const _courseColorMap = {};
function courseColorKey(code) {
  return normaliseCourseCode(code) || '__UNKNOWN__';
}
function courseColor(code) {
  const key = courseColorKey(code);
  if (!_courseColorMap[key]) {
    const idx = Object.keys(_courseColorMap).length % COURSE_COLORS.length;
    _courseColorMap[key] = COURSE_COLORS[idx];
  }
  return _courseColorMap[key][0];
}
function courseColorBorder(code) {
  const key = courseColorKey(code);
  if (!_courseColorMap[key]) courseColor(key);
  return _courseColorMap[key][1];
}
function isLabPlacement(p) {
  if (!p || !p.start_time || !p.end_time) return false;
  const toMin = t => {
    const parts = String(t).split(':');
    const h = Number(parts[0]), m = Number(parts[1]);
    if (!Number.isFinite(h) || !Number.isFinite(m)) return NaN;
    return h * 60 + m;
  };
  const diff = toMin(p.end_time) - toMin(p.start_time);
  return Number.isFinite(diff) && diff > 80;
}

function toMinutes(t) {
  const parts = String(t || '').split(':');
  const h = Number(parts[0]), m = Number(parts[1]);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return NaN;
  return h * 60 + m;
}

function durationMinutes(start, end) {
  const a = toMinutes(start), b = toMinutes(end);
  return Number.isFinite(a) && Number.isFinite(b) ? b - a : 0;
}

function timeOverlaps(aStart, aEnd, bStart, bEnd) {
  const a0 = toMinutes(aStart), a1 = toMinutes(aEnd);
  const b0 = toMinutes(bStart), b1 = toMinutes(bEnd);
  if (![a0, a1, b0, b1].every(Number.isFinite)) return false;
  return a0 < b1 && b0 < a1;
}

function placementInstructor(p) {
  return ((p && p.meetings && p.meetings[0]) ? p.meetings[0].instructor : '') || '';
}

function sectionRank(section) {
  const m = String(section || '').match(/\d+/);
  return m ? Number(m[0]) : 999;
}

function slotAssistKey(kind, day, start) {
  return `${normaliseSlotKind(kind)}:${day}:${start}`;
}

function normaliseSlotKind(kind) {
  const value = String(kind || '').trim().toLowerCase();
  return value.startsWith('lab') ? 'lab' : 'lect';
}

function slotKindForPlacement(placement) {
  return isLabPlacement(placement) ? 'lab' : 'lect';
}

function placementShortLabel(placement) {
  return `${placement.course_code || ''} ${placement.section || ''}`.trim();
}

function protectionKey() {
  return `twsSplitProtections:${S.scenarioId || 'none'}`;
}

function protectionValue(value) {
  return String(value || '').trim().toUpperCase();
}

function timeProtectionKey(day, start) {
  return `${protectionValue(day)}:${String(start || '').trim()}`;
}

function resetProtections() {
  S.protections = { instructors: new Set(), rooms: new Set(), times: new Set() };
}

function loadProtections() {
  resetProtections();
  if (!S.scenarioId) return;
  try {
    const raw = localStorage.getItem(protectionKey());
    if (!raw) return;
    const data = JSON.parse(raw);
    S.protections.instructors = new Set((data.instructors || []).map(protectionValue).filter(Boolean));
    S.protections.rooms = new Set((data.rooms || []).map(protectionValue).filter(Boolean));
    S.protections.times = new Set((data.times || []).map(String).filter(Boolean));
  } catch {
    resetProtections();
  }
}

function saveProtections() {
  if (!S.scenarioId) return;
  localStorage.setItem(protectionKey(), JSON.stringify({
    instructors: Array.from(S.protections.instructors),
    rooms: Array.from(S.protections.rooms),
    times: Array.from(S.protections.times),
  }));
}

function toggleProtection(kind, value) {
  const set = S.protections[kind];
  const key = kind === 'times' ? String(value || '') : protectionValue(value);
  if (!set || !key) return false;
  if (set.has(key)) {
    set.delete(key);
    saveProtections();
    return false;
  }
  set.add(key);
  saveProtections();
  return true;
}

function placementProtectionReasons(placement) {
  if (!placement) return [];
  const reasons = [];
  const instructor = protectionValue(placementInstructor(placement));
  const room = protectionValue(placement.room);
  const time = timeProtectionKey(placement.day, placement.start_time);
  if (placement.is_locked) reasons.push(IS_AR ? 'مقفلة' : 'locked section');
  if (instructor && S.protections.instructors.has(instructor)) reasons.push(`${IS_AR ? 'مدرس محمي' : 'protected instructor'} ${placementInstructor(placement)}`);
  if (room && room !== 'UNASSIGNED' && S.protections.rooms.has(room)) reasons.push(`${IS_AR ? 'قاعة محمية' : 'protected room'} ${placement.room}`);
  if (S.protections.times.has(time)) reasons.push(`${IS_AR ? 'وقت محمي' : 'protected time'} ${placement.day} ${placement.start_time}`);
  return reasons;
}

function isPlacementProtected(placement) {
  return placementProtectionReasons(placement).length > 0;
}

function slotPoolForPlacement(boardData, placement) {
  const kind = slotKindForPlacement(placement);
  const base = kind === 'lab'
    ? ((boardData.lab_slot_config && boardData.lab_slot_config.length) ? boardData.lab_slot_config : DEFAULT_LAB_SLOTS)
    : ((boardData.slot_config && boardData.slot_config.length) ? boardData.slot_config : DEFAULT_LECTURE_SLOTS);
  const duration = durationMinutes(placement.start_time, placement.end_time);
  const matching = base.filter(slot => Math.abs(durationMinutes(slot.start, slot.end) - duration) <= 5);
  return matching.length ? matching : base;
}

function courseCompanionForSplitMove(boardData, placement) {
  const instructor = placementInstructor(placement).trim().toUpperCase();
  const courseCode = normaliseCourseCode(placement.course_code);
  const rank = sectionRank(placement.section);
  const kind = slotKindForPlacement(placement);
  return (boardData.placements || [])
    .filter(p => String(p.id) !== String(placement.id))
    .filter(p => normaliseCourseCode(p.course_code) === courseCode)
    .filter(p => String(p.section || '') !== String(placement.section || ''))
    .filter(p => slotKindForPlacement(p) === kind)
    .sort((a, b) => {
      const ai = placementInstructor(a).trim().toUpperCase();
      const bi = placementInstructor(b).trim().toUpperCase();
      const aSameInstructor = instructor && ai === instructor ? 0 : 1;
      const bSameInstructor = instructor && bi === instructor ? 0 : 1;
      return aSameInstructor - bSameInstructor ||
        Math.abs(sectionRank(a.section) - rank) - Math.abs(sectionRank(b.section) - rank) ||
        sectionRank(a.section) - sectionRank(b.section) ||
        String(a.section || '').localeCompare(String(b.section || ''));
    })[0] || null;
}

function classifySplitTargets(boardData, targets) {
  const movingIds = new Set(targets.map(t => String(t.placement.id)));
  const seen = new Set();
  const evidence = [];
  let critical = 0;
  let warning = 0;

  function add(kind, a, b) {
    const key = `${kind}:${[a, b].map(String).sort().join(':')}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }

  function compare(target, other) {
    const otherPlacement = other.placement || other;
    if (String(target.day) !== String(other.day)) return;
    if (!timeOverlaps(target.start, target.end, other.start_time || other.start, other.end_time || other.end)) return;

    const otherId = otherPlacement.id || `target-${otherPlacement.course_code}-${otherPlacement.section}`;
    const targetName = placementShortLabel(target.placement);
    const otherName = placementShortLabel(otherPlacement);
    const otherSlot = `${other.day || otherPlacement.day} ${other.start_time || other.start}-${other.end_time || other.end}`;

    if (add('time', target.placement.id, otherId)) {
      critical += 1;
      evidence.push(`${otherName} already uses ${otherSlot}`);
    }

    const instructor = placementInstructor(target.placement).trim().toUpperCase();
    const otherInstructor = placementInstructor(otherPlacement).trim().toUpperCase();
    if (instructor && otherInstructor && instructor === otherInstructor && add('instructor', target.placement.id, otherId)) {
      critical += 1;
      evidence.push(`${placementInstructor(target.placement)} teaches ${targetName} and ${otherName}`);
    }

    const room = String(target.placement.room || '').trim().toUpperCase();
    const otherRoom = String(other.room || otherPlacement.room || '').trim().toUpperCase();
    if (room && otherRoom && room !== 'UNASSIGNED' && room === otherRoom && add('room', target.placement.id, otherId)) {
      warning += 1;
      evidence.push(`${target.placement.room} is already occupied`);
    }
  }

  targets.forEach(target => {
    (boardData.placements || []).forEach(other => {
      if (movingIds.has(String(other.id))) return;
      compare(target, other);
    });
  });

  targets.forEach((target, idx) => {
    targets.slice(idx + 1).forEach(other => {
      compare(target, {
        id: other.placement.id,
        placement: other.placement,
        day: other.day,
        start_time: other.start,
        end_time: other.end,
        room: other.placement.room,
      });
    });
  });

  return { critical, warning, evidence: evidence.slice(0, 3) };
}

function splitPairOption(boardData, placement, day, slot, slots) {
  const companion = courseCompanionForSplitMove(boardData, placement);
  if (!companion) return null;
  const idx = slots.findIndex(s => s.start === slot.start && s.end === slot.end);
  if (idx < 0) return null;
  const selectedRank = sectionRank(placement.section);
  const companionRank = sectionRank(companion.section);
  const naturalRelation = companionRank >= selectedRank ? 'after' : 'before';
  const options = [
    { relation: 'after', slot: slots[idx + 1] },
    { relation: 'before', slot: slots[idx - 1] },
  ].filter(option => option.slot);

  return options.map(option => {
    const transition = option.relation === 'after'
      ? toMinutes(option.slot.start) - toMinutes(slot.end)
      : toMinutes(slot.start) - toMinutes(option.slot.end);
    if (transition < 0 || transition > 15) return null;
    const targets = [
      { placement, day, start: slot.start, end: slot.end },
      { placement: companion, day, start: option.slot.start, end: option.slot.end },
    ];
    const result = classifySplitTargets(boardData, targets);
    const preferred = option.relation === naturalRelation;
    const score = (result.critical * 1000) + (result.warning * 120) + (preferred ? -35 : 0) + transition;
    return {
      companion,
      relation: option.relation,
      start: option.slot.start,
      end: option.slot.end,
      critical: result.critical,
      warning: result.warning,
      transition,
      preferred,
      score,
    };
  }).filter(Boolean).sort((a, b) => a.score - b.score)[0] || null;
}

function splitSlotScore(candidate, placement) {
  return (candidate.critical * 1000) +
    (candidate.warning * 120) +
    candidateQualityPenalty(candidate) +
    (candidate.pair && !candidate.pair.critical ? -35 : 0) +
    (String(candidate.day) === String(placement.day) ? 0 : 12) +
    Math.round(Math.abs(toMinutes(candidate.start) - toMinutes(placement.start_time)) / 10);
}

function candidateOutcome(candidate) {
  return candidate?.student_outcome || null;
}

function candidateExactRepair(candidate) {
  return candidate?.exact_repair || candidate?.metrics?.exact_repair || null;
}

function candidateHasDeepRepair(candidate) {
  return !!candidate?.deepRepair;
}

function slotAssistExactRepairLoading(candidate = null) {
  if (!S.slotAssist.deepRepairLoading) return false;
  if (!candidate) return true;
  return !candidateHasDeepRepair(candidate) && !candidateExactRepair(candidate)?.enabled;
}

function candidateExactRepairSolved(candidate) {
  const exact = candidateExactRepair(candidate);
  if (!exact?.enabled) return false;
  const status = String(candidate?.solver_status || exact.solver_status || '').toLowerCase();
  const candidateStatus = String(candidate?.status || '').toLowerCase();
  return ['optimal', 'feasible'].includes(status)
    && (!candidateStatus || candidateStatus === 'feasible');
}

function exactRepairRankKey(candidate) {
  const exact = candidateExactRepair(candidate);
  const ranking = candidate?.metrics?.ranking || {};
  if (Array.isArray(ranking.rank_key) && ranking.rank_key.length) {
    return ranking.rank_key.map(value => Number.isFinite(Number(value)) ? Number(value) : String(value));
  }
  if (!exact?.enabled) return [1, 999999, 999999, 0, 0, 999999, 999999, 999999];
  const solved = candidateExactRepairSolved(candidate) ? 0 : 1;
  return [
    solved,
    Number(exact.existing_lost || 0),
    Number(exact.unresolved_blocked || 0),
    -Number(exact.blocked_recovered || 0),
    -Number(exact.requested_courses_recovered || 0),
    Number(exact.students_moved || 0),
    Number(exact.section_changes || 0),
    Number(exact.timetable_quality?.penalty || 0),
  ];
}

function candidateQualityPenalty(candidate) {
  if (!candidate) return 0;
  const direct = candidate.timetable_quality?.penalty ?? candidate.quality_score;
  if (direct != null) return Number(direct || 0);
  return Number(candidate.student_outcome?.after?.quality_penalty || 0);
}

function candidateQualitySignal(candidate) {
  const quality = candidate?.timetable_quality || {};
  const reasons = Array.isArray(quality.reasons) ? quality.reasons : [];
  const top = reasons.find(row => Number(row.penalty || 0) > 0);
  if (!top) return '';
  return `${String(top.label || top.component || 'Quality')} ${Number(top.penalty || 0)}`;
}

function compareScoreLists(a = [], b = []) {
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i += 1) {
    const av = Number(a[i] ?? 999999);
    const bv = Number(b[i] ?? 999999);
    if (av !== bv) return av - bv;
  }
  return 0;
}

function compareRankLists(a = [], b = []) {
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i += 1) {
    const av = a[i] ?? 999999;
    const bv = b[i] ?? 999999;
    if (typeof av === 'number' && typeof bv === 'number') {
      if (av !== bv) return av - bv;
    } else {
      const cmp = String(av).localeCompare(String(bv));
      if (cmp) return cmp;
    }
  }
  return 0;
}

function compareOutcomeCandidates(a, b) {
  const ae = candidateExactRepair(a);
  const be = candidateExactRepair(b);
  if (ae || be || candidateHasDeepRepair(a) || candidateHasDeepRepair(b)) {
    const exactCompare = compareRankLists(exactRepairRankKey(a), exactRepairRankKey(b));
    if (exactCompare) return exactCompare;
    const ar = Number(a?.score_rank || 999999);
    const br = Number(b?.score_rank || 999999);
    if (ar !== br) return ar - br;
  }
  const ao = candidateOutcome(a);
  const bo = candidateOutcome(b);
  if (ao || bo) {
    const scoreCompare = compareScoreLists(ao?.after?.score || [], bo?.after?.score || []);
    if (scoreCompare) return scoreCompare;
    const qualityCompare = candidateQualityPenalty(a) - candidateQualityPenalty(b);
    if (qualityCompare) return qualityCompare;
    const unresolvedCompare = Number(ao?.unresolved_course_delta ?? 999999) - Number(bo?.unresolved_course_delta ?? 999999);
    if (unresolvedCompare) return unresolvedCompare;
    const blockedCompare = Number(ao?.blocked_students_delta ?? 999999) - Number(bo?.blocked_students_delta ?? 999999);
    if (blockedCompare) return blockedCompare;
  }
  return (a.rank || 999) - (b.rank || 999) || (a.score || 0) - (b.score || 0);
}

function candidateOutcomeSignal(candidate) {
  const exact = candidateExactRepair(candidate);
  if (exact?.enabled) {
    const lost = Number(exact.existing_lost || 0);
    const recovered = Number(exact.blocked_recovered || 0);
    const requested = Number(exact.requested_courses_recovered || 0);
    const unresolved = Number(exact.unresolved_blocked || 0);
    if (lost > 0) return `Would lose ${lost} existing course${lost === 1 ? '' : 's'}`;
    if (!candidateExactRepairSolved(candidate)) return 'No exact repair solution';
    if (recovered > 0) return `${recovered} blocked student${recovered === 1 ? '' : 's'} recovered`;
    if (unresolved > 0) return `${unresolved} blocked student${unresolved === 1 ? '' : 's'} remain`;
    if (requested > 0) return `${requested} requested course${requested === 1 ? '' : 's'} served`;
    return 'Exact repair: no change';
  }
  if (candidateHasDeepRepair(candidate)) return 'No exact repair solution';
  if (slotAssistExactRepairLoading(candidate)) return 'Exact student repair running';
  return '';
}

function candidateOutcomeImpact(candidate) {
  const outcome = candidateOutcome(candidate);
  if (!outcome) return '';
  const before = outcome.before || {};
  const after = outcome.after || {};
  const bits = [
    `blocked ${Number(before.blocked_students || 0)}->${Number(after.blocked_students || 0)}`,
    `courses ${Number(before.unresolved_courses || 0)}->${Number(after.unresolved_courses || 0)}`,
  ];
  const allClashDelta = Number(outcome.all_clash_delta || 0);
  const mixedDelta = Number(outcome.mixed_blockers_delta || 0);
  if (allClashDelta) bits.push(`all_clash ${Number(before.all_clash || 0)}->${Number(after.all_clash || 0)}`);
  if (mixedDelta) bits.push(`mixed ${Number(before.mixed_blockers || 0)}->${Number(after.mixed_blockers || 0)}`);
  const improved = Number(outcome.improved_student_count || 0);
  const worsened = Number(outcome.worsened_student_count || 0);
  if (improved || worsened) bits.push(`students ${improved} better/${worsened} worse`);
  return bits.join(' | ');
}

function candidateRegressionReason(candidate) {
  if (!candidate) return 'missing candidate';
  const exact = candidateExactRepair(candidate);
  if (exact?.enabled) {
    const lost = Number(exact.existing_lost || 0);
    if (lost > 0) return `would lose ${lost} existing course${lost === 1 ? '' : 's'}`;
    if (!candidateExactRepairSolved(candidate)) return 'no exact repair solution';
    return '';
  }
  if (candidateHasDeepRepair(candidate)) return 'no exact repair solution';
  const pairCritical = Number(candidate.pair?.critical || 0);
  if (pairCritical > 0) return `adds ${pairCritical} pair conflict${pairCritical === 1 ? '' : 's'}`;
  const outcome = candidateOutcome(candidate);
  if (outcome) {
    const clashDelta = Number(outcome.actual_clash_delta || 0);
    const unresolvedDelta = Number(outcome.unresolved_course_delta || 0);
    const blockedDelta = Number(outcome.blocked_students_delta || 0);
    const worsened = Number(outcome.worsened_student_count || 0);
    if (clashDelta > 0) return `adds ${clashDelta} real student clash${clashDelta === 1 ? '' : 'es'}`;
    if (unresolvedDelta > 0) return `adds ${unresolvedDelta} blocked course request${unresolvedDelta === 1 ? '' : 's'}`;
    if (blockedDelta > 0) return `adds ${blockedDelta} blocked student${blockedDelta === 1 ? '' : 's'}`;
    if (worsened > 0) return `worsens ${worsened} student${worsened === 1 ? '' : 's'}`;
  }
  const criticalDelta = Number(candidate.critical_improvement || 0);
  if (criticalDelta < 0) return `adds ${Math.abs(criticalDelta)} critical issue${criticalDelta === -1 ? '' : 's'}`;
  if (candidate?.student_outcome_tone === 'worsens') return 'worsens student outcome';
  if (candidateHasEvidence(candidate, ['instructor', 'same_course'])) return 'creates a hard local conflict';
  if (slotAssistTone(candidate) === 'avoid') return 'keeps a critical target conflict';
  return '';
}

function candidateIsHardRegression(candidate) {
  return !!candidateRegressionReason(candidate);
}

function candidateIsValidatedSafe(candidate) {
  if (!candidate || candidateIsHardRegression(candidate)) return false;
  if (slotAssistExactRepairLoading(candidate)) return false;
  if (Number(candidate.critical_count || 0) > 0) return false;
  const exact = candidateExactRepair(candidate);
  if (exact?.enabled) {
    return candidateExactRepairSolved(candidate)
      && Number(exact.existing_lost || 0) === 0
      && (
        Number(exact.blocked_recovered || 0) > 0
        || Number(exact.requested_courses_recovered || 0) > 0
      );
  }
  if (candidateHasDeepRepair(candidate)) return false;
  const outcome = candidateOutcome(candidate);
  if (!outcome) return false;
  const clashDelta = Number(outcome.actual_clash_delta || 0);
  const unresolvedDelta = Number(outcome.unresolved_course_delta || 0);
  const blockedDelta = Number(outcome.blocked_students_delta || 0);
  const worsened = Number(outcome.worsened_student_count || 0);
  if (clashDelta > 0 || unresolvedDelta > 0 || blockedDelta > 0 || worsened > 0) return false;
  return clashDelta < 0
    || unresolvedDelta < 0
    || blockedDelta < 0
    || Number(outcome.improved_student_count || 0) > 0;
}

function candidateHasUsefulImprovement(candidate) {
  if (!candidate) return false;
  if (slotAssistExactRepairLoading(candidate)) return false;
  const exact = candidateExactRepair(candidate);
  if (exact?.enabled) {
    return candidateExactRepairSolved(candidate)
      && Number(exact.existing_lost || 0) === 0
      && (
        Number(exact.blocked_recovered || 0) > 0
        || Number(exact.requested_courses_recovered || 0) > 0
      );
  }
  if (candidateHasDeepRepair(candidate)) return false;
  const outcome = candidateOutcome(candidate);
  if (outcome) {
    if (Number(outcome.actual_clash_delta || 0) < 0) return true;
    if (Number(outcome.unresolved_course_delta || 0) < 0) return true;
    if (Number(outcome.blocked_students_delta || 0) < 0) return true;
    if (Number(outcome.improved_student_count || 0) > 0) return true;
  }
  return Number(candidate.impact_improvement || 0) > 0
    || Number(candidate.student_improvement || 0) > 0
    || Number(candidate.critical_improvement || 0) > 0
    || Number(candidate.warning_improvement || 0) > 0;
}

function quickFixCandidateRank(candidate) {
  const outcome = candidateOutcome(candidate) || {};
  return (
    (canBundleCandidate(candidate) ? 160000 : 0)
    + Math.max(0, -Number(outcome.actual_clash_delta || 0)) * 6000
    + Math.max(0, -Number(outcome.unresolved_course_delta || 0)) * 1400
    + Math.max(0, -Number(outcome.blocked_students_delta || 0)) * 350
    + Number(outcome.improved_student_count || 0) * 25
    + Number(candidate.impact_improvement || 0) * 1000
    + Number(candidate.student_improvement || 0) * 100
    + Number(candidate.critical_improvement || 0) * 100
    + Number(candidate.warning_improvement || 0) * 10
    - Number(candidate.score || 0)
  );
}

function compareQuickFixCandidates(a, b) {
  return quickFixCandidateRank(b) - quickFixCandidateRank(a)
    || compareOutcomeCandidates(a, b);
}

function buildSplitSlotCandidates(paneIdx, placement) {
  const boardData = S.panes[paneIdx]?.boardData;
  if (!boardData || !placement) return [];
  const kind = slotKindForPlacement(placement);
  const slots = slotPoolForPlacement(boardData, placement);
  const rows = [];
  DAYS.forEach(day => {
    slots.forEach(slot => {
      if (String(day) === String(placement.day) && String(slot.start) === String(placement.start_time)) return;
      const result = classifySplitTargets(boardData, [
        { placement, day, start: slot.start, end: slot.end },
      ]);
      const pair = splitPairOption(boardData, placement, day, slot, slots);
      if (pair) {
        const pairReasons = placementProtectionReasons(pair.companion);
        if (pairReasons.length) pair.protected = pairReasons;
      }
      const candidate = {
        kind,
        day,
        start: slot.start,
        end: slot.end,
        critical: result.critical,
        warning: result.warning,
        evidence: result.evidence,
        pair,
      };
      candidate.score = splitSlotScore(candidate, placement);
      rows.push(candidate);
    });
  });
  rows.sort((a, b) => a.score - b.score || DAYS.indexOf(a.day) - DAYS.indexOf(b.day) || toMinutes(a.start) - toMinutes(b.start));
  rows.forEach((row, idx) => { row.rank = idx + 1; });
  return rows;
}

function splitSlotLabel(candidate) {
  if (!candidate) return '';
  const outcomeSignal = candidateOutcomeSignal(candidate);
  if (outcomeSignal) return outcomeSignal;
  if (candidate.pair && !candidate.pair.protected) return 'Bundle';
  const improvement = Number(candidate.impact_improvement || 0);
  const savedStudents = Number(candidate.student_improvement || 0);
  if (improvement > 0 && savedStudents > 0) return `Save ${savedStudents}`;
  if (improvement > 0) return 'Improve';
  if (candidate.badge) return candidate.badge;
  if (candidate.rank === 1) return candidate.critical ? 'Least bad' : 'Best';
  const students = Number(candidate.student_affected_count || candidate.studentAffected || 0);
  if (students) return `${students} students`;
  if (candidate.critical) return `${candidate.critical} conflict`;
  if (candidate.warning) return `${candidate.warning} warning`;
  return 'Clean';
}

function candidateImpactSummary(candidate) {
  const exact = candidateExactRepair(candidate);
  if (exact?.enabled) {
    const bits = [];
    bits.push(`exact repair recovered ${Number(exact.blocked_recovered || 0)}`);
    bits.push(`unresolved ${Number(exact.unresolved_blocked || 0)}`);
    bits.push(`lost ${Number(exact.existing_lost || 0)}`);
    bits.push(`moved ${Number(exact.students_moved || 0)}`);
    const cascade = exact.cascade || {};
    if (cascade.requires_multi_course_cascade) {
      bits.push(`cascade ${Number(cascade.touched_course_count || 0)} courses`);
    }
    return bits.join(' | ');
  }
  if (candidateHasDeepRepair(candidate)) {
    const status = candidate.status || 'not_solved';
    const solver = candidate.solver_status || 'not_run';
    return `exact repair ${status} (${solver})`;
  }
  if (slotAssistExactRepairLoading(candidate)) return 'exact student repair running';
  if (!candidate || candidate.current_impact_score == null) return '';
  const outcomeImpact = candidateOutcomeImpact(candidate);
  const beforeStudents = Number(candidate.current_student_affected_count || 0);
  const afterStudents = Number(candidate.student_affected_count || candidate.studentAffected || 0);
  const studentDelta = Number(candidate.student_improvement || (beforeStudents - afterStudents));
  const beforeCritical = Number(candidate.current_critical_count || 0);
  const afterCritical = Number(candidate.critical_count || candidate.critical || 0);
  const criticalDelta = Number(candidate.critical_improvement || (beforeCritical - afterCritical));
  const beforeWarning = Number(candidate.current_warning_count || 0);
  const afterWarning = Number(candidate.warning_count || candidate.warning || 0);
  const warningDelta = Number(candidate.warning_improvement || (beforeWarning - afterWarning));
  const bits = [];
  if (outcomeImpact) bits.push(`outcome ${outcomeImpact}`);
  const qualitySignal = candidateQualitySignal(candidate);
  if (qualitySignal) bits.push(qualitySignal);
  bits.push(`cross risk ${beforeStudents}->${afterStudents}`);
  if (studentDelta > 0) bits.push(`save ${studentDelta}`);
  else if (studentDelta < 0) bits.push(`${Math.abs(studentDelta)} more`);
  if (criticalDelta > 0) bits.push(`critical ${beforeCritical}->${afterCritical}`);
  else if (criticalDelta < 0) bits.push(`+${Math.abs(criticalDelta)} critical`);
  if (warningDelta > 0) bits.push(`warnings ${beforeWarning}->${afterWarning}`);
  return bits.join(' | ');
}

function candidateEvidenceList(candidate) {
  const rows = Array.isArray(candidate?.evidence) ? candidate.evidence : [];
  return rows
    .map(item => {
      if (typeof item === 'string') {
        return { kind: 'note', tone: 'note', title: item, detail: '', student_count: 0 };
      }
      if (!item || typeof item !== 'object') return null;
      return {
        ...item,
        kind: String(item.kind || 'note').toLowerCase(),
        tone: String(item.tone || '').toLowerCase(),
        title: String(item.title || ''),
        detail: String(item.detail || ''),
        student_count: Number(item.student_count || item.studentCount || 0),
      };
    })
    .filter(Boolean);
}

function candidateEvidenceText(evidence) {
  if (!evidence) return '';
  return `${evidence.title || ''}${evidence.detail ? ` | ${evidence.detail}` : ''}`.trim();
}

function candidateRankingReasons(candidate) {
  const rows = Array.isArray(candidate?.ranking_reasons) ? candidate.ranking_reasons : [];
  return rows
    .map(item => {
      if (!item || typeof item !== 'object') return null;
      return {
        ...item,
        kind: String(item.kind || 'note').toLowerCase(),
        tone: String(item.tone || 'note').toLowerCase(),
        title: String(item.title || ''),
        detail: String(item.detail || ''),
        penalty: Number(item.penalty || 0),
        student_count: Number(item.student_count || item.studentCount || 0),
      };
    })
    .filter(Boolean);
}

function candidatePrimaryReason(candidate) {
  if (candidate?.primary_reason) return String(candidate.primary_reason);
  const reason = candidateRankingReasons(candidate)[0];
  if (reason) return candidateEvidenceText(reason);
  return candidateEvidenceText(candidatePrimaryEvidence(candidate));
}

function candidateReasonToneClass(reason) {
  const tone = String(reason?.tone || '').toLowerCase();
  if (tone === 'good' || tone === 'clean') return ' good';
  if (tone === 'critical' || tone === 'avoid') return ' muted';
  return '';
}

function candidateReasonChipsHtml(candidate, limit = 3) {
  const reasons = candidateRankingReasons(candidate).slice(0, limit);
  if (!reasons.length) return '';
  return `<div class="tws-repair-reasons">${reasons.map(reason => {
    const label = reason.kind === 'quality' && reason.penalty
      ? `${reason.title} +${Number(reason.penalty)}`
      : reason.title;
    return `<span class="tws-repair-chip${candidateReasonToneClass(reason)}">${esc(label || reason.kind)}</span>`;
  }).join('')}</div>`;
}

function candidateHasEvidence(candidate, kinds) {
  const wanted = new Set(kinds);
  return candidateEvidenceList(candidate).some(e => wanted.has(e.kind));
}

function candidatePrimaryEvidence(candidate) {
  const rows = candidateEvidenceList(candidate);
  if (!rows.length) return null;
  const riskWeight = { critical: 0, warning: 1, note: 2 };
  const kindWeight = {
    cross_board_room: 0,
    room: 1,
    students: 2,
    cross_board_students: 3,
    instructor: 4,
    same_course: 5,
    note: 9,
  };
  return [...rows].sort((a, b) => {
    const aRisk = riskWeight[a.tone] ?? 3;
    const bRisk = riskWeight[b.tone] ?? 3;
    return aRisk - bRisk || (kindWeight[a.kind] ?? 8) - (kindWeight[b.kind] ?? 8);
  })[0];
}

function candidateSignalTags(candidate) {
  if (!candidate) return [];
  const rows = candidateEvidenceList(candidate);
  const tags = [];
  const add = value => {
    if (value && !tags.includes(value)) tags.push(value);
  };
  const studentCount = Number(candidate.student_affected_count || candidate.studentAffected || 0)
    || rows.reduce((sum, row) => sum + Number(row.student_count || 0), 0);
  const outcomeSignal = candidateOutcomeSignal(candidate);
  const qualitySignal = candidateQualitySignal(candidate);
  if (outcomeSignal) add(outcomeSignal);
  if (qualitySignal) add(qualitySignal);
  if (candidateHasEvidence(candidate, ['cross_board_room', 'room'])) add('Room busy');
  if (studentCount > 0) add(`Students ${studentCount}`);
  if (candidateHasEvidence(candidate, ['instructor'])) add('Instructor');
  if (candidateHasEvidence(candidate, ['same_course'])) add('Same course');
  if (candidate.pair) add(candidate.pair.protected ? 'Pair blocked' : 'Bundle');
  if (!tags.length && (candidate.critical || candidate.critical_count)) add(`${candidate.critical || candidate.critical_count} issue`);
  if (!tags.length && (candidate.warning || candidate.warning_count)) add(`${candidate.warning || candidate.warning_count} warn`);
  return tags;
}

function candidateSlotSignalLine(candidate, tone) {
  const outcomeSignal = candidateOutcomeSignal(candidate);
  if (outcomeSignal) return outcomeSignal;
  const tags = candidateSignalTags(candidate);
  if (tags.length) return tags.slice(0, 2).join(' | ');
  if (candidate?.pair) {
    return `${candidate.pair.companion.section || 'Pair'} ${candidate.pair.relation} ${candidate.pair.start}`;
  }
  if (tone === 'avoid') return `${candidate.critical || candidate.critical_count || 1} issue`;
  if (tone === 'risky') return `${candidate.warning || candidate.warning_count || 1} warn`;
  return '0 students';
}

function candidateGridBadge(candidate, tone) {
  if (!candidate) return '';
  const exact = candidateExactRepair(candidate);
  if (exact?.enabled) {
    const lost = Number(exact.existing_lost || 0);
    const recovered = Number(exact.blocked_recovered || 0);
    const unresolved = Number(exact.unresolved_blocked || 0);
    const moved = Number(exact.students_moved || 0);
    if (lost > 0) return `Lost ${lost}`;
    if (!candidateExactRepairSolved(candidate)) return 'No solve';
    if (recovered > 0) return `Repair ${recovered}`;
    if (unresolved > 0) return `Unres ${unresolved}`;
    if (moved > 0) return `Move ${moved}`;
    return 'Exact';
  }
  if (candidateHasDeepRepair(candidate)) {
    return String(candidate.status || '').toLowerCase() === 'rejected_before_solver' ? 'Reject' : 'No solve';
  }
  if (slotAssistExactRepairLoading(candidate)) return 'Solving';
  const outcome = candidateOutcome(candidate);
  if (outcome) {
    if (tone === 'avoid' && candidateHasEvidence(candidate, ['instructor'])) return 'Inst';
    if (tone === 'avoid' && candidateHasEvidence(candidate, ['same_course'])) return 'Course';
    if (tone === 'avoid' && candidateHasEvidence(candidate, ['cross_board_room', 'room'])) return 'Room';
    return tone === 'avoid' ? 'Check' : tone === 'risky' ? 'Pending' : 'Ready';
  }
  if (canBundleCandidate(candidate)) return '+1';
  if (candidate?.pair?.protected?.length) return 'Pair';
  const savedStudents = Number(candidate.student_improvement || 0);
  if (savedStudents > 0) return `-${savedStudents}`;
  if (tone === 'clean') return candidate.rank === 1 ? 'Best' : 'OK';
  if (candidateHasEvidence(candidate, ['cross_board_room', 'room'])) return 'Room';
  if (candidateHasEvidence(candidate, ['instructor'])) return 'Inst';
  const students = Number(candidate.student_affected_count || candidate.studentAffected || 0);
  if (students || candidateHasEvidence(candidate, ['students', 'cross_board_students'])) return 'Stu';
  if (tone === 'avoid') return 'Block';
  return 'Risk';
}

function renderSlotAssistSummary(pane, placement, candidates, opts = {}) {
  if (!pane || !placement) return;
  pane.querySelector('.tws-assist-summary')?.remove();
  const exactLoading = slotAssistExactRepairLoading();
  const actionable = candidates.filter(candidate => {
    const cell = slotCellForCandidate(S.slotAssist.paneIdx, candidate, candidate.kind || S.slotAssist.kind);
    return cell && !cell.classList.contains('filled');
  });
  const hasOutcome = !exactLoading && actionable.some(candidate => candidateExactRepair(candidate)?.enabled);
  const counts = actionable.reduce((acc, candidate) => {
    const tone = slotAssistTone(candidate);
    acc[tone] += 1;
    return acc;
  }, { clean: 0, risky: 0, avoid: 0 });
  const best = actionable.find(candidate => slotAssistTone(candidate) === 'clean') || actionable[0] || candidates[0];
  const bestText = exactLoading
    ? `Exact student repair running for ${actionable.length} candidate slot${actionable.length === 1 ? '' : 's'}`
    : (best ? `${best.day} ${best.start}-${best.end}` : 'none');
  const detail = !exactLoading && best && canBundleCandidate(best)
    ? `+ ${placementShortLabel(best.pair.companion)}`
    : '';
  const summary = document.createElement('div');
  summary.className = `tws-assist-summary${exactLoading ? ' loading' : ''}`;
  summary.setAttribute('role', 'status');
  const cleanLabel = exactLoading ? 'Done' : (hasOutcome ? 'Help' : 'OK');
  const riskyLabel = exactLoading ? 'Solving' : (hasOutcome ? 'Neutral' : 'Risk');
  const avoidLabel = exactLoading ? 'Rejected' : (hasOutcome ? 'Worse' : 'Block');
  const cleanCount = exactLoading ? 0 : counts.clean;
  const riskyCount = exactLoading ? actionable.length : counts.risky;
  const avoidCount = exactLoading ? 0 : counts.avoid;
  summary.innerHTML = `
    <div class="tws-assist-main">
      <b>${esc(placementShortLabel(placement))}</b>
      <span>${esc(bestText)}${detail ? ` · ${esc(detail)}` : ''}</span>
    </div>
    <div class="tws-assist-counts" aria-label="${esc(IS_AR ? 'ملخص النقلات' : 'Move summary')}">
      <span class="clean"><em>${cleanLabel}</em>${cleanCount}</span>
      <span class="risky"><em>${riskyLabel}</em>${riskyCount}</span>
      <span class="avoid"><em>${avoidLabel}</em>${avoidCount}</span>
    </div>
    <button type="button" class="tws-assist-clear" aria-label="${esc(IS_AR ? 'إلغاء التحديد' : 'Clear selection')}" title="${esc(IS_AR ? 'إلغاء التحديد' : 'Clear selection')}">×</button>
  `;
  summary.querySelector('.tws-assist-clear')?.addEventListener('click', (e) => {
    e.stopPropagation();
    clearPlacementSelection();
  });
  pane.querySelector('.pane-body')?.before(summary);
}

function createPayloadFromBudgetRow(b, usedOverride = null) {
  const code = String(b?.course_code || '').trim().toUpperCase();
  const used = Number.isFinite(usedOverride) ? usedOverride : parseInt(b?.used_sections || '0', 10);
  return {
    type: 'create_planned',
    course_code: code,
    course_key: courseKeyOf(b) || code,
    course_name: b?.course_name || code,
    section_label: `S${Number.isFinite(used) ? used + 1 : 1}`,
    department: b?.department || '',
    credit_hours: b?.credit_hours || 0,
    max_per_section: b?.max_per_section || 40,
    total_students: b?.total_demand || 0,
    programme_term: b?.programme_term ?? null,
    planned_sections: b?.planned_sections || 0,
    used_sections: Number.isFinite(used) ? used : 0,
    requires_full_section_pattern: !!b?.requires_full_section_pattern,
    required_meetings_per_section: b?.required_meetings_per_section || 1,
  };
}

function createAssistLabel(payload) {
  return `${payload?.course_code || 'Course'} ${payload?.section_label || ''}`.trim();
}

function createAssistCandidateKey(candidate) {
  if (!candidate) return '';
  if (candidate.candidate_id) return `${candidate.paneIdx}:${candidate.candidate_id}`;
  return `${candidate.paneIdx}:${slotAssistKey(candidate.kind || 'lect', candidate.day, candidate.start)}`;
}

function createAssistTone(candidate) {
  const tone = candidate?.tone;
  if (tone === 'avoid' || tone === 'critical') return 'avoid';
  if (tone === 'risky' || tone === 'watch') return 'risky';
  if (candidate?.critical_count || candidate?.critical) return 'avoid';
  if (candidate?.warning_count || candidate?.warning || candidate?.student_affected_count) return 'risky';
  return 'clean';
}

function compareCreateAssistCandidates(a, b) {
  const at = createAssistTone(a);
  const bt = createAssistTone(b);
  const toneRank = { clean: 0, risky: 1, avoid: 2 };
  return (toneRank[at] || 0) - (toneRank[bt] || 0)
    || Number(a.score || 0) - Number(b.score || 0)
    || candidateQualityPenalty(a) - candidateQualityPenalty(b)
    || Number(a.paneIdx || 0) - Number(b.paneIdx || 0)
    || DAYS.indexOf(String(a.day || 'SUN')) - DAYS.indexOf(String(b.day || 'SUN'))
    || toMinutes(a.start) - toMinutes(b.start)
    || String(a.kind || '').localeCompare(String(b.kind || ''));
}

function createAssistDetail(candidate) {
  if (!candidate) return '';
  const reasonText = candidatePrimaryReason(candidate);
  const evidenceText = candidateEvidenceText(candidatePrimaryEvidence(candidate));
  const tags = candidateSignalTags(candidate).slice(0, 4);
  const affected = Number(candidate.student_affected_count || 0);
  const base = candidate.critical_count
    ? (reasonText || evidenceText || `${candidate.critical_count} critical conflict(s)`)
    : candidate.warning_count
      ? (reasonText || evidenceText || `${candidate.warning_count} warning(s)`)
      : affected
        ? (reasonText || `${affected} affected student(s)`)
        : (reasonText || 'Clean target');
  return [base, tags.length ? tags.join(', ') : ''].filter(Boolean).join(' | ');
}

function createAssistCandidateMeetings(candidate) {
  const rows = Array.isArray(candidate?.meetings) && candidate.meetings.length
    ? candidate.meetings
    : candidate
      ? [{ kind: candidate.kind || 'lect', day: candidate.day, start: candidate.start, end: candidate.end }]
      : [];
  return rows
    .map(row => ({
      kind: normaliseSlotKind(row.kind || candidate?.kind || 'lect'),
      day: String(row.day || '').toUpperCase(),
      start: String(row.start || row.start_time || ''),
      end: String(row.end || row.end_time || ''),
    }))
    .filter(row => row.day && row.start && row.end);
}

function createAssistTargetLabel(candidate) {
  if (!candidate) return '';
  const pane = POS_LABELS[Number(candidate.paneIdx || 0)] || `Pane ${Number(candidate.paneIdx || 0) + 1}`;
  const board = candidate.boardLabel || S.panes[candidate.paneIdx]?.boardData?.board?.label || '';
  const meetings = createAssistCandidateMeetings(candidate);
  if (meetings.length > 1) {
    const pattern = meetings.map(m => `${m.day} ${m.start}`).join(', ');
    return `${pane}${board ? ` - ${board}` : ''} - Pattern ${pattern}`;
  }
  const kind = normaliseSlotKind(candidate.kind) === 'lab' ? 'Lab' : 'Slot';
  return `${pane}${board ? ` · ${board}` : ''} · ${kind} ${candidate.day} ${candidate.start}`;
}

function createAssistTargetPanes(payload) {
  const visible = [];
  for (let i = 0; i < paneCount(); i += 1) {
    const pane = S.panes[i];
    if (!pane?.boardId || !pane.boardData) continue;
    visible.push({ idx: i, pane });
  }
  const term = payload?.programme_term == null ? '' : String(payload.programme_term);
  const matchingTerm = term
    ? visible.filter(item => String(item.pane.boardData?.board?.nominal_term || '') === term)
    : [];
  return matchingTerm.length ? matchingTerm : visible;
}

function renderCreateSectionSummary(targetPane, payload, highlighted, candidates = []) {
  if (!targetPane || !payload) return;
  targetPane.querySelector('.tws-create-assist-summary')?.remove();
  const sorted = (candidates || [])
    .filter(createAssistCandidateIsVisibleAndEmpty)
    .slice()
    .sort(compareCreateAssistCandidates);
  const counts = sorted.reduce((acc, candidate) => {
    acc[createAssistTone(candidate)] += 1;
    return acc;
  }, { clean: 0, risky: 0, avoid: 0 });
  const topTargets = sorted.slice(0, 6);
  const summary = document.createElement('div');
  summary.className = 'tws-assist-summary tws-create-assist-summary';
  summary.setAttribute('role', 'status');
  summary.innerHTML = `
    <div class="tws-assist-main">
      <b>${esc(createAssistLabel(payload))}</b>
      <span>${esc(highlighted ? `${highlighted} empty target slot(s)` : 'No empty target slots visible')}</span>
    </div>
    <div class="tws-assist-counts" aria-label="${esc(IS_AR ? 'ملخص وضع الشعبة' : 'Section placement summary')}">
      <span class="clean"><em>${IS_AR ? 'ضع' : 'Place'}</em>${highlighted}</span>
    </div>
    <button type="button" class="tws-assist-clear" aria-label="${esc(IS_AR ? 'إلغاء' : 'Clear placement mode')}" title="${esc(IS_AR ? 'إلغاء' : 'Clear placement mode')}">×</button>
  `;
  const targetText = S.createAssist.loading
    ? 'Finding best target slots...'
    : topTargets.length
      ? `${topTargets.length} ranked target(s), ${counts.clean} clean`
      : highlighted
        ? `${highlighted} empty fallback target(s)`
        : 'No empty target slots visible';
  const mainText = summary.querySelector('.tws-assist-main span');
  if (mainText) mainText.textContent = targetText;
  const countsBox = summary.querySelector('.tws-assist-counts');
  if (countsBox && topTargets.length) {
    countsBox.innerHTML = `
      <span class="clean"><em>Clean</em>${counts.clean}</span>
      <span class="risky"><em>Risk</em>${counts.risky}</span>
      <span class="avoid"><em>Avoid</em>${counts.avoid}</span>
    `;
  }
  if (topTargets.length) {
    const targetsEl = document.createElement('div');
    targetsEl.className = 'tws-create-targets';
    targetsEl.innerHTML = topTargets.map((candidate, idx) => {
      const tone = createAssistTone(candidate);
      const key = createAssistCandidateKey(candidate);
      return `<button type="button" class="tws-create-target ${tone}" data-create-target-key="${esc(key)}" ${tone === 'avoid' ? 'disabled' : ''} title="${esc(createAssistDetail(candidate))}">
        <span>${idx + 1}</span>${esc(createAssistTargetLabel(candidate))}
      </button>`;
    }).join('');
    summary.appendChild(targetsEl);
    targetsEl.querySelectorAll('.tws-create-target[data-create-target-key]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const candidate = S.createAssist.candidates.get(btn.dataset.createTargetKey || '');
        if (!candidate || createAssistTone(candidate) === 'avoid') return;
        btn.disabled = true;
        await applyCreateAssistCandidate(candidate);
      });
    });

    const best = topTargets.find(candidate => createAssistTone(candidate) !== 'avoid') || topTargets[0];
    const primaryReason = candidatePrimaryReason(best);
    const diagnostics = document.createElement('div');
    diagnostics.className = 'tws-create-diagnostics';
    diagnostics.innerHTML = `
      <div><b>Why this target</b><span>${esc(primaryReason || createAssistDetail(best))}</span></div>
      ${candidateReasonChipsHtml(best, 4)}
    `;
    summary.appendChild(diagnostics);
  }
  summary.querySelector('.tws-assist-clear')?.addEventListener('click', (e) => {
    e.stopPropagation();
    clearCreateSectionAssist({ announce: true });
  });
  targetPane.querySelector('.pane-body')?.before(summary);
}

function paintCreateSectionTargets() {
  if (!S.createAssist.active || !S.createAssist.payload) return;
  const payload = S.createAssist.payload;
  clearCreateSectionAssistDecorations();
  const targets = createAssistTargetPanes(payload);
  S.createAssist.targetPaneIdxs = targets.map(item => item.idx);
  const label = createAssistLabel(payload);
  let highlighted = 0;
  let firstPaneEl = null;
  const rankedCandidates = Array.from(S.createAssist.candidates.values()).sort(compareCreateAssistCandidates);
  targets.forEach(({ idx }) => {
    const pane = paneEl(idx);
    if (!pane) return;
    if (!firstPaneEl) firstPaneEl = pane;
    pane.classList.add('assist-active', 'create-assist-active');
    if (rankedCandidates.length || payload.requires_full_section_pattern) return;
    pane.querySelectorAll('.block-grid .cell').forEach(cell => {
      if (cell.classList.contains('filled')) return;
      if (paneHasPlacementAtSlot(idx, slotKindForCell(cell), cell.dataset.day, cell.dataset.start)) return;
      highlighted += 1;
      cell.dataset.createAssist = '1';
      cell.classList.add('assist-clean', 'assist-create');
      if (highlighted === 1) cell.classList.add('assist-best');
      cell.title = `${IS_AR ? 'ضع' : 'Place'} ${label} | ${cell.dataset.day} ${cell.dataset.start}-${cell.dataset.end}`;
      if (highlighted <= 10) {
        const badge = document.createElement('span');
        badge.className = 'slot-assist-badge';
        badge.textContent = IS_AR ? 'ضع' : 'Place';
        cell.appendChild(badge);
      }
    });
  });
  if (rankedCandidates.length) {
    rankedCandidates.forEach(candidate => {
      const pane = paneEl(candidate.paneIdx);
      if (!pane) return;
      const cell = slotCellForCandidate(candidate.paneIdx, candidate, candidate.kind || 'lect');
      if (!cell || !createAssistCandidateIsVisibleAndEmpty(candidate)) return;
      highlighted += 1;
      const tone = createAssistTone(candidate);
      const key = createAssistCandidateKey(candidate);
      cell.dataset.createAssist = '1';
      cell.dataset.createAssistKey = key;
      cell.classList.add('assist-create');
      if (tone === 'avoid') cell.classList.add('assist-avoid');
      else if (tone === 'risky') cell.classList.add('assist-risky');
      else cell.classList.add('assist-clean');
      if (highlighted === 1 && tone !== 'avoid') cell.classList.add('assist-best');
      if (candidateHasEvidence(candidate, ['students', 'cross_board_students'])) cell.classList.add('assist-student');
      if (candidateHasEvidence(candidate, ['instructor', 'same_course'])) cell.classList.add('assist-instructor');
      cell.title = `${IS_AR ? 'Ø¶Ø¹' : 'Place'} ${label} | ${createAssistTargetLabel(candidate)} | ${createAssistDetail(candidate)}`;
      if (highlighted <= 12) {
        const badge = document.createElement('span');
        badge.className = 'slot-assist-badge';
        const meetingCount = createAssistCandidateMeetings(candidate).length;
        badge.textContent = meetingCount > 1 ? `1/${meetingCount}` : candidateGridBadge(candidate, tone);
        cell.appendChild(badge);
      }
      createAssistCandidateCells(candidate).slice(1).forEach((extraCell, meetingIdx) => {
        extraCell.dataset.createAssist = '1';
        extraCell.dataset.createAssistKey = key;
        extraCell.classList.add('assist-create');
        if (tone === 'avoid') extraCell.classList.add('assist-avoid');
        else if (tone === 'risky') extraCell.classList.add('assist-risky');
        else extraCell.classList.add('assist-clean');
        extraCell.title = cell.title;
        const badge = document.createElement('span');
        badge.className = 'slot-assist-badge';
        badge.textContent = `${meetingIdx + 2}/${createAssistCandidateMeetings(candidate).length}`;
        extraCell.appendChild(badge);
      });
    });
  }
  renderCreateSectionSummary(firstPaneEl, payload, highlighted, rankedCandidates);
  const key = payload.course_key || payload.course_code;
  if (key) {
    const escapedKey = window.CSS?.escape ? CSS.escape(key) : String(key).replace(/"/g, '\\"');
    document.querySelector(`.tws-sec-item[data-key="${escapedKey}"]`)?.classList.add('create-active');
  }
  const status = $('twsStatusHover');
  if (status) {
    status.textContent = highlighted
      ? `${IS_AR ? 'اختر خانة فارغة لوضع' : 'Click a highlighted empty slot to place'} ${label}`
      : `${label}: ${IS_AR ? 'لا توجد خانات فارغة ظاهرة' : 'no empty target slots visible'}`;
    const best = rankedCandidates.find(candidate => {
      const cell = slotCellForCandidate(candidate.paneIdx, candidate, candidate.kind || 'lect');
      return cell && !cell.classList.contains('filled') && createAssistTone(candidate) !== 'avoid';
    });
    if (S.createAssist.loading) {
      status.textContent = `${IS_AR ? 'جاري حساب أفضل الأهداف' : 'Finding best targets for'} ${label}...`;
    } else if (best) {
      status.textContent = `${IS_AR ? 'أفضل هدف' : 'Best target'} ${label}: ${createAssistTargetLabel(best)} | ${createAssistDetail(best)}`;
    }
  }
}

function createAssistPreviewParams(payload) {
  const params = new URLSearchParams({
    course_code: payload.course_code || '',
    course_key: payload.course_key || payload.course_code || '',
    section_label: payload.section_label || 'S1',
    limit: '50',
  });
  if (payload.credit_hours) params.set('credit_hours', String(payload.credit_hours));
  if (payload.max_per_section) params.set('max_per_section', String(payload.max_per_section));
  if (payload.kind) params.set('kind', String(payload.kind));
  return params;
}

async function refreshCreateSectionCandidates(token) {
  if (!S.createAssist.active || !S.createAssist.payload) return;
  const payload = S.createAssist.payload;
  const targets = createAssistTargetPanes(payload);
  const params = createAssistPreviewParams(payload);
  const rows = [];
  const messages = [];
  await Promise.all(targets.map(async ({ idx, pane }) => {
    const boardId = pane.boardId;
    if (!boardId) return;
    const data = await api(`/ops/tw/boards/${boardId}/planned-slot-candidates/?${params.toString()}`);
    if (!data) return;
    if (data.message && data.status !== 'ready') messages.push(data.message);
    (data.candidates || []).forEach(candidate => {
      rows.push({
        ...candidate,
        paneIdx: idx,
        boardId,
        boardLabel: data.board?.label || pane.boardData?.board?.label || '',
      });
    });
  }));
  if (!S.createAssist.active || token !== S.createAssist.requestToken) return;
  S.createAssist.loading = false;
  S.createAssist.candidates = new Map();
  rows.sort(compareCreateAssistCandidates).forEach(candidate => {
    S.createAssist.candidates.set(createAssistCandidateKey(candidate), candidate);
  });
  paintCreateSectionTargets();
  if (!rows.length && messages.length) notify.warning(messages[0]);
}

async function applyCreateAssistCandidate(candidate) {
  if (!candidate || !S.createAssist.active || !S.createAssist.payload) return null;
  const cell = slotCellForCandidate(candidate.paneIdx, candidate, candidate.kind || 'lect');
  if (!cell || !createAssistCandidateIsVisibleAndEmpty(candidate)) {
    notify.warning(IS_AR ? 'اختر خانة فارغة' : 'Choose an empty slot');
    return null;
  }
  document.querySelectorAll('.tws-pane .cell.assist-preview').forEach(el => el.classList.remove('assist-preview'));
  createAssistCandidateCells(candidate).forEach(targetCell => targetCell.classList.add('assist-preview'));
  const data = await createPlannedPlacementFromCandidate(candidate, S.createAssist.payload);
  if (data) clearCreateSectionAssist();
  return data;
}

function beginCreateSectionAssist(payload) {
  if (!payload?.course_code) return false;
  clearPlacementSelection({ announce: false });
  clearSlotAssist();
  S.createAssist.active = true;
  S.createAssist.payload = payload;
  S.createAssist.targetPaneIdxs = [];
  S.createAssist.candidates = new Map();
  S.createAssist.loading = true;
  const token = ++S.createAssist.requestToken;
  paintCreateSectionTargets();
  refreshCreateSectionCandidates(token);
  return true;
}

function splitSlotDetail(candidate) {
  if (!candidate) return '';
  const students = Number(candidate.student_affected_count || candidate.studentAffected || 0);
  const impact = candidateImpactSummary(candidate);
  const evidenceText = candidateEvidenceText(candidatePrimaryEvidence(candidate));
  const tags = candidateSignalTags(candidate);
  const signals = tags.length ? `signals: ${tags.join(', ')}` : '';
  const main = students
    ? `${students} affected students${evidenceText ? ` | ${evidenceText}` : ''}`
    : candidate.critical
      ? (evidenceText || `${candidate.critical} conflict`)
      : candidate.warning
        ? (evidenceText || `${candidate.warning} warning`)
        : (candidate.studentAware ? '0 affected students' : 'No local clash');
  const mainWithImpact = [impact, main, signals].filter(Boolean).join(' | ');
  if (!candidate.pair) return mainWithImpact;
  if (candidate.pair.protected) {
    return `${mainWithImpact}; pair blocked (${candidate.pair.protected.join(', ')})`;
  }
  const pairStatus = candidate.pair.critical
    ? `${candidate.pair.critical} pair conflicts`
    : candidate.pair.warning
      ? `${candidate.pair.warning} pair warnings`
      : 'pair clean';
  return `${mainWithImpact}; ${candidate.pair.companion.section || 'pair'} ${candidate.pair.relation} ${candidate.pair.start}-${candidate.pair.end} (${pairStatus})`;
}

function slotAssistTone(candidate) {
  const exact = candidateExactRepair(candidate);
  if (exact?.enabled) {
    if (Number(exact.existing_lost || 0) > 0) return 'avoid';
    if (!candidateExactRepairSolved(candidate)) return 'avoid';
    if (Number(exact.blocked_recovered || 0) > 0) return 'clean';
    if (Number(exact.requested_courses_recovered || 0) > 0) return 'clean';
    return 'risky';
  }
  if (candidateHasDeepRepair(candidate)) return 'avoid';
  if (slotAssistExactRepairLoading(candidate)) return 'risky';
  const outcome = candidateOutcome(candidate);
  if (outcome) {
    const clashDelta = Number(outcome.actual_clash_delta || 0);
    const unresolvedDelta = Number(outcome.unresolved_course_delta || 0);
    const blockedDelta = Number(outcome.blocked_students_delta || 0);
    const worsened = Number(outcome.worsened_student_count || 0);
    if (clashDelta > 0) return 'avoid';
    if (unresolvedDelta > 0 || blockedDelta > 0) return 'avoid';
    if (worsened > 0) return 'risky';
    if (clashDelta < 0 || unresolvedDelta < 0 || blockedDelta < 0) return 'clean';
  }
  if (candidateHasEvidence(candidate, ['instructor', 'same_course'])) return 'avoid';
  const outcomeTone = candidate?.student_outcome_tone;
  if (outcomeTone === 'worsens') return 'avoid';
  if (outcomeTone === 'improves') return 'clean';
  if (outcomeTone === 'stable') return 'risky';
  const tone = candidate?.tone;
  if (tone === 'avoid' || tone === 'critical') return 'avoid';
  if (tone === 'risky' || tone === 'watch') return 'risky';
  if (tone === 'clean' || tone === 'stable') return 'clean';
  if (candidate?.critical) return 'avoid';
  if (candidate?.warning) return 'risky';
  return 'clean';
}

function clearSlotAssistDecorations() {
  document.querySelectorAll('.tws-pane .cell[data-slot-assist-key], .tws-pane .cell[data-slot-assist-title], .tws-pane .cell.assist-clean, .tws-pane .cell.assist-risky, .tws-pane .cell.assist-avoid, .tws-pane .cell.assist-loading, .tws-pane .cell.assist-best, .tws-pane .cell.assist-preview, .tws-pane .cell.assist-room, .tws-pane .cell.assist-student, .tws-pane .cell.assist-instructor').forEach(cell => {
    cell.classList.remove('assist-clean', 'assist-risky', 'assist-avoid', 'assist-loading', 'assist-best', 'assist-preview', 'assist-room', 'assist-student', 'assist-instructor');
    delete cell.dataset.slotAssistKey;
    delete cell.dataset.slotAssistTitle;
    cell.removeAttribute('title');
    cell.querySelectorAll('.slot-assist-badge, .slot-assist-detail').forEach(el => el.remove());
  });
  document.querySelectorAll('.tws-pane.assist-active').forEach(pane => pane.classList.remove('assist-active'));
  document.querySelectorAll('.tws-assist-summary').forEach(el => el.remove());
}

function clearSlotAssist() {
  clearSlotAssistDecorations();
  S.slotAssist.active = false;
  S.slotAssist.paneIdx = null;
  S.slotAssist.placementId = null;
  S.slotAssist.kind = null;
  S.slotAssist.candidates = new Map();
  S.slotAssist.requestToken += 1;
  S.slotAssist.pendingMove = null;
  S.slotAssist.deepRepairRun = null;
  S.slotAssist.deepRepairLoading = false;
}

function clearCreateSectionAssistDecorations() {
  document.querySelectorAll('.tws-pane .cell[data-create-assist], .tws-pane .cell.assist-create').forEach(cell => {
    cell.classList.remove('assist-clean', 'assist-risky', 'assist-avoid', 'assist-best', 'assist-preview', 'assist-create', 'assist-student', 'assist-instructor');
    delete cell.dataset.createAssist;
    delete cell.dataset.createAssistKey;
    cell.removeAttribute('title');
    cell.querySelectorAll('.slot-assist-badge, .slot-assist-detail').forEach(el => el.remove());
  });
  document.querySelectorAll('.tws-pane.create-assist-active').forEach(pane => pane.classList.remove('assist-active', 'create-assist-active'));
  document.querySelectorAll('.tws-create-assist-summary').forEach(el => el.remove());
  document.querySelectorAll('.tws-sec-item.create-active').forEach(el => el.classList.remove('create-active'));
}

function clearCreateSectionAssist({ announce = false } = {}) {
  const wasActive = S.createAssist.active;
  clearCreateSectionAssistDecorations();
  S.createAssist.active = false;
  S.createAssist.payload = null;
  S.createAssist.targetPaneIdxs = [];
  S.createAssist.candidates = new Map();
  S.createAssist.loading = false;
  S.createAssist.requestToken += 1;
  if (announce && wasActive) {
    const status = $('twsStatusHover');
    if (status) status.textContent = IS_AR ? 'تم إلغاء وضع الشعب' : 'Section placement mode cleared';
  }
  return wasActive;
}

function hasActivePlacementSelection() {
  return S.selectedPlacementId != null
    || !!document.querySelector('.tws-pane .cell.selected')
    || S.slotAssist.active
    || S.createAssist.active;
}

function clearPlacementSelection({ announce = true } = {}) {
  const hadSelection = hasActivePlacementSelection();
  document.querySelectorAll('.tws-pane .cell.selected').forEach(cell => cell.classList.remove('selected'));
  S.selectedPlacementId = null;
  S.selectedPaneIdx = null;
  clearSlotAssist();
  clearCreateSectionAssist();
  if (RP.open && RP.tab === 'selection') renderRpanel();
  if (announce && hadSelection) {
    const status = $('twsStatusHover');
    if (status) status.textContent = IS_AR ? 'تم إلغاء التحديد' : 'Selection cleared';
  }
  return hadSelection;
}

function showSlotAssistCellStatus(cell) {
  if (!S.slotAssist.active || !cell?.dataset.slotAssistKey) return;
  const candidate = S.slotAssist.candidates.get(cell.dataset.slotAssistKey);
  if (!candidate) return;
  $('twsStatusHover').textContent = `${splitSlotLabel(candidate)} ${candidate.day} ${candidate.start}-${candidate.end}: ${splitSlotDetail(candidate)}`;
}

function slotCellForCandidate(paneIdx, candidate, kind = null) {
  const pane = paneEl(paneIdx);
  if (!pane || !candidate) return null;
  const slotKind = normaliseSlotKind(kind || candidate.kind || S.slotAssist.kind || 'lect');
  const grid = slotKind === 'lab' ? '.lab-grid' : '.lect-grid';
  return pane.querySelector(`${grid} .cell[data-day="${candidate.day}"][data-start="${candidate.start}"]`)
    || pane.querySelector(`.block-grid .cell[data-day="${candidate.day}"][data-start="${candidate.start}"]`);
}

function createAssistCandidateCells(candidate) {
  if (!candidate) return [];
  return createAssistCandidateMeetings(candidate)
    .map(meeting => slotCellForCandidate(candidate.paneIdx, meeting, meeting.kind))
    .filter(Boolean);
}

function createAssistCandidateIsVisibleAndEmpty(candidate) {
  const meetings = createAssistCandidateMeetings(candidate);
  const cells = createAssistCandidateCells(candidate);
  return meetings.length > 0 && cells.length === meetings.length && cells.every(cell => !cell.classList.contains('filled'));
}

function slotKindForCell(cell) {
  return cell?.closest('.lab-grid') ? 'lab' : 'lect';
}

function paneHasPlacementAtSlot(paneIdx, kind, day, start) {
  const placements = S.panes[paneIdx]?.boardData?.placements || [];
  const slotKind = normaliseSlotKind(kind || 'lect');
  return placements.some(placement =>
    normaliseSlotKind(slotKindForPlacement(placement)) === slotKind
    && String(placement.day || '').toUpperCase() === String(day || '').toUpperCase()
    && String(placement.start_time || '') === String(start || '')
  );
}

function restorePendingMovePreview() {
  const pending = S.slotAssist.pendingMove;
  if (!pending || String(pending.placementId) !== String(S.slotAssist.placementId)) return;
  const cell = slotCellForCandidate(pending.paneIdx, pending, S.slotAssist.kind);
  if (cell && !cell.classList.contains('filled')) {
    cell.classList.add('assist-preview');
    const key = slotAssistKey(S.slotAssist.kind, pending.day, pending.start);
    const candidate = S.slotAssist.candidates.get(key);
    const located = findPlacement(pending.placementId);
    const label = located ? placementShortLabel(located.placement) : 'Selection';
    if (candidate) {
      const pairText = canBundleCandidate(candidate)
        ? ` Bundle ${placementShortLabel(candidate.pair.companion)} ${candidate.pair.relation} ${candidate.pair.start}-${candidate.pair.end}.`
        : '';
      $('twsStatusHover').textContent = `Preview move ${label} -> ${pending.day} ${pending.start}-${pending.end}: ${splitSlotDetail(candidate)}.${pairText} Click same slot again to apply.`;
    }
  }
}

function paintSlotAssistCandidates(paneIdx, placement, candidates, opts = {}) {
  const pane = paneEl(paneIdx);
  pane?.classList.add('assist-active');
  const emptyRanked = new Map();
  let visibleBadgeRank = 0;
  candidates.forEach(candidate => {
    const kind = normaliseSlotKind(candidate.kind || S.slotAssist.kind);
    const key = slotAssistKey(kind, candidate.day, candidate.start);
    S.slotAssist.candidates.set(key, { ...candidate, kind });
    const cell = slotCellForCandidate(paneIdx, candidate, kind);
    if (!cell || cell.classList.contains('filled')) return;
    visibleBadgeRank += 1;
    emptyRanked.set(key, visibleBadgeRank);
  });
  let emptyHighlighted = 0;
  pane?.querySelectorAll('.block-grid .cell').forEach(cell => {
    const kind = slotKindForCell(cell);
    const key = slotAssistKey(kind, cell.dataset.day, cell.dataset.start);
    const candidate = S.slotAssist.candidates.get(key);
    if (!candidate) return;
    cell.dataset.slotAssistKey = key;
    cell.dataset.slotAssistTitle = splitSlotDetail(candidate);
    const exactLoading = slotAssistExactRepairLoading(candidate);
    cell.title = exactLoading
      ? `${candidate.day} ${candidate.start}-${candidate.end} | Exact student repair is running`
      : `${candidate.day} ${candidate.start}-${candidate.end} | ${splitSlotLabel(candidate)} | ${splitSlotDetail(candidate)}`;
    const tone = slotAssistTone(candidate);
    if (tone === 'avoid') cell.classList.add('assist-avoid');
    else if (tone === 'risky') cell.classList.add('assist-risky');
    else cell.classList.add('assist-clean');
    if (exactLoading) cell.classList.add('assist-loading');
    if (candidateHasEvidence(candidate, ['cross_board_room', 'room'])) cell.classList.add('assist-room');
    if (Number(candidate.student_affected_count || candidate.studentAffected || 0) > 0 || candidateHasEvidence(candidate, ['students', 'cross_board_students'])) cell.classList.add('assist-student');
    if (candidateHasEvidence(candidate, ['instructor'])) cell.classList.add('assist-instructor');
    if (!exactLoading && candidate.rank === 1 && !cell.classList.contains('filled')) cell.classList.add('assist-best');
    const badgeRank = emptyRanked.get(key) || 0;
    if (!cell.classList.contains('filled') && badgeRank > 0) {
      emptyHighlighted += 1;
      const badge = document.createElement('span');
      badge.className = 'slot-assist-badge';
      badge.textContent = candidateGridBadge(candidate, tone);
      cell.appendChild(badge);
    }
  });
  renderSlotAssistSummary(pane, placement, candidates, opts);
  const actionable = candidates.filter(c => {
    const cell = slotCellForCandidate(paneIdx, c, c.kind || S.slotAssist.kind);
    return cell && !cell.classList.contains('filled');
  });
  const statRows = actionable.length ? actionable : candidates;
  const clean = statRows.filter(c => slotAssistTone(c) === 'clean').length;
  const affected = candidates.reduce((sum, c) => sum + Number(c.student_affected_count || c.studentAffected || 0), 0);
  const deepRows = candidates.filter(candidateHasDeepRepair);
  const exactRows = deepRows.filter(c => candidateExactRepair(c)?.enabled);
  const outcomeRows = candidates.filter(c => candidateOutcome(c));
  const helps = deepRows.length
    ? exactRows.filter(c => Number(candidateExactRepair(c)?.blocked_recovered || 0) > 0).length
    : outcomeRows.filter(c => c.student_outcome_tone === 'improves').length;
  const worsens = deepRows.length
    ? deepRows.filter(c => Number(candidateExactRepair(c)?.existing_lost || 0) > 0 || !candidateExactRepairSolved(c)).length
    : outcomeRows.filter(c => c.student_outcome_tone === 'worsens').length;
  const bestSafe = actionable.find(candidateIsValidatedSafe);
  const best = bestSafe
    || actionable.find(candidate => slotAssistTone(candidate) === 'clean')
    || actionable.find(candidate => slotAssistTone(candidate) === 'risky')
    || actionable[0]
    || candidates[0];
  const bestText = best ? `${bestSafe ? 'validated ' : 'manual review '}${best.day} ${best.start}-${best.end}` : 'none';
  const pairText = best?.pair ? ` | pair ${best.pair.companion.section || ''} ${best.pair.relation} ${best.pair.start}-${best.pair.end}` : '';
  const exactLoading = slotAssistExactRepairLoading();
  const source = exactLoading
    ? ` | exact repair running for ${actionable.length} slots`
    : deepRows.length
    ? ` | exact repair ${helps} recover / ${worsens} blocked`
    : outcomeRows.length
    ? ''
    : (opts.studentAware ? ` | student-aware ${affected} total pressure` : '');
  const actionText = emptyHighlighted
    ? (exactLoading
      ? 'wait for exact repair results'
      : exactRows.length
      ? 'Click a Repair/Help slot to approve and apply the exact student repair'
      : (opts.sticky ? 'Click a highlighted empty slot to preview; click again to move' : 'drop on a highlighted slot'))
    : 'No empty candidate slots visible in this pane';
  const statusLead = exactLoading
    ? `${opts.sticky ? 'Selected' : 'Dragging'} ${placementShortLabel(placement)} | solving actual student repair`
    : `${opts.sticky ? 'Selected' : 'Dragging'} ${placementShortLabel(placement)} | ${clean} clean | recommended ${bestText}${pairText}`;
  $('twsStatusHover').textContent = `${statusLead}${source} | ${actionText}`;
  restorePendingMovePreview();
}

function normaliseStudentAwareCandidate(row) {
  return {
    ...row,
    studentAware: true,
    critical: Number(row.critical_count || 0),
    warning: Number(row.warning_count || 0),
    studentAffected: Number(row.student_affected_count || 0),
  };
}

function studentOutcomeCandidatePayload(candidates) {
  return candidates.slice(0, 60).map(candidate => {
    const row = {
      kind: candidate.kind || S.slotAssist.kind,
      day: candidate.day,
      start: candidate.start,
      end: candidate.end,
    };
    if (canBundleCandidate(candidate)) {
      row.pair = {
        placement_id: candidate.pair.companion.id,
        day: candidate.day,
        start: candidate.pair.start,
        end: candidate.pair.end,
      };
    }
    return row;
  });
}

function moveOutcomeCandidatePayload(candidate, placement) {
  const row = {
    kind: candidate.kind || slotKindForPlacement(placement),
    day: candidate.day,
    start: candidate.start,
    end: candidate.end,
  };
  if (canBundleCandidate(candidate)) {
    row.pair = {
      placement_id: candidate.pair.companion.id,
      day: candidate.day,
      start: candidate.pair.start,
      end: candidate.pair.end,
    };
  }
  return row;
}

async function enrichMoveCandidate(placementId, placement, candidate) {
  if (!placementId || !placement || !candidate) return candidate;
  try {
    const data = await api(`/ops/tw/placements/${placementId}/student-outcome-candidates/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ candidates: [moveOutcomeCandidatePayload(candidate, placement)] }),
    });
    const row = (data?.candidates || [])[0];
    if (!row) return candidate;
    const normal = normaliseStudentAwareCandidate(row);
    const pair = normal.pair && candidate.pair
      ? { ...candidate.pair, ...normal.pair, companion: candidate.pair.companion || normal.pair.companion }
      : (candidate.pair || normal.pair);
    return { ...candidate, ...normal, pair };
  } catch {
    return candidate;
  }
}

async function refreshStudentAwareSlotAssist(placementId, token, opts = {}) {
  const localCandidates = Array.from(S.slotAssist.candidates.values());
  const data = await api(`/ops/tw/placements/${placementId}/student-outcome-candidates/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ candidates: studentOutcomeCandidatePayload(localCandidates) }),
  });
  if (!data || !S.slotAssist.active || token !== S.slotAssist.requestToken) return;
  if (String(S.slotAssist.placementId) !== String(placementId)) return;
  const located = findPlacement(placementId);
  if (!located) return;
  const placement = located.placement;
  const rows = (data.candidates || []).map(normaliseStudentAwareCandidate);
  if (!rows.length) return;
  rows.forEach(row => {
    const key = slotAssistKey(row.kind || S.slotAssist.kind, row.day, row.start);
    const existing = S.slotAssist.candidates.get(key) || {};
    const pair = row.pair && existing.pair
      ? { ...existing.pair, ...row.pair, companion: existing.pair.companion || row.pair.companion }
      : (row.pair || existing.pair);
    S.slotAssist.candidates.set(key, { ...existing, ...row, pair });
  });
  clearSlotAssistDecorations();
  const candidates = Array.from(S.slotAssist.candidates.values())
    .sort(compareOutcomeCandidates);
  paintSlotAssistCandidates(S.slotAssist.paneIdx, placement, candidates, { ...opts, studentAware: true });
}

function normaliseDeepRepairCandidate(row, runId) {
  const metrics = row?.metrics || {};
  return {
    deepRepair: true,
    deepRepairRunId: runId || '',
    deepRepairCandidateId: row?.candidate_id || '',
    day: row?.day || '',
    start: row?.start_time || row?.start || '',
    end: row?.end_time || row?.end || '',
    room: row?.room || '',
    status: row?.status || '',
    solver_status: row?.solver_status || '',
    score_rank: row?.score_rank || null,
    metrics,
    exact_repair: metrics.exact_repair || {},
    decision: row?.decision || {},
    preflight: row?.preflight || {},
    student_change_count: row?.student_change_count || 0,
    explanation: row?.explanation || {},
    badge: row?.badge || '',
  };
}

async function refreshDeepRepairSlotAssist(placementId, token, opts = {}) {
  const localCandidates = Array.from(S.slotAssist.candidates.values());
  if (!localCandidates.length) return;
  S.slotAssist.deepRepairLoading = true;
  const requestedCandidateCount = Math.min(80, Math.max(1, localCandidates.length));
  let data = null;
  try {
    data = await api('/ops/tw/repair/analyse/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        placement_id: placementId,
        blocked_student_ids: [],
        blocked_requests: [],
        mode: 'conservative',
        move_scope: RP.repair.moveScope || 'single_session',
        active_plan_filter: activePlanFilter(),
        limits: {
          max_candidates: requestedCandidateCount,
          max_solver_seconds: 5,
          max_total_solver_seconds: 45,
        },
      }),
    });
  } catch {
    data = null;
  }
  S.slotAssist.deepRepairLoading = false;
  if (!data || !S.slotAssist.active || token !== S.slotAssist.requestToken || String(S.slotAssist.placementId) !== String(placementId)) {
    if (S.slotAssist.active && token === S.slotAssist.requestToken && String(S.slotAssist.placementId) === String(placementId)) {
      const retryLocated = findPlacement(placementId);
      if (retryLocated) {
        clearSlotAssistDecorations();
        const pendingCandidates = Array.from(S.slotAssist.candidates.values()).sort(compareOutcomeCandidates);
        paintSlotAssistCandidates(S.slotAssist.paneIdx, retryLocated.placement, pendingCandidates, opts);
      }
    }
    return;
  }
  const located = findPlacement(placementId);
  if (!located) return;
  S.slotAssist.deepRepairRun = data;
  const runId = data?.run?.id || '';
  (data.candidates || []).forEach(row => {
    const deep = normaliseDeepRepairCandidate(row, runId);
    if (!deep.day || !deep.start) return;
    const key = slotAssistKey(deep.kind || S.slotAssist.kind, deep.day, deep.start);
    const existing = S.slotAssist.candidates.get(key) || {};
    S.slotAssist.candidates.set(key, { ...existing, ...deep, kind: existing.kind || S.slotAssist.kind });
  });
  clearSlotAssistDecorations();
  const candidates = Array.from(S.slotAssist.candidates.values()).sort(compareOutcomeCandidates);
  paintSlotAssistCandidates(S.slotAssist.paneIdx, located.placement, candidates, {
    ...opts,
    studentAware: true,
    deepRepair: true,
  });
}

function beginSlotAssist(paneIdx, placementId, opts = {}) {
  clearCreateSectionAssist();
  const located = findPlacement(placementId);
  if (!located) return;
  const placement = located.placement;
  const sourcePaneIdx = Number.isFinite(paneIdx) ? paneIdx : located.paneIdx;
  const candidates = buildSplitSlotCandidates(sourcePaneIdx, placement);
  clearSlotAssistDecorations();
  S.slotAssist.active = true;
  S.slotAssist.paneIdx = sourcePaneIdx;
  S.slotAssist.placementId = placementId;
  S.slotAssist.kind = slotKindForPlacement(placement);
  S.slotAssist.requestToken += 1;
  S.slotAssist.pendingMove = null;
  S.slotAssist.deepRepairRun = null;
  S.slotAssist.deepRepairLoading = !!opts.sticky && candidates.length > 0;
  const token = S.slotAssist.requestToken;
  S.slotAssist.candidates = new Map(candidates.map(c => [slotAssistKey(c.kind, c.day, c.start), c]));
  paintSlotAssistCandidates(sourcePaneIdx, placement, candidates, opts);
  if (!opts.sticky) refreshStudentAwareSlotAssist(placementId, token, opts);
  if (opts.sticky) refreshDeepRepairSlotAssist(placementId, token, opts);
}

function applySlotDragHint(cell) {
  if (S.slotAssist.active && cell?.dataset.slotAssistKey) {
    const candidate = S.slotAssist.candidates.get(cell.dataset.slotAssistKey);
    const tone = slotAssistTone(candidate);
    if (tone === 'avoid') cell.classList.add('drop-critical');
    else if (tone === 'risky') cell.classList.add('drop-warning');
    else cell.classList.add('drop-valid');
    showSlotAssistCellStatus(cell);
    return;
  }
  if (cell.classList.contains('filled')) cell.classList.add('drop-warning');
  else cell.classList.add('drop-valid');
}

function groupPlacements(placements) {
  const bySec = {};
  placements.forEach(p => {
    const sec = p.section || 'S1';
    if (!bySec[sec]) bySec[sec] = [];
    bySec[sec].push(p);
  });
  return Object.keys(bySec).sort().map(sec => ({ id: sec, placements: bySec[sec] }));
}

/* ── Scenario loading ── */
async function loadScenarios() {
  const qs = (DEFAULT_YEAR && DEFAULT_TERM)
    ? `?year=${DEFAULT_YEAR}&term=${DEFAULT_TERM}`
    : '';
  const data = await api(`/ops/tw/scenarios/${qs}`);
  if (!data) return;
  S.scenarios = data.scenarios || [];
  const sel = $('twsScenario');
  sel.innerHTML = `<option value="">${T.selectScenario}</option>`;
  S.scenarios.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id; opt.textContent = `${s.name} (${s.status})`;
    sel.appendChild(opt);
  });
  const want = INITIAL_SCENARIO && S.scenarios.find(s => String(s.id) === String(INITIAL_SCENARIO))
    ? INITIAL_SCENARIO
    : (S.scenarios.length > 0 ? String(S.scenarios[0].id) : '');
  if (want) {
    sel.value = want;
    await onScenarioChange();
  }
}

async function onScenarioChange() {
  // Undo/redo stacks are scoped to a scenario's placements; carrying them
  // across scenarios would let an undo mutate a different scenario's data.
  S.undoStack = [];
  S.redoStack = [];
  updateUndoRedoButtons();
  S.selectedPlacementId = null;
  S.selectedPaneIdx = null;
  // Every cached structure belongs to the previous scenario and would
  // paint stale data if left in place — invalidate before loading new.
  RP.capacity = {};
  RP.studentBlockers = {
    token: RP.studentBlockers.token + 1,
    scenarioId: null,
    data: null,
    activeCourse: null,
  };
  if ($('twsRpCountStudents')) $('twsRpCountStudents').textContent = '0';
  RP.fixQueue.cache = {};
  RP.fixQueue.items = [];
  RP.fixQueue.token += 1;
  RP.builder.token += 1;
  RP.builder.resolverToken += 1;
  RP.builder.readiness = null;
  RP.builder.actions = [];
  RP.builder.activeAction = null;
  RP.builder.resolver = null;
  RP.builder.roomCache = {};
  RP.builder.studentCache = {};
  RP.globalRepair.plan = null;
  RP.globalRepair.busy = false;
  SB.budget = [];
  SB.search = '';
  S.search = '';
  resetPlanLens();
  const search = $('twsSectionSearch'); if (search) search.value = '';
  const canvasSearch = $('twsSearch'); if (canvasSearch) canvasSearch.value = '';
  // If a drawer is open it's pointing at a now-invalid placement id.
  if (typeof DRAWER !== 'undefined' && DRAWER.placementId != null) closeDrawer();

  const sid = $('twsScenario').value;
  if (!sid) {
    S.scenarioId = null;
    S.scenarioSummary = null;
    resetProtections();
    S.boards = [];
    $('twsPublish').disabled = true;
    $('twsOptimise').disabled = true;
    $('twsGlobalRepairPlan') && ($('twsGlobalRepairPlan').disabled = true);
    $('twsExport').disabled = true;
    renderSlotBar();
    for (let i = 0; i < paneCount(); i++) renderPaneEmpty(i);
    return;
  }
  const data = await api(`/ops/tw/scenarios/${sid}/`);
  if (!data) return;
  S.scenarioId = data.scenario.id;
  S.scenarioMeta = data.scenario;
  loadProtections();
  const [bdata] = await Promise.all([
    api(`/ops/tw/boards/?scenario_id=${sid}`),
    loadPlanLens(),
  ]);
  S.boards = (bdata && bdata.boards) || [];
  S.scenarioSummary = (bdata && bdata.scenario_summary) || null;
  S.crossBoardClashCount = (bdata && Number(bdata.cross_board_clashes || 0)) || 0;
  S.crossBoardAffectedCount = (bdata && Number(bdata.cross_board_affected_students || 0)) || 0;

  for (let i = 0; i < paneCount(); i++) {
    S.panes[i].boardId = S.boards[i] ? S.boards[i].id : null;
    S.panes[i].group = 0;
    S.panes[i].boardData = null;
  }
  if (INITIAL_BOARD) {
    const idx = S.boards.findIndex(b => String(b.id) === String(INITIAL_BOARD));
    if (idx >= 0) S.panes[0].boardId = S.boards[idx].id;
  }

  $('twsSub').textContent =
    `${data.scenario.name} · ${data.scenario.status.toUpperCase()} · ${S.boards.length} boards`;
  $('twsStBoards').textContent = S.boards.length;
  const published = data.scenario.status === 'published';
  $('twsPublish').disabled = published;
  $('twsOptimise').disabled = false;
  $('twsOptimiseMenu') && ($('twsOptimiseMenu').disabled = false);
  $('twsGlobalRepairPlan') && ($('twsGlobalRepairPlan').disabled = published);
  $('twsExport').disabled = false;
  $('twsNewBoard') && ($('twsNewBoard').disabled = published);
  $('twsSlots') && ($('twsSlots').disabled = published);
  $('twsElectives') && ($('twsElectives').disabled = false);

  updateAggregateMetrics();
  renderSlotBar();
  for (let i = 0; i < paneCount(); i++) await loadAndRenderPane(i);
  if (RP.open) renderRpanel();
  if (SB.open) loadSidebarBudget();
}

function updateAggregateMetrics() {
  let total = 0, placed = 0;
  S.boards.forEach(b => {
    total += (b.primary_count || 0) + (b.visitor_count || 0);
    placed += (b.placement_count || 0);
  });
  const summary = S.scenarioSummary || {};
  const uniqueStudents = Number(summary.unique_students || 0);
  const boardLinks = Number(summary.board_student_links_total || total || 0);
  const placementCount = Number(summary.placements || placed || 0);
  const fallback = '-';
  if ($('twsStStudents')) $('twsStStudents').textContent = uniqueStudents || fallback;
  if ($('twsStLinks')) $('twsStLinks').textContent = boardLinks || fallback;
  if ($('twsStPlaced')) $('twsStPlaced').textContent = placementCount || fallback;
  if ($('twsStCross')) {
    const affected = Number(summary.cross_board_affected_students ?? S.crossBoardAffectedCount ?? 0);
    const pairs = Number(summary.cross_board_conflicts ?? S.crossBoardClashCount ?? 0);
    const incidences = Number(summary.cross_board_student_conflict_incidences || 0);
    $('twsStCross').textContent = affected ? `${affected} students` : String(pairs || 0);
    const metric = $('twsStCross').closest('.metric');
    if (metric) {
      metric.title = affected
        ? `${affected} actual students affected by ${pairs} cross-board section clashes (${incidences} student-clash incidences)`
        : `${pairs} cross-board section clashes`;
    }
  }
}

/* ── Slot bar (boards-on-canvas) ── */
function renderSlotBar() {
  const wrap = $('twsBoardsGrid');
  if (!S.boards.length) {
    wrap.innerHTML = `<span class="lbl" style="color:var(--t4)">${T.noScenario}</span>`;
    return;
  }
  // Only render slots for currently visible panes — the 16-slot array is
  // pre-allocated, but panes beyond cols×rows are hidden and shouldn't
  // appear in the board-bar.
  wrap.innerHTML = S.panes.slice(0, paneCount()).map((p, i) => {
    const paneLetter = String.fromCharCode(65 + i);
    const opts = ['<option value="">—</option>']
      .concat(S.boards.map(b => `<option value="${b.id}"${b.id === p.boardId ? ' selected' : ''}>${esc(b.label)}</option>`))
      .join('');
    const off = !p.boardId;
    return `
      <div class="tws-bslot t${i}${off ? ' off' : ''}" data-pane="${i}">
        <div class="pos" title="${esc(POS_LABELS[i])}">${paneLetter}</div>
        <div class="seg">
          <select data-role="board-select" aria-label="${esc(POS_LABELS[i])} board">${opts}</select>
        </div>
        <button class="off-toggle" data-role="off-toggle"
                aria-label="${off ? (IS_AR ? 'عرض اللوحة' : 'Show pane') : (IS_AR ? 'إخفاء اللوحة' : 'Hide pane')}"
                title="${off ? (IS_AR ? 'عرض' : 'Show pane') : (IS_AR ? 'إخفاء' : 'Hide pane')}">${off ? '+' : '×'}</button>
      </div>`;
  }).join('');
  // Event delegation — no window globals
  wrap.querySelectorAll('.tws-bslot').forEach(slot => {
    const paneIdx = parseInt(slot.dataset.pane);
    slot.querySelector('select[data-role="board-select"]')
        ?.addEventListener('change', (e) => setPaneBoard(paneIdx, e.target.value));
    slot.querySelector('button[data-role="off-toggle"]')
        ?.addEventListener('click', () => togglePaneOff(paneIdx));
  });
}

async function setPaneBoard(idx, boardId) {
  S.panes[idx].boardId = boardId ? parseInt(boardId) : null;
  S.panes[idx].group = 0;
  S.panes[idx].boardData = null;
  renderSlotBar();
  await loadAndRenderPane(idx);
}
async function togglePaneOff(idx) {
  if (S.panes[idx].boardId) {
    S.panes[idx].boardId = null;
  } else {
    const used = new Set(S.panes.filter(p => p.boardId).map(p => p.boardId));
    const nextBoard = S.boards.find(b => !used.has(b.id)) || S.boards[0];
    if (nextBoard) S.panes[idx].boardId = nextBoard.id;
  }
  S.panes[idx].boardData = null;
  renderSlotBar();
  await loadAndRenderPane(idx);
}

/* ── Pane loading + rendering ── */
function paneEl(idx) { return document.querySelector(`.tws-pane[data-idx="${idx}"]`); }

async function loadAndRenderPane(idx) {
  const p = S.panes[idx];
  const el = paneEl(idx);
  if (!el) return;
  if (!p.boardId || !S.scenarioId) { renderPaneEmpty(idx); return; }
  // Board detail carries slot config + placements; the conflicts list is a
  // separate endpoint so we fetch both in parallel.
  const [bdata, cdata] = await Promise.all([
    api(`/ops/tw/boards/${p.boardId}/`),
    api(`/ops/tw/boards/${p.boardId}/conflicts/`),
  ]);
  if (!bdata) return;
  p.boardData = bdata;
  // conflicts endpoint spreads its fields at the response root.
  p.boardData.conflicts = cdata
    ? { overlaps: cdata.overlaps || [], instructor_clashes: cdata.instructor_clashes || [],
        room_clashes: cdata.room_clashes || [], cross_board: cdata.cross_board_conflicts || [],
        student_impact: cdata.student_impact || {} }
    : {};
  renderPane(idx);
  if (S.createAssist.active) paintCreateSectionTargets();
  if (activeSplitSearch()) refreshSplitSearch();
}

function renderPaneEmpty(idx) {
  const el = paneEl(idx);
  if (!el) return;
  el.innerHTML = `
    <div class="pane-hd">
      <span class="term-pick"><span class="caret">▾</span></span>
      <span class="kpis"></span>
      <span class="icons">
        <button data-action="reload" title="Reload pane" aria-label="Reload pane">↻</button>
        <button data-action="maximise" title="Maximise pane" aria-label="Maximise pane">⤢</button>
      </span>
    </div>
    <div class="empty-pane">${T.selectBoard}</div>
  `;
  bindPaneControls(idx);
  updateMaximiseButtons();
}

function renderPane(idx) {
  const p = S.panes[idx];
  const el = paneEl(idx);
  if (!el || !p.boardData) return;
  const data = p.boardData;
  const board = S.boards.find(b => b.id === p.boardId) || { label: '—' };

  const slots = (data.slot_config && data.slot_config.length) ? data.slot_config : DEFAULT_LECTURE_SLOTS;
  const labSlots = (data.lab_slot_config && data.lab_slot_config.length) ? data.lab_slot_config : DEFAULT_LAB_SLOTS;
  const allPlacements = data.placements || [];
  const lensPlacements = allPlacements.filter(itemMatchesPlanLens);
  const placements = lensPlacements.filter(itemMatchesSplitSearch);
  const lensHidden = allPlacements.length - lensPlacements.length;
  const lensActive = activePlanFilter();
  const searchActive = activeSplitSearch();
  const searchHidden = lensPlacements.length - placements.length;

  // Split into lecture vs lab by duration
  const lectP = placements.filter(pl => !isLabPlacement(pl));
  const labP = placements.filter(pl => isLabPlacement(pl));
  const groups = groupPlacements(placements);
  if (p.group >= groups.length) p.group = 0;
  const activeGroup = groups[p.group];

  const groupLect = activeGroup ? lectP.filter(pl => (pl.section || 'S1') === activeGroup.id) : [];
  const groupLab = activeGroup ? labP.filter(pl => (pl.section || 'S1') === activeGroup.id) : [];

  const issueMap = placementIssueMap(data.conflicts || {}, p.boardId);

  // Group-level clash detection: does any placement in each group collide?
  const groupHasClash = groups.map(g =>
    g.placements.some(pl => placementIssuesFromMap(issueMap, pl.id).length)
  );

  // Group tab label = "Gn  Xc · Yst" — mockup format with student counts.
  // Students per group = peak (max) registered/available_capacity across
  // the group's placements, not a sum — because one student takes every
  // course in their group, the group's size equals the biggest single
  // placement, not the total seats.
  const gtabsHtml = groups.map((g, gi) => {
    const courses = new Set(g.placements.map(pl => pl.course_code)).size;
    const stu = g.placements.reduce(
      (m, pl) => Math.max(m, pl.registered_count || pl.available_capacity || 0),
      0,
    );
    return `
      <span class="gtab${gi === p.group ? ' on' : ''}" data-group="${gi}">
        G${gi + 1} <span class="cx">${courses}c${stu ? ' · ' + stu + ' st' : ''}</span>${groupHasClash[gi] ? '<span class="clash-dot"></span>' : ''}
      </span>
    `;
  }).join('');

  const placedCount = activeGroup ? activeGroup.placements.length : 0;
  const hasGroupClash = groupHasClash[p.group];

  el.innerHTML = `
    <div class="pane-hd">
      <span class="term-pick" title="${esc(board.label)}">
        ${esc(board.label)}<span class="caret">▾</span>
      </span>
      <div class="gtabs">${gtabsHtml || '<span class="gtab on">—</span>'}</div>
      <span class="kpis">
        ${lensActive !== 'ALL' ? `<span class="kpi lens">${esc(lensActive === 'SHARED' ? 'Shared' : lensActive)} <b>${placements.length}/${allPlacements.length}</b></span>` : ''}
        ${searchActive ? `<span class="kpi lens">Search <b>${placements.length}/${lensPlacements.length}</b></span>` : ''}
        <span class="kpi">${T.placed} <b>${placedCount}</b></span>
        <span class="kpi ${hasGroupClash ? '' : 'clean'}">${hasGroupClash ? `<b class="warn">${T.clashShort}</b>` : `<b>${T.noClash}</b>`}</span>
      </span>
      <span class="icons">
        <button data-action="reload" title="Reload pane" aria-label="Reload pane">↻</button>
        <button data-action="maximise" title="Maximise pane" aria-label="Maximise pane">⤢</button>
      </span>
    </div>
      <div class="pane-body">
      <div class="lect-block">
        ${renderGridHTML(slots, groupLect, issueMap, 'lect')}
      </div>
      <div class="lab-block${groupLab.length ? '' : ' collapsed'}">
        <div class="block-head" data-action="toggle-lab">
          <span class="caret">▾</span>
          <span class="lab-tag">▣ ${T.labs}</span>
          <span class="spacer"></span>
          <span class="note">${
            groupLab.length
              ? `${groupLab.length} ${T.placed.toLowerCase()} · ` + labSlots.map(s => `${s.start}–${s.end}`).join(' · ')
              : T.noLab
          }</span>
        </div>
        ${renderGridHTML(labSlots, groupLab, issueMap, 'lab')}
      </div>
    </div>
    <div class="pane-status">
      <span class="dot${hasGroupClash ? ' warn' : ''}"></span>
      <span>${esc(board.label)} · G${p.group + 1}</span>
      <span>${(board.primary_count || 0)}${(board.visitor_count || 0) ? '+' + board.visitor_count : ''} st</span>
      ${lensHidden ? `<span>${lensHidden} hidden by lens</span>` : ''}
      ${searchHidden ? `<span>${searchHidden} hidden by search</span>` : ''}
      <span class="sp"></span>
      ${groups.length > 1
        ? `<span>${groups.length - 1} ${IS_AR ? 'مجموعات أخرى' : 'more groups'} ↓</span>`
        : `<span>${(board.placement_count || placements.length)} ${T.placed.toLowerCase()}</span>`
      }
    </div>
  `;
  bindPaneControls(idx);
  updateMaximiseButtons();
}

function renderGridHTML(slots, placements, issueMap, kind) {
  // Mockup layout: slots on Y-axis (rows), days on X-axis (columns).
  // pane-ruler: one header row — "SLOT"/"LAB" corner + day labels.
  // slot-row × N: one per slot — slot number + cells for each day.
  const isLab = kind === 'lab';
  let h = `<div class="block-grid ${isLab ? 'lab-grid' : 'lect-grid'}">`;
  h += `<div class="pane-ruler"><div class="cor">${isLab ? 'LAB' : 'SLOT'}</div>`;
  DAYS.forEach((day, di) => h += `<div class="dh">${esc(DAY_LABELS[di])}</div>`);
  h += `</div>`;
  slots.forEach((slot, si) => {
    // Always label with a compact number/prefix — full time lives in the
    // title tooltip and (for labs) in the lab-head ruler meta line.
    const label = isLab ? `L${si + 1}` : String(si + 1);
    h += `<div class="slot-row" title="${esc(slot.start)}–${esc(slot.end)}">`;
    h += `<div class="slbl">${label}</div>`;
    DAYS.forEach((day) => {
      const placement = placements.find(pl => pl.day === day && pl.start_time === slot.start);
      const issues = placement ? placementIssuesFromMap(issueMap, placement.id) : [];
      const hasClash = issues.length > 0;
      const clashTone = issues.some(i => i.tone === 'critical') ? 'critical'
        : issues.some(i => i.tone === 'warn') ? 'warn'
        : 'cross';
      const issueTitle = hasClash
        ? ` title="${esc((IS_AR ? 'افتح دليل التعارض: ' : 'Open clash evidence: ') + [...new Set(issues.map(i => i.kind))].join(' | '))}"`
        : '';
      const cellAttrs = `data-day="${day}" data-start="${slot.start}" data-end="${slot.end}"`;
      if (placement) {
        const room = placement.room ? esc(placement.room) : '';
        const stu = placement.available_capacity || '';
        const placementTitle = [
          `${placement.course_code || ''} ${placement.section || ''}`.trim(),
          room ? `${IS_AR ? 'قاعة' : 'Room'} ${room}` : '',
          stu ? `${stu} ${IS_AR ? 'مقاعد' : 'seats'}` : '',
        ].filter(Boolean).join(' · ');
        // Per-course pastel palette (user request) — matches XLSX export.
        const bg = courseColor(placement.course_code);
        const accent = courseColorBorder(placement.course_code);
        const style = `background:${bg};border-left-color:${accent}`;
        const cls = `cell filled${hasClash ? ' clash clash-' + clashTone : ''}${placement.is_locked ? ' locked' : ''}`;
        const title = issueTitle || ` title="${esc(placementTitle)}"`;
        h += `<div class="${cls}" ${cellAttrs}${title} data-placement-id="${placement.id}" draggable="${placement.is_locked ? 'false' : 'true'}" style="${style}">`;
        h += `<span class="cid">${esc(placement.course_code)} ${esc(placement.section || '')}</span>`;
        h += `<span class="cmeta">${room}${stu ? '·' + stu : ''}</span>`;
        h += planLensBadgesHtml(placement);
        h += `</div>`;
      } else {
        h += `<div class="cell" ${cellAttrs}></div>`;
      }
    });
    h += `</div>`;
  });
  h += '</div>';
  return h;
}

function bindPaneControls(idx) {
  const el = paneEl(idx);
  if (!el) return;
  // Group tabs
  el.querySelectorAll('.gtab').forEach(tab => {
    tab.addEventListener('click', () => {
      const gi = parseInt(tab.dataset.group);
      if (!Number.isFinite(gi)) return;
      S.panes[idx].group = gi;
      renderPane(idx);
    });
  });
  // Header actions
  el.querySelectorAll('.pane-hd .icons button, .pane-hd .ri button').forEach(btn => {
    const action = btn.dataset.action;
    btn.addEventListener('click', () => {
      if (action === 'reload') loadAndRenderPane(idx);
      else if (action === 'maximise') maximisePane(idx);
    });
  });
  // Lab toggle
  const labHead = el.querySelector('[data-action="toggle-lab"]');
  if (labHead) labHead.addEventListener('click', () => {
    const block = labHead.closest('.lab-block');
    if (!block) return;
    const compactHeight = window.matchMedia && window.matchMedia('(max-height: 760px)').matches;
    if (compactHeight && !block.classList.contains('collapsed')) {
      block.classList.toggle('compact-open');
    } else {
      block.classList.toggle('collapsed');
    }
  });
  // Cell interactions — click to select, drag/drop, sync-hover
  el.querySelectorAll('.cell').forEach(cell => {
    cell.addEventListener('mouseenter', () => {
      broadcastHover(idx, cell.dataset.day, cell.dataset.start);
      showSlotAssistCellStatus(cell);
    });
    cell.addEventListener('mouseleave', () => broadcastHover(idx, null, null));
    cell.addEventListener('dragover', (e) => {
      e.preventDefault();
      applySlotDragHint(cell);
    });
    cell.addEventListener('dragleave', () =>
      cell.classList.remove('drop-valid', 'drop-warning', 'drop-critical'));
    cell.addEventListener('drop', (e) => onCellDrop(idx, cell, e));
    cell.addEventListener('click', async (e) => {
      if (await applyCreateAssistToCell(idx, cell)) {
        e.stopPropagation();
        return;
      }
      if (previewSelectedMoveToSlot(idx, cell)) {
        e.stopPropagation();
      }
    });
    if (cell.classList.contains('filled')) {
      cell.addEventListener('dragstart', (e) => {
        if (cell.classList.contains('locked')) { e.preventDefault(); return; }
        const pid = cell.dataset.placementId;
        S.dragSource = { paneIdx: idx, placementId: pid ? parseInt(pid) : null };
        beginSlotAssist(idx, S.dragSource.placementId);
        e.dataTransfer.setData('text/plain', JSON.stringify({ type: 'move', placement_id: S.dragSource.placementId, source_pane: idx }));
        e.dataTransfer.effectAllowed = 'move';
      });
      cell.addEventListener('dragend', () => clearSlotAssist());
      cell.addEventListener('click', () => {
        document.querySelectorAll('.tws-pane .cell.selected').forEach(c => c.classList.remove('selected'));
        cell.classList.add('selected');
        S.selectedPaneIdx = idx;
        S.selectedPlacementId = parseInt(cell.dataset.placementId);
        $('twsStatusHover').textContent = `Selected ${cell.querySelector('.cid')?.textContent}`;
        beginSlotAssist(idx, S.selectedPlacementId, { sticky: true });
        // Conflicted cells open directly into evidence so the marker is actionable.
        if (cell.classList.contains('clash')) openSelectionInspector();
        else if (RP.open && RP.tab === 'selection') renderRpanel();
      });
      cell.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        openDrawer(idx, parseInt(cell.dataset.placementId));
      });
    }
  });
}

/* ── Drag & drop handler — mirrors main-page onDrop semantics ── */
function initPaneHeaderActionDelegation() {
  const quad = $('twsQuad');
  if (!quad) return;
  quad.addEventListener('click', (e) => {
    const btn = e.target?.closest?.('button[data-action]');
    if (!btn || !quad.contains(btn)) return;
    const action = btn.dataset.action;
    if (action !== 'reload' && action !== 'maximise') return;
    const pane = btn.closest('.tws-pane');
    const paneIdx = Number(pane?.dataset.idx);
    if (!Number.isFinite(paneIdx)) return;
    e.preventDefault();
    e.stopPropagation();
    if (action === 'reload') loadAndRenderPane(paneIdx);
    else maximisePane(paneIdx);
  }, true);
}

function findPlacement(placementId) {
  for (let i = 0; i < paneCount(); i++) {
    const data = S.panes[i].boardData;
    if (!data) continue;
    const found = (data.placements || []).find(p => String(p.id) === String(placementId));
    if (found) return { placement: found, paneIdx: i };
  }
  return null;
}

async function movePlacementToSlot({
  placementId,
  targetPaneIdx,
  sourcePaneIdx,
  day,
  start,
  end,
  room,
  allowProtected = false,
  refresh = true,
  clearAssist = true,
  notifyResult = true,
  recordUndo = true,
  force = false,
}) {
  const located = findPlacement(placementId);
  if (!located) { notify.error(IS_AR ? 'تعذر تحديد الموقع' : 'Source placement not found'); return null; }
  const reasons = placementProtectionReasons(located.placement);
  if (!allowProtected && reasons.length) {
    notify.warning(`${placementShortLabel(located.placement)} ${IS_AR ? 'محمي' : 'is protected'}: ${reasons.join(', ')}`);
    return null;
  }
  const oldDay = located.placement.day;
  const oldStart = located.placement.start_time;
  const oldEnd = located.placement.end_time;
  const oldRoom = located.placement.room || '';
  const newRoom = room === undefined ? oldRoom : room;
  if (!force && day === oldDay && start === oldStart && newRoom === oldRoom) return null;

  const payload = { day, start_time: start, end_time: end };
  if (room !== undefined) payload.room = room;
  const data = await api(`/ops/tw/placements/${placementId}/move/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!data) { notify.error(T.moveFail); return null; }

  const undoAction = {
    type: 'move',
    placement_id: placementId,
    old_day: oldDay, old_start: oldStart, old_end: oldEnd,
    old_room: oldRoom,
    new_day: day, new_start: start, new_end: end,
    new_room: newRoom,
  };
  if (recordUndo) {
    S.undoStack.push(undoAction);
    S.redoStack = [];
    updateUndoRedoButtons();
  }

  const v = data.validation || {};
  if (notifyResult) {
    if ((v.critical_count || 0) > 0) {
      notify.warning(IS_AR ? `تم النقل مع ${v.critical_count} تعارض` : `Moved with ${v.critical_count} conflict(s)`);
    } else {
      notify.success(T.moveOk);
    }
  }

  if (clearAssist) clearSlotAssist();
  const sourceIdx = Number.isFinite(sourcePaneIdx) ? sourcePaneIdx : located.paneIdx;
  const boardIds = new Set();
  const targetBoard = S.panes[targetPaneIdx]?.boardId;
  const sourceBoard = S.panes[sourceIdx]?.boardId;
  if (targetBoard) boardIds.add(targetBoard);
  if (sourceBoard) boardIds.add(sourceBoard);
  if (refresh) {
    for (let i = 0; i < paneCount(); i++) {
      if (boardIds.has(S.panes[i].boardId)) await loadAndRenderPane(i);
    }
    await refreshBoardsSummary();
  }
  return data;
}

function canBundleCandidate(candidate) {
  return !!(
    candidate?.pair?.companion
    && !candidate.pair.protected?.length
    && Number(candidate.pair.critical || 0) === 0
  );
}

async function applyCandidateMove({ placementId, targetPaneIdx, sourcePaneIdx, candidate, day, start, end, room, auto = false }) {
  const located = findPlacement(placementId);
  if (!located) return null;
  const pair = candidate && canBundleCandidate(candidate) ? candidate.pair : null;
  const bundleLabel = pair ? `${placementShortLabel(located.placement)} + ${placementShortLabel(pair.companion)}` : placementShortLabel(located.placement);
  const targetRoom = room === undefined ? (located.placement.room || '') : room;
  const firstMove = {
    type: 'move',
    placement_id: placementId,
    old_day: located.placement.day,
    old_start: located.placement.start_time,
    old_end: located.placement.end_time,
    old_room: located.placement.room || '',
    new_day: day || candidate?.day,
    new_start: start || candidate?.start,
    new_end: end || candidate?.end,
    new_room: targetRoom,
  };
  const secondMove = pair ? {
    type: 'move',
    placement_id: pair.companion.id,
    old_day: pair.companion.day,
    old_start: pair.companion.start_time,
    old_end: pair.companion.end_time,
    old_room: pair.companion.room || '',
    new_day: day || candidate.day,
    new_start: pair.start,
    new_end: pair.end,
    new_room: pair.companion.room || '',
  } : null;
  const first = await movePlacementToSlot({
    placementId,
    targetPaneIdx,
    sourcePaneIdx,
    day: firstMove.new_day,
    start: firstMove.new_start,
    end: firstMove.new_end,
    room,
    refresh: !pair,
    clearAssist: !pair,
    notifyResult: !auto && !pair,
    recordUndo: !pair,
  });
  if (!first) return null;

  if (pair) {
    const second = await movePlacementToSlot({
      placementId: pair.companion.id,
      targetPaneIdx,
      sourcePaneIdx: targetPaneIdx,
      day: secondMove.new_day,
      start: secondMove.new_start,
      end: secondMove.new_end,
      refresh: true,
      clearAssist: true,
      notifyResult: false,
      recordUndo: false,
    });
    if (!second) {
      await movePlacementToSlot({
        placementId,
        targetPaneIdx: located.paneIdx,
        sourcePaneIdx: targetPaneIdx,
        day: firstMove.old_day,
        start: firstMove.old_start,
        end: firstMove.old_end,
        room: firstMove.old_room,
        allowProtected: true,
        refresh: true,
        clearAssist: true,
        notifyResult: false,
        recordUndo: false,
        force: true,
      });
      notify.error(IS_AR ? 'تعذر نقل الحزمة؛ تمت استعادة الشعبة الأولى.' : 'Bundle move failed; first section was restored.');
      return null;
    }
    S.undoStack.push({ type: 'bundle_move', moves: [firstMove, secondMove], label: bundleLabel });
    S.redoStack = [];
    updateUndoRedoButtons();
    notify.success(auto
      ? `${IS_AR ? 'تم نقل الحزمة' : 'Bundled move applied'}: ${bundleLabel}`
      : `${IS_AR ? 'تم نقل الحزمة' : 'Bundled back-to-back move'}: ${bundleLabel}`);
  }
  return first;
}

function openDeepRepairCandidateFromSlot(candidate, placementId) {
  const run = S.slotAssist.deepRepairRun;
  if (!run || !candidate?.deepRepairCandidateId) return false;
  RP.repair.placementId = placementId;
  RP.repair.run = run;
  RP.repair.busy = false;
  toggleRpanel(true);
  setRpanelTab('repair');
  $('twsStatusHover').textContent = `Move optimisation ${candidate.deepRepairCandidateId} loaded. Review, approve, then apply from the repair panel.`;
  return true;
}

function deepRepairCandidateForSlot(candidate) {
  const run = S.slotAssist.deepRepairRun || RP.repair.run;
  const candidateId = candidate?.deepRepairCandidateId || candidate?.candidate_id || '';
  if (!run || !candidateId) return { run, candidateId, candidate };
  const full = (run.candidates || []).find(row => String(row.candidate_id) === String(candidateId));
  return { run, candidateId, candidate: full || candidate };
}

function candidateDirectRepairReady(candidate) {
  const exact = candidateExactRepair(candidate);
  const status = String(candidate?.status || '').toLowerCase();
  const solverStatus = String(candidate?.solver_status || exact?.solver_status || '').toLowerCase();
  const usefulRecovery = Number(exact?.blocked_recovered || 0) > 0
    || Number(exact?.requested_courses_recovered || 0) > 0;
  return candidate?.deepRepair
    && !!(candidate?.deepRepairCandidateId || candidate?.candidate_id)
    && status === 'feasible'
    && ['optimal', 'feasible'].includes(solverStatus)
    && Number(exact?.existing_lost || 0) === 0
    && usefulRecovery;
}

async function applyDeepRepairCandidateFromSlot(candidate, placementId, cell = null) {
  const resolved = deepRepairCandidateForSlot(candidate);
  const run = resolved.run;
  const candidateId = resolved.candidateId;
  const fullCandidate = resolved.candidate;
  if (!run || !candidateId || !fullCandidate) return false;
  RP.repair.placementId = placementId;
  RP.repair.run = run;
  RP.repair.busy = false;
  const ready = candidateDirectRepairReady({ ...candidate, ...fullCandidate, deepRepair: true, deepRepairCandidateId: candidateId });
  if (!ready) {
    openDeepRepairCandidateFromSlot(candidate, placementId);
    notify.error(IS_AR ? 'Ù‡Ø°Ø§ Ø§Ù„Ù…ÙˆØ¶Ø¹ Ù„ÙŠØ³ Ø¥ØµÙ„Ø§Ø­Ø§Ù‹ Ù‚Ø§Ø¨Ù„Ø§Ù‹ Ù„Ù„ØªØ·Ø¨ÙŠÙ‚.' : 'This slot is not an apply-ready repair. Review the repair panel details.');
    return true;
  }
  const ok = await confirmRepairAction({
    title: `Apply optimisation ${candidateId}`,
    sub: 'This will move the selected section and apply the audited student reassignment for this slot.',
    body: `<div class="tws-repair-panel">
      ${repairActionMetricsHtml(fullCandidate)}
      ${repairDecisionGateHtml(fullCandidate)}
      ${repairPreflightGateHtml(fullCandidate)}
      <div class="tws-repair-explain">
        <div class="tws-repair-section-title">Direct slot optimisation</div>
        <div class="tws-repair-explain-row"><b>Click target</b><span>${esc(fullCandidate.day || candidate.day)} ${esc(fullCandidate.start_time || candidate.start)}-${esc(fullCandidate.end_time || candidate.end)}${fullCandidate.room ? ` · ${esc(fullCandidate.room)}` : ''}</span></div>
        <div class="tws-repair-explain-row"><b>Safety</b><span>The existing approval preflight and transactional apply checks will run before any write.</span></div>
        <div class="tws-repair-explain-row"><b>Rollback</b><span>The optimisation remains rollbackable from the repair panel after apply.</span></div>
      </div>
    </div>`,
    confirmLabel: 'Apply optimisation',
  });
  if (!ok) return true;
  if (cell) cell.classList.add('assist-loading');
  $('twsStatusHover').textContent = `Applying optimisation ${candidateId}...`;
  const runId = run?.run?.id;
  if (!runId) {
    if (cell) cell.classList.remove('assist-loading');
    notify.error(IS_AR ? 'Ù„Ø§ ÙŠÙˆØ¬Ø¯ ØªØ­Ù„ÙŠÙ„ Ø¥ØµÙ„Ø§Ø­ Ù†Ø´Ø·.' : 'No active repair run is available for this slot.');
    return true;
  }
  const approved = await api(`/ops/tw/repair/runs/${runId}/candidates/${candidateId}/approve/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ notes: 'Approved from direct slot optimisation click' }),
  });
  if (!approved) {
    if (cell) cell.classList.remove('assist-loading');
    notify.error(IS_AR ? 'ÙØ´Ù„ Ø§Ø¹ØªÙ…Ø§Ø¯ Ø§Ù„Ø¥ØµÙ„Ø§Ø­' : 'Repair approval failed');
    openDeepRepairCandidateFromSlot(candidate, placementId);
    return true;
  }
  RP.repair.run = approved;
  S.slotAssist.deepRepairRun = approved;
  const applied = await api(`/ops/tw/repair/runs/${runId}/candidates/${candidateId}/apply/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  if (!applied) {
    if (cell) cell.classList.remove('assist-loading');
    notify.error(IS_AR ? 'ÙØ´Ù„ ØªØ·Ø¨ÙŠÙ‚ Ø§Ù„Ø¥ØµÙ„Ø§Ø­' : 'Repair apply failed');
    toggleRpanel(true);
    setRpanelTab('repair');
    return true;
  }
  RP.repair.run = applied;
  S.slotAssist.deepRepairRun = applied;
  await refreshAfterRepairMutation();
  clearSlotAssist();
  $('twsStatusHover').textContent = `Optimisation ${candidateId} applied. Rollback is available from the repair panel.`;
  notify.success(IS_AR ? 'ØªÙ… ØªØ·Ø¨ÙŠÙ‚ Ø§Ù„Ø¥ØµÙ„Ø§Ø­' : `Optimisation ${candidateId} applied`);
  if (RP.open) renderRpanel();
  return true;
}

function previewSelectedMoveToSlot(paneIdx, cell) {
  if (!S.slotAssist.active || !S.selectedPlacementId || cell.classList.contains('filled')) return false;
  if (String(S.slotAssist.placementId) !== String(S.selectedPlacementId)) return false;
  const key = cell.dataset.slotAssistKey || slotAssistKey(S.slotAssist.kind, cell.dataset.day, cell.dataset.start);
  const candidate = S.slotAssist.candidates.get(key);
  if (!candidate) return false;
  const day = cell.dataset.day;
  const start = cell.dataset.start;
  const end = cell.dataset.end;
  const pending = S.slotAssist.pendingMove;
  if (candidate.deepRepair && candidate.deepRepairCandidateId) {
    document.querySelectorAll('.tws-pane .cell.assist-preview').forEach(el => el.classList.remove('assist-preview'));
    cell.classList.add('assist-preview');
    applyDeepRepairCandidateFromSlot(candidate, S.selectedPlacementId, cell).catch(() => {
      cell.classList.remove('assist-loading');
      notify.error(IS_AR ? 'ÙØ´Ù„ ØªØ·Ø¨ÙŠÙ‚ Ø§Ù„Ø¥ØµÙ„Ø§Ø­' : 'Repair apply failed');
    });
    return true;
  }
  if (
    pending
    && String(pending.placementId) === String(S.selectedPlacementId)
    && pending.paneIdx === paneIdx
    && pending.day === day
    && pending.start === start
  ) {
    if (pending.applying) return true;
    pending.applying = true;
    $('twsStatusHover').textContent = `Moving to ${day} ${start}-${end}...`;
    applyCandidateMove({
      placementId: S.selectedPlacementId,
      targetPaneIdx: paneIdx,
      sourcePaneIdx: Number.isFinite(S.selectedPaneIdx) ? S.selectedPaneIdx : paneIdx,
      candidate,
      day,
      start,
      end,
    }).finally(() => {
      if (S.slotAssist.pendingMove) S.slotAssist.pendingMove.applying = false;
    });
    return true;
  }

  document.querySelectorAll('.tws-pane .cell.assist-preview').forEach(el => el.classList.remove('assist-preview'));
  cell.classList.add('assist-preview');
  S.slotAssist.pendingMove = { placementId: S.selectedPlacementId, paneIdx, day, start, end };
  const located = findPlacement(S.selectedPlacementId);
  const label = located ? placementShortLabel(located.placement) : 'Selection';
  const pairText = canBundleCandidate(candidate)
    ? ` Bundle ${placementShortLabel(candidate.pair.companion)} ${candidate.pair.relation} ${candidate.pair.start}-${candidate.pair.end}.`
    : '';
  const actionText = 'Click same slot again to apply.';
  $('twsStatusHover').textContent = `Preview move ${label} -> ${day} ${start}-${end}: ${splitSlotDetail(candidate)}.${pairText} ${actionText}`;
  if (RP.open && RP.tab === 'selection') renderRpanel();
  return true;
}

async function createPlannedPlacementFromMeetings(boardId, payload, meetings) {
  const data = await api('/ops/tw/placements/create-planned/', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      board_id: boardId,
      course_code: payload.course_code,
      course_key: payload.course_key || payload.course_code,
      course_name: payload.course_name || payload.course_code,
      section_label: payload.section_label,
      meetings,
      capacity: payload.max_per_section || 40,
    }),
  });
  if (!data) return null;
  const v = data.validation || {};
  if ((v.critical_count || 0) > 0) {
    notify.warning(IS_AR ? `Placed with ${v.critical_count} conflict(s)` : `Placed with ${v.critical_count} conflict(s)`);
  } else {
    const count = Array.isArray(data.placements) ? data.placements.length : 1;
    notify.success(count > 1 ? `Placed full ${count}-meeting pattern` : (IS_AR ? 'Placed' : 'Placed'));
  }
  return data;
}

async function createPlannedPlacementFromCell(paneIdx, cell, payload) {
  const boardId = S.panes[paneIdx].boardId;
  if (!boardId) {
    notify.error(IS_AR ? 'Select a board in this pane first' : 'Select a board in this pane first');
    return null;
  }
  if (cell.classList.contains('filled')) {
    notify.warning(IS_AR ? 'Choose an empty slot' : 'Choose an empty slot');
    return null;
  }
  const day = cell.dataset.day;
  const start = cell.dataset.start;
  const end = cell.dataset.end;
  const data = await createPlannedPlacementFromMeetings(boardId, payload, [{ day, start_time: start, end_time: end }]);
  if (!data) return null;
  S.undoStack.push({
    type: 'create', placement_id: data.placement.id, board_id: boardId,
    term_section_id: data.placement.term_section_id,
    day, start_time: start, end_time: end, room: '',
  });
  S.redoStack = [];
  updateUndoRedoButtons();
  await loadAndRenderPane(paneIdx);
  await refreshBoardsSummary();
  await loadSidebarBudget();
  return data;
}

async function createPlannedPlacementFromCandidate(candidate, payload) {
  const boardId = candidate?.boardId || S.panes[candidate?.paneIdx]?.boardId;
  const meetings = createAssistCandidateMeetings(candidate).map(meeting => ({
    day: meeting.day,
    start_time: meeting.start,
    end_time: meeting.end,
  }));
  if (!boardId || !meetings.length) {
    notify.warning(IS_AR ? 'Ø§Ø®ØªØ± Ø®Ø§Ù†Ø© ÙØ§Ø±ØºØ©' : 'Choose an empty slot');
    return null;
  }
  const data = await createPlannedPlacementFromMeetings(boardId, payload, meetings);
  if (!data) return null;
  const placements = Array.isArray(data.placements) && data.placements.length
    ? data.placements
    : [data.placement].filter(Boolean);
  if (placements.length > 1) {
    S.undoStack.push({
      type: 'bundle_create',
      placement_ids: placements.map(item => item.id),
      board_id: boardId,
      payload: {
        course_code: payload.course_code,
        course_key: payload.course_key || payload.course_code,
        course_name: payload.course_name || payload.course_code,
        section_label: payload.section_label,
        max_per_section: payload.max_per_section || 40,
      },
      meetings,
      label: createAssistLabel(payload),
    });
  } else if (placements[0]) {
    S.undoStack.push({
      type: 'create',
      placement_id: placements[0].id,
      board_id: boardId,
      term_section_id: placements[0].term_section_id,
      day: meetings[0].day,
      start_time: meetings[0].start_time,
      end_time: meetings[0].end_time,
      room: '',
    });
  }
  S.redoStack = [];
  updateUndoRedoButtons();
  await loadAndRenderPane(candidate.paneIdx);
  await refreshBoardsSummary();
  await loadSidebarBudget();
  return data;
}

async function applyCreateAssistToCell(paneIdx, cell) {
  if (!S.createAssist.active || !cell?.dataset.createAssist || !S.createAssist.payload) return false;
  const key = cell.dataset.createAssistKey || '';
  const candidate = key ? S.createAssist.candidates.get(key) : null;
  const data = candidate
    ? await createPlannedPlacementFromCandidate(candidate, S.createAssist.payload)
    : await createPlannedPlacementFromCell(paneIdx, cell, S.createAssist.payload);
  if (data) clearCreateSectionAssist();
  return true;
}

async function onCellDrop(paneIdx, cell, e) {
  e.preventDefault();
  const dropCandidate = S.slotAssist.active && cell?.dataset.slotAssistKey
    ? S.slotAssist.candidates.get(cell.dataset.slotAssistKey)
    : null;
  clearSlotAssist();
  cell.classList.remove('drop-valid', 'drop-warning', 'drop-critical');
  let payload;
  try { payload = JSON.parse(e.dataTransfer.getData('text/plain')); } catch { return; }

  if (payload.type === 'create_planned') {
    await createPlannedPlacementFromCell(paneIdx, cell, payload);
    clearCreateSectionAssist();
    return;
  }
  if (payload.type !== 'move' || !payload.placement_id) return;

  const day = cell.dataset.day;
  const start = cell.dataset.start;
  const end = cell.dataset.end;
  await applyCandidateMove({
    placementId: payload.placement_id,
    targetPaneIdx: paneIdx,
    sourcePaneIdx: Number.isFinite(payload.source_pane) ? payload.source_pane : paneIdx,
    candidate: dropCandidate,
    day,
    start,
    end,
  });
}

async function refreshBoardsSummary() {
  if (!S.scenarioId) return;
  const bdata = await api(`/ops/tw/boards/?scenario_id=${S.scenarioId}`);
  if (bdata && bdata.boards) {
    S.boards = bdata.boards;
    S.scenarioSummary = bdata.scenario_summary || null;
    S.crossBoardClashCount = Number(bdata.cross_board_clashes || 0);
    S.crossBoardAffectedCount = Number(bdata.cross_board_affected_students || 0);
    updateAggregateMetrics();
    renderSlotBar();
  }
  await loadPlanLens({ rerender: true });
  // Invalidate capacity cache and refresh the panels if open — mutations
  // may have changed conflict counts and capacity deltas.
  RP.capacity = {};
  RP.studentBlockers = {
    token: RP.studentBlockers.token + 1,
    scenarioId: null,
    data: null,
    activeCourse: RP.studentBlockers.activeCourse,
  };
  RP.fixQueue.cache = {};
  RP.fixQueue.items = [];
  RP.fixQueue.token += 1;
  RP.builder.token += 1;
  RP.builder.resolverToken += 1;
  RP.builder.readiness = null;
  RP.builder.actions = [];
  RP.builder.activeAction = null;
  RP.builder.resolver = null;
  RP.builder.roomCache = {};
  RP.builder.studentCache = {};
  if (RP.open) renderRpanel();
  if (BP.open) renderBpanel();
}

/* ── Cross-pane sync-hover (direct DOM) ── */
function broadcastHover(sourcePaneIdx, day, start) {
  for (let i = 0; i < paneCount(); i++) {
    if (i === sourcePaneIdx) continue;
    const el = paneEl(i);
    if (!el) continue;
    el.querySelectorAll('.cell.sync-hover').forEach(c => c.classList.remove('sync-hover'));
    if (day && start) {
      const sel = `.cell[data-day="${day}"][data-start="${start}"]`;
      el.querySelectorAll(sel).forEach(c => c.classList.add('sync-hover'));
    }
  }
  $('twsStatusHover').textContent = (day && start) ? `Hovered ${day}/${start}` : 'Hovered —';
}

/* ── Layout ── */
function paneSlotSnapshot() {
  const q = $('twsQuad');
  if (!q) return [];
  return Array.from(q.children)
    .filter(el => el.classList && el.classList.contains('tws-pane'))
    .map(el => ({ el, idx: el.dataset.idx }));
}

function restorePaneSlots(slots) {
  const q = $('twsQuad');
  if (!q || !Array.isArray(slots)) return;
  slots.forEach(slot => {
    if (!slot || !slot.el) return;
    slot.el.dataset.idx = String(slot.idx);
    q.appendChild(slot.el);
  });
}

function updateMaximiseButtons() {
  const restore = !!S.maximised;
  document.querySelectorAll('[data-action="maximise"]').forEach(btn => {
    btn.textContent = restore ? '⤡' : '⤢';
    btn.classList.toggle('active', restore);
    btn.setAttribute('aria-label', restore ? 'Restore split layout' : 'Maximise pane');
    btn.title = restore ? 'Restore split layout' : 'Maximise pane';
  });
}

function restoreMaximisedPane(layoutOverride = null) {
  const snap = S.maximised;
  if (!snap) return false;
  S.maximised = null;
  if (Array.isArray(snap.panes)) S.panes = snap.panes;
  restorePaneSlots(snap.domSlots);
  const cols = layoutOverride?.cols || snap.cols || 2;
  const rows = layoutOverride?.rows || snap.rows || 2;
  applyLayout(cols, rows, { keepMaximisedState: true });
  for (let i = 0; i < paneCount(); i++) {
    const p = S.panes[i];
    if (!p || !p.boardId || !p.boardData) renderPaneEmpty(i);
    else renderPane(i);
  }
  updateMaximiseButtons();
  return true;
}

// Apply an arbitrary C×R layout (clamped to [1..4] on each axis). Viewport
// fit is enforced by the picker before this is called, but we clamp again
// defensively so programmatic callers (shortcuts, maximise) never wedge a
// shape that can't render.
function applyLayout(cols, rows, opts = {}) {
  const c = Math.max(1, Math.min(4, cols | 0));
  const r = Math.max(1, Math.min(4, rows | 0));
  if (S.maximised && !opts.keepMaximisedState) {
    restoreMaximisedPane({ cols: c, rows: r });
    return;
  }
  const prev = paneCount();
  S.cols = c; S.rows = r;
  const q = $('twsQuad');
  q.style.setProperty('--tws-cols', String(c));
  q.style.setProperty('--tws-rows', String(r));
  // Toggle pane visibility via `hidden` so CSS grid packs only the visible
  // ones. data-idx stays stable so paneEl(i) / S.panes[i] stay in sync.
  const count = c * r;
  document.querySelectorAll('#twsQuad .tws-pane').forEach(el => {
    const i = Number(el.dataset.idx);
    el.hidden = i >= count;
  });
  // Update trigger label + status ribbon.
  const lbl = $('twsLayoutTriggerLbl');
  if (lbl) lbl.textContent = `${c}×${r}`;
  const sl = $('twsStatusLayout');
  if (sl) sl.textContent = `Layout ${c}×${r}`;
  // Growing the pane count? Seed the newly-visible slots with unused
  // boards and render them.
  if (prev && count > prev) {
    const used = new Set(S.panes.filter(p => p.boardId).map(p => p.boardId));
    for (let i = 0; i < count; i++) {
      if (!S.panes[i].boardId) {
        const next = S.boards.find(b => !used.has(b.id));
        if (next) { S.panes[i].boardId = next.id; used.add(next.id); }
      }
    }
    renderSlotBar();
    for (let i = 0; i < count; i++) loadAndRenderPane(i);
  } else {
    // Shrinking or same shape — still redraw the boards bar since the
    // slot count it shows is derived from paneCount().
    renderSlotBar();
  }
  updateMaximiseButtons();
}
function maximisePane(idx) {
  if (S.maximised) {
    restoreMaximisedPane();
    return;
  }
  S.maximised = {
    cols: S.cols,
    rows: S.rows,
    panes: S.panes.slice(),
    domSlots: paneSlotSnapshot(),
  };
  if (idx !== 0) {
    // Swap the whole DOM slots (node + data-idx) so paneEl(i) keeps
    // returning the element that holds pane state i. Previously only the
    // S.panes array was swapped, which left data-idx stale and broke
    // every subsequent paneEl() lookup.
    const a = paneEl(0), b = paneEl(idx);
    if (a && b) {
      const aSibling = a.nextSibling;
      b.parentNode.insertBefore(a, b);
      if (aSibling === b) b.parentNode.appendChild(b);
      else b.parentNode.insertBefore(b, aSibling);
      a.dataset.idx = String(idx);
      b.dataset.idx = '0';
    }
    const tmp = S.panes[0]; S.panes[0] = S.panes[idx]; S.panes[idx] = tmp;
  }
  applyLayout(1, 1, { keepMaximisedState: true });
  for (let i = 0; i < paneCount(); i++) renderPane(i);
  updateMaximiseButtons();
}

/* ── Matrix layout picker ──
   Builds a 4×4 grid of cells inside #twsLayoutMatrix. Hovering a cell at
   (c,r) highlights the (1..c × 1..r) region so the user sees exactly what
   layout they're about to pick. Clicking commits with applyLayout(c,r).
   Viewport fit is re-evaluated every time the dropdown opens — cells that
   won't fit get `.disabled` + are not clickable. */
function initLayoutMatrix() {
  const matrix = $('twsLayoutMatrix');
  const dropdown = $('twsLayoutDropdown');
  const trigger = $('twsLayoutTrigger');
  const caption = $('twsLayoutCaption');
  if (!matrix || !dropdown || !trigger) return;

  // Build 4×4 = 16 cells, tagged with their 1-based (c,r) coord.
  matrix.innerHTML = '';
  for (let r = 1; r <= 4; r++) {
    for (let c = 1; c <= 4; c++) {
      const cell = document.createElement('div');
      cell.className = 'tws-layout-cell';
      cell.dataset.c = String(c);
      cell.dataset.r = String(r);
      cell.setAttribute('role', 'gridcell');
      cell.setAttribute('aria-label', `${c}×${r}`);
      matrix.appendChild(cell);
    }
  }
  const cells = Array.from(matrix.children);

  function setCaption(c, r, disabled) {
    if (!caption) return;
    if (disabled) {
      caption.innerHTML = `<span class="warn">${c}×${r} — ${LANGUAGE_CODE === 'ar' ? 'لا يتسع للشاشة' : 'too big for screen'}</span>`;
    } else {
      caption.textContent = `${c}×${r} (${c * r} ${LANGUAGE_CODE === 'ar' ? 'لوحات' : 'panes'})`;
    }
  }

  function refreshDisabled() {
    const maxC = viewportMaxCols(), maxR = viewportMaxRows();
    cells.forEach(cell => {
      const c = Number(cell.dataset.c), r = Number(cell.dataset.r);
      cell.classList.toggle('disabled', c > maxC || r > maxR);
    });
  }

  function highlight(c, r) {
    cells.forEach(cell => {
      const cc = Number(cell.dataset.c), cr = Number(cell.dataset.r);
      cell.classList.toggle('hover', cc <= c && cr <= r);
    });
  }

  function openDropdown() {
    refreshDisabled();
    dropdown.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    highlight(S.cols, S.rows);
    setCaption(S.cols, S.rows, false);
  }
  function closeDropdown() {
    dropdown.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    highlight(0, 0);
  }

  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    if (dropdown.hidden) openDropdown(); else closeDropdown();
  });
  // Close on outside click.
  document.addEventListener('click', (e) => {
    if (dropdown.hidden) return;
    if (!e.target.closest('#twsLayoutPicker')) closeDropdown();
  });
  // Close on Escape.
  dropdown.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeDropdown(); trigger.focus(); }
  });

  matrix.addEventListener('mousemove', (e) => {
    const cell = e.target.closest('.tws-layout-cell');
    if (!cell) return;
    const c = Number(cell.dataset.c), r = Number(cell.dataset.r);
    const disabled = cell.classList.contains('disabled');
    highlight(disabled ? 0 : c, disabled ? 0 : r);
    setCaption(c, r, disabled);
  });
  matrix.addEventListener('mouseleave', () => {
    highlight(S.cols, S.rows);
    setCaption(S.cols, S.rows, false);
  });
  matrix.addEventListener('click', (e) => {
    const cell = e.target.closest('.tws-layout-cell');
    if (!cell || cell.classList.contains('disabled')) return;
    const c = Number(cell.dataset.c), r = Number(cell.dataset.r);
    applyLayout(c, r);
    closeDropdown();
  });

  // Re-evaluate viewport fit on resize — if the current layout no longer
  // fits, shrink to the largest that does.
  window.addEventListener('resize', () => {
    const maxC = viewportMaxCols(), maxR = viewportMaxRows();
    if (S.cols > maxC || S.rows > maxR) {
      applyLayout(Math.min(S.cols, maxC), Math.min(S.rows, maxR));
    }
    if (!dropdown.hidden) refreshDisabled();
  });
}

/* ── Presets ── */
function applyPreset(name) {
  if (!S.boards.length) return;
  if (name === 'first4') {
    for (let i = 0; i < paneCount(); i++) S.panes[i].boardId = S.boards[i] ? S.boards[i].id : null;
  } else if (name === 'cross') {
    const sorted = [...S.boards].sort((a, b) => (b.critical || 0) - (a.critical || 0));
    for (let i = 0; i < paneCount(); i++) S.panes[i].boardId = sorted[i] ? sorted[i].id : null;
  }
  for (const p of S.panes) { p.group = 0; p.boardData = null; }
  renderSlotBar();
  for (let i = 0; i < paneCount(); i++) loadAndRenderPane(i);
}

/* ── Right inspector panel ── */
function toggleRpanel(force) {
  RP.open = (typeof force === 'boolean') ? force : !RP.open;
  const body = $('twsBody');
  const btn = $('twsRpanelToggle');
  const panel = $('twsRpanel');
  body.classList.toggle('rp-open', RP.open);
  if (btn) {
    btn.classList.toggle('primary', RP.open);
    btn.setAttribute('aria-expanded', String(RP.open));
  }
  if (panel) panel.setAttribute('aria-hidden', String(!RP.open));
  if (RP.open) renderRpanel();
}
function closeRpanel({ focusTrigger = false } = {}) {
  if (!RP.open) return;
  toggleRpanel(false);
  if (focusTrigger) document.querySelector('#twsViewMenu > summary')?.focus();
}
function setRpanelTab(tab) {
  RP.tab = tab;
  document.querySelectorAll('.tws-rpanel-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });
  renderRpanel();
}
function aggregateVisibleBoardsData() {
  // Collect conflicts + placements across every pane whose board is loaded.
  // Dedupe by board so the same board shown in two panes only counts once.
  const seen = new Set();
  const items = [];
  S.panes.forEach((p, i) => {
    if (!p.boardData || !p.boardId || seen.has(p.boardId)) return;
    seen.add(p.boardId);
    items.push({
      paneIdx: i,
      boardId: p.boardId,
      boardLabel: (S.boards.find(b => b.id === p.boardId) || {}).label || '—',
      data: p.boardData,
      conflicts: p.boardData.conflicts || {},
    });
  });
  return items;
}
function renderRpanel() {
  if (!RP.open) return;
  const body = $('twsRpanelBody');
  if (!body) return;
  if (RP.tab === 'issues') renderRpanelIssues(body);
  else if (RP.tab === 'students') renderRpanelStudentBlockers(body);
  else if (RP.tab === 'capacity') renderRpanelCapacity(body);
  else if (RP.tab === 'repair') renderRpanelRepair(body);
  else if (RP.tab === 'selection') renderRpanelSelection(body);
}

function openSelectionInspector() {
  if (!RP.open) toggleRpanel(true);
  if (RP.tab !== 'selection') setRpanelTab('selection');
  else renderRpanel();
}

function issueRowIds(row) {
  return (row?.ids || [row?.placement_a_id, row?.placement_b_id]).filter(id => id != null);
}

function addPlacementIssue(map, id, issue) {
  const key = String(id);
  const list = map.get(key) || [];
  list.push(issue);
  map.set(key, list);
}

function placementIssuesFromMap(map, placementId) {
  return map.get(String(placementId)) || [];
}

function placementIssueMap(conflicts, boardId) {
  const map = new Map();
  (conflicts.overlaps || []).forEach(row => {
    const sections = (row.sections || []).join(' / ');
    const shared = row.shared_students != null
      ? ` | ${row.shared_students} shared students`
      : '';
    issueRowIds(row).forEach(id => addPlacementIssue(map, id, {
      kind: 'Same-board time clash',
      tone: row.severity === 'warning' ? 'warn' : 'critical',
      title: sections || 'Same-board time clash',
      detail: `${row.detail || ''}${shared}`,
    }));
  });
  (conflicts.instructor_clashes || []).forEach(row => {
    issueRowIds(row).forEach(id => addPlacementIssue(map, id, {
      kind: 'Instructor clash',
      tone: 'critical',
      title: `${row.instructor || 'Instructor'} | ${(row.sections || []).join(' / ')}`,
      detail: row.detail || '',
    }));
  });
  (conflicts.room_clashes || []).forEach(row => {
    issueRowIds(row).forEach(id => addPlacementIssue(map, id, {
      kind: 'Room clash',
      tone: 'warn',
      title: `${row.room || 'Room'} | ${(row.sections || []).join(' / ')}`,
      detail: row.detail || '',
    }));
  });
  (conflicts.cross_board || []).forEach(row => {
    const isBoardA = String(row.board_a_id) === String(boardId);
    const isBoardB = String(row.board_b_id) === String(boardId);
    if (!isBoardA && !isBoardB) return;
    const otherSection = isBoardA ? row.section_b : row.section_a;
    const otherBoard = isBoardA ? row.board_b_label : row.board_a_label;
    issueRowIds(row).forEach(id => addPlacementIssue(map, id, {
      kind: 'Student clash across boards',
      tone: 'cross',
      title: `${isBoardA ? row.section_a : row.section_b} vs ${otherSection}`,
      detail: `${row.overlap_count || 0} affected students | across boards with ${otherBoard || 'Other board'}${row.time ? ' | ' + row.time : ''}`,
    }));
  });
  return map;
}

function readinessConsoleHtml(readiness) {
  if (!readiness) {
    return `<div class="tws-builder-console loading">${IS_AR ? 'جاري فحص الجاهزية...' : 'Checking readiness...'}</div>`;
  }
  const blockers = readiness.blockers || [];
  const warnings = readiness.warnings || [];
  const ready = !!readiness.ready;
  const rows = [
    { label: IS_AR ? 'الحواجز' : 'Blockers', value: blockers.length, tone: blockers.length ? 'block' : 'ok' },
    { label: IS_AR ? 'تحذيرات' : 'Warnings', value: warnings.length, tone: warnings.length ? 'warn' : 'ok' },
    { label: IS_AR ? 'النشر' : 'Publish', value: ready ? (IS_AR ? 'جاهز' : 'Ready') : (IS_AR ? 'غير جاهز' : 'Blocked'), tone: ready ? 'ok' : 'block' },
  ];
  const details = [...blockers.slice(0, 3).map(text => ({ tone: 'block', text })), ...warnings.slice(0, 2).map(text => ({ tone: 'warn', text }))];
  return `<div class="tws-builder-console ${ready ? 'ready' : 'blocked'}">
    <div class="tws-builder-title">${IS_AR ? 'فحص الجاهزية' : 'Readiness console'}</div>
    <div class="tws-builder-kpis">
      ${rows.map(row => `<span class="${row.tone}"><b>${esc(row.value)}</b>${esc(row.label)}</span>`).join('')}
    </div>
    ${details.length
      ? `<div class="tws-builder-notes">${details.map(d => `<div class="${d.tone}">${esc(d.text)}</div>`).join('')}</div>`
      : `<div class="tws-builder-notes"><div class="ok">${IS_AR ? 'لا توجد حواجز نشر حالياً.' : 'No publish blockers right now.'}</div></div>`}
  </div>`;
}

function builderActionKey(action) {
  return [
    action?.kind || '',
    action?.board_id || '',
    action?.course_code || '',
    (action?.placement_ids || []).join(','),
    action?.title || '',
  ].join('|');
}

function actionPlacementIds(action) {
  return (action?.placement_ids || [])
    .map(v => parseInt(v))
    .filter(Number.isFinite);
}

function isRoomGuidedAction(action) {
  return ['room_clash', 'unassigned_room'].includes(action?.kind || '');
}

function isTimeGuidedAction(action) {
  return ['student_time_clash', 'instructor_clash'].includes(action?.kind || '');
}

function actionCardHtml(action, idx) {
  const ids = (action.placement_ids || []).join(',');
  const severity = action.severity || 'warn';
  const board = action.board_label ? `<span>${esc(action.board_label)}</span>` : '';
  const course = action.course_code ? `<span>${esc(action.course_code)}</span>` : '';
  const active = RP.builder.activeAction && builderActionKey(RP.builder.activeAction) === builderActionKey(action) ? ' active' : '';
  return `<div class="tws-builder-action ${severity}${active}" data-action-idx="${idx}" data-placement-ids="${esc(ids)}" data-kind="${esc(action.kind || '')}">
    <div class="tws-builder-action-main">
      <b>${esc(action.title || '')}</b>
      <em>${esc(action.cta || '')}</em>
    </div>
    <div class="tws-builder-action-detail">${esc(action.detail || '')}</div>
    <div class="tws-builder-action-meta">${board}${course}<span>${esc(action.kind || '')}</span></div>
  </div>`;
}

function renderBuilderActionsHtml(actions) {
  if (!actions) {
    return `<div class="tws-fix-empty">${IS_AR ? 'جاري ترتيب الخطوات التالية...' : 'Ranking next actions...'}</div>`;
  }
  if (!actions.length) {
    return `<div class="tws-fix-empty">${IS_AR ? 'لا توجد خطوات عاجلة.' : 'No urgent builder actions.'}</div>`;
  }
  return actions.slice(0, 8).map(actionCardHtml).join('');
}

async function ensureActionBoardVisible(action) {
  const boardId = parseInt(action?.board_id);
  if (!Number.isFinite(boardId)) return null;
  for (let i = 0; i < paneCount(); i++) {
    if (S.panes[i]?.boardId === boardId) return i;
  }
  let targetPane = -1;
  for (let i = 0; i < paneCount(); i++) {
    if (!S.panes[i]?.boardId) {
      targetPane = i;
      break;
    }
  }
  if (targetPane < 0 && Number.isFinite(S.selectedPaneIdx) && S.selectedPaneIdx >= 0 && S.selectedPaneIdx < paneCount()) {
    targetPane = S.selectedPaneIdx;
  }
  if (targetPane < 0) targetPane = 0;
  await setPaneBoard(targetPane, boardId);
  return targetPane;
}

function selectPlacementForGuidance(placementId, paneIdx, opts = {}) {
  document.querySelectorAll('.tws-pane .cell.selected').forEach(c => c.classList.remove('selected'));
  const sourceCell = document.querySelector(`.tws-pane .cell[data-placement-id="${placementId}"]`);
  if (sourceCell) {
    sourceCell.classList.add('selected');
    sourceCell.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  S.selectedPaneIdx = paneIdx;
  S.selectedPlacementId = placementId;
  if (opts.slotAssist) beginSlotAssist(paneIdx, placementId, { sticky: true });
}

function roomCodeKey(roomCode) {
  return String(roomCode || '').trim().toUpperCase();
}

function roomIsAssignable(room) {
  return !!room?.available
    && room.fits_type !== false
    && room.fits_gender !== false
    && room.fits_capacity !== false;
}

function roomIsScheduleClean(room) {
  return roomIsAssignable(room)
    && (room.schedule_clean ?? room.slot_clean) !== false
    && (room.policy_clean ?? true) !== false
    && room.department_fit !== false
    && Number(room.validation?.critical_count || 0) === 0
    && Number(room.validation?.warning_count || 0) === 0;
}

function bestRoomCandidate(roomData, currentRoom) {
  const rooms = roomData?.candidates || [];
  const current = roomCodeKey(currentRoom);
  return rooms.find(room => roomIsScheduleClean(room) && roomCodeKey(room.room_code) !== current)
    || rooms.find(room => roomIsAssignable(room) && roomCodeKey(room.room_code) !== current)
    || rooms.find(room => roomIsScheduleClean(room))
    || rooms.find(room => roomIsAssignable(room))
    || null;
}

function guidedSlotFromPlacement(placement) {
  return {
    label: 'Current slot',
    day: placement.day,
    start: placement.start_time,
    end: placement.end_time,
  };
}

function guidedSlotFromCandidate(candidate) {
  return {
    label: splitSlotLabel(candidate),
    day: candidate.day,
    start: candidate.start,
    end: candidate.end,
  };
}

async function roomCandidatesForGuidedSlot(placementId, slot) {
  const key = roomCacheKey(placementId, slot);
  if (RP.builder.roomCache[key]) return RP.builder.roomCache[key];
  const qs = new URLSearchParams({ day: slot.day, start_time: slot.start, end_time: slot.end });
  const data = await api(`/ops/tw/placements/${placementId}/room-candidates/?${qs.toString()}`);
  if (data) RP.builder.roomCache[key] = data;
  return data;
}

function guidedCandidateRank(item) {
  const c = item?.candidate || {};
  return quickFixCandidateRank(c);
}

function guidedRoomRank(item) {
  const room = item?.room || {};
  return (
    (roomIsAssignable(room) ? 100000 : 0)
    + (roomIsScheduleClean(room) ? 20000 : 0)
    - Number(room.score || 0)
  );
}

async function buildGuidedRoomResolver(action, locatedItems) {
  const options = [];
  await Promise.all(locatedItems.map(async item => {
    const slot = guidedSlotFromPlacement(item.placement);
    const roomData = await roomCandidatesForGuidedSlot(item.placement.id, slot);
    const room = bestRoomCandidate(roomData, item.placement.room);
    if (!room) return;
    options.push({
      status: 'ready',
      mode: 'room',
      action,
      placementId: item.placement.id,
      paneIdx: item.paneIdx,
      placement: item.placement,
      slot,
      room,
      roomData,
    });
  }));
  if (!options.length) {
    return {
      status: 'empty',
      mode: 'room',
      action,
      message: 'No available room candidate was found for the affected section.',
    };
  }
  options.sort((a, b) => guidedRoomRank(b) - guidedRoomRank(a));
  return options[0];
}

async function buildGuidedTimeResolver(action, locatedItems) {
  const options = [];
  await Promise.all(locatedItems.map(async item => {
    const data = await api(`/ops/tw/placements/${item.placement.id}/slot-candidates/`);
    let candidate = bestVisibleQueueCandidate(item.paneIdx, item.placement, data);
    if (!candidate) return;
    candidate = await enrichMoveCandidate(item.placement.id, item.placement, candidate);
    if (!candidateIsValidatedSafe(candidate)) return;
    const slot = guidedSlotFromCandidate(candidate);
    const roomData = await roomCandidatesForGuidedSlot(item.placement.id, slot);
    options.push({
      status: 'ready',
      mode: 'time',
      action,
      placementId: item.placement.id,
      paneIdx: item.paneIdx,
      placement: item.placement,
      candidate,
      slot,
      room: bestRoomCandidate(roomData, item.placement.room),
      roomData,
    });
  }));
  if (!options.length) {
    return {
      status: 'empty',
      mode: 'time',
      action,
      message: 'No automatically applicable improving empty slot is visible for this action. Open another board pane or review the section manually.',
    };
  }
  options.sort((a, b) => guidedCandidateRank(b) - guidedCandidateRank(a));
  return options[0];
}

function paintGuidedResolverTarget(resolver) {
  if (resolver?.mode !== 'time' || !resolver.candidate) return;
  const located = findPlacement(resolver.placementId);
  if (!located) return;
  const placement = located.placement;
  const candidate = resolver.candidate;
  beginSlotAssist(located.paneIdx, resolver.placementId, { sticky: true });
  const kind = candidate.kind || slotKindForPlacement(placement);
  const key = slotAssistKey(kind, candidate.day, candidate.start);
  const existing = S.slotAssist.candidates.get(key) || {};
  S.slotAssist.candidates.set(key, { ...existing, ...candidate });
  const targetCell = slotCellForCandidate(located.paneIdx, candidate, kind);
  if (!targetCell || targetCell.classList.contains('filled')) return;
  targetCell.dataset.slotAssistKey = key;
  targetCell.dataset.slotAssistTitle = splitSlotDetail(candidate);
  const tone = slotAssistTone(candidate);
  targetCell.classList.add(tone === 'avoid' ? 'assist-avoid' : tone === 'risky' ? 'assist-risky' : 'assist-clean');
  document.querySelectorAll('.tws-pane .cell.assist-preview').forEach(el => el.classList.remove('assist-preview'));
  targetCell.classList.add('assist-preview');
  S.slotAssist.pendingMove = {
    placementId: resolver.placementId,
    paneIdx: located.paneIdx,
    day: candidate.day,
    start: candidate.start,
    end: candidate.end,
  };
  $('twsStatusHover').textContent = `Guided fix preview ${placementShortLabel(placement)} -> ${candidate.day} ${candidate.start}-${candidate.end}: ${splitSlotDetail(candidate)}`;
}

async function focusMissingSectionAction(action) {
  toggleSidebar(true);
  SB.search = action.course_code || '';
  const search = $('twsSectionSearch');
  if (search) search.value = SB.search;
  await loadSidebarBudget();
  const actionKey = String(action.course_key || '').trim().toUpperCase();
  const actionCode = String(action.course_code || '').trim().toUpperCase();
  const target = SB.budget.find(b => {
    const sameCourse = actionKey
      ? String(courseKeyOf(b) || '').toUpperCase() === actionKey
      : (!actionCode || String(b.course_code || '').toUpperCase() === actionCode);
    const remaining = Number(b.remaining_sections ?? Math.max(0, Number(b.planned_sections || 0) - Number(b.used_sections || 0)));
    return sameCourse && remaining > 0;
  });
  if (!target) {
    notify.warning && notify.warning(`No remaining required section was found for ${action.course_code || 'this course'}`);
    return null;
  }
  const payload = createPayloadFromBudgetRow(target);
  beginCreateSectionAssist(payload);
  const required = target.required_meetings_per_section || action.required_meetings_per_section || 1;
  notify.info && notify.info(required > 1
    ? `Select a ranked full ${required}-meeting pattern for ${createAssistLabel(payload)}`
    : `Select a highlighted slot for ${createAssistLabel(payload)}`);
  return payload;
}

async function beginGuidedAction(action) {
  if (!action) return;
  const token = ++RP.builder.resolverToken;
  RP.builder.activeAction = action;
  RP.builder.resolver = { status: 'loading', action, message: 'Building a guided fix...' };

  const placementIds = actionPlacementIds(action);
  if (!placementIds.length) {
    if (action.kind === 'missing_section' && action.board_id) await ensureActionBoardVisible(action);
    if (token !== RP.builder.resolverToken) return;
    const createPayload = action.kind === 'missing_section'
      ? await focusMissingSectionAction(action)
      : null;
    RP.builder.resolver = {
      status: createPayload ? 'manual' : 'empty',
      action,
      message: action.kind === 'missing_section'
        ? ((action.requires_full_section_pattern && !createPayload)
          ? 'This course needs a complete multi-meeting section pattern; single-slot placement is disabled.'
          : createPayload
          ? 'Click a highlighted empty slot to place the missing required section.'
          : 'No remaining required section is available in the sidebar budget.')
        : 'This action needs a manual step before a placement can be selected.',
    };
    if (RP.open) renderRpanel();
    return;
  }

  await ensureActionBoardVisible(action);
  if (token !== RP.builder.resolverToken) return;

  const locatedItems = placementIds
    .map(id => {
      const located = findPlacement(id);
      return located ? { placement: located.placement, paneIdx: located.paneIdx } : null;
    })
    .filter(Boolean);
  if (!locatedItems.length) {
    RP.builder.resolver = {
      status: 'empty',
      action,
      message: 'The affected placement is not visible yet. Load its board and try again.',
    };
    if (RP.open) renderRpanel();
    return;
  }

  const first = locatedItems[0];
  selectPlacementForGuidance(first.placement.id, first.paneIdx, { slotAssist: isTimeGuidedAction(action) });
  setRpanelTab('selection');

  const resolver = isRoomGuidedAction(action)
    ? await buildGuidedRoomResolver(action, locatedItems)
    : await buildGuidedTimeResolver(action, locatedItems);
  if (token !== RP.builder.resolverToken) return;
  RP.builder.resolver = resolver;
  if (resolver.placementId) {
    selectPlacementForGuidance(resolver.placementId, resolver.paneIdx, { slotAssist: false });
    paintGuidedResolverTarget(resolver);
  }
  if (RP.open && RP.tab === 'selection') renderRpanel();
}

function guidedResolverHtml(placement) {
  const resolver = RP.builder.resolver;
  const action = RP.builder.activeAction;
  if (!action || !resolver) return '';
  const ids = actionPlacementIds(action).map(String);
  const selectedMatches = !ids.length || ids.includes(String(placement.id)) || String(resolver.placementId || '') === String(placement.id);
  if (!selectedMatches) return '';

  const title = action.title || 'Guided resolver';
  const detail = action.detail || '';
  if (resolver.status === 'loading') {
    return `<div class="tws-guided-resolver loading">
      <div class="tws-guided-head"><b>Guided resolver</b><span>working</span></div>
      <div class="tws-guided-title">${esc(title)}</div>
      <div class="tws-guided-detail">${esc(resolver.message || 'Computing the best applicable option...')}</div>
    </div>`;
  }
  if (resolver.status !== 'ready') {
    return `<div class="tws-guided-resolver warn">
      <div class="tws-guided-head"><b>Guided resolver</b><span>manual</span></div>
      <div class="tws-guided-title">${esc(title)}</div>
      <div class="tws-guided-detail">${esc(resolver.message || detail || 'No automatic fix is ready.')}</div>
    </div>`;
  }

  const slot = resolver.slot || {};
  const room = resolver.room;
  const roomClean = room ? roomIsScheduleClean(room) : false;
  const roomText = room
    ? `${room.room_code}${room.capacity ? ` | ${room.capacity} seats` : ''}${roomClean ? ' | clean schedule' : roomIsAssignable(room) ? ' | room fits' : ''}`
    : 'No room change suggested';
  const buttonText = resolver.applying ? 'Applying...' : 'Apply guided fix';
  let impactHtml = '';
  if (resolver.mode === 'time') {
    const c = resolver.candidate || {};
    const before = Number(c.current_student_affected_count || 0);
    const after = Number(c.student_affected_count || c.studentAffected || 0);
    const saved = Number(c.student_improvement || before - after || 0);
    const tags = candidateSignalTags(c).slice(0, 4);
    impactHtml = `<div class="tws-guided-metrics">
      <span><b>${before}</b> before</span>
      <span><b>${after}</b> after</span>
      <span><b>${saved > 0 ? `-${saved}` : saved}</b> students</span>
    </div>
    ${tags.length ? `<div class="tws-fix-tags">${tags.map(tag => `<span>${esc(tag)}</span>`).join('')}</div>` : ''}
    ${canBundleCandidate(c) ? `<div class="tws-fix-bundle">Bundle ${esc(placementShortLabel(c.pair.companion))} ${esc(c.pair.relation)} ${esc(c.pair.start)}-${esc(c.pair.end)}</div>` : ''}`;
  }
  return `<div class="tws-guided-resolver ready">
    <div class="tws-guided-head"><b>Guided resolver</b><span>${esc(resolver.mode === 'room' ? 'room' : 'time')}</span></div>
    <div class="tws-guided-title">${esc(title)}</div>
    <div class="tws-guided-detail">${esc(detail)}</div>
    <div class="tws-guided-steps">
      <span><b>1</b>Select ${esc(placementShortLabel(placement))}</span>
      <span><b>2</b>${esc(slot.label || 'Target')} ${esc(slot.day || '')} ${esc(slot.start || '')}-${esc(slot.end || '')}</span>
      <span><b>3</b>Room ${esc(roomText)}</span>
    </div>
    ${impactHtml}
    <button class="tws-btn primary tws-guided-apply" id="twsApplyGuidedFix" ${resolver.applying ? 'disabled' : ''}>${esc(buttonText)}</button>
  </div>`;
}

async function applyGuidedResolver() {
  const resolver = RP.builder.resolver;
  if (!resolver || resolver.status !== 'ready' || resolver.applying) return;
  resolver.applying = true;
  if (RP.open && RP.tab === 'selection') renderRpanel();
  let ok = false;
  if (resolver.mode === 'room') {
    const data = await applyRoomCandidate(resolver.placementId, resolver.room, resolver.slot);
    ok = data !== null;
  } else {
    if (!candidateIsValidatedSafe(resolver.candidate)) {
      resolver.applying = false;
      notify.warning('Guided fix paused: not validated safe. Review manually.');
      if (RP.open && RP.tab === 'selection') renderRpanel();
      return;
    }
    const regression = candidateRegressionReason(resolver.candidate);
    if (regression) {
      resolver.applying = false;
      notify.warning(`Guided fix paused: ${regression}. Review manually.`);
      if (RP.open && RP.tab === 'selection') renderRpanel();
      return;
    }
    const located = findPlacement(resolver.placementId);
    const roomCode = resolver.room?.room_code && roomCodeKey(resolver.room.room_code) !== roomCodeKey(located?.placement?.room)
      ? resolver.room.room_code
      : undefined;
    const data = await applyCandidateMove({
      placementId: resolver.placementId,
      targetPaneIdx: resolver.paneIdx,
      sourcePaneIdx: resolver.paneIdx,
      candidate: resolver.candidate,
      room: roomCode,
      auto: true,
    });
    ok = !!data;
  }
  if (ok) {
    const needsGuidedToast = resolver.mode === 'time' && !canBundleCandidate(resolver.candidate);
    RP.builder.activeAction = null;
    RP.builder.resolver = null;
    if (needsGuidedToast) notify.success('Guided fix applied');
  } else if (RP.builder.resolver) {
    RP.builder.resolver.applying = false;
    if (RP.open && RP.tab === 'selection') renderRpanel();
  }
}

async function refreshBuilderConsole(body) {
  if (!S.scenarioId || !body) return;
  const token = ++RP.builder.token;
  const readinessBox = body.querySelector('#twsReadinessConsole');
  const actionsBox = body.querySelector('#twsNextActions');
  if (readinessBox) readinessBox.innerHTML = readinessConsoleHtml(RP.builder.readiness);
  if (actionsBox) actionsBox.innerHTML = renderBuilderActionsHtml(RP.builder.actions.length ? RP.builder.actions : null);
  const [readinessData, actionsData] = await Promise.all([
    api(`/ops/tw/scenarios/${S.scenarioId}/readiness/`),
    api(`/ops/tw/scenarios/${S.scenarioId}/builder-actions/?limit=12`),
  ]);
  if (token !== RP.builder.token || !RP.open || RP.tab !== 'issues') return;
  RP.builder.readiness = readinessData?.readiness || actionsData?.readiness || null;
  RP.builder.actions = actionsData?.actions || [];
  if (readinessBox) readinessBox.innerHTML = readinessConsoleHtml(RP.builder.readiness);
  if (actionsBox) {
    actionsBox.innerHTML = renderBuilderActionsHtml(RP.builder.actions);
    const actionCount = body.querySelector('#twsNextActionsCount');
    if (actionCount) actionCount.textContent = String(RP.builder.actions.length);
    actionsBox.querySelectorAll('.tws-builder-action[data-placement-ids]').forEach(card => {
      card.addEventListener('click', () => {
        const action = RP.builder.actions[parseInt(card.dataset.actionIdx || '-1')];
        if (!action) return;
        const ids = actionPlacementIds(action);
        if (ids.length) highlightPlacements(ids);
        beginGuidedAction(action);
      });
    });
  }
}

function collectFixQueuePlacementIds(boards) {
  const ids = new Set();
  boards.forEach(b => {
    (b.conflicts.overlaps || []).forEach(o => (o.ids || []).forEach(id => ids.add(id)));
    (b.conflicts.instructor_clashes || []).forEach(o => (o.ids || []).forEach(id => ids.add(id)));
    (b.conflicts.room_clashes || []).forEach(o => (o.ids || []).forEach(id => ids.add(id)));
    (b.conflicts.cross_board || []).forEach(o => issueRowIds(o).forEach(id => ids.add(id)));
  });
  return Array.from(ids).filter(id => {
    const located = findPlacement(id);
    return located && !isPlacementProtected(located.placement);
  });
}

function bestVisibleQueueCandidate(paneIdx, placement, data) {
  const kind = slotKindForPlacement(placement);
  const localBySlot = new Map(
    buildSplitSlotCandidates(paneIdx, placement)
      .map(c => [slotAssistKey(c.kind, c.day, c.start), c])
  );
  const candidates = (data?.candidates || []).map(row => {
    const normal = normaliseStudentAwareCandidate(row);
    const local = localBySlot.get(slotAssistKey(normal.kind || kind, normal.day, normal.start)) || {};
    return { ...local, ...normal, pair: local.pair };
  });
  return candidates
    .filter(c => {
      const cell = slotCellForCandidate(paneIdx, c, c.kind || kind);
      if (!cell || cell.classList.contains('filled')) return false;
      return candidateIsValidatedSafe(c);
    })
    .sort(compareQuickFixCandidates)[0] || null;
}

function compareFixQueueItems(a, b) {
  return compareQuickFixCandidates(a.candidate, b.candidate);
}

async function enrichFixQueueItem(item) {
  const candidate = await enrichMoveCandidate(item.placementId, item.placement, item.candidate);
  return { ...item, candidate };
}

function fixQueueCardHtml(item, idx) {
  const c = item.candidate;
  const before = Number(c.current_student_affected_count || 0);
  const after = Number(c.student_affected_count || c.studentAffected || 0);
  const saved = Number(c.student_improvement || before - after);
  const critical = Number(c.critical_improvement || 0);
  const warning = Number(c.warning_improvement || 0);
  const badge = saved > 0 ? `Save ${saved}` : critical > 0 ? `Reduce critical` : warning > 0 ? 'Reduce warning' : 'Improve';
  const studentDelta = saved > 0 ? `-${saved}` : saved < 0 ? `+${Math.abs(saved)}` : '0';
  const bundle = canBundleCandidate(c)
    ? `<div class="tws-fix-bundle">${IS_AR ? 'حزمة متتالية' : 'Bundle'} ${esc(placementShortLabel(c.pair.companion))} ${esc(c.pair.relation)} ${esc(c.pair.start)}-${esc(c.pair.end)}</div>`
    : '';
  const tags = candidateSignalTags(c);
  const tagHtml = tags.length
    ? `<div class="tws-fix-tags">${tags.slice(0, 4).map(tag => `<span>${esc(tag)}</span>`).join('')}</div>`
    : '';
  const tone = slotAssistTone(c);
  return `<div class="tws-fix-card ${tone}" data-fix-idx="${idx}">
    <div class="tws-fix-main">
      <span class="tws-fix-badge">${esc(badge)}</span>
      <b>${esc(item.label)}</b>
      <span>${esc(c.day)} ${esc(c.start)}-${esc(c.end)}</span>
    </div>
    <div class="tws-fix-impact">
      <span>${IS_AR ? 'قبل' : 'Before'} <b>${before}</b></span>
      <span>${IS_AR ? 'بعد' : 'After'} <b>${after}</b></span>
      <span>${IS_AR ? 'الطلاب' : 'Students'} <b>${studentDelta}</b></span>
    </div>
    ${tagHtml}
    ${bundle}
    <div class="tws-fix-evidence">${esc(splitSlotDetail(c))}</div>
    <div class="tws-fix-card-actions">
      <button type="button" class="tws-mini-action tws-fix-preview" data-fix-idx="${idx}">Preview</button>
      <button type="button" class="tws-mini-action tws-fix-apply" data-fix-idx="${idx}">Apply</button>
    </div>
  </div>`;
}

async function refreshFixQueue(boards) {
  const container = $('twsFixQueueBody');
  if (!container) return;
  const placementIds = collectFixQueuePlacementIds(boards).slice(0, 18);
  if (!placementIds.length) {
    container.innerHTML = `<div class="tws-fix-empty">${IS_AR ? 'لا توجد مشاكل قابلة للترتيب في اللوحات المعروضة.' : 'No visible issue placements to rank.'}</div>`;
    $('twsFixQueueCount').textContent = '0';
    return;
  }

  const token = ++RP.fixQueue.token;
  container.innerHTML = `<div class="tws-fix-empty">${IS_AR ? 'جاري حساب أفضل النقلات...' : 'Calculating best moves...'}</div>`;
  await Promise.all(placementIds.map(async id => {
    if (RP.fixQueue.cache[id]) return;
    const data = await api(`/ops/tw/placements/${id}/slot-candidates/`);
    if (data) RP.fixQueue.cache[id] = data;
  }));
  if (token !== RP.fixQueue.token) return;

  const preliminaryItems = [];
  placementIds.forEach(id => {
    const located = findPlacement(id);
    const data = RP.fixQueue.cache[id];
    if (!located || !data) return;
    const candidate = bestVisibleQueueCandidate(located.paneIdx, located.placement, data);
    if (!candidate) return;
    preliminaryItems.push({
      placementId: id,
      paneIdx: located.paneIdx,
      label: placementShortLabel(located.placement),
      placement: located.placement,
      candidate,
    });
  });

  preliminaryItems.sort(compareFixQueueItems);
  const items = [];
  for (const item of preliminaryItems.slice(0, 12)) {
    if (token !== RP.fixQueue.token) return;
    const enriched = await enrichFixQueueItem(item);
    if (candidateIsHardRegression(enriched.candidate)) continue;
    if (!candidateHasUsefulImprovement(enriched.candidate)) continue;
    items.push(enriched);
  }
  items.sort(compareFixQueueItems);
  const seenMoveKeys = new Set();
  const dedupedItems = [];
  items.forEach(item => {
    const key = [
      item.label,
      item.candidate.kind || slotKindForPlacement(item.placement),
      item.candidate.day,
      item.candidate.start,
    ].join('|');
    if (seenMoveKeys.has(key)) return;
    seenMoveKeys.add(key);
    dedupedItems.push(item);
  });
  RP.fixQueue.items = dedupedItems.slice(0, 6);
  $('twsFixQueueCount').textContent = String(RP.fixQueue.items.length);
  if (!RP.fixQueue.items.length) {
    container.innerHTML = `<div class="tws-fix-empty">${IS_AR ? 'لا توجد نقلة محسنة إلى خانة فارغة حالياً.' : 'No improving move to an empty visible slot right now.'}</div>`;
    return;
  }
  container.innerHTML = RP.fixQueue.items.map(fixQueueCardHtml).join('');
  container.querySelectorAll('.tws-fix-card').forEach(card => {
    card.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      previewFixQueueItem(parseInt(card.dataset.fixIdx));
    });
  });
  container.querySelectorAll('.tws-fix-preview').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      previewFixQueueItem(parseInt(btn.dataset.fixIdx));
    });
  });
  container.querySelectorAll('.tws-fix-apply').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      await applyFixQueueItem(parseInt(btn.dataset.fixIdx));
    });
  });
}

function previewFixQueueItem(idx) {
  const item = RP.fixQueue.items[idx];
  if (!item) return;
  document.querySelectorAll('.tws-pane .cell.selected').forEach(c => c.classList.remove('selected'));
  const sourceCell = document.querySelector(`.tws-pane .cell[data-placement-id="${item.placementId}"]`);
  if (sourceCell) sourceCell.classList.add('selected');
  S.selectedPaneIdx = item.paneIdx;
  S.selectedPlacementId = item.placementId;
  beginSlotAssist(item.paneIdx, item.placementId, { sticky: true });
  const key = slotAssistKey(item.candidate.kind || slotKindForPlacement(item.placement), item.candidate.day, item.candidate.start);
  const existing = S.slotAssist.candidates.get(key) || {};
  S.slotAssist.candidates.set(key, { ...existing, ...item.candidate });
  const targetCell = slotCellForCandidate(item.paneIdx, item.candidate, item.candidate.kind || slotKindForPlacement(item.placement));
  if (targetCell) {
    targetCell.dataset.slotAssistKey = key;
    targetCell.dataset.slotAssistTitle = splitSlotDetail(item.candidate);
    const tone = slotAssistTone(item.candidate);
    targetCell.classList.add(tone === 'avoid' ? 'assist-avoid' : tone === 'risky' ? 'assist-risky' : 'assist-clean');
    previewSelectedMoveToSlot(item.paneIdx, targetCell);
  }
  if (sourceCell) sourceCell.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function applyFixQueueItem(idx) {
  const item = RP.fixQueue.items[idx];
  if (!item) return;
  if (!candidateIsValidatedSafe(item.candidate)) {
    notify.warning('Move paused: not validated safe. Preview it manually first.');
    previewFixQueueItem(idx);
    return;
  }
  const regression = candidateRegressionReason(item.candidate);
  if (regression) {
    notify.warning(`Move paused: ${regression}. Preview it manually first.`);
    previewFixQueueItem(idx);
    return;
  }
  const current = findPlacement(item.placementId);
  if (!current || isPlacementProtected(current.placement)) {
    notify.warning('This section is no longer available for quick apply.');
    return;
  }
  const targetCell = slotCellForCandidate(
    current.paneIdx,
    item.candidate,
    item.candidate.kind || slotKindForPlacement(current.placement)
  );
  if (!targetCell || targetCell.classList.contains('filled')) {
    notify.warning('Target slot is no longer empty. Recalculate the queue.');
    return;
  }
  const result = await applyCandidateMove({
    placementId: item.placementId,
    targetPaneIdx: current.paneIdx,
    sourcePaneIdx: current.paneIdx,
    candidate: item.candidate,
    auto: true,
  });
  if (result) {
    notify.success(`Applied ${item.label} to ${item.candidate.day} ${item.candidate.start}-${item.candidate.end}`);
    await refreshBoardsSummary();
  }
}

function confirmAutoFixModal({ boardLabel, moves, bundleCount }) {
  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const rows = moves.map(item => {
      const c = item.candidate;
      const saved = Number(c.student_improvement || 0);
      const bundle = canBundleCandidate(c) ? ` + ${placementShortLabel(c.pair.companion)}` : '';
      return `<div class="tws-auto-row">
        <b>${esc(item.label)}${esc(bundle)}</b>
        <span>${esc(c.day)} ${esc(c.start)}-${esc(c.end)}</span>
        <em>${saved > 0 ? `save ${saved}` : 'improve'}</em>
      </div>`;
    }).join('');
    openModal({
      title: IS_AR ? 'إصلاح لوحة واحدة' : 'Auto-fix one board',
      sub: boardLabel,
      body: `<div class="tws-auto-confirm">
        <p>${IS_AR ? 'سيتم تطبيق النقلات التالية على اللوحة فقط. يمكن التراجع عنها من زر التراجع.' : `This will apply ${moves.length} move(s) on this board only. Undo remains available after applying.`}</p>
        ${bundleCount ? `<p class="note">${bundleCount} ${IS_AR ? 'حزمة متتالية' : 'back-to-back bundle(s)'}.</p>` : ''}
        ${rows}
      </div>`,
      buttons: [
        { label: IS_AR ? 'إلغاء' : 'Cancel', onClick: () => finish(false) },
        { label: IS_AR ? 'تطبيق' : 'Apply moves', variant: 'primary', onClick: () => finish(true) },
      ],
      onClose: () => finish(false),
    });
  });
}

const BULK_ROOM_ASSIGN_LIMIT = 20;

async function collectCleanRoomAssignments(limit = BULK_ROOM_ASSIGN_LIMIT) {
  const data = await api(`/ops/tw/scenarios/${S.scenarioId}/clean-room-assignments/?limit=${encodeURIComponent(limit)}`);
  const items = (data?.assignments || []).map(item => ({
    ...item,
    placementId: item.placement_id,
    boardId: item.board_id,
    boardLabel: item.board_label || `Board ${item.board_id}`,
    slot: item.slot || { day: item.day, start: item.start, end: item.end },
  }));
  return {
    items,
    totalActions: Number(data?.total_unassigned || items.length || 0),
    skipped: data?.skipped || [],
  };
}

function confirmBulkRoomModal({ items, totalActions }) {
  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const rows = items.map(item => `<div class="tws-auto-row">
      <b>${esc(item.label)}</b>
      <span>${esc(item.boardLabel)} · ${esc(item.slot.day)} ${esc(item.slot.start)}-${esc(item.slot.end)}</span>
      <em>${esc(item.room.room_code)}</em>
    </div>`).join('');
    const more = Math.max(0, totalActions - items.length);
    openModal({
      title: IS_AR ? 'Assign clean rooms' : 'Assign clean rooms',
      sub: `${items.length} clean assignment${items.length === 1 ? '' : 's'}`,
      body: `<div class="tws-auto-confirm">
        <p>${IS_AR ? 'This applies only room candidates that fit requirements and have a clean schedule. Undo remains available for each assignment.' : 'This applies only room candidates that fit requirements and have a clean schedule. Undo remains available for each assignment.'}</p>
        ${more ? `<p class="note">${more} more room action(s) can be handled in another batch.</p>` : ''}
        ${rows}
      </div>`,
      buttons: [
        { label: IS_AR ? 'Cancel' : 'Cancel', onClick: () => finish(false) },
        { label: IS_AR ? 'Assign rooms' : 'Assign rooms', variant: 'primary', onClick: () => finish(true) },
      ],
      onClose: () => finish(false),
    });
  });
}

async function bulkAssignCleanRooms() {
  if (!S.scenarioId) return;
  const btn = $('twsBulkCleanRooms');
  if (btn) btn.disabled = true;
  let items = [];
  let totalActions = 0;
  try {
    if (btn) btn.textContent = IS_AR ? 'Checking...' : 'Checking...';
    const result = await collectCleanRoomAssignments();
    items = result.items || [];
    totalActions = result.totalActions || 0;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = IS_AR ? 'Assign clean rooms' : 'Assign clean rooms';
    }
  }
  if (!items.length) {
    notify.warning(IS_AR ? 'No clean room assignments are ready.' : 'No clean room assignments are ready.');
    return;
  }
  const ok = await confirmBulkRoomModal({ items, totalActions });
  if (!ok) return;

  if (btn) btn.disabled = true;
  let data = null;
  try {
    data = await api(`/ops/tw/scenarios/${S.scenarioId}/clean-room-assignments/apply/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: BULK_ROOM_ASSIGN_LIMIT }),
    });
  } finally {
    if (btn) btn.disabled = false;
  }
  const appliedRows = data?.applied || [];
  const applied = Number(data?.applied_count || appliedRows.length || 0);
  const touchedBoards = new Set(appliedRows.map(row => Number(row.board_id)).filter(Boolean));
  if (appliedRows.length) {
    S.undoStack.push({
      type: 'bundle_move',
      label: 'bulk_clean_rooms',
      moves: appliedRows.map(row => ({
        type: 'move',
        placement_id: row.placement_id,
        old_day: row.old_day || row.day,
        old_start: row.old_start || row.start,
        old_end: row.old_end || row.end,
        old_room: row.old_room || '',
        new_day: row.new_day || row.day,
        new_start: row.new_start || row.start,
        new_end: row.new_end || row.end,
        new_room: row.new_room || row.room?.room_code || '',
      })),
    });
    S.redoStack = [];
    updateUndoRedoButtons();
  }
  RP.builder.roomCache = {};
  RP.builder.actions = [];
  for (let i = 0; i < paneCount(); i += 1) {
    if (touchedBoards.has(Number(S.panes[i].boardId))) await loadAndRenderPane(i);
  }
  await refreshBoardsSummary();
  await loadSidebarBudget();
  if (RP.open && RP.tab === 'issues') renderRpanel();
  if (applied) {
    notify.success(`Assigned ${applied} clean room${applied === 1 ? '' : 's'}`);
  } else {
    notify.warning(IS_AR ? 'No rooms were assigned.' : 'No rooms were assigned.');
  }
}

async function autoFixOneBoard() {
  if (!S.scenarioId) return;
  const selectedBoardId = Number.isFinite(S.selectedPaneIdx)
    ? Number(S.panes[S.selectedPaneIdx]?.boardId || 0)
    : 0;
  const seed = RP.fixQueue.items.find(item => (
    selectedBoardId && Number(S.panes[item.paneIdx]?.boardId || 0) === selectedBoardId
  )) || RP.fixQueue.items[0];
  const boardId = selectedBoardId || Number(seed ? S.panes[seed.paneIdx]?.boardId || 0 : 0);
  if (!boardId) {
    notify.warning(IS_AR ? 'No board selected.' : 'No board is selected.');
    return;
  }
  const boardLabel = (S.boards.find(b => Number(b.id) === Number(boardId)) || {}).label || `Board ${boardId}`;
  const btn = $('twsAutoFixBoard');
  if (btn) btn.disabled = true;
  let preview = null;
  try {
    const qs = new URLSearchParams({ board_id: String(boardId), limit: '3' });
    preview = await api(`/ops/tw/scenarios/${S.scenarioId}/safe-time-moves/?${qs.toString()}`);
  } finally {
    if (btn) btn.disabled = false;
  }
  const moves = (preview?.moves || []).map(row => ({
    ...row,
    placementId: row.placement_id,
    boardId: row.board_id,
    label: row.label || `${row.course_code || 'Course'}-${row.section || ''}`,
    candidate: row.candidate || {
      day: row.new_day,
      start: row.new_start,
      end: row.new_end,
      student_improvement: 0,
    },
  }));
  if (!moves.length) {
    notify.warning(IS_AR ? 'No clean safe moves are ready for this board.' : 'No clean safe moves are ready for this board.');
    return;
  }
  const bundleCount = moves.filter(item => canBundleCandidate(item.candidate)).length;
  const ok = await confirmAutoFixModal({ boardLabel, moves, bundleCount });
  if (!ok) return;

  if (btn) btn.disabled = true;
  let result = null;
  try {
    result = await api(`/ops/tw/scenarios/${S.scenarioId}/safe-time-moves/apply/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ board_id: boardId, limit: 3 }),
    });
  } finally {
    if (btn) btn.disabled = false;
  }
  const appliedRows = result?.applied || [];
  const applied = Number(result?.applied_count || appliedRows.length || 0);
  if (appliedRows.length) {
    S.undoStack.push({
      type: 'bundle_move',
      label: 'bulk_safe_time_moves',
      moves: appliedRows.map(row => ({
        type: 'move',
        placement_id: row.placement_id,
        old_day: row.old_day,
        old_start: row.old_start,
        old_end: row.old_end,
        old_room: row.old_room || '',
        new_day: row.new_day,
        new_start: row.new_start,
        new_end: row.new_end,
        new_room: row.new_room || '',
      })),
    });
    S.redoStack = [];
    updateUndoRedoButtons();
  }
  RP.fixQueue.cache = {};
  RP.fixQueue.items = [];
  for (let i = 0; i < paneCount(); i += 1) {
    if (Number(S.panes[i].boardId) === Number(boardId)) await loadAndRenderPane(i);
  }
  if (applied) {
    notify.success(IS_AR ? `Applied ${applied} move(s)` : `Auto-fix applied ${applied} move(s)`);
  } else {
    notify.warning(IS_AR ? 'No moves were applied.' : 'No moves were applied.');
  }
  await refreshBoardsSummary();
  if (RP.open && RP.tab === 'issues') renderRpanel();
}

async function autoFixOneBoardClientFallback() {
  if (!RP.fixQueue.items.length) {
    notify.warning(IS_AR ? 'لا توجد نقلات مقترحة' : 'No suggested moves ready');
    return;
  }
  const selectedBoardId = Number.isFinite(S.selectedPaneIdx) ? S.panes[S.selectedPaneIdx]?.boardId : null;
  const seed = RP.fixQueue.items.find(item => selectedBoardId && S.panes[item.paneIdx]?.boardId === selectedBoardId)
    || RP.fixQueue.items[0];
  const boardId = S.panes[seed.paneIdx]?.boardId;
  const boardLabel = (S.boards.find(b => b.id === boardId) || {}).label || `Pane ${seed.paneIdx + 1}`;
  const moves = RP.fixQueue.items
    .filter(item => S.panes[item.paneIdx]?.boardId === boardId)
    .filter(item => !isPlacementProtected(item.placement))
    .filter(item => !candidateIsHardRegression(item.candidate))
    .filter(item => candidateHasUsefulImprovement(item.candidate))
    .slice(0, 3);
  if (!moves.length) {
    notify.warning(IS_AR ? 'كل النقلات لهذه اللوحة محمية' : 'All moves on this board are protected');
    return;
  }
  const bundleCount = moves.filter(item => canBundleCandidate(item.candidate)).length;
  const ok = await confirmAutoFixModal({ boardLabel, moves, bundleCount });
  if (!ok) return;

  const btn = $('twsAutoFixBoard');
  if (btn) btn.disabled = true;
  let applied = 0;
  try {
    for (const item of moves) {
      const current = findPlacement(item.placementId);
      if (!current || isPlacementProtected(current.placement)) continue;
      if (candidateIsHardRegression(item.candidate)) continue;
      const targetCell = slotCellForCandidate(current.paneIdx, item.candidate, item.candidate.kind || slotKindForPlacement(current.placement));
      if (!targetCell || targetCell.classList.contains('filled')) continue;
      const result = await applyCandidateMove({
        placementId: item.placementId,
        targetPaneIdx: current.paneIdx,
        sourcePaneIdx: current.paneIdx,
        candidate: item.candidate,
        auto: true,
      });
      if (result) applied += 1;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
  if (applied) {
    notify.success(IS_AR ? `تم تطبيق ${applied} نقلة` : `Auto-fix applied ${applied} move(s)`);
  } else {
    notify.warning(IS_AR ? 'لم يتم تطبيق أي نقلة' : 'No moves were applied');
  }
  await refreshBoardsSummary();
}

function renderRpanelIssues(body) {
  const boards = aggregateVisibleBoardsData();
  if (!boards.length) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">ⓘ</span>${IS_AR ? 'لم يتم تحميل أي لوحة' : 'No boards loaded yet'}</div>`;
    $('twsRpCountIssues').textContent = '0';
    return;
  }
  const overlaps = [];
  const iClashes = [];
  const rClashes = [];
  const crossBoard = [];
  boards.forEach(b => {
    (b.conflicts.overlaps || []).forEach(o => overlaps.push({ ...o, _paneIdx: b.paneIdx, _boardLabel: b.boardLabel }));
    (b.conflicts.instructor_clashes || []).forEach(o => iClashes.push({ ...o, _paneIdx: b.paneIdx, _boardLabel: b.boardLabel }));
    (b.conflicts.room_clashes || []).forEach(o => rClashes.push({ ...o, _paneIdx: b.paneIdx, _boardLabel: b.boardLabel }));
    (b.conflicts.cross_board || []).forEach(o => {
      // Dedupe by the exact placement pair so the same cross-board clash is
      // never listed twice when both boards are visible.
      const ids = issueRowIds(o).map(String).sort();
      const key = ids.length === 2
        ? ids.join(':')
        : [o.board_a_id, o.board_b_id].sort().join('-') + ':' + (o.section_a || '') + (o.section_b || '');
      if (!crossBoard.some(x => x._key === key)) crossBoard.push({ ...o, _key: key });
    });
  });
  const total = overlaps.length + iClashes.length + rClashes.length + crossBoard.length;
  $('twsRpCountIssues').textContent = String(total);
  if (total === 0) {
    body.innerHTML = `<div id="twsReadinessConsole">${readinessConsoleHtml(RP.builder.readiness)}</div>
      <div class="tws-fix-actions">
        <button class="tws-mini-action" id="twsBulkCleanRooms" type="button">${IS_AR ? 'Assign clean rooms' : 'Assign clean rooms'}</button>
      </div>
      <div class="section-head fix">${IS_AR ? 'الخطوات التالية' : 'Next best actions'}<span class="n" id="twsNextActionsCount">...</span></div>
      <div id="twsNextActions" class="tws-builder-actions">${renderBuilderActionsHtml(RP.builder.actions.length ? RP.builder.actions : null)}</div>
      <div class="tws-empty-state"><span class="ic">✓</span>${IS_AR ? 'لا توجد مشاكل' : 'No issues'}</div>`;
    body.querySelector('#twsBulkCleanRooms')?.addEventListener('click', bulkAssignCleanRooms);
    refreshBuilderConsole(body);
    return;
  }
  const issueRow = (kind, title, meta, ids, paneIdx) => {
    const pane = Number.isFinite(paneIdx) ? `<span class="pane-badge">P${paneIdx + 1}</span>` : '';
    return `<div class="tws-issue" data-ids='${JSON.stringify(ids || [])}' data-pane="${paneIdx ?? ''}">
      <span class="dot ${kind}"></span>
      <div class="body">
        <div class="title">${esc(title)}${pane}</div>
        <div class="meta">${esc(meta || '')}</div>
      </div>
    </div>`;
  };
  const crossRow = (c) => {
    const panesInvolved = S.panes.reduce((acc, p, i) => {
      if (p.boardId === c.board_a_id || p.boardId === c.board_b_id) acc.push(i + 1);
      return acc;
    }, []);
    const paneBadges = panesInvolved.map(n => `<span class="pane-badge">P${n}</span>`).join(' ');
    const ids = issueRowIds(c);
    const count = Number(c.overlap_count || c.affected_student_count || 0);
    const headline = `${count} ${count === 1 ? 'Student Clash' : 'Student Clashes'}`;
    const timeParts = String(c.time || '').split(' vs ');
    const timeA = c.time_a || timeParts[0] || '';
    const timeB = c.time_b || timeParts[1] || '';
    const students = Array.isArray(c.affected_students) ? c.affected_students : [];
    const visibleStudents = students.slice(0, 6);
    const studentChips = students.slice(0, 6).map(s => `<span>${esc(s.student_id)} · ${esc(s.program || '')}${s.primary_term ? ` T${esc(s.primary_term)}` : ''}${s.section ? ` · ${esc(s.section)}` : ''}</span>`).join('');
    const hiddenStudentCount = Math.max(0, count - visibleStudents.length);
    const moreStudents = hiddenStudentCount ? `<em>+${hiddenStudentCount} more</em>` : '';
    const crossPayload = { a: c.board_a_id, b: c.board_b_id, sa: c.section_a, sb: c.section_b, ids };
    const fixPayloadA = {
      a: c.board_a_id,
      b: c.board_b_id,
      board_id: c.board_a_id,
      placement_id: c.placement_a_id,
      placement_ids: ids,
      title: headline,
      detail: `${c.section_a} vs ${c.section_b} | ${count} affected students`,
    };
    const fixPayloadB = {
      a: c.board_a_id,
      b: c.board_b_id,
      board_id: c.board_b_id,
      placement_id: c.placement_b_id,
      placement_ids: ids,
      title: headline,
      detail: `${c.section_a} vs ${c.section_b} | ${count} affected students`,
    };
    return `<div class="tws-issue tws-student-clash" data-cross='${esc(JSON.stringify(crossPayload))}'>
      <span class="dot cross"></span>
      <div class="body">
        <div class="title"><b>${esc(headline)}</b>${paneBadges}</div>
        <div class="tws-clash-pair">${esc(c.section_a)} vs ${esc(c.section_b)}</div>
        <div class="meta">${esc(c.board_a_label || '')} ${esc(timeA)} vs ${esc(c.board_b_label || '')} ${esc(timeB)}</div>
        <div class="tws-student-preview">${studentChips}${moreStudents}</div>
        <div class="tws-clash-actions">
          <button type="button" class="tws-mini-action tws-cross-fix" data-cross-fix='${esc(JSON.stringify(fixPayloadA))}'>Review move ${esc(c.section_a || 'A')}</button>
          <button type="button" class="tws-mini-action tws-cross-fix" data-cross-fix='${esc(JSON.stringify(fixPayloadB))}'>Review move ${esc(c.section_b || 'B')}</button>
        </div>
      </div>
    </div>`;
  };

  let html = `<div id="twsReadinessConsole">${readinessConsoleHtml(RP.builder.readiness)}</div>
    <div class="section-head fix">${IS_AR ? 'الخطوات التالية' : 'Next best actions'}<span class="n" id="twsNextActionsCount">...</span></div>
    <div id="twsNextActions" class="tws-builder-actions">${renderBuilderActionsHtml(RP.builder.actions.length ? RP.builder.actions : null)}</div>
    <div class="section-head fix">${IS_AR ? 'قائمة الإصلاح' : 'Fix queue'}<span class="n" id="twsFixQueueCount">...</span></div>
    <div class="tws-fix-actions">
      <button class="tws-mini-action" id="twsAutoFixBoard" type="button">${IS_AR ? 'إصلاح لوحة واحدة' : 'Auto-fix one board'}</button>
      <button class="tws-mini-action" id="twsBulkCleanRooms" type="button">${IS_AR ? 'Assign clean rooms' : 'Assign clean rooms'}</button>
      <button class="tws-mini-action" id="twsClearProtections" type="button">${IS_AR ? 'مسح الحماية' : 'Clear protections'}</button>
    </div>
    <div id="twsFixQueueBody" class="tws-fix-queue">
      <div class="tws-fix-empty">${IS_AR ? 'جاري حساب أفضل النقلات...' : 'Calculating best moves...'}</div>
    </div>`;
  if (overlaps.length) {
    html += `<div class="section-head danger">${IS_AR ? 'تعارض زمني داخل اللوحة' : 'Same-board time clash'}<span class="n">${overlaps.length}</span></div>`;
    html += overlaps.map(o => issueRow('critical', (o.sections || []).join(' / '), `${o._boardLabel}${o.detail ? ' · ' + o.detail : ''}`, o.ids, o._paneIdx)).join('');
  }
  if (iClashes.length) {
    html += `<div class="section-head danger">${IS_AR ? 'تعارض مدرس' : 'Instructor clash'}<span class="n">${iClashes.length}</span></div>`;
    html += iClashes.map(o => issueRow('critical', `${o.instructor || '—'} · ${(o.sections || []).join(' / ')}`, `${o._boardLabel}${o.detail ? ' · ' + o.detail : ''}`, o.ids, o._paneIdx)).join('');
  }
  if (rClashes.length) {
    html += `<div class="section-head warn">${IS_AR ? 'تعارض قاعة' : 'Room clash'}<span class="n">${rClashes.length}</span></div>`;
    html += rClashes.map(o => issueRow('warn', `${o.room || '—'} · ${(o.sections || []).join(' / ')}`, `${o._boardLabel}${o.detail ? ' · ' + o.detail : ''}`, o.ids, o._paneIdx)).join('');
  }
  if (crossBoard.length) {
    html += `<div class="section-head cross">${IS_AR ? 'تعارض طلاب عبر اللوحات' : 'Cross-board student clashes'}<span class="n">${crossBoard.length}</span></div>`;
    html += crossBoard.map(crossRow).join('');
  }
  body.innerHTML = html;
  body.querySelector('#twsAutoFixBoard')?.addEventListener('click', autoFixOneBoard);
  body.querySelector('#twsBulkCleanRooms')?.addEventListener('click', bulkAssignCleanRooms);
  body.querySelector('#twsClearProtections')?.addEventListener('click', () => {
    resetProtections();
    saveProtections();
    RP.fixQueue.cache = {};
    RP.fixQueue.items = [];
    renderRpanel();
    notify.success(IS_AR ? 'تم مسح الحماية' : 'Protections cleared');
  });
  refreshFixQueue(boards);
  refreshBuilderConsole(body);

  // Wire issue row clicks
  body.querySelectorAll('.tws-issue[data-ids]').forEach(row => {
    row.addEventListener('click', () => highlightPlacements(JSON.parse(row.dataset.ids)));
  });
  body.querySelectorAll('.tws-issue[data-cross]').forEach(row => {
    row.addEventListener('click', () => {
      const c = JSON.parse(row.dataset.cross);
      handleCrossBoardClick(c);
    });
  });
  body.querySelectorAll('.tws-cross-fix[data-cross-fix]').forEach(btn => {
    btn.addEventListener('click', async ev => {
      ev.stopPropagation();
      await beginCrossBoardStudentFix(JSON.parse(btn.dataset.crossFix));
    });
  });
}
function highlightPlacements(ids) {
  document.querySelectorAll('.tws-pane .cell.highlight').forEach(c => c.classList.remove('highlight'));
  let firstCell = null;
  ids.forEach(id => {
    document.querySelectorAll(`.tws-pane .cell[data-placement-id="${id}"]`).forEach(c => {
      c.classList.add('highlight');
      if (!firstCell) firstCell = c;
    });
  });
  if (firstCell) firstCell.scrollIntoView({ behavior: 'smooth', block: 'center' });
  // Auto-clear after 3s
  setTimeout(() => document.querySelectorAll('.tws-pane .cell.highlight').forEach(c => c.classList.remove('highlight')), 3000);
}
async function handleCrossBoardClick(c) {
  // Identify panes showing these boards. If neither is on canvas, load
  // board B into the first empty (or last) pane so user sees both sides.
  const inA = S.panes.findIndex(p => p.boardId === c.a);
  const inB = S.panes.findIndex(p => p.boardId === c.b);
  // Only consider visible panes when looking for an empty slot.
  const visible = S.panes.slice(0, paneCount());
  const lastVisible = paneCount() - 1;
  if (inA < 0 && inB < 0) {
    // Load both into the first two panes
    await setPaneBoard(0, c.a);
    if (paneCount() > 1) await setPaneBoard(1, c.b);
    notify.info && notify.info(IS_AR ? 'تم تحميل اللوحتين' : 'Loaded both boards');
  } else if (inA < 0) {
    const empty = visible.findIndex(p => !p.boardId);
    await setPaneBoard(empty >= 0 ? empty : lastVisible, c.a);
  } else if (inB < 0) {
    const empty = visible.findIndex(p => !p.boardId);
    await setPaneBoard(empty >= 0 ? empty : lastVisible, c.b);
  }
  if (c.ids && c.ids.length) highlightPlacements(c.ids);
  // Scroll into pane A view
  const pi = S.panes.findIndex(p => p.boardId === c.a);
  if (pi >= 0) paneEl(pi)?.scrollIntoView({ block: 'center' });
}

async function beginCrossBoardStudentFix(action) {
  if (!action?.placement_id) return;
  await handleCrossBoardClick({
    a: action.a,
    b: action.b,
    ids: action.placement_ids || [action.placement_id],
  });
  await ensureActionBoardVisible({ board_id: action.board_id });
  if (action.placement_ids?.length) highlightPlacements(action.placement_ids);
  const located = findPlacement(action.placement_id);
  if (!located) {
    notify.warning && notify.warning(IS_AR ? 'حمّل اللوحة المتأثرة أولاً' : 'Load the affected board first');
    return;
  }
  if (isPlacementProtected(located.placement)) {
    notify.warning && notify.warning(`${placementShortLabel(located.placement)} ${IS_AR ? 'محمي' : 'is protected'}`);
    return;
  }
  await beginGuidedAction({
    kind: 'student_time_clash',
    severity: 'block',
    title: action.title || (IS_AR ? 'تعارض طلاب عبر اللوحات' : 'Student clash across boards'),
    detail: action.detail || (IS_AR ? 'راجع النقلات المرشحة. يظهر تطبيق مباشر فقط عند عدم وجود تراجع.' : 'Review candidate moves. Direct apply appears only when no regression is found.'),
    cta: IS_AR ? 'راجع النقلات' : 'Review moves',
    board_id: action.board_id,
    placement_ids: [action.placement_id],
  });
}

function setStudentBlockersCount(value) {
  const el = $('twsRpCountStudents');
  if (el) el.textContent = String(value || 0);
}

function renderRpanelStudentBlockers(body) {
  if (!S.scenarioId) {
    setStudentBlockersCount(0);
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">ⓘ</span>${IS_AR ? 'اختر سيناريو أولاً' : 'Select a scenario first'}</div>`;
    return;
  }
  const scenarioId = S.scenarioId;
  if (RP.studentBlockers.scenarioId === scenarioId && RP.studentBlockers.data) {
    renderStudentBlockersNow(body, RP.studentBlockers.data);
    return;
  }
  const token = ++RP.studentBlockers.token;
  body.innerHTML = `<div class="tws-fix-empty">${IS_AR ? 'جاري حساب تعارضات الطلاب الفعلية...' : 'Calculating actual student blockers...'}</div>`;
  api(`/ops/tw/scenarios/${scenarioId}/student-blockers/`).then(data => {
    if (!data || token !== RP.studentBlockers.token || S.scenarioId !== scenarioId) return;
    RP.studentBlockers.scenarioId = scenarioId;
    RP.studentBlockers.data = data;
    if (RP.open && RP.tab === 'students') renderStudentBlockersNow($('twsRpanelBody'), data);
  });
}

function renderStudentBlockersNow(body, data) {
  if (!body) return;
  const summary = data?.summary || {};
  const courses = data?.courses || [];
  setStudentBlockersCount(summary.issue_students || summary.blocked_students || 0);
  if (!data?.available) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">ⓘ</span>${IS_AR ? 'لا توجد بيانات طلاب كافية' : 'No student assignment data available'}</div>`;
    return;
  }
  if (!courses.length) {
    body.innerHTML = `<div class="tws-blocker-totals">
      <div class="m"><b>0</b><span>${IS_AR ? 'طلاب عالقون' : 'blocked students'}</span></div>
      <div class="m"><b>${Number(summary.actual_assigned_clashes || 0)}</b><span>${IS_AR ? 'تعارضات فعلية' : 'assigned clashes'}</span></div>
    </div>
    <div class="tws-empty-state"><span class="ic">✓</span>${IS_AR ? 'لا توجد تعارضات طلاب فعلية' : 'No actual student blockers'}</div>`;
    return;
  }
  const rows = courses.slice(0, 60).map(studentBlockerCourseHtml).join('');
  body.innerHTML = `<div class="tws-blocker-totals">
    <div class="m"><b>${Number(summary.blocked_students || 0)}</b><span>${IS_AR ? 'طلاب عالقون' : 'blocked students'}</span></div>
    <div class="m"><b>${Number(summary.blocked_course_requests || summary.unresolved_courses || 0)}</b><span>${IS_AR ? 'طلبات عالقة' : 'blocked requests'}</span></div>
    <div class="m"><b>${Number(summary.actual_assigned_clashes || 0)}</b><span>${IS_AR ? 'تعارضات مسجلة' : 'assigned clashes'}</span></div>
    <div class="m"><b>${Number(summary.course_count || courses.length)}</b><span>${IS_AR ? 'مقررات' : 'courses'}</span></div>
  </div>
  <div class="section-head cross">${IS_AR ? 'المقررات حسب الطلاب المتأثرين' : 'Courses by actual student impact'}<span class="n">${courses.length}</span></div>
  ${rows}
  ${courses.length > 60 ? `<div class="tws-empty-state" style="padding:8px 0">+${courses.length - 60} more</div>` : ''}`;

  body.querySelectorAll('.tws-blocker-course').forEach(row => {
    row.addEventListener('click', () => {
      body.querySelectorAll('.tws-blocker-course.active').forEach(el => el.classList.remove('active'));
      row.classList.add('active');
      RP.studentBlockers.activeCourse = row.dataset.courseKey || '';
      let ids = [];
      try { ids = JSON.parse(row.dataset.placementIds || '[]'); } catch {}
      const visible = highlightCoursePlacements(row.dataset.courseKey || '', ids);
      const label = row.dataset.courseCode || row.dataset.courseKey || 'course';
      const students = Number(row.dataset.students || 0);
      const requests = Number(row.dataset.requests || 0);
      $('twsStatusHover').textContent = visible
        ? `${label}: ${students} students, ${requests} blocked request(s). Highlighted ${visible} visible placement(s).`
        : `${label}: ${students} students, ${requests} blocked request(s). No visible placement for this course on the canvas.`;
    });
  });
}

function studentBlockerCourseHtml(row) {
  const reasonCounts = row.reason_counts || {};
  const allClash = Number(reasonCounts.all_clash || 0);
  const mixed = Number(reasonCounts.mixed_blockers || 0);
  const full = Number(reasonCounts.full || 0);
  const reserve = Number(reasonCounts.reserve_only || 0);
  const assignedClashStudents = Number(row.assigned_clash_student_count || 0);
  const samples = (row.sample_students || []).slice(0, 4)
    .map(s => `<span>${esc(s.student_id)}${s.program ? ` · ${esc(s.program)}` : ''}${s.risk_tier ? ` · ${esc(s.risk_tier)}` : ''}</span>`)
    .join('');
  const meta = [
    `<span class="danger" title="Every current section for this course clashes with the student's assigned timetable.">all clash ${allClash}</span>`,
    mixed ? `<span class="warn" title="A mix of time, capacity, reserve, or other blockers prevents assignment.">mixed ${mixed}</span>` : '',
    full ? `<span class="warn" title="All currently usable sections are full.">full ${full}</span>` : '',
    reserve ? `<span title="Only reserved seats remain under the current priority policy.">reserve ${reserve}</span>` : '',
    assignedClashStudents ? `<span class="danger" title="Students currently assigned to overlapping sections.">assigned clash ${assignedClashStudents}</span>` : '',
    `<span class="ok">${Number(row.section_count || 0)} section(s)</span>`,
  ].filter(Boolean).join('');
  return `<div class="tws-blocker-course${RP.studentBlockers.activeCourse === row.course_key ? ' active' : ''}"
      data-course-key="${esc(row.course_key)}"
      data-course-code="${esc(row.course_code)}"
      data-students="${Number(row.issue_student_count || row.unique_student_count || 0)}"
      data-requests="${Number(row.unresolved_course_count || 0)}"
      data-placement-ids='${esc(JSON.stringify(row.placement_ids || []))}'>
    <div class="tws-blocker-main">
      <div class="tws-blocker-title">
        <b>${esc(row.course_code || row.course_key)}</b>
        <span>${esc(row.course_name || row.course_key || '')}</span>
      </div>
      <div class="tws-blocker-count"><b>${Number(row.issue_student_count || row.unique_student_count || 0)}</b>${IS_AR ? 'طالب' : 'students'}</div>
    </div>
    <div class="tws-blocker-meta">${meta}</div>
    ${samples ? `<div class="tws-blocker-students">${samples}</div>` : ''}
  </div>`;
}

function highlightCoursePlacements(courseKey, placementIds = []) {
  const ids = Array.isArray(placementIds) ? placementIds.filter(id => id != null) : [];
  document.querySelectorAll('.tws-pane .cell.highlight').forEach(c => c.classList.remove('highlight'));
  let firstCell = null;
  let count = 0;
  ids.forEach(id => {
    document.querySelectorAll(`.tws-pane .cell[data-placement-id="${id}"]`).forEach(cell => {
      cell.classList.add('highlight');
      count += 1;
      if (!firstCell) firstCell = cell;
    });
  });
  if (!count && courseKey) {
    const wanted = normaliseCourseCode(courseKey);
    const wantedCode = normaliseCourseCode(String(courseKey).split('::', 1)[0]);
    S.panes.slice(0, paneCount()).forEach(pane => {
      (pane.boardData?.placements || []).forEach(placement => {
        const key = normaliseCourseCode(courseKeyOf(placement));
        const code = normaliseCourseCode(placement.course_code);
        if (key !== wanted && code !== wantedCode) return;
        document.querySelectorAll(`.tws-pane .cell[data-placement-id="${placement.id}"]`).forEach(cell => {
          cell.classList.add('highlight');
          count += 1;
          if (!firstCell) firstCell = cell;
        });
      });
    });
  }
  if (firstCell) firstCell.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => document.querySelectorAll('.tws-pane .cell.highlight').forEach(c => c.classList.remove('highlight')), 3500);
  return count;
}

function aggregateCapacityCourses(boards) {
  const byCourse = new Map();
  boards.forEach(b => {
    const cap = RP.capacity[b.boardId];
    if (!cap) return;
    (cap.courses || []).forEach(c => {
      const key = c.course_key || c.course_code || '';
      if (!key) return;
      const prev = byCourse.get(key) || {
        course_key: key,
        course_code: c.course_code || key,
        course_name: c.course_name || '',
        demand: 0,
        raw_capacity: 0,
        placed_sections: 0,
        deficit: 0,
      };
      // Demand is scenario-level unique students, so count it once per
      // course. Capacity is physical seats, so sum it across visible boards.
      prev.demand = Math.max(prev.demand || 0, Number(c.demand || 0));
      prev.raw_capacity += Number(c.raw_capacity || 0);
      prev.placed_sections += Number(c.placed_sections || 0);
      if (!prev.course_name && c.course_name) prev.course_name = c.course_name;
      byCourse.set(key, prev);
    });
  });
  const courses = [...byCourse.values()].map(c => ({
    ...c,
    deficit: Math.max(0, Number(c.demand || 0) - Number(c.raw_capacity || 0)),
  })).sort((a, b) => (b.deficit || 0) - (a.deficit || 0) || (b.demand || 0) - (a.demand || 0) || a.course_code.localeCompare(b.course_code) || String(a.course_name || '').localeCompare(String(b.course_name || '')));
  const totals = courses.reduce((acc, c) => {
    acc.demand += Number(c.demand || 0);
    acc.raw_capacity += Number(c.raw_capacity || 0);
    acc.deficit += Number(c.deficit || 0);
    return acc;
  }, { demand: 0, raw_capacity: 0, deficit: 0 });
  return { courses, totals };
}

function renderRpanelCapacity(body) {
  const boards = aggregateVisibleBoardsData();
  if (!boards.length) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">ⓘ</span>${IS_AR ? 'لم يتم تحميل أي لوحة' : 'No boards loaded yet'}</div>`;
    return;
  }
  // Token each parallel fetch against the current scenario id so a scenario
  // switch mid-flight doesn't overwrite fresh cache with stale results.
  const token = S.scenarioId;
  const fetches = boards
    .filter(b => !RP.capacity[b.boardId])
    .map(b => api(`/ops/tw/boards/${b.boardId}/capacity/`).then(d => {
      if (d && S.scenarioId === token) RP.capacity[b.boardId] = d;
    }));
  Promise.all(fetches).then(() => {
    if (S.scenarioId === token) _renderCapacityNow(body, boards);
  });
  if (!fetches.length) _renderCapacityNow(body, boards);
}
function _renderCapacityNow(body, boards) {
  const { courses, totals } = aggregateCapacityCourses(boards);
  const totalDemand = totals.demand;
  const totalCap = totals.raw_capacity;
  const totalDeficit = totals.deficit;
  if (!courses.length) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">ⓘ</span>${IS_AR ? 'لا توجد بيانات' : 'No capacity data'}</div>`;
    return;
  }
  let html = `<div class="tws-cap-totals">
    <div class="m"><span class="v">${totalDemand}</span><span class="l">${IS_AR ? 'الطلب' : 'Demand'}</span></div>
    <div class="m"><span class="v">${totalCap}</span><span class="l">${IS_AR ? 'السعة' : 'Capacity'}</span></div>
    <div class="m"><span class="v${totalDeficit > 0 ? ' deficit' : ''}">${totalDeficit}</span><span class="l">${IS_AR ? 'العجز' : 'Deficit'}</span></div>
  </div>`;
  courses.slice(0, 30).forEach(c => {
    const pct = c.demand > 0 ? Math.min(100, Math.round((c.raw_capacity / c.demand) * 100)) : 100;
    const name = c.course_name && c.course_name !== c.course_code ? `<em>${esc(c.course_name)}</em>` : '';
    html += `<div class="tws-cap-row">
      <div class="hd">
        <span class="code" title="${esc(c.course_key || c.course_code)}">${esc(c.course_code)}${name}</span>
        <span class="nums">${c.demand} / ${c.raw_capacity}${c.deficit > 0 ? ` · <b class="deficit">-${c.deficit}</b>` : ''}</span>
      </div>
      <div class="bar${c.deficit > 0 ? ' deficit' : ''}"><span style="width:${pct}%"></span></div>
    </div>`;
  });
  if (courses.length > 30) {
    html += `<div class="tws-empty-state" style="padding:8px 0">${IS_AR ? '' : '+'} ${courses.length - 30} ${IS_AR ? 'أكثر' : 'more'}</div>`;
  }
  body.innerHTML = html;
}

function parseRepairBlockedIds(raw) {
  const seen = new Set();
  return String(raw || '')
    .split(/[\s,;]+/)
    .map(v => v.trim())
    .filter(Boolean)
    .map(v => Number.parseInt(v, 10))
    .filter(v => Number.isFinite(v))
    .filter(v => {
      if (seen.has(v)) return false;
      seen.add(v);
      return true;
    });
}

function buildRepairBlockedRequests(raw, placement) {
  const courseKey = placement?.course_key || placement?.course_code || '';
  return parseRepairBlockedIds(raw).map(studentId => ({
    student_id: studentId,
    course_key: courseKey,
    status: 'blocked',
    priority: 'normal',
    reason: 'manual_repair_panel',
    source: 'split_repair_panel',
  }));
}

function approvalStatusForCandidate(run, candidateId) {
  const rows = run?.approvals || [];
  const row = [...rows].reverse().find(item => String(item.candidate_id || '') === String(candidateId || ''));
  return row?.status || '';
}

function bestRepairCandidate(run) {
  return (run?.candidates || []).find(c => c.status === 'feasible') || null;
}

function repairMetricHtml(label, value, cls = '') {
  return `<div class="tws-repair-metric ${cls}"><b>${esc(value ?? 0)}</b><span>${esc(label)}</span></div>`;
}

function repairModeLabel(mode) {
  const labels = {
    conservative: 'Conservative',
    balanced: 'Balanced',
    simulation: 'Simulation',
  };
  return labels[String(mode || '').toLowerCase()] || 'Conservative';
}

function repairSolverStrategyLabel(strategy) {
  const labels = {
    min_cost_flow: 'Min-cost flow',
    profile_pattern_cp_sat: 'Profile CP-SAT',
    large_neighbourhood_cp_sat: 'Bounded LNS',
    student_level_cp_sat: 'Student CP-SAT',
  };
  return labels[String(strategy || '')] || (strategy ? String(strategy).replace(/_/g, ' ') : 'Not solved');
}

function repairSolverStrategyChipsHtml(exact) {
  if (!exact?.solver_strategy) return '';
  const flow = exact.min_cost_flow || {};
  const lns = exact.large_neighbourhood || {};
  const profile = exact.profile_solver || {};
  const warm = exact.warm_start || {};
  const budget = exact.solver_budget || {};
  const chips = [
    `<span class="tws-repair-chip good">${esc(repairSolverStrategyLabel(exact.solver_strategy))}</span>`,
  ];
  if (flow.used) chips.push(`<span class="tws-repair-chip">${Number(flow.arc_count || 0)} flow arcs</span>`);
  if (lns.used) chips.push(`<span class="tws-repair-chip">${Number(lns.relaxed_student_count || 0)} relaxed</span>`);
  if (profile.strategy) chips.push(`<span class="tws-repair-chip">${Number(profile.pattern_count || 0)} patterns</span>`);
  if (warm.used) chips.push(`<span class="tws-repair-chip">${Number(warm.hint_count || 0)} hints</span>`);
  if (budget.total_seconds) chips.push(`<span class="tws-repair-chip muted">${Number(budget.total_seconds || 0)}s budget</span>`);
  return `<div class="tws-repair-reasons">${chips.join('')}</div>`;
}

function repairSolverStrategyCountsHtml(summary) {
  const counts = summary?.student_solver?.solver_strategy_counts || {};
  const entries = Object.entries(counts).filter(([, value]) => Number(value || 0) > 0);
  if (!entries.length) return '';
  const rows = entries.slice(0, 4).map(([strategy, value]) => (
    `<span class="tws-repair-chip">${esc(repairSolverStrategyLabel(strategy))} ${Number(value)}</span>`
  )).join('');
  return `<div class="tws-repair-reasons">${rows}</div>`;
}

function repairStatusBadge(candidate, approvalStatus) {
  const exact = candidate?.metrics?.exact_repair || {};
  if (approvalStatus === 'applied') return `<span class="badge good">Applied</span>`;
  if (approvalStatus === 'approved') return `<span class="badge good">Approved</span>`;
  if (approvalStatus === 'rolled_back') return `<span class="badge">Rolled back</span>`;
  if (candidate?.status === 'feasible' && ['optimal', 'feasible'].includes(candidate?.solver_status)) {
    if (Number(exact.existing_lost || 0) === 0) return `<span class="badge good">${esc(candidate.solver_status)}</span>`;
    return `<span class="badge bad">Loss blocked</span>`;
  }
  if (candidate?.status === 'rejected_before_solver') return `<span class="badge bad">Rejected</span>`;
  return `<span class="badge warn">${esc(candidate?.solver_status || candidate?.status || 'Not solved')}</span>`;
}

function repairDecisionLabel(code) {
  const labels = {
    REPAIR_SIMULATION_ONLY: 'Simulation only',
    REPAIR_CANDIDATE_NOT_FEASIBLE: 'Not feasible',
    REPAIR_CANDIDATE_NOT_SOLVED: 'Not solved',
    REPAIR_EXISTING_LOSS_BLOCKED: 'Existing loss',
    REPAIR_NO_STUDENT_CHANGES: 'No audit rows',
    REPAIR_ALREADY_APPLIED: 'Already applied',
    REPAIR_ALREADY_ROLLED_BACK: 'Already rolled back',
    REPAIR_UNRESOLVED_REMAIN: 'Unresolved remain',
    REPAIR_MOVES_EXISTING_STUDENTS: 'Moves students',
    REPAIR_MULTI_COURSE_CASCADE: 'Cascade',
    REPAIR_RUN_STALE: 'Run stale',
    REPAIR_RUN_NOT_COMPLETED: 'Run incomplete',
    REPAIR_RUN_FINGERPRINT_MISSING: 'Fingerprint missing',
    REPAIR_RUN_FINGERPRINT_ERROR: 'Fingerprint error',
  };
  return labels[String(code || '')] || String(code || 'Unknown').replace(/_/g, ' ').toLowerCase();
}

function repairDecisionGateHtml(candidate) {
  const decision = candidate?.decision || {};
  const blocked = Array.isArray(decision.blocked_reasons) ? decision.blocked_reasons : [];
  const cautions = Array.isArray(decision.cautions) ? decision.cautions : [];
  const chips = [];
  if (decision.risk_level) {
    chips.push(`<span class="tws-repair-chip${decision.risk_level === 'safe' ? ' good' : decision.risk_level === 'blocked' ? ' muted' : ''}">Decision ${esc(decision.risk_level)}</span>`);
  }
  blocked.slice(0, 3).forEach(row => chips.push(`<span class="tws-repair-chip muted">${esc(repairDecisionLabel(row.code))}</span>`));
  cautions.slice(0, 3).forEach(row => chips.push(`<span class="tws-repair-chip">${esc(repairDecisionLabel(row.code))}</span>`));
  return chips.length ? `<div class="tws-repair-reasons">${chips.join('')}</div>` : '';
}

function repairPreflightGateHtml(candidate) {
  const preflight = candidate?.preflight || {};
  if (!preflight.status) return '';
  const blocked = Array.isArray(preflight.blocking_reasons) ? preflight.blocking_reasons : [];
  const tone = preflight.status === 'fresh' ? ' good' : preflight.status === 'stale' ? ' muted' : '';
  const chips = [
    `<span class="tws-repair-chip${tone}">Preflight ${esc(preflight.status)}</span>`,
  ];
  if (preflight.approve_ready) chips.push('<span class="tws-repair-chip good">approval ready</span>');
  if (preflight.apply_ready) chips.push('<span class="tws-repair-chip good">apply ready</span>');
  blocked.slice(0, 3).forEach(row => chips.push(`<span class="tws-repair-chip muted">${esc(repairDecisionLabel(row.code))}</span>`));
  return `<div class="tws-repair-reasons">${chips.join('')}</div>`;
}

function repairPreflightDetailHtml(candidate) {
  const preflight = candidate?.preflight || {};
  if (!preflight.status) return '';
  const checks = Array.isArray(preflight.checks) ? preflight.checks : [];
  const rows = checks.map(row => `<div class="tws-repair-change-row">
    <span>${esc(String(row.name || '').replace(/_/g, ' '))} - ${esc(row.status || '')}</span>
    <em>${esc(row.message || '')}</em>
  </div>`).join('');
  const blockers = (preflight.blocking_reasons || []).map(row => `<div class="tws-repair-explain-row">
    <b>${esc(repairDecisionLabel(row.code))}</b><span>${esc(row.message || '')}</span>
  </div>`).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Current-state preflight</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip${preflight.current_state_valid ? ' good' : ' muted'}">${esc(preflight.status)}</span>
      <span class="tws-repair-chip">${preflight.approve_ready ? 'approval ready' : 'approval blocked'}</span>
      <span class="tws-repair-chip">${preflight.apply_ready ? 'apply ready' : 'apply blocked'}</span>
    </div>
    ${rows || (preflight.skipped_reason ? `<div class="tws-repair-explain-row"><b>Skipped</b><span>${esc(String(preflight.skipped_reason).replace(/_/g, ' '))}</span></div>` : '')}
    ${blockers}
  </div>`;
}

function repairRollbackReadinessHtml(run) {
  const readiness = run?.rollback_preflight || {};
  if (!readiness.status || readiness.status === 'not_applicable') return '';
  const chips = [
    `<span class="tws-repair-chip${readiness.rollback_ready ? ' good' : ' muted'}">Rollback ${esc(readiness.status)}</span>`,
  ];
  if (readiness.candidate_id) chips.push(`<span class="tws-repair-chip">${esc(readiness.candidate_id)}</span>`);
  (readiness.blocking_reasons || []).slice(0, 3).forEach(row => {
    chips.push(`<span class="tws-repair-chip muted">${esc(repairDecisionLabel(row.code))}</span>`);
  });
  return `<div class="tws-repair-reasons">${chips.join('')}</div>`;
}

function repairRollbackReadinessDetailHtml(run) {
  const readiness = run?.rollback_preflight || {};
  if (!readiness.status || readiness.status === 'not_applicable') return '';
  const rows = (readiness.checks || []).map(row => `<div class="tws-repair-change-row">
    <span>${esc(String(row.name || '').replace(/_/g, ' '))} - ${esc(row.status || '')}</span>
    <em>${esc(row.message || '')}</em>
  </div>`).join('');
  const blockers = (readiness.blocking_reasons || []).map(row => `<div class="tws-repair-explain-row">
    <b>${esc(repairDecisionLabel(row.code))}</b><span>${esc(row.message || '')}</span>
  </div>`).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Rollback readiness</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip${readiness.rollback_ready ? ' good' : ' muted'}">${esc(readiness.status)}</span>
      ${readiness.candidate_id ? `<span class="tws-repair-chip">${esc(readiness.candidate_id)}</span>` : ''}
    </div>
    ${rows}
    ${blockers}
  </div>`;
}

function repairRunFreshnessHtml(run) {
  const freshness = run?.run_freshness || {};
  if (!freshness.status) return '';
  const status = String(freshness.status || '');
  const tone = freshness.recommendation_current ? ' good' : status === 'stale' || status === 'blocked' ? ' muted' : '';
  const chips = [
    `<span class="tws-repair-chip${tone}">Run ${esc(status.replace(/_/g, ' '))}</span>`,
  ];
  if (freshness.requires_rerun) chips.push('<span class="tws-repair-chip muted">re-run required</span>');
  if (freshness.fingerprint_matches_analysis) chips.push('<span class="tws-repair-chip good">snapshot match</span>');
  if (freshness.approval_state && freshness.approval_state !== 'none') {
    chips.push(`<span class="tws-repair-chip">${esc(String(freshness.approval_state).replace(/_/g, ' '))}</span>`);
  }
  (freshness.blocking_reasons || []).slice(0, 2).forEach(row => {
    chips.push(`<span class="tws-repair-chip muted">${esc(repairDecisionLabel(row.code))}</span>`);
  });
  return `<div class="tws-repair-reasons">${chips.join('')}</div>`;
}

function repairRunFreshnessDetailHtml(run) {
  const freshness = run?.run_freshness || {};
  if (!freshness.status) return '';
  const rows = (freshness.checks || []).map(row => `<div class="tws-repair-change-row">
    <span>${esc(String(row.name || '').replace(/_/g, ' '))} - ${esc(row.status || '')}</span>
    <em>${esc(row.message || '')}</em>
  </div>`).join('');
  const blockers = (freshness.blocking_reasons || []).map(row => `<div class="tws-repair-explain-row">
    <b>${esc(repairDecisionLabel(row.code))}</b><span>${esc(row.message || '')}</span>
  </div>`).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Run freshness</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip${freshness.recommendation_current ? ' good' : ' muted'}">${esc(String(freshness.status).replace(/_/g, ' '))}</span>
      ${freshness.requires_rerun ? '<span class="tws-repair-chip muted">re-run required</span>' : ''}
      ${freshness.fingerprint_matches_analysis ? '<span class="tws-repair-chip good">fingerprint match</span>' : '<span class="tws-repair-chip muted">fingerprint changed</span>'}
    </div>
    ${freshness.message ? `<div class="tws-repair-explain-row"><b>Status</b><span>${esc(freshness.message)}</span></div>` : ''}
    ${rows}
    ${blockers}
  </div>`;
}

function repairAuditTimelineHtml(run, opts = {}) {
  const timeline = Array.isArray(run?.audit_timeline) ? run.audit_timeline : [];
  if (!timeline.length) return '';
  const limit = opts.limit || 8;
  const rows = timeline.slice(-limit).reverse().map(row => `<div class="tws-repair-change-row">
    <span>${esc(String(row.event || '').replace(/_/g, ' '))}${row.candidate_id ? ` - ${esc(row.candidate_id)}` : ''}</span>
    <em>${esc(row.summary || row.actor || row.created_at || '')}</em>
  </div>`).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Audit timeline</div>
    ${rows}
    ${run?.audit_logs_truncated ? '<div class="tws-repair-reasons"><span class="tws-repair-chip muted">log list truncated</span></div>' : ''}
  </div>`;
}

function repairRankingChipsHtml(candidate) {
  const ranking = candidate?.metrics?.ranking || {};
  if (!ranking.strategy && !ranking.primary_reason) return '';
  const chips = [];
  if (ranking.score_rank) chips.push(`<span class="tws-repair-chip good">Rank ${Number(ranking.score_rank)}</span>`);
  chips.push(`<span class="tws-repair-chip">${esc(String(ranking.strategy || 'ranking').replace(/_/g, ' '))}</span>`);
  if (ranking.primary_reason) chips.push(`<span class="tws-repair-chip">${esc(ranking.primary_reason)}</span>`);
  return `<div class="tws-repair-reasons">${chips.join('')}</div>`;
}

function repairRankingDetailHtml(candidate) {
  const ranking = candidate?.metrics?.ranking || {};
  if (!ranking.strategy) return '';
  const rows = (ranking.criteria || []).map(row => `<div class="tws-repair-change-row">
    <span>${esc(String(row.name || '').replace(/_/g, ' '))}</span>
    <em>${esc(row.sense || '')} ${esc(row.value ?? '')}</em>
  </div>`).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Ranking evidence</div>
    <div class="tws-repair-reasons">
      ${ranking.score_rank ? `<span class="tws-repair-chip good">Rank ${Number(ranking.score_rank)}</span>` : ''}
      <span class="tws-repair-chip">${esc(String(ranking.strategy || '').replace(/_/g, ' '))}</span>
    </div>
    ${ranking.primary_reason ? `<div class="tws-repair-explain-row"><b>Reason</b><span>${esc(ranking.primary_reason)}</span></div>` : ''}
    ${rows}
  </div>`;
}

function repairCandidateEvaluationSummaryHtml(summary) {
  const evaluation = summary?.candidate_evaluation || {};
  if (!evaluation.mode) return '';
  const budget = evaluation.budget || {};
  const budgetSkipped = Number(budget.budget_skipped_solver_count || 0);
  return `<div class="tws-repair-reasons">
    <span class="tws-repair-chip">${Number(evaluation.selected_candidate_count || 0)}/${Number(evaluation.prepared_candidate_count || 0)} candidates</span>
    <span class="tws-repair-chip">${Number(evaluation.solver_invoked_count || 0)} solved</span>
    <span class="tws-repair-chip">${Number(evaluation.total_evaluation_runtime_ms || 0)} ms</span>
    ${budget.limit_seconds ? `<span class="tws-repair-chip muted">${Number(budget.limit_seconds)}s budget</span>` : ''}
    ${budgetSkipped ? `<span class="tws-repair-chip muted">${budgetSkipped} skipped by budget</span>` : ''}
    <span class="tws-repair-chip muted">${esc(String(evaluation.mode).replace(/_/g, ' '))}</span>
  </div>${evaluation.best_candidate_reason ? `<span>${esc(evaluation.best_candidate_reason)}</span>` : ''}`;
}

function repairBlockedDemandHtml(summary, opts = {}) {
  const demand = summary?.blocked_demand || {};
  if (!demand.version) return '';
  const rows = Array.isArray(demand.rows) ? demand.rows : [];
  const limit = opts.limit || 0;
  const active = Number(demand.active_request_count || 0);
  const chips = [
    `<span class="tws-repair-chip${active ? ' good' : ' muted'}">${active} active request${active === 1 ? '' : 's'}</span>`,
    `<span class="tws-repair-chip">${esc(demand.target_course_key || '')}</span>`,
  ];
  if (demand.explicit_request_count || demand.explicit_student_count) {
    chips.push(`<span class="tws-repair-chip">explicit ${Number(demand.explicit_request_count || demand.explicit_student_count || 0)}</span>`);
  }
  if (demand.inferred_request_count) chips.push(`<span class="tws-repair-chip">scenario ${Number(demand.inferred_request_count)}</span>`);
  if (demand.already_registered_count) chips.push(`<span class="tws-repair-chip muted">${Number(demand.already_registered_count)} already registered</span>`);
  if (demand.ignored_request_count) chips.push(`<span class="tws-repair-chip muted">${Number(demand.ignored_request_count)} ignored</span>`);
  const detailRows = limit ? rows.slice(0, limit).map(row => `<div class="tws-repair-change-row">
    <span>${esc(row.student_id)} - ${esc(row.course_key)} - ${esc(row.source || '')}</span>
    <em>${row.already_registered_target ? 'already registered' : esc(row.reason || row.priority || '')}</em>
  </div>`).join('') : '';
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Blocked demand</div>
    <div class="tws-repair-reasons">${chips.join('')}</div>
    ${detailRows}
  </div>`;
}

function repairReasonLabel(code) {
  const labels = {
    NO_ELIGIBLE_TARGET_SECTION: 'No eligible target section',
    NO_TARGET_SECTION_OPTIONS: 'No target sections',
    NO_CAPACITY_AFTER_REPAIR: 'No capacity after repair',
    TIMETABLE_CLASH_WITH_PROTECTED_COURSES: 'Clashes with protected courses',
    CAPACITY_OR_TIMETABLE_BLOCKED: 'Capacity or clash blocked',
    CONSERVATIVE_SOLVER_NOT_SELECTED: 'Not selected by conservative solver',
    SECTION_GENDER_MISMATCH: 'Gender side mismatch',
    PROGRAM_MISMATCH: 'Programme mismatch',
    MISSING_PREREQUISITES: 'Missing prerequisites',
    PROTECTED_STUDENT: 'Protected student',
    PROTECTED_ASSIGNMENT: 'Protected assignment',
    LOCKED_SECTION: 'Locked section',
    NO_POLICY_CLEAN_ROOM: 'No clean room',
    TARGET_PLACEMENT_LOCKED: 'Target locked',
    TIME_OR_INSTRUCTOR_CONFLICT: 'Time or instructor conflict',
    COURSE_ALREADY_TAKEN_OR_STUDYING: 'Already taken or studying',
  };
  return labels[String(code || '')] || String(code || 'Unknown').replace(/_/g, ' ').toLowerCase();
}

function repairReasonCountsHtml(counts, opts = {}) {
  const entries = Object.entries(counts || {}).filter(([, value]) => Number(value || 0) > 0);
  if (!entries.length) return '';
  const limit = opts.limit || 4;
  const rows = entries.slice(0, limit).map(([code, value]) => (
    `<span class="tws-repair-chip">${esc(repairReasonLabel(code))}${Number(value || 0) > 1 ? ` ${Number(value)}` : ''}</span>`
  )).join('');
  const extra = entries.length > limit ? `<span class="tws-repair-chip muted">+${entries.length - limit}</span>` : '';
  return `<div class="tws-repair-reasons">${rows}${extra}</div>`;
}

function repairRejectionCounts(candidate) {
  const counts = {};
  (candidate?.rejection_reasons || []).forEach(reason => {
    const code = reason?.code || 'UNKNOWN';
    counts[code] = (counts[code] || 0) + 1;
  });
  return counts;
}

function repairCandidateReasonHtml(candidate) {
  const exact = candidate?.metrics?.exact_repair || {};
  const rejected = repairRejectionCounts(candidate);
  const unresolved = exact.unresolved_diagnostics?.reason_counts || {};
  const eligibility = exact.eligibility_policy?.rejection_counts || {};
  if (Object.keys(rejected).length) return repairReasonCountsHtml(rejected);
  if (Object.keys(unresolved).length) return repairReasonCountsHtml(unresolved);
  if (Object.keys(eligibility).length) return repairReasonCountsHtml(eligibility, { limit: 3 });
  return '';
}

function repairCascadeCardHtml(candidate) {
  const cascade = candidate?.metrics?.exact_repair?.cascade || {};
  if (!cascade.requires_multi_course_cascade) return '';
  const courses = cascade.touched_course_count || (cascade.touched_courses || []).length || 0;
  const students = cascade.multi_course_student_count || 0;
  return `<div class="tws-repair-reasons">
    <span class="tws-repair-chip good">Cascade</span>
    <span class="tws-repair-chip">${Number(courses)} courses</span>
    <span class="tws-repair-chip">${Number(students)} multi-course student${Number(students) === 1 ? '' : 's'}</span>
  </div>`;
}

function repairChangeDirection(ch) {
  return `${esc(ch.before_section_id || 'new')} -> ${esc(ch.after_section_id || '-')}`;
}

function repairChangeDetailHtml(ch) {
  const reason = ch.details?.unresolved_reason;
  if (reason?.code) return `<small>${esc(repairReasonLabel(reason.code))}</small>`;
  const policy = ch.details?.policy || ch.details?.eligibility_policy || '';
  return policy ? `<small>${esc(String(policy).replace(/_/g, ' '))}</small>` : '';
}

function repairRejectedDetailHtml(candidate) {
  const reasons = candidate?.rejection_reasons || [];
  if (!reasons.length) return '';
  const rows = reasons.slice(0, 12).map(reason => `<div class="tws-repair-explain-row">
    <b>${esc(repairReasonLabel(reason.code))}</b>
    <span>${esc(reason.message || reason.code || '')}</span>
  </div>`).join('');
  return `<div class="tws-repair-explain"><div class="tws-repair-section-title">Rejected before solver</div>${rows}</div>`;
}

function repairUnresolvedDetailHtml(exact) {
  const data = exact?.unresolved_diagnostics || {};
  const students = data.students || [];
  const counts = data.reason_counts || {};
  if (!students.length && !Object.keys(counts).length) return '';
  const studentRows = students.slice(0, 20).map(row => `<div class="tws-repair-explain-row">
    <b>${esc(row.student_id)} - ${esc(repairReasonLabel(row.code))}</b>
    <span>${esc(row.message || '')}</span>
  </div>`).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Unresolved reason</div>
    ${repairReasonCountsHtml(counts, { limit: 6 })}
    ${studentRows}
  </div>`;
}

function repairEligibilityDetailHtml(exact) {
  const policy = exact?.eligibility_policy || {};
  const samples = policy.samples || [];
  const counts = policy.rejection_counts || {};
  const priorityCounts = policy.priority_group_counts || {};
  const mobilityCounts = policy.mobility_policy_counts || {};
  if (!samples.length && !Object.keys(counts).length && !Object.keys(priorityCounts).length) return '';
  const priorityRows = Object.entries(priorityCounts)
    .filter(([, value]) => Number(value || 0) > 0)
    .map(([label, value]) => `<span class="tws-repair-chip">${esc(String(label).replace(/_/g, ' '))} ${Number(value)}</span>`)
    .join('');
  const mobilityRows = Object.entries(mobilityCounts)
    .filter(([, value]) => Number(value || 0) > 0)
    .map(([label, value]) => `<span class="tws-repair-chip">${esc(String(label).replace(/_/g, ' '))} ${Number(value)}</span>`)
    .join('');
  const rows = samples.slice(0, 8).map(sample => {
    const reasonCounts = {};
    (sample.reasons || []).forEach(reason => {
      const code = reason?.code || 'UNKNOWN';
      reasonCounts[code] = (reasonCounts[code] || 0) + 1;
    });
    return `<div class="tws-repair-explain-row">
      <b>${esc(sample.student_id)} - ${esc(sample.course_key || '')} - ${esc(sample.section || '')}</b>
      ${repairReasonCountsHtml(reasonCounts, { limit: 3 })}
    </div>`;
  }).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Eligibility checks</div>
    ${priorityRows || mobilityRows ? `<div class="tws-repair-reasons">${priorityRows}${mobilityRows}</div>` : ''}
    ${repairReasonCountsHtml(counts, { limit: 6 })}
    ${rows}
  </div>`;
}

function repairObjectiveTraceHtml(exact) {
  const trace = exact?.objective?.trace || [];
  if (!trace.length) return '';
  const rows = trace.map(stage => `<div class="tws-repair-change-row">
    <span>${esc(stage.stage)} - ${esc(stage.name || '')} - ${esc(stage.status || '')}</span>
    <em>${esc(stage.value ?? '-')} · ${Number(stage.runtime_ms || 0)} ms · ${Number(stage.branches || 0)} branches</em>
  </div>`).join('');
  return `<div class="tws-repair-explain"><div class="tws-repair-section-title">Objective stages</div>${rows}</div>`;
}

function repairSolverStrategyDetailHtml(exact) {
  if (!exact?.solver_strategy) return '';
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Solver strategy</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip good">${esc(repairSolverStrategyLabel(exact.solver_strategy))}</span>
      <span class="tws-repair-chip">${Number(exact.variables || 0)} active vars</span>
      <span class="tws-repair-chip">${Number(exact.student_level_variables || 0)} student vars baseline</span>
      <span class="tws-repair-chip">${Number(exact.runtime_ms || 0)} ms</span>
    </div>
  </div>`;
}

function repairSolverBudgetDetailHtml(exact) {
  const budget = exact?.solver_budget || {};
  if (!budget.enabled) return '';
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Solver budget</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip">${Number(budget.total_seconds || 0)}s candidate budget</span>
      <span class="tws-repair-chip">${Number(budget.stage_seconds || 0)}s per stage</span>
      <span class="tws-repair-chip">${Number(budget.runtime_ms || 0)} ms runtime</span>
      <span class="tws-repair-chip${budget.proof_complete ? ' good' : ' muted'}">${budget.proof_complete ? 'proved' : 'bounded result'}</span>
    </div>
  </div>`;
}

function repairWarmStartDetailHtml(exact) {
  const warm = exact?.warm_start || {};
  if (!warm.enabled) return '';
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Warm start</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip${warm.used ? ' good' : ' muted'}">${warm.used ? 'Used' : 'Not used'}</span>
      <span class="tws-repair-chip">${esc(String(warm.strategy || warm.reason || 'solver hint').replace(/_/g, ' '))}</span>
      <span class="tws-repair-chip">${Number(warm.hint_count || 0)} hints</span>
      ${warm.profile_current_pattern_count ? `<span class="tws-repair-chip">${Number(warm.profile_current_pattern_count)} stable profiles</span>` : ''}
      ${warm.current_assignment_hint_count ? `<span class="tws-repair-chip">${Number(warm.current_assignment_hint_count)} current assignments</span>` : ''}
    </div>
  </div>`;
}

function repairMinCostFlowDetailHtml(exact) {
  const flow = exact?.min_cost_flow || {};
  if (!flow.enabled && !flow.used) return '';
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Min-cost flow shortcut</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip${flow.used ? ' good' : ' muted'}">${flow.used ? 'Used' : 'Not used'}</span>
      <span class="tws-repair-chip">${esc(flow.reason || flow.strategy || 'simple one-course cases only')}</span>
      <span class="tws-repair-chip">${Number(flow.student_count || 0)} students</span>
      <span class="tws-repair-chip">${Number(flow.section_count || 0)} sections</span>
      <span class="tws-repair-chip">${Number(flow.arc_count || 0)} arcs</span>
    </div>
  </div>`;
}

function repairLargeNeighbourhoodDetailHtml(exact) {
  const lns = exact?.large_neighbourhood || {};
  if (!lns.enabled && !lns.used) return '';
  const attempts = Array.isArray(lns.attempts) ? lns.attempts : [];
  const rows = attempts.slice(0, 8).map(row => {
    const status = row.status || 'not_solved';
    const detail = row.failure_reason || row.reason || '';
    const vars = row.variables == null ? '' : ` - ${Number(row.variables)} vars`;
    const recovered = Number(row.blocked_recovered || 0);
    return `<div class="tws-repair-change-row" title="${esc(detail)}">
    <span>${esc(row.name || 'neighbourhood')} - ${esc(status)}${detail ? ` - ${esc(detail)}` : ''}</span>
    <em>${Number(row.relaxed_student_count || 0)} relaxed${vars}${recovered ? ` - ${recovered} recovered` : ''}</em>
  </div>`;
  }).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Bounded LNS fallback</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip${lns.used ? ' good' : ' muted'}">${lns.used ? 'Used' : 'Not used'}</span>
      <span class="tws-repair-chip">${esc(lns.reason || lns.neighbourhood || 'not needed')}</span>
      <span class="tws-repair-chip">${Number(lns.relaxed_student_count || 0)} relaxed</span>
      <span class="tws-repair-chip">${Number(lns.fixed_student_count || 0)} fixed</span>
      <span class="tws-repair-chip">${Number(lns.neighbourhood_count || attempts.length || 0)} attempts</span>
    </div>
    ${rows}
  </div>`;
}

function repairCascadeDetailHtml(exact) {
  const cascade = exact?.cascade || {};
  if (!cascade.required_change_count && !cascade.requires_multi_course_cascade) return '';
  const courses = (cascade.touched_courses || []).join(', ') || '-';
  const rows = (cascade.required_change_samples || []).slice(0, 20).map(row => `<div class="tws-repair-change-row">
    <span>${esc(row.student_id)} - ${esc(row.course_key)} - ${esc(String(row.change_type || '').replace(/_/g, ' '))}</span>
    <em>${esc(row.before_section_id || 'new')} -> ${esc(row.after_section_id || '-')}</em>
  </div>`).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Cascade impact</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip${cascade.requires_multi_course_cascade ? ' good' : ''}">${cascade.requires_multi_course_cascade ? 'Multi-course cascade' : 'Direct repair'}</span>
      <span class="tws-repair-chip">${Number(cascade.touched_course_count || 0)} touched courses</span>
      <span class="tws-repair-chip">${Number(cascade.required_change_count || 0)} required changes</span>
    </div>
    <div class="tws-repair-explain-row"><b>Courses</b><span>${esc(courses)}</span></div>
    ${rows}
  </div>`;
}

function repairCompressionDetailHtml(exact) {
  const data = exact?.profile_compression || {};
  if (!data.enabled) return '';
  const profiles = data.sample_profiles || [];
  const rows = profiles.slice(0, 8).map(row => `<div class="tws-repair-change-row">
    <span>${esc(row.profile_id)} - ${Number(row.student_count || 0)} student${Number(row.student_count || 0) === 1 ? '' : 's'}</span>
    <em>${Number(row.option_variable_count || 0)} vars</em>
  </div>`).join('');
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Profile compression</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip">${Number(data.student_count || 0)} students</span>
      <span class="tws-repair-chip">${Number(data.profile_count || 0)} profiles</span>
      <span class="tws-repair-chip">${Number(data.estimated_variable_reduction || 0)} fewer vars later</span>
      ${data.solver_used ? '<span class="tws-repair-chip good">profile solver used</span>' : ''}
    </div>
    ${rows}
  </div>`;
}

function repairConflictPolicyDetailHtml(exact) {
  const policy = exact?.conflict_policy || {};
  if (!policy.strategy) return '';
  return `<div class="tws-repair-explain">
    <div class="tws-repair-section-title">Conflict constraints</div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip">${Number(policy.logical_conflict_edges || 0)} conflict edges</span>
      <span class="tws-repair-chip">${Number(policy.at_most_one_constraints || 0)} grouped</span>
      <span class="tws-repair-chip">${Number(policy.pairwise_constraints || 0)} pairwise</span>
      ${policy.too_large ? '<span class="tws-repair-chip muted">limit reached</span>' : ''}
    </div>
  </div>`;
}

function repairCandidateCardHtml(candidate, run, isBest) {
  const exact = candidate.metrics?.exact_repair || {};
  const approvalStatus = approvalStatusForCandidate(run, candidate.candidate_id);
  const decision = candidate.decision || {};
  const preflight = candidate.preflight || {};
  const simulationOnly = String(run?.run?.mode || '').toLowerCase() === 'simulation';
  const fallbackCanApprove = candidate.status === 'feasible'
    && ['optimal', 'feasible'].includes(candidate.solver_status)
    && Number(exact.existing_lost || 0) === 0
    && !simulationOnly
    && !['approved', 'applied'].includes(approvalStatus);
  const canApprove = (typeof decision.approve_allowed === 'boolean' ? decision.approve_allowed : fallbackCanApprove)
    && preflight.approve_ready !== false;
  const canApply = (typeof decision.apply_allowed === 'boolean' ? decision.apply_allowed : approvalStatus === 'approved' && !simulationOnly)
    && preflight.apply_ready !== false;
  const rejected = candidate.status !== 'feasible';
  const changes = (run.student_changes || [])
    .filter(ch => String(ch.candidate_id) === String(candidate.candidate_id))
    .filter(ch => !['unchanged', 'unresolved'].includes(ch.change_type))
    .slice(0, 4);
  const changeRows = changes.length
    ? `<div class="tws-repair-changes">${changes.map(ch => `<div class="tws-repair-change-row">
        <span>${esc(ch.student_id)} · ${esc(ch.course_key)} · ${esc(ch.change_type.replace(/_/g, ' '))}</span>
        <em>${esc(ch.before_section_id || 'new')} → ${esc(ch.after_section_id || '—')}</em>
      </div>`).join('')}</div>`
    : '';
  const reasonsHtml = repairCandidateReasonHtml(candidate);
  return `<div class="tws-repair-candidate ${isBest ? 'best' : ''} ${rejected ? 'rejected' : ''}">
    <div class="hd">
      <div class="when"><b>${esc(candidate.candidate_id)}</b><span>${esc(candidate.day)} ${esc(candidate.start_time)}-${esc(candidate.end_time)}</span></div>
      ${repairStatusBadge(candidate, approvalStatus)}
    </div>
    <div class="tws-repair-metrics">
      ${repairMetricHtml('Recovered', exact.blocked_recovered ?? '—')}
      ${repairMetricHtml('Lost', exact.existing_lost ?? '—')}
      ${repairMetricHtml('Moved', exact.students_moved ?? '—')}
      ${repairMetricHtml('Unresolved', exact.unresolved_blocked ?? '—')}
    </div>
    <span>${candidate.room ? `Room ${esc(candidate.room)} · ` : ''}${candidate.student_change_count || 0} audited student change row(s)</span>
    ${repairSolverStrategyChipsHtml(exact)}
    ${repairCascadeCardHtml(candidate)}
    ${repairDecisionGateHtml(candidate)}
    ${repairPreflightGateHtml(candidate)}
    ${repairRankingChipsHtml(candidate)}
    ${simulationOnly ? '<div class="tws-repair-reasons"><span class="tws-repair-chip muted">Simulation only</span></div>' : ''}
    ${reasonsHtml}
    ${changeRows}
    <div class="acts">
      <button class="tws-mini-action" data-repair-detail="${esc(candidate.candidate_id)}" type="button">Details</button>
      <button class="tws-mini-action" data-repair-report="${esc(candidate.candidate_id)}" type="button">Report</button>
      <button class="tws-mini-action" data-repair-approve="${esc(candidate.candidate_id)}" type="button" ${canApprove ? '' : 'disabled'}>Approve</button>
      <button class="tws-mini-action" data-repair-apply="${esc(candidate.candidate_id)}" type="button" ${canApply ? '' : 'disabled'}>Apply</button>
    </div>
  </div>`;
}

function repairRunSummaryHtml(run) {
  if (!run) return '';
  const summary = run.summary || {};
  const solver = summary.student_solver || {};
  const best = bestRepairCandidate(run);
  const exact = best?.metrics?.exact_repair || solver.best_candidate_metrics || {};
  const applied = summary.application?.status === 'applied';
  const rolledBack = summary.rollback?.status === 'rolled_back';
  const rollbackReady = run?.rollback_preflight?.rollback_ready !== false;
  const mode = run?.run?.mode || RP.repair.mode || 'conservative';
  const cacheHit = !!run?.cache?.hit;
  return `<div class="tws-repair-summary">
    <div class="tws-repair-metrics">
      ${repairMetricHtml('Feasible', summary.feasible_candidate_count ?? 0)}
      ${repairMetricHtml('Recovered', exact.blocked_recovered ?? 0)}
      ${repairMetricHtml('Moved', exact.students_moved ?? 0)}
      ${repairMetricHtml('Mode', repairModeLabel(mode))}
    </div>
    <span>${applied ? `Applied ${esc(summary.application.candidate_id || '')}` : rolledBack ? `Rolled back ${esc(summary.rollback.candidate_id || '')}` : `Best candidate ${esc(summary.best_candidate_id || '—')}`}</span>
    ${repairSolverStrategyCountsHtml(summary)}
    ${repairRunFreshnessHtml(run)}
    ${repairBlockedDemandHtml(summary)}
    ${repairCandidateEvaluationSummaryHtml(summary)}
    ${repairRollbackReadinessHtml(run)}
    ${cacheHit ? '<div class="tws-repair-reasons"><span class="tws-repair-chip good">Cached result reused</span></div>' : ''}
    ${repairAuditTimelineHtml(run, { limit: 5 })}
    ${run ? '<div style="margin-top:8px"><button class="tws-mini-action" id="twsRepairReport" type="button">Run report</button></div>' : ''}
    ${applied ? `<div style="margin-top:8px"><button class="tws-mini-action danger" id="twsRepairRollback" type="button" ${rollbackReady ? '' : 'disabled'}>Rollback applied repair</button></div>` : ''}
  </div>`;
}

function repairCandidateDetailModal(candidate, run) {
  const exact = candidate.metrics?.exact_repair || {};
  const changes = (run.student_changes || []).filter(ch => String(ch.candidate_id) === String(candidate.candidate_id));
  const rows = changes.slice(0, 80).map(ch => `<div class="tws-repair-change-row">
    <span>${esc(ch.student_id)} · ${esc(ch.course_key)} · ${esc(ch.change_type.replace(/_/g, ' '))}</span>
    <em>${esc(ch.before_section_id || 'new')} → ${esc(ch.after_section_id || '—')}</em>
  </div>`).join('');
  openModal({
    title: `Repair ${candidate.candidate_id}`,
    sub: `${candidate.day} ${candidate.start_time}-${candidate.end_time}${candidate.room ? ` · ${candidate.room}` : ''}`,
    width: 'wide',
    body: `<div class="tws-repair-panel">
      <div class="tws-repair-metrics">
        ${repairMetricHtml('Recovered', exact.blocked_recovered ?? 0)}
        ${repairMetricHtml('Lost', exact.existing_lost ?? 0)}
        ${repairMetricHtml('Moved', exact.students_moved ?? 0)}
        ${repairMetricHtml('Mode', repairModeLabel(run?.run?.mode || exact.mode || RP.repair.mode))}
      </div>
      ${repairSolverStrategyDetailHtml(exact)}
      ${repairSolverBudgetDetailHtml(exact)}
      ${repairWarmStartDetailHtml(exact)}
      ${repairMinCostFlowDetailHtml(exact)}
      ${repairLargeNeighbourhoodDetailHtml(exact)}
      ${repairRunFreshnessDetailHtml(run)}
      ${repairBlockedDemandHtml(run?.summary || {}, { limit: 12 })}
      ${repairPreflightDetailHtml(candidate)}
      ${repairRankingDetailHtml(candidate)}
      ${repairAuditTimelineHtml(run, { limit: 10 })}
      ${repairRejectedDetailHtml(candidate)}
      ${repairUnresolvedDetailHtml(exact)}
      ${repairCascadeDetailHtml(exact)}
      ${repairCompressionDetailHtml(exact)}
      ${repairConflictPolicyDetailHtml(exact)}
      ${repairEligibilityDetailHtml(exact)}
      ${repairObjectiveTraceHtml(exact)}
      <div class="tws-repair-changes">${rows || '<span>No student changes were proposed for this candidate.</span>'}</div>
    </div>`,
    buttons: [{ label: 'Close' }],
  });
}

function repairCandidateById(candidateId) {
  const candidates = RP.repair.run?.candidates || [];
  return candidates.find(c => String(c.candidate_id) === String(candidateId)) || null;
}

function repairActionMetricsHtml(candidate) {
  const exact = candidate?.metrics?.exact_repair || {};
  return `<div class="tws-repair-metrics">
    ${repairMetricHtml('Recovered', exact.blocked_recovered ?? 0)}
    ${repairMetricHtml('Lost', exact.existing_lost ?? 0)}
    ${repairMetricHtml('Moved', exact.students_moved ?? 0)}
    ${repairMetricHtml('Unresolved', exact.unresolved_blocked ?? 0)}
  </div>`;
}

function confirmRepairAction(opts) {
  return new Promise(resolve => {
    let settled = false;
    const done = value => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    openModal({
      title: opts.title || 'Confirm repair action',
      sub: opts.sub || '',
      width: opts.width || '',
      body: opts.body || '',
      onClose: () => done(null),
      buttons: [
        { label: opts.cancelLabel || 'Cancel', onClick: () => done(null) },
        {
          label: opts.confirmLabel || 'Confirm',
          variant: 'primary',
          onClick: modal => {
            if (opts.collect) done(opts.collect(modal));
            else done(true);
          },
        },
      ],
    });
  });
}

async function runRepairAnalysisFromPanel() {
  if (!S.selectedPlacementId) return;
  const placementId = S.selectedPlacementId;
  const text = $('twsRepairBlockedIds')?.value || '';
  RP.repair.blockedIdsText = text;
  RP.repair.busy = true;
  renderRpanel();
  const data = await api('/ops/tw/repair/analyse/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      placement_id: placementId,
      blocked_student_ids: parseRepairBlockedIds(text),
      blocked_requests: buildRepairBlockedRequests(text, findPlacement(placementId)?.placement || {}),
      mode: RP.repair.mode || 'conservative',
      move_scope: RP.repair.moveScope || 'single_session',
      active_plan_filter: activePlanFilter(),
      limits: { max_candidates: 80, max_solver_seconds: 5, max_total_solver_seconds: 45 },
    }),
  });
  RP.repair.busy = false;
  if (!data) {
    notify.error(IS_AR ? 'فشل تحليل الإصلاح' : 'Repair analysis failed');
    renderRpanel();
    return;
  }
  RP.repair.placementId = placementId;
  RP.repair.run = data;
  notify.success(IS_AR ? 'تم تحليل الإصلاح' : 'Repair analysis ready');
  renderRpanel();
}

async function approveRepairCandidateFromPanel(candidateId) {
  const runId = RP.repair.run?.run?.id;
  if (!runId || !candidateId) return;
  const candidate = repairCandidateById(candidateId);
  if (!candidate) return;
  const notes = await confirmRepairAction({
    title: `Approve repair ${candidateId}`,
    sub: 'Approval validates current timetable and student assignments before enabling apply.',
    body: `<div class="tws-repair-panel">
      ${repairActionMetricsHtml(candidate)}
      ${repairDecisionGateHtml(candidate)}
      ${repairPreflightGateHtml(candidate)}
      <div class="tws-repair-explain">
        <div class="tws-repair-section-title">Approval gate</div>
        <div class="tws-repair-explain-row"><b>Preflight</b><span>Placement, room feasibility, and student assignment state will be rechecked now.</span></div>
        <div class="tws-repair-explain-row"><b>Apply</b><span>No timetable or registration write happens during approval.</span></div>
      </div>
      <div class="tws-repair-form">
        <textarea id="twsRepairApprovalNotes" placeholder="Approval notes">Approved from split workspace repair panel</textarea>
      </div>
    </div>`,
    confirmLabel: 'Approve',
    collect: modal => modal.querySelector('#twsRepairApprovalNotes')?.value || 'Approved from split workspace repair panel',
  });
  if (notes == null) return;
  const data = await api(`/ops/tw/repair/runs/${runId}/candidates/${candidateId}/approve/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ notes }),
  });
  if (!data) {
    notify.error(IS_AR ? 'فشل اعتماد الإصلاح' : 'Repair approval failed');
    return;
  }
  RP.repair.run = data;
  notify.success(IS_AR ? 'تم الاعتماد' : 'Optimisation approved');
  renderRpanel();
}

async function applyRepairCandidateFromPanel(candidateId) {
  const runId = RP.repair.run?.run?.id;
  if (!runId || !candidateId) return;
  const candidate = repairCandidateById(candidateId);
  if (!candidate) return;
  const ok = await confirmRepairAction({
    title: `Apply optimisation ${candidateId}`,
    sub: 'This writes the section move and audited student assignment changes.',
    body: `<div class="tws-repair-panel">
      ${repairActionMetricsHtml(candidate)}
      ${repairDecisionGateHtml(candidate)}
      ${repairPreflightGateHtml(candidate)}
      <div class="tws-repair-explain">
        <div class="tws-repair-section-title">Controlled write</div>
        <div class="tws-repair-explain-row"><b>Transaction</b><span>Timetable and student assignment changes are applied together.</span></div>
        <div class="tws-repair-explain-row"><b>Rollback</b><span>Rollback stays available as long as repair-owned assignments are not manually changed.</span></div>
      </div>
    </div>`,
    confirmLabel: 'Apply optimisation',
  });
  if (!ok) return;
  const data = await api(`/ops/tw/repair/runs/${runId}/candidates/${candidateId}/apply/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  if (!data) {
    notify.error(IS_AR ? 'فشل تطبيق الإصلاح' : 'Repair apply failed');
    return;
  }
  RP.repair.run = data;
  await refreshAfterRepairMutation();
  notify.success(IS_AR ? 'تم تطبيق الإصلاح' : 'Optimisation applied');
  renderRpanel();
}

async function rollbackRepairFromPanel() {
  const runId = RP.repair.run?.run?.id;
  if (!runId) return;
  const appliedId = RP.repair.run?.summary?.application?.candidate_id || '';
  const candidate = appliedId ? repairCandidateById(appliedId) : null;
  const ok = await confirmRepairAction({
    title: appliedId ? `Rollback repair ${appliedId}` : 'Rollback repair',
    sub: 'This restores the original placement and reverses repair-owned assignment changes.',
    body: `<div class="tws-repair-panel">
      ${candidate ? repairActionMetricsHtml(candidate) : ''}
      ${repairRollbackReadinessDetailHtml(RP.repair.run)}
      <div class="tws-repair-explain">
        <div class="tws-repair-section-title">Rollback safety</div>
        <div class="tws-repair-explain-row"><b>Ownership check</b><span>Only assignments written by this repair run are rolled back.</span></div>
        <div class="tws-repair-explain-row"><b>Manual edits</b><span>If a repair-owned assignment was changed manually, rollback stops for review.</span></div>
      </div>
    </div>`,
    confirmLabel: 'Rollback repair',
  });
  if (!ok) return;
  const data = await api(`/ops/tw/repair/runs/${runId}/rollback/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  if (!data) {
    notify.error(IS_AR ? 'فشل الاسترجاع' : 'Rollback failed');
    return;
  }
  RP.repair.run = data;
  await refreshAfterRepairMutation();
  notify.success(IS_AR ? 'تم الاسترجاع' : 'Repair rolled back');
  renderRpanel();
}

function openRepairReportFromPanel(candidateId = '') {
  const runId = RP.repair.run?.run?.id;
  if (!runId) return;
  const qs = candidateId ? `?candidate_id=${encodeURIComponent(candidateId)}` : '';
  window.open(`/ops/tw/repair/runs/${encodeURIComponent(runId)}/report/${qs}`, '_blank');
}

function globalRepairMetricHtml(label, value, cls = '') {
  return repairMetricHtml(label, value, cls);
}

function globalRepairPlanStatusLabel(status) {
  const map = {
    draft: 'Draft',
    approved: 'Approved',
    applied: 'Applied',
    rolled_back: 'Rolled back',
    empty: 'No ready repairs',
    failed: 'Failed',
  };
  return map[String(status || '').toLowerCase()] || String(status || 'Draft').replace(/_/g, ' ');
}

function globalRepairPlanCanApprove(plan) {
  const status = String(plan?.plan?.status || '').toLowerCase();
  return status === 'draft' && Number(plan?.summary?.active_item_count || 0) > 0;
}

function globalRepairPlanCanApply(plan) {
  return String(plan?.plan?.status || '').toLowerCase() === 'approved';
}

function globalRepairPlanCanRollback(plan) {
  return String(plan?.plan?.status || '').toLowerCase() === 'applied';
}

function globalRepairPlanSummaryHtml(plan) {
  if (!plan) return '';
  const summary = plan.summary || {};
  const totals = summary.estimated_totals || {};
  const status = plan.plan?.status || summary.status || '';
  const governance = summary.governance || {};
  const skipped = summary.skipped || [];
  const selected = summary.simulation?.selected_count ?? 0;
  const scanned = summary.simulation?.scanned_run_count ?? 0;
  const scenarioUnresolved = summary.scenario_unresolved || {};
  const unresolvedBefore = totals.scenario_unresolved_students_before ?? scenarioUnresolved.student_count;
  const unresolvedAfter = totals.scenario_unresolved_students_after_plan ?? unresolvedBefore;
  return `<div class="tws-repair-summary">
    <div class="tws-repair-metrics">
      ${globalRepairMetricHtml('Scenario unresolved', unresolvedBefore ?? 0, Number(unresolvedBefore || 0) ? 'bad' : 'good')}
      ${globalRepairMetricHtml('After plan', unresolvedAfter ?? 0, Number(unresolvedAfter || 0) ? '' : 'good')}
      ${globalRepairMetricHtml('Students recovered', totals.distinct_target_students_recovered ?? 0, 'good')}
      ${globalRepairMetricHtml('Existing lost', totals.existing_lost ?? 0, Number(totals.existing_lost || 0) ? 'bad' : '')}
      ${globalRepairMetricHtml('Items', summary.active_item_count ?? 0)}
    </div>
    <div class="tws-repair-reasons">
      <span class="tws-repair-chip good">Objective: unresolved students</span>
      <span class="tws-repair-chip">${esc(globalRepairPlanStatusLabel(status))}</span>
      <span class="tws-repair-chip">${Number(selected)} selected from ${Number(scanned)} scans</span>
      ${governance.approval_required ? '<span class="tws-repair-chip">approval required</span>' : ''}
      ${governance.cross_board_is_not_primary_objective ? '<span class="tws-repair-chip muted">cross-board diagnostic only</span>' : ''}
    </div>
    ${skipped.length ? `<div class="tws-repair-explain">
      <div class="tws-repair-section-title">Skipped opportunities</div>
      ${skipped.slice(0, 5).map(row => `<div class="tws-repair-explain-row">
        <b>${esc(row.course_key || row.code || 'Skipped')}</b><span>${esc(row.message || row.code || '')}</span>
      </div>`).join('')}
    </div>` : ''}
  </div>`;
}

function globalRepairPlanItemsHtml(plan) {
  const items = plan?.items || [];
  if (!items.length) {
    return `<div class="tws-fix-empty">No apply-ready repair items were found for the scanned actual unresolved-student hotspots. Try a wider scan or use Optimise current for scenario-level unresolved-student improvement.</div>`;
  }
  return `<div class="tws-repair-list">
    ${items.map(item => {
      const metrics = item.metrics || {};
      const impact = item.impact || {};
      const recovered = impact.target_recovered_student_ids || [];
      return `<div class="tws-repair-candidate ${item.status === 'applied' ? 'best' : ''}">
        <div class="hd">
          <div class="when"><b>${esc(item.course_key || 'Course')}</b><span>Placement ${esc(item.placement_id || '')} · ${esc(item.status || '')}</span></div>
          <span class="tws-repair-chip">${esc(item.candidate_id || '')}</span>
        </div>
        <div class="tws-repair-metrics">
          ${globalRepairMetricHtml('Recovered', metrics.blocked_recovered ?? 0)}
          ${globalRepairMetricHtml('Unresolved', metrics.unresolved_blocked ?? 0)}
          ${globalRepairMetricHtml('Moved', metrics.students_moved ?? 0)}
          ${globalRepairMetricHtml('Changes', metrics.section_changes ?? 0)}
        </div>
        <span>${Number(recovered.length || 0)} distinct target student sample(s) recovered in this item.</span>
        <div class="acts">
          ${item.links?.run_report ? `<button class="tws-mini-action" data-global-repair-report="${esc(item.links.run_report)}" type="button">Report</button>` : ''}
        </div>
      </div>`;
    }).join('')}
  </div>`;
}

function globalRepairPlanFormHtml() {
  return `<div class="tws-repair-panel">
    <div class="tws-repair-target">
      <b>Global unresolved-student repair plan</b><br>
      <span>Build an approval-first plan from current scenario demand. The primary objective is to reduce unresolved students.</span>
    </div>
    <div class="tws-repair-form">
      <select id="twsGlobalRepairMode" class="tws-repair-mode">
        <option value="conservative" ${RP.globalRepair.mode === 'conservative' ? 'selected' : ''}>Conservative</option>
        <option value="balanced" ${RP.globalRepair.mode === 'balanced' ? 'selected' : ''}>Balanced</option>
      </select>
      <input id="twsGlobalRepairMaxPlacements" type="number" min="1" max="25" step="1" value="${Number(RP.globalRepair.maxPlacements || 8)}" placeholder="Max placements">
      <input id="twsGlobalRepairSolverSeconds" type="number" min="1" max="30" step="1" value="${Number(RP.globalRepair.maxSolverSeconds || 5)}" placeholder="Solver seconds">
      <textarea id="twsGlobalRepairCourseKeys" placeholder="Optional course keys, separated by commas">${esc(RP.globalRepair.courseKeysText || '')}</textarea>
      <span>Leave courses blank to let the system choose unresolved-student hotspots. Apply still requires approval and guarded preflight.</span>
      <div id="twsGlobalRepairStatus" class="tws-fix-empty" style="display:none"></div>
    </div>
  </div>`;
}

function globalRepairPlanResultHtml(plan) {
  return `<div class="tws-repair-panel">
    ${globalRepairPlanSummaryHtml(plan)}
    ${globalRepairPlanItemsHtml(plan)}
  </div>`;
}

function wireGlobalRepairPlanModal(modal, plan) {
  modal.querySelectorAll('[data-global-repair-report]').forEach(btn => {
    btn.addEventListener('click', () => window.open(btn.dataset.globalRepairReport, '_blank'));
  });
  modal.querySelector('#twsGlobalRepairApprove')?.addEventListener('click', () => approveGlobalRepairPlanFromModal(modal));
  modal.querySelector('#twsGlobalRepairApply')?.addEventListener('click', () => applyGlobalRepairPlanFromModal(modal));
  modal.querySelector('#twsGlobalRepairRollback')?.addEventListener('click', () => rollbackGlobalRepairPlanFromModal(modal));
  updateGlobalRepairPlanActionBar(modal, plan);
}

function updateGlobalRepairPlanActionBar(modal, plan) {
  const bar = modal.querySelector('#twsGlobalRepairActions');
  if (!bar) return;
  bar.innerHTML = `
    <button class="tws-mini-action" id="twsGlobalRepairApprove" type="button" ${globalRepairPlanCanApprove(plan) ? '' : 'disabled'}>Approve plan</button>
    <button class="tws-mini-action" id="twsGlobalRepairApply" type="button" ${globalRepairPlanCanApply(plan) ? '' : 'disabled'}>Apply plan</button>
    <button class="tws-mini-action danger" id="twsGlobalRepairRollback" type="button" ${globalRepairPlanCanRollback(plan) ? '' : 'disabled'}>Rollback plan</button>
  `;
  bar.querySelector('#twsGlobalRepairApprove')?.addEventListener('click', () => approveGlobalRepairPlanFromModal(modal));
  bar.querySelector('#twsGlobalRepairApply')?.addEventListener('click', () => applyGlobalRepairPlanFromModal(modal));
  bar.querySelector('#twsGlobalRepairRollback')?.addEventListener('click', () => rollbackGlobalRepairPlanFromModal(modal));
}

function setGlobalRepairPlanModalResult(modal, plan) {
  const body = modal.querySelector('#twsModalBody');
  if (!body) return;
  body.innerHTML = `
    <div id="twsGlobalRepairActions" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px"></div>
    ${globalRepairPlanResultHtml(plan)}
  `;
  wireGlobalRepairPlanModal(modal, plan);
}

function readGlobalRepairPlanPayload(modal) {
  const mode = modal.querySelector('#twsGlobalRepairMode')?.value || RP.globalRepair.mode || 'conservative';
  const rawMaxPlacements = Number(modal.querySelector('#twsGlobalRepairMaxPlacements')?.value || 8);
  const rawSolverSeconds = Number(modal.querySelector('#twsGlobalRepairSolverSeconds')?.value || 5);
  const maxPlacements = Math.max(1, Math.min(25, Number.isFinite(rawMaxPlacements) ? rawMaxPlacements : 8));
  const maxSolverSeconds = Math.max(1, Math.min(30, Number.isFinite(rawSolverSeconds) ? rawSolverSeconds : 5));
  const courseKeysText = modal.querySelector('#twsGlobalRepairCourseKeys')?.value || '';
  const courseKeys = courseKeysText.split(',').map(part => part.trim()).filter(Boolean);
  RP.globalRepair.mode = mode;
  RP.globalRepair.maxPlacements = maxPlacements;
  RP.globalRepair.maxSolverSeconds = maxSolverSeconds;
  RP.globalRepair.courseKeysText = courseKeysText;
  return {
    scenario_id: S.scenarioId,
    mode,
    max_placements: maxPlacements,
    course_keys: courseKeys,
    limits: {
      max_candidates: 8,
      max_solver_seconds: maxSolverSeconds,
    },
    notes: 'Created from split workspace global unresolved-student repair plan.',
  };
}

async function createGlobalRepairPlanFromModal(modal) {
  if (!S.scenarioId || RP.globalRepair.busy) return false;
  const status = modal.querySelector('#twsGlobalRepairStatus');
  if (status) {
    status.style.display = '';
    status.textContent = 'Building global repair plan...';
  }
  RP.globalRepair.busy = true;
  const data = await api('/ops/tw/repair/global-plans/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(readGlobalRepairPlanPayload(modal)),
  });
  RP.globalRepair.busy = false;
  if (!data) {
    if (status) status.textContent = 'Global repair plan failed. Check the current scenario state and try again.';
    notify.error('Global repair plan failed');
    return false;
  }
  RP.globalRepair.plan = data;
  setGlobalRepairPlanModalResult(modal, data);
  notify.success('Global repair plan ready');
  return false;
}

async function approveGlobalRepairPlanFromModal(modal) {
  const planId = RP.globalRepair.plan?.plan?.id;
  if (!planId) return;
  const data = await api(`/ops/tw/repair/global-plans/${encodeURIComponent(planId)}/approve/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ notes: 'Approved from split workspace global repair plan.' }),
  });
  if (!data) {
    notify.error('Global repair approval failed');
    return;
  }
  RP.globalRepair.plan = data;
  setGlobalRepairPlanModalResult(modal, data);
  notify.success('Global repair plan approved');
}

async function applyGlobalRepairPlanFromModal(modal) {
  const planId = RP.globalRepair.plan?.plan?.id;
  if (!planId) return;
  const ok = await confirmRepairAction({
    title: 'Apply global repair plan',
    sub: 'This applies all approved repair items and writes timetable/student assignment changes.',
    body: `<div class="tws-repair-panel">
      ${globalRepairPlanSummaryHtml(RP.globalRepair.plan)}
      <div class="tws-repair-explain">
        <div class="tws-repair-section-title">Controlled global write</div>
        <div class="tws-repair-explain-row"><b>Primary target</b><span>Reduce unresolved students using fresh approved repair runs.</span></div>
        <div class="tws-repair-explain-row"><b>Safety</b><span>Each item uses the existing candidate preflight before write.</span></div>
      </div>
    </div>`,
    confirmLabel: 'Apply plan',
  });
  if (!ok) {
    openGlobalRepairPlanModal(RP.globalRepair.plan);
    return;
  }
  const data = await api(`/ops/tw/repair/global-plans/${encodeURIComponent(planId)}/apply/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  if (!data) {
    notify.error('Global repair apply failed');
    openGlobalRepairPlanModal(RP.globalRepair.plan);
    return;
  }
  RP.globalRepair.plan = data;
  await refreshAfterRepairMutation();
  notify.success('Global repair plan applied');
  openGlobalRepairPlanModal(data);
}

async function rollbackGlobalRepairPlanFromModal(modal) {
  const planId = RP.globalRepair.plan?.plan?.id;
  if (!planId) return;
  const ok = await confirmRepairAction({
    title: 'Rollback global repair plan',
    sub: 'This rolls back the repair-owned writes in reverse order.',
    body: `<div class="tws-repair-panel">
      ${globalRepairPlanSummaryHtml(RP.globalRepair.plan)}
      <div class="tws-repair-explain">
        <div class="tws-repair-section-title">Rollback safety</div>
        <div class="tws-repair-explain-row"><b>Ownership</b><span>Only changes written by this global repair plan are rolled back.</span></div>
      </div>
    </div>`,
    confirmLabel: 'Rollback plan',
  });
  if (!ok) {
    openGlobalRepairPlanModal(RP.globalRepair.plan);
    return;
  }
  const data = await api(`/ops/tw/repair/global-plans/${encodeURIComponent(planId)}/rollback/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  if (!data) {
    notify.error('Global repair rollback failed');
    openGlobalRepairPlanModal(RP.globalRepair.plan);
    return;
  }
  RP.globalRepair.plan = data;
  await refreshAfterRepairMutation();
  notify.success('Global repair plan rolled back');
  openGlobalRepairPlanModal(data);
}

function openGlobalRepairPlanModal(plan = null) {
  if (!S.scenarioId) {
    notify.warning('Select a scenario first');
    return;
  }
  const existing = plan || RP.globalRepair.plan;
  openModal({
    title: 'Global unresolved-student repair',
    sub: 'Scenario-level plan using actual student repair runs',
    width: 'wide',
    body: existing
      ? `<div id="twsGlobalRepairActions" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px"></div>${globalRepairPlanResultHtml(existing)}`
      : globalRepairPlanFormHtml(),
    buttons: existing
      ? [{ label: 'Close' }]
      : [
          { label: 'Cancel' },
          { label: 'Build plan', variant: 'primary', onClick: createGlobalRepairPlanFromModal },
        ],
  });
  const modal = $('twsModal');
  if (existing && modal) wireGlobalRepairPlanModal(modal, existing);
}

async function refreshAfterRepairMutation() {
  RP.capacity = {};
  RP.builder.roomCache = {};
  RP.builder.studentCache = {};
  RP.studentBlockers = {
    token: RP.studentBlockers.token + 1,
    scenarioId: null,
    data: null,
    activeCourse: RP.studentBlockers.activeCourse,
  };
  for (let i = 0; i < paneCount(); i += 1) {
    if (S.panes[i].boardId) await loadAndRenderPane(i);
  }
  await refreshBoardsSummary();
  if (SB.open) await loadSidebarBudget();
}

function renderRpanelRepair(body) {
  if (!S.selectedPlacementId) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">◎</span>${IS_AR ? 'اختر شعبة أولاً' : 'Select a placement first'}</div>`;
    return;
  }
  const located = findPlacement(S.selectedPlacementId);
  if (!located) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">◎</span>${IS_AR ? 'الشعبة غير موجودة' : 'Selected placement was not found'}</div>`;
    return;
  }
  const placement = located.placement;
  const run = String(RP.repair.placementId) === String(placement.id) ? RP.repair.run : null;
  const candidates = run?.candidates || [];
  const bestId = run?.summary?.best_candidate_id || bestRepairCandidate(run)?.candidate_id || '';
  body.innerHTML = `<div class="tws-repair-panel">
    <div class="tws-repair-target">
      <b>${esc(placement.course_code)} ${esc(placement.section || '')}</b><br>
      <span>${esc(placement.day)} ${esc(placement.start_time)}-${esc(placement.end_time)} · ${esc(placement.room || 'No room')} · ${esc(placement.course_name || '')}</span>
    </div>
    <div class="tws-repair-form">
      <select id="twsRepairMode" class="tws-repair-mode">
        <option value="conservative" ${RP.repair.mode === 'conservative' ? 'selected' : ''}>Conservative</option>
        <option value="balanced" ${RP.repair.mode === 'balanced' ? 'selected' : ''}>Balanced</option>
        <option value="simulation" ${RP.repair.mode === 'simulation' ? 'selected' : ''}>Simulation only</option>
      </select>
      <select id="twsRepairMoveScope" class="tws-repair-mode">
        <option value="single_session" ${RP.repair.moveScope === 'single_session' ? 'selected' : ''}>Selected session only</option>
        <option value="all_sessions" ${RP.repair.moveScope === 'all_sessions' ? 'selected' : ''}>All sessions + lab</option>
        <option value="lectures_only" ${RP.repair.moveScope === 'lectures_only' ? 'selected' : ''}>Lectures only</option>
      </select>
      <textarea id="twsRepairBlockedIds" placeholder="Optional blocked student IDs, separated by commas">${esc(RP.repair.blockedIdsText || '')}</textarea>
      <button class="tws-btn primary" id="twsRunRepair" type="button" ${RP.repair.busy ? 'disabled' : ''}>${RP.repair.busy ? 'Analysing...' : 'Analyse move'}</button>
      <span>${IS_AR ? 'اترك القائمة فارغة لاستخدام طلبات المقرر من السيناريو.' : 'Leave blank to use scenario course demand as the recovery target.'}</span>
    </div>
    ${repairRunSummaryHtml(run)}
    <div class="tws-repair-list">
      ${candidates.length
        ? candidates.map(candidate => repairCandidateCardHtml(candidate, run, String(candidate.candidate_id) === String(bestId))).join('')
        : `<div class="tws-fix-empty">${IS_AR ? 'لم يتم تشغيل تحليل الإصلاح بعد.' : 'No repair analysis has been run for this selected section yet.'}</div>`}
    </div>
  </div>`;
  body.querySelector('#twsRepairBlockedIds')?.addEventListener('input', e => {
    RP.repair.blockedIdsText = e.target.value || '';
  });
  body.querySelector('#twsRepairMode')?.addEventListener('change', e => {
    RP.repair.mode = e.target.value || 'conservative';
  });
  body.querySelector('#twsRepairMoveScope')?.addEventListener('change', e => {
    RP.repair.moveScope = e.target.value || 'single_session';
  });
  body.querySelector('#twsRunRepair')?.addEventListener('click', runRepairAnalysisFromPanel);
  body.querySelector('#twsRepairReport')?.addEventListener('click', () => openRepairReportFromPanel());
  body.querySelector('#twsRepairRollback')?.addEventListener('click', rollbackRepairFromPanel);
  body.querySelectorAll('[data-repair-detail]').forEach(btn => {
    btn.addEventListener('click', () => {
      const candidate = candidates.find(c => String(c.candidate_id) === String(btn.dataset.repairDetail));
      if (candidate) repairCandidateDetailModal(candidate, run);
    });
  });
  body.querySelectorAll('[data-repair-approve]').forEach(btn => {
    btn.addEventListener('click', () => approveRepairCandidateFromPanel(btn.dataset.repairApprove));
  });
  body.querySelectorAll('[data-repair-report]').forEach(btn => {
    btn.addEventListener('click', () => openRepairReportFromPanel(btn.dataset.repairReport));
  });
  body.querySelectorAll('[data-repair-apply]').forEach(btn => {
    btn.addEventListener('click', () => applyRepairCandidateFromPanel(btn.dataset.repairApply));
  });
}

function selectionRoomSlot(placement, pending) {
  return pending
    ? { day: pending.day, start: pending.start, end: pending.end, label: 'Preview slot' }
    : { day: placement.day, start: placement.start_time, end: placement.end_time, label: 'Current slot' };
}

function roomCacheKey(placementId, slot) {
  return [placementId, slot.day, slot.start, slot.end].join('|');
}

function roomCandidateStatusText(room) {
  const reasons = Array.isArray(room?.reasons) ? room.reasons.filter(Boolean) : [];
  if (!roomIsAssignable(room)) {
    return reasons.slice(0, 3).join(' | ') || 'Blocked for this section';
  }
  if (roomIsScheduleClean(room)) return 'Room fits and the schedule is clean';
  if (roomIsAssignable(room) && reasons.length) return reasons.slice(0, 3).join(' | ');
  if (Number(room.validation?.critical_count || 0) > 0) {
    return reasons.slice(0, 3).join(' | ') || 'Room fits, but time/instructor conflict remains';
  }
  if (Number(room.validation?.warning_count || 0) > 0) {
    return reasons.slice(0, 3).join(' | ') || 'Room fits, with schedule warning';
  }
  return reasons.slice(0, 3).join(' | ') || 'Room fits this section';
}

function roomCandidateCardHtml(room, idx, currentRoom) {
  const tone = room.tone || (roomIsScheduleClean(room) ? 'clean' : roomIsAssignable(room) ? 'warn' : 'block');
  const detail = roomCandidateStatusText(room);
  const isCurrent = String(currentRoom || '').trim().toUpperCase() === String(room.room_code || '').trim().toUpperCase();
  const apply = roomIsAssignable(room)
    ? `<button class="tws-room-apply" data-room-idx="${idx}" type="button">${isCurrent ? 'Keep' : 'Apply'}</button>`
    : `<button class="tws-room-apply" type="button" disabled>Blocked</button>`;
  return `<div class="tws-room-card ${tone}">
    <div class="tws-room-main">
      <b>${esc(room.room_code)}</b>
      <span>${esc(room.room_type)} · ${esc(room.section || 'Any')} · ${room.capacity || 0}</span>
      ${apply}
    </div>
    <div class="tws-room-detail">${esc(detail)}</div>
    ${room.occupied_by && room.occupied_by.length
      ? `<div class="tws-room-occupied">${room.occupied_by.slice(0, 2).map(o => `${esc(o.board_label)} ${esc(o.section)} ${esc(o.start)}-${esc(o.end)}`).join('<br>')}</div>`
      : ''}
  </div>`;
}

function studentEvidenceHtml(data) {
  if (!data) return `<div class="tws-fix-empty">Loading student evidence...</div>`;
  const conflicts = data.conflicts || [];
  if (!conflicts.length) {
    return `<div class="tws-fix-empty">No direct student evidence for this placement's current time.</div>`;
  }
  const rows = conflicts.slice(0, 4).map(c => `<div class="tws-student-evidence-row">
    <div class="tws-student-evidence-main">
      <b>${esc(c.course_code)}-${esc(c.section)}</b>
      <span>${esc(c.board_label)} · ${esc(c.time)}</span>
      <em>${c.affected_count || 0} students</em>
    </div>
    ${(c.students || []).slice(0, 5).map(s => `<div class="tws-student-chip">${esc(s.student_id)} · ${esc(s.program || '')}${s.section ? ` ${esc(s.section)}` : ''}${s.total_earned_credits != null ? ` · ${s.total_earned_credits}cr` : ''}</div>`).join('')}
  </div>`).join('');
  return `<div class="tws-student-evidence-summary"><b>${data.affected_student_count || 0}</b> affected unique students · ${esc(data.source || '')}</div>${rows}`;
}

async function applyRoomCandidate(placementId, room, slot, opts = {}) {
  const {
    refresh = true,
    notifyResult = true,
    recordUndo = true,
    requireScheduleClean = false,
  } = opts;
  const located = findPlacement(placementId);
  if (!located || !room?.room_code) return null;
  if (requireScheduleClean && !roomIsScheduleClean(room)) {
    if (notifyResult) notify.warning('This room is not clean enough for bulk assignment.');
    return null;
  }
  if (!roomIsAssignable(room)) {
    if (notifyResult) notify.warning('This room does not meet the section requirements.');
    return null;
  }
  const p = located.placement;
  const oldRoom = p.room || '';
  const oldDay = p.day;
  const oldStart = p.start_time;
  const oldEnd = p.end_time;
  if (oldRoom === room.room_code && oldDay === slot.day && oldStart === slot.start) {
    if (notifyResult) notify.success('Room already assigned');
    return { already_assigned: true };
  }
  const data = await doMove(placementId, slot.day, slot.start, slot.end, room.room_code);
  if (!data) {
    if (notifyResult) notify.error('Room assignment failed');
    return null;
  }
  if (recordUndo) {
    S.undoStack.push({
      type: 'move',
      placement_id: placementId,
      old_day: oldDay,
      old_start: oldStart,
      old_end: oldEnd,
      old_room: oldRoom,
      new_day: slot.day,
      new_start: slot.start,
      new_end: slot.end,
      new_room: room.room_code,
    });
    S.redoStack = [];
    updateUndoRedoButtons();
  }
  clearSlotAssist();
  const boardId = S.panes[located.paneIdx]?.boardId;
  if (refresh) {
    for (let i = 0; i < paneCount(); i++) {
      if (S.panes[i].boardId === boardId) await loadAndRenderPane(i);
    }
    await refreshBoardsSummary();
    S.selectedPlacementId = placementId;
    S.selectedPaneIdx = located.paneIdx;
  }
  if (notifyResult) notify.success(`Assigned room ${room.room_code}`);
  return data;
}

async function refreshSelectionBuilderPanels(body, placement, pending) {
  const roomBox = body.querySelector('#twsRoomCandidates');
  const studentBox = body.querySelector('#twsStudentEvidence');
  const placementId = placement.id;
  const slot = selectionRoomSlot(placement, pending);
  const token = ++RP.builder.token;
  if (roomBox) roomBox.innerHTML = `<div class="tws-fix-empty">Finding rooms for ${esc(slot.day)} ${esc(slot.start)}-${esc(slot.end)}...</div>`;
  if (studentBox) studentBox.innerHTML = studentEvidenceHtml(RP.builder.studentCache[placementId]);

  const key = roomCacheKey(placementId, slot);
  const roomPromise = RP.builder.roomCache[key]
    ? Promise.resolve(RP.builder.roomCache[key])
    : api(`/ops/tw/placements/${placementId}/room-candidates/?${new URLSearchParams({ day: slot.day, start_time: slot.start, end_time: slot.end }).toString()}`).then(data => {
      if (data) RP.builder.roomCache[key] = data;
      return data;
    });
  const studentPromise = RP.builder.studentCache[placementId]
    ? Promise.resolve(RP.builder.studentCache[placementId])
    : api(`/ops/tw/placements/${placementId}/student-evidence/?limit=40`).then(data => {
      if (data) RP.builder.studentCache[placementId] = data;
      return data;
    });

  const [roomData, studentData] = await Promise.all([roomPromise, studentPromise]);
  if (token !== RP.builder.token || !RP.open || RP.tab !== 'selection' || String(S.selectedPlacementId) !== String(placementId)) return;
  if (roomBox) {
    const rooms = roomData?.candidates || [];
    const target = roomData?.target || {};
    const onlineRoom = !!(target.is_online || roomData?.summary?.is_online);
    roomBox.innerHTML = onlineRoom
      ? `<div class="tws-room-target">
          <b>${esc(slot.label)}</b>
          <span>${esc(slot.day)} ${esc(slot.start)}-${esc(slot.end)} · online</span>
        </div>
        <div class="tws-fix-empty">${IS_AR ? 'مقرر إلكتروني، لا يحتاج إلى قاعة.' : 'Online course, no physical room needed.'}</div>`
      : `<div class="tws-room-target">
          <b>${esc(slot.label)}</b>
          <span>${esc(slot.day)} ${esc(slot.start)}-${esc(slot.end)} · ${esc(target.required_type || '')} · ${target.required_capacity || 0} seats${target.required_gender ? ` · ${esc(target.required_gender)}` : ''}</span>
        </div>
        ${rooms.length ? rooms.slice(0, 8).map((room, idx) => roomCandidateCardHtml(room, idx, placement.room)).join('') : `<div class="tws-fix-empty">No rooms found.</div>`}`;
    roomBox.querySelectorAll('.tws-room-apply[data-room-idx]').forEach(btn => {
      btn.addEventListener('click', () => {
        const room = rooms[parseInt(btn.dataset.roomIdx || '-1')];
        applyRoomCandidate(placementId, room, slot);
      });
    });
  }
  if (studentBox) studentBox.innerHTML = studentEvidenceHtml(studentData);
}

function renderRpanelSelection(body) {
  if (!S.selectedPlacementId) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">◉</span>${IS_AR ? 'اختر شعبة للاطلاع' : 'Click a placement to inspect'}</div>`;
    return;
  }
  const located = findPlacement(S.selectedPlacementId);
  if (!located) { body.innerHTML = `<div class="tws-empty-state"><span class="ic">◉</span>${IS_AR ? 'غير موجود' : 'Not found'}</div>`; return; }
  const p = located.placement;
  const instructor = (p.meetings && p.meetings[0]) ? p.meetings[0].instructor : '—';
  const board = S.boards.find(b => b.id === S.panes[located.paneIdx].boardId) || { label: '—' };
  const pending = S.slotAssist.pendingMove && String(S.slotAssist.pendingMove.placementId) === String(p.id)
    ? S.slotAssist.pendingMove
    : null;
  const pendingCandidate = pending
    ? S.slotAssist.candidates.get(slotAssistKey(S.slotAssist.kind, pending.day, pending.start))
    : null;
  const instructorKey = protectionValue(instructor);
  const roomKey = protectionValue(p.room);
  const timeKey = timeProtectionKey(p.day, p.start_time);
  const instructorProtected = instructorKey && S.protections.instructors.has(instructorKey);
  const roomProtected = roomKey && roomKey !== 'UNASSIGNED' && S.protections.rooms.has(roomKey);
  const timeProtected = S.protections.times.has(timeKey);
  const protectionReasons = placementProtectionReasons(p);
  const selectedIssues = placementIssuesFromMap(
    placementIssueMap((S.panes[located.paneIdx].boardData || {}).conflicts || {}, S.panes[located.paneIdx].boardId),
    p.id,
  );
  body.innerHTML = `
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'المقرر' : 'Course'}</span><span class="value">${esc(p.course_code)}</span></div>
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'الاسم' : 'Name'}</span><span class="value">${esc(p.course_name || '—')}</span></div>
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'الشعبة' : 'Section'}</span><span class="value">${esc(p.section || '—')}</span></div>
    ${planLensDetailsFieldsHtml(p)}
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'اللوحة' : 'Board'}</span><span class="value">${esc(board.label)}</span></div>
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'اليوم' : 'Day'}</span><span class="value">${esc(p.day)}</span></div>
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'الوقت' : 'Time'}</span><span class="value">${esc(p.start_time)} – ${esc(p.end_time)}</span></div>
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'القاعة' : 'Room'}</span><span class="value">${esc(p.room || '—')}</span></div>
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'المدرس' : 'Instructor'}</span><span class="value">${esc(instructor)}</span></div>
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'السعة' : 'Capacity'}</span><span class="value">${p.available_capacity || '—'}</span></div>
    <div class="tws-sel-field"><span class="label">${IS_AR ? 'مقفل' : 'Locked'}</span><span class="value">${p.is_locked ? (IS_AR ? 'نعم' : 'Yes') : (IS_AR ? 'لا' : 'No')}</span></div>
    ${selectionConflictEvidenceHtml(selectedIssues)}
    <div class="tws-protect-panel">
      <div class="tws-protect-title">${IS_AR ? 'حماية التحريك التلقائي' : 'Move protection'}</div>
      <div class="tws-protect-grid">
        <button class="tws-protect-btn${p.is_locked ? ' on' : ''}" data-protect="section">${p.is_locked ? (IS_AR ? 'فتح الشعبة' : 'Unlock section') : (IS_AR ? 'قفل الشعبة' : 'Lock section')}</button>
        <button class="tws-protect-btn${instructorProtected ? ' on' : ''}" data-protect="instructor" ${instructorKey ? '' : 'disabled'}>${IS_AR ? 'حماية المدرس' : 'Protect instructor'}</button>
        <button class="tws-protect-btn${roomProtected ? ' on' : ''}" data-protect="room" ${roomKey && roomKey !== 'UNASSIGNED' ? '' : 'disabled'}>${IS_AR ? 'حماية القاعة' : 'Protect room'}</button>
        <button class="tws-protect-btn${timeProtected ? ' on' : ''}" data-protect="time">${IS_AR ? 'حماية الوقت' : 'Protect time'}</button>
      </div>
      <div class="tws-protect-note">${protectionReasons.length ? esc(protectionReasons.join(' · ')) : (IS_AR ? 'غير محمي من الإصلاح التلقائي.' : 'Not protected from auto-fix.')}</div>
    </div>
    ${guidedResolverHtml(p)}
    <div class="tws-sel-action">
      ${pending
        ? `<b>${IS_AR ? 'معاينة النقل' : 'Move preview'}:</b> ${esc(pending.day)} ${esc(pending.start)}-${esc(pending.end)} · ${esc(splitSlotDetail(pendingCandidate))}<br>${IS_AR ? 'انقر نفس الخلية مرة أخرى للتطبيق.' : 'Click the same highlighted slot again to apply.'}`
        : `<b>${IS_AR ? 'إجراء سريع' : 'Quick action'}:</b> ${IS_AR ? 'بعد تحديد الشعبة، انقر خانة مضاءة للمعاينة ثم انقرها مرة ثانية للنقل.' : 'After selecting this section, click a highlighted empty slot to preview, then click it again to move.'}`}
    </div>
    <div class="tws-builder-panel">
      <div class="tws-builder-title">${IS_AR ? 'وضع القاعة' : 'Room assignment mode'}</div>
      <div id="twsRoomCandidates" class="tws-room-list"><div class="tws-fix-empty">${IS_AR ? 'جاري فحص القاعات...' : 'Checking rooms...'}</div></div>
    </div>
    <div class="tws-builder-panel">
      <div class="tws-builder-title">${IS_AR ? 'دليل الطلاب' : 'Student evidence'}</div>
      <div id="twsStudentEvidence" class="tws-student-evidence"><div class="tws-fix-empty">${IS_AR ? 'جاري تحميل الطلاب...' : 'Loading students...'}</div></div>
    </div>
    <div style="display:flex;gap:6px;margin-top:12px">
      <button class="tws-btn" id="twsSelOpen" style="flex:1">${IS_AR ? 'عرض التفاصيل' : 'Open drawer'}</button>
      <button class="tws-btn primary" id="twsSelRepair" style="flex:1">${IS_AR ? 'إصلاح' : 'Repair'}</button>
    </div>
  `;
  // Wire the button — no inline onclick
  const openBtn = body.querySelector('#twsSelOpen');
  if (openBtn) openBtn.addEventListener('click', () => openDrawer(located.paneIdx, p.id));
  const repairBtn = body.querySelector('#twsSelRepair');
  if (repairBtn) repairBtn.addEventListener('click', () => setRpanelTab('repair'));
  const guidedBtn = body.querySelector('#twsApplyGuidedFix');
  if (guidedBtn) guidedBtn.addEventListener('click', applyGuidedResolver);
  body.querySelectorAll('.tws-protect-btn[data-protect]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const kind = btn.dataset.protect;
      if (kind === 'section') {
        await doToggleLock(p.id, located.paneIdx);
        return;
      }
      if (kind === 'instructor') toggleProtection('instructors', instructor);
      if (kind === 'room') toggleProtection('rooms', p.room);
      if (kind === 'time') toggleProtection('times', timeKey);
      RP.fixQueue.cache = {};
      RP.fixQueue.items = [];
      if (RP.open) renderRpanel();
      notify.success(IS_AR ? 'تم تحديث الحماية' : 'Protection updated');
    });
  });
  refreshSelectionBuilderPanels(body, p, pending);
}

function selectionConflictEvidenceHtml(issues) {
  if (!issues.length) {
    return `<div class="tws-conflict-evidence clean">
      <div class="tws-conflict-title">${IS_AR ? 'دليل التعارض' : 'Clash evidence'}<span>${IS_AR ? 'نظيف' : 'Clean'}</span></div>
      <div class="tws-conflict-empty">${IS_AR ? 'لا توجد تعارضات مسجلة لهذه الشعبة.' : 'No recorded clashes for this placement.'}</div>
    </div>`;
  }
  const rows = issues.map(issue => `<div class="tws-conflict-row ${esc(issue.tone || '')}">
    <span class="dot"></span>
    <div>
      <b>${esc(issue.kind)}</b>
      <strong>${esc(issue.title || '')}</strong>
      <em>${esc(issue.detail || '')}</em>
    </div>
  </div>`).join('');
  return `<div class="tws-conflict-evidence">
    <div class="tws-conflict-title">${IS_AR ? 'دليل التعارض' : 'Clash evidence'}<span>${issues.length}</span></div>
    ${rows}
  </div>`;
}

/* ── Left sidebar (required sections, drag-create) ───────────────── */
function toggleSidebar(force) {
  SB.open = (typeof force === 'boolean') ? force : !SB.open;
  const body = $('twsBody');
  body.classList.toggle('sb-open', SB.open);
  const btn = $('twsSidebarToggle');
  if (btn) btn.classList.toggle('primary', SB.open);
  if (SB.open) loadSidebarBudget();
}
async function loadSidebarBudget() {
  if (!S.scenarioId) { renderSidebar(); return; }
  const data = await api(`/ops/tw/scenarios/${S.scenarioId}/budget/`);
  SB.budget = (data && data.budget) || [];
  renderSidebar();
}
function renderSidebar() {
  const body = $('twsSidebarBody');
  if (!body) return;
  if (!S.scenarioId) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">ⓘ</span>${IS_AR ? 'اختر سيناريو أولاً' : 'Select a scenario first'}</div>`;
    return;
  }
  const q = (SB.search || S.search || '').trim().toUpperCase();
  const filtered = SB.budget
    .filter(itemMatchesPlanLens)
    .filter(b => budgetMatchesSplitSearch(b, q))
    .sort((a, b) => (b.remaining_sections || 0) - (a.remaining_sections || 0) || courseKeyOf(a).localeCompare(courseKeyOf(b)));
  if (!filtered.length) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">—</span>${IS_AR ? 'لا توجد نتائج' : 'No matches'}</div>`;
    return;
  }
  body.innerHTML = filtered.map(b => {
    const used = b.used_sections || 0;
    const planned = b.planned_sections || 0;
    const remaining = b.remaining_sections != null ? b.remaining_sections : Math.max(0, planned - used);
    const exhausted = remaining <= 0;
    const key = courseKeyOf(b);
    const courseLens = courseLensForItem(b);
    const planBits = courseLens && courseLens.plans
      ? Object.entries(courseLens.plans).filter(([, count]) => Number(count) > 0).map(([plan, count]) => `${plan} ${count}`).join(' ')
      : '';
    // Store payload as an id; the real object lives in a side map so we
    // don't have to round-trip JSON through a double-quoted attribute,
    // which would mis-escape any single-quote character in the string.
    return `<div class="tws-sec-item${exhausted ? ' exhausted' : ''}" draggable="${exhausted ? 'false' : 'true'}" data-key="${esc(key)}" data-code="${esc(b.course_code)}" data-used="${used}" title="${esc(b.department || b.course_code)} · ${b.credit_hours || 0}h">
      <div class="code">
        <span>${esc(b.course_code)}</span>
        <span class="count${exhausted ? ' exhausted' : ''}"><span class="used">${used}</span>/${planned}</span>
      </div>
      <div class="meta">
        <span>${esc(b.department || '')} · T${b.programme_term || '?'}</span>
      </div>
      <div class="meta">
        ${b.total_demand ? `<span class="dem">${b.total_demand} ${IS_AR ? 'طالب' : 'stu'}</span>` : ''}
        ${b.credit_hours ? `<span class="dem">${b.credit_hours}h</span>` : ''}
        ${b.max_per_section ? `<span class="dem">${IS_AR ? 'حد' : 'cap'} ${b.max_per_section}</span>` : ''}
      </div>
      ${planBits ? `<div class="meta plan-line">${esc(planBits)}${courseLens.shared_overflow ? ' | shared overflow' : ''}</div>` : ''}
    </div>`;
  }).join('');
  // Wire drag sources — look up the budget row by code + used count at
  // dragstart time so we always send fresh data even after budget updates.
  body.querySelectorAll('.tws-sec-item[draggable="true"]').forEach(el => {
    el.addEventListener('click', () => {
      const key = el.dataset.key || el.dataset.code;
      const b = SB.budget.find(x => courseKeyOf(x) === key);
      if (!b) return;
      beginCreateSectionAssist(createPayloadFromBudgetRow(b));
    });
    el.addEventListener('dragstart', (e) => {
      const key = el.dataset.key || el.dataset.code;
      const b = SB.budget.find(x => courseKeyOf(x) === key) || {};
      const payload = createPayloadFromBudgetRow(b, parseInt(el.dataset.used || '0', 10));
      try {
        e.dataTransfer.setData('text/plain', JSON.stringify(payload));
        e.dataTransfer.effectAllowed = 'copy';
      } catch {}
    });
  });
}

/* ── Shared modal primitive ──────────────────────────────────────── */
// openModal({ title, sub, body, width, buttons: [{label, variant, onClick}], onClose })
// buttons: variant in { 'primary', '' (default) }. onClick can return false to keep modal open.
let _modalOpen = false;
let _modalLastFocus = null;
function openModal(opts) {
  const modal = $('twsModal');
  const backdrop = $('twsModalBackdrop');
  _modalLastFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  modal.hidden = false;
  backdrop.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
  backdrop.setAttribute('aria-hidden', 'false');
  $('twsModalTitle').textContent = opts.title || '';
  $('twsModalSub').textContent = opts.sub || '';
  const bodyEl = $('twsModalBody');
  if (typeof opts.body === 'string') bodyEl.innerHTML = opts.body;
  else { bodyEl.innerHTML = ''; if (opts.body) bodyEl.appendChild(opts.body); }
  // Width class
  modal.classList.remove('wide', 'xwide');
  if (opts.width === 'wide') modal.classList.add('wide');
  else if (opts.width === 'xwide') modal.classList.add('xwide');
  // Buttons
  const footer = $('twsModalFooter');
  footer.innerHTML = '';
  (opts.buttons || []).forEach(b => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = b.label;
    if (b.variant === 'primary') btn.classList.add('primary');
    if (b.id) btn.id = b.id;
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const res = b.onClick ? await b.onClick(modal) : undefined;
      if (res !== false) closeModal();
    });
    footer.appendChild(btn);
  });
  modal.classList.add('open');
  backdrop.classList.add('open');
  _modalOpen = true;
  _modalOnClose = opts.onClose || null;
  // Focus the first actionable control; confirmation dialogs often only have footer buttons.
  setTimeout(() => {
    const target = bodyEl.querySelector('input, select, textarea, button')
      || footer.querySelector('button')
      || $('twsModalClose');
    target?.focus();
  }, 40);
}
let _modalOnClose = null;
function closeModal() {
  const modal = $('twsModal');
  const backdrop = $('twsModalBackdrop');
  modal.classList.remove('open');
  backdrop.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  backdrop.setAttribute('aria-hidden', 'true');
  modal.hidden = true;
  backdrop.hidden = true;
  _modalOpen = false;
  if (_modalOnClose) { try { _modalOnClose(); } catch {} _modalOnClose = null; }
  const focusTarget = _modalLastFocus;
  _modalLastFocus = null;
  if (focusTarget && document.contains(focusTarget)) {
    setTimeout(() => focusTarget.focus?.(), 0);
  }
}

function leaveSplitWorkspace() {
  if (window.history.length > 1) window.history.back();
  else window.location.href = '/timetable-workspace/';
}

function confirmCloseSplitWorkspace() {
  if (_modalOpen) return;
  closeOpenDetailsMenus();
  openModal({
    title: IS_AR ? 'إغلاق مساحة المقارنة؟' : 'Close split compare?',
    sub: IS_AR ? 'ستغادر هذه الشاشة.' : 'You will leave this split workspace.',
    body: `<div class="hint">${IS_AR ? 'هل أنت متأكد أنك تريد إغلاق هذه النافذة؟' : 'Are you sure you want to close this window?'}</div>`,
    buttons: [
      { label: IS_AR ? 'لا' : 'No' },
      { label: IS_AR ? 'نعم، إغلاق' : 'Yes, close', variant: 'primary', onClick: leaveSplitWorkspace },
    ],
  });
}

/* ── Generate Scenario (inline) ──────────────────────────────────── */
async function openGenerateModal() {
  const strategies = [
    { v: 'compact', l: IS_AR ? 'مضغوط' : 'Compact' },
    { v: 'morning', l: IS_AR ? 'صباحي' : 'Morning-first' },
    { v: 'balanced', l: IS_AR ? 'متوازن' : 'Balanced' },
    { v: 'optimal', l: IS_AR ? 'الأمثل' : 'Optimal (CP-SAT)' },
    { v: 'hybrid', l: IS_AR ? 'هجين' : 'Hybrid (best)' },
    { v: 'load_balanced', l: IS_AR ? 'متوازن الأيام' : 'Load-balanced' },
    { v: 'adaptive', l: IS_AR ? 'تكيّفي' : 'Adaptive (best overall)' },
  ];
  const body = `
    <div class="tws-form-grid">
      <div><label for="twsGenYear">${IS_AR ? 'السنة' : 'Year'}</label>
        <input id="twsGenYear" type="text" inputmode="numeric" value="${DEFAULT_YEAR || '1448'}"></div>
      <div><label for="twsGenSem">${IS_AR ? 'فصل' : 'Sem'}</label>
        <input id="twsGenSem" type="text" inputmode="numeric" value="${DEFAULT_TERM || '1'}"></div>
      <div style="grid-column:span 2"><label for="twsGenProgram">${IS_AR ? 'البرنامج' : 'Program'} (AI, DS, COE…)</label>
        <input id="twsGenProgram" type="text" placeholder="AI,DS" autocapitalize="characters"></div>
      <div><label for="twsGenSection">${IS_AR ? 'شعبة' : 'Section'}</label>
        <input id="twsGenSection" type="text" placeholder="M" autocapitalize="characters"></div>
      <div class="full"><label for="twsGenStrategy">${IS_AR ? 'الاستراتيجية' : 'Strategy'}</label>
        <select id="twsGenStrategy">
          ${strategies.map(s => `<option value="${s.v}"${s.v === 'compact' ? ' selected' : ''}>${s.l}</option>`).join('')}
        </select></div>
      <div class="hint">${IS_AR ? 'يمكن تحديد عدة برامج بفصلها بفاصلة (مثال: AI,DS)' : 'Multiple programs may be comma-separated (e.g. AI,DS). Leave section blank for all sections.'}</div>
      <div id="twsGenStatus" class="full" style="color:var(--t4);font-size:11px;min-height:16px"></div>
    </div>
  `;
  openModal({
    title: IS_AR ? 'توليد سيناريو جديد' : 'Generate new scenario',
    sub: IS_AR ? 'سيُنشئ السيناريو مع اللوحات وتصنيف الطلاب' : 'Creates scenario, boards and student classification',
    body,
    width: 'wide',
    buttons: [
      { label: IS_AR ? 'إلغاء' : 'Cancel' },
      { label: IS_AR ? 'توليد' : 'Generate', variant: 'primary', id: 'twsGenSubmit', onClick: async (modal) => {
        const year = parseInt(document.getElementById('twsGenYear').value.trim());
        const semester = parseInt(document.getElementById('twsGenSem').value.trim());
        const program = document.getElementById('twsGenProgram').value.trim().toUpperCase();
        const section = (document.getElementById('twsGenSection').value.trim().toUpperCase() || null);
        const strategy = document.getElementById('twsGenStrategy').value;
        if (!year || !semester || !program) {
          notify.error(IS_AR ? 'أدخل السنة، الفصل، والبرنامج' : 'Enter year, semester, and program');
          return false;
        }
        const statusEl = document.getElementById('twsGenStatus');
        const submitBtn = document.getElementById('twsGenSubmit');
        submitBtn.disabled = true;
        statusEl.textContent = IS_AR ? 'جاري التوليد…' : 'Generating…';
        const data = await api('/ops/tw/generate-workspace/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ year, semester, program, section, strategy }),
        });
        submitBtn.disabled = false;
        if (!data) { statusEl.textContent = IS_AR ? 'فشل' : 'Failed'; return false; }
        const ss = data.student_summary || {};
        notify.success(IS_AR
          ? `تم التوليد — ${data.boards.length} لوحة · ${ss.classified || 0} طالب`
          : `Generated — ${data.boards.length} boards · ${ss.classified || 0} students`);
        // Refresh scenarios list and jump to the new scenario
        await loadScenarios();
        const sel = $('twsScenario');
        sel.value = String(data.scenario.id);
        await onScenarioChange();
      } },
    ],
  });
}

/* ── New empty scenario + New board ──────────────────────────────── */
async function openNewScenarioModal() {
  const body = `
    <div class="tws-form-grid">
      <div><label for="twsSNYear">${IS_AR ? 'السنة' : 'Year'}</label>
        <input id="twsSNYear" type="text" inputmode="numeric" value="${DEFAULT_YEAR || '1448'}"></div>
      <div><label for="twsSNSem">${IS_AR ? 'فصل' : 'Sem'}</label>
        <input id="twsSNSem" type="text" inputmode="numeric" value="${DEFAULT_TERM || '1'}"></div>
      <div class="full"><label for="twsSNName">${IS_AR ? 'الاسم' : 'Name'}</label>
        <input id="twsSNName" type="text" placeholder="${IS_AR ? 'مثال: سيناريو أ' : 'e.g. Scenario A'}"></div>
      <div class="hint">${IS_AR ? 'يُنشئ سيناريو فارغ بدون أي لوحات. أضف لوحات بعد الإنشاء.' : 'Creates an empty scenario with no boards. Add boards after creation.'}</div>
    </div>
  `;
  openModal({
    title: IS_AR ? 'سيناريو فارغ' : 'Empty scenario',
    body, width: 'wide',
    buttons: [
      { label: IS_AR ? 'إلغاء' : 'Cancel' },
      { label: IS_AR ? 'إنشاء' : 'Create', variant: 'primary', onClick: async () => {
        const year = parseInt(document.getElementById('twsSNYear').value.trim());
        const term = parseInt(document.getElementById('twsSNSem').value.trim());
        const raw = (document.getElementById('twsSNName').value || '').trim();
        const name = raw || `Scenario ${new Date().toISOString().slice(0, 10)}`;
        if (!year || !term) { notify.error(IS_AR ? 'أدخل السنة والفصل' : 'Enter year and semester'); return false; }
        const data = await api('/ops/tw/scenarios/create/', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ academic_year: year, term, name }),
        });
        if (!data) return false;
        notify.success(IS_AR ? 'تم الإنشاء' : 'Scenario created');
        await loadScenarios();
        $('twsScenario').value = String(data.scenario.id);
        await onScenarioChange();
      } },
    ],
  });
}

async function openNewBoardModal() {
  if (!S.scenarioId) { notify.error(IS_AR ? 'اختر سيناريو أولاً' : 'Select a scenario first'); return; }
  const body = `
    <div class="tws-form-grid">
      <div class="full"><label for="twsBLabel">${IS_AR ? 'اسم اللوحة' : 'Board label'}</label>
        <input id="twsBLabel" type="text" placeholder="Term 5 Group B"></div>
      <div><label for="twsBTerm">${IS_AR ? 'المستوى' : 'Nominal term'}</label>
        <input id="twsBTerm" type="text" inputmode="numeric" placeholder="5"></div>
      <div style="grid-column:span 4"><label for="twsBNotes">${IS_AR ? 'ملاحظات' : 'Notes'}</label>
        <input id="twsBNotes" type="text" placeholder=""></div>
      <div class="hint">${IS_AR ? 'تُنشأ اللوحة فارغة. اسحب أو استخدم التوليد لملئها.' : 'Board starts empty — drag placements or re-run Generate to populate.'}</div>
    </div>
  `;
  openModal({
    title: IS_AR ? 'لوحة جديدة' : 'New board',
    sub: S.scenarioMeta ? S.scenarioMeta.name : '',
    body, width: 'wide',
    buttons: [
      { label: IS_AR ? 'إلغاء' : 'Cancel' },
      { label: IS_AR ? 'إنشاء' : 'Create', variant: 'primary', onClick: async () => {
        const label = (document.getElementById('twsBLabel').value || '').trim();
        if (!label) { notify.error(IS_AR ? 'أدخل اسم اللوحة' : 'Enter a board label'); return false; }
        const nominal = parseInt(document.getElementById('twsBTerm').value.trim()) || null;
        const notes = (document.getElementById('twsBNotes').value || '').trim();
        const payload = { scenario_id: S.scenarioId, label };
        if (nominal) payload.nominal_term = nominal;
        if (notes) payload.notes = notes;
        const data = await api('/ops/tw/boards/create/', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
        });
        if (!data) return false;
        notify.success(IS_AR ? 'تمت إضافة اللوحة' : 'Board added');
        // Refresh boards + slot bar; don't reload panes unless this is the first board.
        const bdata = await api(`/ops/tw/boards/?scenario_id=${S.scenarioId}`);
        if (bdata && bdata.boards) {
          S.boards = bdata.boards;
          renderSlotBar();
          updateAggregateMetrics();
        }
      } },
    ],
  });
}

/* ── Slot editor ─────────────────────────────────────────────────── */
function _parseSlotLines(text) {
  const out = [];
  // Normalise line endings — Windows CRLF would otherwise leave a trailing
  // \r on each line and the regex-driven branch would silently fail.
  String(text || '').replace(/\r\n/g, '\n').trim().split('\n').filter(l => l.trim()).forEach(line => {
    const parts = line.split(/\t|\s{2,}/).map(s => s.trim()).filter(Boolean);
    if (parts.length >= 3) out.push({ label: parts[0], start: parts[1], end: parts[2] });
    else if (parts.length === 2) out.push({ label: `${parts[0]}-${parts[1]}`, start: parts[0], end: parts[1] });
    else {
      const m = line.trim().match(/^(\S+)\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})$/);
      if (m) out.push({ label: m[1], start: m[2], end: m[3] });
    }
  });
  return out;
}
function openSlotEditorModal() {
  if (!S.scenarioId || !S.scenarioMeta) { notify.error(IS_AR ? 'اختر سيناريو أولاً' : 'Select a scenario first'); return; }
  if (S.scenarioMeta.status === 'published') { notify.error(IS_AR ? 'السيناريو منشور' : 'Published scenarios are read-only'); return; }
  const slots = S.scenarioMeta.slot_config || [];
  const labSlots = S.scenarioMeta.lab_slot_config || [];
  const slotsText = slots.length ? slots.map(s => `${s.label || ''}\t${s.start}\t${s.end}`).join('\n')
    : '09:00-10:15\t09:00\t10:15\n10:30-11:45\t10:30\t11:45\n13:00-14:15\t13:00\t14:15\n14:30-15:45\t14:30\t15:45\n16:00-17:15\t16:00\t17:15';
  const labText = labSlots.length ? labSlots.map(s => `${s.label || ''}\t${s.start}\t${s.end}`).join('\n')
    : 'Lab 1\t09:00\t10:40\nLab 2\t10:45\t12:25\nLab 3\t13:00\t14:40\nLab 4\t14:45\t16:25\nLab 5\t16:30\t18:10\nLab 6\t18:10\t19:50';

  const body = `
    <div class="tws-form-grid">
      <div class="full">
        <label for="twsSlotLect">${IS_AR ? '📚 فترات المحاضرات (75 دقيقة)' : '📚 Lecture slots (75 min)'}</label>
        <textarea id="twsSlotLect" rows="6">${esc(slotsText)}</textarea>
      </div>
      <div class="full">
        <label for="twsSlotLab">${IS_AR ? '🔬 فترات المعامل (100 دقيقة)' : '🔬 Lab slots (100 min)'}</label>
        <textarea id="twsSlotLab" rows="6">${esc(labText)}</textarea>
      </div>
      <div class="hint">${IS_AR ? 'كل سطر: الاسم [Tab] البداية [Tab] النهاية — مثال: 09:00-10:15[Tab]09:00[Tab]10:15' : 'One per line: label [Tab] start [Tab] end — e.g. 09:00-10:15[Tab]09:00[Tab]10:15'}</div>
    </div>
  `;
  openModal({
    title: IS_AR ? 'تعديل الفترات الزمنية' : 'Edit time slots',
    sub: S.scenarioMeta.name,
    body, width: 'wide',
    buttons: [
      { label: IS_AR ? 'إلغاء' : 'Cancel' },
      { label: IS_AR ? 'حفظ' : 'Save', variant: 'primary', onClick: async () => {
        const newLect = _parseSlotLines(document.getElementById('twsSlotLect').value);
        const newLab = _parseSlotLines(document.getElementById('twsSlotLab').value);
        if (!newLect.length) { notify.error(IS_AR ? 'لم يتم تحليل أي فترة' : 'No valid lecture slots parsed'); return false; }
        const data = await api(`/ops/tw/scenarios/${S.scenarioId}/slots/update/`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slot_config: newLect, lab_slot_config: newLab }),
        });
        if (!data) return false;
        S.scenarioMeta = data.scenario;
        notify.success(IS_AR
          ? `تم الحفظ — ${newLect.length} محاضرة · ${newLab.length} معمل`
          : `Saved — ${newLect.length} lecture + ${newLab.length} lab slots`);
        // Board data has its own slot_config copy; reload each pane
        for (let i = 0; i < paneCount(); i++) await loadAndRenderPane(i);
      } },
    ],
  });
}

/* ── Elective mapping ───────────────────────────────────────────── */
function openElectivesModal() {
  if (!S.scenarioMeta) { notify.error(IS_AR ? 'اختر سيناريو أولاً' : 'Select a scenario first'); return; }
  const year = S.scenarioMeta.academic_year;
  const term = S.scenarioMeta.term;
  const body = `
    <div class="tws-form-grid">
      <div><label for="twsElecYear">${IS_AR ? 'السنة' : 'Year'}</label>
        <input id="twsElecYear" type="text" inputmode="numeric" value="${year || ''}"></div>
      <div><label for="twsElecTerm">${IS_AR ? 'فصل' : 'Sem'}</label>
        <input id="twsElecTerm" type="text" inputmode="numeric" value="${term || ''}"></div>
      <div style="grid-column:span 3"><label for="twsElecProg">${IS_AR ? 'البرنامج' : 'Programme'}</label>
        <input id="twsElecProg" type="text" placeholder="AI" autocapitalize="characters"></div>
      <div class="hint">${IS_AR ? 'أدخل برنامج واحد (AI / DS / COE…). سيتم تحميل فتحات الاختيارية من خطة البرنامج.' : 'Enter a single programme (AI / DS / COE…). Elective placeholders are pulled from that programme plan.'}</div>
      <div class="full" id="twsElecList" style="margin-top:4px;max-height:50vh;overflow:auto"></div>
    </div>
  `;
  openModal({
    title: IS_AR ? 'ربط المقررات الاختيارية' : 'Map electives',
    sub: S.scenarioMeta.name,
    body, width: 'xwide',
    buttons: [
      { label: IS_AR ? 'تحميل' : 'Load', onClick: async () => {
        await loadElectivesInto(document.getElementById('twsElecList'));
        return false; // keep modal open
      } },
      { label: IS_AR ? 'إغلاق' : 'Close' },
      { label: IS_AR ? 'حفظ الربط' : 'Save mappings', variant: 'primary', onClick: async () => {
        const y = parseInt(document.getElementById('twsElecYear').value.trim());
        const t = parseInt(document.getElementById('twsElecTerm').value.trim());
        const prog = (document.getElementById('twsElecProg').value || '').trim().toUpperCase();
        if (!y || !t || !prog) { notify.error(IS_AR ? 'أدخل السنة والفصل والبرنامج' : 'Enter year, term, programme'); return false; }
        const mappings = [];
        document.querySelectorAll('#twsElecList .elec-chk:checked').forEach(chk => {
          mappings.push({ placeholder_code: chk.dataset.ph, course_code: chk.dataset.code });
        });
        const data = await api('/ops/electives/mapping/set/', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ academic_year: y, term: t, programme: prog, mappings }),
        });
        if (!data) return false;
        notify.success(IS_AR ? `تم حفظ ${data.created} ربط` : `${data.created} mappings saved`);
      } },
    ],
  });
  // Auto-load if programme defaults exist (first board's programme)
  const firstBoard = S.boards[0];
  if (firstBoard && firstBoard.program) {
    document.getElementById('twsElecProg').value = String(firstBoard.program).toUpperCase();
    loadElectivesInto(document.getElementById('twsElecList'));
  }
}
async function loadElectivesInto(listEl) {
  const y = parseInt(document.getElementById('twsElecYear').value.trim());
  const t = parseInt(document.getElementById('twsElecTerm').value.trim());
  const prog = (document.getElementById('twsElecProg').value || '').trim().toUpperCase();
  if (!y || !t || !prog) { notify.error(IS_AR ? 'أدخل السنة والفصل والبرنامج' : 'Enter year, term, programme'); return; }
  listEl.innerHTML = `<div class="tws-empty-state"><span class="ic">◌</span>${IS_AR ? 'جاري التحميل…' : 'Loading…'}</div>`;
  const [catData, mapData, phData] = await Promise.all([
    api(`/ops/electives/catalogue/?programme=${encodeURIComponent(prog)}`),
    api(`/ops/electives/mapping/?academic_year=${encodeURIComponent(y)}&term=${encodeURIComponent(t)}&programme=${encodeURIComponent(prog)}`),
    api(`/ops/electives/placeholders/?programme=${encodeURIComponent(prog)}`),
  ]);
  const catalogue = (catData && catData.items) || [];
  const currentMappings = (mapData && mapData.items) || [];
  const placeholders = (phData && phData.items) || [];
  if (!catalogue.length) {
    listEl.innerHTML = `<div class="tws-empty-state"><span class="ic">—</span>${IS_AR ? 'لا يوجد كتالوج اختيارية' : 'No elective catalogue'}</div>`;
    return;
  }
  if (!placeholders.length) {
    listEl.innerHTML = `<div class="tws-empty-state"><span class="ic">—</span>${IS_AR ? 'لا يوجد فتحات اختيارية في الخطة' : 'No elective placeholders'}</div>`;
    return;
  }
  const mapLookup = {};
  currentMappings.forEach(m => {
    if (!mapLookup[m.placeholder_code]) mapLookup[m.placeholder_code] = new Set();
    mapLookup[m.placeholder_code].add(m.course_code);
  });
  listEl.innerHTML = placeholders.map(ph => {
    const selected = mapLookup[ph.course_code] || new Set();
    return `<div style="padding:10px;margin-bottom:10px;border:1px solid var(--line);border-radius:8px;background:rgba(255,255,255,0.02)">
      <div style="font-weight:700;color:var(--teal);font-family:'JetBrains Mono',monospace;font-size:12px;margin-bottom:6px">
        ${esc(ph.course_code)}
        <span style="font-weight:400;color:var(--t4);font-size:10px">· T${ph.programme_term || '?'} · ${ph.type || 'Elective'} · ${ph.credit_hours || 3}cr</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:4px 12px">
        ${catalogue.map(c => `
          <label style="display:flex;gap:6px;align-items:center;font-size:11px;cursor:pointer;padding:2px 0">
            <input type="checkbox" class="elec-chk" data-ph="${esc(ph.course_code)}" data-code="${esc(c.course_code)}"${selected.has(c.course_code) ? ' checked' : ''}>
            <span style="font-family:'JetBrains Mono',monospace;font-weight:700">${esc(c.course_code)}</span>
            <span style="color:var(--t3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.course_name || '')}</span>
          </label>
        `).join('')}
      </div>
    </div>`;
  }).join('');
}

/* ── Bottom panel (Demand · Resources) ── */
function toggleBpanel(force) {
  BP.open = (typeof force === 'boolean') ? force : !BP.open;
  const bp = $('twsBpanel');
  if (!bp) return;
  bp.classList.toggle('collapsed', !BP.open);
  if (BP.open) renderBpanel();
}
function setBpanelTab(tab) {
  BP.tab = tab;
  document.querySelectorAll('.tws-bpanel-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });
  // Opening a tab also opens the panel if closed
  if (!BP.open) toggleBpanel(true);
  else renderBpanel();
}
function renderBpanel() {
  const body = $('twsBpanelBody');
  const meta = $('twsBpanelMeta');
  if (!body) return;
  if (BP.tab === 'demand') renderBpanelDemand(body, meta);
  else if (BP.tab === 'resources') renderBpanelResources(body, meta);
}
function renderBpanelDemand(body, meta) {
  const boards = aggregateVisibleBoardsData();
  if (!boards.length) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">ⓘ</span>${IS_AR ? 'لم يتم تحميل أي لوحة' : 'No boards loaded yet'}</div>`;
    if (meta) meta.textContent = '';
    return;
  }
  // Same scenario-token guard as the capacity tab to prevent stale writes.
  const token = S.scenarioId;
  const fetches = boards
    .filter(b => !RP.capacity[b.boardId])
    .map(b => api(`/ops/tw/boards/${b.boardId}/capacity/`).then(d => {
      if (d && S.scenarioId === token) RP.capacity[b.boardId] = d;
    }));
  Promise.all(fetches).then(() => {
    if (S.scenarioId === token) _renderDemandNow(body, meta, boards);
  });
  if (!fetches.length) _renderDemandNow(body, meta, boards);
}
function _renderDemandNow(body, meta, boards) {
  const { courses, totals } = aggregateCapacityCourses(boards);
  const totDemand = totals.demand;
  const totCap = totals.raw_capacity;
  const totDeficit = totals.deficit;
  if (!courses.length) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">ⓘ</span>${IS_AR ? 'لا توجد بيانات' : 'No demand data'}</div>`;
    if (meta) meta.textContent = '';
    return;
  }
  let html = `<table>
    <thead><tr>
      <th>${IS_AR ? 'المقرر' : 'Course'}</th>
      <th>${IS_AR ? 'الطلب' : 'Demand'}</th>
      <th>${IS_AR ? 'السعة' : 'Raw cap'}</th>
      <th>${IS_AR ? 'الشعب' : 'Sections'}</th>
      <th>${IS_AR ? 'العجز' : 'Deficit'}</th>
    </tr></thead><tbody>`;
  courses.forEach(c => {
    const name = c.course_name && c.course_name !== c.course_code ? `<small>${esc(c.course_name)}</small>` : '';
    html += `<tr>
      <td class="code" title="${esc(c.course_key || c.course_code)}">${esc(c.course_code)}${name}</td>
      <td>${c.demand}</td>
      <td>${c.raw_capacity}</td>
      <td>${c.placed_sections}</td>
      <td${c.deficit > 0 ? ' class="deficit"' : ''}>${c.deficit > 0 ? '-' + c.deficit : '0'}</td>
    </tr>`;
  });
  html += `</tbody><tfoot><tr>
    <td>${IS_AR ? 'الإجمالي' : 'Total'}</td>
    <td>${totDemand}</td>
    <td>${totCap}</td>
    <td>${courses.length}</td>
    <td${totDeficit > 0 ? ' class="deficit"' : ''}>${totDeficit > 0 ? '-' + totDeficit : '0'}</td>
  </tr></tfoot></table>`;
  body.innerHTML = html;
  if (meta) meta.textContent = `${courses.length} ${IS_AR ? 'مقرر' : 'courses'} · ${boards.length} ${IS_AR ? 'لوحات' : 'boards'}`;
}
function renderBpanelResources(body, meta) {
  const boards = aggregateVisibleBoardsData();
  if (!boards.length) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">ⓘ</span>${IS_AR ? 'لم يتم تحميل أي لوحة' : 'No boards loaded yet'}</div>`;
    if (meta) meta.textContent = '';
    return;
  }
  const iClashes = [];
  const rClashes = [];
  boards.forEach(b => {
    (b.conflicts.instructor_clashes || []).forEach(o => iClashes.push({ ...o, _boardLabel: b.boardLabel, _paneIdx: b.paneIdx }));
    (b.conflicts.room_clashes || []).forEach(o => rClashes.push({ ...o, _boardLabel: b.boardLabel, _paneIdx: b.paneIdx }));
  });
  if (!iClashes.length && !rClashes.length) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">✓</span>${IS_AR ? 'لا توجد تعارضات موارد' : 'No resource conflicts'}</div>`;
    if (meta) meta.textContent = '';
    return;
  }
  let html = '';
  if (iClashes.length) {
    html += `<div class="section-head danger" style="margin-top:4px">${IS_AR ? 'تعارض مدرس' : 'Instructor clashes'}<span class="n">${iClashes.length}</span></div>`;
    html += `<table><thead><tr>
      <th>${IS_AR ? 'المدرس' : 'Instructor'}</th>
      <th>${IS_AR ? 'الشعب' : 'Sections'}</th>
      <th>${IS_AR ? 'اللوحة' : 'Board'}</th>
      <th>${IS_AR ? 'التفاصيل' : 'Detail'}</th>
    </tr></thead><tbody>`;
    iClashes.forEach(c => {
      html += `<tr data-ids='${JSON.stringify(c.ids || [])}'>
        <td class="code">${esc(c.instructor || '—')}</td>
        <td>${esc((c.sections || []).join(', '))}</td>
        <td>${esc(c._boardLabel)} <span class="pane-badge">P${c._paneIdx + 1}</span></td>
        <td>${esc(c.detail || '')}</td>
      </tr>`;
    });
    html += '</tbody></table>';
  }
  if (rClashes.length) {
    html += `<div class="section-head warn" style="margin-top:10px">${IS_AR ? 'تعارض قاعة' : 'Room clashes'}<span class="n">${rClashes.length}</span></div>`;
    html += `<table><thead><tr>
      <th>${IS_AR ? 'القاعة' : 'Room'}</th>
      <th>${IS_AR ? 'الشعب' : 'Sections'}</th>
      <th>${IS_AR ? 'اللوحة' : 'Board'}</th>
      <th>${IS_AR ? 'التفاصيل' : 'Detail'}</th>
    </tr></thead><tbody>`;
    rClashes.forEach(c => {
      html += `<tr data-ids='${JSON.stringify(c.ids || [])}'>
        <td class="code">${esc(c.room || '—')}</td>
        <td>${esc((c.sections || []).join(', '))}</td>
        <td>${esc(c._boardLabel)} <span class="pane-badge">P${c._paneIdx + 1}</span></td>
        <td>${esc(c.detail || '')}</td>
      </tr>`;
    });
    html += '</tbody></table>';
  }
  body.innerHTML = html;
  if (meta) meta.textContent = `${iClashes.length + rClashes.length} ${IS_AR ? 'تعارض' : 'clashes'}`;
  // Wire row clicks to highlight placements
  body.querySelectorAll('tr[data-ids]').forEach(row => {
    row.addEventListener('click', () => {
      try { highlightPlacements(JSON.parse(row.dataset.ids)); } catch {}
    });
    row.style.cursor = 'pointer';
  });
}

/* ── Section drawer ── */
const DRAWER = {
  placementId: null,
  paneIdx: null,
};

function openDrawer(paneIdx, placementId) {
  const located = findPlacement(placementId);
  if (!located) return;
  const p = located.placement;
  DRAWER.placementId = placementId;
  DRAWER.paneIdx = paneIdx;

  // Accent strip takes the pane's term colour (t0..t3) so the drawer
  // visually belongs to the same pane the placement sits in.
  const accent = $('twsDrawerAccent');
  const termVar = `--tws-c-t${paneIdx}`;
  accent.style.background = getComputedStyle(document.documentElement).getPropertyValue(termVar).trim() || 'var(--teal)';

  $('twsDrawerTitle').textContent = `${p.course_code} ${p.section || ''}`.trim();
  const board = S.boards.find(b => b.id === S.panes[paneIdx].boardId);
  $('twsDrawerSub').textContent = `${board ? board.label : '—'} · ${p.day} · ${p.start_time}–${p.end_time}`;

  const conflicts = S.panes[paneIdx].boardData?.conflicts || {};
  const clashIds = new Set();
  (conflicts.overlaps || []).forEach(o => (o.ids || []).forEach(id => clashIds.add(id)));
  (conflicts.instructor_clashes || []).forEach(o => (o.ids || []).forEach(id => clashIds.add(id)));
  (conflicts.room_clashes || []).forEach(o => (o.ids || []).forEach(id => clashIds.add(id)));

  const statusPill = p.is_locked
    ? '<span class="pill locked">🔒 Locked</span>'
    : clashIds.has(p.id)
      ? '<span class="pill clash">⚠ Conflict</span>'
      : '<span class="pill ok">● Clean</span>';

  const instructor = (p.meetings && p.meetings[0]) ? p.meetings[0].instructor : null;
  const meetings = (p.meetings || []).map(m =>
    `<div class="meeting-row"><span class="d">${esc(m.day)}</span><span class="t">${esc(m.start_time)}–${esc(m.end_time)}</span><span class="r">${esc(m.room || '—')} · ${esc(m.instructor || '—')}</span></div>`
  ).join('') || '<div class="meeting-row"><span class="r">—</span></div>';

  $('twsDrawerBody').innerHTML = `
    <div class="field"><span class="label">${IS_AR ? 'المقرر' : 'Course'}</span><span class="value mono">${esc(p.course_code)}</span></div>
    <div class="field"><span class="label">${IS_AR ? 'الاسم' : 'Name'}</span><span class="value">${esc(p.course_name || '—')}</span></div>
    <div class="field"><span class="label">${IS_AR ? 'الشعبة' : 'Section'}</span><span class="value mono">${esc(p.section || '—')}</span></div>
    ${planLensDetailsFieldsHtml(p, 'field')}
    <div class="field"><span class="label">${IS_AR ? 'اليوم' : 'Day'}</span><span class="value mono">${esc(p.day)}</span></div>
    <div class="field"><span class="label">${IS_AR ? 'الوقت' : 'Time'}</span><span class="value mono">${esc(p.start_time)}–${esc(p.end_time)}</span></div>
    <div class="field"><span class="label">${IS_AR ? 'القاعة' : 'Room'}</span><span class="value mono">${esc(p.room || '—')}</span></div>
    <div class="field"><span class="label">${IS_AR ? 'المدرس' : 'Instructor'}</span><span class="value">${esc(instructor || '—')}</span></div>
    <div class="field"><span class="label">${IS_AR ? 'السعة' : 'Capacity'}</span><span class="value mono">${p.available_capacity || '—'}</span></div>
    <div class="field"><span class="label">${IS_AR ? 'المسجلون' : 'Registered'}</span><span class="value mono">${p.registered_count || '—'}</span></div>
    <div class="field"><span class="label">${IS_AR ? 'الحالة' : 'Status'}</span><span class="value">${statusPill}</span></div>
    <div class="section-title">${IS_AR ? 'جميع اللقاءات' : 'All meetings'}</div>
    ${meetings}
  `;

  // Lock-button label mirrors current state (only update the label span)
  const lockBtn = $('twsDrawerLock');
  const lockLbl = lockBtn.querySelector('.lbl');
  if (lockLbl) {
    lockLbl.textContent = p.is_locked
      ? (IS_AR ? 'فتح القفل' : 'Unlock')
      : (IS_AR ? 'قفل' : 'Lock');
  }

  $('twsDrawer').classList.add('open');
  $('twsDrawer').setAttribute('aria-hidden', 'false');
  $('twsDrawer').removeAttribute('inert');
  $('twsDrawerBackdrop').classList.add('open');
}

function closeDrawer() {
  DRAWER.placementId = null;
  DRAWER.paneIdx = null;
  $('twsDrawer').classList.remove('open');
  $('twsDrawer').setAttribute('aria-hidden', 'true');
  $('twsDrawer').setAttribute('inert', '');
  $('twsDrawerBackdrop').classList.remove('open');
}

/* ── Lock / unlock ── */
async function doToggleLock(placementId, paneIdx) {
  const located = findPlacement(placementId);
  if (!located) { notify.error(IS_AR ? 'غير موجود' : 'Placement not found'); return; }
  const wasLocked = !!located.placement.is_locked;
  const data = await api(`/ops/tw/placements/${placementId}/lock/`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  });
  if (!data) return;
  notify.success(wasLocked
    ? (IS_AR ? 'تم فتح القفل' : 'Unlocked')
    : (IS_AR ? 'تم القفل' : 'Locked'));
  // Reload every pane — placement could be visible in more than one
  for (let i = 0; i < paneCount(); i++) await loadAndRenderPane(i);
  await refreshBoardsSummary();
  // If drawer open on this placement, reopen to refresh the body
  if (DRAWER.placementId === placementId) openDrawer(paneIdx, placementId);
}

/* ── Remove + undo ── */
async function doRemove(placementId, paneIdx) {
  const located = findPlacement(placementId);
  if (!located) { notify.error(IS_AR ? 'غير موجود' : 'Placement not found'); return; }
  const p = located.placement;
  const confirmed = window.confirm(IS_AR
    ? `إزالة ${p.course_code} ${p.section || ''}؟`
    : `Remove ${p.course_code} ${p.section || ''}?`);
  if (!confirmed) return;

  // Snapshot before delete for undo (need term_section_id + original position).
  const snapshot = {
    placement_id: placementId,
    board_id: S.panes[paneIdx].boardId,
    term_section_id: p.term_section_id,
    day: p.day, start_time: p.start_time, end_time: p.end_time,
    room: p.room || '',
  };

  const data = await api(`/ops/tw/placements/${placementId}/remove/`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: p.is_locked ? JSON.stringify({ override: true }) : '{}',
  });
  if (!data) return;
  notify.success(IS_AR ? 'تم الحذف' : 'Removed');
  S.undoStack.push({ type: 'remove', ...snapshot });
  S.redoStack = [];
  updateUndoRedoButtons();
  closeDrawer();
  for (let i = 0; i < paneCount(); i++) await loadAndRenderPane(i);
  await refreshBoardsSummary();
}

/* ── Undo / Redo (global, matching main-page semantics) ── */
function updateUndoRedoButtons() {
  const u = $('twsUndo'), r = $('twsRedo');
  if (u) u.disabled = S.undoStack.length === 0;
  if (r) r.disabled = S.redoStack.length === 0;
}

async function doMove(placementId, day, start, end, room) {
  const payload = { day, start_time: start, end_time: end };
  if (room !== undefined) payload.room = room;
  const data = await api(`/ops/tw/placements/${placementId}/move/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return data;
}

async function applyMoveList(moves, direction) {
  const ordered = direction === 'undo' ? [...moves].reverse() : [...moves];
  const applied = [];
  for (const move of ordered) {
    const data = direction === 'undo'
      ? await doMove(move.placement_id, move.old_day, move.old_start, move.old_end, move.old_room)
      : await doMove(move.placement_id, move.new_day, move.new_start, move.new_end, move.new_room);
    if (!data) {
      const rollbackDirection = direction === 'undo' ? 'redo' : 'undo';
      for (const done of applied.reverse()) {
        if (rollbackDirection === 'undo') {
          await doMove(done.placement_id, done.old_day, done.old_start, done.old_end, done.old_room);
        } else {
          await doMove(done.placement_id, done.new_day, done.new_start, done.new_end, done.new_room);
        }
      }
      return false;
    }
    applied.push(move);
  }
  return true;
}

async function doCreate(action) {
  return api('/ops/tw/placements/create/', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      board_id: action.board_id,
      term_section_id: action.term_section_id,
      day: action.day,
      start_time: action.start_time,
      end_time: action.end_time,
      room: action.room || '',
    }),
  });
}
async function doCreatePlanned(action) {
  const payload = action.payload || {};
  return api('/ops/tw/placements/create-planned/', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      board_id: action.board_id,
      course_code: payload.course_code,
      course_key: payload.course_key || payload.course_code,
      course_name: payload.course_name || payload.course_code,
      section_label: payload.section_label,
      capacity: payload.max_per_section || 40,
      meetings: action.meetings || [],
    }),
  });
}
async function doRemoveApi(placementId) {
  return api(`/ops/tw/placements/${placementId}/remove/`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ override: true }),
  });
}

async function removePlacementList(placementIds = []) {
  const removed = [];
  for (const placementId of [...placementIds].reverse()) {
    const data = await doRemoveApi(placementId);
    if (!data) return { ok: false, removed };
    removed.push(placementId);
  }
  return { ok: true, removed };
}

async function doUndo() {
  const action = S.undoStack.pop();
  if (!action) return;
  if (action.type === 'move') {
    const data = await doMove(action.placement_id, action.old_day, action.old_start, action.old_end, action.old_room);
    if (!data) { S.undoStack.push(action); return; }
    S.redoStack.push(action);
    notify.success(IS_AR ? 'تم التراجع' : 'Undone');
  } else if (action.type === 'bundle_move') {
    const ok = await applyMoveList(action.moves || [], 'undo');
    if (!ok) { S.undoStack.push(action); return; }
    S.redoStack.push(action);
    notify.success(IS_AR ? 'تم التراجع عن الحزمة' : 'Bundle undone');
  } else if (action.type === 'remove') {
    // Undo remove = re-create the placement at its original position.
    const data = await doCreate(action);
    if (!data) { S.undoStack.push(action); return; }
    const newId = data.placement && data.placement.id;
    S.redoStack.push({ ...action, restored_placement_id: newId });
    notify.success(IS_AR ? 'تم استرجاع الشعبة' : 'Placement restored');
  } else if (action.type === 'create') {
    // Undo create = delete the placement we just created.
    const data = await doRemoveApi(action.placement_id);
    if (!data) { S.undoStack.push(action); return; }
    S.redoStack.push(action);
    notify.success(IS_AR ? 'تم التراجع' : 'Undone');
  } else if (action.type === 'bundle_create') {
    const result = await removePlacementList(action.placement_ids || []);
    if (!result.ok) { S.undoStack.push(action); return; }
    S.redoStack.push(action);
    notify.success('Full pattern undone');
  }
  updateUndoRedoButtons();
  for (let i = 0; i < paneCount(); i++) await loadAndRenderPane(i);
  await refreshBoardsSummary();
  await loadSidebarBudget();
}

async function doRedo() {
  const action = S.redoStack.pop();
  if (!action) return;
  if (action.type === 'move') {
    const data = await doMove(action.placement_id, action.new_day, action.new_start, action.new_end, action.new_room);
    if (!data) { S.redoStack.push(action); return; }
    S.undoStack.push(action);
    notify.success(IS_AR ? 'تم الإعادة' : 'Redone');
  } else if (action.type === 'bundle_move') {
    const ok = await applyMoveList(action.moves || [], 'redo');
    if (!ok) { S.redoStack.push(action); return; }
    S.undoStack.push(action);
    notify.success(IS_AR ? 'تمت إعادة الحزمة' : 'Bundle redone');
  } else if (action.type === 'remove') {
    // Redo remove = delete again (using the newly-restored placement id).
    const targetId = action.restored_placement_id || action.placement_id;
    const data = await doRemoveApi(targetId);
    if (!data) { S.redoStack.push(action); return; }
    S.undoStack.push({ ...action, placement_id: targetId });
    notify.success(IS_AR ? 'تمت الإزالة مرة أخرى' : 'Removed again');
  } else if (action.type === 'create') {
    // Redo create = re-create at the original position.
    const data = await doCreate(action);
    if (!data) { S.redoStack.push(action); return; }
    const newId = data.placement && data.placement.id;
    S.undoStack.push({ ...action, placement_id: newId });
    notify.success(IS_AR ? 'تم الإعادة' : 'Redone');
  } else if (action.type === 'bundle_create') {
    const data = await doCreatePlanned(action);
    if (!data) { S.redoStack.push(action); return; }
    const placements = Array.isArray(data.placements) ? data.placements : [];
    S.undoStack.push({ ...action, placement_ids: placements.map(item => item.id) });
    notify.success('Full pattern redone');
  }
  updateUndoRedoButtons();
  for (let i = 0; i < paneCount(); i++) await loadAndRenderPane(i);
  await refreshBoardsSummary();
  await loadSidebarBudget();
}

/* ── Top-bar actions ── */
async function doOptimise(mode = 'current') {
  if (!S.scenarioId) return;
  const isFull = mode === 'full';
  const confirmMsg = isFull
    ? (IS_AR ? 'إعادة بناء كاملة بـ 7 استراتيجيات؟ قد يستغرق دقيقتين.' : 'Full rebuild with 7 strategies? This can take ~2 minutes.')
    : T.optimiseConfirm;
  if (!confirm(confirmMsg)) return;
  const btn = $('twsOptimise');
  const menuBtn = $('twsOptimiseMenu');
  const origText = btn.textContent;
  btn.disabled = true; menuBtn && (menuBtn.disabled = true);
  btn.textContent = isFull
    ? (IS_AR ? 'جاري إعادة البناء…' : 'Rebuilding…')
    : (IS_AR ? 'جاري التحسين…' : 'Optimising…');

  const payload = { mode };
  if (isFull) {
    payload.strategies = ['compact', 'morning', 'balanced', 'load_balanced', 'optimal', 'hybrid', 'adaptive'];
  }
  payload.run_local_search = true;
  payload.max_iterations = 50;
  payload.run_chain_search = true;
  payload.run_cpsat_polish = true;
  payload.cpsat_time_limit = 60;

  const data = await api(`/ops/tw/scenarios/${S.scenarioId}/optimise-v2/`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  btn.disabled = false; menuBtn && (menuBtn.disabled = false);
  btn.textContent = origText;
  if (!data || !data.optimisation) { notify.error(IS_AR ? 'فشل التحسين' : 'Optimisation failed'); return; }
  showOptimiseResults(data.optimisation, mode);
  await onScenarioChange();
}

function showOptimiseResults(o, mode) {
  const isFull = mode === 'full';
  const score = o.final_score || [];
  const assigned = (o.total_students || 0) - (o.unresolved_students || 0);
  const crossBefore = Number(o.cross_board_before ?? NaN);
  const crossAfter = Number(o.cross_board_after ?? NaN);
  const hasCrossMetric = Number.isFinite(crossBefore) && Number.isFinite(crossAfter);
  const crossDelta = hasCrossMetric ? crossBefore - crossAfter : 0;
  const crossAffectedBefore = Number(o.cross_board_affected_students_before ?? NaN);
  const crossAffectedAfter = Number(o.cross_board_affected_students_after ?? NaN);
  const hasCrossAffectedMetric = Number.isFinite(crossAffectedBefore) && Number.isFinite(crossAffectedAfter);
  const crossAffectedDelta = hasCrossAffectedMetric ? crossAffectedBefore - crossAffectedAfter : 0;
  const crossPrimaryBefore = hasCrossAffectedMetric ? crossAffectedBefore : crossBefore;
  const crossPrimaryAfter = hasCrossAffectedMetric ? crossAffectedAfter : crossAfter;
  const crossPrimaryDelta = hasCrossAffectedMetric ? crossAffectedDelta : crossDelta;
  const persistAction = o.persist_result && o.persist_result.action;
  const safetyBlocked = Boolean(
    o.safety_blocked
    || persistAction === 'rolled_back_safety_regression'
    || persistAction === 'blocked_safety_regression'
  );
  const safetyRegressions = (o.safety_regression && Array.isArray(o.safety_regression.regressions))
    ? o.safety_regression.regressions
    : [];
  const candList = (o.all_scores && o.all_scores.length > 1) ? `
    <div class="section-title">${IS_AR ? 'مقارنة المرشحين' : 'Candidate comparison'} (${o.candidates_evaluated || o.all_scores.length})</div>
    <table style="width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:11px">
      <thead><tr style="color:var(--t4);border-bottom:1px solid var(--line)">
        <th style="text-align:left;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Strategy</th>
        <th style="text-align:right;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Tier-A</th>
        <th style="text-align:right;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Unres.</th>
        <th style="text-align:right;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Clash</th>
        <th style="text-align:right;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Gaps</th>
        <th style="text-align:right;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Quality</th>
      </tr></thead>
      <tbody>
        ${o.all_scores.map(s => {
          const isBest = s.id === o.best_candidate_id;
          const row = isBest ? 'background:rgba(10,142,110,0.08);font-weight:700' : '';
          const clashClr = s.score[3] > 0 ? '#F06060' : 'var(--teal)';
          return `<tr style="border-bottom:1px solid var(--line);${row}">
            <td style="padding:4px 6px">${esc(String(s.id).replace(/_\d+$/, ''))}${isBest ? ' ★' : ''}</td>
            <td style="text-align:right;padding:4px 6px">${s.score[0]}</td>
            <td style="text-align:right;padding:4px 6px">${s.score[1]}</td>
            <td style="text-align:right;padding:4px 6px;color:${clashClr}">${s.score[3]}</td>
            <td style="text-align:right;padding:4px 6px">${(s.score[4] || 0).toLocaleString()}</td>
            <td style="text-align:right;padding:4px 6px">${Number(s.quality_penalty || 0).toLocaleString()}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
  ` : '';
  const finalLabel = mode === 'current'
    ? (IS_AR ? 'النتيجة بعد التحسين' : 'Score after optimisation')
    : `${IS_AR ? 'النتيجة النهائية' : 'Final score'} (${String(o.best_candidate_id || '').replace(/_\d+$/, '')}${o.local_search_applied ? ' + local search' : ''})`;
  const scoreLabels = [
    IS_AR ? 'طلاب الخطر A' : 'Tier-A unresolved',
    IS_AR ? 'طلاب غير محلولين' : 'Unresolved students',
    IS_AR ? 'مقررات غير معينة' : 'Unassigned courses',
    IS_AR ? 'تعارضات زمنية' : 'Time clashes',
    IS_AR ? 'دقائق فراغ' : 'Gap minutes',
    IS_AR ? 'احتياط مستخدم' : 'Reserve used',
  ];
  const scoreRows = score.slice(0, 6).map((v, i) => {
    const display = i === 4 ? Number(v).toLocaleString() : v;
    const clr = (i === 3 && v > 0) ? '#F06060' : (i === 3 ? 'var(--teal)' : 'var(--ink)');
    return `<tr style="border-bottom:1px solid var(--line)"><td style="padding:4px 0;color:var(--t3)">${scoreLabels[i]}</td><td style="text-align:right;font-family:'JetBrains Mono',monospace;color:${clr};font-weight:700">${display}</td></tr>`;
  }).join('');
  const quality = o.quality_score || {};
  const qualityComponents = quality.components || {};
  const qualityRows = Object.entries(qualityComponents)
    .filter(([, value]) => Number(value || 0) > 0)
    .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
    .map(([name, value]) => `<span class="tws-repair-chip">${esc(String(name).replace(/_/g, ' '))}: ${Number(value || 0).toLocaleString()}</span>`)
    .join('');
  const qualityBlock = quality.penalty != null ? `
    <div style="margin-bottom:10px">
      <span class="section-title">${IS_AR ? 'Ø¬ÙˆØ¯Ø© Ø§Ù„Ø¬Ø¯ÙˆÙ„' : 'Timetable quality'}</span>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">
        <span class="tws-repair-chip good">${IS_AR ? 'Ø§Ù„Ø¹Ù‚ÙˆØ¨Ø©' : 'Penalty'}: ${Number(quality.penalty || 0).toLocaleString()}</span>
        ${qualityRows || `<span class="tws-repair-chip">${IS_AR ? 'Ù„Ø§ Ø¶ØºØ· Ù†Ø§Ø¹Ù…' : 'No soft pressure'}</span>`}
      </div>
    </div>` : '';
  const safetyBlock = safetyBlocked ? `
    <div style="padding:10px 12px;border-radius:6px;background:rgba(240,96,96,0.1);border:1px solid rgba(240,96,96,0.3);margin:10px 0">
      <div style="font-weight:800;color:#F06060;margin-bottom:5px">Result not applied</div>
      <div style="font-size:11px;color:var(--t3)">The optimiser candidate was rejected because it worsened unresolved students, solver clashes, or hard operational constraints.</div>
      ${safetyRegressions.length ? `<div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:7px">${safetyRegressions.map(r =>
        `<span class="tws-repair-chip" style="border-color:rgba(240,96,96,0.35);color:#F06060">${esc(r.label || r.metric)}: ${Number(r.before || 0).toLocaleString()} -> ${Number(r.after || 0).toLocaleString()}</span>`
      ).join('')}</div>` : ''}
    </div>` : '';
  const crossTradeoffBlock = (!safetyBlocked && hasCrossMetric && crossPrimaryDelta < 0) ? `
    <div style="padding:8px 12px;border-radius:6px;background:rgba(245,183,49,0.08);border:1px solid rgba(245,183,49,.25);margin-top:10px;color:#F5B731;font-weight:600">
      Tradeoff: ${hasCrossAffectedMetric ? 'affected cross-board students' : 'cross-board overlaps'} increased from ${crossPrimaryBefore} to ${crossPrimaryAfter}.
      <div style="margin-top:4px;font-size:11px;color:var(--t3);font-weight:500">Student unresolved outcome remains the primary optimisation target.</div>
    </div>` : '';
  let diffBlock = '';
  if (mode === 'current' && Array.isArray(o.baseline_score) && Array.isArray(o.final_score)
      && o.baseline_score.length >= 5 && o.final_score.length >= 5) {
    const bs = o.baseline_score, fs = o.final_score;
    const improved = !safetyBlocked && (
      fs[1] < bs[1] || fs[3] < bs[3] || fs[4] < bs[4] || crossPrimaryDelta > 0
    );
    const safeToLocale = v => (typeof v === 'number' ? v.toLocaleString() : String(v ?? '—'));
    diffBlock = safetyBlocked
      ? `<div style="padding:8px 12px;border-radius:6px;background:rgba(240,96,96,0.08);border:1px solid rgba(240,96,96,.24);margin-top:10px;color:#F06060;font-weight:700">
          No timetable changes were applied.
        </div>`
      : improved
      ? `<div style="padding:8px 12px;border-radius:6px;background:rgba(10,142,110,0.1);border:1px solid rgba(10,142,110,0.25);margin-top:10px">
          <div style="font-weight:700;color:var(--teal);margin-bottom:4px">✓ ${IS_AR ? 'تم التحسين' : 'Board improved'}</div>
          <div style="font-size:11px">Unresolved: <b>${bs[1]}</b> → <b style="color:var(--teal)">${fs[1]}</b></div>
          ${bs[3] !== fs[3] ? `<div style="font-size:11px">Clashes: <b>${bs[3]}</b> → <b style="color:var(--teal)">${fs[3]}</b></div>` : ''}
          ${hasCrossMetric && crossPrimaryBefore !== crossPrimaryAfter ? `<div style="font-size:11px">${hasCrossAffectedMetric ? 'Affected students' : 'Cross-board'}: <b>${crossPrimaryBefore}</b> → <b style="color:${crossPrimaryDelta > 0 ? 'var(--teal)' : '#F06060'}">${crossPrimaryAfter}</b>${hasCrossAffectedMetric ? ` <span style="color:var(--t4)">(${crossAfter} section clashes)</span>` : ''}</div>` : ''}
          <div style="font-size:11px">Gaps: <b>${safeToLocale(bs[4])}</b> → <b>${safeToLocale(fs[4])}</b></div>
        </div>`
      : `<div style="padding:8px 12px;border-radius:6px;background:rgba(80,104,240,0.08);border:1px solid rgba(80,104,240,.22);margin-top:10px;color:#aeb9ff;font-weight:600">
          ━ ${IS_AR ? 'لم يتم العثور على تحسين تلقائي' : 'No automatic improvement was found'}
          ${hasCrossMetric && crossPrimaryAfter > 0 ? `<div style="margin-top:5px;font-size:11px;color:var(--t3);font-weight:500">${hasCrossAffectedMetric ? 'Affected cross-board students' : 'Cross-board overlaps'} remain <b style="color:#F06060">${crossPrimaryAfter}</b>. Use guided fixes or Full rebuild to target this metric.</div>` : ''}
        </div>`;
  }
  const crossCard = hasCrossMetric ? `
      <div style="padding:12px;border-radius:8px;background:rgba(240,96,96,0.08);text-align:center">
        <div style="font-size:9.5px;color:var(--t4);text-transform:uppercase;letter-spacing:0.08em">${IS_AR ? 'عبر اللوحات' : 'Cross'}</div>
        <div style="font-weight:700;color:${crossPrimaryAfter > 0 ? '#F06060' : 'var(--teal)'};font-size:20px">${crossPrimaryAfter}<span style="color:var(--t4);font-size:12px;font-weight:400">${crossPrimaryBefore !== crossPrimaryAfter ? `/${crossPrimaryBefore}` : ''}</span></div>
        ${hasCrossAffectedMetric ? `<div style="font-size:10px;color:var(--t4);margin-top:2px">${crossAfter} section clashes</div>` : ''}
      </div>` : '';
  const hotspots = (o.hotspot_courses || []).length ? `
    <div class="section-title" style="color:#F06060;margin-top:12px">${IS_AR ? 'مقررات مزدحمة' : 'Hotspot courses'}</div>
    <div style="display:flex;flex-wrap:wrap;gap:4px">${o.hotspot_courses.map(c =>
      `<span style="padding:3px 9px;border-radius:999px;background:rgba(240,96,96,0.12);color:#F06060;font-weight:700;font-family:'JetBrains Mono',monospace;font-size:11px">${esc(c)}</span>`
    ).join('')}</div>
  ` : '';
  const reservePressure = (o.reserve_heavy_sections || []).length ? `
    <div class="section-title" style="color:#F5B731;margin-top:12px">${IS_AR ? 'ضغط احتياطي' : 'Reserve pressure'}</div>
    <div style="display:flex;flex-wrap:wrap;gap:4px">${o.reserve_heavy_sections.slice(0, 8).map(s =>
      `<span style="padding:3px 9px;border-radius:999px;background:rgba(245,183,49,0.12);color:#F5B731;font-weight:700;font-family:'JetBrains Mono',monospace;font-size:11px">${esc(s.section)} ${Math.round(s.ratio * 100)}%</span>`
    ).join('')}</div>
  ` : '';
  const body = `
    <div style="display:grid;grid-template-columns:repeat(${hasCrossMetric ? 4 : 3},1fr);gap:8px;margin-bottom:14px">
      <div style="padding:12px;border-radius:8px;background:rgba(10,142,110,0.08);text-align:center">
        <div style="font-size:9.5px;color:var(--t4);text-transform:uppercase;letter-spacing:0.08em">${IS_AR ? 'تم تعيينهم' : 'Assigned'}</div>
        <div style="font-weight:700;color:var(--teal);font-size:20px">${assigned}<span style="color:var(--t4);font-size:12px;font-weight:400">/${o.total_students || 0}</span></div>
      </div>
      <div style="padding:12px;border-radius:8px;background:rgba(245,183,49,0.08);text-align:center">
        <div style="font-size:9.5px;color:var(--t4);text-transform:uppercase;letter-spacing:0.08em">${IS_AR ? 'غير محلول' : 'Unresolved'}</div>
        <div style="font-weight:700;color:#F5B731;font-size:20px">${o.unresolved_students || 0}</div>
      </div>
      <div style="padding:12px;border-radius:8px;background:rgba(80,104,240,0.08);text-align:center">
        <div style="font-size:9.5px;color:var(--t4);text-transform:uppercase;letter-spacing:0.08em">${IS_AR ? 'الوقت' : 'Time'}</div>
        <div style="font-weight:700;color:#5068F0;font-size:20px">${o.elapsed_seconds || '?'}s</div>
      </div>
      ${crossCard}
    </div>
    ${candList}
    <div class="section-title" style="color:var(--teal);margin-top:12px">${finalLabel}</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">${scoreRows}</table>
    ${qualityBlock}
    ${safetyBlock}
    ${diffBlock}
    ${crossTradeoffBlock}
    ${hotspots}
    ${reservePressure}
  `;
  openModal({
    title: isFull ? (IS_AR ? 'نتائج إعادة البناء' : 'Full rebuild results') : (IS_AR ? 'نتائج التحسين' : 'Optimise current · results'),
    sub: S.scenarioMeta ? S.scenarioMeta.name : '',
    body, width: 'xwide',
    buttons: [
      { label: IS_AR ? 'تطبيق' : 'Apply & refresh', variant: 'primary' },
    ],
  });
  if (safetyBlocked) {
    const footerBtn = $('twsModalFooter')?.querySelector('button');
    if (footerBtn) {
      footerBtn.textContent = 'Close';
      footerBtn.classList.remove('primary');
    }
  }
}
async function doPublish() {
  if (!S.scenarioId) return;
  if (!confirm(T.publishConfirm)) return;
  const data = await api(`/ops/tw/scenarios/${S.scenarioId}/publish/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  if (!data) return;
  await onScenarioChange();
}
function doExport() {
  if (!S.scenarioId) return;
  const params = new URLSearchParams();
  const parts = [];
  const plan = activePlanFilter();
  const search = activeSplitSearch();
  if (plan && plan !== 'ALL') parts.push(plan);
  if (search) parts.push(search);
  const query = parts.join(' ').trim();
  if (query) params.set('search', query);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  window.open(`/ops/tw/scenarios/${S.scenarioId}/export-per-plan${suffix}`, '_blank');
}

const DISMISSIBLE_DETAILS = ['twsViewMenu', 'twsMoreMenu'];

function closeOpenDetailsMenus(except = null) {
  document.querySelectorAll('.tws-presets[open], .tws-shortcuts[open]')
    .forEach(menu => {
      if (menu !== except) menu.removeAttribute('open');
    });
  DISMISSIBLE_DETAILS
    .map(id => $(id))
    .filter(Boolean)
    .forEach(menu => {
      if (menu !== except) menu.removeAttribute('open');
    });
}

function hasOpenDetailsMenu() {
  return DISMISSIBLE_DETAILS.some(id => $(id)?.hasAttribute('open'))
    || !!document.querySelector('.tws-presets[open], .tws-shortcuts[open]');
}

function initDismissibleDetailsMenus() {
  const menus = [
    ...DISMISSIBLE_DETAILS.map(id => $(id)).filter(Boolean),
    ...document.querySelectorAll('.tws-presets, .tws-shortcuts'),
  ];
  menus.forEach(menu => {
    const summary = menu.querySelector(':scope > summary');
    summary?.addEventListener('click', (e) => {
      e.preventDefault();
      const shouldOpen = !menu.open;
      closeOpenDetailsMenus(shouldOpen ? menu : null);
      menu.open = shouldOpen;
    });
    summary?.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      summary.click();
    });
    menu.addEventListener('toggle', () => {
      if (menu.open) closeOpenDetailsMenus(menu);
    });
  });
  document.addEventListener('click', (e) => {
    const openMenus = menus.filter(menu => menu.open);
    if (!openMenus.length) return;
    if (openMenus.some(menu => menu.contains(e.target))) return;
    closeOpenDetailsMenus();
  });
}

/* ── Init ── */
(function init() {
  $('twsScenario').addEventListener('change', onScenarioChange);
  initDismissibleDetailsMenus();
  initPaneHeaderActionDelegation();
  $('twsOptimise').addEventListener('click', () => doOptimise('current'));
  $('twsGlobalRepairPlan')?.addEventListener('click', () => openGlobalRepairPlanModal());
  $('twsOptimiseMenu')?.addEventListener('click', (e) => {
    e.stopPropagation();
    const dd = document.getElementById('twsOptimiseDropdown');
    if (dd) dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
  });
  document.querySelectorAll('.tws-opt-item').forEach(item => {
    item.addEventListener('mouseenter', () => { item.style.background = 'rgba(10,142,110,0.08)'; });
    item.addEventListener('mouseleave', () => { item.style.background = ''; });
    item.addEventListener('click', () => {
      document.getElementById('twsOptimiseDropdown').style.display = 'none';
      doOptimise(item.dataset.mode);
    });
  });
  document.addEventListener('click', (e) => {
    const group = document.getElementById('twsOptimiseDropdown');
    if (group && !e.target.closest('#twsOptimiseMenu, #twsOptimiseDropdown')) {
      group.style.display = 'none';
    }
  });
  $('twsPublish').addEventListener('click', doPublish);
  $('twsExport').addEventListener('click', doExport);
  document.querySelectorAll('.tws-more-menu button').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.id !== 'twsOptimiseMenu') $('twsMoreMenu')?.removeAttribute('open');
    });
  });
  $('twsUndo')?.addEventListener('click', doUndo);
  $('twsRedo')?.addEventListener('click', doRedo);
  updateUndoRedoButtons();

  // Drawer events
  $('twsDrawerClose').addEventListener('click', closeDrawer);
  $('twsDrawerBackdrop').addEventListener('click', closeDrawer);
  $('twsDrawerLock').addEventListener('click', () => {
    if (DRAWER.placementId != null) doToggleLock(DRAWER.placementId, DRAWER.paneIdx);
  });
  $('twsDrawerRemove').addEventListener('click', () => {
    if (DRAWER.placementId != null) doRemove(DRAWER.placementId, DRAWER.paneIdx);
  });

  // Generate / New scenario / New board
  $('twsGenerate')?.addEventListener('click', openGenerateModal);
  $('twsNewScenario')?.addEventListener('click', openNewScenarioModal);
  $('twsNewBoard')?.addEventListener('click', openNewBoardModal);
  $('twsSlots')?.addEventListener('click', openSlotEditorModal);
  $('twsElectives')?.addEventListener('click', openElectivesModal);

  // Sidebar toggle + search
  $('twsSidebarToggle')?.addEventListener('click', () => toggleSidebar());
  $('twsSearch')?.addEventListener('input', (e) => {
    S.search = e.target.value || '';
    for (let i = 0; i < paneCount(); i++) renderPane(i);
    if (SB.open) renderSidebar();
    refreshSplitSearch();
  });
  $('twsSectionSearch')?.addEventListener('input', (e) => {
    SB.search = e.target.value;
    renderSidebar();
  });

  // Generic modal close
  $('twsModalClose')?.addEventListener('click', (e) => {
    e.preventDefault();
    closeModal();
  });
  $('twsModalBackdrop')?.addEventListener('click', (e) => {
    e.preventDefault();
    closeModal();
  });

  // Right panel toggle + tabs
  $('twsRpanelToggle')?.addEventListener('click', () => toggleRpanel());
  $('twsRpanelClose')?.addEventListener('click', () => closeRpanel({ focusTrigger: true }));
  document.querySelectorAll('.tws-rpanel-tab').forEach(t => {
    t.addEventListener('click', () => setRpanelTab(t.dataset.tab));
    t.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setRpanelTab(t.dataset.tab); }
    });
  });

  // Bottom panel — handle toggles open/collapsed, tabs switch
  $('twsBpanelHandle')?.addEventListener('click', (e) => {
    // Clicking a tab shouldn't toggle the collapsed state, only switch tab
    if (e.target.closest('.tws-bpanel-tab')) return;
    toggleBpanel();
  });
  document.querySelectorAll('.tws-bpanel-tab').forEach(t => {
    t.addEventListener('click', (e) => {
      e.stopPropagation();
      setBpanelTab(t.dataset.tab);
    });
  });
  $('twsClose').addEventListener('click', confirmCloseSplitWorkspace);
  // Matrix layout picker — trigger toggles dropdown, matrix shows
  // hover-preview, click commits.
  initLayoutMatrix();
  // Sync-scroll / hover / slot — pure UI toggles; functional wiring
  // for sync-scroll / sync-slot is a follow-up. Click just flips .on.
  document.querySelectorAll('#twsSyncToggle .tg').forEach(tg => {
    tg.addEventListener('click', () => tg.classList.toggle('on'));
  });
  // Show labs / Lecture-only — hides lab-block in each pane when
  // "Lecture only" is selected.
  document.querySelectorAll('#twsLabToggle .tg').forEach(tg => {
    tg.addEventListener('click', () => {
      document.querySelectorAll('#twsLabToggle .tg').forEach(t => t.classList.toggle('on', t === tg));
      const hideLabs = tg.dataset.lab === 'hide';
      document.querySelectorAll('.tws-pane .lab-block').forEach(lb => {
        lb.style.display = hideLabs ? 'none' : '';
      });
    });
  });
  document.querySelectorAll('.tws-preset').forEach(b => {
    b.addEventListener('click', () => applyPreset(b.dataset.preset));
  });
  document.addEventListener('keydown', (e) => {
    // Skip when typing in an input/select/textarea
    const tag = (e.target && e.target.tagName) || '';
    const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target?.isContentEditable;

    if (e.key === 'Escape') {
      if (hasOpenDetailsMenu()) { closeOpenDetailsMenus(); return; }
      if (_modalOpen) { closeModal(); return; }
      if ($('twsDrawer').classList.contains('open')) { closeDrawer(); return; }
      if (clearPlacementSelection()) return;
      if (RP.open) { closeRpanel(); return; }
      confirmCloseSplitWorkspace();
      return;
    }
    // Undo / redo — Ctrl+Z, Ctrl+Shift+Z / Ctrl+Y
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      if (e.shiftKey) doRedo(); else doUndo();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
      e.preventDefault();
      doRedo();
      return;
    }

    if (typing) return;

    // Placement-level shortcuts — target is the drawer's placement if open,
    // else the currently selected cell's placement.
    const pid = DRAWER.placementId != null ? DRAWER.placementId : S.selectedPlacementId;
    const pidx = DRAWER.placementId != null ? DRAWER.paneIdx : S.selectedPaneIdx;
    if ((e.key === 'Delete' || e.key === 'Backspace') && pid != null) {
      e.preventDefault();
      doRemove(pid, pidx);
      return;
    }
    if (e.key === 'l' && pid != null) {
      // Lowercase L toggles lock on the targeted placement
      e.preventDefault();
      doToggleLock(pid, pidx);
      return;
    }

    if (e.key >= '1' && e.key <= '4') {
      const idx = parseInt(e.key) - 1;
      paneEl(idx)?.scrollIntoView({ block: 'center' });
    } else if (e.key === 'L') {
      // Uppercase L (Shift+L) cycles common layouts. Keeps lowercase
      // 'l' free for per-cell lock. Respects viewport caps.
      const cycle = [[1,1],[2,1],[2,2],[3,2],[3,3]];
      const cur = cycle.findIndex(([c,r]) => c === S.cols && r === S.rows);
      const maxC = viewportMaxCols(), maxR = viewportMaxRows();
      for (let step = 1; step <= cycle.length; step++) {
        const [c,r] = cycle[(cur + step) % cycle.length];
        if (c <= maxC && r <= maxR) { applyLayout(c, r); break; }
      }
    } else if (e.key === 'i' || e.key === 'I') {
      // Toggle right inspector panel
      toggleRpanel();
    } else if (e.key === 'd' || e.key === 'D') {
      // Toggle bottom demand panel
      toggleBpanel();
    } else if (e.key === 's' || e.key === 'S') {
      // Toggle left sections sidebar
      toggleSidebar();
    }
  });
  // Seed initial 2×2 layout so the hidden-attr is applied consistently on
  // panes 4..15 (HTML defaults only hide 6..15, and --tws-cols/--tws-rows
  // defaults need the class on .tws-quad to match).
  applyLayout(S.cols, S.rows);
  for (let i = 0; i < paneCount(); i++) renderPaneEmpty(i);
  loadScenarios();
})();

// Fully self-contained — no globals leak onto window.
