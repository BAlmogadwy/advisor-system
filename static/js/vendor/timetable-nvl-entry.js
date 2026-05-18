import {
  ForceDirectedLayoutType,
  FreeLayoutType,
  NVL,
} from '@neo4j-nvl/base';
import {
  ClickInteraction,
  DragNodeInteraction,
  PanInteraction,
  ZoomInteraction,
} from '@neo4j-nvl/interaction-handlers';

const NODE_COLORS = {
  TTStudent: '#0a8e6e',
  TTCourse: '#2672d9',
  TTSection: '#ef6262',
  TTSlot: '#d0901f',
  TTBoard: '#7b61ff',
  TTProgram: '#18a38a',
  TTPlanTerm: '#efb94a',
  TTGroup: '#18a38a',
  TTRoom: '#7aa6c2',
  TTInstructor: '#d66fd0',
  TTScenario: '#eef2ff',
};

const EDGE_COLORS = {
  critical: '#ef6262',
  warning: '#efb94a',
  active: '#0a8e6e',
  neutral: '#7f8dff',
};

const NODE_TYPE_LABELS = {
  TTStudent: 'Student',
  TTCourse: 'Course',
  TTSection: 'Section',
  TTSlot: 'Slot',
  TTBoard: 'Group',
  TTProgram: 'Program',
  TTPlanTerm: 'Term',
  TTGroup: 'Group',
  TTRoom: 'Room',
  TTInstructor: 'Instructor',
  TTScenario: 'Scenario',
};

function normalizeCaption(value, fallback, maxLength = 22) {
  const text = String(value || fallback || '').trim();
  return text.length > maxLength ? `${text.slice(0, maxLength - 3)}...` : text;
}

function captionForNode(node) {
  return normalizeCaption(node.label, node.id, node.type === 'TTStudent' ? 14 : 24);
}

function toNvlNodes(nodes) {
  return (nodes || []).map(node => {
    const caption = captionForNode(node);
    return {
      id: String(node.id),
      caption,
      color: NODE_COLORS[node.type] || '#748094',
      size: Math.max(24, Math.min(74, Number(node.size || 10) * 2.45)),
      captionSize: 2,
      captionAlign: 'bottom',
      activated: Boolean(node.expanded),
      selected: Boolean(node.selected),
      raw: node,
    };
  });
}

function toNvlRelationships(edges) {
  return (edges || []).map(edge => ({
    id: String(edge.id),
    from: String(edge.source),
    to: String(edge.target),
    type: edge.type,
    caption: edge.type === 'CLASHES_WITH' ? 'CLASH' : '',
    color: EDGE_COLORS[edge.tone] || EDGE_COLORS.neutral,
    width: edge.type === 'CLASHES_WITH'
      ? edge.tone === 'critical' ? 4 : 3
      : edge.tone === 'active' ? 2.5 : 1.5,
    captionSize: 10,
    raw: edge,
  }));
}

function applyPlanPositions(nodes, relationships, container) {
  const levelByType = {
    TTScenario: 0,
    TTProgram: 1,
    TTPlanTerm: 2,
    TTBoard: 3,
    TTCourse: 4,
    TTSection: 5,
    TTStudent: 6,
  };
  const planEdgeTypes = new Set([
    'HAS_PROGRAM',
    'HAS_PLAN_TERM',
    'HAS_GROUP',
    'SCHEDULES_COURSE',
    'HAS_SECTION',
    'HAS_ENROLLED_STUDENT',
  ]);
  const nodeById = new Map(nodes.map(node => [node.id, node]));
  const childrenByParent = new Map();
  const parentByChild = new Map();
  relationships.forEach(rel => {
    if (!planEdgeTypes.has(rel.type) || !nodeById.has(rel.from) || !nodeById.has(rel.to)) return;
    if (!childrenByParent.has(rel.from)) childrenByParent.set(rel.from, []);
    childrenByParent.get(rel.from).push(rel.to);
    if (!parentByChild.has(rel.to)) parentByChild.set(rel.to, rel.from);
  });

  childrenByParent.forEach(children => {
    children.sort((a, b) => {
      const nodeA = nodeById.get(a);
      const nodeB = nodeById.get(b);
      return String(nodeA?.caption || a).localeCompare(String(nodeB?.caption || b));
    });
  });

  const width = Math.max(900, container.clientWidth || 900);
  const height = Math.max(560, container.clientHeight || 560);

  nodes.forEach(node => {
    node.size = {
      TTScenario: 52,
      TTProgram: 44,
      TTPlanTerm: 38,
      TTBoard: 42,
      TTCourse: 34,
      TTSection: 32,
      TTStudent: 18,
    }[node.raw?.type] || node.size;
    node.captionSize = {
      TTScenario: 3,
      TTProgram: 3,
      TTPlanTerm: 3,
      TTBoard: 3,
      TTCourse: 2,
      TTSection: 2,
      TTStudent: 1,
    }[node.raw?.type] || 2;
    node.captionAlign = 'bottom';
  });

  if (nodes.length === 1) {
    nodes[0].x = width / 2;
    nodes[0].y = height / 2;
    nodes[0].pinned = true;
    return;
  }

  const maxLevel = Math.max(1, ...nodes.map(node => levelByType[node.raw?.type] ?? 3));
  const xGap = Math.max(120, (width - 120) / maxLevel);
  const roots = nodes
    .filter(node => (levelByType[node.raw?.type] || 0) === 0 || !parentByChild.has(node.id))
    .sort((a, b) => String(a.caption).localeCompare(String(b.caption)));
  const leafCache = new Map();

  function leafCount(nodeId, visited = new Set()) {
    if (leafCache.has(nodeId)) return leafCache.get(nodeId);
    if (visited.has(nodeId)) return 1;
    visited.add(nodeId);
    const children = childrenByParent.get(nodeId) || [];
    const leaves = children.length
      ? children.reduce((sum, childId) => sum + leafCount(childId, new Set(visited)), 0)
      : 1;
    leafCache.set(nodeId, leaves);
    return leaves;
  }

  const totalLeaves = Math.max(roots.reduce((sum, node) => sum + leafCount(node.id), 0), 1);
  const yStep = Math.max(24, Math.min(54, height / (Math.min(totalLeaves, 18) + 1)));
  let cursor = yStep;

  function place(nodeId, startY, endY) {
    const node = nodeById.get(nodeId);
    if (!node) return;
    const level = levelByType[node.raw?.type] ?? 3;
    node.x = 64 + xGap * level;
    node.y = (startY + endY) / 2;
    node.pinned = true;
    if (node.raw?.type === 'TTStudent' && nodes.filter(item => item.raw?.type === 'TTStudent').length > 18) {
      node.caption = '';
      node.captions = [];
    }

    const children = childrenByParent.get(nodeId) || [];
    let childCursor = startY;
    const span = Math.max(endY - startY, yStep);
    const leafTotal = Math.max(children.reduce((sum, childId) => sum + leafCount(childId), 0), 1);
    children.forEach(childId => {
      const childSpan = Math.max(yStep, span * (leafCount(childId) / leafTotal));
      place(childId, childCursor, childCursor + childSpan);
      childCursor += childSpan;
    });
  }

  roots.forEach(root => {
    const span = Math.max(yStep, leafCount(root.id) * yStep);
    place(root.id, cursor, cursor + span);
    cursor += span + yStep * 0.8;
  });
}

class TimetableNvlViewer {
  constructor(container, callbacks = {}) {
    this.container = container;
    this.callbacks = callbacks;
    this.nvl = null;
    this.interactions = [];
    this.rawNodes = new Map();
    this.rawEdges = new Map();
  }

  render(data) {
    this.destroy();
    const nodes = toNvlNodes(data.nodes || []);
    const relationships = toNvlRelationships(data.edges || []);
    const isPlanView = data.mode === 'plan';
    if (isPlanView) {
      applyPlanPositions(nodes, relationships, this.container);
    }
    this.rawNodes = new Map(nodes.map(node => [node.id, node.raw]));
    this.rawEdges = new Map(relationships.map(rel => [rel.id, rel.raw]));

    this.nvl = new NVL(
      this.container,
      nodes,
      relationships,
      {
        renderer: 'canvas',
        layout: isPlanView ? FreeLayoutType : ForceDirectedLayoutType,
        layoutOptions: {},
        initialZoom: isPlanView ? 0.62 : 0.7,
        minZoom: 0.05,
        maxZoom: 4,
        disableTelemetry: true,
        styling: {
          defaultNodeColor: '#748094',
          defaultRelationshipColor: '#7f8dff',
          selectedBorderColor: '#2dd4bf',
          selectedInnerBorderColor: '#08111f',
          nodeDefaultBorderColor: 'rgba(255,255,255,0.55)',
          dropShadowColor: 'rgba(45,212,191,0.26)',
        },
      },
      {
        onLayoutDone: () => {
          this.fit();
          this.callbacks.onReady?.({
            nodes: nodes.length,
            edges: relationships.length,
          });
        },
        onError: error => this.callbacks.onError?.(error),
      },
    );

    this.interactions = [
      new DragNodeInteraction(this.nvl),
      new PanInteraction(this.nvl),
      new ZoomInteraction(this.nvl),
      new ClickInteraction(this.nvl, { selectOnClick: true }),
    ];

    const click = this.interactions[this.interactions.length - 1];
    click.updateCallback('onNodeClick', node => {
      this.callbacks.onSelect?.(this.rawNodes.get(node.id) || node, 'node');
    });
    click.updateCallback('onRelationshipClick', relationship => {
      this.callbacks.onSelect?.(
        this.rawEdges.get(relationship.id) || relationship,
        'relationship',
      );
    });
    click.updateCallback('onCanvasClick', () => {
      this.callbacks.onSelect?.(null, 'canvas');
    });
  }

  fit() {
    if (!this.nvl) return;
    const ids = this.nvl.getNodes().map(node => node.id);
    if (ids.length) {
      this.nvl.fit(ids, { duration: 350 });
    }
  }

  destroy() {
    this.interactions.forEach(interaction => {
      if (interaction && typeof interaction.destroy === 'function') {
        interaction.destroy();
      }
    });
    this.interactions = [];
    if (this.nvl && typeof this.nvl.destroy === 'function') {
      this.nvl.destroy();
    }
    this.nvl = null;
  }
}

window.TimetableNvlViewer = TimetableNvlViewer;
