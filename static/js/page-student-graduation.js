/* Standalone graduation-scenario graph controller.

   The server owns the summary and the narrow-screen term list. This file only
   enhances the desktop disclosure with the shared prerequisite SVG, and waits
   until the student opens that disclosure before doing the expensive layout. */
(function () {
  'use strict';

  const presentation = window.__STUDENT_GRADUATION_PRESENTATION__;
  const details = document.getElementById('sgMapDetails');
  const host = document.getElementById('sgGraph');
  const termButton = document.getElementById('sgPgTerm');
  const chainButton = document.getElementById('sgPgChain');

  if (!presentation || !details || !host || !termButton || !chainButton) return;

  const sourceGraph = presentation.graph || {};

  /* A completed student's whole history can put forty passed courses in one
     "before baseline" row and reduce the actual remaining chain to thumbnail
     size. Keep every unfinished node plus its immediate completed prerequisites.
     Older completed ancestry remains in the progress record: it no longer blocks
     a remaining course and does not need to crowd this decision-focused map. */
  function focusRemainingPath(source) {
    const items = Array.isArray(source.items) ? source.items : [];
    const statuses = source.statusOf || {};
    const allNodes = Array.isArray(source.extraNodes) ? source.extraNodes : [];
    const kept = new Set(allNodes.filter(code => statuses[code] !== 'passed'));
    items.forEach(edge => {
      if (kept.has(edge.course_code) && statuses[edge.prerequisite_course_code] === 'passed') {
        kept.add(edge.prerequisite_course_code);
      }
    });
    const pick = values => Object.fromEntries(
      Object.entries(values || {}).filter(([code]) => kept.has(code))
    );
    return {
      items: items.filter(edge => (
        kept.has(edge.course_code) && kept.has(edge.prerequisite_course_code)
      )),
      termOf: pick(source.termOf),
      nameOf: pick(source.nameOf),
      statusOf: pick(statuses),
      extraNodes: Array.from(kept),
    };
  }

  const graph = focusRemainingPath(sourceGraph);
  const bandLabels = presentation.band_labels || {};
  const baselineKind = presentation.planning_baseline_kind || 'registered_timetable';
  const AR = (document.documentElement.lang || '').toLowerCase().startsWith('ar');
  const MOBILE_QUERY = '(max-width: 768px)';
  const renderOnMobile = details.dataset.renderMobile === 'true';

  function isMobile() {
    if (typeof window.matchMedia === 'function') {
      return window.matchMedia(MOBILE_QUERY).matches;
    }
    return Number(window.innerWidth || 0) <= 768;
  }

  /* Values arrive in a deliberately language-neutral presentation contract. */
  function localiseBand(value) {
    const label = String(value == null ? '' : value);
    if (label === 'Completed before the scenario') {
      return AR ? 'مجتاز قبل فصل البداية' : 'Completed before the scenario';
    }
    if (label.indexOf('Recommended starting term ') === 0) {
      const term = label.slice('Recommended starting term '.length);
      return AR
        ? `المقررات الموصى بها لفصل البداية: ${term}`
        : `Recommended starting-term courses ${term}`;
    }
    if (label.indexOf('Registered timetable ') === 0) {
      const term = label.slice('Registered timetable '.length);
      return AR ? `الجدول المسجّل لفصل البداية: ${term}` : `Registered timetable ${term}`;
    }
    if (label.indexOf('Optimized current offerings ') === 0) {
      const term = label.slice('Optimized current offerings '.length);
      return AR
        ? `العروض المحسّنة لفصل البداية: ${term}`
        : `Optimized current offerings ${term}`;
    }
    if (label.indexOf('Planning baseline ') === 0) {
      const term = label.slice('Planning baseline '.length);
      return AR ? `فصل البداية: ${term}` : `Planning baseline term ${term}`;
    }
    if (label.indexOf('Current ') === 0) {
      // Stored adviser presentations created before the baseline terminology
      // changed still use this prefix. Read them without repeating the old,
      // potentially false claim that the configured planning term is "current".
      const term = label.slice('Current '.length);
      return AR ? `فصل البداية: ${term}` : `Planning baseline term ${term}`;
    }
    if (label.indexOf('Projected ') === 0) {
      const term = label.slice('Projected '.length);
      return AR ? `فصل تقديري: ${term}` : `Projected term ${term}`;
    }
    return label;
  }

  function bandLabel(number) {
    const key = String(number);
    if (Object.prototype.hasOwnProperty.call(bandLabels, key)) {
      return localiseBand(bandLabels[key]);
    }
    return AR ? `الفصل التقديري: ${number}` : `Scenario term ${number}`;
  }

  /* Full descriptions belong in the tooltip and accessible label. The fixed
     left axis needs a concise label that remains readable at every viewport. */
  function compactBandLabel(number) {
    const key = String(number);
    const raw = Object.prototype.hasOwnProperty.call(bandLabels, key)
      ? String(bandLabels[key] == null ? '' : bandLabels[key])
      : '';
    if (raw === 'Completed before the scenario') {
      return AR ? 'قبل البداية' : 'Before baseline';
    }
    const baselinePrefixes = [
      'Recommended starting term ',
      'Registered timetable ',
      'Optimized current offerings ',
      'Planning baseline ',
      'Current ',
    ];
    const baselinePrefix = baselinePrefixes.find(prefix => raw.indexOf(prefix) === 0);
    if (baselinePrefix) {
      const term = raw.slice(baselinePrefix.length);
      return AR ? `البداية ${term}` : `Baseline ${term}`;
    }
    if (raw.indexOf('Projected ') === 0) {
      const term = raw.slice('Projected '.length);
      return AR ? `الفصل ${term}` : `Term ${term}`;
    }
    return AR ? `الفصل ${number}` : `Term ${number}`;
  }

  function graphStrings() {
    return {
      termHeading: compactBandLabel,
      termHeadingTitle: bandLabel,
      pgNoTermBand: AR ? 'خارج الفصول التي شملها التقدير' : 'Outside the scenario order',
      pgGateTip: hours => (AR
        ? `شرط الساعات المعتمدة: ${hours}`
        : `Credit-hour requirement: ${hours} credits`),
      pgInferredTip: AR
        ? 'موضع تقديري خارج الفصول المرتّبة'
        : 'Position inferred outside the scenario order',
      pgTermTip: bandLabel,
      pgGate: AR ? 'شرط الساعات المعتمدة' : 'credit-hour requirement',
      pgInferred: AR ? 'موضع محدّد تقديريًا' : 'inferred position',
      pgFoundation: AR ? 'بداية سلسلة المتطلبات' : 'chain start',
      pgIntermediate: AR ? 'وسط سلسلة المتطلبات' : 'chain middle',
      pgTerminal: AR ? 'نهاية سلسلة المتطلبات' : 'chain end',
      pgHoverHint: AR ? 'مرّر على مقرر لإبراز سلسلة متطلباته' : 'hover to highlight a chain',
      pgPassed: AR ? 'مجتاز قبل فصل البداية' : 'completed before the scenario',
      pgStudying: AR ? 'مسجّل فعليًا، ويُفترض اجتيازه' : 'registered; assumed passed by term end',
      pgOpen: baselineKind === 'recommended_current_term' || baselineKind === 'optimized_current_offerings'
        ? (AR ? 'مقرر مقترح في المسار' : 'proposed in the scenario')
        : (AR ? 'مُدرج في فصل تقديري' : 'planned in the scenario'),
      pgLocked: AR ? 'تعذّر تحديد فصل مناسب له' : 'unresolved',
      pgSameTermWarn: count => (AR
        ? `علاقات متطلبات سابقة داخل الفصل نفسه: ${count}.`
        : `${count} prerequisite relation(s) within one scenario term`),
      pgBackwardWarn: count => (AR
        ? `علاقات يظهر فيها المتطلب السابق بعد المقرر الذي يعتمد عليه: ${count}.`
        : `${count} prerequisite relation(s) scheduled after their course`),
    };
  }

  let mode = 'term';
  let drawn = false;
  let lastDrawWidth = 0;
  let resizeFrame = 0;
  const scroller = host.closest('.ap-grad-graph-scroll');

  function setButtonState() {
    const termIsActive = mode === 'term';
    termButton.classList.toggle('is-on', termIsActive);
    termButton.setAttribute('aria-pressed', String(termIsActive));
    chainButton.classList.toggle('is-on', !termIsActive);
    chainButton.setAttribute('aria-pressed', String(!termIsActive));
  }

  function draw() {
    /* The server-rendered term list is the sole presentation on narrow screens. */
    if ((isMobile() && !renderOnMobile) || !window.PrereqGraph) return false;

    host.setAttribute('dir', 'ltr');
    host.setAttribute('role', 'img');
    if (!host.hasAttribute('aria-label')) {
      host.setAttribute(
        'aria-label',
        AR ? 'خريطة المسار التقديري حتى إكمال الخطة الدراسية' : 'Graduation scenario path map'
      );
    }

    window.PrereqGraph.render(graph.items || [], host, {
      termOf: graph.termOf || {},
      nameOf: graph.nameOf || {},
      statusOf: graph.statusOf || {},
      extraNodes: graph.extraNodes || [],
      mode: mode,
      fitToWidth: true,
      t: graphStrings(),
    });
    lastDrawWidth = Math.round((scroller || host).clientWidth || 0);
    drawn = true;
    return true;
  }

  function chooseMode(nextMode) {
    if ((isMobile() && !renderOnMobile) || mode === nextMode) return;
    mode = nextMode;
    setButtonState();
    if (details.open && drawn) {
      if (scroller) scroller.scrollLeft = 0;
      draw();
    }
  }

  details.addEventListener('toggle', function () {
    if (details.open && !drawn) draw();
  });
  termButton.addEventListener('click', function () { chooseMode('term'); });
  chainButton.addEventListener('click', function () { chooseMode('depth'); });

  const modeButtons = [
    { button: termButton, mode: 'term' },
    { button: chainButton, mode: 'depth' },
  ];
  modeButtons.forEach((entry, index) => {
    entry.button.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let nextIndex = index;
      if (event.key === 'Home') nextIndex = 0;
      else if (event.key === 'End') nextIndex = modeButtons.length - 1;
      else nextIndex = (index + (event.key === 'ArrowRight' ? 1 : -1) + modeButtons.length) % modeButtons.length;
      chooseMode(modeButtons[nextIndex].mode);
      modeButtons[nextIndex].button.focus();
    });
  });

  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver(() => {
      const width = Math.round((scroller || host).clientWidth || 0);
      if (!details.open || !drawn || !width || Math.abs(width - lastDrawWidth) < 8) return;
      if (resizeFrame) cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => {
        resizeFrame = 0;
        draw();
      });
    });
    observer.observe(scroller || host);
  }

  /* An explicitly open disclosure still renders lazily at enhancement time. */
  if (details.open) draw();
})();
