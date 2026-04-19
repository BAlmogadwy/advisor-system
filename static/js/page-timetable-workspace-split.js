/* ══════════════════════════════════════════════════════════════════
   Timetable Workspace — Split-Pane Host
   ────────────────────────────────────────────────────────────────
   Shell page that embeds four iframes of the main workspace in
   ?embed=1 mode. Delegates editing, drag, optimise, publish, export
   to the main page; coordinates scenario picking, per-pane board/
   group selection, layout and cross-pane sync at this level.
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
  reloaded: IS_AR ? 'تم التحديث' : 'Reloaded',
};

/* ── State ── */
const S = {
  scenarios: [],
  scenarioId: null,
  scenarioMeta: null,
  boards: [],
  panes: [
    { boardId: null, group: 0 },
    { boardId: null, group: 0 },
    { boardId: null, group: 0 },
    { boardId: null, group: 0 },
  ],
  layout: 'quad',
};

const POS_LABELS = ['Pane A', 'Pane B', 'Pane C', 'Pane D'];

/* ── API ── */
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
      alert(msg);
      return null;
    }
    return await r.json();
  } catch (e) {
    console.error(e);
    alert(e.message || String(e));
    return null;
  }
}

/* ── Scenario loading ── */
async function loadScenarios() {
  // Year/term are optional on the list endpoint; fetch everything when
  // defaults are unset (the server already orders by -created_at).
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
  // Auto-pick: prefer ?scenario= param, else first available
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
    clearAllPanes();
    return;
  }
  const data = await api(`/ops/tw/scenarios/${sid}/`);
  if (!data) return;
  S.scenarioId = data.scenario.id;
  S.scenarioMeta = data.scenario;
  // Scenario detail returns scenario only; boards with summary come from a
  // separate endpoint that includes placement counts, critical counts, etc.
  const bdata = await api(`/ops/tw/boards/?scenario_id=${sid}`);
  S.boards = (bdata && bdata.boards) || [];

  // Seed panes with first 4 boards (or initial board if passed)
  for (let i = 0; i < 4; i++) {
    S.panes[i].boardId = S.boards[i] ? S.boards[i].id : null;
    S.panes[i].group = 0;
  }
  if (INITIAL_BOARD) {
    const idx = S.boards.findIndex(b => String(b.id) === String(INITIAL_BOARD));
    if (idx >= 0) {
      S.panes[0].boardId = S.boards[idx].id;
    }
  }

  $('twsSub').textContent =
    `${data.scenario.name} · ${data.scenario.status.toUpperCase()} · ${S.boards.length} boards`;
  $('twsStBoards').textContent = S.boards.length;
  $('twsPublish').disabled = data.scenario.status === 'published';
  $('twsOptimise').disabled = false;
  $('twsExport').disabled = false;

  updateAggregateMetrics();
  renderSlotBar();
  renderAllPanes();
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
  if (!S.boards.length) { wrap.innerHTML = `<span class="lbl" style="color:var(--t4)">${T.noScenario}</span>`; return; }
  wrap.innerHTML = S.panes.map((p, i) => {
    const board = S.boards.find(b => b.id === p.boardId);
    const opts = ['<option value="">—</option>']
      .concat(S.boards.map(b => `<option value="${b.id}"${b.id === p.boardId ? ' selected' : ''}>${_esc(b.label)}</option>`))
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

function setPaneBoard(idx, boardId) {
  S.panes[idx].boardId = boardId ? parseInt(boardId) : null;
  renderPane(idx);
  renderSlotBar();
}
function togglePaneOff(idx) {
  if (S.panes[idx].boardId) {
    S.panes[idx].boardId = null;
  } else {
    // re-pick a sensible default
    const used = new Set(S.panes.filter(p => p.boardId).map(p => p.boardId));
    const nextBoard = S.boards.find(b => !used.has(b.id)) || S.boards[0];
    if (nextBoard) S.panes[idx].boardId = nextBoard.id;
  }
  renderPane(idx);
  renderSlotBar();
}

/* ── Pane iframe rendering ── */
function paneEl(idx) { return document.querySelector(`.tws-pane[data-idx="${idx}"]`); }

function renderPane(idx) {
  const el = paneEl(idx);
  if (!el) return;
  const p = S.panes[idx];
  const label = el.querySelector('[data-role="label"]');
  if (!p.boardId || !S.scenarioId) {
    // empty state
    const existingFrame = el.querySelector('iframe');
    if (existingFrame) existingFrame.remove();
    if (!el.querySelector('.empty-pane')) {
      const ph = document.createElement('div');
      ph.className = 'empty-pane';
      ph.textContent = T.selectBoard;
      el.appendChild(ph);
    }
    label.textContent = '—';
    return;
  }
  const board = S.boards.find(b => b.id === p.boardId);
  label.textContent = board ? board.label : '—';
  const url = `/timetable-workspace/?embed=1&scenario=${S.scenarioId}&board=${p.boardId}&_pane=${idx}`;
  const existingEmpty = el.querySelector('.empty-pane');
  if (existingEmpty) existingEmpty.remove();
  let frame = el.querySelector('iframe');
  if (!frame) {
    frame = document.createElement('iframe');
    frame.setAttribute('data-pane-idx', String(idx));
    el.appendChild(frame);
  }
  if (frame.src !== url && !frame.src.endsWith(url)) {
    frame.src = url;
  }
}

function renderAllPanes() {
  for (let i = 0; i < 4; i++) renderPane(i);
}
function clearAllPanes() {
  for (let i = 0; i < 4; i++) {
    const el = paneEl(i);
    const frame = el.querySelector('iframe');
    if (frame) frame.remove();
    el.querySelector('[data-role="label"]').textContent = '—';
    if (!el.querySelector('.empty-pane')) {
      const ph = document.createElement('div');
      ph.className = 'empty-pane';
      ph.textContent = T.selectBoard;
      el.appendChild(ph);
    }
  }
}

function reloadPane(idx) {
  const el = paneEl(idx);
  const frame = el && el.querySelector('iframe');
  if (frame) frame.src = frame.src;
}
function maximisePane(idx) {
  setLayout('single');
  // reorder so clicked pane is first (primary)
  // simplest: move its frame to primary by swapping panes[0] and panes[idx]
  if (idx !== 0) {
    const tmp = S.panes[0]; S.panes[0] = S.panes[idx]; S.panes[idx] = tmp;
    renderAllPanes();
    renderSlotBar();
  }
}

/* ── Layout ── */
function setLayout(mode) {
  S.layout = mode;
  const q = $('twsQuad');
  q.className = 'tws-quad layout-' + mode;
  document.querySelectorAll('#twsLayoutSwitch button').forEach(b => {
    b.classList.toggle('on', b.dataset.layout === mode);
  });
  $('twsStatusLayout').textContent = ({ single: 'Layout 1', vert: 'Layout 2×1', horz: 'Layout 1×2', quad: 'Layout 2×2' })[mode];
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
  renderSlotBar();
  renderAllPanes();
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
  // Refresh scenario meta (boards list, metrics) and reload every pane's iframe.
  await onScenarioChange();
  for (let i = 0; i < 4; i++) reloadPane(i);
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
  for (let i = 0; i < 4; i++) reloadPane(i);
}

function doExport() {
  if (!S.scenarioId) return;
  window.open(`/ops/tw/scenarios/${S.scenarioId}/export.xlsx`, '_blank');
}

/* ── postMessage bridge ── */
window.addEventListener('message', (e) => {
  if (e.origin !== window.location.origin) return;
  const msg = e.data;
  if (!msg || typeof msg !== 'object' || !msg.__tw) return;
  if (msg.type === 'tw:board-refreshed') {
    // Refresh aggregate metrics from the boards endpoint. Debounce so a
    // burst of mutations doesn't hammer the API.
    _scheduleStatsRefresh();
  } else if (msg.type === 'tw:cell-hover') {
    _broadcastSyncHover(msg.payload, msg.pane);
    $('twsStatusHover').textContent = msg.payload
      ? `Hovered ${msg.payload.day}/${msg.payload.start}`
      : 'Hovered —';
  }
});

let _statsRefreshT = null;
function _scheduleStatsRefresh() {
  if (_statsRefreshT) clearTimeout(_statsRefreshT);
  _statsRefreshT = setTimeout(async () => {
    if (!S.scenarioId) return;
    const bdata = await api(`/ops/tw/boards/?scenario_id=${S.scenarioId}`);
    if (bdata && bdata.boards) {
      S.boards = bdata.boards;
      updateAggregateMetrics();
    }
  }, 400);
}

function _broadcastSyncHover(payload, sourcePane) {
  document.querySelectorAll('.tws-pane iframe').forEach(f => {
    const idx = parseInt(f.getAttribute('data-pane-idx') || '-1');
    if (idx === sourcePane) return; // skip origin
    try {
      f.contentWindow.postMessage(
        { __tw: true, type: 'tw:sync-hover', payload },
        window.location.origin,
      );
    } catch (e) { /* ignore */ }
  });
}

/* ── Utils ── */
function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ── Init ── */
(function init() {
  $('twsScenario').addEventListener('change', onScenarioChange);
  $('twsOptimise').addEventListener('click', doOptimise);
  $('twsPublish').addEventListener('click', doPublish);
  $('twsExport').addEventListener('click', doExport);
  $('twsClose').addEventListener('click', () => {
    if (window.history.length > 1) {
      window.history.back();
    } else {
      window.location.href = '/timetable-workspace/';
    }
  });

  document.querySelectorAll('#twsLayoutSwitch button').forEach(b => {
    b.addEventListener('click', () => setLayout(b.dataset.layout));
  });

  document.querySelectorAll('.tws-preset').forEach(b => {
    b.addEventListener('click', () => applyPreset(b.dataset.preset));
  });

  document.querySelectorAll('.tws-pane .pane-hd button').forEach(btn => {
    const action = btn.dataset.action;
    btn.addEventListener('click', () => {
      const pane = btn.closest('.tws-pane');
      const idx = parseInt(pane.dataset.idx);
      if (action === 'reload') reloadPane(idx);
      else if (action === 'maximise') maximisePane(idx);
    });
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      $('twsClose').click();
    } else if (e.key >= '1' && e.key <= '4') {
      const idx = parseInt(e.key) - 1;
      const pane = paneEl(idx);
      if (pane) pane.querySelector('iframe')?.focus();
    } else if (e.key === 'l' || e.key === 'L') {
      const modes = ['quad', 'vert', 'horz', 'single'];
      setLayout(modes[(modes.indexOf(S.layout) + 1) % modes.length]);
    }
  });

  loadScenarios();
})();

// Expose handlers used by inline onclick in the slot bar
window.setPaneBoard = setPaneBoard;
window.togglePaneOff = togglePaneOff;
