/* "Mainly for programme": a section's programme mark (owner 2026-10-21).

   Shared by the Timetable Workspace and its split view, which draw it in
   their own markup: this module holds the words and the parts — the choices,
   the seated mix, the "nobody needs it" warning, the grouping trade-off — and
   each page wraps them and saves through its own fetch helper.

   The server adds `preferred_programme` (and `preferred_programme_editable`)
   to a placement only with the feature on; without the key nothing renders.
   The seated mix comes from the plan lens a page already holds; it is never
   reloaded per edit (a full seating evaluation), only on request. */
(function () {
  'use strict';
  const isArabic = () => (typeof LANGUAGE_CODE === 'string' ? LANGUAGE_CODE : document.documentElement.lang) === 'ar';
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const words = {
    get label() { return isArabic() ? 'مخصّصة أساساً لبرنامج' : 'Mainly for programme'; },
    get any() { return isArabic() ? 'أي برنامج' : 'Any'; },
    get seated() { return isArabic() ? 'المقاعد' : 'Seated'; },
    get loading() { return isArabic() ? 'جارٍ حساب توزيع البرامج…' : 'Loading programme mix…'; },
    get saved() { return isArabic() ? 'حُفظ برنامج الشعبة' : 'Section programme saved'; },
  };

  function hasMark(placement) {
    return Boolean(placement) && Object.prototype.hasOwnProperty.call(placement, 'preferred_programme');
  }

  /* [[value, label]]: "Any", then the scenario's programmes (and the current
     mark, if it is not among them). */
  function options(placement, plans) {
    const mark = String(placement.preferred_programme || '');
    const list = (plans || []).filter(plan => plan && plan !== 'UNKNOWN');
    if (mark && !list.includes(mark)) list.push(mark);
    return [['', words.any], ...list.map(plan => [plan, plan])];
  }

  function optionsHtml(placement, plans) {
    const mark = String(placement.preferred_programme || '');
    return options(placement, plans)
      .map(([value, label]) => `<option value="${escapeHtml(value)}"${value === mark ? ' selected' : ''}>${escapeHtml(label)}</option>`)
      .join('');
  }

  /* Who sits there, from a section's lens row: "38: AI2 30 · AI 6". */
  function mixHtml(sectionLens) {
    const total = Number(sectionLens?.actual_total || 0);
    const entries = Object.entries(sectionLens?.actual_plans || {})
      .filter(([, count]) => Number(count) > 0)
      .sort((a, b) => Number(b[1]) - Number(a[1]) || String(a[0]).localeCompare(String(b[0])));
    if (!sectionLens || !total || !entries.length) return '';
    const parts = entries.map(([plan, count]) => `<bdi>${escapeHtml(plan)}</bdi> ${Number(count)}`).join(' · ');
    // One LTR isolate: the figure leads with digits, which an Arabic page
    // would otherwise paint as «23 AI2 :23».
    return `<bdi dir="ltr">${total}: ${parts}</bdi>`;
  }

  /* The mark names a programme none of whose students needs the course (the
     programme isolated, as it reads inside Arabic prose). */
  function nobodyHtml(placement, courseLens) {
    const mark = String(placement.preferred_programme || '');
    if (!mark || !courseLens || Number((courseLens.plans || {})[mark] || 0)) return '';
    return isArabic()
      ? `لا يحتاج أي طالب من <bdi>${escapeHtml(mark)}</bdi> هذا المقرر؛ العلامة توجّه البرامج الأخرى إلى الشعب الأخرى فقط.`
      : `No <bdi>${escapeHtml(mark)}</bdi> student needs this course; the mark only steers other programmes to the other sections.`;
  }

  /* What grouping by programme cost, from the lens. */
  function tradeHtml(grouping) {
    if (!grouping || !Number.isFinite(Number(grouping.students_regrouped))) return '';
    const moved = Number(grouping.students_regrouped);
    const minutes = Number(grouping.student_gap_minutes_change || 0);
    // The signed figure is isolated so Arabic prose never paints «450+».
    const signed = `<bdi dir="ltr">${minutes > 0 ? '+' : minutes < 0 ? '−' : '±'}${Math.abs(minutes)}</bdi>`;
    return isArabic()
      ? `نقل التجميع ${moved} طالباً؛ إجمالي انتظار الطلاب ${signed} دقيقة`
      : `Grouping moved ${moved} student${moved === 1 ? '' : 's'}; total student waiting ${signed} min`;
  }

  /* The mark saves as its select changes. Chrome on Windows fires `change`
     for every arrow key on a closed select, which would save each programme
     passed on the way: a choice made from the keyboard saves once the keys
     rest (or at once on Enter, or on leaving the select); a pointer choice
     saves at once. Delegated from `root`, so a select drawn again later is
     covered too; `save(capture(select), value)` is called once per settled
     choice, with what `capture` read from the select when it changed. */
  const SETTLE_MS = 700;
  function saveOnSettle(root, selector, save, capture = select => select) {
    const state = new WeakMap();
    const of = select => {
      if (!state.has(select)) state.set(select, { timer: 0, keyed: false });
      return state.get(select);
    };
    const target = event => (event.target && event.target.closest ? event.target.closest(selector) : null);
    const commit = select => {
      const entry = of(select);
      if (!entry.timer) return;
      clearTimeout(entry.timer);
      entry.timer = 0;
      save(entry.target, entry.value);
    };
    root.addEventListener('pointerdown', event => { const select = target(event); if (select) of(select).keyed = false; });
    root.addEventListener('keydown', event => {
      const select = target(event);
      if (!select) return;
      of(select).keyed = true;
      if (event.key === 'Enter') commit(select);
    });
    root.addEventListener('change', event => {
      const select = target(event);
      if (!select) return;
      const entry = of(select);
      clearTimeout(entry.timer);
      entry.timer = 0;
      // What the choice is about, read now: the select may be drawn again (or
      // taken off the page) before a keyboard choice settles.
      entry.target = capture(select);
      entry.value = select.value;
      if (!entry.keyed) { save(entry.target, entry.value); return; }
      entry.timer = setTimeout(() => commit(select), api.settleMs);
    });
    root.addEventListener('focusout', event => { const select = target(event); if (select) commit(select); });
  }

  const api = { words, hasMark, options, optionsHtml, mixHtml, nobodyHtml, tradeHtml, saveOnSettle, settleMs: SETTLE_MS };
  window.TimetableProgrammeMark = api;
})();
