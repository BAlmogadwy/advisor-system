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
  let needsConfirmation = false;

  /* Used only until the server has spoken. Kept identical to the server's wording
     on purpose, and never preferred over it. */
  const FALLBACK_WARNING =
    'سيقترح النظام جدولًا جديدًا قد يتضمّن شُعبًا غير التي سجّلت فيها. لن يتغيّر تسجيلك الفعلي.';

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

  /* Arabic counts three ways, not two: one, a pair, then 3-10, then 11+ reverts to
     the singular. "6 ساعة معتمدة" and "تم إعداد 6 جدول" are both wrong. */
  function plural(n, one, two, few) {
    const count = Number(n) || 0;
    if (count === 1) return one;
    if (count === 2) return two;
    if (count >= 3 && count <= 10) return count + ' ' + few;
    return count + ' ' + one;
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
    const heading = text('h3', 'الخيار ' + (index + 1), 'h6');
    heading.id = 'spOption' + index;
    card.setAttribute('aria-labelledby', heading.id);
    head.appendChild(heading);
    head.appendChild(text('span', plural(option.credit_hours, 'ساعة معتمدة', 'ساعتان معتمدتان', 'ساعات معتمدة'), 'sp-muted'));
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
    /* The scroll container is a WRAPPER, never the table. `display:block` on a
       <table> drops its implicit table role, so the thead/th/td structure below is
       announced as a flat run of text — no row or column navigation, no header
       association. `tabindex` because a scrollable region a keyboard cannot reach
       is content a keyboard cannot read. */
    const wrap = document.createElement('div');
    wrap.className = 'sp-grid-wrap';
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', 'جدول الخيار ' + (index + 1));

    const table = document.createElement('table');
    table.className = 'sp-grid';
    const caption = text('caption', 'الخيار ' + (index + 1));
    caption.className = 'sp-visually-hidden';
    table.appendChild(caption);
    const thead = document.createElement('thead');
    const hrow = document.createElement('tr');
    ['المقرر', 'الشعبة', 'اليوم', 'من', 'إلى'].forEach(function (label) {
      const th = text('th', label);
      th.setAttribute('scope', 'col');
      hrow.appendChild(th);
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
    wrap.appendChild(table);
    card.appendChild(wrap);

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
    needsConfirmation = !!draft.needs_confirmation;

    const options = data.alternatives || [];
    el.options.replaceChildren();
    el.optionsEmpty.hidden = options.length > 0;
    /* Three different nothings, and telling the student to press the button they
       just pressed is only right for one of them. */
    if (!options.length) {
      el.optionsEmpty.textContent = !draft.has_current_generation
        ? 'اضغط «اعرض الجداول الممكنة» لعرض الخيارات.'
        : 'لا يوجد جدول ممكن بهذه المقررات. جرّب تعديل اختيارك.';
    }
    options.forEach(function (option, i) { el.options.appendChild(renderOption(option, i)); });

    if (!draft.is_live) {
      say('انتهت صلاحية هذا المخطط. ابدأ من جديد من قائمة مقرراتك.');
      el.generate.disabled = true;
    } else if (draft.is_stale) {
      /* The timetables are still ON SCREEN — they were valid when built and the
         student may still want to read them. What changes is that they are labelled
         instead of quietly presented as current. */
      say('تغيّرت شُعبك المسجّلة منذ إعداد هذه الجداول. اضغط «اعرض الجداول الممكنة» لتحديثها.');
    }
  }

  /* ── actions ───────────────────────────────────────────────── */

  async function load() {
    /* Said while it is in flight. Without it the first paint is a heading over an
       empty list with its own empty-state hidden — indistinguishable from a draft
       with nothing in it. */
    say('جارٍ تحميل مخططك…');
    const res = await api(base, { headers: { Accept: 'application/json' } });
    if (!res.ok) {
      say((res.body.error || 'تعذّر تحميل المخطط.') + ' أعد تحميل الصفحة للمحاولة مرة أخرى.');
      return;
    }
    say('');
    render(res.body);
  }

  /* Settled before it is sent. The two radios are one arrow-key group, so holding
     ← or → auto-repeats through them — and every change used to POST `edit/`, which
     spends the CONVERSATION budget the ADVISER CHAT also draws on. Thirty in ten
     minutes, gone in two seconds of arrow-key, and the student's next question to
     the adviser answers «لقد أرسلت طلبات كثيرة». */
  let modeTimer = null;
  let modeInFlight = false;

  function requestMode(keep) {
    confirmation = null;
    el.confirm.hidden = true;
    if (modeTimer) clearTimeout(modeTimer);
    modeTimer = setTimeout(function () { modeTimer = null; setMode(keep); }, 400);
  }

  async function setMode(keep) {
    /* Any edit kills a confirmation the server has already invalidated. Holding on
       to it here would only produce a 428 the student cannot explain. */
    if (modeInFlight) return;
    modeInFlight = true;
    confirmation = null;
    el.confirm.hidden = true;
    const res = await post(base + 'edit/', { keep_current_sections: keep });
    modeInFlight = false;
    if (!res.ok) { say(res.body.error || 'تعذّر حفظ التغيير.'); await load(); return; }
    render(res.body);
    if (!keep) askConfirmation();
  }

  function askConfirmation(serverWarning) {
    /* The SERVER's sentence when there is one. It was being written into the box and
       hidden in the same tick, so the wording the student agreed to was this file's
       copy — which means changing the registrar's wording server-side would have
       changed nothing on screen. */
    el.confirmText.textContent = serverWarning || FALLBACK_WARNING;
    el.confirm.hidden = false;
    /* Announced and focused. Choosing the destructive option used to reveal a box
       silently: a screen-reader user heard the radio change and nothing else, and
       found out a confirmation was needed only by pressing the button and being
       refused. */
    say(el.confirmText.textContent);
    el.confirmBtn.focus();
  }

  async function confirmRebuild() {
    const res = await post(base + 'confirm-rebuild/', {});
    if (!res.ok) { say(res.body.error || 'تعذّر تأكيد إعادة البناء.'); return; }
    confirmation = res.body.confirmation;
    el.confirm.hidden = true;
    el.generate.focus();
    say('تم التأكيد. اضغط «اعرض الجداول الممكنة».');
  }

  async function generate() {
    if (busy) return;
    /* Ask BEFORE spending a generation. Posting first to discover the 428 worked,
       but it billed a unit of the expensive budget for a request that never
       reached the solver — two of the six a student gets in ten minutes, for one
       rebuild. The server still refuses without a real token; this only avoids
       walking into the refusal on purpose. */
    /* The SERVER already said whether a confirmation is needed. Re-deriving it
       from the radio would be a second implementation of a rule that arrived in
       the response. */
    if (needsConfirmation && !confirmation) { askConfirmation(); return; }

    setBusy(true);
    say('جارٍ إعداد الجداول…');
    const res = await post(base + 'generate/', confirmation ? { confirmation: confirmation } : {});

    if (res.status === 429) {
      /* The server already worked out how long. Re-enabling the button and letting
         the student hammer it would only spend the wait again. */
      const wait = Number(res.body.retry_after) || 60;
      say((res.body.error || 'لقد أرسلت طلبات كثيرة.') + ' حاول بعد ' + wait + ' ثانية.');
      setTimeout(function () { setBusy(false); say(''); }, wait * 1000);
      return;
    }
    setBusy(false);

    if (res.status === 428) {
      /* The server, not this file, decided the confirmation was missing, stale or
         spent. Ask again rather than guessing which. */
      confirmation = null;
      askConfirmation(res.body.error);
      return;
    }
    if (!res.ok) { say(res.body.error || 'تعذّر إعداد الجداول.'); return; }

    /* Single-use, and the server has now spent it. */
    confirmation = null;
    render(res.body);
    const count = (res.body.alternatives || []).length;
    say(count
      ? 'تم إعداد ' + plural(count, 'جدول واحد', 'جدولين', 'جداول') + '.'
      : 'لا يوجد جدول ممكن بهذه المقررات. راجع أسباب التعذّر أعلاه.');
  }

  async function select(key) {
    const res = await post(base + 'select/', { key: key });
    if (!res.ok) { say(res.body.error || 'تعذّر حفظ اختيارك.'); return; }
    render(res.body);
    /* Said explicitly, every time. "Saved" beside a timetable reads as "registered"
       unless the sentence says otherwise, and it is not. */
    say(res.body.message || 'تم حفظ هذا الجدول كخيارك المفضل. لم يتم تسجيلك في أي مقرر.');
  }

  el.keep.addEventListener('change', function () { if (el.keep.checked) requestMode(true); });
  el.rebuild.addEventListener('change', function () { if (el.rebuild.checked) requestMode(false); });
  el.confirmBtn.addEventListener('click', confirmRebuild);
  el.confirmCancel.addEventListener('click', function () {
    el.confirm.hidden = true;
    el.keep.checked = true;
    el.keep.focus();
    requestMode(true);
  });
  el.generate.addEventListener('click', generate);

  load();
})();
