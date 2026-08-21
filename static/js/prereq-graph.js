/* Prerequisite dependency graph — shared renderer.
   Extracted from page-dashboard.js so the advisor dashboard and the student
   portal draw the SAME graph from one implementation. Behaviour is unchanged;
   the i18n strings the renderer needs are passed in via opts.t, and two new
   optional inputs personalise it:
     opts.extraNodes : codes to draw even when they have no prerequisite edge
                       (about a third of a programme has none, and was invisible)
     opts.statusOf   : {CODE: 'passed'|'studying'|'open'|'locked'} -> colour by
                       the signed-in student's own progress
*/
(function () {
  'use strict';
  const IS_AR = document.documentElement.lang === 'ar';

  /* Fallbacks so a caller may pass only the strings it cares about. */
  const DEFAULT_T = {
    termHeading: n => (IS_AR ? `المستوى ${n}` : `Term ${n}`),
    pgNoTermBand: IS_AR ? 'مقررات لم يُحدّد مستواها' : 'no courses',
    pgGateTip: h => (IS_AR ? `شرط الساعات المعتمدة: ${h}` : `Gate: ${h} credit hours`),
    pgInferredTip: IS_AR ? 'المستوى غير محدّد في الخطة؛ وُضع تقديريًا' : 'term inferred',
    pgTermTip: n => (IS_AR ? `المستوى ${n}` : `Term ${n}`),
    pgGate: IS_AR ? 'شرط الساعات المعتمدة' : 'credit-hour gate',
    pgInferred: IS_AR ? 'المستوى محدّد تقديريًا' : 'term inferred',
    pgFoundation: IS_AR ? 'بداية سلسلة المتطلبات' : 'foundation',
    pgIntermediate: IS_AR ? 'وسط سلسلة المتطلبات' : 'intermediate',
    pgTerminal: IS_AR ? 'نهاية سلسلة المتطلبات' : 'terminal',
    pgHoverHint: IS_AR ? 'مرّر على مقرر لإبراز سلسلة متطلباته' : 'hover to highlight a chain',
    pgPassed: IS_AR ? 'مجتاز' : 'passed',
    pgStudying: IS_AR ? 'قيد الدراسة حاليًا' : 'studying now',
    pgOpen: IS_AR ? 'متطلباته مستوفاة' : 'open now',
    pgLocked: IS_AR ? 'متطلباته غير مستوفاة' : 'blocked',
    pgSameTermWarn: n => (IS_AR ? `علاقات متطلبات سابقة داخل المستوى نفسه: ${n}.` : `${n} prerequisite(s) in the same term`),
    pgBackwardWarn: n => (IS_AR ? `علاقات يظهر فيها المتطلب السابق بعد المقرر الذي يعتمد عليه: ${n}.` : `${n} prerequisite(s) after their course`),
  };

  const PG_GATE_RE = /^\s*(\d+)\s*\(\s*HOURS?\s*\)\s*$/i;

  function pgEsc(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* Build adjacency once; both layout modes read it. */
  function pgAdjacency(items) {
    const prereqs = {}, dependents = {}, all = new Set();
    items.forEach(row => {
      const c = row.course_code, p = row.prerequisite_course_code;
      all.add(c); all.add(p);
      if (!prereqs[c]) prereqs[c] = [];
      if (!prereqs[c].includes(p)) prereqs[c].push(p);
      if (!dependents[p]) dependents[p] = [];
      if (!dependents[p].includes(c)) dependents[p].push(c);
    });
    return { prereqs, dependents, all };
  }

  /* Row = declared programme term. Nodes with no declared term (credit-hour
     gates, and courses that gate this plan without belonging to it) are placed
     one term before the earliest course that depends on them, and flagged so
     the inference is visible rather than silent. */
  function pgTermRows(all, dependents, termOf) {
    const row = {}, inferred = new Set();
    all.forEach(c => {
      const t = termOf[c];
      if (typeof t === 'number' && isFinite(t)) row[c] = t;
    });
    const declared = Object.values(row);
    const floor = declared.length ? Math.min(...declared) : 1;
    let changed = true, guard = 0;
    while (changed && guard++ <= all.size) {
      changed = false;
      all.forEach(c => {
        if (row[c] !== undefined) return;
        const known = (dependents[c] || []).map(d => row[d]).filter(v => v !== undefined);
        if (!known.length) return;
        row[c] = Math.min(...known) - 1;
        inferred.add(c);
        changed = true;
      });
    }
    /* orphans: nothing depends on them and no declared term — park at the top */
    all.forEach(c => {
      if (row[c] === undefined) { row[c] = floor - 1; inferred.add(c); }
    });
    return { row, inferred };
  }

  /* Row = longest prerequisite chain length (the original layout). */
  function pgDepthRows(all, prereqs) {
    const row = {};
    function depth(c, vis) {
      if (row[c] !== undefined) return row[c];
      if (!vis) vis = new Set();
      if (vis.has(c)) return 0;
      vis.add(c);
      const ps = prereqs[c] || [];
      if (!ps.length) { row[c] = 0; return 0; }
      const d = Math.max(...ps.map(p => depth(p, new Set(vis)))) + 1;
      row[c] = d; return d;
    }
    all.forEach(c => depth(c));
    return { row, inferred: new Set() };
  }

  /* An edge spanning more than one band gets a routing point in each band it
     crosses. Those points take part in the ordering, so a long edge is steered
     into a channel between nodes instead of straight through them. */
  function pgBuildSlots(edges, row, minR, maxR, allNodes) {
    const slots = new Map();
    for (let r = minR; r <= maxR; r++) slots.set(r, []);
    const seen = new Set();
    const seed = id => {
      if (seen.has(id)) return;
      seen.add(id);
      const r = row[id];
      if (slots.has(r)) slots.get(r).push({ id, kind: 'node', key: id });
    };
    edges.forEach(e => { seed(e.f); seed(e.t); });
    /* Courses with no prerequisite edge still need a slot, otherwise they get a
       row but no position and silently vanish (a third of a typical programme). */
    if (allNodes) allNodes.forEach(seed);
    const up = {}, dn = {}, chain = {};
    const link = (a, b) => {
      if (!dn[a]) dn[a] = [];
      if (!up[b]) up[b] = [];
      dn[a].push(b); up[b].push(a);
    };
    edges.forEach((e, i) => {
      if (e.warn) { chain[i] = null; return; }
      const r1 = row[e.f], r2 = row[e.t];
      const path = [e.f];
      for (let r = r1 + 1; r < r2; r++) {
        const id = ` d${i}@${r}`;
        slots.get(r).push({ id, kind: 'route', key: `${e.f}>${e.t}` });
        path.push(id);
      }
      path.push(e.t);
      for (let j = 0; j < path.length - 1; j++) link(path[j], path[j + 1]);
      chain[i] = path;
    });
    return { slots, up, dn, chain };
  }

  /* Barycentre sweeps with normalised positions, keeping the arrangement that
     crosses least. Alphabetical seed and tie-break keep it deterministic. */
  function pgOrderSlots(slots, up, dn, edges, chain) {
    const keys = [...slots.keys()].sort((a, b) => a - b);
    keys.forEach(k => slots.get(k).sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0)));

    const rowOfSlot = {};
    keys.forEach(k => slots.get(k).forEach(s => { rowOfSlot[s.id] = k; }));
    const idx = {}, span = {};
    const reindex = () => keys.forEach(k => {
      const arr = slots.get(k);
      span[k] = arr.length;
      arr.forEach((s, i) => { idx[s.id] = i; });
    });
    reindex();
    /* fractional position, so bands of different widths compare fairly */
    const p = id => (idx[id] + 0.5) / Math.max(1, span[rowOfSlot[id]]);

    /* crossings over every pair of consecutive chain segments */
    const segs = () => {
      const out = [];
      edges.forEach((e, i) => {
        const path = chain[i];
        if (!path) return;
        for (let j = 0; j < path.length - 1; j++) out.push([path[j], path[j + 1]]);
      });
      return out;
    };
    const countCrossings = () => {
      const sg = segs();
      let n = 0;
      for (let a = 0; a < sg.length; a++) {
        for (let b = a + 1; b < sg.length; b++) {
          const [a1, b1] = sg[a], [a2, b2] = sg[b];
          if (rowOfSlot[a1] !== rowOfSlot[a2] || rowOfSlot[b1] !== rowOfSlot[b2]) continue;
          if (a1 === a2 || b1 === b2) continue;
          if ((idx[a1] - idx[a2]) * (idx[b1] - idx[b2]) < 0) n++;
        }
      }
      return n;
    };

    const snapshot = () => { const m = {}; keys.forEach(k => { m[k] = slots.get(k).map(s => s.id); }); return m; };
    const restore = m => keys.forEach(k => {
      const byId = {}; slots.get(k).forEach(s => { byId[s.id] = s; });
      slots.set(k, m[k].map(id => byId[id]));
    });

    let best = snapshot(), bestX = countCrossings();
    for (let pass = 0; pass < 8; pass++) {
      const down = pass % 2 === 0;
      (down ? keys : [...keys].reverse()).forEach(k => {
        const arr = slots.get(k), bc = {};
        arr.forEach(s => {
          const nb = ((down ? up[s.id] : dn[s.id]) || [])
            .filter(x => idx[x] !== undefined).map(p);
          bc[s.id] = nb.length ? nb.reduce((x, y) => x + y, 0) / nb.length : p(s.id);
        });
        arr.sort((a, b) => (bc[a.id] - bc[b.id]) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
        reindex();
      });
      const x = countCrossings();
      if (x < bestX) { bestX = x; best = snapshot(); }
    }
    restore(best);
    reindex();
    return { keys, crossings: bestX };
  }

  /* ── Prereq graph renderer ── */
  function renderPrereqGraph(items, container, opts) {
    const o = opts || {};
    const t = o.t || DEFAULT_T;
    const termOf = o.termOf || {}, nameOf = o.nameOf || {};
    const statusOf = o.statusOf || {};
    const hasStatus = Object.keys(statusOf).length > 0;
    const { prereqs, dependents, all } = pgAdjacency(items);
    /* Nodes come from edge endpoints only, so a course with no prerequisite row
       was invisible (about a third of a programme). extraNodes puts them back. */
    (o.extraNodes || []).forEach(c => { if (c) all.add(c); });
    if (!all.size) return;
    /* term mode needs at least one declared term; otherwise fall back to the
       chain layout (as the export does) instead of a degenerate single band */
    const hasDeclaredTerm = [...all].some(c => Number.isFinite(termOf[c]));
    const byTerm = o.mode !== 'depth' && hasDeclaredTerm;

    /* Keep the map legible at normal desktop zoom.  The SVG may scroll when a
       chain is genuinely wide, but course codes must never be shrunk into
       thumbnail-sized labels just to force the whole plan into one viewport. */
    const nH = 42, gX = 24, padY = 13, emptyH = 32, padX = 28, rW = 16;
    const requestedTermGutter = Number(o.termGutter);
    const TERM_GUTTER = Number.isFinite(requestedTermGutter)
      ? Math.max(80, Math.min(220, requestedTermGutter))
      : 104;
    const maxChars = Math.max(...[...all].map(c => c.length));
    const nW = Math.max(92, maxChars * 9 + 26);
    const slotW = sl => (sl.kind === 'node' ? nW : rW);

    /* An edge is a "warn" (unsatisfiable as declared) only when BOTH endpoints
       have a declared term; an inferred-term endpoint has no declared term, so
       the "declared same/later term" message would be false there. */
    const edgesFor = (rowMap, inf) => items.map(rw => {
      const f = rw.prerequisite_course_code, t = rw.course_code;
      return { f, t, warn: !inf.has(f) && !inf.has(t) && rowMap[f] >= rowMap[t] };
    });

    /* Measure the active layout only.  Sharing the chain view's (usually much
       wider) viewBox with the term view made the default map about half-size on
       screen even though there was ample room around it. */
    const naturalContentW = (rowMap, inf) => {
      const vals = [...all].map(c => rowMap[c]);
      const { slots: sl } = pgBuildSlots(edgesFor(rowMap, inf), rowMap, Math.min(...vals), Math.max(...vals), all);
      let mx = 0;
      sl.forEach(arr => {
        const w = arr.reduce((a, s) => a + slotW(s), 0) + Math.max(0, arr.length - 1) * gX;
        if (w > mx) mx = w;
      });
      return mx;
    };
    const termLayout = pgTermRows(all, dependents, termOf);
    const depthLayout = pgDepthRows(all, prereqs);

    /* active layout */
    const { row, inferred } = byTerm ? termLayout : depthLayout;
    const gutter = byTerm ? TERM_GUTTER : 0;
    const svgW = Math.max(560, padX * 2 + gutter + naturalContentW(row, inferred));

    /* bands: every row from min..max, so a term with no linked course still
       occupies the axis instead of silently collapsing */
    const rowVals = [...all].map(c => row[c]);
    const minR = Math.min(...rowVals), maxR = Math.max(...rowVals);

    const edges = edgesFor(row, inferred);
    const { slots, up, dn, chain } = pgBuildSlots(edges, row, minR, maxR, all);
    const { keys } = pgOrderSlots(slots, up, dn, edges, chain);

    const bandW = k => {
      const arr = slots.get(k);
      return arr.reduce((a, sl) => a + slotW(sl), 0) + Math.max(0, arr.length - 1) * gX;
    };
    const bandH = {}, bandY = {};
    let y = 0;
    keys.forEach(k => {
      const h = slots.get(k).some(sl => sl.kind === 'node') ? nH + padY * 2 : emptyH;
      bandY[k] = y; bandH[k] = h; y += h;
    });
    const svgH = y;

    /* slot centres, centred inside the content area (right of the gutter) */
    const cx0 = gutter + padX, cxW = svgW - gutter - padX * 2;
    const pos = {};
    keys.forEach(k => {
      const arr = slots.get(k);
      if (!arr.length) return;
      let x = cx0 + Math.max(0, (cxW - bandW(k)) / 2);
      arr.forEach(sl => {
        pos[sl.id] = { x: x + slotW(sl) / 2, y: bandY[k] + bandH[k] / 2, kind: sl.kind };
        x += slotW(sl) + gX;
      });
    });

    /* Grow modestly on a large panel, but never shrink below the natural width.
       A wide chain remains readable inside the host's horizontal scroller. */
    const PG_MAX_SCALE = 1.25;
    const pgMaxW = Math.round(svgW * PG_MAX_SCALE);
    let s = `<svg class="prereq-svg" viewBox="0 0 ${svgW} ${svgH}" style="min-width:${svgW}px;max-width:${pgMaxW}px;height:auto" role="img">`;
    s += '<defs>';
    s += `<marker id="pgA" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><polygon points="0 0,10 4,0 8" fill="var(--teal)" opacity="0.78"/></marker>`;
    s += `<marker id="pgAW" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><polygon points="0 0,10 4,0 8" fill="#b45309" opacity="0.9"/></marker>`;
    s += `<filter id="nSh"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="rgba(17,17,68,0.12)"/></filter>`;
    s += '</defs>';

    /* term bands + gutter labels */
    if (byTerm) {
      keys.forEach(k => {
        const filled = slots.get(k).some(sl => sl.kind === 'node');
        s += `<rect class="pg-band${k % 2 ? ' pg-band-alt' : ''}" x="0" y="${bandY[k]}" width="${svgW}" height="${bandH[k]}"/>`;
        s += `<line class="pg-band-rule" x1="0" y1="${bandY[k]}" x2="${svgW}" y2="${bandY[k]}"/>`;
        const ly = bandY[k] + bandH[k] / 2;
        s += `<text class="pg-band-lbl${filled ? '' : ' pg-band-lbl-empty'}" x="${gutter - 14}" y="${ly}" text-anchor="end" dominant-baseline="middle">${pgEsc(t.termHeading(k))}</text>`;
        if (!filled) {
          s += `<text class="pg-band-note" x="${gutter + 14}" y="${ly}" dominant-baseline="middle">${pgEsc(t.pgNoTermBand)}</text>`;
        }
      });
      s += `<line class="pg-gutter-rule" x1="${gutter}" y1="0" x2="${gutter}" y2="${svgH}"/>`;
    }

    /* edges */
    const offRows = [];
    edges.forEach((e, i) => {
      const f = pos[e.f], t = pos[e.t];
      if (!f || !t) return;
      const attrs = `data-f="${pgEsc(e.f)}" data-t="${pgEsc(e.t)}"`;
      if (e.warn) {
        /* a prereq at or after its own dependent cannot be drawn downward —
           bow it out sideways and mark it as a data warning */
        if (byTerm) offRows.push({ f: e.f, t: e.t, same: row[e.f] === row[e.t] });
        const dir = t.x >= f.x ? 1 : -1;
        const x1 = f.x + dir * (nW / 2), x2 = t.x - dir * (nW / 2);
        const yb = Math.max(f.y, t.y) + nH / 2 + 14;
        s += `<path class="pg-edge pg-edge-warn" d="M${x1},${f.y} C${x1 + dir * 26},${yb} ${x2 - dir * 26},${yb} ${x2},${t.y}" ${attrs} marker-end="url(#pgAW)"/>`;
        return;
      }
      /* walk the chain: node bottom → routing points → node top */
      const path = chain[i] || [e.f, e.t];
      const pts = path.map((id, j) => {
        const q = pos[id];
        if (j === 0) return { x: q.x, y: q.y + nH / 2 };
        if (j === path.length - 1) return { x: q.x, y: q.y - nH / 2 };
        return { x: q.x, y: q.y };
      });
      let d = `M${pts[0].x},${pts[0].y}`;
      for (let j = 0; j < pts.length - 1; j++) {
        const a = pts[j], b = pts[j + 1], cy = (a.y + b.y) / 2;
        d += ` C${a.x},${cy} ${b.x},${cy} ${b.x},${b.y}`;
      }
      s += `<path class="pg-edge" d="${d}" ${attrs} marker-end="url(#pgA)"/>`;
    });

    /* nodes */
    const isPreOf = new Set(items.map(r => r.prerequisite_course_code));
    let anyInferred = false, anyGate = false;
    all.forEach(c => {
      const p = pos[c]; if (!p) return;
      const gate = PG_GATE_RE.exec(c);
      const isInferred = inferred.has(c);
      const hasP = (prereqs[c] || []).length > 0, isP = isPreOf.has(c);
      let fl = 'rgba(255,255,255,0.65)', st = 'rgba(17,17,68,0.08)', tc = 'var(--navy)';
      let dash = '', rx = 8, label = c, cls = 'pg-node';
      if (gate) {
        anyGate = true;
        fl = 'rgba(180,83,9,0.07)'; st = 'rgba(180,83,9,0.30)'; tc = '#b45309';
        rx = nH / 2; dash = ' stroke-dasharray="4 3"'; cls += ' pg-node-gate';
        label = IS_AR ? `الساعات المعتمدة: ${gate[1]}` : `${gate[1]} hrs`;
      } else if (statusOf[c]) {
        /* personalised: the student's own progress outranks the structural role */
        const S = {
          passed:   ['rgba(10,142,110,0.24)', 'rgba(10,142,110,0.72)', '#075f49'],
          studying: ['rgba(64,86,227,0.20)',  'rgba(64,86,227,0.68)',  '#3043bd'],
          open:     ['rgba(255,255,255,0.92)', '#0a8e6e',              'var(--navy)'],
          locked:   ['rgba(120,124,150,0.09)', 'rgba(120,124,150,0.42)', '#596173'],
        }[statusOf[c]];
        if (S) { fl = S[0]; st = S[1]; tc = S[2]; cls += ' pg-node-' + statusOf[c]; }
        if (statusOf[c] === 'open') { cls += ' pg-node-open'; }
        if (isInferred) { anyInferred = true; dash = ' stroke-dasharray="5 3"'; cls += ' pg-node-inferred'; }
      } else {
        if (!hasP && isP) { fl = 'rgba(10,142,110,0.08)'; st = 'rgba(10,142,110,0.22)'; tc = '#087a5e'; }
        else if (hasP && !isP) { fl = 'rgba(64,86,227,0.08)'; st = 'rgba(64,86,227,0.22)'; tc = '#3548c9'; }
        if (isInferred) { anyInferred = true; dash = ' stroke-dasharray="5 3"'; cls += ' pg-node-inferred'; }
      }
      /* tooltip: course name and term make the band label concrete */
      const bits = [c];
      if (nameOf[c]) bits.push(nameOf[c]);
      if (gate) bits.push(t.pgGateTip(gate[1]));
      else if (isInferred) bits.push(t.pgInferredTip);
      else if (typeof termOf[c] === 'number') bits.push(t.pgTermTip(termOf[c]));
      s += `<g class="${cls}" data-c="${pgEsc(c)}"><title>${pgEsc(bits.join(' — '))}</title>`
        + `<rect x="${p.x - nW / 2}" y="${p.y - nH / 2}" width="${nW}" height="${nH}" rx="${rx}" fill="${fl}" stroke="${st}" stroke-width="${statusOf[c] === 'open' ? 2.4 : 1.6}"${dash} filter="url(#nSh)"/>`
        + `<text x="${p.x}" y="${p.y + 1}" text-anchor="middle" dominant-baseline="middle" fill="${tc}" font-family="var(--font-mono)" font-size="13" font-weight="750">${pgEsc(label)}</text></g>`;
    });
    s += '</svg>';

    /* legend — only advertise the states actually on screen. When the graph is
       coloured by a student's own progress the structural roles are not what the
       colours mean, so the legend must describe the progress instead. */
    let leg;
    if (hasStatus) {
      const seen = new Set(Object.keys(statusOf).filter(c => all.has(c)).map(c => statusOf[c]));
      const L = [
        ['passed', 'rgba(10,142,110,0.55)', t.pgPassed],
        ['studying', 'rgba(64,86,227,0.50)', t.pgStudying],
        ['open', '#0a8e6e', t.pgOpen],
        ['locked', 'rgba(120,124,150,0.45)', t.pgLocked],
      ].filter(x => seen.has(x[0]));
      leg = L.map(([, col, lab]) =>
        `<span><span class="pg-legend-dot" style="color:${col};background:${col}"></span>${pgEsc(lab)}</span>`
      ).join('');
    } else {
      leg = `<span><span class="pg-legend-dot text-teal" style="background:var(--teal)"></span>${pgEsc(t.pgFoundation)}</span>`
      + `<span><span class="pg-legend-dot text-royal" style="background:var(--royal)"></span>${pgEsc(t.pgTerminal)}</span>`
      + `<span><span class="pg-legend-dot" style="color:var(--navy);background:var(--navy)"></span>${pgEsc(t.pgIntermediate)}</span>`;
    }
    if (anyGate) leg += `<span><span class="pg-legend-dot" style="color:#b45309;background:#b45309"></span>${pgEsc(t.pgGate)}</span>`;
    if (anyInferred) leg += `<span><span class="pg-legend-dash"></span>${pgEsc(t.pgInferred)}</span>`;
    leg += `<span style="margin-inline-start:auto;opacity:0.4;font-weight:400">${pgEsc(t.pgHoverHint)}</span>`;
    s += `<div class="pg-legend">${leg}</div>`;

    /* surface term-order defects the band layout exposes */
    const sameN = offRows.filter(e => e.same).length, backN = offRows.length - sameN;
    if (sameN || backN) {
      const detail = offRows.slice(0, 6)
        .map(e => `${e.f} → ${e.t}`).join(', ') + (offRows.length > 6 ? ' …' : '');
      const msgs = [];
      if (sameN) msgs.push(t.pgSameTermWarn(sameN));
      if (backN) msgs.push(t.pgBackwardWarn(backN));
      s += `<div class="pg-warn" role="status">${pgEsc(msgs.join(' '))} <span class="pg-warn-detail">${pgEsc(detail)}</span></div>`;
    }

    container.innerHTML = s;

    /* interactivity — hover highlights the full chain */
    container.querySelectorAll('.pg-node').forEach(nd => {
      nd.addEventListener('mouseenter', () => {
        const cc = nd.dataset.c, conn = new Set();
        (function up(x) { conn.add(x); (prereqs[x] || []).forEach(p => { if (!conn.has(p)) up(p); }); })(cc);
        (function dn(x) { conn.add(x); (dependents[x] || []).forEach(k => { if (!conn.has(k)) dn(k); }); })(cc);
        container.querySelectorAll('.pg-node').forEach(n => { n.classList.toggle('hl', conn.has(n.dataset.c)); n.classList.toggle('dm', !conn.has(n.dataset.c)); });
        container.querySelectorAll('.pg-edge').forEach(e => { const ok = conn.has(e.dataset.f) && conn.has(e.dataset.t); e.classList.toggle('hl', ok); e.classList.toggle('dm', !ok); });
      });
      nd.addEventListener('mouseleave', () => {
        container.querySelectorAll('.pg-node,.pg-edge').forEach(el => { el.classList.remove('hl', 'dm'); });
      });
    });
  }
  window.PrereqGraph = {
    render: function (items, container, opts) {
      const o = Object.assign({}, opts || {});
      o.t = Object.assign({}, DEFAULT_T, o.t || {});
      return renderPrereqGraph(items, container, o);
    },
    adjacency: pgAdjacency,
  };
})();
