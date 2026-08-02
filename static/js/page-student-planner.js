/* The student's timetable planner.
 *
 * This file renders and posts. It decides NOTHING: not whether a course may be
 * taken, not which sections are open to this student, not whether a rebuild has
 * been approved, not which timetables are still current. Every one of those is a
 * server answer, because a rule that also exists in JavaScript is a rule with two
 * implementations and one of them ships to whoever wants to edit it.
 *
 * The rebuild confirmation is the clearest case. The dialog below asks the
 * question; it does not grant the permission. The answer to "yes, rebuild" is a
 * token the server issued, and generate/ refuses without one — so a client that
 * posts `{"confirmed": true}`, or skips the dialog entirely, gets 428.
 */
(function () {
  'use strict';

  const root = document.querySelector('.sp-layout');
  if (!root) return;

  const draftId = root.dataset.draftId;
  const base = '/student/planner/drafts/' + encodeURIComponent(draftId) + '/';

  const el = {
    requested: document.getElementById('spRequested'),
    requestedEmpty: document.getElementById('spRequestedEmpty'),
    unplaced: document.getElementById('spUnplaced'),
    unplacedNote: document.getElementById('spUnplacedNote'),
    keep: document.getElementById('spKeep'),
    rebuild: document.getElementById('spRebuild'),
    confirm: document.getElementById('spConfirm'),
    confirmText: document.getElementById('spConfirmText'),
    confirmBtn: document.getElementById('spConfirmBtn'),
    confirmCancel: document.getElementById('spConfirmCancel'),
    generate: document.getElementById('spGenerate'),
    options: document.getElementById('spOptions'),
    optionsEmpty: document.getElementById('spOptionsEmpty'),
    status: document.getElementById('spStatus'),
  };

  /* Held in page memory only, never in storage: it authorises one destructive
     action, and a token that survives the tab survives the intent behind it. */
  let confirmation = null;
  let busy = false;

  function csrf() {
    const input = document.querySelector('[name=csrfmiddlewaretoken]');
    if (input) return input.value;
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  /* Never throws. `fetch` REJECTS on a dropped connection rather than resolving,
     so an unguarded await would propagate out and skip the line that clears
     `busy` — leaving the page permanently unable to do anything. */
  async function api(path, options) {
    try {
      const res = await fetch(path, options);
      let body = null;
      try { body = await res.json(); } catch (_) { body = null; }
      /* A 200 with no body is a failure, not an empty success: it means something
         between here and the view answered instead of the view. */
      return { ok: res.ok && body !== null, status: res.status, body: body || {} };
    } catch (_) {
      return { ok: false, status: 0, body: {} };
    }
  }

  function post(path, payload) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify(payload || {}),
    });
  }

  function say(text) { el.status.textContent = text || ''; }

  function setBusy(state) {
    busy = state;
    el.generate.disabled = state;
    el.generate.setAttribute('aria-busy', state ? 'true' : 'false');
  }

  function text(tag, value, cls) {
    const node = document.createElement(tag);
    node.textContent = value == null ? '' : String(value);
    if (cls) node.className = cls;
    return node;
  }

  /* ── rendering ─────────────────────────────────────────────── */

  function renderRequested(draft) {
    el.requested.replaceChildren();
    const rows = draft.requested || [];
    el.requestedEmpty.hidden = rows.length > 0;
    rows.forEach(function (row) {
      const li = document.createElement('li');
      li.className = 'sp-requested-item';
      li.appendChild(text('span', row.course_code, 'sp-code'));
      if (row.course_name) li.appendChild(text('span', row.course_name, 'sp-name'));
      /* The distinction the student actually needs: which of these did I fix in
         place, and which is the planner free to move? */
      li.appendChild(text(
        'span',
        row.fixed_section_id ? 'شعبة محدَّدة' : 'يختارها المخطط',
        row.fixed_section_id ? 'sp-tag sp-tag-fixed' : 'sp-tag sp-tag-auto'
      ));
      el.requested.appendChild(li);
    });
  }

  function renderUnplaced(unplaced) {
    el.unplaced.replaceChildren();
    el.unplacedNote.hidden = !unplaced.length;
    unplaced.forEach(function (row) {
      const li = document.createElement('li');
      li.appendChild(text('span', row.course_code + (row.course_name ? ' — ' + row.course_name : ''), 'sp-code'));
      if (row.reason) li.appendChild(text('span', row.reason, 'sp-reason'));
      el.unplaced.appendChild(li);
    });
  }

  function renderOption(option, index) {
    const card = document.createElement('article');
    card.className = 'sp-option' + (option.selected ? ' sp-option-selected' : '');

    const head = document.createElement('header');
    head.appendChild(text('h3', 'الخيار ' + (index + 1), 'h6'));
    head.appendChild(text('span', option.credit_hours + ' ساعة معتمدة', 'sp-muted'));
    card.appendChild(head);

    /* The builder fills the term from the student's own plan, so an alternative
       routinely holds courses they never named. Said out loud — a list that mixes
       "what I asked for" with "what was added" and marks neither is a list the
       student has to reverse-engineer. */
    const added = (option.courses || []).filter(function (c) { return !c.requested; });
    if (added.length) {
      card.appendChild(text(
        'p',
        'أُضيفت من خطتك: ' + added.map(function (c) { return c.course_code; }).join('، '),
        'sp-added'
      ));
    }

    /* Course, section, day, start, end. Nothing else: rooms, instructors and
       enrolment counts are the institution's business, not this student's week. */
    const table = document.createElement('table');
    table.className = 'sp-grid';
    const thead = document.createElement('thead');
    const hrow = document.createElement('tr');
    ['المقرر', 'الشعبة', 'اليوم', 'من', 'إلى'].forEach(function (label) {
      hrow.appendChild(text('th', label));
    });
    thead.appendChild(hrow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    (option.meetings || []).forEach(function (m) {
      const tr = document.createElement('tr');
      tr.appendChild(text('td', m.course_code + (m.course_name ? ' — ' + m.course_name : '')));
      tr.appendChild(text('td', m.section));
      tr.appendChild(text('td', m.day));
      tr.appendChild(text('td', m.start));
      tr.appendChild(text('td', m.end));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    card.appendChild(table);

    const choose = document.createElement('button');
    choose.type = 'button';
    choose.className = option.selected ? 'btn btn-success btn-sm' : 'btn btn-outline-primary btn-sm';
    choose.textContent = option.selected ? 'هذا خيارك المحفوظ' : 'احفظ هذا الجدول';
    choose.disabled = !!option.selected;
    choose.addEventListener('click', function () { select(option.key); });
    card.appendChild(choose);
    return card;
  }

  function render(data) {
    const draft = data.draft || {};
    renderRequested(draft);
    renderUnplaced(data.unplaced || []);

    /* Reflect the SERVER's toggle, not whatever the radio happened to be set to:
       a failed edit must not leave the page claiming a choice the draft rejected. */
    el.keep.checked = draft.keep_current_sections;
    el.rebuild.checked = !draft.keep_current_sections;

    const options = data.alternatives || [];
    el.options.replaceChildren();
    el.optionsEmpty.hidden = options.length > 0;
    options.forEach(function (option, i) { el.options.appendChild(renderOption(option, i)); });

    if (!draft.is_live) {
      say('انتهت صلاحية هذا المخطط. ابدأ من جديد من قائمة مقرراتك.');
      el.generate.disabled = true;
    }
  }

  /* ── actions ───────────────────────────────────────────────── */

  async function load() {
    const res = await api(base, { headers: { Accept: 'application/json' } });
    if (!res.ok) { say(res.body.error || 'تعذّر تحميل المخطط.'); return; }
    render(res.body);
  }

  async function setMode(keep) {
    /* Any edit kills a confirmation the server has already invalidated. Holding on
       to it here would only produce a 428 the student cannot explain. */
    confirmation = null;
    el.confirm.hidden = true;
    const res = await post(base + 'edit/', { keep_current_sections: keep });
    if (!res.ok) { say(res.body.error || 'تعذّر حفظ التغيير.'); await load(); return; }
    render(res.body);
    if (!keep) askConfirmation();
  }

  function askConfirmation() {
    el.confirmText.textContent =
      'سيتم تجاهل الشُعب المسجّلة حاليًا وإعادة بناء الجدول من جديد.';
    el.confirm.hidden = false;
  }

  async function confirmRebuild() {
    const res = await post(base + 'confirm-rebuild/', {});
    if (!res.ok) { say(res.body.error || 'تعذّر تأكيد إعادة البناء.'); return; }
    confirmation = res.body.confirmation;
    el.confirmText.textContent = res.body.warning || el.confirmText.textContent;
    el.confirm.hidden = true;
    say('تم التأكيد. اضغط «اعرض الجداول الممكنة».');
  }

  async function generate() {
    if (busy) return;
    setBusy(true);
    say('جارٍ إعداد الجداول…');
    const res = await post(base + 'generate/', confirmation ? { confirmation: confirmation } : {});
    setBusy(false);

    if (res.status === 428) {
      /* The server, not this file, decided the confirmation was missing, stale or
         spent. Ask again rather than guessing which. */
      confirmation = null;
      askConfirmation();
      say(res.body.error || 'يلزم تأكيد إعادة البناء أولًا.');
      return;
    }
    if (!res.ok) { say(res.body.error || 'تعذّر إعداد الجداول.'); return; }

    /* Single-use, and the server has now spent it. */
    confirmation = null;
    render(res.body);
    const count = (res.body.alternatives || []).length;
    say(count ? 'تم إعداد ' + count + ' جدول.' : 'لا يوجد جدول ممكن بهذه المقررات.');
  }

  async function select(key) {
    const res = await post(base + 'select/', { key: key });
    if (!res.ok) { say(res.body.error || 'تعذّر حفظ اختيارك.'); return; }
    render(res.body);
    /* Said explicitly, every time. "Saved" beside a timetable reads as "registered"
       unless the sentence says otherwise, and it is not. */
    say(res.body.message || 'تم حفظ هذا الجدول كخيارك المفضل. لم يتم تسجيلك في أي مقرر.');
  }

  el.keep.addEventListener('change', function () { if (el.keep.checked) setMode(true); });
  el.rebuild.addEventListener('change', function () { if (el.rebuild.checked) setMode(false); });
  el.confirmBtn.addEventListener('click', confirmRebuild);
  el.confirmCancel.addEventListener('click', function () {
    el.confirm.hidden = true;
    el.keep.checked = true;
    setMode(true);
  });
  el.generate.addEventListener('click', generate);

  load();
})();
