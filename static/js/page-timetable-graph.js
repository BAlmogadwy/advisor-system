(function () {
  const cfg = window.twGraphConfig || {};
  const $ = id => document.getElementById(id);
  const PLAN_EDGE_TYPES = new Set([
    'HAS_PROGRAM',
    'HAS_PLAN_TERM',
    'HAS_GROUP',
    'SCHEDULES_COURSE',
    'HAS_SECTION',
    'HAS_ENROLLED_STUDENT',
  ]);
  const PLAN_CHILD_LABELS = {
    TTScenario: 'programs',
    TTProgram: 'terms',
    TTPlanTerm: 'groups',
    TTBoard: 'courses',
    TTCourse: 'sections',
    TTSection: 'students',
  };
  const state = {
    scenarios: [],
    selectedScenarioId: String(cfg.initialScenario || ''),
    summary: null,
    graphMode: 'plan',
    graphData: { nodes: [], edges: [] },
    graphLayout: new Map(),
    graphDrag: null,
    graphViewer: null,
    graphSelection: { item: null, isEdge: false },
    pendingFocusNodeId: '',
    sectionOps: new Map(),
    planFilters: {
      program: '',
      planTerm: '',
      includeStudents: true,
      options: { programs: [], terms_by_program: {} },
    },
    planExplore: {
      expanded: new Set(),
      selectedNodeId: '',
      childrenByParent: new Map(),
      parentByChild: new Map(),
      preferredParentByChild: new Map(),
    },
  };

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[ch]));
  }

  async function api(url, options = {}) {
    const headers = {
      'X-CSRFToken': cfg.csrfToken || '',
      ...(options.headers || {}),
    };
    const res = await fetch(url, { ...options, headers });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      const message = data.error?.message || `${res.status} ${res.statusText}`;
      throw new Error(message);
    }
    return data;
  }

  function setBusy(buttonId, busy) {
    const button = $(buttonId);
    if (!button) return;
    button.disabled = Boolean(busy);
    button.classList.toggle('is-busy', Boolean(busy));
  }

  function setScenarioStatus(label, tone = 'idle') {
    const pill = $('tgScenarioStatus');
    if (!pill) return;
    pill.textContent = label;
    pill.dataset.tone = tone;
  }

  function setStatusMessage(message) {
    const target = $('tgNeo4jMessage');
    if (target) target.textContent = message;
  }

  function setGraphToolStatus(message, tone = '') {
    const target = $('tgGraphToolStatus');
    if (!target) return;
    target.textContent = message;
    if (tone) {
      target.dataset.tone = tone;
    } else {
      delete target.dataset.tone;
    }
  }

  function urlFor(template, scenarioId) {
    return String(template || '').replace('__SCENARIO_ID__', encodeURIComponent(scenarioId));
  }

  function placementUrlFor(template, placementId) {
    return String(template || '').replace('__PLACEMENT_ID__', encodeURIComponent(placementId));
  }

  async function checkStatus() {
    try {
      const data = await api(cfg.statusUrl);
      renderStatus(data.neo4j || {});
    } catch (err) {
      renderStatus({
        configured: false,
        driver_installed: false,
        connected: false,
        uri: '-',
        database: '-',
        message: err.message,
      });
    }
  }

  function renderStatus(status) {
    const connected = Boolean(status.connected);
    const configured = Boolean(status.configured);
    const pill = $('tgNeo4jPill');
    if (pill) {
      pill.textContent = connected ? 'Connected' : configured ? 'Not connected' : 'Not configured';
      pill.dataset.tone = connected ? 'good' : configured ? 'warn' : 'idle';
    }
    $('tgDriver').textContent = status.driver_installed ? 'Installed' : 'Missing';
    $('tgUri').textContent = status.uri || '-';
    $('tgDatabase').textContent = status.database || '-';
    setStatusMessage(status.message || 'Neo4j status checked.');
  }

  async function loadScenarios() {
    setBusy('tgLoadScenarios', true);
    setScenarioStatus('Loading', 'idle');
    const year = $('tgYear').value.trim();
    const term = $('tgTerm').value.trim();
    try {
      const params = new URLSearchParams();
      if (year) params.set('year', year);
      if (term) params.set('term', term);
      const data = await api(`${cfg.scenariosUrl}?${params.toString()}`);
      state.scenarios = data.scenarios || [];
      renderScenarioOptions();
      if (state.selectedScenarioId) {
        await previewScenario(state.selectedScenarioId);
      } else if (state.scenarios.length) {
        state.selectedScenarioId = String(state.scenarios[0].id);
        $('tgScenario').value = state.selectedScenarioId;
        await previewScenario(state.selectedScenarioId);
      } else {
        renderEmpty('No scenarios found for this year and term.');
      }
    } catch (err) {
      renderEmpty(err.message || 'Could not load scenarios.');
    } finally {
      setBusy('tgLoadScenarios', false);
    }
  }

  function renderScenarioOptions() {
    const select = $('tgScenario');
    select.innerHTML = '<option value="">Select scenario</option>';
    state.scenarios.forEach(scenario => {
      const option = document.createElement('option');
      option.value = scenario.id;
      option.textContent = `${scenario.name} (${scenario.status})`;
      select.appendChild(option);
    });
    if (
      state.selectedScenarioId &&
      state.scenarios.some(scenario => String(scenario.id) === state.selectedScenarioId)
    ) {
      select.value = state.selectedScenarioId;
    } else {
      state.selectedScenarioId = '';
    }
  }

  async function previewScenario(scenarioId) {
    if (!scenarioId) {
      renderEmpty('Select a scenario to preview its graph twin.');
      return;
    }
    setBusy('tgPreview', true);
    setScenarioStatus('Previewing', 'idle');
    try {
      const data = await api(urlFor(cfg.summaryUrlTemplate, scenarioId));
      state.summary = data;
      resetPlanExplore();
      renderSummary(data);
      await loadGraphView(state.graphMode);
      setScenarioStatus('Preview ready', 'good');
    } catch (err) {
      renderEmpty(err.message || 'Could not preview graph.');
      setScenarioStatus('Preview failed', 'warn');
    } finally {
      setBusy('tgPreview', false);
    }
  }

  async function syncScenario() {
    const scenarioId = $('tgScenario').value;
    if (!scenarioId) {
      renderEmpty('Select a scenario before syncing Neo4j.');
      return;
    }
    setBusy('tgSync', true);
    setScenarioStatus('Syncing', 'idle');
    try {
      const data = await api(urlFor(cfg.syncUrlTemplate, scenarioId), { method: 'POST' });
      state.summary = data;
      renderSummary(data);
      setScenarioStatus('Synced to Neo4j', 'good');
      setStatusMessage(`Neo4j sync completed at ${data.synced_at || 'now'}.`);
      await checkStatus();
    } catch (err) {
      setScenarioStatus('Sync blocked', 'warn');
      setStatusMessage(err.message || 'Neo4j sync failed.');
    } finally {
      setBusy('tgSync', false);
    }
  }

  function renderEmpty(message) {
    $('tgScenarioName').textContent = 'No scenario selected';
    $('tgMetrics').innerHTML = '';
    $('tgNodeCounts').innerHTML = '';
    $('tgRelCounts').innerHTML = '';
    $('tgSamples').innerHTML = `<div class="tg-empty">${esc(message)}</div>`;
    $('tgSampleCount').textContent = '0 rows';
    clearGraphExplorer(message);
    setScenarioStatus('Idle', 'idle');
  }

  function renderSummary(data) {
    const scenario = data.scenario || {};
    const summary = data.summary || {};
    $('tgScenarioName').textContent = scenario.name || `Scenario ${scenario.id || ''}`;
    renderMetrics(summary);
    renderCounts('tgNodeCounts', summary.node_counts || {});
    renderCounts('tgRelCounts', summary.relationship_counts || {});
    renderSamples(data.samples?.relationships || data.relationships || []);
  }

  function renderMetrics(summary) {
    const metrics = [
      ['Nodes', summary.node_count || 0],
      ['Relationships', summary.relationship_count || 0],
      ['Students', summary.students || 0],
      ['Courses', summary.courses || 0],
      ['Boards', summary.boards || 0],
      ['Placements', summary.placements || 0],
    ];
    $('tgMetrics').innerHTML = metrics.map(([label, value]) => `
      <div class="tg-metric">
        <span>${esc(label)}</span>
        <strong>${esc(value)}</strong>
      </div>
    `).join('');
  }

  function renderCounts(targetId, counts) {
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    const target = $(targetId);
    if (!entries.length) {
      target.innerHTML = '<div class="tg-empty small">No rows yet.</div>';
      return;
    }
    target.innerHTML = entries.map(([label, value]) => `
      <div class="tg-count">
        <span>${esc(label)}</span>
        <strong>${esc(value)}</strong>
      </div>
    `).join('');
  }

  function renderSamples(relationships) {
    const rows = relationships.slice(0, 18);
    $('tgSampleCount').textContent = `${rows.length} rows`;
    if (!rows.length) {
      $('tgSamples').innerHTML = '<div class="tg-empty">No relationship samples yet.</div>';
      return;
    }
    $('tgSamples').innerHTML = rows.map(rel => {
      const props = rel.props || {};
      const detail = props.kind || props.link_type || props.source || props.program || '';
      return `
        <div class="tg-sample-row">
          <span>${esc(rel.start_label)}<b>${esc(rel.start_key)}</b></span>
          <strong>${esc(rel.type)}</strong>
          <span>${esc(rel.end_label)}<b>${esc(rel.end_key)}</b></span>
          ${detail ? `<em>${esc(detail)}</em>` : ''}
        </div>
      `;
    }).join('');
  }

  async function loadGraphView(mode) {
    const scenarioId = $('tgScenario').value || state.selectedScenarioId;
    if (!scenarioId) {
      clearGraphExplorer('Select a scenario to load a graph view.');
      return;
    }
    state.graphMode = mode || 'clashes';
    try {
      const limit = state.graphMode === 'plan' ? 900 : 90;
      const params = new URLSearchParams({
        mode: state.graphMode,
        limit: String(limit),
      });
      if (state.graphMode === 'plan') {
        params.set('progressive', '1');
        if (state.planFilters.program) params.set('program', state.planFilters.program);
        if (state.planFilters.planTerm) params.set('plan_term', state.planFilters.planTerm);
        params.set('include_students', state.planFilters.includeStudents ? '1' : '0');
      }
      const url = `${urlFor(cfg.viewUrlTemplate, scenarioId)}?${params.toString()}`;
      const data = await api(url);
      state.graphData = { ...data, nodes: data.nodes || [], edges: data.edges || [] };
      state.graphSelection = { item: null, isEdge: false };
      state.pendingFocusNodeId = '';
      if (state.graphMode === 'plan') resetPlanExplore();
      renderPlanControls(data);
      renderGraphExplorer(data);
    } catch (err) {
      clearGraphExplorer(err.message || 'Could not load graph view.');
    }
  }

  function renderPlanControls(data) {
    const controls = $('tgPlanControls');
    if (!controls) return;
    const isPlan = (data?.mode || state.graphMode) === 'plan';
    controls.classList.toggle('is-hidden', !isPlan);
    renderGraphTools();
    if (!isPlan) return;

    state.planFilters.options = data.filters || state.planFilters.options;
    const programs = state.planFilters.options.programs || [];
    const termsByProgram = state.planFilters.options.terms_by_program || {};
    const programSelect = $('tgPlanProgram');
    const termSelect = $('tgPlanTerm');
    if (programSelect) {
      programSelect.innerHTML = [
        '<option value="">All programs</option>',
        ...programs.map(program => (
          `<option value="${esc(program)}"${program === state.planFilters.program ? ' selected' : ''}>${esc(program)}</option>`
        )),
      ].join('');
    }
    if (termSelect) {
      const termSet = new Set();
      if (state.planFilters.program) {
        (termsByProgram[state.planFilters.program] || []).forEach(term => termSet.add(term));
      } else {
        Object.values(termsByProgram).forEach(terms => terms.forEach(term => termSet.add(term)));
      }
      const terms = Array.from(termSet).sort((a, b) => Number(a) - Number(b));
      termSelect.innerHTML = [
        '<option value="">All terms</option>',
        ...terms.map(term => (
          `<option value="${esc(term)}"${term === state.planFilters.planTerm ? ' selected' : ''}>Term ${esc(term)}</option>`
        )),
      ].join('');
    }
    const studentsToggle = $('tgPlanStudents');
    if (studentsToggle) studentsToggle.checked = state.planFilters.includeStudents;
    renderTreeSummary(data);
    renderGraphTools();
  }

  function renderGraphTools() {
    const isPlan = state.graphMode === 'plan';
    const hasNodeSelection = Boolean(state.graphSelection.item && !state.graphSelection.isEdge);
    const selectedId = hasNodeSelection ? state.graphSelection.item.id : state.planExplore.selectedNodeId;
    const canExpand = isPlan && selectedId && getPlanChildren(selectedId).length && !state.planExplore.expanded.has(selectedId);
    const canCollapse = isPlan && selectedId && state.planExplore.expanded.has(selectedId);
    const expandButton = $('tgGraphExpand');
    const collapseButton = $('tgGraphCollapse');
    if (expandButton) expandButton.disabled = !canExpand;
    if (collapseButton) collapseButton.disabled = !canCollapse;
  }

  function renderTreeSummary(data) {
    const target = $('tgTreeSummary');
    if (!target) return;
    const tree = data?.tree || {};
    const counts = tree.visible_node_counts || tree.node_counts || {};
    const fullCounts = tree.source_node_counts || tree.node_counts || {};
    const parts = [
      ['Scenarios', counts.TTScenario],
      ['Programs', counts.TTProgram],
      ['Terms', counts.TTPlanTerm],
      ['Groups', counts.TTBoard],
      ['Courses', counts.TTCourse],
      ['Sections', counts.TTSection],
      ['Students', counts.TTStudent],
    ].filter(([, value]) => value);
    const totalVisible = Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0);
    const totalFull = Object.values(fullCounts).reduce((sum, value) => sum + Number(value || 0), 0);
    const hidden = Math.max(0, totalFull - totalVisible);
    const selected = state.planExplore.selectedNodeId
      ? (state.graphData.nodes || []).find(node => node.id === state.planExplore.selectedNodeId)
      : null;
    const nextCount = selected ? getPlanChildren(selected.id).length : 0;
    const nextText = selected && nextCount
      ? ` · next ${nextCount} ${PLAN_CHILD_LABELS[selected.type] || 'nodes'}`
      : '';
    target.textContent = parts.length
      ? `${parts.map(([label, value]) => `${value} ${label}`).join(' -> ')}${hidden ? ` · ${hidden} hidden` : ''}${nextText}`
      : 'Scenario';
  }

  function resetPlanExplore() {
    state.planExplore.expanded = new Set();
    state.planExplore.selectedNodeId = '';
    state.planExplore.childrenByParent = new Map();
    state.planExplore.parentByChild = new Map();
    state.planExplore.preferredParentByChild = new Map();
  }

  function countByType(nodes) {
    return nodes.reduce((counts, node) => {
      counts[node.type] = (counts[node.type] || 0) + 1;
      return counts;
    }, {});
  }

  function indexPlanTree(data) {
    const nodesById = new Map((data.nodes || []).map(node => [node.id, node]));
    const childrenByParent = new Map();
    const parentByChild = new Map();
    (data.edges || []).forEach(edge => {
      if (!PLAN_EDGE_TYPES.has(edge.type) || !nodesById.has(edge.source) || !nodesById.has(edge.target)) return;
      if (!childrenByParent.has(edge.source)) childrenByParent.set(edge.source, []);
      childrenByParent.get(edge.source).push(edge);
      if (!parentByChild.has(edge.target)) parentByChild.set(edge.target, edge.source);
    });
    childrenByParent.forEach(edges => {
      edges.sort((a, b) => {
        const nodeA = nodesById.get(a.target);
        const nodeB = nodesById.get(b.target);
        return String(nodeA?.label || a.target).localeCompare(String(nodeB?.label || b.target));
      });
    });
    state.planExplore.childrenByParent = childrenByParent;
    state.planExplore.parentByChild = parentByChild;
    return { nodesById, childrenByParent, parentByChild };
  }

  function getPlanRoot(data, nodesById, parentByChild) {
    return (data.nodes || []).find(node => node.type === 'TTScenario')
      || (data.nodes || []).find(node => !parentByChild.has(node.id))
      || (data.nodes || [])[0]
      || null;
  }

  function getPlanChildren(nodeId) {
    return state.planExplore.childrenByParent.get(nodeId) || [];
  }

  function buildVisiblePlanGraph(data) {
    const { nodesById, childrenByParent, parentByChild } = indexPlanTree(data);
    const root = getPlanRoot(data, nodesById, parentByChild);
    if (!root) return data;

    const visibleNodes = new Map();
    const visibleEdges = new Map();

    function addNode(nodeId) {
      const node = nodesById.get(nodeId);
      if (!node) return;
      const children = childrenByParent.get(nodeId) || [];
      visibleNodes.set(nodeId, {
        ...node,
        expanded: state.planExplore.expanded.has(nodeId),
        expandable: Boolean(children.length),
        selected: state.planExplore.selectedNodeId === nodeId,
      });
    }

    function visit(parentId) {
      if (!state.planExplore.expanded.has(parentId)) return;
      (childrenByParent.get(parentId) || []).forEach(edge => {
        visibleEdges.set(edge.id, edge);
        addNode(edge.target);
        visit(edge.target);
      });
    }

    addNode(root.id);
    visit(root.id);

    const nodes = Array.from(visibleNodes.values());
    const edges = Array.from(visibleEdges.values());
    const fullCounts = data.tree?.node_counts || countByType(data.nodes || []);
    return {
      ...data,
      nodes,
      edges,
      tree: {
        ...(data.tree || {}),
        visible_root: root.id,
        visible_node_counts: countByType(nodes),
        source_node_counts: fullCounts,
      },
    };
  }

  function expandPlanNode(nodeId) {
    const children = getPlanChildren(nodeId);
    if (!children.length || state.planExplore.expanded.has(nodeId)) return false;
    state.planExplore.expanded.add(nodeId);
    return true;
  }

  function collectPlanDescendants(nodeId, descendants = new Set()) {
    getPlanChildren(nodeId).forEach(edge => {
      if (descendants.has(edge.target)) return;
      descendants.add(edge.target);
      collectPlanDescendants(edge.target, descendants);
    });
    return descendants;
  }

  function collapsePlanBranch(nodeId) {
    if (!nodeId) return false;
    const affected = collectPlanDescendants(nodeId);
    affected.add(nodeId);
    let changed = false;
    affected.forEach(id => {
      if (state.planExplore.expanded.delete(id)) changed = true;
    });
    return changed;
  }

  function getSelectedGraphNodeId() {
    if (state.graphSelection.item && !state.graphSelection.isEdge) return state.graphSelection.item.id;
    return state.graphMode === 'plan' ? state.planExplore.selectedNodeId : '';
  }

  function searchablePlanText(node) {
    const meta = node.meta || {};
    return [
      node.label,
      node.id,
      node.type,
      ...Object.values(meta),
    ].join(' ').toLowerCase();
  }

  function findPlanNode(query) {
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) return null;
    const matches = (state.graphData.nodes || [])
      .filter(node => searchablePlanText(node).includes(needle))
      .sort((a, b) => {
        const labelA = String(a.label || '').toLowerCase();
        const labelB = String(b.label || '').toLowerCase();
        const scoreA = labelA === needle ? 0 : labelA.startsWith(needle) ? 1 : labelA.includes(needle) ? 2 : 3;
        const scoreB = labelB === needle ? 0 : labelB.startsWith(needle) ? 1 : labelB.includes(needle) ? 2 : 3;
        return scoreA - scoreB || labelA.localeCompare(labelB);
      });
    return matches[0] || null;
  }

  function revealPlanNode(nodeId) {
    const { parentByChild } = indexPlanTree(state.graphData);
    let current = state.planExplore.preferredParentByChild.get(nodeId) || parentByChild.get(nodeId);
    const seen = new Set();
    while (current && !seen.has(current)) {
      seen.add(current);
      state.planExplore.expanded.add(current);
      current = state.planExplore.preferredParentByChild.get(current) || parentByChild.get(current);
    }
    state.planExplore.selectedNodeId = nodeId;
    state.graphSelection = {
      item: (state.graphData.nodes || []).find(node => node.id === nodeId) || null,
      isEdge: false,
    };
    state.pendingFocusNodeId = nodeId;
    renderGraphExplorer(state.graphData);
  }

  function searchGraph() {
    const query = $('tgGraphSearch')?.value || '';
    if (!query.trim()) {
      state.graphViewer?.clearFocus?.();
      setGraphToolStatus('Type a node name, course, section, or student ID.', 'warn');
      return;
    }
    if (state.graphMode === 'plan') {
      const match = findPlanNode(query);
      if (!match) {
        setGraphToolStatus(`No plan node found for "${query.trim()}".`, 'warn');
        return;
      }
      revealPlanNode(match.id);
      setGraphToolStatus(`Found ${match.label || match.id}. Path opened.`, 'good');
      return;
    }
    const result = state.graphViewer?.search?.(query);
    if (!result?.match) {
      setGraphToolStatus(`No visible node found for "${query.trim()}".`, 'warn');
      return;
    }
    state.graphSelection = { item: result.match, isEdge: false };
    renderGraphDetail(result.match);
    setGraphToolStatus(`Focused ${result.match.label || result.match.id}.`, 'good');
  }

  function clearGraphExplorer(message) {
    destroyGraphViewer();
    state.graphSelection = { item: null, isEdge: false };
    state.pendingFocusNodeId = '';
    $('tgExplorerNodes').innerHTML = '';
    $('tgExplorerEdges').innerHTML = '';
    const canvas = $('tgGraphCanvas');
    const svg = $('tgExplorerSvg');
    if (canvas) {
      canvas.innerHTML = '';
      canvas.classList.add('is-hidden');
    }
    if (svg) {
      svg.classList.remove('is-hidden');
    }
    const empty = $('tgExplorerEmpty');
    empty.textContent = message || 'No graph loaded.';
    empty.classList.remove('is-hidden');
    renderGraphDetail(null);
    renderGraphTools();
    setGraphToolStatus(message || 'No graph loaded.', 'warn');
  }

  function renderGraphExplorer(data) {
    const viewData = state.graphMode === 'plan' ? buildVisiblePlanGraph(data) : data;
    if (state.graphMode === 'plan') renderTreeSummary(viewData);
    const graph = {
      nodes: (viewData.nodes || []).map(node => ({ ...node })),
      edges: (viewData.edges || []).filter(edge => edge.source && edge.target),
    };
    const empty = $('tgExplorerEmpty');
    if (!graph.nodes.length) {
      clearGraphExplorer('This graph lens has no rows for the selected scenario.');
      return;
    }
    empty.classList.add('is-hidden');
    renderGraphTools();
    if (window.TimetableCyGraphViewer && $('tgGraphCanvas')) {
      renderGraphWithCytoscape(viewData, graph);
      return;
    }
    renderGraphWithSvg(viewData, graph);
  }

  function renderGraphWithCytoscape(data, graph) {
    destroyGraphViewer();
    const canvas = $('tgGraphCanvas');
    const svg = $('tgExplorerSvg');
    if (!canvas) {
      renderGraphWithSvg(data, graph);
      return;
    }
    $('tgExplorerEdges').innerHTML = '';
    $('tgExplorerNodes').innerHTML = '';
    canvas.classList.remove('is-hidden');
    if (svg) svg.classList.add('is-hidden');

    try {
      state.graphViewer = new window.TimetableCyGraphViewer(canvas, {
        onReady: counts => {
          if (state.pendingFocusNodeId) {
            const focused = state.graphViewer?.focusNode?.(state.pendingFocusNodeId);
            if (focused) {
              renderGraphDetail(focused);
              state.pendingFocusNodeId = '';
              renderGraphTools();
              return;
            }
            state.pendingFocusNodeId = '';
          }
          const selected = getSelectedPlanNode();
          if (selected) {
            renderGraphDetail(selected);
          } else {
            renderGraphDetail({
              label: state.graphMode === 'plan' ? 'Scenario root loaded' : 'Graph loaded',
              type: data.mode || state.graphMode,
              detail: `Cytoscape explorer with ${counts.nodes} native-labelled nodes and ${counts.edges} relationships.`,
              id: data.scenario?.name || '',
            });
          }
          renderGraphTools();
        },
        onSelect: (item, kind) => handleGraphSelection(item, kind === 'edge'),
        onError: err => {
          console.warn('Cytoscape render failed; using SVG fallback.', err);
          renderGraphWithSvg(data, graph);
        },
      });
      state.graphViewer.render({ ...graph, mode: data.mode || state.graphMode });
      renderGraphDetail(getSelectedPlanNode() || {
        label: state.graphMode === 'plan' ? 'Scenario root' : 'Loading graph',
        type: data.mode || state.graphMode,
        detail: state.graphMode === 'plan'
          ? 'Click a labelled node to expand the timetable tree. Drag any node to adjust the view.'
          : 'The graph renderer is arranging the timetable relationships.',
        id: data.scenario?.name || '',
      });
    } catch (err) {
      console.warn('Cytoscape render failed; using SVG fallback.', err);
      renderGraphWithSvg(data, graph);
    }
  }

  function renderGraphWithSvg(data, graph) {
    destroyGraphViewer();
    const canvas = $('tgGraphCanvas');
    const svg = $('tgExplorerSvg');
    if (canvas) canvas.classList.add('is-hidden');
    if (svg) svg.classList.remove('is-hidden');
    seedGraphPositions(graph.nodes);
    relaxGraph(graph.nodes, graph.edges);
    drawGraph(graph);
    renderGraphDetail({
      label: 'SVG graph loaded',
      type: data.mode || state.graphMode,
      detail: `${data.summary?.nodes || graph.nodes.length} nodes and ${data.summary?.edges || graph.edges.length} relationships. Cytoscape was unavailable, so this screen is using the built-in fallback.`,
      id: data.scenario?.name || '',
    });
  }

  function destroyGraphViewer() {
    if (state.graphViewer && typeof state.graphViewer.destroy === 'function') {
      state.graphViewer.destroy();
    }
    state.graphViewer = null;
  }

  function getSelectedPlanNode() {
    if (state.graphMode !== 'plan' || !state.planExplore.selectedNodeId) return null;
    return (state.graphData.nodes || []).find(node => node.id === state.planExplore.selectedNodeId) || null;
  }

  function handleGraphSelection(item, isEdge = false) {
    if (!item) {
      state.graphSelection = { item: null, isEdge: false };
      if (state.graphMode === 'plan') state.planExplore.selectedNodeId = '';
      renderGraphDetail(null);
      if (state.graphMode === 'plan') renderTreeSummary(buildVisiblePlanGraph(state.graphData));
      renderGraphTools();
      setGraphToolStatus('Selection cleared.');
      return;
    }
    state.graphSelection = { item, isEdge };
    if (isEdge || state.graphMode !== 'plan') {
      renderGraphDetail(item, isEdge);
      renderGraphTools();
      setGraphToolStatus(isEdge ? 'Relationship focused.' : `Focused ${item.label || item.id}.`, 'good');
      return;
    }
    state.planExplore.selectedNodeId = item.id;
    if (expandPlanNode(item.id)) {
      state.pendingFocusNodeId = item.id;
      renderGraphExplorer(state.graphData);
      setGraphToolStatus(`Expanded ${item.label || item.id}.`, 'good');
      return;
    }
    renderGraphDetail(item);
    renderTreeSummary(buildVisiblePlanGraph(state.graphData));
    renderGraphTools();
    setGraphToolStatus(`Focused ${item.label || item.id}.`, 'good');
  }

  function seedGraphPositions(nodes) {
    const typeAnchors = {
      TTStudent: [170, 230],
      TTProgram: [180, 110],
      TTGroup: [190, 350],
      TTCourse: [390, 115],
      TTSection: [475, 240],
      TTSlot: [690, 245],
      TTBoard: [735, 115],
      TTRoom: [740, 340],
      TTInstructor: [615, 355],
      TTScenario: [460, 60],
    };
    nodes.forEach((node, index) => {
      if (state.graphLayout.has(node.id)) return;
      const [ax, ay] = typeAnchors[node.type] || [460, 230];
      const ring = 24 + (index % 7) * 8;
      const angle = index * 2.399;
      state.graphLayout.set(node.id, {
        x: ax + Math.cos(angle) * ring,
        y: ay + Math.sin(angle) * ring,
        vx: 0,
        vy: 0,
      });
    });
  }

  function relaxGraph(nodes, edges) {
    const nodeIds = new Set(nodes.map(node => node.id));
    const links = edges.filter(edge => nodeIds.has(edge.source) && nodeIds.has(edge.target));
    for (let tick = 0; tick < 110; tick += 1) {
      for (let i = 0; i < nodes.length; i += 1) {
        const a = state.graphLayout.get(nodes[i].id);
        for (let j = i + 1; j < nodes.length; j += 1) {
          const b = state.graphLayout.get(nodes[j].id);
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const distSq = Math.max(80, dx * dx + dy * dy);
          const force = 650 / distSq;
          const dist = Math.sqrt(distSq);
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          a.vx += fx; a.vy += fy;
          b.vx -= fx; b.vy -= fy;
        }
      }
      links.forEach(edge => {
        const a = state.graphLayout.get(edge.source);
        const b = state.graphLayout.get(edge.target);
        if (!a || !b) return;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
        const target = edge.type === 'CLASHES_WITH' ? 145 : 118;
        const force = (dist - target) * 0.012;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      });
      nodes.forEach(node => {
        const p = state.graphLayout.get(node.id);
        const anchor = node.type === 'TTSection' ? [480, 235] : [460, 230];
        p.vx += (anchor[0] - p.x) * 0.002;
        p.vy += (anchor[1] - p.y) * 0.002;
        p.x = Math.max(28, Math.min(892, p.x + p.vx));
        p.y = Math.max(28, Math.min(432, p.y + p.vy));
        p.vx *= 0.72;
        p.vy *= 0.72;
      });
    }
  }

  function drawGraph(graph) {
    const nodesById = new Map(graph.nodes.map(node => [node.id, node]));
    const visibleEdges = graph.edges.filter(edge => nodesById.has(edge.source) && nodesById.has(edge.target));
    $('tgExplorerEdges').innerHTML = visibleEdges.map(edge => {
      const source = state.graphLayout.get(edge.source);
      const target = state.graphLayout.get(edge.target);
      return `
        <line class="tg-svg-edge" data-edge-id="${esc(edge.id)}" data-tone="${esc(edge.tone || 'neutral')}"
          x1="${source.x.toFixed(1)}" y1="${source.y.toFixed(1)}"
          x2="${target.x.toFixed(1)}" y2="${target.y.toFixed(1)}">
          <title>${esc(edge.label)} ${esc(edge.detail || '')}</title>
        </line>
      `;
    }).join('');
    $('tgExplorerNodes').innerHTML = graph.nodes.map(node => {
      const p = state.graphLayout.get(node.id);
      const label = String(node.label || '').slice(0, 18);
      return `
        <g class="tg-svg-node" data-node-id="${esc(node.id)}" data-type="${esc(node.type)}"
          transform="translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})">
          <circle r="${Number(node.size || 10) + 5}"></circle>
          <text x="${Number(node.size || 10) + 12}" y="4">${esc(label)}</text>
          <title>${esc(node.type)} ${esc(node.label)} ${esc(node.detail || '')}</title>
        </g>
      `;
    }).join('');
    bindGraphSvg(graph);
  }

  function bindGraphSvg(graph) {
    const svg = $('tgExplorerSvg');
    const nodesById = new Map(graph.nodes.map(node => [node.id, node]));
    svg.querySelectorAll('.tg-svg-node').forEach(nodeEl => {
      nodeEl.addEventListener('pointerdown', event => {
        const nodeId = nodeEl.dataset.nodeId;
        const point = svgPoint(svg, event);
        const current = state.graphLayout.get(nodeId);
        state.graphDrag = { nodeId, dx: current.x - point.x, dy: current.y - point.y };
        nodeEl.setPointerCapture(event.pointerId);
        renderGraphDetail(nodesById.get(nodeId));
      });
      nodeEl.addEventListener('pointermove', event => {
        if (!state.graphDrag || state.graphDrag.nodeId !== nodeEl.dataset.nodeId) return;
        const point = svgPoint(svg, event);
        const current = state.graphLayout.get(state.graphDrag.nodeId);
        current.x = Math.max(28, Math.min(892, point.x + state.graphDrag.dx));
        current.y = Math.max(28, Math.min(432, point.y + state.graphDrag.dy));
        drawGraph(graph);
      });
      nodeEl.addEventListener('pointerup', () => {
        state.graphDrag = null;
      });
      nodeEl.addEventListener('click', () => handleGraphSelection(nodesById.get(nodeEl.dataset.nodeId)));
    });
    svg.querySelectorAll('.tg-svg-edge').forEach(edgeEl => {
      edgeEl.addEventListener('click', () => {
        const edge = graph.edges.find(item => item.id === edgeEl.dataset.edgeId);
        handleGraphSelection(edge, true);
      });
    });
  }

  function svgPoint(svg, event) {
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const matrix = svg.getScreenCTM();
    return matrix ? point.matrixTransform(matrix.inverse()) : { x: event.offsetX, y: event.offsetY };
  }

  function renderGraphDetail(item, isEdge = false) {
    const target = $('tgExplorerDetail');
    if (!item) {
      target.innerHTML = `
        <div class="tg-kicker">Selection</div>
        <h4>No node selected</h4>
        <p>Select a node or relationship to inspect why it exists.</p>
      `;
      return;
    }
    const viewItem = !isEdge && state.graphMode === 'plan' ? enrichPlanDetail(item) : item;
    const metaHtml = renderMetaRows(viewItem.meta || {});
    const pathHtml = !isEdge && state.graphMode === 'plan' ? renderTreePath(viewItem) : '';
    const actionsHtml = renderDetailActions(viewItem, isEdge);
    const evidenceHtml = renderDetailEvidence(viewItem, isEdge);
    target.innerHTML = `
      <div class="tg-kicker">${isEdge ? 'Relationship' : 'Node'}</div>
      <h4>${esc(viewItem.label || viewItem.type || 'Selected item')}</h4>
      <p>${esc(viewItem.detail || (isEdge ? 'Graph relationship from the selected scenario.' : 'Graph node from the selected scenario.'))}</p>
      ${actionsHtml}
      <dl>
        ${pathHtml}
        <div><dt>Type</dt><dd>${esc(viewItem.type || '-')}</dd></div>
        <div><dt>ID</dt><dd>${esc(viewItem.id || '-')}</dd></div>
        ${viewItem.shared_students != null ? `<div><dt>Shared students</dt><dd>${esc(viewItem.shared_students)}</dd></div>` : ''}
        ${viewItem.source ? `<div><dt>Source</dt><dd>${esc(viewItem.source)}</dd></div>` : ''}
        ${viewItem.target ? `<div><dt>Target</dt><dd>${esc(viewItem.target)}</dd></div>` : ''}
        ${metaHtml}
      </dl>
      ${evidenceHtml}
    `;
    bindDetailPanelActions(target);
  }

  function enrichPlanDetail(item) {
    const children = getPlanChildren(item.id);
    if (!children.length) return item;
    const childLabel = PLAN_CHILD_LABELS[item.type] || 'neighbors';
    const expanded = state.planExplore.expanded.has(item.id);
    return {
      ...item,
      detail: item.detail || `${children.length} ${childLabel} connected to this node.`,
      meta: {
        ...(item.meta || {}),
        [expanded ? 'Visible children' : 'Hidden children']: `${children.length} ${childLabel}`,
        Expanded: expanded ? 'Yes' : 'No',
      },
    };
  }

  function renderMetaRows(meta) {
    return Object.entries(meta || {}).map(([label, value]) => `
      <div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>
    `).join('');
  }

  function getNodeById(nodeId) {
    return (state.graphData.nodes || []).find(node => node.id === nodeId) || null;
  }

  function getPlanParentId(nodeId) {
    return state.planExplore.preferredParentByChild.get(nodeId)
      || state.planExplore.parentByChild.get(nodeId)
      || '';
  }

  function getPlanParent(nodeId) {
    const parentId = getPlanParentId(nodeId);
    return parentId ? getNodeById(parentId) : null;
  }

  function connectedEdgesForNode(nodeId) {
    return (state.graphData.edges || []).filter(edge => edge.source === nodeId || edge.target === nodeId);
  }

  function counterpartForEdge(edge, nodeId) {
    const otherId = edge.source === nodeId ? edge.target : edge.source;
    return getNodeById(otherId) || { id: otherId, label: otherId, type: edge.source === nodeId ? edge.target_label : edge.source_label };
  }

  function renderDetailActions(item, isEdge) {
    if (isEdge) {
      return `
        <div class="tg-detail-actions">
          ${item.source ? `<button type="button" data-tg-focus-node="${esc(item.source)}">Focus Source</button>` : ''}
          ${item.target ? `<button type="button" data-tg-focus-node="${esc(item.target)}">Focus Target</button>` : ''}
        </div>
      `;
    }
    const isPlan = state.graphMode === 'plan';
    const children = isPlan ? getPlanChildren(item.id) : [];
    const expanded = isPlan && state.planExplore.expanded.has(item.id);
    const parent = isPlan ? getPlanParent(item.id) : null;
    const sectionLink = item.type === 'TTSection'
      ? '<a href="/timetable-workspace/split/" class="tg-detail-link">Open Split Workspace</a>'
      : '';
    const sectionOps = item.type === 'TTSection'
      ? '<button type="button" data-tg-detail-action="safe-slots">Safe Slots</button>'
      : '';
    return `
      <div class="tg-detail-actions">
        ${isPlan && children.length && !expanded ? '<button type="button" data-tg-detail-action="expand">Expand Node</button>' : ''}
        ${isPlan && expanded ? '<button type="button" data-tg-detail-action="collapse">Collapse Branch</button>' : ''}
        ${parent ? `<button type="button" data-tg-focus-node="${esc(parent.id)}">Focus Parent</button>` : ''}
        <button type="button" data-tg-detail-action="fit">Fit Graph</button>
        ${sectionOps}
        ${sectionLink}
      </div>
    `;
  }

  function renderDetailEvidence(item, isEdge) {
    if (isEdge) return renderEdgeEvidence(item);
    if (state.graphMode === 'plan') return renderPlanEvidence(item);
    return renderNetworkEvidence(item);
  }

  function renderEdgeEvidence(edge) {
    const source = getNodeById(edge.source) || { id: edge.source, label: edge.source };
    const target = getNodeById(edge.target) || { id: edge.target, label: edge.target };
    return `
      <section class="tg-detail-section">
        <div class="tg-detail-title">Relationship Ends</div>
        <button type="button" class="tg-detail-row" data-tg-focus-node="${esc(source.id)}">
          <span>Source</span><strong>${esc(source.label || source.id)}</strong>
        </button>
        <button type="button" class="tg-detail-row" data-tg-focus-node="${esc(target.id)}">
          <span>Target</span><strong>${esc(target.label || target.id)}</strong>
        </button>
      </section>
    `;
  }

  function renderPlanEvidence(item) {
    const parent = getPlanParent(item.id);
    const childEdges = getPlanChildren(item.id);
    const children = childEdges.map(edge => getNodeById(edge.target)).filter(Boolean);
    const childRows = children.slice(0, 12).map(child => `
      <button type="button" class="tg-detail-row" data-tg-focus-node="${esc(child.id)}" data-tg-parent-node="${esc(item.id)}">
        <span>${esc(child.type?.replace('TT', '') || 'Node')}</span><strong>${esc(child.label || child.id)}</strong>
      </button>
    `).join('');
    const more = children.length > 12
      ? `<div class="tg-detail-more">${esc(children.length - 12)} more hidden from this list</div>`
      : '';
    const parentRow = parent ? `
      <button type="button" class="tg-detail-row" data-tg-focus-node="${esc(parent.id)}">
        <span>Parent</span><strong>${esc(parent.label || parent.id)}</strong>
      </button>
    ` : '';
    const sectionOps = item.type === 'TTSection' ? renderSectionOperations(item) : '';
    if (!parentRow && !childRows) return sectionOps;
    return `
      <section class="tg-detail-section">
        <div class="tg-detail-title">Tree Navigation</div>
        ${parentRow}
        ${childRows}
        ${more}
      </section>
      ${sectionOps}
    `;
  }

  function renderNetworkEvidence(item) {
    const edges = connectedEdgesForNode(item.id);
    const sectionOps = item.type === 'TTSection' ? renderSectionOperations(item) : '';
    if (!edges.length) return sectionOps;
    const rows = edges.slice(0, 12).map(edge => {
      const other = counterpartForEdge(edge, item.id);
      const shared = edge.shared_students != null ? ` | ${edge.shared_students} shared` : '';
      return `
        <button type="button" class="tg-detail-row" data-tg-focus-node="${esc(other.id)}">
          <span>${esc(edge.type || 'REL')}${esc(shared)}</span><strong>${esc(other.label || other.id)}</strong>
        </button>
      `;
    }).join('');
    const more = edges.length > 12
      ? `<div class="tg-detail-more">${esc(edges.length - 12)} more relationships in this lens</div>`
      : '';
    return `
      <section class="tg-detail-section">
        <div class="tg-detail-title">Connected Evidence</div>
        ${rows}
        ${more}
      </section>
      ${sectionOps}
    `;
  }

  function placementIdForSection(item) {
    return item?.meta?.Placement || '';
  }

  function candidateKey(candidate) {
    return `${candidate.day}|${candidate.start}|${candidate.end}`;
  }

  function renderSectionOperations(item) {
    const placementId = placementIdForSection(item);
    if (!placementId) {
      return `
        <section class="tg-detail-section">
          <div class="tg-detail-title">Section Operations</div>
          <div class="tg-detail-more">No placement id is attached to this section.</div>
        </section>
      `;
    }
    const cached = state.sectionOps.get(String(placementId));
    if (!cached) {
      return `
        <section class="tg-detail-section">
          <div class="tg-detail-title">Section Operations</div>
          <div class="tg-section-current">
            <strong>${esc(item.label || 'Section')}</strong>
            <span>${esc(item.meta?.Day || '-')} ${esc(item.meta?.Time || '-')} | ${esc(item.meta?.Room || 'UNASSIGNED')}</span>
          </div>
          <button type="button" class="tg-detail-row tg-detail-command" data-tg-placement-slots="${esc(placementId)}">
            <span>Read-only recommender</span><strong>Load safe move slots</strong>
          </button>
        </section>
      `;
    }
    if (cached.loading) {
      return `
        <section class="tg-detail-section">
          <div class="tg-detail-title">Section Operations</div>
          <div class="tg-detail-more">Loading safe move slots...</div>
        </section>
      `;
    }
    if (cached.error) {
      return `
        <section class="tg-detail-section">
          <div class="tg-detail-title">Section Operations</div>
          <div class="tg-detail-more">${esc(cached.error)}</div>
          <button type="button" class="tg-detail-row tg-detail-command" data-tg-placement-slots="${esc(placementId)}">
            <span>Retry</span><strong>Load safe move slots</strong>
          </button>
        </section>
      `;
    }
    const preview = cached.data || {};
    const current = preview.current_impact || {};
    const candidates = (preview.candidates || []).slice(0, 5);
    const selectedKey = cached.previewKey || (candidates[0] ? candidateKey(candidates[0]) : '');
    const selected = candidates.find(candidate => candidateKey(candidate) === selectedKey) || candidates[0] || null;
    const rows = candidates.map(candidate => {
      const key = candidateKey(candidate);
      const selectedAttr = selected && candidateKey(selected) === key ? 'true' : 'false';
      return `
      <div class="tg-candidate-row" data-tone="${esc(candidate.tone || 'neutral')}" data-selected="${selectedAttr}">
        <div class="tg-candidate-head">
          <div>
            <strong>${esc(candidate.day)} ${esc(candidate.start)}-${esc(candidate.end)}</strong>
            <span>${esc(candidate.badge || candidate.tone || 'Candidate')}</span>
          </div>
          <button type="button" data-tg-preview-slot="${esc(key)}" data-tg-placement-id="${esc(placementId)}">Preview</button>
        </div>
        <dl>
          <div><dt>Critical</dt><dd>${esc(candidate.critical_count || 0)}</dd></div>
          <div><dt>Warnings</dt><dd>${esc(candidate.warning_count || 0)}</dd></div>
          <div><dt>Students</dt><dd>${esc(candidate.student_affected_count || 0)}</dd></div>
          <div><dt>Improves</dt><dd>${esc(candidate.impact_improvement || 0)}</dd></div>
        </dl>
      </div>
    `;
    }).join('');
    const selectedPreview = selected ? renderMovePreview(selected) : '';
    return `
      <section class="tg-detail-section">
        <div class="tg-detail-title">Section Operations</div>
        <div class="tg-section-current">
          <strong>Current impact</strong>
          <span>${esc(current.critical_count || 0)} critical | ${esc(current.warning_count || 0)} warnings | ${esc(current.student_affected_count || 0)} students</span>
        </div>
        ${selectedPreview}
        ${rows || '<div class="tg-detail-more">No move candidates returned.</div>'}
        <button type="button" class="tg-detail-row tg-detail-command" data-tg-placement-slots="${esc(placementId)}">
          <span>Refresh</span><strong>Recalculate safe slots</strong>
        </button>
      </section>
    `;
  }

  function renderMovePreview(candidate) {
    const evidenceRows = (candidate.evidence || []).slice(0, 5).map(item => `
      <div class="tg-preview-evidence" data-tone="${esc(item.tone || 'neutral')}">
        <strong>${esc(item.title || item.kind || 'Evidence')}</strong>
        <span>${esc(item.detail || '')}${item.student_count ? ` | ${esc(item.student_count)} students` : ''}</span>
      </div>
    `).join('');
    const currentCritical = candidate.current_critical_count ?? 0;
    const currentWarning = candidate.current_warning_count ?? 0;
    const currentStudents = candidate.current_student_affected_count ?? 0;
    return `
      <div class="tg-move-preview" data-tone="${esc(candidate.tone || 'neutral')}">
        <div class="tg-detail-title">Move Preview</div>
        <div class="tg-move-preview-head">
          <strong>${esc(candidate.day)} ${esc(candidate.start)}-${esc(candidate.end)}</strong>
          <span>${esc(candidate.badge || candidate.tone || 'Candidate')}</span>
        </div>
        <div class="tg-before-after">
          <div>
            <span>Current</span>
            <strong>${esc(currentCritical)}C / ${esc(currentWarning)}W / ${esc(currentStudents)}S</strong>
          </div>
          <div>
            <span>After</span>
            <strong>${esc(candidate.critical_count || 0)}C / ${esc(candidate.warning_count || 0)}W / ${esc(candidate.student_affected_count || 0)}S</strong>
          </div>
          <div>
            <span>Gain</span>
            <strong>${esc(candidate.impact_improvement || 0)}</strong>
          </div>
        </div>
        ${evidenceRows || '<div class="tg-detail-more">No conflict evidence for this candidate.</div>'}
      </div>
    `;
  }

  function bindDetailPanelActions(target) {
    target.querySelectorAll('[data-tg-detail-action]').forEach(button => {
      button.addEventListener('click', () => {
        const action = button.dataset.tgDetailAction;
        if (action === 'expand') expandSelectedGraphNode();
        if (action === 'collapse') collapseSelectedGraphBranch();
        if (action === 'safe-slots') {
          const placementId = placementIdForSection(state.graphSelection.item);
          if (placementId) loadSectionSlotCandidates(placementId);
        }
        if (action === 'fit' && runViewerCommand('fit')) {
          setGraphToolStatus('Graph fitted to the stage.', 'good');
        }
      });
    });
    target.querySelectorAll('[data-tg-focus-node]').forEach(button => {
      button.addEventListener('click', () => (
        focusNodeFromDetail(button.dataset.tgFocusNode, button.dataset.tgParentNode || '')
      ));
    });
    target.querySelectorAll('[data-tg-placement-slots]').forEach(button => {
      button.addEventListener('click', () => loadSectionSlotCandidates(button.dataset.tgPlacementSlots));
    });
    target.querySelectorAll('[data-tg-preview-slot]').forEach(button => {
      button.addEventListener('click', () => previewCandidateSlot(
        button.dataset.tgPlacementId,
        button.dataset.tgPreviewSlot,
      ));
    });
  }

  function focusNodeFromDetail(nodeId, parentId = '') {
    if (!nodeId) return;
    if (state.graphMode === 'plan') {
      if (parentId) state.planExplore.preferredParentByChild.set(nodeId, parentId);
      revealPlanNode(nodeId);
      const node = getNodeById(nodeId);
      setGraphToolStatus(`Focused ${node?.label || nodeId}.`, 'good');
      return;
    }
    const raw = state.graphViewer?.focusNode?.(nodeId, true);
    if (raw) {
      state.graphSelection = { item: raw, isEdge: false };
      renderGraphDetail(raw);
      setGraphToolStatus(`Focused ${raw.label || raw.id}.`, 'good');
    }
  }

  async function loadSectionSlotCandidates(placementId) {
    if (!placementId) return;
    const key = String(placementId);
    state.sectionOps.set(key, { loading: true });
    renderGraphDetail(state.graphSelection.item, state.graphSelection.isEdge);
    setGraphToolStatus('Loading section safe-slot recommender...', 'good');
    try {
      const data = await api(placementUrlFor(cfg.slotCandidatesUrlTemplate, placementId));
      const best = (data.candidates || [])[0];
      state.sectionOps.set(key, { loading: false, data, previewKey: best ? candidateKey(best) : '' });
      renderGraphDetail(state.graphSelection.item, state.graphSelection.isEdge);
      const clean = (data.candidates || []).filter(candidate => candidate.tone === 'clean').length;
      setGraphToolStatus(`${clean} clean safe slots found for this section.`, clean ? 'good' : 'warn');
    } catch (err) {
      state.sectionOps.set(key, { loading: false, error: err.message || 'Could not load safe slots.' });
      renderGraphDetail(state.graphSelection.item, state.graphSelection.isEdge);
      setGraphToolStatus('Safe-slot preview failed.', 'warn');
    }
  }

  function previewCandidateSlot(placementId, slotKey) {
    if (!placementId || !slotKey) return;
    const key = String(placementId);
    const cached = state.sectionOps.get(key);
    if (!cached || !cached.data) return;
    state.sectionOps.set(key, { ...cached, previewKey: slotKey });
    renderGraphDetail(state.graphSelection.item, state.graphSelection.isEdge);
    const candidate = (cached.data.candidates || []).find(item => candidateKey(item) === slotKey);
    setGraphToolStatus(
      candidate
        ? `Previewing ${candidate.day} ${candidate.start}-${candidate.end}.`
        : 'Preview selected.',
      'good',
    );
  }

  function renderTreePath(item) {
    const nodesById = new Map((state.graphData.nodes || []).map(node => [node.id, node]));
    const parentByChild = new Map();
    const planEdgeTypes = new Set([
      'HAS_PROGRAM',
      'HAS_PLAN_TERM',
      'HAS_GROUP',
      'SCHEDULES_COURSE',
      'HAS_SECTION',
      'HAS_ENROLLED_STUDENT',
    ]);
    (state.graphData.edges || []).forEach(edge => {
      if (planEdgeTypes.has(edge.type) && !parentByChild.has(edge.target)) {
        parentByChild.set(edge.target, edge.source);
      }
    });
    const labels = [];
    let current = item.id;
    const seen = new Set();
    while (current && !seen.has(current)) {
      seen.add(current);
      const node = nodesById.get(current);
      if (node) labels.push(node.label || node.id);
      current = state.planExplore.preferredParentByChild.get(current) || parentByChild.get(current);
    }
    if (labels.length < 2) return '';
    return `<div><dt>Tree path</dt><dd>${esc(labels.reverse().join(' -> '))}</dd></div>`;
  }

  function expandSelectedGraphNode() {
    if (state.graphMode !== 'plan') {
      setGraphToolStatus('Expansion is available in Plan View.', 'warn');
      return;
    }
    const nodeId = getSelectedGraphNodeId();
    if (!nodeId) {
      setGraphToolStatus('Select a plan node before expanding.', 'warn');
      return;
    }
    const node = (state.graphData.nodes || []).find(item => item.id === nodeId);
    if (!expandPlanNode(nodeId)) {
      setGraphToolStatus(`${node?.label || nodeId} has no hidden children.`, 'warn');
      return;
    }
    state.planExplore.selectedNodeId = nodeId;
    state.pendingFocusNodeId = nodeId;
    renderGraphExplorer(state.graphData);
    setGraphToolStatus(`Expanded ${node?.label || nodeId}.`, 'good');
  }

  function collapseSelectedGraphBranch() {
    if (state.graphMode !== 'plan') {
      setGraphToolStatus('Collapse branch is available in Plan View.', 'warn');
      return;
    }
    const nodeId = getSelectedGraphNodeId();
    if (!nodeId) {
      setGraphToolStatus('Select a plan node before collapsing.', 'warn');
      return;
    }
    const node = (state.graphData.nodes || []).find(item => item.id === nodeId);
    if (!collapsePlanBranch(nodeId)) {
      setGraphToolStatus(`${node?.label || nodeId} is already collapsed.`, 'warn');
      return;
    }
    state.planExplore.selectedNodeId = nodeId;
    state.pendingFocusNodeId = nodeId;
    renderGraphExplorer(state.graphData);
    setGraphToolStatus(`Collapsed ${node?.label || nodeId}.`, 'good');
  }

  function runViewerCommand(command, ...args) {
    const viewer = state.graphViewer;
    if (!viewer || typeof viewer[command] !== 'function') {
      setGraphToolStatus('Graph renderer is not ready yet.', 'warn');
      return false;
    }
    const ok = viewer[command](...args);
    if (!ok) setGraphToolStatus('Graph command could not run.', 'warn');
    return Boolean(ok);
  }

  function bind() {
    $('tgLoadScenarios').addEventListener('click', loadScenarios);
    $('tgPreview').addEventListener('click', () => previewScenario($('tgScenario').value));
    $('tgSync').addEventListener('click', syncScenario);
    $('tgCheck').addEventListener('click', checkStatus);
    document.querySelectorAll('[data-tg-mode]').forEach(button => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-tg-mode]').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
        loadGraphView(button.dataset.tgMode);
      });
    });
    $('tgPlanProgram')?.addEventListener('change', event => {
      state.planFilters.program = event.target.value;
      const validTerms = state.planFilters.program
        ? state.planFilters.options.terms_by_program?.[state.planFilters.program] || []
        : Object.values(state.planFilters.options.terms_by_program || {}).flat();
      if (state.planFilters.planTerm && !validTerms.includes(state.planFilters.planTerm)) {
        state.planFilters.planTerm = '';
      }
      loadGraphView('plan');
    });
    $('tgPlanTerm')?.addEventListener('change', event => {
      state.planFilters.planTerm = event.target.value;
      loadGraphView('plan');
    });
    $('tgPlanStudents')?.addEventListener('change', event => {
      state.planFilters.includeStudents = Boolean(event.target.checked);
      loadGraphView('plan');
    });
    $('tgPlanReset')?.addEventListener('click', () => {
      resetPlanExplore();
      renderGraphExplorer(state.graphData);
      setGraphToolStatus('Tree reset to scenario root.');
    });
    $('tgGraphFit')?.addEventListener('click', () => {
      if (runViewerCommand('fit')) setGraphToolStatus('Graph fitted to the stage.', 'good');
    });
    $('tgGraphZoomIn')?.addEventListener('click', () => {
      if (runViewerCommand('zoomBy', 1.22)) setGraphToolStatus('Zoomed in.', 'good');
    });
    $('tgGraphZoomOut')?.addEventListener('click', () => {
      if (runViewerCommand('zoomBy', 0.82)) setGraphToolStatus('Zoomed out.', 'good');
    });
    $('tgGraphLayout')?.addEventListener('click', () => {
      if (runViewerCommand('relayout')) setGraphToolStatus('Layout recalculated.', 'good');
    });
    $('tgGraphExpand')?.addEventListener('click', expandSelectedGraphNode);
    $('tgGraphCollapse')?.addEventListener('click', collapseSelectedGraphBranch);
    $('tgGraphSearch')?.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        searchGraph();
      }
    });
    $('tgGraphSearch')?.addEventListener('search', searchGraph);
    $('tgScenario').addEventListener('change', event => {
      state.selectedScenarioId = event.target.value;
      previewScenario(state.selectedScenarioId);
    });
  }

  bind();
  checkStatus();
  loadScenarios();
})();
