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
  dragSource: null,
  slotAssist: {
    active: false,
    paneIdx: null,
    placementId: null,
    kind: null,
    candidates: new Map(),
    requestToken: 0,
    pendingMove: null,
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
const TWS_CHROME_Y = 150;

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

function planLensBadgesHtml(placement) {
  const lens = sectionLensForPlacement(placement);
  if (!lens) return '';
  const label = planLensLabelForPlacement(placement);
  if (!label) return '';
  const counts = lens.actual_plans || {};
  const actual = Object.entries(counts)
    .filter(([, count]) => Number(count) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 2)
    .map(([plan, count]) => `${plan} ${count}`)
    .join(' ');
  const cls = lens.shared ? ' shared' : '';
  const title = actual
    ? `${label} ${lens.role || ''} | actual ${actual}`
    : `${label} ${lens.role || ''}`;
  return `<span class="plan-badges${cls}" title="${esc(title)}"><span>${esc(label)}</span>${actual ? `<em>${esc(actual)}</em>` : ''}</span>`;
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
  wrap.innerHTML = `<span class="plbl">Plan lens</span>${buttons}`;
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
  return `${kind}:${day}:${start}`;
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
    (candidate.pair && !candidate.pair.critical ? -35 : 0) +
    (String(candidate.day) === String(placement.day) ? 0 : 12) +
    Math.round(Math.abs(toMinutes(candidate.start) - toMinutes(placement.start_time)) / 10);
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
  if (!candidate || candidate.current_impact_score == null) return '';
  const beforeStudents = Number(candidate.current_student_affected_count || 0);
  const afterStudents = Number(candidate.student_affected_count || candidate.studentAffected || 0);
  const studentDelta = Number(candidate.student_improvement || (beforeStudents - afterStudents));
  const beforeCritical = Number(candidate.current_critical_count || 0);
  const afterCritical = Number(candidate.critical_count || candidate.critical || 0);
  const criticalDelta = Number(candidate.critical_improvement || (beforeCritical - afterCritical));
  const beforeWarning = Number(candidate.current_warning_count || 0);
  const afterWarning = Number(candidate.warning_count || candidate.warning || 0);
  const warningDelta = Number(candidate.warning_improvement || (beforeWarning - afterWarning));
  const bits = [`students ${beforeStudents}->${afterStudents}`];
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
  const tags = candidateSignalTags(candidate);
  if (tags.length) return tags.slice(0, 2).join(' | ');
  if (candidate?.pair) {
    return `${candidate.pair.companion.section || 'Pair'} ${candidate.pair.relation} ${candidate.pair.start}`;
  }
  if (tone === 'avoid') return `${candidate.critical || candidate.critical_count || 1} issue`;
  if (tone === 'risky') return `${candidate.warning || candidate.warning_count || 1} warn`;
  return '0 students';
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
  const tone = candidate?.tone;
  if (tone === 'avoid' || tone === 'critical') return 'avoid';
  if (tone === 'risky' || tone === 'watch') return 'risky';
  if (tone === 'clean' || tone === 'stable') return 'clean';
  if (candidate?.critical) return 'avoid';
  if (candidate?.warning) return 'risky';
  return 'clean';
}

function clearSlotAssistDecorations() {
  document.querySelectorAll('.tws-pane .cell.assist-clean, .tws-pane .cell.assist-risky, .tws-pane .cell.assist-avoid, .tws-pane .cell.assist-best, .tws-pane .cell.assist-preview, .tws-pane .cell.assist-room, .tws-pane .cell.assist-student, .tws-pane .cell.assist-instructor').forEach(cell => {
    cell.classList.remove('assist-clean', 'assist-risky', 'assist-avoid', 'assist-best', 'assist-preview', 'assist-room', 'assist-student', 'assist-instructor');
    delete cell.dataset.slotAssistKey;
    delete cell.dataset.slotAssistTitle;
    cell.querySelectorAll('.slot-assist-badge, .slot-assist-detail').forEach(el => el.remove());
  });
  document.querySelectorAll('.tws-pane.assist-active').forEach(pane => pane.classList.remove('assist-active'));
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
  const slotKind = kind || candidate.kind || S.slotAssist.kind || 'lect';
  const grid = slotKind === 'lab' ? '.lab-grid' : '.lect-grid';
  return pane.querySelector(`${grid} .cell[data-day="${candidate.day}"][data-start="${candidate.start}"]`);
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
  const selector = S.slotAssist.kind === 'lab' ? '.lab-grid .cell' : '.lect-grid .cell';
  pane?.classList.add('assist-active');
  pane?.querySelectorAll(selector).forEach(cell => {
    const key = slotAssistKey(S.slotAssist.kind, cell.dataset.day, cell.dataset.start);
    const candidate = S.slotAssist.candidates.get(key);
    if (!candidate) return;
    cell.dataset.slotAssistKey = key;
    cell.dataset.slotAssistTitle = splitSlotDetail(candidate);
    cell.title = `${candidate.day} ${candidate.start}-${candidate.end} | ${splitSlotLabel(candidate)} | ${splitSlotDetail(candidate)}`;
    const tone = slotAssistTone(candidate);
    if (tone === 'avoid') cell.classList.add('assist-avoid');
    else if (tone === 'risky') cell.classList.add('assist-risky');
    else cell.classList.add('assist-clean');
    if (candidateHasEvidence(candidate, ['cross_board_room', 'room'])) cell.classList.add('assist-room');
    if (Number(candidate.student_affected_count || candidate.studentAffected || 0) > 0 || candidateHasEvidence(candidate, ['students', 'cross_board_students'])) cell.classList.add('assist-student');
    if (candidateHasEvidence(candidate, ['instructor'])) cell.classList.add('assist-instructor');
    if (candidate.rank === 1) cell.classList.add('assist-best');
    if (candidate.rank <= 4) {
      const badge = document.createElement('span');
      badge.className = 'slot-assist-badge';
      badge.textContent = splitSlotLabel(candidate);
      const detail = document.createElement('span');
      detail.className = 'slot-assist-detail';
      detail.textContent = candidateSlotSignalLine(candidate, tone);
      cell.appendChild(badge);
      cell.appendChild(detail);
    }
  });
  const clean = candidates.filter(c => slotAssistTone(c) === 'clean').length;
  const affected = candidates.reduce((sum, c) => sum + Number(c.student_affected_count || c.studentAffected || 0), 0);
  const best = candidates[0];
  const bestText = best ? `${best.day} ${best.start}-${best.end}` : 'none';
  const pairText = best?.pair ? ` | pair ${best.pair.companion.section || ''} ${best.pair.relation} ${best.pair.start}-${best.pair.end}` : '';
  const source = opts.studentAware ? ` | student-aware ${affected} total pressure` : '';
  const actionText = opts.sticky ? 'Click a highlighted empty slot to preview; click again to move' : 'drop on a highlighted slot';
  $('twsStatusHover').textContent = `${opts.sticky ? 'Selected' : 'Dragging'} ${placementShortLabel(placement)} | ${clean} clean | recommended ${bestText}${pairText}${source} | ${actionText}`;
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

async function refreshStudentAwareSlotAssist(placementId, token, opts = {}) {
  const data = await api(`/ops/tw/placements/${placementId}/slot-candidates/`);
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
    S.slotAssist.candidates.set(key, { ...existing, ...row });
  });
  clearSlotAssistDecorations();
  const candidates = Array.from(S.slotAssist.candidates.values())
    .sort((a, b) => (a.rank || 999) - (b.rank || 999) || (a.score || 0) - (b.score || 0));
  paintSlotAssistCandidates(S.slotAssist.paneIdx, placement, candidates, { ...opts, studentAware: true });
}

function beginSlotAssist(paneIdx, placementId, opts = {}) {
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
  const token = S.slotAssist.requestToken;
  S.slotAssist.candidates = new Map(candidates.map(c => [slotAssistKey(c.kind, c.day, c.start), c]));
  paintSlotAssistCandidates(sourcePaneIdx, placement, candidates, opts);
  refreshStudentAwareSlotAssist(placementId, token, opts);
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
    resetProtections();
    S.boards = [];
    $('twsPublish').disabled = true;
    $('twsOptimise').disabled = true;
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
  S.crossBoardClashCount = (bdata && Number(bdata.cross_board_clashes || 0)) || 0;

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
  $('twsPublish').disabled = data.scenario.status === 'published';
  $('twsOptimise').disabled = false;
  $('twsOptimiseMenu') && ($('twsOptimiseMenu').disabled = false);
  $('twsExport').disabled = false;
  const published = data.scenario.status === 'published';
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
  $('twsStStudents').textContent = total || '—';
  $('twsStPlaced').textContent = placed || '—';
  $('twsStCross').textContent = String(S.crossBoardClashCount || 0);
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
    const opts = ['<option value="">—</option>']
      .concat(S.boards.map(b => `<option value="${b.id}"${b.id === p.boardId ? ' selected' : ''}>${esc(b.label)}</option>`))
      .join('');
    const off = !p.boardId;
    return `
      <div class="tws-bslot t${i}${off ? ' off' : ''}" data-pane="${i}">
        <div class="pos">${POS_LABELS[i]}</div>
        <div class="seg">
          <select data-role="board-select">${opts}</select>
        </div>
        <button class="off-toggle" data-role="off-toggle"
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
        <button data-action="reload" title="Reload">↻</button>
        <button data-action="maximise" title="Maximise">⤢</button>
      </span>
    </div>
    <div class="empty-pane">${T.selectBoard}</div>
  `;
  bindPaneControls(idx);
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
        <button data-action="reload" title="Reload">↻</button>
        <button data-action="maximise" title="Maximise">⤢</button>
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
        // Per-course pastel palette (user request) — matches XLSX export.
        const bg = courseColor(placement.course_code);
        const accent = courseColorBorder(placement.course_code);
        const style = `background:${bg};border-left-color:${accent}`;
        const cls = `cell filled${hasClash ? ' clash clash-' + clashTone : ''}${placement.is_locked ? ' locked' : ''}`;
        h += `<div class="${cls}" ${cellAttrs}${issueTitle} data-placement-id="${placement.id}" draggable="${placement.is_locked ? 'false' : 'true'}" style="${style}">`;
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
  el.querySelectorAll('.pane-hd .ri button').forEach(btn => {
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
    if (block) block.classList.toggle('collapsed');
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
    cell.addEventListener('click', (e) => {
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
  return !!(candidate?.pair?.companion && !candidate.pair.protected?.length);
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
  $('twsStatusHover').textContent = `Preview move ${label} -> ${day} ${start}-${end}: ${splitSlotDetail(candidate)}.${pairText} Click same slot again to apply.`;
  if (RP.open && RP.tab === 'selection') renderRpanel();
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

  const day = cell.dataset.day;
  const start = cell.dataset.start;
  const end = cell.dataset.end;

  // Drag from sidebar: create a planned placement on the target pane's board.
  if (payload.type === 'create_planned') {
    const boardId = S.panes[paneIdx].boardId;
    if (!boardId) { notify.error(IS_AR ? 'اختر لوحة أولاً' : 'Select a board in this pane first'); return; }
    const data = await api('/ops/tw/placements/create-planned/', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        board_id: boardId,
        course_code: payload.course_code,
        course_key: payload.course_key || payload.course_code,
        course_name: payload.course_name || payload.course_code,
        section_label: payload.section_label,
        day, start_time: start, end_time: end,
        capacity: payload.max_per_section || 40,
      }),
    });
    if (!data) return;
    const v = data.validation || {};
    if ((v.critical_count || 0) > 0) {
      notify.warning(IS_AR ? `تم الوضع مع ${v.critical_count} تعارض` : `Placed with ${v.critical_count} conflict(s)`);
    } else {
      notify.success(IS_AR ? 'تم الوضع' : 'Placed');
    }
    // Track as a 'create' action — undo will delete, redo will re-create.
    S.undoStack.push({
      type: 'create', placement_id: data.placement.id, board_id: boardId,
      term_section_id: data.placement.term_section_id,
      day, start_time: start, end_time: end, room: '',
    });
    S.redoStack = [];
    updateUndoRedoButtons();
    // Reload the target pane and refresh sidebar + aggregates
    await loadAndRenderPane(paneIdx);
    await refreshBoardsSummary();
    await loadSidebarBudget();
    return;
  }
  if (payload.type !== 'move' || !payload.placement_id) return;

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
    S.crossBoardClashCount = Number(bdata.cross_board_clashes || 0);
    updateAggregateMetrics();
    renderSlotBar();
  }
  await loadPlanLens({ rerender: true });
  // Invalidate capacity cache and refresh the panels if open — mutations
  // may have changed conflict counts and capacity deltas.
  RP.capacity = {};
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
// Apply an arbitrary C×R layout (clamped to [1..4] on each axis). Viewport
// fit is enforced by the picker before this is called, but we clamp again
// defensively so programmatic callers (shortcuts, maximise) never wedge a
// shape that can't render.
function applyLayout(cols, rows) {
  const prev = paneCount();
  const c = Math.max(1, Math.min(4, cols | 0));
  const r = Math.max(1, Math.min(4, rows | 0));
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
}
function maximisePane(idx) {
  applyLayout(1, 1);
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
    renderSlotBar();
    for (let i = 0; i < paneCount(); i++) renderPane(i);
  }
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
  body.classList.toggle('rp-open', RP.open);
  if (btn) btn.classList.toggle('primary', RP.open);
  if (RP.open) renderRpanel();
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
  else if (RP.tab === 'capacity') renderRpanelCapacity(body);
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

function bestRoomCandidate(roomData, currentRoom) {
  const rooms = roomData?.candidates || [];
  const current = roomCodeKey(currentRoom);
  return rooms.find(room => room.available && room.slot_clean && roomCodeKey(room.room_code) !== current)
    || rooms.find(room => room.available && roomCodeKey(room.room_code) !== current)
    || rooms.find(room => room.available && room.slot_clean)
    || rooms.find(room => room.available)
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
  return (
    (canBundleCandidate(c) ? 160000 : 0)
    + Number(c.impact_improvement || 0) * 1000
    + Number(c.student_improvement || 0) * 100
    + Number(c.critical_improvement || 0) * 100
    + Number(c.warning_improvement || 0) * 10
    - Number(c.score || 0)
  );
}

function guidedRoomRank(item) {
  const room = item?.room || {};
  return (
    (room.available ? 100000 : 0)
    + (room.slot_clean ? 20000 : 0)
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
    const candidate = bestVisibleQueueCandidate(item.paneIdx, item.placement, data);
    if (!candidate) return;
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
      message: 'No improving empty slot is visible for this action. Open another board pane or unlock protected sections.',
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
  notify.info && notify.info(`Opened required sections for ${action.course_code || 'this course'}`);
}

async function beginGuidedAction(action) {
  if (!action) return;
  const token = ++RP.builder.resolverToken;
  RP.builder.activeAction = action;
  RP.builder.resolver = { status: 'loading', action, message: 'Building the safest guided fix...' };

  const placementIds = actionPlacementIds(action);
  if (!placementIds.length) {
    if (action.kind === 'missing_section') await focusMissingSectionAction(action);
    RP.builder.resolver = {
      status: 'manual',
      action,
      message: action.kind === 'missing_section'
        ? 'Drag the missing section from Required Sections into a highlighted slot.'
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
      <div class="tws-guided-detail">${esc(resolver.message || 'Computing the safest option...')}</div>
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
  const roomText = room
    ? `${room.room_code}${room.capacity ? ` | ${room.capacity} seats` : ''}${room.slot_clean ? ' | clean' : room.available ? ' | available' : ''}`
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
      const improvement = Number(c.impact_improvement || 0);
      const studentImprovement = Number(c.student_improvement || 0);
      const criticalImprovement = Number(c.critical_improvement || 0);
      return improvement > 0 || studentImprovement > 0 || criticalImprovement > 0;
    })
    .sort((a, b) =>
      (canBundleCandidate(b) ? 160 : 0) - (canBundleCandidate(a) ? 160 : 0)
      ||
      Number(b.impact_improvement || 0) - Number(a.impact_improvement || 0)
      || Number(b.student_improvement || 0) - Number(a.student_improvement || 0)
      || Number(b.critical_improvement || 0) - Number(a.critical_improvement || 0)
      || Number(a.score || 0) - Number(b.score || 0)
    )[0] || null;
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

  const items = [];
  placementIds.forEach(id => {
    const located = findPlacement(id);
    const data = RP.fixQueue.cache[id];
    if (!located || !data) return;
    const candidate = bestVisibleQueueCandidate(located.paneIdx, located.placement, data);
    if (!candidate) return;
    items.push({
      placementId: id,
      paneIdx: located.paneIdx,
      label: placementShortLabel(located.placement),
      placement: located.placement,
      candidate,
    });
  });

  items.sort((a, b) =>
    (canBundleCandidate(b.candidate) ? 160 : 0) - (canBundleCandidate(a.candidate) ? 160 : 0)
    || Number(b.candidate.impact_improvement || 0) - Number(a.candidate.impact_improvement || 0)
    || Number(b.candidate.student_improvement || 0) - Number(a.candidate.student_improvement || 0)
    || Number(a.candidate.score || 0) - Number(b.candidate.score || 0)
  );
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
    card.addEventListener('click', () => previewFixQueueItem(parseInt(card.dataset.fixIdx)));
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

async function autoFixOneBoard() {
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
      <div class="section-head fix">${IS_AR ? 'الخطوات التالية' : 'Next best actions'}<span class="n" id="twsNextActionsCount">...</span></div>
      <div id="twsNextActions" class="tws-builder-actions">${renderBuilderActionsHtml(RP.builder.actions.length ? RP.builder.actions : null)}</div>
      <div class="tws-empty-state"><span class="ic">✓</span>${IS_AR ? 'لا توجد مشاكل' : 'No issues'}</div>`;
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
          <button type="button" class="tws-mini-action tws-cross-fix" data-cross-fix='${esc(JSON.stringify(fixPayloadA))}'>Safe move ${esc(c.section_a || 'A')}</button>
          <button type="button" class="tws-mini-action tws-cross-fix" data-cross-fix='${esc(JSON.stringify(fixPayloadB))}'>Safe move ${esc(c.section_b || 'B')}</button>
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
    detail: action.detail || (IS_AR ? 'اختر نقلة آمنة تقلل الطلاب المتأثرين.' : 'Find a safer slot that reduces affected students.'),
    cta: IS_AR ? 'اعرض النقلات الآمنة' : 'Show safe moves',
    board_id: action.board_id,
    placement_ids: [action.placement_id],
  });
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
  const byCourse = new Map();
  let totalDemand = 0, totalCap = 0, totalDeficit = 0;
  boards.forEach(b => {
    const cap = RP.capacity[b.boardId];
    if (!cap) return;
    const t = cap.totals || {};
    totalDemand += t.demand || 0;
    totalCap += t.raw_capacity || 0;
    totalDeficit += t.deficit || 0;
    (cap.courses || []).forEach(c => {
      const prev = byCourse.get(c.course_code) || { course_code: c.course_code, demand: 0, raw_capacity: 0, deficit: 0 };
      prev.demand += c.demand || 0;
      prev.raw_capacity += c.raw_capacity || 0;
      prev.deficit += c.deficit || 0;
      byCourse.set(c.course_code, prev);
    });
  });
  const courses = [...byCourse.values()].sort((a, b) => (b.deficit || 0) - (a.deficit || 0));
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
    html += `<div class="tws-cap-row">
      <div class="hd">
        <span class="code">${esc(c.course_code)}</span>
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

function selectionRoomSlot(placement, pending) {
  return pending
    ? { day: pending.day, start: pending.start, end: pending.end, label: 'Preview slot' }
    : { day: placement.day, start: placement.start_time, end: placement.end_time, label: 'Current slot' };
}

function roomCacheKey(placementId, slot) {
  return [placementId, slot.day, slot.start, slot.end].join('|');
}

function roomCandidateCardHtml(room, idx, currentRoom) {
  const tone = room.tone || (room.available ? 'clean' : 'block');
  const reasons = room.reasons && room.reasons.length ? room.reasons.slice(0, 2).join(' | ') : (room.available ? 'available' : 'not available');
  const isCurrent = String(currentRoom || '').trim().toUpperCase() === String(room.room_code || '').trim().toUpperCase();
  const apply = room.available
    ? `<button class="tws-room-apply" data-room-idx="${idx}" type="button">${isCurrent ? 'Keep' : 'Apply'}</button>`
    : `<button class="tws-room-apply" type="button" disabled>Blocked</button>`;
  return `<div class="tws-room-card ${tone}">
    <div class="tws-room-main">
      <b>${esc(room.room_code)}</b>
      <span>${esc(room.room_type)} · ${esc(room.section || 'Any')} · ${room.capacity || 0}</span>
      ${apply}
    </div>
    <div class="tws-room-detail">${esc(reasons)}</div>
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

async function applyRoomCandidate(placementId, room, slot) {
  const located = findPlacement(placementId);
  if (!located || !room?.room_code) return null;
  const p = located.placement;
  const oldRoom = p.room || '';
  const oldDay = p.day;
  const oldStart = p.start_time;
  const oldEnd = p.end_time;
  if (oldRoom === room.room_code && oldDay === slot.day && oldStart === slot.start) {
    notify.success('Room already assigned');
    return { already_assigned: true };
  }
  const data = await doMove(placementId, slot.day, slot.start, slot.end, room.room_code);
  if (!data) {
    notify.error('Room assignment failed');
    return null;
  }
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
  clearSlotAssist();
  const boardId = S.panes[located.paneIdx]?.boardId;
  for (let i = 0; i < paneCount(); i++) {
    if (S.panes[i].boardId === boardId) await loadAndRenderPane(i);
  }
  await refreshBoardsSummary();
  S.selectedPlacementId = placementId;
  S.selectedPaneIdx = located.paneIdx;
  notify.success(`Assigned room ${room.room_code}`);
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
    roomBox.innerHTML = `<div class="tws-room-target">
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
    </div>
  `;
  // Wire the button — no inline onclick
  const openBtn = body.querySelector('#twsSelOpen');
  if (openBtn) openBtn.addEventListener('click', () => openDrawer(located.paneIdx, p.id));
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
    el.addEventListener('dragstart', (e) => {
      const key = el.dataset.key || el.dataset.code;
      const code = el.dataset.code;
      const used = parseInt(el.dataset.used || '0');
      const b = SB.budget.find(x => courseKeyOf(x) === key) || {};
      const payload = {
        type: 'create_planned',
        course_code: code,
        course_key: courseKeyOf(b) || code,
        course_name: b.course_name || '',
        section_label: `S${used + 1}`,
        department: b.department || '',
        credit_hours: b.credit_hours || 0,
        max_per_section: b.max_per_section || 40,
        total_students: b.total_demand || 0,
      };
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
function openModal(opts) {
  const modal = $('twsModal');
  const backdrop = $('twsModalBackdrop');
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
    btn.addEventListener('click', async () => {
      const res = b.onClick ? await b.onClick(modal) : undefined;
      if (res !== false) closeModal();
    });
    footer.appendChild(btn);
  });
  modal.classList.add('open');
  backdrop.classList.add('open');
  _modalOpen = true;
  _modalOnClose = opts.onClose || null;
  // Focus first input
  setTimeout(() => bodyEl.querySelector('input, select, textarea, button')?.focus(), 40);
}
let _modalOnClose = null;
function closeModal() {
  $('twsModal').classList.remove('open');
  $('twsModalBackdrop').classList.remove('open');
  _modalOpen = false;
  if (_modalOnClose) { try { _modalOnClose(); } catch {} _modalOnClose = null; }
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
  const byCourse = new Map();
  let totDemand = 0, totCap = 0, totDeficit = 0;
  boards.forEach(b => {
    const cap = RP.capacity[b.boardId];
    if (!cap) return;
    const t = cap.totals || {};
    totDemand += t.demand || 0;
    totCap += t.raw_capacity || 0;
    totDeficit += t.deficit || 0;
    (cap.courses || []).forEach(c => {
      const prev = byCourse.get(c.course_code) || {
        course_code: c.course_code, demand: 0, raw_capacity: 0,
        placed_sections: 0, deficit: 0,
      };
      prev.demand += c.demand || 0;
      prev.raw_capacity += c.raw_capacity || 0;
      prev.placed_sections += c.placed_sections || 0;
      prev.deficit += c.deficit || 0;
      byCourse.set(c.course_code, prev);
    });
  });
  const courses = [...byCourse.values()].sort((a, b) => (b.deficit || 0) - (a.deficit || 0) || a.course_code.localeCompare(b.course_code));
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
    html += `<tr>
      <td class="code">${esc(c.course_code)}</td>
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
  $('twsDrawerBackdrop').classList.add('open');
}

function closeDrawer() {
  DRAWER.placementId = null;
  DRAWER.paneIdx = null;
  $('twsDrawer').classList.remove('open');
  $('twsDrawer').setAttribute('aria-hidden', 'true');
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
async function doRemoveApi(placementId) {
  return api(`/ops/tw/placements/${placementId}/remove/`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ override: true }),
  });
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
  const candList = (o.all_scores && o.all_scores.length > 1) ? `
    <div class="section-title">${IS_AR ? 'مقارنة المرشحين' : 'Candidate comparison'} (${o.candidates_evaluated || o.all_scores.length})</div>
    <table style="width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:11px">
      <thead><tr style="color:var(--t4);border-bottom:1px solid var(--line)">
        <th style="text-align:left;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Strategy</th>
        <th style="text-align:right;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Tier-A</th>
        <th style="text-align:right;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Unres.</th>
        <th style="text-align:right;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Clash</th>
        <th style="text-align:right;padding:4px 6px;font-size:9.5px;text-transform:uppercase">Gaps</th>
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
  let diffBlock = '';
  if (mode === 'current' && Array.isArray(o.baseline_score) && Array.isArray(o.final_score)
      && o.baseline_score.length >= 5 && o.final_score.length >= 5) {
    const bs = o.baseline_score, fs = o.final_score;
    const improved = fs[1] < bs[1] || fs[3] < bs[3] || fs[4] < bs[4];
    const safeToLocale = v => (typeof v === 'number' ? v.toLocaleString() : String(v ?? '—'));
    diffBlock = improved
      ? `<div style="padding:8px 12px;border-radius:6px;background:rgba(10,142,110,0.1);border:1px solid rgba(10,142,110,0.25);margin-top:10px">
          <div style="font-weight:700;color:var(--teal);margin-bottom:4px">✓ ${IS_AR ? 'تم التحسين' : 'Board improved'}</div>
          <div style="font-size:11px">Unresolved: <b>${bs[1]}</b> → <b style="color:var(--teal)">${fs[1]}</b></div>
          ${bs[3] !== fs[3] ? `<div style="font-size:11px">Clashes: <b>${bs[3]}</b> → <b style="color:var(--teal)">${fs[3]}</b></div>` : ''}
          <div style="font-size:11px">Gaps: <b>${safeToLocale(bs[4])}</b> → <b>${safeToLocale(fs[4])}</b></div>
        </div>`
      : `<div style="padding:8px 12px;border-radius:6px;background:rgba(80,104,240,0.08);margin-top:10px;color:#5068F0;font-weight:600">━ ${IS_AR ? 'الجدول الحالي في أفضل حالة' : 'Current board is already optimal'}</div>`;
  }
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
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px">
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
    </div>
    ${candList}
    <div class="section-title" style="color:var(--teal);margin-top:12px">${finalLabel}</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">${scoreRows}</table>
    ${diffBlock}
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

/* ── Init ── */
(function init() {
  $('twsScenario').addEventListener('change', onScenarioChange);
  $('twsOptimise').addEventListener('click', () => doOptimise('current'));
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
  $('twsModalClose')?.addEventListener('click', closeModal);
  $('twsModalBackdrop')?.addEventListener('click', closeModal);

  // Right panel toggle + tabs
  $('twsRpanelToggle')?.addEventListener('click', () => toggleRpanel());
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
  $('twsClose').addEventListener('click', () => {
    if (window.history.length > 1) window.history.back();
    else window.location.href = '/timetable-workspace/';
  });
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
      if (_modalOpen) { closeModal(); return; }
      if ($('twsDrawer').classList.contains('open')) { closeDrawer(); return; }
      $('twsClose').click();
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
