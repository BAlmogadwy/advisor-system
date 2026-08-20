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

  const graph = presentation.graph || {};
  const bandLabels = presentation.band_labels || {};
  const AR = (document.documentElement.lang || '').toLowerCase().startsWith('ar');
  const MOBILE_QUERY = '(max-width: 768px)';

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

  function graphStrings() {
    return {
      termHeading: bandLabel,
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
      pgStudying: AR ? 'يُفترض اجتيازه بنهاية فصل البداية' : 'assumed passed in the planning baseline term',
      pgOpen: AR ? 'مُدرج في فصل تقديري' : 'planned in the scenario',
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

  function setButtonState() {
    const termIsActive = mode === 'term';
    termButton.classList.toggle('is-on', termIsActive);
    termButton.setAttribute('aria-pressed', String(termIsActive));
    chainButton.classList.toggle('is-on', !termIsActive);
    chainButton.setAttribute('aria-pressed', String(!termIsActive));
  }

  function draw() {
    /* The server-rendered term list is the sole presentation on narrow screens. */
    if (isMobile() || !window.PrereqGraph) return false;

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
      t: graphStrings(),
    });
    drawn = true;
    return true;
  }

  function chooseMode(nextMode) {
    if (isMobile() || mode === nextMode) return;
    mode = nextMode;
    setButtonState();
    if (details.open && drawn) draw();
  }

  details.addEventListener('toggle', function () {
    if (details.open && !drawn) draw();
  });
  termButton.addEventListener('click', function () { chooseMode('term'); });
  chainButton.addEventListener('click', function () { chooseMode('depth'); });

  /* An explicitly open disclosure still renders lazily at enhancement time. */
  if (details.open) draw();
})();
