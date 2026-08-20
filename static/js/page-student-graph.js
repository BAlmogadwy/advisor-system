/* Personalised prerequisite map on the student portal.

   Two renderings of the SAME payload, chosen by viewport:
     >= 768px  the shared SVG map (static/js/prereq-graph.js), coloured by progress
     <  768px  a vertical term-by-term list — the SVG is ~1950px wide for a 48-course
               plan, so on a phone it is a horizontally scrolling island. The list
               carries the same information (which term, which course, what state)
               in a shape that actually reads on a narrow screen.

   The dedicated map page renders immediately. A legacy <details> host is still
   supported and renders lazily when opened. */
(function () {
  'use strict';
  const payload = window.__STUDENT_GRAPH__;
  const wrap = document.getElementById('scGraphWrap');
  const host = document.getElementById('scGraph');
  const modes = document.querySelector('.pg-modes');
  if (!payload || !wrap || !host) return;
  const disclosure = wrap.tagName === 'DETAILS';

  const AR = (document.documentElement.lang || '').startsWith('ar');
  const NARROW = '(max-width: 768px)';
  const T = {
    term: n => (AR ? `المستوى ${n}` : `Term ${n}`),
    noTerm: AR ? 'مقررات لم يُحدّد مستواها' : 'no term set',
    passed: AR ? 'مجتاز' : 'passed',
    studying: AR ? 'قيد الدراسة حاليًا' : 'studying',
    open: AR ? 'متطلباته مستوفاة' : 'open now',
    locked: AR ? 'متطلباته غير مستوفاة' : 'blocked',
  };
  /* status -> existing pill classes; no new CSS */
  const PILL = {
    passed: 'pill-status pill-g',
    studying: 'pill-status pill-b',
    open: 'pill-status pill-sky',   // cyan: pill-teal is too close to pill-g (passed)
    locked: 'pill-status pill-muted',
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function isNarrow() {
    return window.matchMedia && window.matchMedia(NARROW).matches;
  }

  /* ── mobile: one block per term, courses as coloured pills ── */
  function drawList() {
    const termOf = payload.termOf || {}, statusOf = payload.statusOf || {}, nameOf = payload.nameOf || {};
    const codes = (payload.extraNodes || []).slice();
    const byTerm = new Map();
    codes.forEach(c => {
      const t = Number.isFinite(termOf[c]) ? termOf[c] : 0;
      if (!byTerm.has(t)) byTerm.set(t, []);
      byTerm.get(t).push(c);
    });
    const terms = [...byTerm.keys()].sort((a, b) => (a === 0 ? 1 : b === 0 ? -1 : a - b));

    let html = '';
    terms.forEach(t => {
      const list = byTerm.get(t).sort();
      const headingId = `scGraphTerm${t || 'None'}`;
      html += `<section class="mb-2" aria-labelledby="${headingId}"><div class="card-heading" id="${headingId}">${esc(t ? T.term(t) : T.noTerm)}</div><div role="list">`;
      list.forEach(c => {
        const st = statusOf[c] || 'locked';
        const title = nameOf[c] ? `${c} — ${nameOf[c]} (${T[st] || st})` : c;
        html += `<span class="${PILL[st] || PILL.locked}" title="${esc(title)}" dir="ltr" role="listitem">`
          + `<span class="pill-dot" aria-hidden="true"></span>${esc(c)}</span> `;
      });
      html += '</div></section>';
    });

    /* key, using the same pills so the colours are self-explanatory */
    html += '<div class="mt-2">';
    ['passed', 'studying', 'open', 'locked'].forEach(k => {
      if (!Object.values(payload.statusOf || {}).includes(k)) return;
      html += `<span class="${PILL[k]}"><span class="pill-dot" aria-hidden="true"></span>${esc(T[k])}</span> `;
    });
    html += '</div>';

    host.removeAttribute('dir');
    host.classList.remove('overflow-x');
    host.setAttribute('role', 'region');
    host.innerHTML = html;
  }

  function drawSvg() {
    if (!window.PrereqGraph) return;
    host.setAttribute('dir', 'ltr');
    host.classList.add('overflow-x');
    host.setAttribute('role', 'img');
    host.innerHTML = '';
    window.PrereqGraph.render(payload.items || [], host, {
      termOf: payload.termOf || {},
      nameOf: payload.nameOf || {},
      statusOf: payload.statusOf || {},
      extraNodes: payload.extraNodes || [],
      mode: mode,
    });
  }

  let mode = 'term';
  let drawn = false;
  let lastNarrow = null;

  function draw() {
    const narrow = isNarrow();
    lastNarrow = narrow;
    if (modes) modes.style.display = narrow ? 'none' : '';  // By term/By chain is SVG-only
    if (narrow) drawList(); else drawSvg();
    drawn = true;
  }

  if (disclosure) {
    wrap.addEventListener('toggle', function () {
      if (wrap.open && !drawn) draw();
    });
  }

  /* re-render when the phone rotates across the breakpoint */
  let t = null;
  window.addEventListener('resize', function () {
    if (!drawn || (disclosure && !wrap.open)) return;
    clearTimeout(t);
    t = setTimeout(function () { if (isNarrow() !== lastNarrow) draw(); }, 200);
  });

  const bTerm = document.getElementById('scPgTerm');
  const bChain = document.getElementById('scPgChain');
  function setMode(next, on, off) {
    mode = next;
    on.classList.add('is-on'); on.setAttribute('aria-pressed', 'true');
    off.classList.remove('is-on'); off.setAttribute('aria-pressed', 'false');
    draw();
  }
  if (bTerm && bChain) {
    bTerm.addEventListener('click', () => setMode('term', bTerm, bChain));
    bChain.addEventListener('click', () => setMode('depth', bChain, bTerm));
  }

  if (!disclosure || wrap.open || wrap.dataset.autoRender === 'true') draw();
})();
