/*
 * The Student lists page: who sits each exam, and where, in one saved run.
 *
 * - The navigator (GET, counts and timetable facts only) lists rooms by day,
 *   period and group - rooms the timetable could not room and online rooms
 *   last - or exams by time, program and flags.
 * - Choosing a room or an exam loads its roster (POST, audited, fail-closed)
 *   into the pane through the shared roster (static/js/exam-roster.js), and
 *   is a history entry: Back returns to the previous room. The address holds
 *   the run, the view and a period, room or exam - never a student, a name
 *   or a search.
 * - Find matches exams and rooms here and students on the server (POST, a
 *   pause after typing, one audited request per settled search). A student
 *   opens as a lookup of every exam they sit, with no history entry; Back
 *   and Esc return.
 * - Export to Excel opens the phase-1 dialog (static/js/exam-student-export.js)
 *   preset to the scope on screen, through the host this page lends it.
 * - Below 800px the page is master-detail: the navigator, then the pane.
 *
 * Every sentence comes from #examRosterCopy in the page language.
 */
(() => {
  'use strict';

  const R = window.ExamRoster;
  const main = document.querySelector('main[data-exam-rosters]');
  if (!R || !main || main.dataset.rosterState !== 'ready') return;

  const $ = id => document.getElementById(id);
  const words = R.copy();
  const { el, bdi, ltr, count } = R;
  const RUN_ID = Number(main.dataset.runId);
  const URLS = { index: main.dataset.indexUrl, roster: main.dataset.rosterUrl, lookup: main.dataset.lookupUrl, timetable: main.dataset.timetableUrl };
  const TIMING = { findDelay: 400, ...(window.__examRostersTiming || {}) };
  const STORE_KEY = 'exam-rosters';
  const FIND_LIMIT = 8;
  const NARROW = '(max-width: 799px)';
  const WEEK_ORDER = [6, 0, 1, 2, 3, 4, 5]; // Sunday first (Python weekday numbers)
  // The navigator's section lists, each item opening its section's list:
  // parts the timetable could not room, and students with no seat.
  const SECTION_LISTS = ['not_assigned', 'no_seat'];
  const collator = new Intl.Collator(words.locale === 'ar' ? 'ar' : 'en', { numeric: true, sensitivity: 'base' });

  const csrf = () => {
    const cookie = document.cookie.split('; ').find(item => item.startsWith('csrftoken='));
    if (cookie) return decodeURIComponent(cookie.slice('csrftoken='.length));
    return typeof djCsrfToken === 'string' ? djCsrfToken : '';
  };
  const announce = text => { $('examRostersLive').textContent = text; };
  const narrow = () => Boolean(window.matchMedia?.(NARROW)?.matches);

  // ── State ───────────────────────────────────────────────────

  let nav = null;          // the navigator answer
  let navRefused = null;   // a gate refused this run
  let shown = null;        // the build the header names: { check, checked_at }
  let syncing = false;     // the navigator is being brought up to a newer build
  const state = {
    view: 'room', day: null, slot: null, group: 'all', roomReview: false, room: null,
    course: null, program: '', hasClash: false, courseReview: false, sort: 'time',
    section: null,         // a section opened from the navigator: { key, from }
  };
  let current = null;      // the pane's scope: { scope, answer, from, stale }
  let lookup = null;       // an open student lookup: { studentId, answer, from }
  let scopeToken = 0;
  let scopeController = null;
  let lookupToken = 0;
  let lookupController = null;

  // Per-viewer conveniences: the last day, period, group and view.
  function remembered() {
    try {
      const value = JSON.parse(window.localStorage.getItem(STORE_KEY) || '{}');
      return value && typeof value === 'object' ? value : {};
    } catch (_) {
      return {};
    }
  }

  function remember(extra = {}) {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify({
        ...remembered(), view: state.view, day: state.day, slot: state.slot, group: state.group, ...extra,
      }));
    } catch (_) {
      // Blocked storage: the defaults serve next time.
    }
  }

  // ── The address: run, view, period, room or exam. Nothing else. ─
  // A section opened from the navigator is a history entry too; its key (a
  // timetable fact, never a student) rides in the entry's state, not the URL.

  function readAddress() {
    const params = new URLSearchParams(window.location.search);
    const slot = params.get('slot');
    const section = window.history.state?.section;
    return {
      view: params.get('view') === 'course' ? 'course' : params.get('view') === 'room' ? 'room' : null,
      slot: slot !== null && /^[0-9]{1,4}$/.test(slot) ? Number(slot) : null,
      room: params.get('room'),
      course: params.get('course'),
      section: section && typeof section.key === 'string' && SECTION_LISTS.includes(section.from) ? { key: section.key, from: section.from } : null,
    };
  }

  function writeAddress({ push = false } = {}) {
    const params = new URLSearchParams({ run: String(RUN_ID), view: state.view });
    if (state.view === 'room') {
      if (state.slot !== null) params.set('slot', String(state.slot));
      if (state.room) params.set('room', state.room);
    } else if (state.course) {
      params.set('course', state.course);
    }
    const url = `${window.location.pathname}?${params}`;
    const entry = {
      rosters: true, pushed: push || Boolean(window.history.state?.pushed),
      section: state.view === 'room' && state.section ? { ...state.section } : null,
    };
    try {
      if (push) window.history.pushState(entry, '', url);
      else window.history.replaceState(entry, '', url);
    } catch (_) {
      // A sandboxed frame: the page still works without its address.
    }
  }

  // ── Facts ───────────────────────────────────────────────────

  const slotOf = index => nav?.slots.find(item => item.slot_index === index) || null;
  const examOf = code => nav?.exams.find(item => item.code === code) || null;
  const roomOf = (slot, code) => nav?.rooms.find(item => item.slot_index === slot && item.room_code === code) || null;
  const sectionItems = from => (SECTION_LISTS.includes(from) && nav?.[from]) || [];
  const sectionItemOf = pick => (pick ? sectionItems(pick.from).find(item => R.groupKey(item) === pick.key) || null : null);

  function dayParts(code) {
    const match = /^W(\d+)-(.+)$/.exec(String(code));
    return match ? { week: Number(match[1]), label: match[2] } : { week: 1, label: String(code) };
  }

  function examTitle(exam) {
    const name = words.locale === 'ar' && exam?.name_ar ? exam.name_ar : exam?.name;
    const out = document.createDocumentFragment();
    out.append(bdi(exam?.code || ''));
    if (name) out.append(' · ', R.nameNode(name));
    return out;
  }

  function sectionLabel(item) {
    const name = R.sectionName(item);
    return item.section_status === 'mapped' ? bdi(name) : document.createTextNode(name);
  }

  // ── Provenance ──────────────────────────────────────────────
  // The header names ONE build: its check and its time together, from the
  // newest answer seen (the navigator, a list, a lookup or a search). An
  // answer from a newer build than the navigator's brings the navigator up to
  // it - a GET the same cached build answers - so its counts agree too.

  const buildTime = iso => {
    const time = Date.parse(iso || '');
    return Number.isNaN(time) ? 0 : time;
  };

  function paintProvenance() {
    if (!shown) return;
    $('examRostersCheck').replaceChildren(' · ', R.checkLine(shown.check));
    $('examRostersChecked').replaceChildren(words.fragment('lists-checked', { time: ltr(R.clock(shown.checked_at)) }));
  }

  function renderProvenance(answer) {
    if (nav?.run) {
      const term = words.fragment('term', { year: ltr(nav.run.academic_year), term: ltr(nav.run.term) });
      $('examRostersLists').replaceChildren(words.fragment('lists-source', { term }));
    }
    if (answer?.checked_at && answer.check && (!shown || buildTime(answer.checked_at) >= buildTime(shown.checked_at))) {
      shown = { check: answer.check, checked_at: answer.checked_at };
    }
    paintProvenance();
    if (answer && nav && answer !== nav && answer.checked_at && answer.checked_at !== nav.checked_at
      && buildTime(answer.checked_at) >= buildTime(nav.checked_at)) syncNav();
  }

  async function syncNav() {
    if (syncing) return;
    syncing = true;
    let result = null;
    try {
      result = await R.send(URLS.index);
    } catch (_) {
      result = null;
    }
    syncing = false;
    if (!result?.data?.ok || !nav || buildTime(result.data.checked_at) < buildTime(nav.checked_at)) return;
    nav = result.data;
    renderProvenance(nav);
    renderExportState();
    renderNavKeepingFocus();
    if (current && !lookup) renderPaneHead();
  }

  function renderExportState() {
    const reason = navRefused ? navRefused.content().textContent : !nav ? words.raw('loading') : '';
    $('examRostersExport').setAttribute('aria-disabled', String(Boolean(reason)));
    $('examRostersExportReason').textContent = navRefused ? reason : '';
  }

  // ── Navigator: loading ──────────────────────────────────────

  function navSkeleton() {
    $('examRostersNavState').replaceChildren(el('div', { class: 'et-roster-skeleton', 'aria-busy': 'true' },
      ...Array.from({ length: 6 }, () => el('span', { class: 'et-skeleton-bar is-item' }))));
  }

  async function loadNav({ refresh = false } = {}) {
    if (!nav) navSkeleton();
    $('examRostersRefresh').setAttribute('aria-busy', 'true');
    if (refresh) $('examRostersChecked').replaceChildren(words.fragment('refreshing'));
    let result = null;
    let error = null;
    try {
      result = await R.send(refresh ? `${URLS.index}?refresh=1` : URLS.index);
    } catch (caught) {
      error = caught;
    }
    $('examRostersRefresh').removeAttribute('aria-busy');
    if (error || !result.data.ok) {
      const found = R.problem(result, error);
      if (found.refused) {
        navRefused = found;
        $('examRostersNavState').replaceChildren(el('div', { class: 'alert alert-warning et-refused', role: 'alert' }, found.content()));
        $('examRostersPaneEmpty').replaceChildren(found.content());
        $('examRostersChecked').replaceChildren('—');
      } else {
        const retry = el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary' }, words.raw('try-again'));
        retry.addEventListener('click', () => loadNav({ refresh }));
        $('examRostersNavState').replaceChildren(el('div', { class: 'alert alert-danger et-roster-error', role: 'alert' }, el('span', {}, found.content()), retry));
        // The build the header named is still the one on screen.
        paintProvenance();
      }
      renderExportState();
      return false;
    }
    navRefused = null;
    const first = !nav;
    nav = result.data;
    $('examRostersNavState').replaceChildren();
    renderProvenance(nav);
    renderExportState();
    if (first) applyAddress({ initial: true });
    else renderNavKeepingFocus();
    return true;
  }

  // ── Navigator: rendering ────────────────────────────────────

  // Which navigator item a key names: a room of a period, an exam, or a
  // section of one of the section lists.
  function navItemKey(item) {
    if (!item) return null;
    return { room: item.dataset.room, slot: item.dataset.slot, course: item.dataset.course, section: item.dataset.sectionGroup, from: item.dataset.from };
  }

  function findNavItem(key) {
    if (!key) return null;
    return [...$('examRostersNav').querySelectorAll('.et-nav-item')].find(item => (key.room ? item.dataset.room === key.room && item.dataset.slot === String(key.slot)
      : key.course ? item.dataset.course === key.course
        : Boolean(key.section) && item.dataset.sectionGroup === key.section && item.dataset.from === key.from)) || null;
  }

  // Focus a navigator item (and scroll it into view) as its list's one tab stop.
  function focusNavItem(item) {
    if (!item) return false;
    const list = item.closest('#examRostersRooms, #examRostersCourses');
    list?.querySelectorAll('.et-nav-item').forEach(node => { node.tabIndex = node === item ? 0 : -1; });
    item.focus({ preventScroll: true });
    item.scrollIntoView?.({ block: 'nearest' });
    return document.activeElement === item;
  }

  // Draw the navigator again without dropping a keyboard user's place: the
  // item that had focus is a new element afterwards.
  function renderNavKeepingFocus() {
    const active = document.activeElement?.closest?.('.et-nav-item');
    const key = active && $('examRostersNav').contains(active) ? navItemKey(active) : null;
    renderNav();
    if (key) focusNavItem(findNavItem(key));
  }

  function renderNav() {
    if (!nav) return;
    $('examRostersViews').querySelectorAll('[role="radio"]').forEach(button => {
      const on = button.dataset.view === state.view;
      button.setAttribute('aria-checked', String(on));
      button.tabIndex = on ? 0 : -1;
    });
    $('examRostersByRoom').hidden = state.view !== 'room';
    $('examRostersByCourse').hidden = state.view !== 'course';
    $('examRostersScreenBackText').textContent = words.raw(state.view === 'room' ? 'back-rooms' : 'back-courses');
    if (state.view === 'room') renderRoomNav();
    else renderCourseNav();
  }

  function renderDays() {
    const grid = $('examRostersDays');
    const columns = [];
    nav.days.forEach(day => {
      const key = Number.isInteger(day.weekday) ? day.weekday : `x-${dayParts(day.day).label}`;
      if (!columns.includes(key)) columns.push(key);
    });
    columns.sort((a, b) => {
      const rank = key => (typeof key === 'number' ? WEEK_ORDER.indexOf(key) : 7);
      return rank(a) - rank(b);
    });
    const weeks = [...new Set(nav.days.map(day => dayParts(day.day).week))].sort((a, b) => a - b);
    grid.style.setProperty('--et-day-columns', String(columns.length));
    // One week needs no week column: its days take the whole width.
    grid.classList.toggle('is-one-week', weeks.length <= 1);
    const cells = [];
    weeks.forEach(week => {
      if (weeks.length > 1) cells.push(el('span', { class: 'et-week-label', 'aria-hidden': 'true' }, `W${week}`));
      columns.forEach(column => {
        const day = nav.days.find(item => dayParts(item.day).week === week
          && (Number.isInteger(item.weekday) ? item.weekday : `x-${dayParts(item.day).label}`) === column);
        if (!day) {
          cells.push(el('span', { class: 'et-day-gap', 'aria-hidden': 'true' }));
          return;
        }
        const selected = day.day === state.day;
        cells.push(el('button', {
          type: 'button', role: 'tab', class: 'et-day-tab', id: `examRostersDay${day.day_no}`, 'data-day': day.day,
          'aria-selected': String(selected), 'aria-controls': 'examRostersDayPanel', tabindex: selected ? '0' : '-1',
          'aria-label': words.text('day-tab', { day: day.day, n: R.NUMBER.format(day.students) }),
        }, bdi(dayParts(day.day).label), el('small', {}, R.NUMBER.format(day.students))));
      });
    });
    grid.replaceChildren(...cells);
    const selectedTab = grid.querySelector('[aria-selected="true"]');
    if (selectedTab) $('examRostersDayPanel').setAttribute('aria-labelledby', selectedTab.id);
  }

  function slotsOfDay(day) {
    return nav.slots.filter(item => item.day === day).sort((a, b) => a.slot_index - b.slot_index);
  }

  function renderPeriods() {
    $('examRostersPeriods').replaceChildren(...slotsOfDay(state.day).map(item => {
      const on = item.slot_index === state.slot;
      const empty = !item.exams.length;
      return el('button', {
        type: 'button', role: 'radio', class: `et-nav-chip${empty ? ' is-empty' : ''}`, 'data-slot': String(item.slot_index),
        'aria-checked': String(on), tabindex: on ? '0' : '-1',
        // Distinct students, as a day tab and the file's Period summary count
        // them: a student with a clash sits twice in a period, but is one student.
        'aria-label': empty ? words.text('period-empty', { period: item.period }) : words.text('period-chip', { period: item.period, n: R.NUMBER.format(item.students) }),
      }, bdi(item.period), empty ? null : el('small', { 'aria-hidden': 'true' }, R.NUMBER.format(item.students)));
    }));
  }

  function renderGroups() {
    const present = new Set(nav.rooms.map(room => room.gender));
    const options = ['all', 'M', 'F'].filter(group => group === 'all' || present.has(group));
    if (!options.includes(state.group)) state.group = 'all';
    $('examRostersGroups').replaceChildren(...options.map(group => {
      const on = group === state.group;
      return el('button', {
        type: 'button', role: 'radio', class: 'et-nav-chip', 'data-group': group, 'aria-checked': String(on), tabindex: on ? '0' : '-1',
      }, words.raw(group === 'all' ? 'group-all' : `group-${group.toLowerCase()}`));
    }));
  }

  function roomNeedsReview(room) {
    return room.review.length > 0;
  }

  function roomItem(room) {
    const over = room.capacity && room.seated_now > room.capacity;
    const full = room.capacity && !over && room.seated_now >= 0.95 * room.capacity;
    const figure = el('span', { class: `et-nav-figure${over ? ' is-over' : full ? ' is-full' : ''}` },
      bdi(`${R.NUMBER.format(room.seated_now)}/${R.NUMBER.format(room.capacity || 0)}`));
    if (over) figure.append(' ', words.raw('over-capacity'));
    // What the room's own list holds: a student without a seat is in no
    // room's list, so a room never counts one (the period's No seat does).
    const details = room.sections.map(section => {
      const bits = [bdi(section.exam), ' ', sectionLabel(section)];
      if (section.parts > 1) bits.push(' · ', words.fragment('item-part', { part: ltr(section.part), parts: ltr(section.parts) }));
      if (section.membership !== 'matches') bits.push(' · ', el('span', { class: 'is-warning' }, words.raw('item-changed')));
      const out = document.createDocumentFragment();
      out.append(...bits);
      return out;
    });
    if (room.flags.clash) details.push(el('span', { class: 'is-clash' }, words.fragment('item-clash', { n: count(room.flags.clash) })));
    if (room.flags.same_day) details.push(el('span', { class: 'is-warning' }, words.fragment('item-same_day', { n: count(room.flags.same_day) })));
    const chosen = state.room === room.room_code && !lookup;
    return el('li', {}, el('button', {
      type: 'button', class: 'et-nav-item', 'data-room': room.room_code, 'data-slot': String(room.slot_index),
      'aria-current': chosen ? 'true' : null, tabindex: '-1',
    }, el('span', { class: 'et-nav-code' }, bdi(room.room_code), room.online ? [' ', el('span', { class: 'et-online-badge' }, words.raw('online'))] : null),
    figure, el('span', { class: 'et-nav-detail' }, R.join(details))));
  }

  function sectionItem(item, from) {
    const key = R.groupKey(item);
    const chosen = state.section?.key === key && state.section.from === from && !lookup;
    const figure = from === 'no_seat' ? item.no_seat : item.now;
    const detail = from === 'no_seat'
      ? R.join([
        el('span', { class: 'is-clash' }, words.fragment('item-no_seat', { n: count(item.no_seat) })),
        item.membership === 'new' ? el('span', { class: 'is-warning' }, words.raw('marker-new'))
          : item.membership !== 'matches' ? el('span', { class: 'is-warning' }, words.raw('item-changed')) : null,
      ])
      : words.raw('item-not_assigned');
    return el('li', {}, el('button', {
      type: 'button', class: 'et-nav-item', 'data-section-group': key, 'data-from': from, 'aria-current': chosen ? 'true' : null, tabindex: '-1',
    }, el('span', { class: 'et-nav-code' }, bdi(item.exam), ' ', sectionLabel(item)),
    el('span', { class: 'et-nav-figure' }, bdi(R.NUMBER.format(figure))),
    el('span', { class: 'et-nav-detail' }, detail)));
  }

  function sectionScopeKey() {
    const scope = current?.scope;
    return scope?.kind === 'section' ? `${scope.exam}|${scope.section_key}|${scope.gender}` : null;
  }

  // A group heading names what it counts - sections, students or rooms - so
  // "Not assigned" and "No seat" beside each other never read as one unit.
  const countedHeading = (key, n) => words.fragment(n === 1 ? `${key}-one` : key, { n: count(n) });

  // A heading's words are one inline run: a flex heading would drop the
  // spaces between its codes, counts and separators.
  function navGroup(heading, items, { danger = false } = {}) {
    const nodes = [];
    if (heading) nodes.push(el('div', { class: `et-nav-heading${danger ? ' is-danger' : ''}`, role: 'heading', 'aria-level': '4' }, el('span', {}, heading)));
    nodes.push(el('ul', { class: 'et-nav-list' }, items));
    return nodes;
  }

  function renderRoomNav() {
    renderDays();
    renderPeriods();
    renderGroups();
    const slot = slotOf(state.slot);
    const inGroup = item => state.group === 'all' || item.gender === state.group;
    const rooms = nav.rooms.filter(room => room.slot_index === state.slot && inGroup(room));
    const unassigned = nav.not_assigned.filter(item => item.slot_index === state.slot && inGroup(item));
    const noSeat = (nav.no_seat || []).filter(item => item.slot_index === state.slot && inGroup(item));
    const review = rooms.filter(roomNeedsReview).length + unassigned.length + noSeat.length;
    const toggle = $('examRostersRoomReview');
    toggle.replaceChildren(el('span', {}, words.fragment('needs-review', { n: count(review) })));
    toggle.setAttribute('aria-pressed', String(state.roomReview));
    const shown = state.roomReview ? rooms.filter(roomNeedsReview) : rooms;
    const regular = shown.filter(room => !room.online);
    const online = shown.filter(room => room.online);
    const list = $('examRostersRooms');
    const nodes = [];
    if (!slot || !slot.exams.length) {
      nodes.push(el('p', { class: 'et-nav-empty' }, words.fragment('empty-period', { slot: slot ? R.slot(slot.day, slot.period) : '' })));
    } else if (!regular.length && !online.length && !unassigned.length && !noSeat.length) {
      nodes.push(el('p', { class: 'et-nav-empty' }, words.fragment('empty-rooms')));
    } else {
      if (regular.length) nodes.push(...navGroup(null, regular.map(roomItem)));
      if (unassigned.length) nodes.push(...navGroup(countedHeading('not-assigned-group', unassigned.length), unassigned.map(item => sectionItem(item, 'not_assigned')), { danger: true }));
      // Students with no seat are in no room's list: here, by section, each
      // opening its section's list at its No seat rows.
      if (noSeat.length) {
        const students = noSeat.reduce((sum, item) => sum + item.no_seat, 0);
        nodes.push(...navGroup(countedHeading('no-seat-group', students), noSeat.map(item => sectionItem(item, 'no_seat')), { danger: true }));
      }
      if (online.length) nodes.push(...navGroup(countedHeading('online-group', online.length), online.map(roomItem)));
    }
    list.replaceChildren(...nodes);
    rovingItems(list);
  }

  function examFlagWords(exam) {
    const bits = [];
    if (exam.flags.clash) bits.push(el('span', { class: 'is-clash' }, words.fragment('item-clash', { n: count(exam.flags.clash) })));
    if (exam.flags.same_day) bits.push(el('span', { class: 'is-warning' }, words.fragment('item-same_day', { n: count(exam.flags.same_day) })));
    if (exam.flags.no_seat) bits.push(el('span', { class: 'is-clash' }, words.fragment('item-no_seat', { n: count(exam.flags.no_seat) })));
    if (exam.review.includes('changed')) bits.push(el('span', { class: 'is-warning' }, words.raw('item-changed')));
    if (exam.flags.not_recorded) bits.push(words.raw('item-not_recorded'));
    if (exam.flags.not_assigned) bits.push(words.raw('item-not_assigned'));
    return bits;
  }

  function courseItem(exam) {
    const name = words.locale === 'ar' && exam.name_ar ? exam.name_ar : exam.name;
    const chosen = state.course === exam.code && !lookup;
    return el('li', {}, el('button', {
      type: 'button', class: 'et-nav-item', 'data-course': exam.code, 'aria-current': chosen ? 'true' : null, tabindex: '-1',
    }, el('span', { class: 'et-nav-code' }, bdi(exam.code), exam.online ? [' ', el('span', { class: 'et-online-badge' }, words.raw('online'))] : null),
    el('span', { class: 'et-nav-figure', 'aria-label': words.text('item-students', { n: R.NUMBER.format(exam.students) }) }, bdi(R.NUMBER.format(exam.students))),
    el('span', { class: 'et-nav-detail' }, R.join([name ? R.nameNode(name) : null, ...examFlagWords(exam)]))));
  }

  function renderCourseNav() {
    const program = $('examRostersProgram');
    const programs = nav.programs || [];
    if (state.program && !programs.some(item => item.program === state.program)) state.program = '';
    program.replaceChildren(el('option', { value: '' }, words.raw('program-all')),
      ...programs.map(item => el('option', { value: item.program }, words.text('program-option', { program: item.program, n: R.NUMBER.format(item.students) }))));
    program.value = state.program;
    $('examRostersSort').value = state.sort;
    const inProgram = nav.exams.filter(exam => !state.program || (exam.programs[state.program] || 0) > 0);
    const clashes = inProgram.filter(exam => exam.flags.clash > 0).length;
    const reviews = inProgram.filter(exam => exam.review.length > 0).length;
    $('examRostersHasClash').replaceChildren(el('span', {}, words.fragment('has-clashes', { n: count(clashes) })));
    $('examRostersHasClash').setAttribute('aria-pressed', String(state.hasClash));
    $('examRostersCourseReview').replaceChildren(el('span', {}, words.fragment('needs-review', { n: count(reviews) })));
    $('examRostersCourseReview').setAttribute('aria-pressed', String(state.courseReview));
    const shown = inProgram.filter(exam => (!state.hasClash || exam.flags.clash > 0) && (!state.courseReview || exam.review.length > 0));
    const list = $('examRostersCourses');
    const nodes = [];
    if (!shown.length) {
      nodes.push(el('p', { class: 'et-nav-empty' }, words.fragment('empty-courses')));
    } else if (state.sort === 'time') {
      // Grouped by period in timetable order; unplaced exams last.
      const groups = new Map();
      shown.forEach(exam => {
        const key = exam.scheduled ? exam.slot_index : 'none';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(exam);
      });
      groups.forEach((exams, key) => {
        const slot = key === 'none' ? null : slotOf(key);
        const heading = slot ? R.join([R.slot(slot.day, slot.period), bdi(R.NUMBER.format(exams.length))])
          : words.fragment('not-scheduled-group', { n: count(exams.length) });
        nodes.push(...navGroup(heading, exams.map(courseItem)));
      });
    } else {
      const flagTotal = exam => exam.flags.clash + exam.flags.same_day + exam.flags.no_seat + exam.flags.not_recorded + exam.flags.not_assigned;
      const sorted = [...shown].sort({
        code: (a, b) => collator.compare(a.code, b.code),
        students: (a, b) => b.students - a.students || collator.compare(a.code, b.code),
        flags: (a, b) => flagTotal(b) - flagTotal(a) || collator.compare(a.code, b.code),
      }[state.sort]);
      nodes.push(...navGroup(null, sorted.map(courseItem)));
    }
    list.replaceChildren(...nodes);
    rovingItems(list);
  }

  // One tab stop per list: the chosen item, else the first; arrows move.
  function rovingItems(list) {
    const items = [...list.querySelectorAll('.et-nav-item')];
    const chosen = items.find(item => item.getAttribute('aria-current') === 'true') || items[0];
    items.forEach(item => { item.tabIndex = item === chosen ? 0 : -1; });
  }

  // ── The pane ────────────────────────────────────────────────

  const roster = R.createRoster($('examRostersRoster'), {
    idPrefix: 'examRostersRosterList',
    programFilter: true,
    examHref: code => `${window.location.pathname}?${new URLSearchParams({ run: String(RUN_ID), view: 'course', course: code })}`,
    onOpenExam: code => selectCourse(code, { push: true, focusPane: true }),
    announce,
  });

  function setPaneHead(title, meta, { timetable = null } = {}) {
    $('examRostersPaneHead').hidden = false;
    $('examRostersPaneEmpty').hidden = true;
    $('examRostersPane').setAttribute('aria-labelledby', 'examRostersPaneTitle');
    $('examRostersPaneTitle').replaceChildren(title);
    $('examRostersPaneMeta').replaceChildren(meta);
    const show = $('examRostersShowInTimetable');
    show.hidden = !timetable;
    if (timetable) show.href = `${URLS.timetable}?${new URLSearchParams({ run: String(RUN_ID), focus: timetable })}`;
  }

  function clearPane() {
    scopeToken++;
    scopeController?.abort();
    current = null;
    roster.clear();
    $('examRostersPaneHead').hidden = true;
    $('examRostersPaneEmpty').hidden = false;
    if (!navRefused) $('examRostersPaneEmpty').replaceChildren(words.fragment('pane-empty'));
    $('examRostersPane').setAttribute('aria-labelledby', 'examRostersPaneEmpty');
  }

  function roomHead(scope, answer) {
    const room = answer?.room || roomOf(scope.slot_index, scope.room_code);
    const slot = slotOf(scope.slot_index);
    const title = words.fragment('room-title', { room: ltr(scope.room_code) });
    const meta = [];
    if (slot) meta.push(R.slot(slot.day, slot.period));
    if (room) meta.push(words.fragment('seats', { now: count(room.seated_now), capacity: count(room.capacity || 0) }));
    if (room?.building && room?.floor) meta.push(words.fragment('building-floor', { building: ltr(room.building), floor: ltr(room.floor) }));
    else if (room?.building) meta.push(words.fragment('building', { building: ltr(room.building) }));
    else if (room?.floor) meta.push(words.fragment('floor', { floor: ltr(room.floor) }));
    const facts = roomOf(scope.slot_index, scope.room_code);
    if (facts?.sections?.length) {
      const list = R.join(facts.sections.map(section => {
        const out = document.createDocumentFragment();
        out.append(bdi(section.exam), ' ', sectionLabel(section), ' (', bdi(R.NUMBER.format(section.now)), ')');
        return out;
      }), ', ');
      meta.push(words.fragment('in-room', { list }));
    }
    if (room?.online) meta.push(words.raw('online'));
    return { title, meta: R.join(meta) };
  }

  function courseHead(code, answer, section = null) {
    const exam = examOf(code) || answer?.exams?.[code] || { code };
    const title = examTitle(exam);
    if (section) title.append(' · ', sectionLabel(section));
    const meta = [];
    meta.push(exam.scheduled === false ? words.raw('not-scheduled') : R.slot(exam.day, exam.period));
    meta.push(words.fragment('students', { n: count(answer ? answer.counts.students : (exam.students ?? 0)) }));
    const rooms = answer ? [...new Set(answer.sections.flatMap(item => item.parts.map(part => part.room)).filter(Boolean))] : (exam.rooms || []);
    if (rooms.length) meta.push(words.fragment('rooms-list', { rooms: R.join(rooms.map(room => bdi(room)), ', ') }));
    if (exam.online) meta.push(words.raw('online'));
    return { title, meta: R.join(meta) };
  }

  function renderPaneHead() {
    if (!current) return;
    const { scope, answer } = current;
    if (scope.kind === 'room') {
      const head = roomHead(scope, answer);
      setPaneHead(head.title, head.meta);
    } else if (scope.kind === 'course') {
      const head = courseHead(scope.exam, answer);
      setPaneHead(head.title, head.meta, { timetable: scope.exam });
    } else {
      const section = sectionItemOf(current.from ? { key: sectionScopeKey(), from: current.from } : null)
        || answer?.sections?.find(item => item.section_key === scope.section_key && item.gender === scope.gender);
      const head = courseHead(scope.exam, answer, section);
      setPaneHead(head.title, head.meta, { timetable: scope.exam });
    }
  }

  function expectedStudents(scope, from) {
    if (scope.kind === 'room') return roomOf(scope.slot_index, scope.room_code)?.seated_now ?? null;
    if (scope.kind === 'course') return examOf(scope.exam)?.students ?? null;
    return sectionItemOf({ key: `${scope.exam}|${scope.section_key}|${scope.gender}`, from })?.now ?? null;
  }

  function scopeName(scope) {
    if (scope.kind === 'room') return () => words.fragment('room-title', { room: ltr(scope.room_code) });
    return () => bdi(scope.exam);
  }

  // `keep`: the same list again (Refresh), shown with the section, flag,
  // filter and sort it was left at. `flag`: the chip to open on. `from`: the
  // navigator's section list that opened a section.
  async function loadScope(scope, { focusPane = false, keep = false, flag = 'all', from = null } = {}) {
    // A lookup gives way first: closing it may clear the pane.
    closeLookup({ focus: false, reload: false });
    const mine = ++scopeToken;
    scopeController?.abort();
    scopeController = new AbortController();
    current = { scope, answer: null, from, stale: false };
    renderPaneHead();
    setScreen();
    revealPane();
    if (focusPane || narrow()) $('examRostersPaneTitle').focus({ preventScroll: narrow() });
    roster.loading({ students: expectedStudents(scope, from) });
    let result = null;
    let error = null;
    try {
      result = await R.send(URLS.roster, { body: { scope }, signal: scopeController.signal, csrf });
    } catch (caught) {
      if (caught?.name === 'AbortError') return;
      error = caught;
    }
    if (mine !== scopeToken) return;
    if (error || !result.data.ok) {
      const found = R.problem(result, error);
      roster.failed({ content: found.content, retry: found.refused ? null : () => loadScope(scope, { flag, from }) });
      return;
    }
    current.answer = result.data;
    renderPaneHead();
    renderProvenance(result.data);
    roster.show(result.data, { scope: scopeName(scope), keep, flag });
    revealPane();
    const counts = result.data.counts;
    const title = $('examRostersPaneTitle').textContent;
    announce(words.text('announce-scope', {
      title, n: R.NUMBER.format(counts.students), clash: R.NUMBER.format(counts.clash), same_day: R.NUMBER.format(counts.same_day),
    }));
  }

  // ── Choosing ────────────────────────────────────────────────

  function selectRoom(slot, code, { push = true, focusPane = false } = {}) {
    const room = roomOf(slot, code);
    if (!room) return;
    state.view = 'room';
    state.slot = slot;
    state.day = slotOf(slot)?.day ?? state.day;
    state.room = code;
    state.course = null;
    state.section = null;
    writeAddress({ push });
    remember();
    renderNav();
    loadScope({ kind: 'room', slot_index: slot, room_code: code }, { focusPane });
  }

  function selectCourse(code, { push = true, focusPane = false } = {}) {
    if (!examOf(code)) return;
    state.view = 'course';
    state.course = code;
    state.room = null;
    state.section = null;
    writeAddress({ push });
    remember();
    renderNav();
    loadScope({ kind: 'course', exam: code }, { focusPane });
  }

  const sectionScope = item => ({ kind: 'section', exam: item.exam, section_key: item.section_key, gender: item.gender });

  // A section from the navigator's Not assigned or No seat list: a history
  // entry like a room, so a phone's Back returns to the navigator. From No
  // seat it opens on its No seat rows.
  function selectSection(key, from, { push = true, focusPane = false } = {}) {
    const item = sectionItemOf({ key, from });
    if (!item) return;
    state.room = null;
    state.section = { key, from };
    writeAddress({ push });
    renderNav();
    loadScope(sectionScope(item), { focusPane, from, flag: from === 'no_seat' ? 'no_seat' : 'all' });
  }

  function chooseDay(day) {
    if (day === state.day) return;
    state.day = day;
    const slots = slotsOfDay(day);
    state.slot = (slots.find(item => item.exams.length) || slots[0])?.slot_index ?? null;
    state.room = null;
    state.section = null;
    clearPane();
    writeAddress();
    remember();
    renderNav();
  }

  function chooseSlot(slot) {
    if (slot === state.slot) return;
    state.slot = slot;
    state.room = null;
    state.section = null;
    clearPane();
    writeAddress();
    remember();
    renderNav();
  }

  function setView(view) {
    if (view === state.view) return;
    state.view = view;
    state.room = null;
    state.course = null;
    state.section = null;
    clearPane();
    writeAddress();
    remember();
    renderNav();
    setScreen();
  }

  // What the address says, applied: on load, and on Back and Forward.
  function applyAddress({ initial = false } = {}) {
    const address = readAddress();
    const stored = initial ? remembered() : {};
    state.view = address.view || (stored.view === 'course' ? 'course' : 'room');
    if (initial && ['M', 'F'].includes(stored.group)) state.group = stored.group;
    const slot = slotOf(address.slot) || slotOf(stored.slot)
      || nav.slots.find(item => item.exams.length) || nav.slots[0] || null;
    state.slot = slot ? slot.slot_index : null;
    state.day = slot ? slot.day : nav.days[0]?.day ?? null;
    state.room = state.view === 'room' && address.room && roomOf(state.slot, address.room) ? address.room : null;
    state.course = state.view === 'course' && address.course && examOf(address.course) ? address.course : null;
    const section = state.view === 'room' && !state.room ? sectionItemOf(address.section) : null;
    state.section = section && section.slot_index === state.slot ? address.section : null;
    if (initial) writeAddress();
    renderNav();
    if (state.room) {
      if (current?.stale || current?.scope?.kind !== 'room' || current.scope.room_code !== state.room || current.scope.slot_index !== state.slot) {
        loadScope({ kind: 'room', slot_index: state.slot, room_code: state.room });
      }
    } else if (state.section) {
      if (current?.stale || sectionScopeKey() !== state.section.key || current.from !== state.section.from) {
        loadScope(sectionScope(section), { from: state.section.from, flag: state.section.from === 'no_seat' ? 'no_seat' : 'all' });
      }
    } else if (state.course) {
      if (current?.stale || current?.scope?.kind !== 'course' || current.scope.exam !== state.course) loadScope({ kind: 'course', exam: state.course });
    } else {
      closeLookup({ focus: false, reload: false });
      clearPane();
    }
    setScreen();
  }

  // On a phone the pane is the screen: bring its top into view (again once
  // its list has grown the page).
  function revealPane() {
    if (narrow()) $('examRostersPane').scrollIntoView?.({ block: 'start' });
  }

  // Master-detail below 800px: the pane only while something is chosen. One
  // way back at a time: while a lookup is open, its own Back leads.
  function setScreen() {
    $('examRostersLayout').dataset.screen = current || lookup ? 'pane' : 'nav';
    $('examRostersScreenBack').hidden = Boolean(lookup);
  }

  // What the pane shows, as a navigator item's key (to find its item again).
  function chosenKey() {
    if (state.room) return { room: state.room, slot: state.slot };
    if (state.section) return { section: state.section.key, from: state.section.from };
    if (state.course) return { course: state.course };
    return null;
  }

  // After Back, Forward or ‹ Rooms the navigator was drawn again: focus goes
  // to the item now chosen, else to the one whose list was just left (the
  // control that opened it), else the list's tab stop - scrolled into view,
  // never left on the page body.
  function restoreNavFocus(left) {
    if (narrow() && $('examRostersLayout').dataset.screen === 'pane') return;
    const list = state.view === 'room' ? $('examRostersRooms') : $('examRostersCourses');
    const item = findNavItem(chosenKey()) || findNavItem(left) || list.querySelector('.et-nav-item[tabindex="0"]');
    if (!focusNavItem(item)) $('examRostersNav').querySelector('[role="tab"][aria-selected="true"], [role="radio"][aria-checked="true"]')?.focus();
  }

  function screenBack() {
    if (lookup) {
      closeLookup();
      if (current) return;
    }
    if (window.history.state?.rosters && window.history.state.pushed && (state.room || state.course || state.section)) {
      window.history.back();
      return;
    }
    const left = chosenKey();
    state.room = null;
    state.course = null;
    state.section = null;
    clearPane();
    writeAddress();
    renderNav();
    setScreen();
    restoreNavFocus(left);
  }

  // ── Student lookup: POST, no history entry ──────────────────

  function lookupRow(row, answer) {
    const exam = answer.exams[row.exam] || {};
    const flags = el('td', { role: 'cell', class: 'et-col-flags' });
    const link = (kind, code) => {
      const facts = answer.exams[code];
      const node = el('a', {
        class: `et-flag et-flag--${kind === 'same_day' ? 'sameday' : kind}`, 'data-exam': code,
        href: `${window.location.pathname}?${new URLSearchParams({ run: String(RUN_ID), view: 'course', course: code })}`,
      }, el('span', { class: 'et-flag-icon', 'aria-hidden': 'true' }), words.fragment(`flag-${kind}`, { code: ltr(code) }),
      R.visuallyHidden(` — ${words.text(`flag-${kind}-full`, { code, slot: facts ? R.slotText(facts.day, facts.period) : '' })}`));
      return node;
    };
    (row.clash_with || []).forEach(code => flags.append(link('clash', code)));
    (row.same_day_with || []).forEach(code => flags.append(link('same_day', code)));
    if (row.room_basis === 'no_seat') flags.append(el('span', { class: 'et-flag et-flag--noseat' }, el('span', { class: 'et-flag-icon', 'aria-hidden': 'true' }), words.raw('no-seat')));
    // The room, or why there is none: a fresh node for each place it is said.
    const roomWords = () => ((row.room_basis === 'whole' || row.room_basis === 'split') && row.room ? bdi(row.room)
      : el('span', { class: row.room_basis === 'no_seat' ? 'et-room-noseat' : 'et-room-none' },
        words.raw(row.room_basis === 'no_seat' ? 'no-seat' : row.room_basis === 'not_scheduled' ? 'not-scheduled' : 'not-assigned')));
    const open = el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary', 'data-open-course': row.exam, 'aria-label': words.text('lookup-open-label', { code: row.exam }) },
      words.raw('lookup-open'), ' ', el('span', { class: 'et-chevron', 'aria-hidden': 'true' }, '›'));
    // In a narrow pane the row is a card: the course, then when, section and
    // room on one line under it (shown only there), then the flags.
    const when = exam.day ? R.slot(exam.day, exam.period) : words.raw('not-scheduled');
    const sub = el('span', { class: 'et-lookup-sub' }, R.join([when, sectionLabel(row), roomWords()]));
    return el('tr', { role: 'row', class: `et-lookup-row${row.clash ? ' is-clash' : ''}` },
      el('td', { role: 'cell', class: 'et-col-day' }, exam.day ? bdi(exam.day) : words.raw('not-scheduled')),
      el('td', { role: 'cell', class: 'et-col-period' }, exam.period ? bdi(exam.period) : '—'),
      el('td', { role: 'cell', class: 'et-col-name' }, bdi(row.exam), exam.name ? [' ', el('small', { class: 'et-lookup-name' }, R.nameNode(words.locale === 'ar' && exam.name_ar ? exam.name_ar : exam.name))] : null, sub),
      el('td', { role: 'cell', class: 'et-col-section' }, sectionLabel(row)),
      el('td', { role: 'cell', class: 'et-col-room' }, roomWords()), flags,
      el('td', { role: 'cell', class: 'et-col-open' }, open));
  }

  function renderLookup(studentId, answer) {
    const box = $('examRostersLookup');
    const rows = answer.rows || [];
    const first = rows[0];
    const title = words.fragment('lookup-title', { id: ltr(studentId) });
    const meta = first ? R.join([R.nameNode(first.name), bdi(first.program)]) : document.createTextNode('');
    $('examRostersPaneHead').hidden = false;
    $('examRostersPaneEmpty').hidden = true;
    $('examRostersPane').setAttribute('aria-labelledby', 'examRostersPaneTitle');
    $('examRostersPaneTitle').replaceChildren(title);
    $('examRostersPaneMeta').replaceChildren(meta);
    $('examRostersShowInTimetable').hidden = true;
    if (!answer.found) {
      box.replaceChildren(el('p', { class: 'et-pane-empty' }, words.fragment('lookup-none', { id: ltr(studentId) })));
      return;
    }
    const summary = el('p', { class: 'et-roster-count' }, words.fragment('lookup-summary', {
      run: ltr(RUN_ID), n: count(rows.length), clash: count(answer.counts.clash), same_day: count(answer.counts.same_day),
    }));
    const head = el('tr', { role: 'row' }, ...['col-day', 'col-period', 'col-course', 'col-section', 'col-room', 'col-flags', 'col-open']
      .map(key => el('th', { scope: 'col', role: 'columnheader', class: ['col-day', 'col-period', 'col-open'].includes(key) ? `et-${key}` : null }, words.raw(key))));
    const table = el('table', { class: 'et-roster-table et-lookup-table', role: 'table' },
      el('caption', { class: 'visually-hidden' }, $('examRostersPaneTitle').textContent),
      el('thead', { role: 'rowgroup' }, head),
      el('tbody', { role: 'rowgroup' }, rows.map(row => lookupRow(row, answer))));
    box.replaceChildren(summary, table);
  }

  // `focus`: the heading takes focus once the student is in (not on Refresh,
  // which keeps the keyboard on its button).
  async function lookupStudent(studentId, { focus = true } = {}) {
    const mine = ++lookupToken;
    lookupController?.abort();
    lookupController = new AbortController();
    // Back leads where the first lookup was opened from: a second lookup (or
    // a refreshed one) keeps it, never "Back to Student …".
    const from = lookup ? lookup.from : current ? $('examRostersPaneTitle').textContent : '';
    lookup = { studentId, from, answer: null };
    $('examRostersRoster').hidden = true;
    $('examRostersLookup').hidden = false;
    $('examRostersLookup').replaceChildren(el('div', { class: 'et-roster-skeleton', 'aria-busy': 'true' },
      ...Array.from({ length: 4 }, () => el('span', { class: 'et-skeleton-bar' }))));
    const back = $('examRostersLookupBack');
    back.hidden = false;
    $('examRostersLookupBackText').replaceChildren(from ? words.fragment('lookup-back-to', { where: from }) : words.fragment('lookup-back'));
    setScreen();
    renderNav();
    let result = null;
    let error = null;
    try {
      result = await R.send(URLS.lookup, { body: { student_id: String(studentId) }, signal: lookupController.signal, csrf });
    } catch (caught) {
      if (caught?.name === 'AbortError') return;
      error = caught;
    }
    if (mine !== lookupToken || !lookup) return;
    if (error || !result.data.ok) {
      const found = R.problem(result, error);
      const alert = el('div', { class: 'alert alert-danger et-roster-error', role: 'alert' }, el('span', {}, found.content()));
      if (!found.refused) {
        const retry = el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary' }, words.raw('try-again'));
        retry.addEventListener('click', () => lookupStudent(studentId));
        alert.append(retry);
      }
      $('examRostersLookup').replaceChildren(alert);
      return;
    }
    lookup.answer = result.data;
    renderProvenance(result.data);
    renderLookup(studentId, result.data);
    if (focus) $('examRostersPaneTitle').focus({ preventScroll: false });
  }

  // `reload`: a list behind the lookup that Refresh left stale is asked for
  // again as it is shown (off when the caller is about to load a list).
  function closeLookup({ focus = true, reload = true } = {}) {
    if (!lookup) return;
    lookupToken++;
    lookupController?.abort();
    lookup = null;
    $('examRostersLookup').hidden = true;
    $('examRostersLookup').replaceChildren();
    $('examRostersLookupBack').hidden = true;
    $('examRostersRoster').hidden = false;
    if (current) renderPaneHead();
    else clearPane();
    setScreen();
    renderNav();
    if (focus) {
      const target = current ? $('examRostersPaneTitle') : $('examRostersFind');
      target.focus({ preventScroll: false });
    }
    if (reload && current?.stale) loadScope(current.scope, { keep: true, from: current.from });
  }

  // ── Find ────────────────────────────────────────────────────

  const find = $('examRostersFind');
  const findList = $('examRostersFindList');
  let findTimer = 0;
  let findToken = 0;
  let findController = null;
  let findOptions = [];   // [{ id, kind, choose() }]
  let activeOption = -1;
  let students = null;    // the settled student search for the current query
  let lastQuery = '';
  let pendingEnter = null; // Enter pressed before this query's students arrived

  // The server's rule (core/services/exam_roster_view.py parse_lookup), so
  // Find never sends what it would refuse: 4+ digits of an ID, or a name of
  // 3+ letters with 2+ in each word. Digits beside letters are a course or
  // room code, matched here and never searched (or audited) as a student.
  const letters = text => (String(text).match(/\p{L}/gu) || []).length;
  function studentQuery(text) {
    const folded = R.fold(text);
    const compact = folded.replace(/ /g, '');
    if (/^\d+$/.test(compact)) return compact.length >= 4 ? compact : null;
    if (/\p{Nd}/u.test(compact)) return null;
    return letters(compact) >= 3 && folded.split(' ').every(word => letters(word) >= 2) ? folded : null;
  }

  function localMatches(text) {
    const query = R.fold(text);
    if (!query || !nav) return { courses: [], rooms: [] };
    const compact = query.replace(/ /g, '');
    const courses = nav.exams.filter(exam => R.fold(exam.code).replace(/ /g, '').includes(compact)
      || (compact.length >= 3 && [exam.name, exam.name_ar].some(name => name && R.fold(name).includes(query))))
      .sort((a, b) => Number(!R.fold(b.code).startsWith(compact)) - Number(!R.fold(a.code).startsWith(compact)) || collator.compare(a.code, b.code))
      .slice(0, FIND_LIMIT);
    const rooms = nav.rooms.filter(room => R.fold(room.room_code).includes(compact))
      .sort((a, b) => a.slot_index - b.slot_index || collator.compare(a.room_code, b.room_code))
      .slice(0, FIND_LIMIT);
    return { courses, rooms };
  }

  function option(label, choose, kind) {
    const id = `examRostersFindOption${findOptions.length}`;
    findOptions.push({ id, kind, choose });
    return el('div', { role: 'option', id, class: 'et-find-option', 'aria-selected': 'false' }, label);
  }

  function group(key, options) {
    const labelId = `examRostersFindGroup-${key}`;
    return el('div', { role: 'group', 'aria-labelledby': labelId },
      el('div', { id: labelId, class: 'et-find-group', role: 'presentation' }, words.raw(`find-${key}`)), options);
  }

  function renderFind() {
    const text = find.value.trim();
    findOptions = [];
    activeOption = -1;
    find.removeAttribute('aria-activedescendant');
    if (!text) {
      closeFind();
      return;
    }
    const { courses, rooms } = localMatches(text);
    const nodes = [];
    if (courses.length) {
      nodes.push(group('courses', courses.map(exam => {
        const label = document.createDocumentFragment();
        label.append(examTitle(exam));
        if (exam.scheduled) label.append(' · ', R.slot(exam.day, exam.period));
        return option(label, () => selectCourse(exam.code, { push: true, focusPane: true }), 'course');
      })));
    }
    if (rooms.length) {
      nodes.push(group('rooms', rooms.map(room => {
        const slot = slotOf(room.slot_index);
        return option(words.fragment('find-room-item', { room: ltr(room.room_code), slot: slot ? R.slot(slot.day, slot.period) : '', n: count(room.seated_now) }),
          () => selectRoom(room.slot_index, room.room_code, { push: true, focusPane: true }), 'room');
      })));
    }
    const query = studentQuery(text);
    if (query && students?.query === query) {
      if (students.failed) {
        nodes.push(el('div', { class: 'et-find-note', role: 'presentation' }, words.fragment('find-failed')));
      } else if (students.matches.length) {
        nodes.push(group('students', students.matches.map(match => option(words.fragment('find-student-item', {
          id: ltr(match.student_id), name: R.nameNode(match.name), program: ltr(match.program), n: count(match.exams),
        }), () => lookupStudent(match.student_id), 'student'))));
        if (students.more) nodes.push(el('div', { class: 'et-find-note', role: 'presentation' }, words.fragment('find-more', { n: count(students.total - students.matches.length) })));
      }
    } else if (query) {
      nodes.push(el('div', { class: 'et-find-note', role: 'presentation' }, words.fragment('find-searching')));
    }
    if (!findOptions.length && !(query && students?.query !== query)) {
      nodes.push(el('div', { class: 'et-find-note', role: 'presentation' }, words.fragment('find-none', { q: text }),
        query ? '' : [' ', words.fragment('find-hint')]));
    }
    findList.replaceChildren(...nodes);
    findList.hidden = false;
    find.setAttribute('aria-expanded', 'true');
  }

  function closeFind() {
    findList.hidden = true;
    findList.replaceChildren();
    find.setAttribute('aria-expanded', 'false');
    find.removeAttribute('aria-activedescendant');
    findOptions = [];
    activeOption = -1;
  }

  // Students are matched on the server once typing pauses: one request (and
  // one audit row) per settled search, never per keystroke.
  function scheduleStudents() {
    clearTimeout(findTimer);
    const query = studentQuery(find.value.trim());
    if (!query || query === lastQuery) return;
    findTimer = setTimeout(() => searchStudents(query), TIMING.findDelay);
  }

  async function searchStudents(query) {
    const mine = ++findToken;
    findController?.abort();
    findController = new AbortController();
    lastQuery = query;
    let result = null;
    let error = null;
    try {
      result = await R.send(URLS.lookup, { body: { query: find.value.trim() }, signal: findController.signal, csrf });
    } catch (caught) {
      if (caught?.name === 'AbortError') return;
      error = caught;
    }
    if (mine !== findToken) return;
    if (error || !result.data.ok) {
      students = { query, failed: true, matches: [], total: 0, more: false };
      lastQuery = '';
    } else {
      students = { query, failed: false, matches: result.data.matches, total: result.data.total, more: result.data.more };
      renderProvenance(result.data);
    }
    if (studentQuery(find.value.trim()) === query) {
      renderFind();
      announce(words.text('find-results', { n: R.NUMBER.format(findOptions.length) }));
      // Enter was pressed before the students arrived (an ID typed or pasted,
      // then Enter at once): it chooses the first student now, as it would have.
      if (pendingEnter === query) {
        pendingEnter = null;
        const first = findOptions.findIndex(item => item.kind === 'student');
        if (first >= 0) chooseOption(first);
      }
    }
    if (pendingEnter === query) pendingEnter = null;
  }

  function setActive(index) {
    const nodes = findList.querySelectorAll('[role="option"]');
    nodes.forEach((node, i) => node.setAttribute('aria-selected', String(i === index)));
    activeOption = index;
    if (index >= 0 && nodes[index]) {
      find.setAttribute('aria-activedescendant', nodes[index].id);
      nodes[index].scrollIntoView?.({ block: 'nearest' });
    } else {
      find.removeAttribute('aria-activedescendant');
    }
  }

  function chooseOption(index) {
    const chosen = findOptions[index];
    if (!chosen) return;
    closeFind();
    find.value = '';
    students = null;
    lastQuery = '';
    pendingEnter = null;
    chosen.choose();
  }

  find.addEventListener('input', () => {
    if (pendingEnter && studentQuery(find.value.trim()) !== pendingEnter) pendingEnter = null;
    renderFind();
    scheduleStudents();
  });
  find.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (findList.hidden) renderFind();
      if (!findOptions.length) return;
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      setActive((activeOption + step + findOptions.length) % findOptions.length);
    } else if (event.key === 'Enter') {
      if (!findList.hidden && findOptions.length) {
        event.preventDefault();
        chooseOption(activeOption >= 0 ? activeOption : 0);
        return;
      }
      // No option yet, but students are on their way (or wait for the pause
      // after typing): remember the Enter, and ask now rather than after it.
      const query = studentQuery(find.value.trim());
      if (!query || students?.query === query) return;
      event.preventDefault();
      pendingEnter = query;
      if (lastQuery !== query) {
        clearTimeout(findTimer);
        searchStudents(query);
      }
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      pendingEnter = null;
      // Esc clears the field, then closes the list.
      if (find.value) {
        find.value = '';
        students = null;
        lastQuery = '';
        clearTimeout(findTimer);
      }
      closeFind();
    }
  });
  findList.addEventListener('mousedown', event => event.preventDefault());
  findList.addEventListener('click', event => {
    const node = event.target.closest('[role="option"]');
    if (node) chooseOption(findOptions.findIndex(item => item.id === node.id));
  });
  find.addEventListener('blur', () => setTimeout(() => {
    if (document.activeElement !== find) closeFind();
  }, 0));

  // "/" finds, unless typing somewhere or switched off (WCAG 2.1.4).
  const shortcut = $('examRostersShortcut');
  shortcut.checked = remembered().shortcut !== false;
  $('examRostersFindKey').hidden = !shortcut.checked;
  shortcut.addEventListener('change', () => {
    remember({ shortcut: shortcut.checked });
    $('examRostersFindKey').hidden = !shortcut.checked;
  });
  document.addEventListener('keydown', event => {
    if (event.key === '/' && shortcut.checked && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const target = event.target;
      const typing = target?.closest?.('input, textarea, select, [contenteditable="true"]');
      if (typing || document.querySelector('dialog[open]')) return;
      event.preventDefault();
      find.focus();
      return;
    }
    if (event.key === 'Escape' && lookup && !event.defaultPrevented && !document.querySelector('dialog[open]')) {
      event.preventDefault();
      closeLookup();
    }
  });

  // ── Events ──────────────────────────────────────────────────

  function radioKeys(container, selector, apply) {
    container.addEventListener('keydown', event => {
      const items = [...container.querySelectorAll(selector)];
      const next = R.rovingKeys(event, items, event.target.closest(selector));
      if (!next) return;
      apply(next);
      container.querySelector(`${selector}[aria-checked="true"], ${selector}[aria-selected="true"]`)?.focus();
    });
  }

  $('examRostersViews').addEventListener('click', event => {
    const button = event.target.closest('[data-view]');
    if (button) setView(button.dataset.view);
  });
  radioKeys($('examRostersViews'), '[role="radio"]', next => setView(next.dataset.view));

  $('examRostersDays').addEventListener('click', event => {
    const tab = event.target.closest('[role="tab"]');
    if (!tab) return;
    chooseDay(tab.dataset.day);
    $('examRostersDays').querySelector('[aria-selected="true"]')?.focus();
  });
  radioKeys($('examRostersDays'), '[role="tab"]', next => chooseDay(next.dataset.day));

  $('examRostersPeriods').addEventListener('click', event => {
    const chip = event.target.closest('[role="radio"]');
    if (!chip) return;
    chooseSlot(Number(chip.dataset.slot));
    $('examRostersPeriods').querySelector('[aria-checked="true"]')?.focus();
  });
  radioKeys($('examRostersPeriods'), '[role="radio"]', next => chooseSlot(Number(next.dataset.slot)));

  function chooseGroup(group) {
    if (group === state.group) return;
    state.group = group;
    remember();
    renderNav();
  }
  $('examRostersGroups').addEventListener('click', event => {
    const chip = event.target.closest('[role="radio"]');
    if (!chip) return;
    chooseGroup(chip.dataset.group);
    $('examRostersGroups').querySelector('[aria-checked="true"]')?.focus();
  });
  radioKeys($('examRostersGroups'), '[role="radio"]', next => chooseGroup(next.dataset.group));

  $('examRostersRoomReview').addEventListener('click', () => {
    state.roomReview = !state.roomReview;
    renderNav();
  });
  $('examRostersHasClash').addEventListener('click', () => {
    state.hasClash = !state.hasClash;
    renderNav();
  });
  $('examRostersCourseReview').addEventListener('click', () => {
    state.courseReview = !state.courseReview;
    renderNav();
  });
  $('examRostersProgram').addEventListener('change', event => {
    state.program = event.target.value;
    renderNav();
  });
  $('examRostersSort').addEventListener('change', event => {
    state.sort = ['time', 'code', 'students', 'flags'].includes(event.target.value) ? event.target.value : 'time';
    renderNav();
  });

  // The navigator lists: one tab stop, Up and Down (and Home, End) move.
  ['examRostersRooms', 'examRostersCourses'].forEach(id => {
    const list = $(id);
    list.addEventListener('click', event => {
      const item = event.target.closest('.et-nav-item');
      if (!item) return;
      const focusPane = narrow();
      const key = navItemKey(item);
      if (item.dataset.room) selectRoom(Number(item.dataset.slot), item.dataset.room, { push: true, focusPane });
      else if (item.dataset.course) selectCourse(item.dataset.course, { push: true, focusPane });
      else if (item.dataset.sectionGroup) selectSection(item.dataset.sectionGroup, item.dataset.from, { push: true, focusPane });
      if (!focusPane) findNavItem(key)?.focus({ preventScroll: true });
    });
    list.addEventListener('keydown', event => {
      const items = [...list.querySelectorAll('.et-nav-item')];
      const at = items.indexOf(event.target.closest('.et-nav-item'));
      if (at < 0) return;
      let next = null;
      if (event.key === 'ArrowDown') next = items[Math.min(items.length - 1, at + 1)];
      else if (event.key === 'ArrowUp') next = items[Math.max(0, at - 1)];
      else if (event.key === 'Home') next = items[0];
      else if (event.key === 'End') next = items[items.length - 1];
      if (!next) return;
      event.preventDefault();
      items.forEach(item => { item.tabIndex = item === next ? 0 : -1; });
      next.focus();
    });
  });

  $('examRostersScreenBack').addEventListener('click', screenBack);
  $('examRostersLookupBack').addEventListener('click', () => closeLookup());
  $('examRostersLookup').addEventListener('click', event => {
    const open = event.target.closest('[data-open-course]');
    const flag = event.target.closest('a[data-exam]');
    if (flag && (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return;
    const code = open?.dataset.openCourse || flag?.dataset.exam;
    if (!code) return;
    event.preventDefault();
    selectCourse(code, { push: true, focusPane: true });
  });
  // Refresh checks the lists again and re-asks for what is on screen, as it
  // was left: a lookup (the list behind it once it closes), or the list with
  // its section tab, flag chip, filter and sort.
  $('examRostersRefresh').addEventListener('click', async () => {
    if (!await loadNav({ refresh: true })) return;
    if (lookup) {
      if (current) current.stale = true;
      lookupStudent(lookup.studentId, { focus: false });
    } else if (current) {
      loadScope(current.scope, { keep: true, from: current.from });
    }
  });
  window.addEventListener('popstate', () => {
    if (!nav) return;
    const left = chosenKey();
    const active = document.activeElement;
    const lost = !active || active === document.body || $('examRostersNav').contains(active) || $('examRostersPane').contains(active);
    closeLookup({ focus: false, reload: false });
    applyAddress();
    if (lost) restoreNavFocus(left);
  });
  // Restored from the back/forward cache: no list or lookup a Back could
  // bring back stays on the page, and the lists are asked for again (a
  // session that ended says so in place).
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    closeLookup({ focus: false, reload: false });
    find.value = '';
    students = null;
    lastQuery = '';
    closeFind();
    state.room = null;
    state.course = null;
    state.section = null;
    clearPane();
    setScreen();
    writeAddress();
    if (nav) renderNav();
    loadNav();
  });

  // ── Export to Excel: the phase-1 dialog, preset ─────────────

  function exportScope() {
    const scope = current?.scope;
    if (scope?.kind === 'room' || scope?.kind === 'section') return { ...scope };
    if (scope?.kind === 'course') {
      const group = roster.shown() ? roster.selection().group : null;
      return group ? { kind: 'section', ...group } : { kind: 'course', exam: scope.exam };
    }
    if (state.view === 'room' && state.slot !== null) return { kind: 'period', slot_index: state.slot };
    return null;
  }

  // The dialog's view of this run: its exams, periods, rooms and days, and
  // the sections of the list on screen. Partial, so it waits for the
  // preflight's counts instead of showing saved ones.
  function runForExport() {
    const roomsByExam = new Map();
    nav.rooms.forEach(room => room.exams.forEach(code => {
      if (!roomsByExam.has(code)) roomsByExam.set(code, []);
      roomsByExam.get(code).push({ room_code: room.room_code, gender: room.gender });
    }));
    const enrollment = {};
    (current?.answer?.sections || []).forEach(section => {
      (enrollment[section.exam] ||= []).push({
        section_key: section.section_key, gender: section.gender, section: section.section || '', mapping_status: section.section_status,
      });
    });
    return {
      partial: true,
      label: nav.run.label,
      created_at: nav.run.saved_at,
      academic_year: nav.run.academic_year,
      term: nav.run.term,
      genders: [...new Set([...nav.rooms, ...nav.not_assigned, ...(nav.no_seat || [])].map(item => item.gender))],
      slots: nav.slots.map(slot => ({ index: slot.slot_index, day: slot.day, period: slot.period })),
      schedule: nav.exams.map(exam => ({
        course_code: exam.code, course_name: exam.name, day: exam.scheduled ? exam.day : 'OVERFLOW', period: exam.period || '',
        slot_index: exam.scheduled ? exam.slot_index : -1, rooms: roomsByExam.get(exam.code) || [],
      })),
      section_enrollment: enrollment,
    };
  }

  async function readForExport(response) {
    const { data } = await R.readAnswer(response);
    if (data.code === 'session' || data.code === 'throttled') throw new Error(words.raw(`error-${data.code}`));
    return data;
  }

  window.examStudentExportHost = Object.freeze({
    blockReason: () => (navRefused ? navRefused.content().textContent : !nav ? words.raw('loading') : ''),
    context: () => ({ runId: RUN_ID }),
    isCurrent: context => Boolean(context) && context.runId === RUN_ID,
    savedRun: runForExport,
    readResponse: readForExport,
    csrf,
    closeDepartmentFiles: () => {},
  });

  $('examRostersExport').addEventListener('click', event => {
    if (event.currentTarget.getAttribute('aria-disabled') === 'true' || !window.examStudentExport) return;
    const scope = exportScope();
    window.examStudentExport.open(event.currentTarget, scope ? { scope } : null);
  });

  // ── Start ───────────────────────────────────────────────────

  clearPane();
  renderExportState();
  loadNav();
})();
