(function () {
  document.body.classList.add('twm-root');

  const $ = id => document.getElementById(id);
  const state = {
    scenarioId: initialScenario || '',
    boardId: initialBoard || '',
    mode: 'system',
    scenario: null,
    boards: [],
    boardDetails: new Map(),
    conflicts: new Map(),
    capacities: new Map(),
    budget: [],
    graphFocusBoardId: '',
    graphFocusGroupKey: '',
    graphExpandedBoardIds: new Set(),
    graphHitNodes: [],
    graphPositions: new Map(),
    graphDragging: null,
    graphDidDrag: false,
    graphLens: 'pressure',
    graphGroupLimit: '6',
    graphLabels: 'focus',
    genomeFilter: 'all',
    buildPreviewId: '',
    movePlacementId: '',
    moveSlot: null,
    moveSlotFilter: 'all',
    moveMessage: '',
    moveRepairTrail: [],
    moveUndo: null,
  };

  const toneFor = pressure => pressure >= 70 ? 'critical' : pressure >= 35 ? 'watch' : 'stable';
  const DAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU'];

  async function api(url, options = {}) {
    const headers = { 'X-CSRFToken': djCsrfToken, ...(options.headers || {}) };
    const res = await fetch(url, { ...options, headers });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[ch]));
  }

  function pct(value, max) {
    if (!max || max <= 0) return 0;
    return Math.max(2, Math.min(100, Math.round((value / max) * 100)));
  }

  function num(value) {
    return Number(value || 0).toLocaleString('en-US');
  }

  function plural(value, singular, pluralLabel = `${singular}s`) {
    return `${num(value)} ${Number(value || 0) === 1 ? singular : pluralLabel}`;
  }

  function currentUrl(path, includeBoard = true) {
    const params = new URLSearchParams();
    if (state.scenarioId) params.set('scenario', state.scenarioId);
    if (includeBoard && state.boardId) params.set('board', state.boardId);
    return `${path}${params.toString() ? '?' + params.toString() : ''}`;
  }

  function toMinutes(time) {
    const [hh, mm] = String(time || '0:0').split(':').map(Number);
    return (hh || 0) * 60 + (mm || 0);
  }

  function durationMinutes(start, end) {
    return Math.max(0, toMinutes(end) - toMinutes(start));
  }

  function overlaps(aStart, aEnd, bStart, bEnd) {
    return toMinutes(aStart) < toMinutes(bEnd) && toMinutes(bStart) < toMinutes(aEnd);
  }

  function sameSlot(a, b) {
    return String(a.day) === String(b.day) &&
      String(a.start) === String(b.start_time || b.start) &&
      String(a.end) === String(b.end_time || b.end);
  }

  function boardStudents(board) {
    return (board.primary_count || 0) + (board.visitor_count || 0);
  }

  function pressureFor(scan) {
    return Math.min(100, Math.round(
      scan.overlaps * 26 +
      scan.instructors * 18 +
      scan.rooms * 10 +
      scan.crossBoard * 4 +
      scan.deficitCourses * 7 +
      Math.min(24, Math.ceil(scan.affected / 4)) +
      scan.blocked * 2
    ));
  }

  function scanBoard(board) {
    const conflicts = state.conflicts.get(board.id) || {};
    const capacity = state.capacities.get(board.id) || {};
    const capCourses = capacity.courses || [];
    const deficitCourses = capCourses.filter(c => (c.deficit || 0) > 0);
    const impact = conflicts.student_impact || {};
    const scan = {
      board,
      students: boardStudents(board),
      placements: board.placement_count || 0,
      overlaps: (conflicts.overlaps || []).length,
      instructors: (conflicts.instructor_clashes || []).length,
      rooms: (conflicts.room_clashes || []).length,
      crossBoard: (conflicts.cross_board_conflicts || []).length,
      affected: impact.affected_count || 0,
      blocked: impact.blocked_count || 0,
      deficit: (capacity.totals || {}).deficit || 0,
      demand: (capacity.totals || {}).demand || 0,
      deficitCourses: deficitCourses.length,
      conflicts,
      capacity,
      impact,
    };
    scan.pressure = pressureFor(scan);
    scan.tone = toneFor(scan.pressure);
    return scan;
  }

  function currentScans() {
    const scans = state.boards.map(scanBoard);
    if (state.boardId) return scans.filter(s => String(s.board.id) === String(state.boardId));
    return scans;
  }

  function aggregateScan(scans) {
    return scans.reduce((acc, s) => {
      acc.students += s.students;
      acc.placements += s.placements;
      acc.overlaps += s.overlaps;
      acc.instructors += s.instructors;
      acc.rooms += s.rooms;
      acc.crossBoard += s.crossBoard;
      acc.affected += s.affected;
      acc.blocked += s.blocked;
      acc.deficit += s.deficit;
      acc.demand += s.demand;
      acc.deficitCourses += s.deficitCourses;
      acc.pressure = Math.max(acc.pressure, s.pressure);
      return acc;
    }, {
      students: 0,
      placements: 0,
      overlaps: 0,
      instructors: 0,
      rooms: 0,
      crossBoard: 0,
      affected: 0,
      blocked: 0,
      deficit: 0,
      demand: 0,
      deficitCourses: 0,
      pressure: 0,
    });
  }

  async function loadScenarios() {
    const year = $('twmYear').value.trim();
    const term = $('twmTerm').value.trim();
    const data = await api(`/ops/tw/scenarios/?year=${encodeURIComponent(year)}&term=${encodeURIComponent(term)}`);
    const select = $('twmScenario');
    const scenarios = data.scenarios || [];
    select.innerHTML = '<option value="">Select Scenario</option>';
    scenarios.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = `${s.name} (${s.status})`;
      select.appendChild(opt);
    });
    if (state.scenarioId && scenarios.some(s => String(s.id) === String(state.scenarioId))) {
      select.value = state.scenarioId;
      await loadScenario(state.scenarioId);
    } else if (scenarios.length) {
      state.scenarioId = String(scenarios[0].id);
      select.value = state.scenarioId;
      await loadScenario(state.scenarioId);
    } else {
      renderEmpty('No scenarios found for this year/term.');
    }
  }

  async function loadScenario(id) {
    state.scenarioId = String(id || '');
    state.boardId = '';
    state.scenario = null;
    state.boards = [];
    state.boardDetails.clear();
    state.conflicts.clear();
    state.capacities.clear();
    state.budget = [];
    state.graphFocusBoardId = '';
    state.graphFocusGroupKey = '';
    state.graphExpandedBoardIds.clear();
    state.graphHitNodes = [];
    state.graphPositions.clear();
    state.graphDragging = null;
    state.graphDidDrag = false;
    state.genomeFilter = 'all';
    state.buildPreviewId = '';
    state.movePlacementId = '';
    state.moveSlot = null;
    state.moveSlotFilter = 'all';
    state.moveMessage = '';
    state.moveRepairTrail = [];
    state.moveUndo = null;
    if (!state.scenarioId) {
      renderEmpty('Select a scenario to scan.');
      return;
    }

    const [scenarioData, boardsData, budgetData] = await Promise.all([
      api(`/ops/tw/scenarios/${state.scenarioId}/`),
      api(`/ops/tw/boards/?scenario_id=${state.scenarioId}`),
      api(`/ops/tw/scenarios/${state.scenarioId}/budget/`),
    ]);
    state.scenario = scenarioData.scenario;
    state.boards = boardsData.boards || [];
    state.budget = budgetData.budget || [];

    const boardSelect = $('twmBoard');
    boardSelect.innerHTML = '<option value="">All boards</option>';
    state.boards.forEach(b => {
      const opt = document.createElement('option');
      opt.value = b.id;
      opt.textContent = `${b.label} (${boardStudents(b)} students)`;
      boardSelect.appendChild(opt);
    });
    if (initialBoard && state.boards.some(b => String(b.id) === String(initialBoard))) {
      state.boardId = String(initialBoard);
      boardSelect.value = state.boardId;
    }

    await Promise.all(state.boards.map(async board => {
      const [detail, conflicts, capacity] = await Promise.all([
        api(`/ops/tw/boards/${board.id}/`),
        api(`/ops/tw/boards/${board.id}/conflicts/`),
        api(`/ops/tw/boards/${board.id}/capacity/`),
      ]);
      state.boardDetails.set(board.id, detail);
      state.conflicts.set(board.id, conflicts);
      state.capacities.set(board.id, capacity);
    }));
    render();
  }

  function renderEmpty(message) {
    $('twmPressure').textContent = '0';
    $('twmScanTitle').textContent = 'No scan';
    $('twmScanSubtitle').textContent = message;
    const pressureExplain = $('twmPressureExplain');
    if (pressureExplain) pressureExplain.innerHTML = '';
    const handoff = $('twmHandoff');
    if (handoff) handoff.innerHTML = '';
    const topSurgery = $('twmTopSurgery');
    if (topSurgery) topSurgery.innerHTML = '';
    $('twmMetrics').innerHTML = '';
    $('twmBoardMap').innerHTML = `<div class="twm-empty">${esc(message)}</div>`;
    $('twmLayers').innerHTML = '';
    $('twmHotspots').innerHTML = '';
    $('twmStudentSlices').innerHTML = '';
    const buildMoves = $('twmBuildMoves');
    if (buildMoves) buildMoves.innerHTML = '';
    const buildStatus = $('twmBuildStatus');
    if (buildStatus) buildStatus.textContent = 'No moves';
    const detail = $('twmGraphDetail');
    if (detail) detail.innerHTML = `<strong>Graph drill-down</strong><span>${esc(message)}</span>`;
    drawFlow([]);
  }

  function render() {
    const scans = currentScans();
    const aggregate = aggregateScan(scans);
    const tone = toneFor(aggregate.pressure);
    $('twmScoreCard').className = `twm-score-card ${tone}`;
    $('twmPressure').textContent = aggregate.pressure;
    $('twmScanTitle').textContent = state.boardId
      ? (scans[0]?.board.label || 'Board scan')
      : (state.scenario?.name || 'Scenario scan');
    $('twmScanSubtitle').textContent = state.boardId
      ? `${num(aggregate.students)} students, ${num(aggregate.placements)} placed sections on this board`
      : `${state.boards.length} boards scanned as one diagnostic twin`;

    renderPressureExplain(aggregate, scans);
    renderHandoff();
    renderTopSurgery(scans);
    renderMetrics(aggregate);
    renderBoardMap();
    renderLayers(aggregate, scans);
    renderHotspots(scans);
    renderGenome(scans);
    renderGroups(scans);
    renderStudentSlices(scans);
    renderInterventions(scans);
    renderBuild(scans);
    $('twmFlowStatus').textContent = `${num(aggregate.affected)} affected`;
    updateGraphLabelButton();
    drawFlow(scans);
    renderGraphDetail(scans);
    applyMode();
    updateBackLink();
  }

  function renderMetrics(scan) {
    const metrics = [
      ['Students', scan.students],
      ['Placed', scan.placements],
      ['Critical', scan.overlaps + scan.instructors],
      ['Cross-board', scan.crossBoard],
      ['Affected', scan.affected],
      ['Seat gap', scan.deficit],
    ];
    $('twmMetrics').innerHTML = metrics.map(([label, value]) => `
      <div class="twm-metric">
        <strong>${num(value)}</strong>
        <span>${esc(label)}</span>
      </div>
    `).join('');
  }

  function pressureComponents(scan) {
    const rows = [
      {
        label: 'Time collisions',
        score: scan.overlaps * 26,
        detail: `${plural(scan.overlaps, 'overlap')} detected`,
      },
      {
        label: 'Instructor pressure',
        score: scan.instructors * 18,
        detail: `${plural(scan.instructors, 'instructor clash', 'instructor clashes')}`,
      },
      {
        label: 'Room pressure',
        score: scan.rooms * 10,
        detail: `${plural(scan.rooms, 'room clash', 'room clashes')}`,
      },
      {
        label: 'Cross-board network',
        score: scan.crossBoard * 4,
        detail: `${plural(scan.crossBoard, 'shared-student link')}`,
      },
      {
        label: 'Capacity gap',
        score: scan.deficitCourses * 7,
        detail: `${plural(scan.deficitCourses, 'course')} under capacity`,
      },
      {
        label: 'Student harm',
        score: Math.min(24, Math.ceil(scan.affected / 4)) + scan.blocked * 2,
        detail: `${plural(scan.affected, 'affected student')} | ${plural(scan.blocked, 'blocked route')}`,
      },
    ];
    return rows
      .map(row => ({ ...row, score: Math.max(0, Math.round(row.score || 0)) }))
      .sort((a, b) => b.score - a.score);
  }

  function capacityCourseDiagnosis(course) {
    const demand = course.demand || 0;
    const seats = course.raw_capacity || 0;
    const deficit = course.deficit || 0;
    if (demand > 0 && seats <= 0) {
      return {
        tone: 'critical',
        label: 'No seats recorded',
        detail: `${num(demand)} demand has no usable room capacity source.`,
      };
    }
    if (deficit > 0) {
      return {
        tone: 'watch',
        label: 'Demand exceeds seats',
        detail: `${num(demand)} demand / ${num(seats)} seats / ${num(deficit)} short.`,
      };
    }
    return {
      tone: 'stable',
      label: 'Capacity stable',
      detail: `${num(demand)} demand / ${num(seats)} seats.`,
    };
  }

  function capacityDiagnosis(scans) {
    const courses = [];
    scans.forEach(scan => {
      ((scan.capacity || {}).courses || []).forEach(course => {
        if (!((course.deficit || 0) > 0)) return;
        courses.push({
          ...course,
          boardLabel: scan.board.label,
          diagnosis: capacityCourseDiagnosis(course),
        });
      });
    });
    if (!courses.length) {
      return {
        tone: 'stable',
        label: 'Capacity stable',
        detail: 'No course-board seat gap detected in this scan.',
      };
    }
    courses.sort((a, b) =>
      (b.diagnosis.tone === 'critical' ? 1 : 0) - (a.diagnosis.tone === 'critical' ? 1 : 0) ||
      (b.deficit || 0) - (a.deficit || 0));
    const top = courses[0];
    const zeroSeatCount = courses.filter(course => course.diagnosis.tone === 'critical').length;
    return {
      tone: top.diagnosis.tone,
      label: zeroSeatCount ? 'Capacity source issue' : 'Demand exceeds seats',
      detail: zeroSeatCount
        ? `${plural(zeroSeatCount, 'course-board row')} ${zeroSeatCount === 1 ? 'has' : 'have'} demand but no seats. Top: ${top.course_code} on ${top.boardLabel}.`
        : `${top.course_code} on ${top.boardLabel}: ${top.diagnosis.detail}`,
      top,
    };
  }

  function renderPressureExplain(scan, scans) {
    const target = $('twmPressureExplain');
    if (!target) return;
    const components = pressureComponents(scan);
    const raw = components.reduce((sum, row) => sum + row.score, 0);
    const active = components.filter(row => row.score > 0).slice(0, 4);
    const capacity = capacityDiagnosis(scans);
    target.innerHTML = `
      <div class="twm-pressure-note ${esc(capacity.tone)}">
        <b>${scan.pressure >= 100 ? `Capped at 100 from ${num(raw)} raw pressure` : `${num(raw)} raw pressure`}</b>
        <span>${esc(capacity.label)} | ${esc(capacity.detail)}</span>
      </div>
      <div class="twm-pressure-bars">
        ${active.length ? active.map(row => `
          <span>
            <b>${esc(row.label)}</b>
            <i style="width:${pct(row.score, Math.max(1, raw))}%"></i>
            <em>${num(row.score)} pts | ${esc(row.detail)}</em>
          </span>
        `).join('') : '<span><b>No active pressure</b><i style="width:2%"></i><em>The selected scope has no MRI pressure evidence.</em></span>'}
      </div>
    `;
  }

  function renderHandoff() {
    const target = $('twmHandoff');
    if (!target) return;
    if (!state.scenarioId) {
      target.innerHTML = '';
      return;
    }
    target.innerHTML = `
      <a href="${esc(currentUrl('/timetable-workspace/'))}">Open Workspace</a>
      <a href="${esc(currentUrl('/timetable-workspace/split/'))}">Open Split</a>
      <a href="${esc(currentUrl('/timetable-workspace/graph/', false))}">Open Graph</a>
      <a href="/ops/tw/scenarios/${esc(state.scenarioId)}/export.xlsx">Export XLSX</a>
    `;
  }

  function topSurgeryFor(scans) {
    const capacity = capacityDiagnosis(scans);
    const repair = buildRepairQueue(scans)[0];
    if (repair) {
      const first = repair.evidence[0] || {};
      return {
        tone: repair.tone,
        title: `Repair ${placementLabel(repair.placement)} first`,
        detail: `${repair.board.label}: ${first.title || 'MRI pressure'} | ${first.detail || repair.slotSummary.verdict}`,
        before: `${repair.critical} red / ${repair.warning} amber`,
        after: repair.slotSummary.verdict,
        action: 'Start Repair',
        placementId: String(repair.placement.id),
      };
    }
    if (capacity.tone !== 'stable') {
      return {
        tone: capacity.tone,
        title: capacity.label,
        detail: capacity.detail,
        before: capacity.top ? `${capacity.top.course_code} deficit ${num(capacity.top.deficit)}` : 'Capacity gap',
        after: 'Fix seats source before rebuilding',
        action: 'Review Actions',
        mode: 'actions',
      };
    }
    const move = buildSuggestions(scans)[0];
    if (move) {
      return {
        tone: move.tone,
        title: move.title,
        detail: move.detail,
        before: move.before,
        after: move.after,
        action: move.command || 'Preview',
        previewId: move.id,
      };
    }
    return {
      tone: 'stable',
      title: 'No surgery required',
      detail: 'The selected MRI scope has no damaged placed section, no suggested rebuild move, and no capacity gap.',
      before: 'Clean',
      after: 'Monitor only',
      action: 'Review System',
      mode: 'system',
    };
  }

  function renderTopSurgery(scans) {
    const target = $('twmTopSurgery');
    if (!target) return;
    const item = topSurgeryFor(scans);
    target.className = `twm-top-surgery ${item.tone || 'stable'}`;
    target.innerHTML = `
      <div class="twm-top-surgery-copy">
        <span>Top surgery</span>
        <strong>${esc(item.title)}</strong>
        <em>${esc(item.detail)}</em>
      </div>
      <div class="twm-top-surgery-delta">
        <span><b>Before</b>${esc(item.before)}</span>
        <span><b>Target</b>${esc(item.after)}</span>
      </div>
      <button type="button"
        ${item.placementId ? `data-twm-top-repair="${esc(item.placementId)}"` : ''}
        ${item.previewId ? `data-twm-top-preview="${esc(item.previewId)}"` : ''}
        ${item.mode ? `data-twm-top-mode="${esc(item.mode)}"` : ''}>
        ${esc(item.action)}
      </button>
    `;
    const btn = target.querySelector('button');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const repairId = btn.dataset.twmTopRepair || '';
      if (repairId) {
        startRepairSelection(currentScans(), repairId, 'Top surgery selected the highest-value repair. Candidate slots are ready.');
        return;
      }
      const previewId = btn.dataset.twmTopPreview || '';
      if (previewId) {
        state.buildPreviewId = previewId;
        applyBuildPreviewFocus();
        state.mode = 'build';
        applyMode({ scroll: true });
        refreshGraphOnly();
        return;
      }
      state.mode = btn.dataset.twmTopMode || 'system';
      applyMode({ scroll: true });
    });
  }

  function renderBoardMap() {
    $('twmBoardCount').textContent = `${state.boards.length} boards`;
    $('twmBoardMap').innerHTML = state.boards.map(board => {
      const scan = scanBoard(board);
      const active = String(board.id) === String(state.boardId);
      return `
        <button class="twm-board-node ${scan.tone}${active ? ' active' : ''}" data-board-id="${board.id}" type="button">
          <span class="twm-node-ring" style="--p:${scan.pressure}%">${scan.pressure}</span>
          <span>
            <strong>${esc(board.label)}</strong>
            <em>${scan.students} students | ${scan.placements} placed | ${scan.crossBoard} links</em>
          </span>
        </button>
      `;
    }).join('');
    document.querySelectorAll('.twm-board-node').forEach(btn => {
      btn.addEventListener('click', () => {
        state.boardId = String(btn.dataset.boardId || '');
        state.graphFocusBoardId = state.boardId;
        state.graphFocusGroupKey = '';
        setSingleGraphExpansion(state.boardId);
        $('twmBoard').value = state.boardId;
        render();
      });
    });
  }

  function renderLayers(scan, scans) {
    const boardStudentCounts = state.boards.map(boardStudents);
    const avg = boardStudentCounts.length
      ? Math.round(boardStudentCounts.reduce((sum, n) => sum + n, 0) / boardStudentCounts.length)
      : 0;
    const currentDelta = scans.length === 1 ? Math.abs(scans[0].students - avg) : 0;
    const capacityDx = capacityDiagnosis(scans);
    const layers = [
      ['Student flow', scan.affected, Math.max(1, scan.students), `${scan.affected} affected, ${scan.blocked} blocked`, scan.blocked ? 'critical' : scan.affected ? 'watch' : 'stable'],
      ['Time layer', scan.overlaps, Math.max(1, scan.placements), `${scan.overlaps} time overlaps`, scan.overlaps ? 'critical' : 'stable'],
      ['Cross-board network', scan.crossBoard, Math.max(1, state.boards.length * 8), `${scan.crossBoard} shared-student links`, scan.crossBoard ? 'watch' : 'stable'],
      ['Capacity layer', scan.deficit, Math.max(1, scan.demand), `${scan.deficitCourses} courses under capacity | ${capacityDx.label}`, scan.deficit ? capacityDx.tone : 'stable'],
      ['Room/instructor layer', scan.rooms + scan.instructors, Math.max(1, scan.placements), `${scan.instructors} instructor, ${scan.rooms} room`, scan.instructors ? 'critical' : scan.rooms ? 'watch' : 'stable'],
      ['Term/board layer', currentDelta, Math.max(1, avg), scans.length === 1 ? `${currentDelta} students from board average` : `${state.boards.length} boards compared`, currentDelta > Math.max(15, avg * 0.35) ? 'watch' : 'stable'],
    ];
    $('twmLayerStatus').textContent = `${layers.filter(l => l[4] !== 'stable').length} active layers`;
    $('twmLayers').innerHTML = layers.map(([label, value, max, detail, tone]) => `
      <div class="twm-layer ${tone}">
        <div>
          <strong>${esc(label)}</strong>
          <span>${esc(detail)}</span>
        </div>
        <div class="twm-layer-bar"><i style="width:${pct(value, max)}%"></i></div>
      </div>
    `).join('');
  }

  function collectHotspots(scans) {
    const grouped = new Map();
    const add = item => {
      const key = item.key || `${item.kind}:${item.title}:${item.detail}`;
      const existing = grouped.get(key);
      if (!existing) {
        grouped.set(key, {
          ...item,
          count: 1,
          score: item.score || 0,
          maxStudents: item.students || 0,
          boards: new Set(item.boards || []),
          placementIds: new Set(item.placementIds || []),
        });
        return;
      }
      existing.count += 1;
      existing.score += item.score || 0;
      existing.maxStudents = Math.max(existing.maxStudents || 0, item.students || 0);
      (item.boards || []).forEach(board => existing.boards.add(board));
      (item.placementIds || []).forEach(id => existing.placementIds.add(String(id)));
    };
    scans.forEach(scan => {
      const board = scan.board;
      (scan.conflicts.overlaps || []).forEach(o => add({
        key: `time:${board.id}:${(o.ids || []).slice().sort().join(',') || (o.sections || []).join('|')}`,
        tone: 'critical',
        kind: 'Time overlap',
        title: (o.sections || []).join(' / '),
        detail: `${board.label} | ${o.detail || 'same time collision'}`,
        score: 120,
        boards: [String(board.id)],
        placementIds: (o.ids || []).map(String),
      }));
      (scan.conflicts.cross_board_conflicts || []).forEach(c => {
        const sections = [c.section_a, c.section_b].sort();
        const boards = [String(c.board_a_id || ''), String(c.board_b_id || '')].sort();
        add({
          key: `cross:${sections.join('|')}:${boards.join('|')}`,
          tone: 'watch',
          kind: 'Cross-board',
          title: `${c.section_a} <-> ${c.section_b}`,
          detail: `${c.overlap_count || 0} shared students | ${c.board_a_label} <-> ${c.board_b_label}`,
          students: c.overlap_count || 0,
          score: 40 + (c.overlap_count || 0),
          boards: [String(c.board_a_id || board.id), String(c.board_b_id || '')].filter(Boolean),
        });
      });
      ((scan.capacity.courses || []).filter(c => (c.deficit || 0) > 0)).forEach(c => {
        const diagnosis = capacityCourseDiagnosis(c);
        add({
          key: `capacity:${board.id}:${c.course_code}`,
          tone: diagnosis.tone,
          kind: 'Capacity',
          title: c.course_code,
          detail: `${diagnosis.label} | ${board.label} | ${diagnosis.detail}`,
          score: 35 + (c.deficit || 0) + (diagnosis.tone === 'critical' ? 70 : 0),
          boards: [String(board.id)],
        });
      });
    });
    return Array.from(grouped.values()).map(item => ({
      ...item,
      boards: Array.from(item.boards || []),
      placementIds: Array.from(item.placementIds || []),
      detail: item.kind === 'Cross-board' && item.count > 1
        ? `${num(item.maxStudents || 0)} peak shared students | ${plural(item.count, 'meeting link')} | ${item.detail.split('|').slice(-1)[0].trim()}`
        : item.detail,
    })).sort((a, b) =>
      (b.tone === 'critical' ? 1 : 0) - (a.tone === 'critical' ? 1 : 0) ||
      (b.score || 0) - (a.score || 0) ||
      (b.count || 0) - (a.count || 0)).slice(0, 24);
  }

  function renderHotspots(scans) {
    const hotspots = collectHotspots(scans);
    $('twmHotspotCount').textContent = hotspots.length;
    $('twmHotspots').innerHTML = hotspots.length ? hotspots.map(h => `
      <button class="twm-hotspot ${h.tone}" type="button"
        data-twm-hotspot-board="${esc((h.boards || [])[0] || '')}"
        data-twm-hotspot-placement="${esc((h.placementIds || [])[0] || '')}">
        <span>${esc(h.kind)}</span>
        <strong>${esc(h.title || '-')}</strong>
        ${h.count > 1 ? `<small>${plural(h.count, 'signal')} grouped</small>` : ''}
        <em>${esc(h.detail || '')}</em>
      </button>
    `).join('') : '<div class="twm-empty">No active hot spots in this scan.</div>';
    document.querySelectorAll('.twm-hotspot[data-twm-hotspot-board]').forEach(btn => {
      btn.addEventListener('click', () => {
        const scans = currentScans();
        const placementId = btn.dataset.twmHotspotPlacement || '';
        const placement = findPlacementById(scans, placementId);
        if (placement) {
          focusPlacementGroup(placement);
          state.movePlacementId = placementId;
          state.moveSlot = null;
          state.moveSlotFilter = 'all';
          state.moveMessage = `Hot spot selected ${placementLabel(placement)}. Move Section is ready.`;
          state.mode = 'build';
          refreshGraphOnly();
          applyMode({ scroll: true });
          return;
        }
        const boardId = btn.dataset.twmHotspotBoard || '';
        if (boardId) {
          state.graphFocusBoardId = boardId;
          state.graphFocusGroupKey = '';
          state.graphExpandedBoardIds.add(boardId);
          refreshGraphOnly();
          document.getElementById('twmSectionStudents')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    });
  }

  function renderStudentSlices(scans) {
    const slices = [];
    scans.forEach(scan => {
      (scan.impact.overlap_details || []).forEach(d => slices.push({ board: scan.board.label, ...d }));
    });
    $('twmStudentStatus').textContent = slices.length
      ? `${slices.reduce((sum, s) => sum + (s.affected || 0), 0)} affected students`
      : 'No affected students';
    $('twmStudentSlices').innerHTML = slices.length ? slices.slice(0, 16).map(s => `
      <div class="twm-student-slice ${s.blocked ? 'critical' : 'stable'}">
        <div>
          <strong>${esc((s.courses || []).join(' / '))}</strong>
          <span>${s.affected || 0} affected</span>
        </div>
        <em>${esc(s.board)} | ${s.resolvable ? 'alternative route exists' : 'no clean route found'}</em>
        <small>${(s.students || []).length ? `sample: ${esc((s.students || []).join(', '))}` : 'no student sample'}</small>
      </div>
    `).join('') : '<div class="twm-empty">No student overlap slices in this scan.</div>';
  }

  function placementEvidence(scan, placement) {
    const id = String(placement.id);
    const sectionKey = `${placement.course_code}-${placement.section}`;
    const rows = [];

    (scan.conflicts.overlaps || []).forEach(item => {
      if (!(item.ids || []).some(other => String(other) === id)) return;
      rows.push({
        tone: 'critical',
        title: 'Time overlap',
        detail: `${(item.sections || []).join(' / ') || placementLabel(placement)} | ${item.detail || 'same time collision'}`,
      });
    });
    (scan.conflicts.instructor_clashes || []).forEach(item => {
      if (!(item.ids || []).some(other => String(other) === id)) return;
      rows.push({
        tone: 'watch',
        title: 'Instructor clash',
        detail: `${item.instructor || placementInstructor(placement) || 'Instructor'} | ${item.detail || 'same instructor pressure'}`,
      });
    });
    (scan.conflicts.room_clashes || []).forEach(item => {
      if (!(item.ids || []).some(other => String(other) === id)) return;
      rows.push({
        tone: 'watch',
        title: 'Room clash',
        detail: `${item.room || placement.room || 'Room'} | ${item.detail || 'same room pressure'}`,
      });
    });
    (scan.conflicts.cross_board_conflicts || []).forEach(item => {
      if (item.section_a !== sectionKey && item.section_b !== sectionKey) return;
      rows.push({
        tone: 'watch',
        title: 'Cross-board students',
        detail: `${item.overlap_count || 0} shared students | ${item.board_a_label || ''} <-> ${item.board_b_label || ''}`,
      });
    });
    (scan.capacity.courses || []).forEach(item => {
      if (item.course_code !== placement.course_code || !(item.deficit > 0)) return;
      const diagnosis = capacityCourseDiagnosis(item);
      rows.push({
        tone: diagnosis.tone,
        title: 'Seat pressure',
        detail: `${diagnosis.label} | ${diagnosis.detail}`,
      });
    });

    return rows;
  }

  function placementTone(scan, placement) {
    const evidence = placementEvidence(scan, placement);
    if (evidence.some(item => item.tone === 'critical')) return 'critical';
    return evidence.length ? 'watch' : 'stable';
  }

  function renderGeneInspector(scans) {
    const selected = findMovePlacement(scans);
    if (!selected) return '';
    const scan = scans.find(item => String(item.board.id) === String(selected.board_id));
    if (!scan) return '';
    const evidence = placementEvidence(scan, selected);
    const tone = evidence.some(item => item.tone === 'critical') ? 'critical' : evidence.length ? 'watch' : 'stable';
    return `
      <div class="twm-gene-inspector ${tone}">
        <div class="twm-gene-inspector-head">
          <span>Selected gene</span>
          <strong>${esc(placementLabel(selected))}</strong>
          <em>${esc(scan.board.label)} | ${esc(selected.day)} ${esc(selected.start_time)}-${esc(selected.end_time)}</em>
        </div>
        <div class="twm-gene-grid">
          <span><b>Instructor</b>${esc(placementInstructor(selected) || 'Unassigned')}</span>
          <span><b>Room</b>${esc(selected.room || 'No room')}</span>
          <span><b>Section</b>${esc(selected.section || 'S1')}</span>
          <span><b>Repair</b>Move Section opened</span>
        </div>
        <div class="twm-gene-evidence">
          ${evidence.length ? evidence.map(item => `
            <span class="${esc(item.tone)}"><b>${esc(item.title)}</b>${esc(item.detail)}</span>
          `).join('') : '<span class="stable"><b>Clean gene</b>No direct MRI damage detected for this placed section.</span>'}
        </div>
      </div>
    `;
  }

  function renderGenome(scans) {
    const genes = [];
    scans.forEach(scan => {
      const detail = state.boardDetails.get(scan.board.id) || {};
      (detail.placements || []).forEach(p => {
        genes.push({
          id: p.id,
          code: p.course_code,
          section: p.section,
          board: scan.board.label,
          day: p.day,
          time: p.start_time,
          end: p.end_time,
          room: p.room || '',
          instructor: placementInstructor(p),
          tone: placementTone(scan, p),
        });
      });
    });
    const counts = {
      all: genes.length,
      damage: genes.filter(g => g.tone !== 'stable').length,
      critical: genes.filter(g => g.tone === 'critical').length,
      watch: genes.filter(g => g.tone === 'watch').length,
      stable: genes.filter(g => g.tone === 'stable').length,
    };
    const filter = state.genomeFilter || 'all';
    const visibleGenes = genes.filter(g => (
      filter === 'all' ||
      (filter === 'damage' && g.tone !== 'stable') ||
      g.tone === filter
    ));
    const filterLabel = ({
      all: 'all',
      damage: 'damaged',
      critical: 'red',
      watch: 'amber',
      stable: 'clean',
    })[filter] || 'all';
    $('twmGenomeStatus').textContent = filter === 'all'
      ? `${genes.length} section genes`
      : `${visibleGenes.length}/${genes.length} ${filterLabel} genes`;
    $('twmGenome').innerHTML = genes.length ? `
      <div class="twm-genome-tools" aria-label="DNA triage filters">
        ${[
          ['all', 'All', counts.all],
          ['damage', 'Damaged', counts.damage],
          ['critical', 'Red', counts.critical],
          ['watch', 'Amber', counts.watch],
          ['stable', 'Clean', counts.stable],
        ].map(([value, label, count]) => `
          <button class="${filter === value ? 'active' : ''}" type="button" data-twm-genome-filter="${value}">
            <span>${label}</span><b>${count}</b>
          </button>
        `).join('')}
      </div>
      <div class="twm-genome-strip">
        ${visibleGenes.map(g => `
          <button class="twm-gene ${g.tone}${String(g.id) === String(state.movePlacementId) ? ' active' : ''}" type="button"
            data-twm-gene-placement="${esc(g.id)}"
            title="${esc(g.code)} ${esc(g.section)} | ${esc(g.board)} | ${esc(g.day)} ${esc(g.time)}-${esc(g.end)} | ${esc(g.room || 'no room')}">
            <span>${esc(g.code.replace(/[0-9]/g, ''))}</span>
          </button>
        `).join('') || '<div class="twm-empty">No genes match this triage filter.</div>'}
      </div>
      ${renderGeneInspector(scans)}
      <div class="twm-genome-note">Each gene is one placed section. Red means direct time harm, amber means network pressure, green means locally clean.</div>
    ` : '<div class="twm-empty">No placed section genes in this scan.</div>';
  }

  function groupScans(scans) {
    const out = [];
    scans.forEach(scan => {
      const detail = state.boardDetails.get(scan.board.id) || {};
      const placements = detail.placements || [];
      const capCourses = (scan.capacity || {}).courses || [];
      const bySection = new Map();
      placements.forEach(p => {
        const key = p.section || 'S1';
        if (!bySection.has(key)) bySection.set(key, []);
        bySection.get(key).push(p);
      });
      const overlapIds = new Set();
      (scan.conflicts.overlaps || []).forEach(o => (o.ids || []).forEach(id => overlapIds.add(id)));
      const crossSections = new Set();
      (scan.conflicts.cross_board_conflicts || []).forEach(c => {
        crossSections.add(c.section_a);
        crossSections.add(c.section_b);
      });
      const affectedByCourse = new Map();
      (scan.impact.overlap_details || []).forEach(d => {
        (d.courses || []).forEach(code => affectedByCourse.set(code, (affectedByCourse.get(code) || 0) + (d.affected || 0)));
      });
      Array.from(bySection.entries()).sort(([a], [b]) => String(a).localeCompare(String(b))).forEach(([section, groupPlacements], idx) => {
        const courses = Array.from(new Set(groupPlacements.map(p => p.course_code))).sort();
        const localConflicts = groupPlacements.filter(p => overlapIds.has(p.id)).length;
        const networkHits = groupPlacements.filter(p => crossSections.has(`${p.course_code}-${p.section}`)).length;
        const affected = courses.reduce((sum, code) => sum + (affectedByCourse.get(code) || 0), 0);
        const seatGap = courses.reduce((sum, code) => {
          const course = capCourses.find(c => c.course_code === code);
          return sum + Math.max(0, (course || {}).deficit || 0);
        }, 0);
        const pressure = Math.min(100, Math.round(localConflicts * 24 + networkHits * 10 + Math.min(35, Math.ceil(affected / 3))));
        out.push({
          board: scan.board,
          label: `Group ${idx + 1}`,
          section,
          courses,
          placements: groupPlacements.length,
          localConflicts,
          networkHits,
          affected,
          seatGap,
          pressure,
          tone: toneFor(pressure),
        });
      });
    });
    return out.sort((a, b) => b.pressure - a.pressure || b.affected - a.affected);
  }

  function graphGroupKey(group) {
    return `${group.board.id}::${group.section}`;
  }

  function graphGroupEvidence(group, scans) {
    const scan = scans.find(s => String(s.board.id) === String(group.board.id));
    if (!scan) return [];
    const detail = state.boardDetails.get(scan.board.id) || {};
    const groupPlacements = (detail.placements || []).filter(p => String(p.section || 'S1') === String(group.section));
    const placementIds = new Set(groupPlacements.map(p => p.id));
    const groupSections = new Set(groupPlacements.map(p => `${p.course_code}-${p.section}`));
    const rows = [];

    (scan.conflicts.overlaps || []).forEach(item => {
      const ids = item.ids || [];
      if (!ids.some(id => placementIds.has(id))) return;
      rows.push({
        tone: 'critical',
        label: 'Time overlap',
        title: (item.sections || []).join(' / ') || group.label,
        detail: item.detail || 'Two or more sections collide in the same time window.',
      });
    });

    (scan.conflicts.cross_board_conflicts || []).forEach(item => {
      if (!groupSections.has(item.section_a) && !groupSections.has(item.section_b)) return;
      rows.push({
        tone: 'watch',
        label: 'Cross-board',
        title: `${item.section_a} <-> ${item.section_b}`,
        detail: `${item.overlap_count || 0} shared students | ${item.board_a_label || ''} <-> ${item.board_b_label || ''}`,
      });
    });

    (scan.capacity.courses || []).forEach(item => {
      if (!group.courses.includes(item.course_code) || !(item.deficit > 0)) return;
      const diagnosis = capacityCourseDiagnosis(item);
      rows.push({
        tone: diagnosis.tone,
        label: 'Seat gap',
        title: item.course_code,
        detail: `${diagnosis.label} | ${diagnosis.detail}`,
        deficit: item.deficit || 0,
      });
    });

    return rows.slice(0, 8);
  }

  function expandedBoardIds(scans) {
    const ids = new Set(state.graphExpandedBoardIds);
    if (state.boardId) ids.add(String(state.boardId));
    if (scans.length === 1) ids.add(String(scans[0]?.board.id || ''));
    return ids;
  }

  function setSingleGraphExpansion(boardId) {
    state.graphExpandedBoardIds.clear();
    if (boardId) state.graphExpandedBoardIds.add(String(boardId));
  }

  function refreshGraphOnly() {
    const scans = currentScans();
    updateGraphLabelButton();
    drawFlow(scans);
    renderGraphDetail(scans);
    renderGenome(scans);
    renderBuild(scans);
  }

  function graphLensLabel() {
    return ({
      pressure: 'pressure',
      affected: 'affected students',
      clashes: 'direct clashes',
      capacity: 'seat gap',
    })[state.graphLens] || 'pressure';
  }

  function graphGroupLimitCount() {
    if (state.graphGroupLimit === 'all') return Infinity;
    return Math.max(1, Number(state.graphGroupLimit || 6) || 6);
  }

  function updateGraphLabelButton() {
    const btn = $('twmGraphLabels');
    if (!btn) return;
    const all = state.graphLabels === 'all';
    btn.textContent = all ? 'All labels' : 'Focus labels';
    btn.setAttribute('aria-pressed', all ? 'true' : 'false');
    btn.classList.toggle('active', all);
  }

  function graphLensValue(item) {
    if (state.graphLens === 'affected') return item.affected || 0;
    if (state.graphLens === 'clashes') return (item.overlaps || 0) + (item.localConflicts || 0) + (item.networkHits || 0);
    if (state.graphLens === 'capacity') return item.deficit || item.seatGap || item.deficitCourses || 0;
    return item.pressure || 0;
  }

  function graphLensMeta(item, fallback) {
    if (state.graphLens === 'affected') return `${item.affected || 0} affected | ${fallback}`;
    if (state.graphLens === 'clashes') {
      const direct = (item.overlaps || 0) + (item.localConflicts || 0);
      const network = item.crossBoard || item.networkHits || 0;
      return `${direct} direct | ${network} network`;
    }
    if (state.graphLens === 'capacity') {
      const gap = item.deficit || item.seatGap || item.deficitCourses || 0;
      return `${gap} seat gap | ${fallback}`;
    }
    return fallback;
  }

  function selectedGraphGroup(scans) {
    if (!state.graphFocusGroupKey) return null;
    return groupScans(scans).find(group => graphGroupKey(group) === state.graphFocusGroupKey) || null;
  }

  function buildSuggestionId(kind, boardId, key) {
    return `${kind}:${boardId || 'all'}:${key || 'system'}`;
  }

  function buildPreviewTarget() {
    if (!state.buildPreviewId) return null;
    const [kind, boardId, key] = state.buildPreviewId.split(':');
    return {
      kind,
      boardId: boardId || '',
      key: key || '',
      groupKey: boardId && key && key !== 'balance' ? `${boardId}::${key}` : '',
    };
  }

  function applyBuildPreviewFocus() {
    const target = buildPreviewTarget();
    if (!target?.boardId || target.boardId === 'all') return;
    state.graphExpandedBoardIds.add(target.boardId);
    state.graphFocusBoardId = target.boardId;
    state.graphFocusGroupKey = target.groupKey || '';
  }

  function renderGroups(scans) {
    const groups = groupScans(scans);
    $('twmGroupStatus').textContent = `${groups.length} groups`;
    $('twmGroups').innerHTML = groups.length ? groups.slice(0, 18).map(g => `
      <button class="twm-group ${g.tone}" type="button" title="${esc(g.courses.join(', '))}"
        data-twm-group-board="${esc(g.board.id)}" data-twm-group-key="${esc(graphGroupKey(g))}">
        <span class="twm-group-score">${g.pressure}</span>
        <span class="twm-group-main">
          <strong>${esc(g.board.label)} / ${esc(g.label)}</strong>
          <em>${g.placements} sections | ${g.courses.length} courses | ${g.affected} affected</em>
          <small>${g.localConflicts} local conflict hits | ${g.networkHits} network hits | group key ${esc(g.section)}</small>
        </span>
        <span class="twm-group-bar"><i style="width:${g.pressure || 3}%"></i></span>
      </button>
    `).join('') : '<div class="twm-empty">No group-level placements found in this scan.</div>';
    document.querySelectorAll('#twmGroups [data-twm-group-key]').forEach(btn => {
      btn.addEventListener('click', () => {
        const boardId = btn.dataset.twmGroupBoard || '';
        state.graphFocusBoardId = boardId;
        state.graphFocusGroupKey = btn.dataset.twmGroupKey || '';
        if (boardId) state.graphExpandedBoardIds.add(String(boardId));
        state.buildPreviewId = '';
        refreshGraphOnly();
        document.getElementById('twmSectionStudents')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
  }

  function renderInterventions(scans) {
    const actions = new Map();
    const addAction = (key, item) => {
      const existing = actions.get(key);
      if (!existing) {
        actions.set(key, { ...item, count: 1 });
        return;
      }
      existing.count += 1;
      existing.priority = Math.min(existing.priority, item.priority);
    };
    scans.forEach(scan => {
      (scan.conflicts.overlaps || []).forEach(o => addAction(
        `overlap:${scan.board.id}:${(o.ids || []).slice().sort().join(',') || (o.sections || []).join('|')}`,
        {
        priority: 1,
        tone: 'critical',
        title: `Separate ${(o.sections || []).join(' / ')}`,
        detail: `${scan.board.label}: ${o.detail || 'direct overlap'} | first move should protect affected students`,
        }
      ));
      (scan.impact.overlap_details || []).forEach(d => {
        if (d.blocked) {
          addAction(`blocked:${scan.board.id}:${(d.courses || []).slice().sort().join('|')}`, {
            priority: 2,
            tone: 'critical',
            title: `Create clean route for ${(d.courses || []).join(' / ')}`,
            detail: `${scan.board.label}: ${d.blocked} blocked students, sample ${((d.students || []).slice(0, 4)).join(', ') || '-'}`,
          });
        }
      });
      (scan.conflicts.cross_board_conflicts || []).forEach(c => addAction(
        `cross:${[c.section_a, c.section_b].sort().join('|')}:${[c.board_a_id, c.board_b_id].sort().join('|')}`,
        {
        priority: 3,
        tone: 'watch',
        title: `Reduce shared-student bridge ${c.section_a} <-> ${c.section_b}`,
        detail: `${c.overlap_count} shared students across ${c.board_a_label} and ${c.board_b_label}`,
        }
      ));
      ((scan.capacity.courses || []).filter(c => (c.deficit || 0) > 0)).forEach(c => {
        const diagnosis = capacityCourseDiagnosis(c);
        addAction(`capacity:${scan.board.id}:${c.course_code}`, {
        priority: 4,
        tone: diagnosis.tone,
        title: `Add seats or split ${c.course_code}`,
        detail: `${scan.board.label}: ${diagnosis.label} | ${diagnosis.detail}`,
        });
      });
    });
    const rows = Array.from(actions.values()).sort((a, b) => a.priority - b.priority || b.count - a.count);
    $('twmInterventionStatus').textContent = `${rows.length} candidate actions`;
    $('twmInterventions').innerHTML = rows.length ? rows.slice(0, 12).map((a, idx) => `
      <div class="twm-action ${a.tone}">
        <span>${String(idx + 1).padStart(2, '0')}</span>
        <div>
          <strong>${esc(a.title)}</strong>
          <em>${esc(a.detail)}${a.count > 1 ? ` | ${plural(a.count, 'signal')} grouped` : ''}</em>
        </div>
      </div>
    `).join('') : '<div class="twm-empty">No intervention candidates in this scan.</div>';
  }

  function buildSuggestions(scans) {
    const selectedGroup = selectedGraphGroup(scans);
    const focusedScan = scans.find(scan => String(scan.board.id) === String(state.graphFocusBoardId));
    const scopeScans = selectedGroup
      ? scans.filter(scan => String(scan.board.id) === String(selectedGroup.board.id))
      : focusedScan ? [focusedScan] : scans;
    const groups = groupScans(scopeScans);
    const targetGroups = selectedGroup ? [selectedGroup] : groups.slice(0, 5);
    const moves = [];

    targetGroups.forEach(group => {
      const scan = scopeScans.find(item => String(item.board.id) === String(group.board.id));
      if (!scan) return;
      const evidence = graphGroupEvidence(group, scopeScans);
      const direct = evidence.filter(item => item.tone === 'critical').length;
      const network = group.networkHits || 0;
      const seatEvidence = evidence.filter(item => item.label === 'Seat gap');
      const baseAffected = group.affected || scan.affected || 0;

      if (direct || group.localConflicts) {
        const after = Math.max(0, baseAffected - Math.max(8, Math.ceil(baseAffected * 0.62)));
        moves.push({
          id: buildSuggestionId('reslot', group.board.id, group.section),
          tone: 'critical',
          title: `Reslot ${group.board.label} / ${group.label}`,
          detail: `Move the overlapping section pair to a green slot before changing capacity. This attacks direct student harm first.`,
          before: `${baseAffected} affected`,
          after: `${after} estimated`,
          impact: baseAffected - after,
          confidence: direct ? 86 : 74,
          command: 'Preview clean-slot search',
        });
      }

      if (network) {
        const afterLinks = Math.max(0, network - Math.ceil(network * 0.55));
        moves.push({
          id: buildSuggestionId('decouple', group.board.id, group.section),
          tone: 'watch',
          title: `Decouple network pressure in ${group.label}`,
          detail: `Protect the shared-student bridge by moving one high-network course away from neighboring boards.`,
          before: `${network} network hits`,
          after: `${afterLinks} estimated`,
          impact: network - afterLinks,
          confidence: 68,
          command: 'Preview bridge split',
        });
      }

      if (seatEvidence.length) {
        const totalGap = seatEvidence.reduce((sum, item) => {
          const match = item.detail.match(/(\d+) deficit/);
          return sum + (item.deficit || (match ? Number(match[1]) : 1));
        }, 0);
        moves.push({
          id: buildSuggestionId('capacity', group.board.id, group.section),
          tone: 'watch',
          title: `Add or split seats for ${group.label}`,
          detail: `Capacity is blocking clean placement. Split one constrained course or add seats before rescheduling.`,
          before: `${totalGap} seat gap`,
          after: '0 target gap',
          impact: totalGap,
          confidence: 72,
          command: 'Preview capacity relief',
        });
      }
    });

    scopeScans.forEach(scan => {
      if (scan.overlaps || scan.crossBoard) {
        moves.push({
          id: buildSuggestionId('board-balance', scan.board.id, 'balance'),
          tone: scan.overlaps ? 'critical' : 'watch',
          title: `Balance ${scan.board.label} before adding new sections`,
          detail: `Freeze low-risk groups and rebuild only the hot groups. This preserves most of the timetable while reducing blast radius.`,
          before: `${scan.overlaps + scan.crossBoard} pressure links`,
          after: `${Math.max(0, scan.overlaps + scan.crossBoard - 6)} estimated`,
          impact: Math.min(6, scan.overlaps + scan.crossBoard),
          confidence: 64,
          command: 'Preview partial rebuild',
        });
      }
    });

    return moves
      .filter((move, idx, arr) => arr.findIndex(item => item.id === move.id) === idx)
      .sort((a, b) => b.impact - a.impact || b.confidence - a.confidence)
      .slice(0, 9);
  }

  function buildRepairQueue(scans) {
    const rows = [];
    scans.forEach(scan => {
      const detail = state.boardDetails.get(scan.board.id) || {};
      (detail.placements || []).forEach(placement => {
        const evidence = placementEvidence(scan, placement)
          .filter(item => item.title !== 'Seat pressure');
        if (!evidence.length) return;
        const critical = evidence.filter(item => item.tone === 'critical').length;
        const warning = evidence.length - critical;
        const slots = candidateMoveSlots(placement);
        const slotSummary = moveSlotSummary(slots);
        const repairBonus = (slotSummary.clean * 45) + (slotSummary.risky * 16) - (slotSummary.avoid * 2);
        rows.push({
          placement,
          board: scan.board,
          evidence,
          critical,
          warning,
          slotSummary,
          repairTone: slotSummary.clean ? 'stable' : slotSummary.risky ? 'watch' : 'critical',
          tone: critical ? 'critical' : 'watch',
          score: (critical * 100) + (warning * 20) + repairBonus + (placement.is_locked ? -10 : 0),
        });
      });
    });
    return rows
      .sort((a, b) => b.score - a.score ||
        b.critical - a.critical ||
        b.slotSummary.clean - a.slotSummary.clean ||
        String(a.board.label).localeCompare(String(b.board.label)) ||
        String(a.placement.course_code).localeCompare(String(b.placement.course_code)))
      .slice(0, 8);
  }

  function renderRepairQueue(scans) {
    const rows = buildRepairQueue(scans);
    if (!rows.length) {
      return `
        <section class="twm-repair-queue stable">
          <div class="twm-repair-queue-head">
            <strong>Repair Queue</strong>
            <em>No damaged placed sections in this scan.</em>
          </div>
        </section>
      `;
    }
    const critical = rows.filter(row => row.tone === 'critical').length;
    return `
      <section class="twm-repair-queue">
        <div class="twm-repair-queue-head">
          <strong>Repair Queue</strong>
          <em>${critical} urgent / ${rows.length - critical} pressure items. Pick one to start the existing Move Section workflow.</em>
        </div>
        <div class="twm-repair-items">
          ${rows.map((row, idx) => {
            const first = row.evidence[0] || {};
            return `
              <article class="twm-repair-item ${row.tone}${String(row.placement.id) === String(state.movePlacementId) ? ' active' : ''}">
                <div class="twm-repair-rank">${String(idx + 1).padStart(2, '0')}</div>
                <div class="twm-repair-main">
                  <strong>${esc(placementLabel(row.placement))}</strong>
                  <span>${esc(placementContextLabel(row.placement))}</span>
                  <em>${esc(first.title || 'MRI pressure')} | ${esc(first.detail || 'Open this section to inspect candidate slots.')}</em>
                  <em class="${esc(row.repairTone)}">Repairability | ${esc(row.slotSummary.verdict)}</em>
                </div>
                <div class="twm-repair-score">
                  <span><b>${row.critical}</b> red</span>
                  <span><b>${row.warning}</b> amber</span>
                  <span class="stable"><b>${row.slotSummary.clean}</b> clean</span>
                  <span class="critical"><b>${row.slotSummary.avoid}</b> avoid</span>
                </div>
                <button type="button" data-twm-repair-placement="${esc(row.placement.id)}">Start Repair</button>
              </article>
            `;
          }).join('')}
        </div>
      </section>
    `;
  }

  function renderBuild(scans) {
    const target = $('twmBuildMoves');
    if (!target) return;
    const moves = buildSuggestions(scans);
    const selected = moves.find(move => move.id === state.buildPreviewId);
    $('twmBuildStatus').textContent = selected ? `Previewing ${selected.command}` : `${moves.length} suggested moves`;
    const moveList = moves.length ? moves.map((move, idx) => `
      <article class="twm-build-move ${move.tone}${move.id === state.buildPreviewId ? ' active' : ''}">
        <div class="twm-build-rank">${String(idx + 1).padStart(2, '0')}</div>
        <div class="twm-build-main">
          <strong>${esc(move.title)}</strong>
          <em>${esc(move.detail)}</em>
          <div class="twm-build-delta">
            <span><b>Before</b>${esc(move.before)}</span>
            <span><b>After</b>${esc(move.after)}</span>
            <span><b>Confidence</b>${move.confidence}%</span>
          </div>
        </div>
        <button type="button" data-twm-build-preview="${esc(move.id)}">${move.id === state.buildPreviewId ? 'Selected' : 'Preview'}</button>
      </article>
    `).join('') : '<div class="twm-empty">Select a hot term or group in the graph to generate build moves.</div>';
    target.innerHTML = `${renderRepairQueue(scans)}${renderMoveBuilder(scans)}${moveList}`;
  }

  function buildPreviewBanner(scans) {
    const move = buildSuggestions(scans).find(item => item.id === state.buildPreviewId);
    if (!move) return '';
    return `
      <div class="twm-build-preview-note ${move.tone}">
        <span>Preview selected</span>
        <strong>${esc(move.title)}</strong>
        <em>${esc(move.before)} -> ${esc(move.after)} | ${move.confidence}% confidence</em>
      </div>
    `;
  }

  function placementInstructor(placement) {
    return ((placement.meetings || [])[0] || {}).instructor || '';
  }

  function placementLabel(placement) {
    return `${placement.course_code || 'Course'} ${placement.section || ''}`.trim();
  }

  function placementSectionKey(placement) {
    return `${placement?.course_code || ''}-${placement?.section || ''}`;
  }

  function placementBoardLabel(placement) {
    const boardId = String(placement?.board_id || placement?.board?.id || '');
    return (state.boards.find(board => String(board.id) === boardId) || {}).label || placement?.board_label || 'Board';
  }

  function placementTimeLabel(item) {
    const day = item?.day || '';
    const start = item?.start_time || item?.start || '';
    const end = item?.end_time || item?.end || '';
    return `${day} ${start}${end ? '-' + end : ''}`.trim();
  }

  function placementContextLabel(item) {
    const placement = item?.placement || item || {};
    const time = item?.placement ? placementTimeLabel(item) : placementTimeLabel(placement);
    return `${placementLabel(placement)} | ${placementBoardLabel(placement)} | ${time}`;
  }

  function moveTargetLabel(target) {
    return `${placementLabel(target.placement)} -> ${target.day} ${target.start || target.start_time}-${target.end || target.end_time}`;
  }

  function focusedGroupPlacements(scans) {
    const group = selectedGraphGroup(scans);
    if (!group) return [];
    const detail = state.boardDetails.get(group.board.id) || {};
    return (detail.placements || [])
      .filter(p => String(p.section || 'S1') === String(group.section))
      .sort((a, b) => String(a.course_code).localeCompare(String(b.course_code)));
  }

  function sectionRank(section) {
    const match = String(section || '').match(/\d+/);
    return match ? Number(match[0]) : 99;
  }

  function findPlacementById(scans, id) {
    const key = String(id || '');
    if (!key) return null;
    for (const scan of scans) {
      const detail = state.boardDetails.get(scan.board.id) || {};
      const found = (detail.placements || []).find(p => String(p.id) === key);
      if (found) return found;
    }
    return null;
  }

  function findMovePlacement(scans) {
    return findPlacementById(scans, state.movePlacementId);
  }

  function focusPlacementGroup(placement) {
    const boardId = String(placement?.board_id || placement?.board?.id || '');
    if (!boardId) return;
    state.graphFocusBoardId = boardId;
    state.graphFocusGroupKey = `${boardId}::${placement.section || 'S1'}`;
    state.graphExpandedBoardIds.add(boardId);
  }

  function startRepairSelection(scans, placementId, message) {
    const placement = findPlacementById(scans, placementId);
    if (!placement) return false;
    focusPlacementGroup(placement);
    state.movePlacementId = String(placementId || '');
    state.moveSlot = null;
    state.moveSlotFilter = 'all';
    state.buildPreviewId = '';
    state.moveMessage = message || `Repair queue selected ${placementLabel(placement)}. Candidate slots are ready.`;
    state.mode = 'build';
    refreshGraphOnly();
    applyMode({ scroll: true });
    requestAnimationFrame(() => {
      $('twmMoveBuilder')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    return true;
  }

  function moveSlotTrailLabel(slot) {
    if (!slot) return 'No slot selected';
    const pair = slot.pairId
      ? ` + ${slot.pairSection || 'paired section'} ${slot.pairRelation || 'adjacent'} ${slot.pairStart || ''}-${slot.pairEnd || ''}`
      : '';
    return `${slot.day} ${slot.start}-${slot.end}${pair}`;
  }

  function pushMoveRepairTrail(scans, blockerPlacement, reason) {
    const selected = findMovePlacement(scans);
    if (!selected || !blockerPlacement || String(selected.id) === String(blockerPlacement.id)) return;
    const item = {
      fromPlacementId: String(selected.id),
      fromLabel: placementLabel(selected),
      fromSlot: state.moveSlot ? { ...state.moveSlot } : null,
      fromSlotLabel: moveSlotTrailLabel(state.moveSlot),
      toPlacementId: String(blockerPlacement.id),
      toLabel: placementLabel(blockerPlacement),
      reason: reason || 'blocks this candidate',
    };
    const next = state.moveRepairTrail.filter(existing => !(
      existing.fromPlacementId === item.fromPlacementId &&
      existing.toPlacementId === item.toPlacementId &&
      existing.fromSlotLabel === item.fromSlotLabel
    ));
    next.push(item);
    state.moveRepairTrail = next.slice(-4);
  }

  function moveSlotPool(placement) {
    const detail = state.boardDetails.get(placement.board_id) || {};
    const seen = new Set();
    const allSlots = [...(detail.slot_config || []), ...(detail.lab_slot_config || [])]
      .filter(slot => {
        if (!slot?.start || !slot?.end) return false;
        const key = `${slot.start}-${slot.end}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .sort((a, b) => toMinutes(a.start) - toMinutes(b.start) || toMinutes(a.end) - toMinutes(b.end));
    const duration = durationMinutes(placement.start_time, placement.end_time);
    const matching = allSlots.filter(slot => Math.abs(durationMinutes(slot.start, slot.end) - duration) <= 5);
    return matching.length ? matching : allSlots;
  }

  function courseCompanionForMove(placement) {
    const detail = state.boardDetails.get(placement.board_id) || {};
    const instructor = placementInstructor(placement).trim().toUpperCase();
    const rank = sectionRank(placement.section);
    return (detail.placements || [])
      .filter(p => String(p.id) !== String(placement.id))
      .filter(p => String(p.course_code) === String(placement.course_code))
      .filter(p => String(p.section || '') !== String(placement.section || ''))
      .sort((a, b) => {
        const aInstructor = placementInstructor(a).trim().toUpperCase();
        const bInstructor = placementInstructor(b).trim().toUpperCase();
        const aSameInstructor = instructor && aInstructor === instructor ? 0 : 1;
        const bSameInstructor = instructor && bInstructor === instructor ? 0 : 1;
        return aSameInstructor - bSameInstructor ||
          Math.abs(sectionRank(a.section) - rank) - Math.abs(sectionRank(b.section) - rank) ||
          sectionRank(a.section) - sectionRank(b.section) ||
          String(a.section).localeCompare(String(b.section));
      })[0] || null;
  }

  function findPlacementBySectionKey(boardId, sectionKey) {
    const detail = state.boardDetails.get(Number(boardId)) || state.boardDetails.get(String(boardId)) || {};
    return (detail.placements || []).find(item => placementSectionKey(item) === sectionKey) || null;
  }

  function crossBoardCounterparts(placement) {
    const boardId = String(placement?.board_id || placement?.board?.id || '');
    const scanConflicts = state.conflicts.get(Number(boardId)) || state.conflicts.get(boardId) || {};
    const sectionKey = placementSectionKey(placement);
    const rows = [];
    (scanConflicts.cross_board_conflicts || []).forEach(item => {
      let otherBoardId = '';
      let otherSection = '';
      if (item.section_a === sectionKey && (!item.board_a_id || String(item.board_a_id) === boardId)) {
        otherBoardId = String(item.board_b_id || '');
        otherSection = item.section_b || '';
      } else if (item.section_b === sectionKey && (!item.board_b_id || String(item.board_b_id) === boardId)) {
        otherBoardId = String(item.board_a_id || '');
        otherSection = item.section_a || '';
      }
      if (!otherBoardId || !otherSection) return;
      const counterpart = findPlacementBySectionKey(otherBoardId, otherSection);
      if (!counterpart) return;
      rows.push({
        placement: counterpart,
        shared: item.overlap_count || 0,
        item,
      });
    });
    return rows;
  }

  function currentConflictSummary(placement, companion = null) {
    const conflicts = state.conflicts.get(placement.board_id) || {};
    const placements = [placement, companion].filter(Boolean);
    const ids = new Set(placements.map(p => String(p.id)));
    const hasId = item => (item.ids || []).some(id => ids.has(String(id)));
    const networkWarnings = placements.reduce((sum, item) => sum + crossBoardCounterparts(item).length, 0);
    return {
      critical: (conflicts.overlaps || []).filter(hasId).length +
        (conflicts.instructor_clashes || []).filter(hasId).length,
      warning: (conflicts.room_clashes || []).filter(hasId).length + networkWarnings,
    };
  }

  function classifyMoveTargets(targets) {
    const detail = state.boardDetails.get(targets[0]?.placement.board_id) || {};
    const movingIds = new Set(targets.map(target => String(target.placement.id)));
    const seen = new Set();
    const evidence = [];
    let overlapCount = 0;
    let instructorCount = 0;
    let roomCount = 0;
    let networkCount = 0;

    function add(kind, a, b) {
      const key = `${kind}:${[a, b].map(String).sort().join(':')}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }

    function compareTarget(target, other) {
      if (String(target.day) !== String(other.day)) return;
      if (!overlaps(target.start, target.end, other.start_time || other.start, other.end_time || other.end)) return;
      const otherId = other.id || `move-${other.placement?.id}`;
      const otherRoom = other.room || other.placement?.room || '';
      const targetContext = placementContextLabel(target);
      const otherContext = placementContextLabel(other);
      if (add('overlap', target.placement.id, otherId)) {
        overlapCount += 1;
        evidence.push({
          kind: 'time',
          tone: 'critical',
          title: `${placementLabel(target.placement)} overlaps ${placementContextLabel(other.placement || other)}`,
          detail: `${targetContext} conflicts with ${otherContext}.`,
          blockerId: other.placement ? other.placement.id : other.id,
          blockerLabel: placementContextLabel(other.placement || other),
        });
      }
      const instructor = placementInstructor(target.placement).trim().toUpperCase();
      const otherInstructor = placementInstructor(other.placement || other).trim().toUpperCase();
      if (instructor && otherInstructor && instructor === otherInstructor && add('instructor', target.placement.id, otherId)) {
        instructorCount += 1;
        evidence.push({
          kind: 'instructor',
          tone: 'critical',
          title: `Instructor clash: ${placementInstructor(target.placement)}`,
          detail: `${targetContext} and ${otherContext} would require the same instructor at the same time.`,
          blockerId: other.placement ? other.placement.id : other.id,
          blockerLabel: placementContextLabel(other.placement || other),
        });
      }
      if (
        target.placement.room &&
        otherRoom &&
        String(target.placement.room).toUpperCase() !== 'UNASSIGNED' &&
        String(target.placement.room).trim().toUpperCase() === String(otherRoom).trim().toUpperCase() &&
        add('room', target.placement.id, otherId)
      ) {
        roomCount += 1;
        evidence.push({
          kind: 'room',
          tone: 'watch',
          title: `Room warning: ${target.placement.room}`,
          detail: `${targetContext} and ${otherContext} would share the same room.`,
          blockerId: other.placement ? other.placement.id : other.id,
          blockerLabel: placementContextLabel(other.placement || other),
        });
      }
    }

    targets.forEach(target => {
      (detail.placements || []).forEach(other => {
        if (movingIds.has(String(other.id))) return;
        compareTarget(target, other);
      });
      crossBoardCounterparts(target.placement).forEach(link => {
        const other = link.placement;
        if (!other || String(target.day) !== String(other.day)) return;
        if (!overlaps(target.start, target.end, other.start_time, other.end_time)) return;
        if (!add('network', target.placement.id, other.id || placementSectionKey(other))) return;
        networkCount += 1;
        evidence.push({
          kind: 'network',
          tone: 'watch',
          title: `Cross-board students: ${placementLabel(target.placement)}`,
          detail: `${link.shared || 0} shared students would still overlap with ${placementContextLabel(other)}.`,
          blockerId: other.id,
          blockerLabel: placementContextLabel(other),
        });
      });
    });

    targets.forEach((target, idx) => {
      targets.slice(idx + 1).forEach(other => {
        compareTarget(target, {
          id: other.placement.id,
          placement: other.placement,
          day: other.day,
          start_time: other.start,
          end_time: other.end,
          room: other.placement.room,
        });
      });
    });

    const critical = overlapCount + instructorCount;
    const warning = roomCount + networkCount;
    return {
      critical,
      warning,
      evidence: evidence.slice(0, 8),
    };
  }

  function explainCleanMove(targets, hasCompanion) {
    const labels = targets.map(moveTargetLabel).join(' | ');
    return hasCompanion
      ? `No local time, instructor, or room conflict found for the bundled move: ${labels}.`
      : `No local time, instructor, or room conflict found for ${labels}.`;
  }

  function scoreMoveSlot(slot, placement) {
    return (slot.critical * 1000) +
      (slot.warning * 120) +
      (slot.pairPreferred ? -35 : 0) +
      (String(slot.day) === String(placement.day) ? 0 : 12) +
      Math.round(Math.abs(toMinutes(slot.start) - toMinutes(placement.start_time)) / 10);
  }

  function finishMoveSlots(slots, placement) {
    const sorted = slots
      .map(slot => ({
        ...slot,
        score: scoreMoveSlot(slot, placement),
      }))
      .sort((a, b) => a.score - b.score || DAYS.indexOf(a.day) - DAYS.indexOf(b.day) || toMinutes(a.start) - toMinutes(b.start))
      .slice(0, 20);
    return sorted.map((slot, idx) => {
      let badge = slot.critical ? 'Avoid' : slot.warning ? 'Risky' : 'Clean';
      if (idx === 0) badge = slot.critical || slot.warning ? 'Least bad' : 'Best';
      return {
        ...slot,
        rank: idx + 1,
        badge,
      };
    });
  }

  function moveSlotSummary(slots) {
    const clean = slots.filter(slot => !slot.critical && !slot.warning).length;
    const risky = slots.filter(slot => !slot.critical && slot.warning).length;
    const avoid = slots.filter(slot => slot.critical).length;
    const first = slots[0];
    const verdict = clean
      ? `${clean} clean option${clean === 1 ? '' : 's'} available`
      : first
        ? `No clean option found. Least-bad preview still has ${first.critical} critical / ${first.warning} warning.`
        : 'No candidate slots found.';
    return { clean, risky, avoid, verdict };
  }

  function classifyMoveSlot(placement, day, slot) {
    const targets = [{ placement, day, start: slot.start, end: slot.end }];
    const result = classifyMoveTargets(targets);
    return {
      day,
      start: slot.start,
      end: slot.end,
      critical: result.critical,
      warning: result.warning,
      evidence: result.evidence,
      targets,
      tone: result.critical ? 'critical' : result.warning ? 'watch' : 'stable',
      label: result.critical ? `${result.critical} conflict` : result.warning ? `${result.warning} warning` : 'Clean',
    };
  }

  function pairTransitionMinutes(primarySlot, companionSlot, relation) {
    if (!primarySlot || !companionSlot) return Infinity;
    if (relation === 'after') return toMinutes(companionSlot.start) - toMinutes(primarySlot.end);
    return toMinutes(primarySlot.start) - toMinutes(companionSlot.end);
  }

  function candidateMoveSlots(placement) {
    const slots = moveSlotPool(placement);
    const companion = courseCompanionForMove(placement);
    if (companion) {
      const selectedRank = sectionRank(placement.section);
      const companionRank = sectionRank(companion.section);
      const naturalRelation = companionRank >= selectedRank ? 'after' : 'before';
      return finishMoveSlots(DAYS.flatMap(day => slots.flatMap((slot, idx) => ([
        { relation: 'after', slot: slots[idx + 1] },
        { relation: 'before', slot: slots[idx - 1] },
      ]).filter(option => option.slot).map(option => {
        const transition = pairTransitionMinutes(slot, option.slot, option.relation);
        if (transition < 0 || transition > 15) return null;
        const targets = [
          { placement, day, start: slot.start, end: slot.end },
          { placement: companion, day, start: option.slot.start, end: option.slot.end },
        ];
        const duplicateTarget = targets.some((target, targetIdx) => targets.slice(targetIdx + 1)
          .some(other => target.day === other.day && target.start === other.start && target.end === other.end));
        if (duplicateTarget) return null;
        const selectedTakesCompanionCurrent = sameSlot(targets[0], companion);
        const companionTakesSelectedCurrent = sameSlot(targets[1], placement);
        if (selectedTakesCompanionCurrent && companionTakesSelectedCurrent) return null;
        const result = classifyMoveTargets(targets);
        const preferred = option.relation === naturalRelation;
        return {
          day,
          start: slot.start,
          end: slot.end,
          critical: result.critical,
          warning: result.warning,
          evidence: result.evidence,
          targets,
          tone: result.critical ? 'critical' : result.warning ? 'watch' : 'stable',
          label: result.critical ? `${result.critical} pair conflicts` : result.warning ? `${result.warning} pair warnings` : 'Clean pair',
          pairId: companion.id,
          pairSection: companion.section,
          pairDay: day,
          pairStart: option.slot.start,
          pairEnd: option.slot.end,
          pairRelation: option.relation,
          pairTransition: transition,
          pairPreferred: preferred,
        };
      }).filter(Boolean)))
        .filter(slot => !(slot.day === placement.day && slot.start === placement.start_time)), placement);
    }

    return finishMoveSlots(
      DAYS.flatMap(day => slots.map(slot => classifyMoveSlot(placement, day, slot)))
        .filter(slot => !(slot.day === placement.day && slot.start === placement.start_time)),
      placement
    );
  }

  function moveSlotActive(slot, chosen) {
    return chosen &&
      chosen.day === slot.day &&
      chosen.start === slot.start &&
      String(chosen.pairId || '') === String(slot.pairId || '') &&
      String(chosen.pairStart || '') === String(slot.pairStart || '');
  }

  function moveSlotCategory(slot) {
    if (slot.critical) return 'avoid';
    if (slot.warning) return 'risky';
    return 'clean';
  }

  function filteredMoveSlots(slots) {
    const filter = state.moveSlotFilter || 'all';
    if (filter === 'all') return slots;
    return slots.filter(slot => moveSlotCategory(slot) === filter);
  }

  function renderMoveSlotTools(slots) {
    const counts = {
      all: slots.length,
      clean: slots.filter(slot => moveSlotCategory(slot) === 'clean').length,
      risky: slots.filter(slot => moveSlotCategory(slot) === 'risky').length,
      avoid: slots.filter(slot => moveSlotCategory(slot) === 'avoid').length,
    };
    const filter = state.moveSlotFilter || 'all';
    return `
      <div class="twm-move-slot-tools" aria-label="Candidate slot filters">
        ${[
          ['all', 'All', counts.all],
          ['clean', 'Clean', counts.clean],
          ['risky', 'Risky', counts.risky],
          ['avoid', 'Avoid', counts.avoid],
        ].map(([value, label, count]) => `
          <button class="${filter === value ? 'active' : ''}" type="button" data-twm-slot-filter="${value}">
            <span>${label}</span><b>${count}</b>
          </button>
        `).join('')}
      </div>
    `;
  }

  function moveSlotDataAttrs(slot) {
    return `data-twm-move-day="${esc(slot.day)}" data-twm-move-start="${esc(slot.start)}" data-twm-move-end="${esc(slot.end)}"
      data-twm-move-critical="${slot.critical}" data-twm-move-warning="${slot.warning}"
      data-twm-move-pair-id="${esc(slot.pairId || '')}" data-twm-move-pair-day="${esc(slot.pairDay || '')}"
      data-twm-move-pair-start="${esc(slot.pairStart || '')}" data-twm-move-pair-end="${esc(slot.pairEnd || '')}"
      data-twm-move-pair-section="${esc(slot.pairSection || '')}" data-twm-move-pair-relation="${esc(slot.pairRelation || '')}"`;
  }

  function moveSlotImpact(before, slot) {
    const beforeCritical = before?.critical || 0;
    const beforeWarning = before?.warning || 0;
    const criticalDelta = beforeCritical - (slot?.critical || 0);
    const warningDelta = beforeWarning - (slot?.warning || 0);
    const worsens = criticalDelta < 0 || warningDelta < 0;
    const improves = criticalDelta > 0 || warningDelta > 0;
    const clean = !(slot?.critical || slot?.warning);
    const removed = [
      criticalDelta > 0 ? `${criticalDelta} red` : '',
      warningDelta > 0 ? `${warningDelta} amber` : '',
    ].filter(Boolean).join(' / ');
    const added = [
      criticalDelta < 0 ? `${Math.abs(criticalDelta)} red` : '',
      warningDelta < 0 ? `${Math.abs(warningDelta)} amber` : '',
    ].filter(Boolean).join(' / ');
    return {
      criticalDelta,
      warningDelta,
      tone: worsens ? 'critical' : clean || improves ? 'stable' : 'watch',
      label: worsens
        ? `Adds ${added} locally`
        : improves
          ? `Removes ${removed} locally`
          : clean
            ? 'Clean local fit'
            : 'No local conflict gain',
    };
  }

  function renderRecommendedMoveSlot(slot, before) {
    if (!slot) return '';
    const impact = moveSlotImpact(before, slot);
    return `
      <div class="twm-move-recommendation ${impact.tone}">
        <div class="twm-move-recommendation-main">
          <span>Recommended next move</span>
          <strong>${esc(slot.day)} ${esc(slot.start)}-${esc(slot.end)} | ${esc(slot.badge || 'Candidate')}</strong>
          <em>${esc(impact.label)}. ${esc(slot.label)}${slot.pairId ? ` with ${esc(slot.pairSection)} ${esc(slot.pairRelation)}` : ''}.</em>
        </div>
        <div class="twm-move-recommendation-grid">
          <span><b>${before?.critical || 0}/${before?.warning || 0}</b> before</span>
          <span><b>${slot.critical}/${slot.warning}</b> preview</span>
          <span><b>${impact.criticalDelta}/${impact.warningDelta}</b> delta</span>
        </div>
        <button type="button" ${moveSlotDataAttrs(slot)}>Select Pick</button>
      </div>
    `;
  }

  function renderMoveUndoAction() {
    if (!state.moveUndo?.actions?.length) return '';
    return `
      <div class="twm-move-undo">
        <span><b>Last MRI move</b>${esc(state.moveUndo.label || 'Move applied to draft')}</span>
        <button type="button" data-twm-undo-move>Undo Last MRI Move</button>
      </div>
    `;
  }

  function renderMoveRepairTrail() {
    if (!state.moveRepairTrail.length) return '';
    const offset = Math.max(0, state.moveRepairTrail.length - 4);
    const shown = state.moveRepairTrail.slice(offset);
    return `
      <div class="twm-move-trail">
        <div class="twm-move-trail-main">
          <div class="twm-move-trail-head">
            <b>Repair trail</b>
            <span>${shown.length} blocker jump${shown.length === 1 ? '' : 's'} remembered</span>
          </div>
          <div class="twm-move-trail-steps">
            ${shown.map((item, idx) => `
              <button type="button" data-twm-repair-return="${offset + idx}">
                <b>${esc(item.fromLabel)}</b>
                <span>${esc(item.fromSlotLabel)}</span>
                <em>blocked by ${esc(item.toLabel)} | ${esc(item.reason)}</em>
              </button>
            `).join('')}
          </div>
        </div>
        <button class="twm-move-trail-clear" type="button" data-twm-repair-clear>Clear Trail</button>
      </div>
    `;
  }

  function renderActiveRepairHeader(selected, group, before, summary) {
    if (!selected) return '';
    const beforeLabel = before ? `${before.critical} red / ${before.warning} amber` : 'No conflicts';
    const targetLabel = summary ? summary.verdict : 'Choose a slot to preview impact.';
    return `
      <div class="twm-repair-now">
        <div>
          <span>Repair lane</span>
          <strong>${esc(placementLabel(selected))}</strong>
          <em>${esc(group.board.label)} / ${esc(group.label)} | ${esc(placementTimeLabel(selected))}</em>
        </div>
        <div class="twm-repair-steps">
          <span class="done"><b>1</b>Section selected</span>
          <span class="${summary ? 'done' : ''}"><b>2</b>${esc(targetLabel)}</span>
          <span class="${state.moveSlot ? 'done' : ''}"><b>3</b>${state.moveSlot ? 'Preview ready' : 'Pick slot'}</span>
          <span><b>4</b>${esc(beforeLabel)}</span>
        </div>
      </div>
    `;
  }

  function renderBeforeAfterPanel(slot, before) {
    if (!slot || !before) return '';
    const impact = moveSlotImpact(before, slot);
    const beforeTotal = (before.critical || 0) + (before.warning || 0);
    const afterTotal = (slot.critical || 0) + (slot.warning || 0);
    const targetText = slot.pairId
      ? `${slot.targets.length} linked sections move as one repair`
      : 'Single section move';
    return `
      <div class="twm-before-after ${impact.tone}">
        <span><b>Before</b>${before.critical} red / ${before.warning} amber</span>
        <span><b>After Preview</b>${slot.critical} red / ${slot.warning} amber</span>
        <span><b>Net Change</b>${beforeTotal - afterTotal >= 0 ? '-' : '+'}${Math.abs(beforeTotal - afterTotal)} local signals</span>
        <span><b>Scope</b>${esc(targetText)}</span>
      </div>
    `;
  }

  function renderMoveDetails(slot, before) {
    if (!slot?.targets?.length) return '';
    const evidence = slot.evidence?.length
      ? slot.evidence
      : [{
        tone: 'stable',
        title: 'No local conflict evidence',
        detail: explainCleanMove(slot.targets, Boolean(slot.pairId)),
      }];
    const transitionText = slot.pairId
      ? (() => {
        const ordered = slot.targets.slice().sort((a, b) => toMinutes(a.start) - toMinutes(b.start));
        const gap = Math.max(0, toMinutes(ordered[1].start) - toMinutes(ordered[0].end));
        return `Back-to-back teaching order with ${gap} minute transition.`;
      })()
      : 'Single-section move.';
    const blockers = evidence
      .filter(item => item.blockerId)
      .filter((item, idx, arr) => arr.findIndex(other => String(other.blockerId) === String(item.blockerId)) === idx);

    return `
      <div class="twm-move-detail">
        <div class="twm-move-detail-head">
          <span><b>${esc(slot.badge || 'Candidate')}</b>Rank ${slot.rank || '-'} | score ${slot.score ?? '-'}</span>
          <span>${esc(transitionText)}</span>
        </div>
        <div class="twm-move-actions">
          ${slot.targets.map(target => `
            <span><b>${esc(placementLabel(target.placement))}</b>${esc(target.day)} ${esc(target.start)}-${esc(target.end)}</span>
          `).join('')}
        </div>
        ${renderBeforeAfterPanel(slot, before)}
        <div class="twm-move-evidence">
          ${evidence.map(item => `
            <span class="${esc(item.tone || 'stable')}">
              <b>${esc(item.title)}</b>
              <em>${esc(item.detail)}</em>
            </span>
          `).join('')}
        </div>
        ${blockers.length ? `
          <div class="twm-move-blockers">
            <span><b>Make this clean next</b>Move the blocker section(s), then re-test this candidate.</span>
            ${blockers.map(item => `
              <button type="button" data-twm-move-placement="${esc(item.blockerId)}" data-twm-repair-jump="1" data-twm-repair-reason="${esc(item.title || 'slot blocker')}">
                Inspect ${esc(item.blockerLabel || 'blocker')}
              </button>
            `).join('')}
          </div>
        ` : ''}
        <div class="twm-move-dryrun">
          <span><b>Dry-run apply preview</b>${slot.targets.length} endpoint action(s), ${before.critical}/${before.warning} before -> ${slot.critical}/${slot.warning} preview.</span>
          <button type="button" data-twm-apply-move>${slot.critical ? 'Apply Risky Draft Move' : 'Apply This Draft Move'}</button>
        </div>
      </div>
    `;
  }

  function renderMoveBuilder(scans) {
    const group = selectedGraphGroup(scans);
    if (!group) {
      return `
        <section class="twm-move-builder" id="twmMoveBuilder">
          <strong>Move Section</strong>
          <em>Click a group node in the graph, then choose a course section to move.</em>
          ${renderMoveUndoAction()}
        </section>
      `;
    }

    const placements = focusedGroupPlacements(scans);
    const selected = findMovePlacement(scans);
    const companion = selected ? courseCompanionForMove(selected) : null;
    const slots = selected ? candidateMoveSlots(selected) : [];
    const before = selected ? currentConflictSummary(selected, companion) : null;
    const chosen = state.moveSlot;
    const chosenSlot = chosen ? slots.find(slot => moveSlotActive(slot, chosen)) : null;
    const summary = selected ? moveSlotSummary(slots) : null;
    const visibleSlots = selected ? filteredMoveSlots(slots) : [];
    const slotFilterLabel = ({
      all: 'candidate',
      clean: 'clean',
      risky: 'risky',
      avoid: 'avoid',
    })[state.moveSlotFilter || 'all'] || 'candidate';

    return `
      <section class="twm-move-builder" id="twmMoveBuilder">
        <div class="twm-move-head">
          <strong>Move Section</strong>
          <em>${esc(group.board.label)} / ${esc(group.label)} | choose one placed course section</em>
        </div>
        ${renderActiveRepairHeader(selected, group, before, summary)}
        ${renderMoveRepairTrail()}
        <div class="twm-move-sections">
          ${placements.map(p => `
            <button class="${String(p.id) === String(state.movePlacementId) ? 'active' : ''}" type="button" data-twm-move-placement="${p.id}">
              <span>${esc(p.course_code)} ${esc(p.section || '')}</span>
              <small>${esc(p.day)} ${esc(p.start_time)}-${esc(p.end_time)}</small>
            </button>
          `).join('') || '<div class="twm-empty">No placed sections found for this group.</div>'}
        </div>
        ${selected ? `
          ${companion ? `
            <div class="twm-move-pair">
              <b>Instructor continuity rule</b>
              <span>${esc(selected.course_code)} ${esc(selected.section || '')} is bundled with ${esc(companion.section || '')}. MRI only suggests adjacent before/after slots for this course.</span>
            </div>
          ` : ''}
          <div class="twm-move-rank-summary ${summary.clean ? 'stable' : 'critical'}">
            <span><b>${summary.clean}</b> clean</span>
            <span><b>${summary.risky}</b> risky</span>
            <span><b>${summary.avoid}</b> avoid</span>
            <em>${esc(summary.verdict)}</em>
          </div>
          ${renderMoveSlotTools(slots)}
          ${renderRecommendedMoveSlot(slots[0], before)}
          <div class="twm-move-slots">
            ${visibleSlots.map(slot => `
              <button class="${slot.tone}${moveSlotActive(slot, chosen) ? ' active' : ''}${slot.pairPreferred ? ' preferred' : ''}" type="button"
                ${moveSlotDataAttrs(slot)}>
                <strong><i>${esc(slot.badge || '')}</i> ${esc(slot.day)}</strong>
                <span>${esc(selected.section || 'Section')} ${esc(slot.start)}-${esc(slot.end)}</span>
                ${slot.pairId ? `<small>${esc(slot.pairSection)} ${esc(slot.pairRelation)} | ${esc(slot.pairStart)}-${esc(slot.pairEnd)}</small>` : ''}
                <em>${esc(slot.label)}</em>
              </button>
            `).join('') || `<div class="twm-empty">No ${esc(slotFilterLabel)} slots found for this section.</div>`}
          </div>
          <div class="twm-move-preview ${chosen ? chosen.tone : ''}">
            <span><b>Before</b>${before.critical} critical / ${before.warning} warning${companion ? ' for pair' : ''}</span>
            <span><b>Preview</b>${chosen ? `${chosen.critical} critical / ${chosen.warning} warning${chosen.pairId ? ' as pair' : ''}` : 'Select a slot'}</span>
            <button type="button" disabled>${chosenSlot ? 'Review Below' : 'Select Slot'}</button>
          </div>
          ${renderMoveDetails(chosenSlot, before)}
        ` : ''}
        ${state.moveMessage ? `<div class="twm-move-message">${esc(state.moveMessage)}</div>` : ''}
        ${renderMoveUndoAction()}
      </section>
    `;
  }

  function renderGraphDetail(scans) {
    const target = $('twmGraphDetail');
    if (!target) return;
    const groups = groupScans(scans);
    const expandedIds = expandedBoardIds(scans);
    const focusedScan = scans.find(s => String(s.board.id) === String(state.graphFocusBoardId));
    const focusedGroup = groups.find(g => graphGroupKey(g) === state.graphFocusGroupKey);

    if (focusedGroup) {
      const evidence = graphGroupEvidence(focusedGroup, scans);
      target.innerHTML = `
        <div>
          <strong>${esc(focusedGroup.board.label)} / ${esc(focusedGroup.label)}</strong>
          <span>Viewing ${esc(graphLensLabel())} | group key ${esc(focusedGroup.section)} | pressure ${focusedGroup.pressure} | ${focusedGroup.affected} affected students</span>
        </div>
        <div class="twm-graph-grid">
          <span><b>${focusedGroup.placements}</b> placed sections</span>
          <span><b>${focusedGroup.localConflicts}</b> local clash hits</span>
          <span><b>${focusedGroup.networkHits}</b> cross-board hits</span>
          <span><b>${focusedGroup.courses.length}</b> courses</span>
        </div>
        <div class="twm-graph-chips">${focusedGroup.courses.map(code => `<i>${esc(code)}</i>`).join('')}</div>
        <div class="twm-graph-evidence">
          ${evidence.length ? evidence.map(item => `
            <div class="${item.tone}">
              <span>${esc(item.label)}</span>
              <strong>${esc(item.title)}</strong>
              <em>${esc(item.detail)}</em>
            </div>
          `).join('') : '<div class="stable"><span>Evidence</span><strong>No direct clash evidence found</strong><em>This group is mainly affected through surrounding network pressure.</em></div>'}
        </div>
        ${buildPreviewBanner(scans)}
      `;
      return;
    }

    if (focusedScan) {
      const boardGroups = groups
        .filter(g => String(g.board.id) === String(focusedScan.board.id))
        .sort((a, b) => b.pressure - a.pressure || b.affected - a.affected);
      const limit = graphGroupLimitCount();
      const visibleGroups = Number.isFinite(limit) ? Math.min(boardGroups.length, limit) : boardGroups.length;
      target.innerHTML = `
        <div>
          <strong>${esc(focusedScan.board.label)} expanded</strong>
          <span>Viewing ${esc(graphLensLabel())} | showing ${visibleGroups}/${boardGroups.length} groups | ${focusedScan.students} students | ${focusedScan.affected} affected</span>
        </div>
        <div class="twm-graph-grid">
          <span><b>${boardGroups.length}</b> groups</span>
          <span><b>${focusedScan.overlaps}</b> overlap conflicts</span>
          <span><b>${focusedScan.crossBoard}</b> network links</span>
          <span><b>${focusedScan.deficitCourses}</b> seat-gap courses</span>
        </div>
        <div class="twm-graph-chips">
          ${boardGroups.slice(0, 8).map(g => `<i>${esc(g.label)} ${g.pressure}</i>`).join('')}
        </div>
        ${buildPreviewBanner(scans)}
      `;
      return;
    }

    target.innerHTML = `
      <div>
        <strong>Scenario graph</strong>
        <span>Viewing ${esc(graphLensLabel())}. Click term nodes to expand; group display is set to ${state.graphGroupLimit === 'all' ? 'all groups' : `top ${state.graphGroupLimit} per board`} with ${state.graphLabels === 'all' ? 'all labels' : 'focus labels'}.</span>
      </div>
      <div class="twm-graph-grid">
        <span><b>${scans.length}</b> terms</span>
        <span><b>${groups.length}</b> groups</span>
        <span><b>${expandedIds.size}</b> expanded</span>
        <span><b>${scans.reduce((sum, s) => sum + s.affected, 0)}</b> affected</span>
      </div>
      ${buildPreviewBanner(scans)}
    `;
  }

  function drawFlow(scans) {
    const canvas = $('twmFlowCanvas');
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    state.graphHitNodes = [];
    ctx.clearRect(0, 0, w, h);
    const bg = ctx.createLinearGradient(0, 0, w, h);
    bg.addColorStop(0, '#090d17');
    bg.addColorStop(0.55, '#0e1322');
    bg.addColorStop(1, '#070a12');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = 'rgba(255,255,255,0.035)';
    ctx.lineWidth = 1;
    for (let x = 0; x < w; x += 44) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x + 110, h);
      ctx.stroke();
    }

    const cx = w * 0.5;
    const cy = h * 0.5;
    const radius = Math.min(w, h) * 0.32;
    const groups = groupScans(scans);
    const expandedIds = expandedBoardIds(scans);
    const expandedScans = scans.filter(scan => expandedIds.has(String(scan.board.id)));
    const previewTarget = buildPreviewTarget();
    const maxBoardLens = Math.max(1, ...scans.map(graphLensValue));
    const maxGroupLens = Math.max(1, ...groups.map(graphLensValue));
    const colorFor = tone => tone === 'critical' ? '#f06060' : tone === 'watch' ? '#f5b731' : '#2ec9a0';
    const clampPoint = point => ({
      x: Math.max(56, Math.min(w - 56, point.x)),
      y: Math.max(56, Math.min(h - 74, point.y)),
    });
    const positioned = (key, point) => {
      if (!state.graphPositions.has(key)) state.graphPositions.set(key, clampPoint(point));
      return state.graphPositions.get(key);
    };

    function line(from, to, tone, width, alpha) {
      ctx.strokeStyle = `${colorFor(tone)}${Math.round(alpha * 255).toString(16).padStart(2, '0')}`;
      ctx.lineWidth = width;
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.quadraticCurveTo(cx, cy, to.x, to.y);
      ctx.stroke();
    }

    function node(point, radiusValue, color, label, meta, score, active, preview, showLabel = true) {
      if (preview) {
        ctx.save();
        ctx.setLineDash([8, 6]);
        ctx.strokeStyle = 'rgba(46,201,160,0.86)';
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.arc(point.x, point.y, radiusValue + 34, 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(46,201,160,0.18)';
        ctx.beginPath();
        ctx.arc(point.x, point.y, radiusValue + 43, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#52e7c3';
        ctx.font = '900 10px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('PREVIEW', point.x, point.y - radiusValue - 38);
        ctx.restore();
      }

      ctx.fillStyle = color;
      ctx.globalAlpha = active ? 0.16 : 0.08;
      ctx.beginPath();
      ctx.arc(point.x, point.y, radiusValue + 24, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;

      ctx.shadowColor = color;
      ctx.shadowBlur = active ? 34 : 20;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(point.x, point.y, radiusValue, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;

      ctx.fillStyle = active ? 'rgba(11,14,24,0.92)' : 'rgba(8,11,19,0.78)';
      ctx.beginPath();
      ctx.arc(point.x, point.y, Math.max(12, radiusValue - 13), 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = '#f4f7ff';
      ctx.font = '900 16px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(String(score), point.x, point.y + 5);

      if (!showLabel) return;
      ctx.fillStyle = '#e2e4ec';
      ctx.font = '800 14px sans-serif';
      ctx.fillText(label, point.x, point.y + radiusValue + 22);
      ctx.fillStyle = 'rgba(226,228,236,0.66)';
      ctx.font = '12px sans-serif';
      ctx.fillText(meta, point.x, point.y + radiusValue + 39);
    }

    ctx.strokeStyle = 'rgba(255,255,255,0.07)';
    ctx.lineWidth = 2;
    [0.65, 0.86, 1.08].forEach(scale => {
      ctx.beginPath();
      ctx.ellipse(cx, cy, radius * scale * 1.45, radius * scale, 0, 0, Math.PI * 2);
      ctx.stroke();
    });

    const points = scans.map((scan, i) => {
      const angle = -Math.PI / 2 + (Math.PI * 2 * i / Math.max(1, scans.length));
      const point = positioned(`board:${scan.board.id}`, {
        x: cx + Math.cos(angle) * radius * 1.45,
        y: cy + Math.sin(angle) * radius,
      });
      return {
        scan,
        x: point.x,
        y: point.y,
        angle,
      };
    });

    const aggregate = aggregateScan(scans);
    const rootTone = toneFor(aggregate.pressure);
    const rootPoint = positioned('root', { x: cx, y: cy });
    state.graphHitNodes.push({ type: 'root', key: 'root', x: rootPoint.x, y: rootPoint.y, r: 42 });

    points.forEach(point => {
      const active = expandedIds.has(String(point.scan.board.id));
      line(rootPoint, point, active ? point.scan.tone : 'stable', active ? 5 : 2, active ? 0.64 : 0.26);
    });

    expandedScans.forEach(focusedScan => {
      const base = points.find(point => String(point.scan.board.id) === String(focusedScan.board.id));
      const focusedGroups = groups.filter(group => String(group.board.id) === String(focusedScan.board.id));
      const limit = graphGroupLimitCount();
      const groupLimit = focusedGroups.slice(0, Number.isFinite(limit) ? limit : focusedGroups.length);
      groupLimit.forEach((group, idx) => {
        const spread = Math.PI * (expandedScans.length > 1 ? 0.95 : 1.34);
        const start = (base?.angle || -Math.PI / 2) - spread / 2;
        const angle = start + (spread * (idx + 0.5) / Math.max(1, groupLimit.length));
        const gr = radius * (expandedScans.length > 1 ? 1.38 : 1.48);
        const defaultPoint = {
          x: cx + Math.cos(angle) * gr * 1.38,
          y: cy + Math.sin(angle) * gr * 0.82,
        };
        const key = `group:${graphGroupKey(group)}`;
        const point = positioned(key, defaultPoint);
        const active = graphGroupKey(group) === state.graphFocusGroupKey;
        const color = colorFor(group.tone);
        line(base || rootPoint, point, group.tone, active ? 4 : 1.4 + group.pressure / 34, active ? 0.76 : 0.38);
        const groupScore = graphLensValue(group);
        const groupRadius = 14 + Math.min(22, (groupScore / maxGroupLens) * 22);
        const groupKey = graphGroupKey(group);
        const showGroupLabel = state.graphLabels === 'all' ||
          active ||
          previewTarget?.groupKey === groupKey ||
          expandedScans.length <= 1;
        node(
          point,
          groupRadius,
          color,
          group.label,
          graphLensMeta(group, `${group.courses.length} courses`),
          groupScore,
          active,
          previewTarget?.groupKey === groupKey,
          showGroupLabel
        );
        state.graphHitNodes.push({
          type: 'group',
          key,
          boardId: String(group.board.id),
          groupKey: graphGroupKey(group),
          x: point.x,
          y: point.y,
          r: groupRadius + 18,
        });
      });
    });

    points.forEach(point => {
      const scan = point.scan;
      const color = colorFor(scan.tone);
      const scanScore = graphLensValue(scan);
      const nodeRadius = 24 + (scanScore / maxBoardLens) * 34;
      const active = expandedIds.has(String(scan.board.id));
      node(
        point,
        nodeRadius,
        color,
        scan.board.label,
        graphLensMeta(scan, `${scan.students} students`),
        scanScore,
        active,
        previewTarget?.boardId === String(scan.board.id)
      );
      state.graphHitNodes.push({
        type: 'board',
        key: `board:${scan.board.id}`,
        boardId: String(scan.board.id),
        x: point.x,
        y: point.y,
        r: nodeRadius + 18,
      });
    });

    const rootColor = colorFor(rootTone);
    ctx.shadowColor = rootColor;
    ctx.shadowBlur = 24;
    ctx.fillStyle = rootColor;
    ctx.beginPath();
    ctx.arc(rootPoint.x, rootPoint.y, 42, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.fillStyle = 'rgba(8,11,19,0.86)';
    ctx.beginPath();
    ctx.arc(rootPoint.x, rootPoint.y, 29, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#f4f7ff';
    ctx.font = '900 15px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('MRI', rootPoint.x, rootPoint.y + 5);
    ctx.fillStyle = 'rgba(226,228,236,0.72)';
    ctx.font = '12px sans-serif';
    ctx.fillText(
      expandedIds.size ? `${expandedIds.size} terms expanded | lens: ${graphLensLabel()}` : `lens: ${graphLensLabel()} | click terms to expand`,
      rootPoint.x,
      rootPoint.y + 61
    );
  }

  function graphNodeAt(canvas, event) {
    const point = graphPointer(canvas, event);
    return state.graphHitNodes
      .slice()
      .reverse()
      .find(node => Math.hypot(node.x - point.x, node.y - point.y) <= node.r);
  }

  function graphPointer(canvas, event) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) * (canvas.width / rect.width),
      y: (event.clientY - rect.top) * (canvas.height / rect.height),
    };
  }

  function moveGraphNode(canvas, event) {
    if (!state.graphDragging) return;
    const point = graphPointer(canvas, event);
    const x = Math.max(56, Math.min(canvas.width - 56, point.x - state.graphDragging.dx));
    const y = Math.max(56, Math.min(canvas.height - 74, point.y - state.graphDragging.dy));
    state.graphPositions.set(state.graphDragging.key, { x, y });
    state.graphDidDrag = true;
    drawFlow(currentScans());
    renderGraphDetail(currentScans());
  }

  function stopGraphDrag() {
    state.graphDragging = null;
  }

  function onGraphClick(event) {
    if (state.graphDidDrag) {
      state.graphDidDrag = false;
      return;
    }
    const canvas = $('twmFlowCanvas');
    const hit = graphNodeAt(canvas, event);
    if (!hit) return;
    if (hit.type === 'root') {
      state.graphFocusBoardId = '';
      state.graphFocusGroupKey = '';
      state.graphExpandedBoardIds.clear();
    } else if (hit.type === 'board') {
      if (state.graphExpandedBoardIds.has(hit.boardId)) {
        state.graphExpandedBoardIds.delete(hit.boardId);
        if (state.graphFocusBoardId === hit.boardId) state.graphFocusBoardId = '';
      } else {
        state.graphExpandedBoardIds.add(hit.boardId);
        state.graphFocusBoardId = hit.boardId;
      }
      state.graphFocusGroupKey = '';
    } else if (hit.type === 'group') {
      state.graphExpandedBoardIds.add(hit.boardId);
      state.graphFocusBoardId = hit.boardId;
      state.graphFocusGroupKey = hit.groupKey;
      state.buildPreviewId = '';
    }
    drawFlow(currentScans());
    renderGraphDetail(currentScans());
    renderBuild(currentScans());
  }

  function selectedMoveTargets(scans) {
    const placement = findMovePlacement(scans);
    const slot = state.moveSlot;
    if (!placement || !slot) return [];
    const targets = [{ placement, day: slot.day, start: slot.start, end: slot.end }];
    if (slot.pairId) {
      const companion = findPlacementById(scans, slot.pairId);
      if (companion) {
        targets.push({
          placement: companion,
          day: slot.pairDay || slot.day,
          start: slot.pairStart,
          end: slot.pairEnd,
        });
      }
    }
    return targets.filter(target => target.day && target.start && target.end);
  }

  function orderedMoveTargets(targets) {
    if (targets.length < 2) return targets;
    const [first, second] = targets;
    const firstNeedsSecondVacated = sameSlot(first, second.placement);
    const secondNeedsFirstVacated = sameSlot(second, first.placement);
    if (firstNeedsSecondVacated && !secondNeedsFirstVacated) return [second, first];
    if (secondNeedsFirstVacated && !firstNeedsSecondVacated) return [first, second];
    return targets;
  }

  async function applySelectedMove() {
    const scans = currentScans();
    const targets = selectedMoveTargets(scans);
    const placement = targets[0]?.placement;
    const slot = state.moveSlot;
    if (!placement || !slot || !targets.length) return;
    if (targets.some(target => target.placement.is_locked)) {
      state.moveMessage = 'Move blocked: one section in this instructor bundle is locked.';
      refreshGraphOnly();
      return;
    }
    const previous = {
      boardId: state.boardId,
      focusBoard: state.graphFocusBoardId,
      focusGroup: state.graphFocusGroupKey,
      placementId: String(placement.id),
    };
    const undoActions = targets.map(target => ({
      placement_id: target.placement.id,
      course_code: target.placement.course_code,
      section: target.placement.section,
      old_day: target.placement.day,
      old_start: target.placement.start_time,
      old_end: target.placement.end_time,
      old_room: target.placement.room || '',
      new_day: target.day,
      new_start: target.start,
      new_end: target.end,
      new_room: target.placement.room || '',
    }));
    const moved = [];
    try {
      for (const target of orderedMoveTargets(targets)) {
        if (sameSlot(target, target.placement)) continue;
        const data = await api(`/ops/tw/placements/${target.placement.id}/move/`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            day: target.day,
            start_time: target.start,
            end_time: target.end,
            room: target.placement.room || '',
          }),
        });
        moved.push(data);
      }
      const criticalCount = moved.reduce((sum, data) => sum + ((data.validation || {}).critical_count || 0), 0);
      const movedCount = moved.length || 1;
      const message = criticalCount > 0
        ? `Moved ${movedCount} section(s) with ${criticalCount} conflict(s). MRI refreshed.`
        : `Moved ${movedCount} section(s) cleanly. MRI refreshed.`;
      await loadScenario(state.scenarioId);
      state.boardId = previous.boardId;
      if ($('twmBoard')) $('twmBoard').value = previous.boardId;
      state.graphFocusBoardId = previous.focusBoard;
      state.graphFocusGroupKey = previous.focusGroup;
      if (previous.focusBoard) state.graphExpandedBoardIds.add(previous.focusBoard);
      state.movePlacementId = previous.placementId;
      state.moveSlot = null;
      state.moveSlotFilter = 'all';
      state.moveMessage = message;
      state.moveUndo = {
        actions: undoActions,
        label: undoActions.map(action => `${action.course_code} ${action.section}`).join(' + '),
        previous,
      };
      render();
    } catch (err) {
      const errorMessage = moved.length
        ? `Move partially applied, then failed: ${err.message || 'Move failed.'}`
        : err.message || 'Move failed.';
      await loadScenario(state.scenarioId);
      state.boardId = previous.boardId;
      if ($('twmBoard')) $('twmBoard').value = previous.boardId;
      state.graphFocusBoardId = previous.focusBoard;
      state.graphFocusGroupKey = previous.focusGroup;
      if (previous.focusBoard) state.graphExpandedBoardIds.add(previous.focusBoard);
      state.movePlacementId = previous.placementId;
      state.moveMessage = errorMessage;
      render();
    }
  }

  async function undoLastMove() {
    const undo = state.moveUndo;
    if (!undo?.actions?.length) return;
    const previous = undo.previous || {
      boardId: state.boardId,
      focusBoard: state.graphFocusBoardId,
      focusGroup: state.graphFocusGroupKey,
      placementId: state.movePlacementId,
    };
    try {
      for (const action of undo.actions.slice().reverse()) {
        await api(`/ops/tw/placements/${action.placement_id}/move/`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            day: action.old_day,
            start_time: action.old_start,
            end_time: action.old_end,
            room: action.old_room || '',
          }),
        });
      }
      await loadScenario(state.scenarioId);
      state.boardId = previous.boardId || '';
      if ($('twmBoard')) $('twmBoard').value = state.boardId;
      state.graphFocusBoardId = previous.focusBoard || '';
      state.graphFocusGroupKey = previous.focusGroup || '';
      if (previous.focusBoard) state.graphExpandedBoardIds.add(previous.focusBoard);
      state.movePlacementId = previous.placementId || '';
      state.moveSlot = null;
      state.moveSlotFilter = 'all';
      state.moveMessage = 'Last MRI move undone. MRI refreshed.';
      state.moveUndo = null;
      render();
    } catch (err) {
      state.moveMessage = err.message || 'Undo failed.';
      refreshGraphOnly();
    }
  }

  function onGraphPointerDown(event) {
    const canvas = $('twmFlowCanvas');
    const hit = graphNodeAt(canvas, event);
    if (!hit?.key) return;
    const point = graphPointer(canvas, event);
    state.graphDragging = {
      key: hit.key,
      dx: point.x - hit.x,
      dy: point.y - hit.y,
    };
    state.graphDidDrag = false;
  }

  function updateBackLink() {
    const params = new URLSearchParams();
    if (state.scenarioId) params.set('scenario', state.scenarioId);
    if (state.boardId) params.set('board', state.boardId);
    $('twmBack').href = `/timetable-workspace/${params.toString() ? '?' + params.toString() : ''}`;
  }

  function bind() {
    $('twmYear').addEventListener('change', loadScenarios);
    $('twmTerm').addEventListener('change', loadScenarios);
    $('twmScenario').addEventListener('change', e => loadScenario(e.target.value));
    $('twmBoard').addEventListener('change', e => {
      state.boardId = e.target.value;
      state.graphFocusBoardId = state.boardId || '';
      state.graphFocusGroupKey = '';
      setSingleGraphExpansion(state.boardId);
      render();
    });
    const flowCanvas = $('twmFlowCanvas');
    $('twmGraphExpandAll').addEventListener('click', () => {
      currentScans().forEach(scan => state.graphExpandedBoardIds.add(String(scan.board.id)));
      state.graphFocusBoardId = '';
      state.graphFocusGroupKey = '';
      refreshGraphOnly();
    });
    $('twmGraphCollapseAll').addEventListener('click', () => {
      state.graphExpandedBoardIds.clear();
      state.graphFocusBoardId = '';
      state.graphFocusGroupKey = '';
      refreshGraphOnly();
    });
    $('twmGraphResetLayout').addEventListener('click', () => {
      state.graphPositions.clear();
      state.graphDragging = null;
      state.graphDidDrag = false;
      refreshGraphOnly();
    });
    $('twmGraphGroupLimit')?.addEventListener('change', e => {
      state.graphGroupLimit = e.target.value || '6';
      state.graphPositions.clear();
      refreshGraphOnly();
    });
    $('twmGraphLabels')?.addEventListener('click', () => {
      state.graphLabels = state.graphLabels === 'all' ? 'focus' : 'all';
      refreshGraphOnly();
    });
    document.querySelectorAll('[data-twm-graph-lens]').forEach(btn => {
      btn.addEventListener('click', () => {
        state.graphLens = btn.dataset.twmGraphLens || 'pressure';
        document.querySelectorAll('[data-twm-graph-lens]').forEach(item => {
          item.classList.toggle('active', item === btn);
        });
        refreshGraphOnly();
      });
    });
    $('twmGenome').addEventListener('click', event => {
      const filterBtn = event.target.closest('[data-twm-genome-filter]');
      if (filterBtn) {
        state.genomeFilter = filterBtn.dataset.twmGenomeFilter || 'all';
        renderGenome(currentScans());
        return;
      }
      const geneBtn = event.target.closest('[data-twm-gene-placement]');
      if (!geneBtn) return;
      const scans = currentScans();
      const placementId = geneBtn.dataset.twmGenePlacement || '';
      startRepairSelection(scans, placementId, 'DNA selected this section. Move Section is ready.');
    });
    $('twmBuildMoves').addEventListener('click', event => {
      if (event.target.closest('[data-twm-undo-move]')) {
        undoLastMove();
        return;
      }
      const returnBtn = event.target.closest('[data-twm-repair-return]');
      if (returnBtn) {
        const trailIdx = Number(returnBtn.dataset.twmRepairReturn || -1);
        const item = state.moveRepairTrail[trailIdx];
        if (item) {
          const placement = findPlacementById(currentScans(), item.fromPlacementId);
          if (placement) focusPlacementGroup(placement);
          state.movePlacementId = item.fromPlacementId || '';
          state.moveSlot = item.fromSlot ? { ...item.fromSlot } : null;
          state.moveSlotFilter = 'all';
          state.moveRepairTrail = state.moveRepairTrail.slice(0, trailIdx);
          state.moveMessage = `Returned to ${item.fromLabel || 'the original section'} from repair trail.`;
          refreshGraphOnly();
        }
        return;
      }
      if (event.target.closest('[data-twm-repair-clear]')) {
        state.moveRepairTrail = [];
        state.moveMessage = 'Repair trail cleared.';
        refreshGraphOnly();
        return;
      }
      const repairBtn = event.target.closest('[data-twm-repair-placement]');
      if (repairBtn) {
        const scans = currentScans();
        const placementId = repairBtn.dataset.twmRepairPlacement || '';
        startRepairSelection(scans, placementId);
        return;
      }
      const placementBtn = event.target.closest('[data-twm-move-placement]');
      if (placementBtn) {
        const scans = currentScans();
        const placementId = placementBtn.dataset.twmMovePlacement || '';
        const placement = findPlacementById(scans, placementId);
        if (placementBtn.dataset.twmRepairJump) {
          pushMoveRepairTrail(scans, placement, placementBtn.dataset.twmRepairReason || 'slot blocker');
          if (placement) focusPlacementGroup(placement);
        }
        state.movePlacementId = placementId;
        state.moveSlot = null;
        state.moveSlotFilter = 'all';
        state.moveMessage = placementBtn.dataset.twmRepairJump && placement
          ? `Inspecting ${placementLabel(placement)} because it blocks the previous candidate.`
          : '';
        refreshGraphOnly();
        return;
      }
      const slotFilterBtn = event.target.closest('[data-twm-slot-filter]');
      if (slotFilterBtn) {
        state.moveSlotFilter = slotFilterBtn.dataset.twmSlotFilter || 'all';
        const scans = currentScans();
        const selected = findMovePlacement(scans);
        const slots = selected ? candidateMoveSlots(selected) : [];
        if (state.moveSlot && !filteredMoveSlots(slots).some(slot => moveSlotActive(slot, state.moveSlot))) {
          state.moveSlot = null;
        }
        state.moveMessage = '';
        refreshGraphOnly();
        return;
      }
      const slotBtn = event.target.closest('[data-twm-move-day]');
      if (slotBtn) {
        state.moveSlot = {
          day: slotBtn.dataset.twmMoveDay || '',
          start: slotBtn.dataset.twmMoveStart || '',
          end: slotBtn.dataset.twmMoveEnd || '',
          critical: Number(slotBtn.dataset.twmMoveCritical || 0),
          warning: Number(slotBtn.dataset.twmMoveWarning || 0),
          tone: Number(slotBtn.dataset.twmMoveCritical || 0) ? 'critical' : Number(slotBtn.dataset.twmMoveWarning || 0) ? 'watch' : 'stable',
          pairId: slotBtn.dataset.twmMovePairId || '',
          pairDay: slotBtn.dataset.twmMovePairDay || '',
          pairStart: slotBtn.dataset.twmMovePairStart || '',
          pairEnd: slotBtn.dataset.twmMovePairEnd || '',
          pairSection: slotBtn.dataset.twmMovePairSection || '',
          pairRelation: slotBtn.dataset.twmMovePairRelation || '',
        };
        state.moveMessage = '';
        refreshGraphOnly();
        return;
      }
      if (event.target.closest('[data-twm-apply-move]')) {
        applySelectedMove();
        return;
      }
      const btn = event.target.closest('[data-twm-build-preview]');
      if (!btn) return;
      state.buildPreviewId = state.buildPreviewId === btn.dataset.twmBuildPreview ? '' : btn.dataset.twmBuildPreview;
      applyBuildPreviewFocus();
      refreshGraphOnly();
    });
    flowCanvas.addEventListener('mousedown', onGraphPointerDown);
    flowCanvas.addEventListener('click', onGraphClick);
    flowCanvas.addEventListener('mousemove', event => {
      if (state.graphDragging) {
        moveGraphNode(flowCanvas, event);
        flowCanvas.style.cursor = 'grabbing';
      } else {
        flowCanvas.style.cursor = graphNodeAt(flowCanvas, event) ? 'grab' : 'default';
      }
    });
    flowCanvas.addEventListener('mouseup', stopGraphDrag);
    flowCanvas.addEventListener('mouseleave', stopGraphDrag);
    document.querySelectorAll('.twm-mode-rail [data-twm-mode]').forEach(btn => {
      btn.addEventListener('click', () => {
        state.mode = btn.dataset.twmMode || 'system';
        applyMode({ scroll: true });
      });
    });
  }

  function modeTargetId(mode) {
    return ({
      system: 'twmSectionSystem',
      groups: 'twmSectionGroups',
      students: 'twmSectionStudents',
      actions: 'twmSectionActions',
      build: 'twmSectionBuild',
    })[mode] || 'twmSectionSystem';
  }

  function applyMode(options = {}) {
    document.body.dataset.twmMode = state.mode;
    document.querySelectorAll('.twm-mode-rail [data-twm-mode]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.twmMode === state.mode);
    });
    if (options.scroll) {
      requestAnimationFrame(() => {
        $(modeTargetId(state.mode))?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
  }

  bind();
  loadScenarios().catch(err => {
    console.error(err);
    renderEmpty(err.message || 'Failed to load MRI page.');
  });
})();
