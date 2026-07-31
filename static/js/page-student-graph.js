/* Personalised prerequisite map on the student portal.
   Draws the SAME graph as the advisor dashboard (static/js/prereq-graph.js),
   coloured by this student's own progress. Renders lazily: the graph is inside a
   collapsed <details>, and laying out ~50 nodes before the student asks for it
   would be wasted work on a phone. */
(function () {
  'use strict';
  const payload = window.__STUDENT_GRAPH__;
  const wrap = document.getElementById('scGraphWrap');
  const host = document.getElementById('scGraph');
  if (!payload || !wrap || !host || !window.PrereqGraph) return;

  const AR = (document.documentElement.lang || '').startsWith('ar');
  let drawn = false;
  let mode = 'term';

  function draw() {
    host.innerHTML = '';
    window.PrereqGraph.render(payload.items || [], host, {
      termOf: payload.termOf || {},
      nameOf: payload.nameOf || {},
      statusOf: payload.statusOf || {},
      extraNodes: payload.extraNodes || [],
      mode: mode,
    });
    drawn = true;
  }

  wrap.addEventListener('toggle', function () {
    if (wrap.open && !drawn) draw();
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
})();
