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
     covered too.

     `capture(select)` names what the choice is about (the section), read when
     it changes; `save(that, value)` is called once per settled choice. The
     wait belongs to that section, not to an element: a select drawn again
     while the keys move carries on the same wait (no intermediate save), and
     one taken off the page by a redraw (the browser blurs it as it goes) is
     not "leaving" — its wait still ends in one save. */
  const SETTLE_MS = 700;
  function saveOnSettle(root, selector, save, capture = select => select) {
    const keyed = new WeakMap();   // select → its last input came from the keyboard
    const waiting = new Map();     // what the choice is about → { timer, value }
    const target = event => (event.target && event.target.closest ? event.target.closest(selector) : null);
    const commit = about => {
      const entry = waiting.get(about);
      if (!entry) return;
      clearTimeout(entry.timer);
      waiting.delete(about);
      save(about, entry.value);
    };
    root.addEventListener('pointerdown', event => { const select = target(event); if (select) keyed.set(select, false); });
    root.addEventListener('keydown', event => {
      const select = target(event);
      if (!select) return;
      keyed.set(select, true);
      if (event.key === 'Enter') commit(capture(select));
    });
    root.addEventListener('change', event => {
      const select = target(event);
      if (!select) return;
      const about = capture(select);
      const earlier = waiting.get(about);
      if (earlier) clearTimeout(earlier.timer);
      waiting.delete(about);
      if (!keyed.get(select)) { save(about, select.value); return; }
      const entry = { value: select.value, timer: 0 };
      entry.timer = setTimeout(() => commit(about), api.settleMs);
      waiting.set(about, entry);
    });
    root.addEventListener('focusout', event => {
      const select = target(event);
      if (!select) return;
      const about = capture(select);
      // After the event: a select still on the page was left by the user.
      queueMicrotask(() => { if (select.isConnected) commit(about); });
    });
  }

  const api = { words, hasMark, options, optionsHtml, mixHtml, nobodyHtml, tradeHtml, saveOnSettle, settleMs: SETTLE_MS };
  window.TimetableProgrammeMark = api;
})();
