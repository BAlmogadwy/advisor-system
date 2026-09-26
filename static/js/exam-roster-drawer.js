/*
 * The course drawer on the Timetable page, and each card's "N students" link.
 *
 * - The link is the first item of a card's .et-review-badges. Its count is
 *   the SAVED run's section_enrollment for that exam - no request - and it is
 *   a <button>, which the grid never drags or pins. A card whose exam is not
 *   in the saved run has no link.
 * - The drawer (#examRosterDrawer, a native modal on the inline end) shows
 *   the saved run, never the draft, and says so when the draft has moved
 *   exams: downloads then wait, under the same rule as Export Excel. Its
 *   heading takes focus; closing returns focus to the card's link, found
 *   again by data-course if the grid re-rendered meanwhile.
 * - Students come from the audited POST endpoint, one request per exam
 *   opened; a flag's link opens the other exam here, and Back returns to the
 *   list already shown without asking again.
 * - Download: this section or all sections through the phase-1 export
 *   endpoint (window.examStudentExport.download), or More options in the
 *   phase-1 dialog, preset to the same scope.
 *
 * The page lends the saved run and its rules through window.examRosterHost
 * (static/js/page-exam-timetable.js); nothing here decides policy.
 */
(() => {
  'use strict';

  const R = window.ExamRoster;
  const host = window.examRosterHost;
  const drawer = document.getElementById('examRosterDrawer');
  if (!R || !host || !drawer) return;

  const $ = id => document.getElementById(id);
  const words = R.copy();
  const { el, bdi, ltr, count } = R;
  const UNASSIGNED = 'UNASSIGNED';
  const OVERFLOW = 'OVERFLOW';
  const GENDERS = ['M', 'F', 'U'];
  const LIST_ICON = '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/></svg>';

  let context = null;   // { runId, run, card: the opener's data-course }
  let current = null;   // { code, answer, section, flag }
  let stack = [];       // exams opened through flag links, for Back
  let requestToken = 0;
  let controller = null;
  let downloadToken = 0;
  let downloadController = null;
  let lastDownload = null;

  const rosterUrl = runId => drawer.dataset.rosterUrl.replace('/0/rosters/', `/${encodeURIComponent(runId)}/rosters/`);
  const pageUrl = (runId, code) => `${drawer.dataset.rostersPage}?${new URLSearchParams({ run: String(runId), view: 'course', course: code })}`;
  const announce = text => { $('examRosterDrawerLive').textContent = text; };

  const roster = R.createRoster($('examRosterDrawerBody'), {
    idPrefix: 'examRosterDrawerRoster',
    examHref: code => (context ? pageUrl(context.runId, code) : null),
    onOpenExam: code => openExam(code),
    onChange: ({ section, flag }) => {
      if (current) Object.assign(current, { section, flag });
      renderMenu();
    },
    announce,
  });

  // ── The saved run ───────────────────────────────────────────

  function savedEntry(code) {
    return (context?.run?.schedule || []).find(entry => entry.course_code === code) || null;
  }

  function savedCount(run, code) {
    const groups = run?.section_enrollment?.[code];
    return Array.isArray(groups) ? groups.reduce((sum, group) => sum + (Number(group?.student_count) || 0), 0) : null;
  }

  // ── Card links: the saved count, no request ─────────────────

  function paintLinks() {
    const grid = $('schedGrid');
    if (!grid) return;
    const run = host.runId() ? host.savedRun() : null;
    const saved = new Map((run?.schedule || []).map(entry => [entry.course_identity || entry.course_code, entry]));
    const draft = new Map(host.currentSchedule().map(entry => [entry.course_code, entry]));
    grid.querySelectorAll('.et-course[data-course]').forEach(card => {
      const badges = card.querySelector('.et-review-badges');
      if (!badges) return;
      let link = [...badges.children].find(child => child.classList.contains('et-roster-link')) || null;
      const entry = draft.get(card.dataset.course);
      const match = entry ? saved.get(entry.course_identity || entry.course_code) : null;
      const n = match ? savedCount(run, match.course_code) : null;
      if (n === null) {
        link?.remove();
        return;
      }
      if (!link) {
        link = el('button', { type: 'button', class: 'et-roster-link', 'aria-haspopup': 'dialog', 'aria-controls': 'examRosterDrawer' });
      }
      const one = n === 1 ? '-one' : '';
      link.dataset.rosterExam = match.course_code;
      link.innerHTML = LIST_ICON;
      link.append(el('span', {}, words.fragment(`card-link${one}`, { n: count(n) })));
      link.setAttribute('aria-label', words.text(`card-link-label${one}`, { code: match.course_code, n: R.NUMBER.format(n) }));
      if (badges.firstElementChild !== link) badges.prepend(link);
    });
  }

  // ── Header ──────────────────────────────────────────────────

  function examName(code) {
    const facts = current?.answer?.exams?.[code];
    const arabic = words.locale === 'ar' && facts?.name_ar;
    return arabic ? facts.name_ar : (facts?.name || savedEntry(code)?.course_name || '');
  }

  function renderHeader() {
    const { code, answer } = current;
    const entry = savedEntry(code);
    const name = examName(code);
    $('examRosterDrawerTitle').replaceChildren(bdi(code), ...(name ? [' · ', R.nameNode(name)] : []));

    const placed = entry && entry.day !== OVERFLOW;
    const rooms = new Set((entry?.rooms || []).map(room => room?.room_code).filter(room => room && room !== UNASSIGNED));
    const students = answer ? answer.counts.students : savedCount(context.run, code);
    $('examRosterDrawerMeta').replaceChildren(R.join([
      placed ? R.slot(entry.day, entry.period) : words.raw('not-scheduled'),
      words.fragment('students', { n: count(students ?? 0) }),
      words.fragment('rooms-count', { n: count(rooms.size) }),
    ]));

    const source = $('examRosterDrawerSource');
    source.replaceChildren(R.sourceLine({ id: context.runId, label: context.run.label, saved_at: context.run.created_at }));
    if (answer) {
      const changed = answer.sections.filter(section => section.membership !== 'matches' || section.program_mix !== 'matches').length;
      source.append(' · ', words.fragment('lists-checked', { time: ltr(R.clock(answer.checked_at)) }), ' · ', R.checkLine(null, changed));
    }

    // The saved run, never the draft: say so, and where the draft has it.
    const moves = host.unsavedMoves();
    const dirty = host.hasUnsavedChanges() || moves.length > 0;
    $('examRosterDrawerNotice').hidden = !dirty;
    if (dirty) {
      $('examRosterDrawerUnsaved').replaceChildren(moves.length === 1 ? words.fragment('unsaved-moves-one')
        : moves.length ? words.fragment('unsaved-moves', { n: count(moves.length) }) : words.fragment('unsaved-changes'));
      const identity = entry?.course_identity || code;
      const moved = moves.find(item => (item.course_identity || item.course_code) === identity);
      const draft = $('examRosterDrawerDraft');
      draft.hidden = !moved;
      draft.replaceChildren(!moved ? '' : moved.day === OVERFLOW ? words.fragment('draft-unplaced')
        : words.fragment('draft-at', { slot: R.slot(moved.day, moved.period) }));
    }

    const back = $('examRosterDrawerBack');
    back.hidden = !stack.length;
    if (stack.length) $('examRosterDrawerBackText').replaceChildren(words.fragment('back-to', { code: ltr(stack[stack.length - 1].code) }));
    $('examRosterDrawerOpenPage').href = pageUrl(context.runId, code);
  }

  // ── Loading one exam ────────────────────────────────────────

  async function load() {
    const mine = ++requestToken;
    controller?.abort();
    controller = new AbortController();
    const { code } = current;
    roster.loading({ students: savedCount(context.run, code) });
    let result = null;
    let error = null;
    try {
      result = await R.send(rosterUrl(context.runId), {
        body: { scope: { kind: 'course', exam: code } }, signal: controller.signal, csrf: host.csrf,
      });
    } catch (caught) {
      if (caught?.name === 'AbortError') return;
      error = caught;
    }
    if (mine !== requestToken || !drawer.open) return;
    if (error || !result.data.ok) {
      const found = R.problem(result, error);
      roster.failed({ content: found.content, retry: found.refused ? null : () => load() });
      renderMenu();
      return;
    }
    current.answer = result.data;
    showCurrent();
    const counts = result.data.counts;
    announce(words.text('announce-scope', {
      title: code, n: R.NUMBER.format(counts.students), clash: R.NUMBER.format(counts.clash), same_day: R.NUMBER.format(counts.same_day),
    }));
  }

  function showCurrent() {
    roster.show(current.answer, { scope: () => bdi(current.code), section: current.section, flag: current.flag });
    renderHeader();
    renderMenu();
  }

  function openExam(code) {
    if (!context || !code || code === current?.code) return;
    stack.push(current);
    current = { code, answer: null, section: 'all', flag: 'all' };
    renderHeader();
    load();
    $('examRosterDrawerTitle').focus({ preventScroll: true });
  }

  function back() {
    const previous = stack.pop();
    if (!previous) return;
    requestToken++;
    controller?.abort();
    current = previous;
    if (current.answer) showCurrent();
    else {
      renderHeader();
      load();
    }
    $('examRosterDrawerTitle').focus({ preventScroll: true });
  }

  // ── Open and close ──────────────────────────────────────────

  function open(code, from = null) {
    if (drawer.open) return;
    const runId = host.runId();
    const run = host.savedRun();
    if (!runId || !run || savedCount(run, code) === null) return;
    context = { runId, run, card: from?.closest?.('.et-course')?.dataset.course ?? null };
    stack = [];
    current = { code, answer: null, section: 'all', flag: 'all' };
    clearDownload();
    renderHeader();
    renderMenu();
    if (drawer.showModal) drawer.showModal();
    else drawer.setAttribute('open', '');
    $('examRosterDrawerTitle').focus({ preventScroll: true });
    load();
  }

  function close() {
    requestToken++;
    downloadToken++;
    controller?.abort();
    downloadController?.abort();
    closeMenu({ focus: false });
    roster.clear();
    if (drawer.close) drawer.close();
    else drawer.removeAttribute('open');
    const card = context?.card;
    context = null;
    current = null;
    stack = [];
    // The grid may have re-rendered while the drawer was open.
    const chip = [...document.querySelectorAll('#schedGrid .et-course')].find(item => item.dataset.course === card);
    (chip?.querySelector('.et-roster-link') || chip)?.focus({ preventScroll: true });
  }

  // ── Download ────────────────────────────────────────────────

  const menu = $('examRosterDrawerMenu');
  const downloadButton = $('examRosterDrawerDownload');
  const items = () => [...menu.querySelectorAll('[role^="menuitem"]')];

  function blockReason() {
    return context ? host.blockReason() : '';
  }

  // The scope on screen: the section tab chosen, else the whole exam, with
  // the groups that sit it (a file needs at least one).
  function scopeOf(kind) {
    const group = roster.selection().group;
    if (kind === 'section' || (kind === 'screen' && group)) {
      return group ? { scope: { kind: 'section', ...group }, groups: [group.gender] } : null;
    }
    const present = new Set((current?.answer?.sections || []).map(section => section.gender));
    const groups = GENDERS.filter(gender => present.has(gender));
    return { scope: { kind: 'course', exam: current.code }, groups: groups.length ? groups : GENDERS };
  }

  function renderMenu() {
    const reason = blockReason();
    const ready = Boolean(current?.answer);
    downloadButton.setAttribute('aria-disabled', String(Boolean(reason) || !ready));
    downloadButton.setAttribute('aria-busy', String(Boolean(downloadController)));
    $('examRosterDrawerReason').textContent = reason;
    if (reason) downloadButton.setAttribute('aria-describedby', 'examRosterDrawerReason');
    else downloadButton.removeAttribute('aria-describedby');
    const selection = roster.shown() ? roster.selection() : { group: null };
    const sectionItem = menu.querySelector('[data-download="section"]');
    const section = selection.group && (current?.answer?.sections || []).find(item => item.section_key === selection.group.section_key && item.gender === selection.group.gender);
    sectionItem.querySelector('.et-menu-label').replaceChildren(section ? words.fragment('menu-section', {
      section: section.section_status === 'mapped' ? ltr(R.sectionName(section)) : R.sectionName(section),
    }) : words.fragment('menu-this-section'));
    sectionItem.setAttribute('aria-disabled', String(!section));
    $('examRosterDrawerSectionHint').hidden = Boolean(section);
    if (section) sectionItem.removeAttribute('aria-describedby');
    else sectionItem.setAttribute('aria-describedby', 'examRosterDrawerSectionHint');
    const language = window.examStudentExport?.language() || 'ar';
    menu.querySelectorAll('[role="menuitemradio"]').forEach(item => item.setAttribute('aria-checked', String(item.dataset.language === language)));
  }

  function openMenu() {
    if (downloadButton.getAttribute('aria-disabled') === 'true' || !menu.hidden) return;
    renderMenu();
    menu.hidden = false;
    downloadButton.setAttribute('aria-expanded', 'true');
    const first = items().find(item => item.getAttribute('aria-disabled') !== 'true');
    first?.focus();
  }

  function closeMenu({ focus = true } = {}) {
    if (menu.hidden) return;
    menu.hidden = true;
    downloadButton.setAttribute('aria-expanded', 'false');
    if (focus) downloadButton.focus();
  }

  function clearDownload() {
    downloadToken++;
    downloadController?.abort();
    downloadController = null;
    lastDownload = null;
    $('examRosterDrawerStatus').replaceChildren();
    $('examRosterDrawerError').hidden = true;
  }

  async function download(kind) {
    if (!current?.answer || blockReason() || downloadController) return;
    const chosen = scopeOf(kind);
    if (!chosen) return;
    const options = {
      scope: chosen.scope, programs: [], groups: chosen.groups, one_file_per_group: false,
      rows: 'all', contents: 'full', language: window.examStudentExport.language(), dates: {},
    };
    lastDownload = kind;
    const mine = ++downloadToken;
    downloadController = new AbortController();
    $('examRosterDrawerError').hidden = true;
    $('examRosterDrawerStatus').replaceChildren(words.fragment('downloading'));
    renderMenu();
    try {
      const { name, reference } = await window.examStudentExport.download(context.runId, options, { signal: downloadController.signal });
      if (mine !== downloadToken) return;
      const done = words.fragment('downloaded', { file: ltr(name), reference: ltr(reference) });
      $('examRosterDrawerStatus').replaceChildren(done);
      announce($('examRosterDrawerStatus').textContent);
    } catch (error) {
      if (mine !== downloadToken || error?.name === 'AbortError') return;
      $('examRosterDrawerStatus').replaceChildren();
      $('examRosterDrawerErrorText').replaceChildren(error.content ? error.content() : words.fragment('error-load'));
      $('examRosterDrawerError').hidden = false;
    } finally {
      if (mine === downloadToken) {
        downloadController = null;
        renderMenu();
      }
    }
  }

  function choose(item) {
    if (!item || item.getAttribute('aria-disabled') === 'true') return;
    if (item.dataset.language) {
      window.examStudentExport.rememberLanguage(item.dataset.language);
      renderMenu();
      return;
    }
    const kind = item.dataset.download;
    closeMenu();
    if (kind === 'more') {
      window.examStudentExport.open(downloadButton, { scope: scopeOf('screen').scope });
      return;
    }
    download(kind);
  }

  // ── Events ──────────────────────────────────────────────────

  $('schedGrid')?.addEventListener('click', event => {
    const link = event.target.closest('.et-roster-link');
    if (!link || $('schedGrid').inert) return;
    open(link.dataset.rosterExam, link);
  });
  $('examRosterDrawerClose').addEventListener('click', close);
  $('examRosterDrawerBack').addEventListener('click', back);
  drawer.addEventListener('cancel', event => {
    event.preventDefault();
    if (!menu.hidden) closeMenu();
    else close();
  });
  // A click on the backdrop (outside the panel) closes, as Esc does.
  drawer.addEventListener('click', event => {
    if (event.target === drawer) close();
  });
  downloadButton.addEventListener('click', () => (menu.hidden ? openMenu() : closeMenu()));
  downloadButton.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      openMenu();
    }
  });
  menu.addEventListener('click', event => choose(event.target.closest('[role^="menuitem"]')));
  menu.addEventListener('keydown', event => {
    const list = items();
    const at = list.indexOf(document.activeElement);
    let next = null;
    if (event.key === 'ArrowDown') next = list[(at + 1) % list.length];
    else if (event.key === 'ArrowUp') next = list[(at - 1 + list.length) % list.length];
    else if (event.key === 'Home') next = list[0];
    else if (event.key === 'End') next = list[list.length - 1];
    else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeMenu();
      return;
    } else if (event.key === 'Tab') {
      closeMenu({ focus: false });
      return;
    }
    if (next) {
      event.preventDefault();
      next.focus();
    }
  });
  document.addEventListener('focusin', event => {
    if (!menu.hidden && !menu.contains(event.target) && event.target !== downloadButton) closeMenu({ focus: false });
  });
  $('examRosterDrawerRetry').addEventListener('click', () => {
    downloadButton.focus({ preventScroll: true });
    if (lastDownload) download(lastDownload);
  });

  window.ExamRosterDrawer = Object.freeze({ paintLinks, open, close, isOpen: () => drawer.open });
})();
