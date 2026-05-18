import cytoscape from 'cytoscape';

const NODE_COLORS = {
  TTScenario: '#eef2ff',
  TTProgram: '#18a38a',
  TTPlanTerm: '#efb94a',
  TTBoard: '#7b61ff',
  TTGroup: '#18a38a',
  TTCourse: '#2672d9',
  TTSection: '#ef6262',
  TTStudent: '#0a8e6e',
  TTSlot: '#d0901f',
  TTRoom: '#7aa6c2',
  TTInstructor: '#d66fd0',
};

const EDGE_COLORS = {
  critical: '#ef6262',
  warning: '#efb94a',
  active: '#0a8e6e',
  neutral: '#7f8dff',
};

const EDGE_LABELS = {
  HAS_PROGRAM: 'HAS_PROGRAM',
  HAS_PLAN_TERM: 'HAS_TERM',
  HAS_GROUP: 'HAS_GROUP',
  SCHEDULES_COURSE: 'SCHEDULES',
  HAS_SECTION: 'HAS_SECTION',
  HAS_ENROLLED_STUDENT: 'ENROLLED',
  CURRENTLY_REGISTERED_IN: 'REGISTERED',
  PLACED_IN: 'PLACED_IN',
  ASSIGNED_ROOM: 'ROOM',
  TAUGHT_BY: 'TAUGHT_BY',
  OF_COURSE: 'OF_COURSE',
  CLASHES_WITH: 'CLASHES_WITH',
};

const NODE_SIZES = {
  TTScenario: 68,
  TTProgram: 58,
  TTPlanTerm: 52,
  TTBoard: 54,
  TTGroup: 50,
  TTCourse: 46,
  TTSection: 44,
  TTStudent: 24,
  TTSlot: 42,
  TTRoom: 38,
  TTInstructor: 38,
};

const NODE_FONT_SIZES = {
  TTScenario: 12,
  TTProgram: 12,
  TTPlanTerm: 11,
  TTBoard: 12,
  TTGroup: 11,
  TTCourse: 10,
  TTSection: 10,
  TTStudent: 8,
};

function truncate(value, length) {
  const text = String(value || '').trim();
  return text.length > length ? `${text.slice(0, length - 3)}...` : text;
}

function nodeLabel(node) {
  if (node.type === 'TTScenario') return 'Scenario';
  return truncate(node.label || node.id, node.type === 'TTStudent' ? 12 : 16);
}

function edgeLabel(edge) {
  return EDGE_LABELS[edge.type] || edge.type || '';
}

function nodeSize(node) {
  const base = NODE_SIZES[node.type] || Math.max(28, Number(node.size || 10) * 2.4);
  return Math.max(20, Math.min(76, base));
}

function nodeFontSize(node) {
  return NODE_FONT_SIZES[node.type] || 10;
}

function toElements(data) {
  const nodes = (data.nodes || []).map(node => {
    const classes = [];
    if (node.expandable) classes.push('is-expandable');
    if (node.expanded) classes.push('is-expanded');
    if (node.selected) classes.push('is-selected');
    if (node.type === 'TTStudent') classes.push('is-student');
    return {
      group: 'nodes',
      data: {
        id: String(node.id),
        label: nodeLabel(node),
        type: node.type || 'Node',
        raw: node,
        color: NODE_COLORS[node.type] || '#748094',
        borderColor: node.expandable ? '#2dd4bf' : 'rgba(255,255,255,0.55)',
        size: nodeSize(node),
        fontSize: nodeFontSize(node),
        labelMaxWidth: node.type === 'TTStudent' ? 54 : 76,
      },
      classes: classes.join(' '),
      grabbable: true,
    };
  });
  const nodeIds = new Set(nodes.map(node => node.data.id));
  const edges = (data.edges || [])
    .filter(edge => edge.source && edge.target && nodeIds.has(String(edge.source)) && nodeIds.has(String(edge.target)))
    .map(edge => {
      const classes = [];
      if (edge.tone) classes.push(`tone-${edge.tone}`);
      if (edge.type === 'CLASHES_WITH') classes.push('is-clash');
      return {
        group: 'edges',
        data: {
          id: String(edge.id),
          source: String(edge.source),
          target: String(edge.target),
          label: edgeLabel(edge),
          type: edge.type || 'RELATES_TO',
          raw: edge,
          color: EDGE_COLORS[edge.tone] || EDGE_COLORS.neutral,
          width: edge.type === 'CLASHES_WITH' ? 3.6 : edge.tone === 'active' ? 2.4 : 1.7,
        },
        classes: classes.join(' '),
      };
    });
  return [...nodes, ...edges];
}

function styleSheet() {
  return [
    {
      selector: 'node',
      style: {
        width: 'data(size)',
        height: 'data(size)',
        'background-color': 'data(color)',
        'background-opacity': 0.98,
        'background-blacken': -0.04,
        'border-width': 4,
        'border-color': '#0b1220',
        label: 'data(label)',
        color: '#f8fafc',
        'font-family': 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        'font-size': 'data(fontSize)',
        'font-weight': 800,
        'text-valign': 'center',
        'text-halign': 'center',
        'text-wrap': 'wrap',
        'text-max-width': 'data(labelMaxWidth)',
        'text-outline-color': '#07111f',
        'text-outline-opacity': 0.82,
        'text-outline-width': 3,
        'overlay-opacity': 0,
        'shadow-blur': 16,
        'shadow-color': '#020617',
        'shadow-opacity': 0.45,
        'transition-property': 'border-color, border-width, opacity, shadow-opacity',
        'transition-duration': '120ms',
      },
    },
    {
      selector: 'node.is-student',
      style: {
        'text-opacity': 0,
        'border-width': 2,
        'min-zoomed-font-size': 8,
      },
    },
    {
      selector: 'node.is-expandable',
      style: {
        'border-width': 5,
        'border-color': '#2dd4bf',
        'shadow-color': '#2dd4bf',
        'shadow-opacity': 0.22,
      },
    },
    {
      selector: 'node.is-expanded',
      style: {
        'border-color': '#fbbf24',
        'shadow-color': '#fbbf24',
        'shadow-opacity': 0.25,
      },
    },
    {
      selector: 'node.is-selected',
      style: {
        'border-width': 6,
        'border-color': '#ffffff',
        'shadow-blur': 22,
        'shadow-color': '#2dd4bf',
        'shadow-opacity': 0.42,
      },
    },
    {
      selector: 'node.is-search-result',
      style: {
        'border-width': 7,
        'border-color': '#fbbf24',
        'shadow-blur': 28,
        'shadow-color': '#fbbf24',
        'shadow-opacity': 0.48,
      },
    },
    {
      selector: 'edge',
      style: {
        width: 'data(width)',
        'line-color': 'data(color)',
        'target-arrow-color': 'data(color)',
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.72,
        'curve-style': 'unbundled-bezier',
        'control-point-distance': 34,
        'control-point-weight': 0.5,
        opacity: 0.78,
        label: 'data(label)',
        color: '#dbeafe',
        'font-size': 8,
        'font-weight': 800,
        'text-rotation': 'autorotate',
        'text-background-color': '#08111f',
        'text-background-opacity': 0.86,
        'text-background-padding': 4,
        'text-border-color': 'data(color)',
        'text-border-width': 1,
        'text-border-opacity': 0.18,
        'text-outline-color': '#07111f',
        'text-outline-width': 2,
      },
    },
    {
      selector: 'edge.is-clash',
      style: {
        'line-style': 'dashed',
        opacity: 0.92,
      },
    },
    {
      selector: '.is-neighbour',
      style: {
        opacity: 1,
      },
    },
    {
      selector: '.is-faded',
      style: {
        opacity: 0.18,
        'text-opacity': 0.12,
      },
    },
  ];
}

function planLayout(cy) {
  if (cy.nodes().length <= 2) {
    return { name: 'grid', fit: true, padding: 72, animate: false };
  }
  return {
    name: 'cose',
    animate: false,
    fit: true,
    padding: 70,
    randomize: true,
    avoidOverlap: true,
    nodeDimensionsIncludeLabels: true,
    nodeRepulsion: 760000,
    idealEdgeLength: cy.nodes().length > 90 ? 112 : 138,
    edgeElasticity: 120,
    nestingFactor: 1.05,
    gravity: 62,
    numIter: 1200,
  };
}

function networkLayout(cy) {
  if (cy.nodes().length <= 2) {
    return { name: 'grid', fit: true, padding: 56, animate: false };
  }
  return {
    name: 'cose',
    animate: false,
    fit: true,
    padding: 52,
    randomize: true,
    nodeRepulsion: 520000,
    idealEdgeLength: 118,
    edgeElasticity: 90,
    nestingFactor: 1.1,
    gravity: 80,
    numIter: 900,
  };
}

class TimetableCyGraphViewer {
  constructor(container, callbacks = {}) {
    this.container = container;
    this.callbacks = callbacks;
    this.cy = null;
    this.mode = 'plan';
  }

  render(data) {
    this.destroy();
    this.mode = data.mode || 'plan';
    const elements = toElements(data);
    this.cy = cytoscape({
      container: this.container,
      elements,
      style: styleSheet(),
      minZoom: 0.08,
      maxZoom: 4,
      wheelSensitivity: 0.18,
      boxSelectionEnabled: true,
      autoungrabify: false,
      autounselectify: true,
    });
    this.bindEvents();
    this.runLayout(this.mode);
  }

  bindEvents() {
    this.cy.on('tap', event => {
      if (event.target === this.cy) {
        this.clearFocus();
        this.callbacks.onSelect?.(null, 'background');
      }
    });
    this.cy.on('tap', 'node', event => {
      const node = event.target;
      this.focus(node);
      this.callbacks.onSelect?.(node.data('raw'), 'node');
    });
    this.cy.on('tap', 'edge', event => {
      const edge = event.target;
      this.focus(edge);
      this.callbacks.onSelect?.(edge.data('raw'), 'edge');
    });
  }

  runLayout(mode) {
    let fired = false;
    const done = () => {
      if (fired || !this.cy) return;
      fired = true;
      if (this.cy.nodes().length <= 1) {
        this.cy.zoom(1.25);
        this.cy.center(this.cy.nodes());
      } else {
        this.cy.fit(this.cy.elements(), 56);
      }
      this.callbacks.onReady?.({
        nodes: this.cy.nodes().length,
        edges: this.cy.edges().length,
      });
    };
    this.cy.one('layoutstop', done);
    const layout = this.cy.layout(mode === 'plan' ? planLayout(this.cy) : networkLayout(this.cy));
    layout.run();
    window.setTimeout(done, 300);
  }

  focus(element) {
    this.clearFocus(false);
    const neighbourhood = element.isNode() ? element.closedNeighborhood() : element.connectedNodes().union(element);
    neighbourhood.addClass('is-neighbour');
    this.cy.elements().not(neighbourhood).addClass('is-faded');
    element.addClass('is-selected');
  }

  fit() {
    if (!this.cy || !this.cy.elements().length) return false;
    this.cy.fit(this.cy.elements(), 56);
    return true;
  }

  zoomBy(factor) {
    if (!this.cy) return false;
    const zoom = Math.max(this.cy.minZoom(), Math.min(this.cy.maxZoom(), this.cy.zoom() * factor));
    this.cy.zoom({
      level: zoom,
      renderedPosition: {
        x: this.container.clientWidth / 2,
        y: this.container.clientHeight / 2,
      },
    });
    return true;
  }

  relayout() {
    if (!this.cy) return false;
    this.clearFocus();
    this.runLayout(this.mode);
    return true;
  }

  focusNode(nodeId, markSearch = false) {
    if (!this.cy || !nodeId) return null;
    const node = this.cy.getElementById(String(nodeId));
    if (!node || !node.length) return null;
    this.clearFocus();
    this.focus(node);
    if (markSearch) node.addClass('is-search-result');
    this.cy.fit(node.closedNeighborhood(), 88);
    this.cy.center(node);
    return node.data('raw') || null;
  }

  search(query) {
    if (!this.cy) return { match: null, count: 0 };
    const needle = String(query || '').trim().toLowerCase();
    this.cy.nodes().removeClass('is-search-result');
    if (!needle) {
      this.clearFocus();
      return { match: null, count: 0 };
    }
    const matches = this.cy.nodes().filter(node => {
      const raw = node.data('raw') || {};
      const meta = raw.meta || {};
      const text = [
        raw.label,
        raw.id,
        raw.type,
        ...Object.values(meta),
      ].join(' ').toLowerCase();
      return text.includes(needle);
    });
    if (!matches.length) return { match: null, count: 0 };
    const sorted = matches.toArray().sort((a, b) => {
      const labelA = String(a.data('raw')?.label || '').toLowerCase();
      const labelB = String(b.data('raw')?.label || '').toLowerCase();
      const scoreA = labelA === needle ? 0 : labelA.startsWith(needle) ? 1 : 2;
      const scoreB = labelB === needle ? 0 : labelB.startsWith(needle) ? 1 : 2;
      return scoreA - scoreB || labelA.localeCompare(labelB);
    });
    const node = sorted[0];
    const raw = this.focusNode(node.id(), true);
    return { match: raw, count: matches.length };
  }

  clearFocus(clearSelected = true) {
    if (!this.cy) return;
    this.cy.elements().removeClass('is-faded is-neighbour');
    if (clearSelected) this.cy.elements().removeClass('is-selected is-search-result');
  }

  destroy() {
    if (this.cy) {
      this.cy.destroy();
      this.cy = null;
    }
    this.container.innerHTML = '';
  }
}

window.TimetableCyGraphViewer = TimetableCyGraphViewer;
