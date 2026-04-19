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

const POS_LABELS = ['Pane A', 'Pane B', 'Pane C', 'Pane D'];

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

// 20 distinct pastel colours — same palette the main page uses so the
// split view matches the XLSX export and the main grid exactly.
const COURSE_COLORS = [
  '#D4E6F1','#D5F5E3','#FADBD8','#FCF3CF','#D7BDE2',
  '#A9DFBF','#F9E79F','#AED6F1','#F5CBA7','#A3E4D7',
  '#E8DAEF','#FDEBD0','#ABB2B9','#A2D9CE','#F5B7B1',
  '#D6DBDF','#ABEBC6','#FAD7A0','#D2B4DE','#AEB6BF',
];
const _courseColorMap = {};
function courseColor(code) {
  if (!code) return COURSE_COLORS[0];
  if (!_courseColorMap[code]) {
    const idx = Object.keys(_courseColorMap).length % COURSE_COLORS.length;
    _courseColorMap[code] = COURSE_COLORS[idx];
  }
  return _courseColorMap[code];
}
// Side-band accent stripe matching the course card border on the main page.
function courseColorBorder(code) {
  const bg = courseColor(code);
  // Derive a darker accent by reducing lightness — simple approximation by
  // shifting the hex toward black.
  const hex = bg.replace('#', '');
  const r = Math.max(0, parseInt(hex.slice(0, 2), 16) - 60);
  const g = Math.max(0, parseInt(hex.slice(2, 4), 16) - 60);
  const b = Math.max(0, parseInt(hex.slice(4, 6), 16) - 60);
  return `rgb(${r},${g},${b})`;
}
function isLabPlacement(p) {
  const toMin = t => { const [h, m] = String(t).split(':').map(Number); return h * 60 + m; };
  return (toMin(p.end_time) - toMin(p.start_time)) > 80;
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
  const sid = $('twsScenario').value;
  if (!sid) {
    S.scenarioId = null;
    S.boards = [];
    $('twsPublish').disabled = true;
    $('twsOptimise').disabled = true;
    $('twsExport').disabled = true;
    renderSlotBar();
    for (let i = 0; i < 4; i++) renderPaneEmpty(i);
    return;
  }
  const data = await api(`/ops/tw/scenarios/${sid}/`);
  if (!data) return;
  S.scenarioId = data.scenario.id;
  S.scenarioMeta = data.scenario;
  const bdata = await api(`/ops/tw/boards/?scenario_id=${sid}`);
  S.boards = (bdata && bdata.boards) || [];

  for (let i = 0; i < 4; i++) {
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
  $('twsExport').disabled = false;

  updateAggregateMetrics();
  renderSlotBar();
  for (let i = 0; i < 4; i++) await loadAndRenderPane(i);
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
  const wrap = $('twsSlots');
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
      <div class="tws-bslot t${i}${off ? ' off' : ''}">
        <div class="pos">${POS_LABELS[i]}</div>
        <div class="seg">
          <select onchange="setPaneBoard(${i}, this.value)">${opts}</select>
        </div>
        <button class="off-toggle" onclick="togglePaneOff(${i})"
                title="${off ? (IS_AR ? 'عرض' : 'Show pane') : (IS_AR ? 'إخفاء' : 'Hide pane')}">${off ? '+' : '×'}</button>
      </div>`;
  }).join('');
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
      <span class="dot"></span>
      <span class="term-name">—</span>
      <span class="ri">
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

  const gtabsHtml = groups.map((g, gi) => `
    <span class="gtab${gi === p.group ? ' on' : ''}" data-group="${gi}">
      G${gi + 1}<span class="cx">${g.placements.length}c</span>${groupHasClash[gi] ? '<span class="clash-dot"></span>' : ''}
    </span>
  `).join('');

  const placedCount = activeGroup ? activeGroup.placements.length : 0;
  const hasGroupClash = groupHasClash[p.group];

  el.innerHTML = `
    <div class="pane-hd">
      <span class="dot"></span>
      <span class="term-name">${esc(board.label)}</span>
      <div class="gtabs">${gtabsHtml || '<span class="gtab on">—</span>'}</div>
      <span class="kpi">${T.placed} <b>${placedCount}</b></span>
      <span class="kpi">${hasGroupClash ? `<b class="warn">${T.clashShort}</b>` : `<b style="color:var(--teal)">${T.noClash}</b>`}</span>
      <span class="ri">
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
          <span class="note">${groupLab.length ? `${groupLab.length} ${T.placed.toLowerCase()}` : T.noLab}</span>
        </div>
        ${renderGridHTML(labSlots, groupLab, clashIds, 'lab')}
      </div>
    </div>
    <div class="pane-status">
      <span>${esc(board.label)}</span>
      <span>${(board.primary_count || 0)}${(board.visitor_count || 0) ? '+' + board.visitor_count : ''} st</span>
      <span>${groups.length} groups</span>
      <span class="sp"></span>
      <span>${(board.placement_count || placements.length)} placed</span>
    </div>
  `;
  bindPaneControls(idx);
}

function renderGridHTML(slots, placements, clashIds, kind) {
  const numSlots = slots.length;
  const cols = `26px repeat(${numSlots}, 1fr)`;
  let h = `<div class="block-grid" style="grid-template-columns:${cols};grid-template-rows:16px repeat(5,minmax(0,1fr))">`;
  h += `<div class="cor">${kind === 'lab' ? 'LAB' : 'SLOT'}</div>`;
  slots.forEach((s, i) => h += `<div class="dh" title="${esc(s.start)}–${esc(s.end)}">${esc(s.label || String(i + 1))}</div>`);
  DAYS.forEach((day, di) => {
    h += `<div class="slbl">${esc(DAY_LABELS[di])}</div>`;
    slots.forEach((slot) => {
      const placement = placements.find(pl => pl.day === day && pl.start_time === slot.start);
      const hasClash = placement && clashIds.has(placement.id);
      const cellAttrs = `data-day="${day}" data-start="${slot.start}" data-end="${slot.end}"`;
      if (placement) {
        const room = placement.room ? esc(placement.room) : '';
        const stu = placement.available_capacity || '';
        const bg = courseColor(placement.course_code);
        const accent = courseColorBorder(placement.course_code);
        // Colour the card with the same palette used on the main page
        // (per-course pastel bg + darker accent stripe on the leading edge).
        const style = `background:${bg};border-left-color:${accent};color:#111827`;
        h += `<div class="cell filled${hasClash ? ' clash' : ''}" ${cellAttrs} data-placement-id="${placement.id}" draggable="true" style="${style}">`;
        h += `<span class="cid" style="color:#111827">${esc(placement.course_code)} ${esc(placement.section || '')}</span>`;
        h += `<span class="cmeta" style="color:#4b5563">${room}${stu ? '·' + stu : ''}</span>`;
        h += `</div>`;
      } else {
        h += `<div class="cell" ${cellAttrs}></div>`;
      }
    });
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
      });
    }
  });
}

/* ── Drag & drop handler — mirrors main-page onDrop semantics ── */
function findPlacement(placementId) {
  for (let i = 0; i < 4; i++) {
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
  if (payload.type !== 'move' || !payload.placement_id) return;

  // Capture old position BEFORE the mutation so undo can revert cleanly,
  // matching the main page's onDrop behaviour.
  const located = findPlacement(payload.placement_id);
  const oldDay = located ? located.placement.day : '';
  const oldStart = located ? located.placement.start_time : '';
  const oldEnd = located ? located.placement.end_time : '';

  const day = cell.dataset.day;
  const start = cell.dataset.start;
  const end = cell.dataset.end;
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
  for (let i = 0; i < 4; i++) {
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
}

/* ── Cross-pane sync-hover (direct DOM) ── */
function broadcastHover(sourcePaneIdx, day, start) {
  for (let i = 0; i < 4; i++) {
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
  S.layout = mode;
  const q = $('twsQuad');
  q.className = 'tws-quad layout-' + mode;
  document.querySelectorAll('#twsLayoutSwitch button').forEach(b => {
    b.classList.toggle('on', b.dataset.layout === mode);
  });
  $('twsStatusLayout').textContent =
    ({ single: 'Layout 1', vert: 'Layout 2×1', horz: 'Layout 1×2', quad: 'Layout 2×2' })[mode];
}
function maximisePane(idx) {
  setLayout('single');
  if (idx !== 0) {
    const tmp = S.panes[0]; S.panes[0] = S.panes[idx]; S.panes[idx] = tmp;
    renderSlotBar();
    for (let i = 0; i < 4; i++) renderPane(i);
  }
}

/* ── Presets ── */
function applyPreset(name) {
  if (!S.boards.length) return;
  if (name === 'first4') {
    for (let i = 0; i < 4; i++) S.panes[i].boardId = S.boards[i] ? S.boards[i].id : null;
  } else if (name === 'cross') {
    const sorted = [...S.boards].sort((a, b) => (b.critical || 0) - (a.critical || 0));
    for (let i = 0; i < 4; i++) S.panes[i].boardId = sorted[i] ? sorted[i].id : null;
  }
  for (const p of S.panes) { p.group = 0; p.boardData = null; }
  renderSlotBar();
  for (let i = 0; i < 4; i++) loadAndRenderPane(i);
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

async function doUndo() {
  const action = S.undoStack.pop();
  if (!action) return;
  if (action.type === 'move') {
    const data = await doMove(action.placement_id, action.old_day, action.old_start, action.old_end);
    if (!data) { S.undoStack.push(action); return; }
    S.redoStack.push(action);
    notify.success(IS_AR ? 'تم التراجع' : 'Undone');
  }
  updateUndoRedoButtons();
  // Reload any pane whose board shows this placement
  const located = findPlacement(action.placement_id);
  for (let i = 0; i < 4; i++) {
    if (located && S.panes[i].boardId === located.placement.board_id) {
      await loadAndRenderPane(i);
    }
  }
  // Simpler: reload all panes to guarantee consistency
  for (let i = 0; i < 4; i++) await loadAndRenderPane(i);
  await refreshBoardsSummary();
}

async function doRedo() {
  const action = S.redoStack.pop();
  if (!action) return;
  if (action.type === 'move') {
    const data = await doMove(action.placement_id, action.new_day, action.new_start, action.new_end);
    if (!data) { S.redoStack.push(action); return; }
    S.undoStack.push(action);
    notify.success(IS_AR ? 'تم الإعادة' : 'Redone');
  }
  updateUndoRedoButtons();
  for (let i = 0; i < 4; i++) await loadAndRenderPane(i);
  await refreshBoardsSummary();
}

/* ── Top-bar actions ── */
async function doOptimise() {
  if (!S.scenarioId) return;
  if (!confirm(T.optimiseConfirm)) return;
  const btn = $('twsOptimise');
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = IS_AR ? 'جاري التحسين…' : 'Optimising…';
  const data = await api(`/ops/tw/scenarios/${S.scenarioId}/optimise-v2/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: 'current' }),
  });
  btn.disabled = false;
  btn.textContent = origText;
  if (!data) return;
  await onScenarioChange();
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
  $('twsOptimise').addEventListener('click', doOptimise);
  $('twsPublish').addEventListener('click', doPublish);
  $('twsExport').addEventListener('click', doExport);
  $('twsUndo')?.addEventListener('click', doUndo);
  $('twsRedo')?.addEventListener('click', doRedo);
  updateUndoRedoButtons();
  $('twsClose').addEventListener('click', () => {
    if (window.history.length > 1) window.history.back();
    else window.location.href = '/timetable-workspace/';
  });
  document.querySelectorAll('#twsLayoutSwitch button').forEach(b => {
    b.addEventListener('click', () => setLayout(b.dataset.layout));
  });
  document.querySelectorAll('.tws-preset').forEach(b => {
    b.addEventListener('click', () => applyPreset(b.dataset.preset));
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { $('twsClose').click(); return; }
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
    if (e.key >= '1' && e.key <= '4') {
      const idx = parseInt(e.key) - 1;
      paneEl(idx)?.scrollIntoView({ block: 'center' });
    } else if (e.key === 'l' || e.key === 'L') {
      const modes = ['quad', 'vert', 'horz', 'single'];
      setLayout(modes[(modes.indexOf(S.layout) + 1) % modes.length]);
    }
  });
  for (let i = 0; i < 4; i++) renderPaneEmpty(i);
  loadScenarios();
})();

// Expose handlers used by inline onclick in the slot bar
window.setPaneBoard = setPaneBoard;
window.togglePaneOff = togglePaneOff;
