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
  panes: [
    { boardId: null, group: 0, boardData: null },
    { boardId: null, group: 0, boardData: null },
    { boardId: null, group: 0, boardData: null },
    { boardId: null, group: 0, boardData: null },
    { boardId: null, group: 0, boardData: null },
    { boardId: null, group: 0, boardData: null },
  ],
  // Global undo/redo stacks shared across panes, matching the main page's
  // single-stack model. Each action has enough state to fully revert.
  undoStack: [],
  redoStack: [],
  selectedPaneIdx: null,
  selectedPlacementId: null,
  layout: 'quad',
  dragSource: null,
};

const POS_LABELS = ['Pane A', 'Pane B', 'Pane C', 'Pane D', 'Pane E', 'Pane F'];

/* Number of panes currently visible given the layout mode.
   Hexa (3x2) shows 6; quad shows 4; tri shows 3; vert/horz show 2; single shows 1. */
function paneCount() {
  return ({ single: 1, vert: 2, horz: 2, tri: 3, quad: 4, hexa: 6 }[S.layout] || 4);
}

// Right inspector panel state
const RP = {
  open: false,
  tab: 'issues',
  capacity: {}, // boardId -> capacity response
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
function courseColor(code) {
  if (!code) return COURSE_COLORS[0][0];
  if (!_courseColorMap[code]) {
    const idx = Object.keys(_courseColorMap).length % COURSE_COLORS.length;
    _courseColorMap[code] = COURSE_COLORS[idx];
  }
  return _courseColorMap[code][0];
}
function courseColorBorder(code) {
  if (!code) return COURSE_COLORS[0][1];
  if (!_courseColorMap[code]) courseColor(code);
  return _courseColorMap[code][1];
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
  SB.budget = [];
  SB.search = '';
  const search = $('twsSectionSearch'); if (search) search.value = '';
  // If a drawer is open it's pointing at a now-invalid placement id.
  if (typeof DRAWER !== 'undefined' && DRAWER.placementId != null) closeDrawer();

  const sid = $('twsScenario').value;
  if (!sid) {
    S.scenarioId = null;
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
  const bdata = await api(`/ops/tw/boards/?scenario_id=${sid}`);
  S.boards = (bdata && bdata.boards) || [];

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
  let total = 0, placed = 0, cross = 0;
  S.boards.forEach(b => {
    total += (b.primary_count || 0) + (b.visitor_count || 0);
    placed += (b.placement_count || 0);
    cross += (b.critical || 0);
  });
  $('twsStStudents').textContent = total || '—';
  $('twsStPlaced').textContent = placed || '—';
  $('twsStCross').textContent = cross || '0';
}

/* ── Slot bar (boards-on-canvas) ── */
function renderSlotBar() {
  const wrap = $('twsBoardsGrid');
  if (!S.boards.length) {
    wrap.innerHTML = `<span class="lbl" style="color:var(--t4)">${T.noScenario}</span>`;
    return;
  }
  wrap.innerHTML = S.panes.map((p, i) => {
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
  const placements = data.placements || [];

  // Split into lecture vs lab by duration
  const lectP = placements.filter(pl => !isLabPlacement(pl));
  const labP = placements.filter(pl => isLabPlacement(pl));
  const groups = groupPlacements(placements);
  if (p.group >= groups.length) p.group = 0;
  const activeGroup = groups[p.group];

  const groupLect = activeGroup ? lectP.filter(pl => (pl.section || 'S1') === activeGroup.id) : [];
  const groupLab = activeGroup ? labP.filter(pl => (pl.section || 'S1') === activeGroup.id) : [];

  const conflicts = data.conflicts || {};
  const overlaps = conflicts.overlaps || [];
  const instrClashes = conflicts.instructor_clashes || [];
  const roomClashes = conflicts.room_clashes || [];
  const clashIds = new Set();
  overlaps.forEach(o => (o.ids || []).forEach(id => clashIds.add(id)));
  instrClashes.forEach(c => (c.ids || []).forEach(id => clashIds.add(id)));
  roomClashes.forEach(c => (c.ids || []).forEach(id => clashIds.add(id)));

  // Group-level clash detection: does any placement in each group collide?
  const groupHasClash = groups.map(g =>
    g.placements.some(pl => clashIds.has(pl.id))
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
        ${renderGridHTML(slots, groupLect, clashIds, 'lect')}
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
        ${renderGridHTML(labSlots, groupLab, clashIds, 'lab')}
      </div>
    </div>
    <div class="pane-status">
      <span class="dot${hasGroupClash ? ' warn' : ''}"></span>
      <span>${esc(board.label)} · G${p.group + 1}</span>
      <span>${(board.primary_count || 0)}${(board.visitor_count || 0) ? '+' + board.visitor_count : ''} st</span>
      <span class="sp"></span>
      ${groups.length > 1
        ? `<span>${groups.length - 1} ${IS_AR ? 'مجموعات أخرى' : 'more groups'} ↓</span>`
        : `<span>${(board.placement_count || placements.length)} ${T.placed.toLowerCase()}</span>`
      }
    </div>
  `;
  bindPaneControls(idx);
}

function renderGridHTML(slots, placements, clashIds, kind) {
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
      const hasClash = placement && clashIds.has(placement.id);
      const cellAttrs = `data-day="${day}" data-start="${slot.start}" data-end="${slot.end}"`;
      if (placement) {
        const room = placement.room ? esc(placement.room) : '';
        const stu = placement.available_capacity || '';
        // Per-course pastel palette (user request) — matches XLSX export.
        const bg = courseColor(placement.course_code);
        const accent = courseColorBorder(placement.course_code);
        const style = `background:${bg};border-left-color:${accent}`;
        const cls = `cell filled${hasClash ? ' clash' : ''}${placement.is_locked ? ' locked' : ''}`;
        h += `<div class="${cls}" ${cellAttrs} data-placement-id="${placement.id}" draggable="${placement.is_locked ? 'false' : 'true'}" style="${style}">`;
        h += `<span class="cid">${esc(placement.course_code)} ${esc(placement.section || '')}</span>`;
        h += `<span class="cmeta">${room}${stu ? '·' + stu : ''}</span>`;
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
    cell.addEventListener('mouseenter', () => broadcastHover(idx, cell.dataset.day, cell.dataset.start));
    cell.addEventListener('mouseleave', () => broadcastHover(idx, null, null));
    cell.addEventListener('dragover', (e) => {
      e.preventDefault();
      // Mirror main-page feedback: highlight occupied cells in amber (will
      // collide) and empty cells in teal (clean drop).
      if (cell.classList.contains('filled')) cell.classList.add('drop-warning');
      else cell.classList.add('drop-valid');
    });
    cell.addEventListener('dragleave', () =>
      cell.classList.remove('drop-valid', 'drop-warning', 'drop-critical'));
    cell.addEventListener('drop', (e) => onCellDrop(idx, cell, e));
    if (cell.classList.contains('filled')) {
      cell.addEventListener('dragstart', (e) => {
        if (cell.classList.contains('locked')) { e.preventDefault(); return; }
        const pid = cell.dataset.placementId;
        S.dragSource = { paneIdx: idx, placementId: pid ? parseInt(pid) : null };
        e.dataTransfer.setData('text/plain', JSON.stringify({ type: 'move', placement_id: S.dragSource.placementId, source_pane: idx }));
        e.dataTransfer.effectAllowed = 'move';
      });
      cell.addEventListener('click', () => {
        document.querySelectorAll('.tws-pane .cell.selected').forEach(c => c.classList.remove('selected'));
        cell.classList.add('selected');
        S.selectedPaneIdx = idx;
        S.selectedPlacementId = parseInt(cell.dataset.placementId);
        $('twsStatusHover').textContent = `Selected ${cell.querySelector('.cid')?.textContent}`;
        // Refresh selection tab if the inspector is showing it
        if (RP.open && RP.tab === 'selection') renderRpanel();
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
    const found = (data.placements || []).find(p => p.id === placementId);
    if (found) return { placement: found, paneIdx: i };
  }
  return null;
}
async function onCellDrop(paneIdx, cell, e) {
  e.preventDefault();
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

  // Capture old position BEFORE the mutation so undo can revert cleanly,
  // matching the main page's onDrop behaviour. If the placement isn't in
  // our cached boardData (stale cache, mid-optimise, etc.) we abort — the
  // main page relies on the same pre-check.
  const located = findPlacement(payload.placement_id);
  if (!located) { notify.error(IS_AR ? 'تعذّر تحديد الموقع' : 'Source placement not found'); return; }
  const oldDay = located.placement.day;
  const oldStart = located.placement.start_time;
  const oldEnd = located.placement.end_time;

  // day, start, end already captured at the top of onCellDrop
  // Skip no-op drops
  if (day === oldDay && start === oldStart) return;

  const data = await api(`/ops/tw/placements/${payload.placement_id}/move/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ day, start_time: start, end_time: end }),
  });
  if (!data) { notify.error(IS_AR ? 'تعذّر النقل' : 'Move failed'); return; }

  // Push full-fidelity undo entry — identical shape to main-page undo stack.
  S.undoStack.push({
    type: 'move',
    placement_id: payload.placement_id,
    old_day: oldDay, old_start: oldStart, old_end: oldEnd,
    new_day: day, new_start: start, new_end: end,
  });
  S.redoStack = [];
  updateUndoRedoButtons();

  // Match main-page UX: show a warning toast when placement lands on conflicts
  // and a success toast otherwise.
  const v = data.validation || {};
  if ((v.critical_count || 0) > 0) {
    notify.warning(IS_AR
      ? `تم النقل مع ${v.critical_count} تعارض`
      : `Moved with ${v.critical_count} conflict(s)`);
  } else {
    notify.success(IS_AR ? 'تم النقل' : 'Moved');
  }

  // Refresh every pane that could be affected (target, source, and any pane
  // showing the same board), plus aggregate metrics.
  const sourceIdx = Number.isFinite(payload.source_pane) ? payload.source_pane : paneIdx;
  const boardIds = new Set([S.panes[paneIdx].boardId, S.panes[sourceIdx].boardId]);
  for (let i = 0; i < paneCount(); i++) {
    if (boardIds.has(S.panes[i].boardId)) await loadAndRenderPane(i);
  }
  await refreshBoardsSummary();
}

async function refreshBoardsSummary() {
  if (!S.scenarioId) return;
  const bdata = await api(`/ops/tw/boards/?scenario_id=${S.scenarioId}`);
  if (bdata && bdata.boards) {
    S.boards = bdata.boards;
    updateAggregateMetrics();
    renderSlotBar();
  }
  // Invalidate capacity cache and refresh the panels if open — mutations
  // may have changed conflict counts and capacity deltas.
  RP.capacity = {};
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
function setLayout(mode) {
  const prev = S.layout;
  S.layout = mode;
  const q = $('twsQuad');
  q.className = 'tws-quad layout-' + mode;
  document.querySelectorAll('#twsLayoutSwitch button').forEach(b => {
    b.classList.toggle('on', b.dataset.layout === mode);
  });
  $('twsStatusLayout').textContent =
    ({ single: 'Layout 1', vert: 'Layout 2×1', horz: 'Layout 1×2',
       quad: 'Layout 2×2', tri: 'Layout 1+2', hexa: 'Layout 3×2' })[mode] || '';
  // Growing the pane count? Seed the newly visible slots with unused
  // boards and render them — they may still be placeholders from init.
  if (prev && paneCount() > ({ single: 1, vert: 2, horz: 2, tri: 3, quad: 4, hexa: 6 }[prev] || 4)) {
    const used = new Set(S.panes.filter(p => p.boardId).map(p => p.boardId));
    for (let i = 0; i < paneCount(); i++) {
      if (!S.panes[i].boardId) {
        const next = S.boards.find(b => !used.has(b.id));
        if (next) { S.panes[i].boardId = next.id; used.add(next.id); }
      }
    }
    renderSlotBar();
    for (let i = 0; i < paneCount(); i++) loadAndRenderPane(i);
  }
}
function maximisePane(idx) {
  setLayout('single');
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
      // Dedupe cross-board by the pair of ids (a,b) so it's not listed twice
      const key = [o.board_a_id, o.board_b_id].sort().join('-') + ':' + (o.section_a || '') + (o.section_b || '');
      if (!crossBoard.some(x => x._key === key)) crossBoard.push({ ...o, _key: key });
    });
  });
  const total = overlaps.length + iClashes.length + rClashes.length + crossBoard.length;
  $('twsRpCountIssues').textContent = String(total);
  if (total === 0) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">✓</span>${IS_AR ? 'لا توجد مشاكل' : 'No issues'}</div>`;
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
    return `<div class="tws-issue" data-cross='${JSON.stringify({ a: c.board_a_id, b: c.board_b_id, sa: c.section_a, sb: c.section_b })}'>
      <span class="dot cross"></span>
      <div class="body">
        <div class="title">${esc(c.section_a)} ↔ ${esc(c.section_b)} ${paneBadges}</div>
        <div class="meta">${c.overlap_count || 0} ${IS_AR ? 'طالب مشترك' : 'shared students'} · ${esc(c.board_a_label || '')} ↔ ${esc(c.board_b_label || '')}</div>
      </div>
    </div>`;
  };

  let html = '';
  if (overlaps.length) {
    html += `<div class="section-head danger">${IS_AR ? 'تداخل زمني' : 'Time overlap'}<span class="n">${overlaps.length}</span></div>`;
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
    html += `<div class="section-head cross">${IS_AR ? 'عبر اللوحات' : 'Cross-board'}<span class="n">${crossBoard.length}</span></div>`;
    html += crossBoard.map(crossRow).join('');
  }
  body.innerHTML = html;

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
function handleCrossBoardClick(c) {
  // Identify panes showing these boards. If neither is on canvas, load
  // board B into the first empty (or last) pane so user sees both sides.
  const inA = S.panes.findIndex(p => p.boardId === c.a);
  const inB = S.panes.findIndex(p => p.boardId === c.b);
  if (inA < 0 && inB < 0) {
    // Load both into the first two panes
    setPaneBoard(0, c.a);
    setPaneBoard(1, c.b);
    notify.info && notify.info(IS_AR ? 'تم تحميل اللوحتين' : 'Loaded both boards');
  } else if (inA < 0) {
    const empty = S.panes.findIndex(p => !p.boardId) >= 0 ? S.panes.findIndex(p => !p.boardId) : 3;
    setPaneBoard(empty, c.a);
  } else if (inB < 0) {
    const empty = S.panes.findIndex(p => !p.boardId) >= 0 ? S.panes.findIndex(p => !p.boardId) : 3;
    setPaneBoard(empty, c.b);
  }
  // Scroll into pane A view
  const pi = S.panes.findIndex(p => p.boardId === c.a);
  if (pi >= 0) paneEl(pi)?.scrollIntoView({ block: 'center' });
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
    <div style="display:flex;gap:6px;margin-top:12px">
      <button class="tws-btn" id="twsSelOpen" style="flex:1">${IS_AR ? 'عرض التفاصيل' : 'Open drawer'}</button>
    </div>
  `;
  // Wire the button — no inline onclick
  const openBtn = body.querySelector('#twsSelOpen');
  if (openBtn) openBtn.addEventListener('click', () => openDrawer(located.paneIdx, p.id));
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
  const q = (SB.search || '').trim().toUpperCase();
  const filtered = SB.budget
    .filter(b => !q || (b.course_code || '').toUpperCase().includes(q) || (b.department || '').toUpperCase().includes(q))
    .sort((a, b) => (b.remaining_sections || 0) - (a.remaining_sections || 0) || a.course_code.localeCompare(b.course_code));
  if (!filtered.length) {
    body.innerHTML = `<div class="tws-empty-state"><span class="ic">—</span>${IS_AR ? 'لا توجد نتائج' : 'No matches'}</div>`;
    return;
  }
  body.innerHTML = filtered.map(b => {
    const used = b.used_sections || 0;
    const planned = b.planned_sections || 0;
    const remaining = b.remaining_sections != null ? b.remaining_sections : Math.max(0, planned - used);
    const exhausted = remaining <= 0;
    // Store payload as an id; the real object lives in a side map so we
    // don't have to round-trip JSON through a double-quoted attribute,
    // which would mis-escape any single-quote character in the string.
    return `<div class="tws-sec-item${exhausted ? ' exhausted' : ''}" draggable="${exhausted ? 'false' : 'true'}" data-code="${esc(b.course_code)}" data-used="${used}" title="${esc(b.department || b.course_code)} · ${b.credit_hours || 0}h">
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
    </div>`;
  }).join('');
  // Wire drag sources — look up the budget row by code + used count at
  // dragstart time so we always send fresh data even after budget updates.
  body.querySelectorAll('.tws-sec-item[draggable="true"]').forEach(el => {
    el.addEventListener('dragstart', (e) => {
      const code = el.dataset.code;
      const used = parseInt(el.dataset.used || '0');
      const b = SB.budget.find(x => x.course_code === code) || {};
      const payload = {
        type: 'create_planned',
        course_code: code,
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

async function doMove(placementId, day, start, end) {
  const data = await api(`/ops/tw/placements/${placementId}/move/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ day, start_time: start, end_time: end }),
  });
  return data;
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
    const data = await doMove(action.placement_id, action.old_day, action.old_start, action.old_end);
    if (!data) { S.undoStack.push(action); return; }
    S.redoStack.push(action);
    notify.success(IS_AR ? 'تم التراجع' : 'Undone');
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
    const data = await doMove(action.placement_id, action.new_day, action.new_start, action.new_end);
    if (!data) { S.redoStack.push(action); return; }
    S.undoStack.push(action);
    notify.success(IS_AR ? 'تم الإعادة' : 'Redone');
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
  window.open(`/ops/tw/scenarios/${S.scenarioId}/export.xlsx`, '_blank');
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
  document.querySelectorAll('#twsLayoutSwitch button').forEach(b => {
    b.addEventListener('click', () => setLayout(b.dataset.layout));
  });
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
      // Uppercase L (Shift+L) cycles layout — keeps lowercase L for per-cell lock
      const modes = ['quad', 'vert', 'horz', 'single'];
      setLayout(modes[(modes.indexOf(S.layout) + 1) % modes.length]);
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
  for (let i = 0; i < paneCount(); i++) renderPaneEmpty(i);
  loadScenarios();
})();

// Fully self-contained — no globals leak onto window.
