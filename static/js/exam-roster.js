/*
 * Student lists: the one roster component, shared by the course drawer on
 * the Timetable page (static/js/exam-roster-drawer.js) and the Student lists
 * page (static/js/page-exam-rosters.js).
 *
 * It renders a scope answer of core/exam_roster_views.py exactly as the
 * server sent it: the rows are the export's own rows (same seat rule, flags
 * and verdicts), in seat order. The component only chooses which to show and
 * in what order:
 *
 * - Section tabs (a tablist; after 8 tabs the rest wait in "More"), flag
 *   chips (a single-choice radiogroup whose labels carry their counts), an ID
 *   or name filter and, on the page, a program filter. Kinds combine with AND.
 * - A real <table>: a hidden caption, sortable column headers (aria-sort),
 *   one row group per section part in seat order with its facts (students,
 *   ID range, split, changed, not recorded, no seat), a clash stripe, and
 *   flags as words that link to the other exam's list. Large lists render in
 *   batches at idle time, the count saying "Showing 400 of 900…" meanwhile.
 * - Loading, failure and empty states in place; good news said as such.
 *
 * Nothing here requests a student or writes the address: the containers
 * fetch (POST, audited) and own history. Every sentence comes from the
 * template's #examRosterCopy in the page language; codes, IDs, times and
 * counts sit in <bdi dir="ltr">, names in a plain <bdi>. Never dir="auto".
 */
(function (root) {
  'use strict';

  const doc = root.document;
  const TIMING = { batch: 200, loadingNotice: 400, filterDelay: 150, announceDelay: 600, ...(root.__examRosterTiming || {}) };
  const NUMBER = new Intl.NumberFormat('en-US');
  const ARABIC_SCRIPT = /\p{Script=Arabic}/u;
  const LTR = Symbol('ltr');
  const SEATED = new Set(['whole', 'split']);
  const FLAGS = ['all', 'clash', 'same_day', 'no_seat', 'not_recorded'];
  const VISIBLE_TABS = 8;

  // ── Copy ────────────────────────────────────────────────────

  const ltr = value => ({ [LTR]: String(value ?? '') });
  const count = n => ltr(NUMBER.format(Number(n) || 0));

  function bdi(text, dir = 'ltr') {
    const node = doc.createElement('bdi');
    if (dir) node.dir = dir;
    node.textContent = String(text ?? '');
    return node;
  }

  // A name keeps its own direction; Arabic script says so inside English.
  function nameNode(name) {
    const node = bdi(name, '');
    if (ARABIC_SCRIPT.test(String(name || '')) && !String(doc.documentElement.lang || '').startsWith('ar')) node.lang = 'ar';
    return node;
  }

  function createCopy(node) {
    const raw = key => node?.getAttribute(`data-${key}`) ?? '';
    // A template's {name} slots take text, a node, or ltr(...) for a code,
    // count, time or ID, isolated left to right. A fresh fragment each call.
    function fragment(key, values = {}) {
      const out = doc.createDocumentFragment();
      raw(key).split(/(\{\w+\})/).forEach(part => {
        const slot = /^\{(\w+)\}$/.exec(part);
        if (!slot) {
          if (part) out.append(part);
          return;
        }
        const value = values[slot[1]];
        if (value instanceof root.Node) out.append(value);
        else if (value && typeof value === 'object' && LTR in value) out.append(bdi(value[LTR]));
        else out.append(String(value ?? ''));
      });
      return out;
    }
    // Plain text, for attributes (an accessible name, a title).
    const text = (key, values = {}) => fragment(key, Object.fromEntries(Object.entries(values)
      .map(([name, value]) => [name, value instanceof root.Node ? value.textContent : value]))).textContent;
    return { raw, fragment, text, locale: raw('locale') || 'en' };
  }

  let sharedCopy = null;
  const copy = () => (sharedCopy ||= createCopy(doc.getElementById('examRosterCopy')));

  // ── DOM ─────────────────────────────────────────────────────

  function el(tag, attributes = {}, ...children) {
    const node = doc.createElement(tag);
    Object.entries(attributes).forEach(([name, value]) => {
      if (value === null || value === undefined || value === false) return;
      if (name === 'class') node.className = value;
      else if (name === 'text') node.textContent = value;
      else node.setAttribute(name, value === true ? '' : String(value));
    });
    node.append(...children.flat().filter(child => child !== null && child !== undefined && child !== false && child !== ''));
    return node;
  }

  const visuallyHidden = text => el('span', { class: 'visually-hidden' }, text);

  function join(nodes, separator = ' · ') {
    const out = doc.createDocumentFragment();
    nodes.filter(Boolean).forEach((node, index) => out.append(...(index ? [separator, node] : [node])));
    return out;
  }

  // ── Words shared by both containers ─────────────────────────

  const groupWord = gender => copy().raw(`group-${String(gender || 'u').toLowerCase()}`);

  // The section as the export words it: the label of a mapped section, else
  // what is missing and for which group.
  function sectionName(item) {
    if (item?.section_status === 'mapped' && item.section) return String(item.section);
    const base = copy().raw(item?.section_status === 'ambiguous' ? 'section-ambiguous' : 'section-missing');
    return copy().text('section-with-group', { section: base, group: groupWord(item?.gender) });
  }

  // "Sun 08:00-10:00", each code isolated.
  function slot(day, period) {
    const out = doc.createDocumentFragment();
    if (day) out.append(bdi(day));
    if (day && period) out.append(' ');
    if (period) out.append(bdi(period));
    return out;
  }

  const slotText = (day, period) => [day, period].filter(Boolean).join(' ');

  function pad(n) {
    return String(n).padStart(2, '0');
  }

  function savedAt(iso) {
    const date = new Date(iso);
    if (!iso || Number.isNaN(date.getTime())) return '';
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function clock(iso) {
    const date = new Date(iso);
    if (!iso || Number.isNaN(date.getTime())) return '';
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  // Name and ID matching, folded as the server folds a search (case,
  // spacing, Arabic letter variants and diacritics, Arabic-Indic digits).
  const FOLD = new Map([
    [0x0623, 0x0627], [0x0625, 0x0627], [0x0622, 0x0627], [0x0671, 0x0627],
    [0x0649, 0x064A], [0x0629, 0x0647],
    ...Array.from({ length: 10 }, (_, d) => [0x0660 + d, 0x30 + d]),
    ...Array.from({ length: 10 }, (_, d) => [0x06F0 + d, 0x30 + d]),
  ]);
  const DROPPED = code => (code >= 0x064B && code <= 0x065F) || code === 0x0670 || code === 0x0640;
  function fold(text) {
    let out = '';
    for (const char of String(text ?? '').normalize('NFKC')) {
      const code = char.codePointAt(0);
      if (DROPPED(code)) continue;
      out += FOLD.has(code) ? String.fromCodePoint(FOLD.get(code)) : char;
    }
    return out.toLowerCase().split(/\s+/).filter(Boolean).join(' ');
  }

  // ── Requests ────────────────────────────────────────────────

  // The JSON answer and its status, whatever came back. A login page (the
  // session ended) or a non-JSON page is named, never parsed or navigated to.
  async function readAnswer(response) {
    const finalPath = response.url ? new URL(response.url, root.location.href).pathname : '';
    if (response.status === 401 || (response.redirected && /\/login\/?$/.test(finalPath))) {
      return { status: 401, data: { ok: false, code: 'session' } };
    }
    if (response.status === 429) return { status: 429, data: { ok: false, code: 'throttled' } };
    let data = null;
    try {
      const type = response.headers?.get('content-type') || '';
      if (!type || /\bjson\b/i.test(type)) data = await response.json();
    } catch (_) {
      data = null;
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return { status: response.status, data: { ok: false, code: response.status === 403 ? 'forbidden' : 'server' } };
    }
    return { status: response.status, data };
  }

  async function send(url, { body, signal, csrf } = {}) {
    const init = body === undefined
      ? { headers: { Accept: 'application/json' }, credentials: 'same-origin', signal }
      : {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': csrf ? csrf() : '' },
        body: JSON.stringify(body),
        credentials: 'same-origin',
        signal,
      };
    return readAnswer(await root.fetch(url, init));
  }

  const REFUSALS = new Set(['rebuild_required', 'lists_unavailable', 'lists_term_mismatch', 'not_found', 'forbidden']);

  // What went wrong, in the page's words: a refusal of this run (no Try
  // again), or a failure of this attempt (Try again).
  function problem(result, error) {
    if (error) {
      return { code: 'network', refused: false, content: () => copy().fragment(error instanceof TypeError ? 'error-network' : 'error-load') };
    }
    const { status, data } = result;
    let code = String(data?.code || data?.error_code || '');
    if (status === 404 || code === 'run_not_found') code = 'not_found';
    if (status === 403 && !code) code = 'forbidden';
    const known = {
      audit_unavailable: 'error-audit', roster_busy: 'error-busy', session: 'error-session', throttled: 'error-throttled',
      not_found: 'error-not_found', rebuild_required: 'error-rebuild_required', lists_unavailable: 'error-lists_unavailable',
      forbidden: 'error-forbidden',
    };
    let content;
    if (code === 'lists_term_mismatch' && Array.isArray(data.live_term) && Array.isArray(data.saved_term)) {
      const term = ([year, number]) => copy().fragment('term', { year: ltr(year), term: ltr(number) });
      content = () => copy().fragment('error-lists_term_mismatch', { live: term(data.live_term), saved: term(data.saved_term) });
    } else {
      const key = known[code] || 'error-load';
      content = () => copy().fragment(key);
    }
    return { code: code || 'server', refused: REFUSALS.has(code), content };
  }

  // ── Provenance ──────────────────────────────────────────────

  function sourceLine(run) {
    const label = bdi(run?.label || '', '');
    return copy().fragment('source-run', { id: ltr(run?.id ?? ''), label, saved: ltr(savedAt(run?.saved_at || run?.created_at)) });
  }

  // "✓ Lists match…" or "≠ Sections changed since saving: n": a mark and
  // words. The whole run's check, or a scope's own count of changed sections.
  function checkLine(check, scopeChanged = null) {
    const changed = scopeChanged ?? (Number(check?.sections_changed || 0) + Number(check?.sections_new || 0)
      + Number(check?.sections_gone || 0) + Number(check?.program_mix_changed || 0));
    const status = scopeChanged === null ? (check?.status === 'matches' ? 'matches' : 'changed') : (scopeChanged ? 'changed' : 'matches');
    const node = el('span', { class: `et-check-mark-line is-${status}`, 'data-check': status },
      el('span', { class: 'et-check-glyph', 'aria-hidden': 'true' }, status === 'matches' ? '✓' : '≠'), ' ',
      el('span', {}, status === 'matches' ? copy().fragment('check-matches')
        : copy().fragment('check-changed', { n: count(changed || (check?.changed?.length ?? 0)) })));
    return node;
  }

  // ── The roster ──────────────────────────────────────────────

  // A section group: a section's own facts carry `gender`, a row's `group`.
  const groupKey = item => `${item.exam}|${item.section_key}|${item.gender ?? item.group}`;

  const COLUMNS = [
    { key: 'number', label: 'col-number', sort: null },
    { key: 'id', label: 'col-id', sort: 'id' },
    { key: 'name', label: 'col-name', sort: 'name' },
    { key: 'program', label: 'col-program', sort: 'program' },
    { key: 'section', label: 'col-section', sort: 'section' },
    { key: 'room', label: 'col-room', sort: 'room' },
    { key: 'flags', label: 'col-flags', sort: 'flags' },
  ];
  const BASIS_RANK = { whole: 0, split: 0, no_seat: 1, unassigned: 2, not_scheduled: 3 };

  function schedule(callback) {
    if (typeof TIMING.schedule === 'function') return TIMING.schedule(callback);
    if (typeof root.requestIdleCallback === 'function') return root.requestIdleCallback(callback, { timeout: 250 });
    return root.setTimeout(callback, 16);
  }

  // Tablist and radiogroup keys: arrows (mirrored right to left), Home, End.
  function rovingKeys(event, items, current, { vertical = true } = {}) {
    const rtl = doc.documentElement.dir === 'rtl';
    const index = items.indexOf(current);
    if (index < 0) return null;
    const forward = rtl ? 'ArrowLeft' : 'ArrowRight';
    const back = rtl ? 'ArrowRight' : 'ArrowLeft';
    let next = null;
    if (event.key === forward || (vertical && event.key === 'ArrowDown')) next = items[(index + 1) % items.length];
    else if (event.key === back || (vertical && event.key === 'ArrowUp')) next = items[(index - 1 + items.length) % items.length];
    else if (event.key === 'Home') next = items[0];
    else if (event.key === 'End') next = items[items.length - 1];
    if (next) event.preventDefault();
    return next;
  }

  /*
   * createRoster(container, options): the roster in a container.
   *   idPrefix      unique id stem inside the page
   *   programFilter show the program select (the page)
   *   examHref      code -> the other exam's list, for flag links
   *   onOpenExam    code -> open that exam's list here (a plain click)
   *   announce      text -> the container's polite live region
   *   onChange      ({ section, flag }) -> the page keeps its address
   */
  function createRoster(container, options = {}) {
    const words = copy();
    const prefix = options.idPrefix || 'examRoster';
    const collator = new Intl.Collator(words.locale === 'ar' ? 'ar' : 'en', { numeric: true, sensitivity: 'base' });
    const onChange = options.onChange || (() => {});
    const announce = options.announce || (() => {});

    let answer = null;
    let scopeLabel = () => '';
    let sections = new Map();
    let state = { section: 'all', flag: 'all', program: '', text: '', sort: null };
    let swapped = null; // an overflow section shown in the strip
    let renderToken = 0;
    let loadingTimer = 0;
    let filterTimer = 0;
    let announceTimer = 0;

    const tabs = el('div', { class: 'et-section-tabs', role: 'tablist', 'aria-label': words.raw('tabs-label') });
    const more = el('select', { class: 'form-select et-section-more', 'aria-label': words.raw('more-sections-label') });
    const tabRow = el('div', { class: 'et-section-strip' }, tabs, more);
    const chips = el('div', { class: 'et-flag-chips', role: 'radiogroup', 'aria-label': words.raw('chips-label') });
    const program = options.programFilter
      ? el('select', { class: 'form-select et-roster-program', 'aria-label': words.raw('program-label') }) : null;
    const filter = el('input', {
      type: 'search', class: 'form-control et-roster-filter', autocomplete: 'off', spellcheck: 'false',
      'aria-label': words.raw('filter'), placeholder: words.raw('filter'),
    });
    const tools = el('div', { class: 'et-roster-tools' }, chips, el('div', { class: 'et-roster-filters' }, program, filter));
    const countLine = el('p', { class: 'et-roster-count', id: `${prefix}Count` });
    const status = el('div', { class: 'et-roster-status' });
    const caption = el('caption', { class: 'visually-hidden' });
    const headRow = el('tr', { role: 'row' });
    const thead = el('thead', { role: 'rowgroup' }, headRow);
    const table = el('table', { class: 'et-roster-table', role: 'table', 'aria-describedby': `${prefix}Count` }, caption, thead);
    const empty = el('div', { class: 'et-roster-empty', hidden: true });
    const panel = el('div', { class: 'et-roster-panel', id: `${prefix}Panel` }, table, empty);
    const legend = el('p', { class: 'et-roster-legend' }, words.fragment('legend'));
    const body = el('div', { class: 'et-roster', hidden: true }, tabRow, tools, countLine, panel, legend);
    container.replaceChildren(status, body);

    COLUMNS.forEach(column => {
      const th = el('th', { scope: 'col', role: 'columnheader', class: `et-col-${column.key}` });
      if (column.sort) {
        th.setAttribute('aria-sort', 'none');
        th.append(el('button', { type: 'button', class: 'et-sort', 'data-sort': column.sort },
          words.raw(column.label), el('span', { class: 'et-sort-mark', 'aria-hidden': 'true' })));
      } else {
        th.append(words.raw(column.label));
      }
      headRow.append(th);
    });

    // ── Filters ──

    function inSection(row) {
      return state.section === 'all' || groupKey(row) === state.section;
    }

    function hasFlag(row, flag) {
      if (flag === 'clash') return Boolean(row.clash);
      if (flag === 'same_day') return Boolean(row.same_day);
      if (flag === 'no_seat') return row.room_basis === 'no_seat';
      if (flag === 'not_recorded') return row.section_status !== 'mapped';
      return true;
    }

    function matchesText(row, query) {
      if (!query) return true;
      const compact = query.replace(/ /g, '');
      if (/^\d+$/.test(compact)) return String(row.student_id).startsWith(compact);
      const name = fold(row.name);
      return query.split(' ').every(word => name.includes(word));
    }

    // Rows of the chosen section (and program): what the chips count.
    function base() {
      return (answer?.rows || []).filter(row => inSection(row) && (!state.program || row.program === state.program));
    }

    function visibleRows() {
      const query = fold(state.text);
      const rows = base().map((row, index) => ({ row, index })).filter(({ row }) => hasFlag(row, state.flag) && matchesText(row, query));
      if (state.sort) {
        const { key, dir } = state.sort;
        const flagScore = row => (row.clash ? 4 : 0) + (row.same_day ? 2 : 0) + (row.room_basis === 'no_seat' || row.section_status !== 'mapped' ? 1 : 0);
        const compare = {
          id: (a, b) => a.student_id - b.student_id,
          name: (a, b) => collator.compare(a.name || '', b.name || ''),
          program: (a, b) => collator.compare(a.program || '', b.program || ''),
          section: (a, b) => collator.compare(sectionName(a), sectionName(b)),
          room: (a, b) => (BASIS_RANK[a.room_basis] ?? 9) - (BASIS_RANK[b.room_basis] ?? 9) || collator.compare(a.room || '', b.room || ''),
          flags: (a, b) => flagScore(b) - flagScore(a),
        }[key];
        rows.sort((a, b) => (compare(a.row, b.row) * (dir === 'descending' ? -1 : 1)) || a.row.student_id - b.row.student_id || a.index - b.index);
      }
      return rows.map(({ row }) => row);
    }

    // ── Section tabs ──

    function tabLabel(section, multiExam) {
      const name = sectionName(section);
      return multiExam ? `${section.exam} ${name}` : name;
    }

    function renderTabs() {
      const list = [...sections.values()];
      const multiExam = new Set(list.map(section => section.exam)).size > 1;
      tabRow.hidden = list.length <= 1;
      if (tabRow.hidden) {
        panel.removeAttribute('role');
        panel.removeAttribute('aria-labelledby');
        return;
      }
      const all = { key: 'all', label: words.raw('tab-all'), count: (answer?.rows || []).length };
      const items = [all, ...list.map(section => ({
        key: groupKey(section), label: tabLabel(section, multiExam), count: section.rows, section,
      }))];
      let shown = items.slice(0, VISIBLE_TABS);
      let rest = items.slice(VISIBLE_TABS);
      if (swapped && rest.some(item => item.key === swapped)) {
        const pick = rest.find(item => item.key === swapped);
        rest = [shown[shown.length - 1], ...rest.filter(item => item !== pick)];
        shown = [...shown.slice(0, -1), pick];
      }
      tabs.replaceChildren(...shown.map((item, index) => {
        const selected = item.key === state.section;
        const membership = item.section?.membership;
        const marker = membership && membership !== 'matches' ? membership : null;
        const tab = el('button', {
          type: 'button', role: 'tab', id: `${prefix}Tab${index}`, class: 'et-section-tab',
          'aria-selected': String(selected), 'aria-controls': `${prefix}Panel`, tabindex: selected ? '0' : '-1',
          'data-section': item.key,
        }, el('span', { class: 'et-tab-label' }, item.section?.section_status === 'mapped' ? bdi(item.label) : item.label),
        ' ', el('span', { class: 'et-tab-count' }, bdi(NUMBER.format(item.count || 0))));
        if (marker) {
          tab.classList.add('is-changed');
          tab.append(el('span', { class: 'et-tab-dot', 'aria-hidden': 'true' }), visuallyHidden(` ${words.raw(`marker-${marker}`)}`));
        }
        if (item.section && item.section.section_status !== 'mapped') tab.classList.add('is-unrecorded');
        return tab;
      }));
      const selectedTab = tabs.querySelector('[aria-selected="true"]');
      if (selectedTab) {
        panel.setAttribute('role', 'tabpanel');
        panel.setAttribute('aria-labelledby', selectedTab.id);
      }
      more.hidden = !rest.length;
      more.replaceChildren(
        el('option', { value: '' }, words.text('more-sections', { n: NUMBER.format(rest.length) })),
        ...rest.map(item => el('option', { value: item.key }, `${item.label} · ${NUMBER.format(item.count || 0)}`)),
      );
      more.value = '';
    }

    // ── Chips and programs ──

    function renderChips() {
      const rows = base();
      const counts = Object.fromEntries(FLAGS.map(flag => [flag, rows.filter(row => hasFlag(row, flag)).length]));
      const shown = FLAGS.filter(flag => flag !== 'not_recorded' || counts[flag] || state.flag === flag);
      chips.replaceChildren(...shown.map(flag => {
        const checked = state.flag === flag;
        return el('button', {
          type: 'button', role: 'radio', class: `et-flag-chip is-${flag}`, 'data-flag': flag,
          'aria-checked': String(checked), tabindex: checked ? '0' : '-1',
        }, el('span', {}, words.fragment(`chip-${flag}`, { n: count(counts[flag]) })));
      }));
      if (program) {
        const programs = new Map();
        (answer?.rows || []).filter(inSection).forEach(row => programs.set(row.program, (programs.get(row.program) || 0) + 1));
        const names = [...programs.keys()].sort(collator.compare);
        if (state.program && !programs.has(state.program)) state.program = '';
        program.replaceChildren(
          el('option', { value: '' }, words.raw('program-all')),
          ...names.map(name => el('option', { value: name }, words.text('program-option', { program: name, n: NUMBER.format(programs.get(name)) }))),
        );
        program.value = state.program;
        program.hidden = names.length <= 1;
      }
    }

    // ── Table ──

    function roomCell(row) {
      const cell = el('td', { role: 'cell', class: 'et-col-room' });
      if (SEATED.has(row.room_basis) && row.room) {
        cell.append(bdi(row.room));
        const section = sections.get(groupKey(row));
        const parts = section?.parts?.length || 0;
        if (parts > 1 && row.part) cell.append(' ', el('small', { class: 'et-room-part' }, words.fragment('part-of', { part: ltr(row.part), parts: ltr(parts) })));
      } else if (row.room_basis === 'no_seat') {
        cell.append(el('span', { class: 'et-room-noseat' }, words.raw('no-seat')));
      } else if (row.room_basis === 'not_scheduled') {
        cell.append(el('span', { class: 'et-room-none' }, words.raw('not-scheduled')));
      } else {
        cell.append(el('span', { class: 'et-room-none' }, words.raw('not-assigned')));
      }
      return cell;
    }

    function flagLink(kind, code) {
      const exam = answer?.exams?.[code];
      const full = words.text(`flag-${kind}-full`, { code, slot: exam ? slotText(exam.day, exam.period) : '' });
      const href = code && options.examHref ? options.examHref(code) : null;
      return el(href ? 'a' : 'span', {
        class: `et-flag et-flag--${kind === 'same_day' ? 'sameday' : kind}`, href, 'data-exam': href ? code : null,
      }, el('span', { class: 'et-flag-icon', 'aria-hidden': 'true' }), words.fragment(`flag-${kind}`, { code: ltr(code) }), visuallyHidden(` — ${full}`));
    }

    function flagWord(kind, cls) {
      return el('span', { class: `et-flag et-flag--${cls}` }, el('span', { class: 'et-flag-icon', 'aria-hidden': 'true' }),
        kind === 'no_seat' ? words.raw('no-seat') : words.raw('section-missing'),
        visuallyHidden(` — ${words.raw(`flag-${kind}-full`)}`));
    }

    function flagsCell(row) {
      const badges = [
        ...(row.clash_with?.length ? row.clash_with.map(code => flagLink('clash', code)) : row.clash ? [flagLink('clash', '')] : []),
        ...(row.same_day_with?.length ? row.same_day_with.map(code => flagLink('same_day', code)) : row.same_day ? [flagLink('same_day', '')] : []),
        ...(row.room_basis === 'no_seat' ? [flagWord('no_seat', 'noseat')] : []),
        ...(row.section_status !== 'mapped' ? [flagWord('not_recorded', 'unrecorded')] : []),
      ];
      const cell = el('td', { role: 'cell', class: 'et-col-flags' }, badges.slice(0, 2));
      if (badges.length > 2) {
        const rest = badges.slice(2);
        cell.append(el('span', { class: 'et-flag et-flag--more' },
          el('span', { 'aria-hidden': 'true' }, words.text('flag-more', { n: NUMBER.format(rest.length) })),
          visuallyHidden(rest.map(badge => badge.textContent).join('; '))));
      }
      return cell;
    }

    function studentRow(row, number, multiExam) {
      const tr = el('tr', { role: 'row', class: `et-roster-row${row.clash ? ' is-clash' : ''}` });
      const section = el('td', { role: 'cell', class: 'et-col-section' });
      if (multiExam) section.append(bdi(row.exam), ' ');
      section.append(row.section_status === 'mapped' && row.section ? bdi(row.section) : el('span', { class: 'et-muted-dash' }, '—'));
      if (row.change) section.append(' ', el('span', { class: 'et-change-marker' }, el('span', { class: 'et-tab-dot', 'aria-hidden': 'true' }), words.raw(`marker-${row.change}`)));
      tr.append(
        el('td', { role: 'cell', class: 'et-col-number' }, NUMBER.format(number)),
        el('td', { role: 'cell', class: 'et-col-id' }, bdi(row.student_id)),
        el('td', { role: 'cell', class: 'et-col-name' }, nameNode(row.name),
          el('span', { class: 'et-roster-sub' }, bdi(row.program))),
        el('td', { role: 'cell', class: 'et-col-program' }, bdi(row.program)),
        section,
        roomCell(row),
        flagsCell(row),
      );
      return tr;
    }

    // The facts of one section part, from the scope's own rows. What is true
    // of the whole section (split, changed, not recorded) is said once, on
    // its first group shown.
    function groupHeading(row, multiExam, first) {
      const section = sections.get(groupKey(row));
      const name = sectionName(section || row);
      const parts = section?.parts || [];
      const part = row.part ? parts[row.part - 1] : null;
      const facts = [];
      facts.push(multiExam ? join([bdi(row.exam), row.section_status === 'mapped' ? bdi(name) : name], ' ') : row.section_status === 'mapped' ? bdi(name) : name);
      if (SEATED.has(row.room_basis) && part?.room) {
        const place = el('span', {}, bdi(part.room));
        if (parts.length > 1) place.append(' (', words.fragment('part-of', { part: ltr(row.part), parts: ltr(parts.length) }), ')');
        facts.push(place);
        facts.push(words.fragment('students', { n: count(part.rows) }));
        // A range reads left to right as one run, in either language.
        if (part.first_id !== null && part.first_id !== undefined) facts.push(words.fragment('ids', { range: ltr(`${part.first_id}–${part.last_id}`) }));
      } else {
        const inGroup = (answer?.rows || []).filter(item => groupKey(item) === groupKey(row) && item.room_basis === row.room_basis);
        facts.push(el('span', { class: row.room_basis === 'no_seat' ? 'et-room-noseat' : 'et-room-none' },
          words.raw(row.room_basis === 'no_seat' ? 'no-seat' : row.room_basis === 'not_scheduled' ? 'not-scheduled' : 'not-assigned')));
        facts.push(words.fragment('students', { n: count(inGroup.length) }));
      }
      const notes = [];
      if (section && first && row.room_basis === 'split' && parts.length > 1) {
        const ranges = join(parts.filter(item => item.room && item.first_id !== null && item.first_id !== undefined)
          .map(item => words.fragment('split-range', {
            range: ltr(`${item.first_id}–${item.last_id}`), first: ltr(item.first_id), last: ltr(item.last_id), room: ltr(item.room),
          })), ', ');
        notes.push(words.fragment('split', { n: count(parts.filter(item => item.room).length), ranges }));
      }
      if (section && first) {
        const label = section.section_status === 'mapped' ? ltr(name) : name;
        if (section.membership === 'changed') notes.push(words.fragment('changed-note', { section: label, saved: count(section.saved), now: count(section.now) }));
        else if (section.membership === 'new') notes.push(words.fragment('new-note', { section: label }));
        if (section.program_mix && section.program_mix !== 'matches' && section.membership === 'matches') notes.push(words.fragment('mix-note', { section: label }));
      }
      if (row.room_basis === 'no_seat') notes.push(words.fragment('no-seat-note'));
      if (row.section_status !== 'mapped' && first) notes.push(words.fragment('unrecorded-note'));
      else if (row.room_basis === 'unassigned' && first) notes.push(words.fragment('unassigned-note'));
      const th = el('th', { scope: 'rowgroup', role: 'rowheader', colspan: String(COLUMNS.length), class: 'et-roster-group-head' },
        el('span', { class: 'et-group-facts' }, join(facts)),
        ...notes.flatMap(note => [' ', el('span', { class: 'et-group-note' }, note)]));
      if (section?.membership && section.membership !== 'matches' && first) th.classList.add('is-changed');
      return el('tr', { role: 'row', class: 'et-roster-group-row' }, th);
    }

    function goneHeadings(multiExam) {
      // A section gone since saving has no rows; it still says so.
      return [...sections.values()].filter(section => section.membership === 'gone' && !section.rows
        && (state.section === 'all' || groupKey(section) === state.section) && state.flag === 'all' && !state.text && !state.program)
        .map(section => {
          const name = sectionName(section);
          return el('tbody', { role: 'rowgroup', class: 'et-roster-group is-gone' }, el('tr', { role: 'row', class: 'et-roster-group-row' },
            el('th', { scope: 'rowgroup', role: 'rowheader', colspan: String(COLUMNS.length), class: 'et-roster-group-head is-changed' },
              el('span', { class: 'et-group-facts' }, multiExam ? join([bdi(section.exam), name], ' ') : name), ' ',
              el('span', { class: 'et-group-note' }, words.fragment('gone-note', { section: section.section_status === 'mapped' ? ltr(name) : name, saved: count(section.saved) })))));
        });
    }

    function renderTable() {
      const token = ++renderToken;
      const rows = visibleRows();
      const total = base().length;
      const multiExam = new Set((answer?.rows || []).map(row => row.exam)).size > 1;
      const grouped = !state.sort;
      table.querySelectorAll('tbody').forEach(node => node.remove());
      table.removeAttribute('aria-busy');
      body.classList.remove('is-stale');
      headRow.querySelectorAll('th[aria-sort]').forEach(th => {
        const key = th.querySelector('button')?.dataset.sort;
        th.setAttribute('aria-sort', state.sort?.key === key ? state.sort.dir : 'none');
      });
      const scopeName = scopeText();
      const sectionTab = state.section !== 'all' && sections.get(state.section);
      caption.textContent = words.text('caption', {
        scope: sectionTab ? `${scopeName} ${tabLabel(sectionTab, multiExam)}` : scopeName,
        n: NUMBER.format(rows.length),
        order: state.sort
          ? words.text('order-column', { column: words.raw(COLUMNS.find(column => column.sort === state.sort.key).label) })
          : words.raw('order-seat'),
      });
      goneHeadings(multiExam).forEach(node => table.append(node));
      renderEmpty(rows);
      let index = 0;
      let group = null;
      let groupId = null;
      const said = new Set();
      const step = () => {
        if (token !== renderToken) return;
        const end = Math.min(rows.length, index + TIMING.batch);
        for (; index < end; index++) {
          const row = rows[index];
          const id = grouped ? `${groupKey(row)}|${row.room_basis}|${row.part ?? ''}` : 'all';
          if (!group || id !== groupId) {
            group = el('tbody', { role: 'rowgroup', class: 'et-roster-group' });
            if (grouped) group.append(groupHeading(row, multiExam, !said.has(groupKey(row))));
            said.add(groupKey(row));
            table.append(group);
            groupId = id;
          }
          group.append(studentRow(row, index + 1, multiExam));
        }
        countLine.replaceChildren(words.fragment(index < rows.length ? 'showing-more' : 'showing', { shown: count(index), total: count(total) }));
        if (index < rows.length) schedule(step);
      };
      step();
    }

    function renderEmpty(rows) {
      empty.replaceChildren();
      empty.classList.remove('is-good');
      empty.hidden = rows.length > 0;
      table.hidden = rows.length === 0 && !table.querySelector('tbody');
      if (rows.length) return;
      if (!(answer?.rows || []).length) {
        empty.append(el('p', {}, words.fragment('empty-scope')));
        return;
      }
      if (state.flag !== 'all' && !state.text && !state.program) {
        // Good news, said as such.
        empty.classList.add('is-good');
        empty.append(el('p', {}, el('span', { class: 'et-good-mark', 'aria-hidden': 'true' }, '✓'), ' ',
          words.fragment(`empty-good-${state.flag}`, { scope: scopeNode() })));
        return;
      }
      const clear = el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary et-roster-clear' }, words.raw('clear-filters'));
      empty.append(el('p', {}, words.fragment('empty-filter')), clear);
    }

    function render({ tabsToo = true } = {}) {
      if (tabsToo) renderTabs();
      renderChips();
      renderTable();
    }

    function announceCount() {
      clearTimeout(announceTimer);
      announceTimer = root.setTimeout(() => announce(words.text('showing', { shown: NUMBER.format(visibleRows().length), total: NUMBER.format(base().length) })), TIMING.announceDelay);
    }

    function changed() {
      onChange({ section: state.section, flag: state.flag });
    }

    // The scope's name for captions (text) and good news (a node).
    function scopeNode() {
      const value = scopeLabel();
      return value instanceof root.Node ? value : String(value ?? '');
    }
    const scopeText = () => {
      const value = scopeNode();
      return typeof value === 'string' ? value : value.textContent;
    };

    // ── Events ──

    tabs.addEventListener('click', event => {
      const tab = event.target.closest('[role="tab"]');
      if (!tab || tab.dataset.section === state.section) return;
      state.section = tab.dataset.section;
      render();
      tabs.querySelector('[aria-selected="true"]')?.focus();
      announceCount();
      changed();
    });
    tabs.addEventListener('keydown', event => {
      const items = [...tabs.querySelectorAll('[role="tab"]')];
      const next = rovingKeys(event, items, event.target.closest('[role="tab"]'), { vertical: false });
      if (next) next.click();
    });
    more.addEventListener('change', () => {
      if (!more.value) return;
      swapped = more.value;
      state.section = more.value;
      render();
      tabs.querySelector('[aria-selected="true"]')?.focus();
      announceCount();
      changed();
    });
    chips.addEventListener('click', event => {
      const chip = event.target.closest('[role="radio"]');
      if (!chip || chip.dataset.flag === state.flag) return;
      state.flag = chip.dataset.flag;
      render({ tabsToo: false });
      chips.querySelector('[aria-checked="true"]')?.focus();
      announceCount();
      changed();
    });
    chips.addEventListener('keydown', event => {
      const items = [...chips.querySelectorAll('[role="radio"]')];
      const next = rovingKeys(event, items, event.target.closest('[role="radio"]'));
      if (next) next.click();
    });
    program?.addEventListener('change', () => {
      state.program = program.value;
      render({ tabsToo: false });
      announceCount();
    });
    filter.addEventListener('input', () => {
      clearTimeout(filterTimer);
      filterTimer = root.setTimeout(() => {
        state.text = filter.value;
        renderTable();
        announceCount();
      }, TIMING.filterDelay);
    });
    filter.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || !filter.value) return;
      // Esc clears the filter first; a second Esc belongs to the container.
      event.preventDefault();
      event.stopPropagation();
      filter.value = '';
      state.text = '';
      renderTable();
      announceCount();
    });
    headRow.addEventListener('click', event => {
      const button = event.target.closest('button[data-sort]');
      if (!button) return;
      const key = button.dataset.sort;
      state.sort = state.sort?.key === key
        ? (state.sort.dir === 'ascending' ? { key, dir: 'descending' } : null)
        : { key, dir: 'ascending' };
      renderTable();
      headRow.querySelector(`button[data-sort="${key}"]`)?.focus();
    });
    empty.addEventListener('click', event => {
      if (!event.target.closest('.et-roster-clear')) return;
      state = { ...state, flag: 'all', program: '', text: '' };
      filter.value = '';
      render({ tabsToo: false });
      chips.querySelector('[aria-checked="true"]')?.focus();
      announceCount();
      changed();
    });
    panel.addEventListener('click', event => {
      const link = event.target.closest('a[data-exam]');
      if (!link || !options.onOpenExam) return;
      // A plain click opens the other list here; a modified click keeps the
      // browser's own behaviour (a new tab).
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      options.onOpenExam(link.dataset.exam);
    });

    // ── API ──

    function clearTimers() {
      clearTimeout(loadingTimer);
      clearTimeout(filterTimer);
      loadingTimer = 0;
      renderToken++;
    }

    return {
      // Show a scope answer. The section and flag come from the address or
      // the drawer's own history; sort and the text filter start fresh.
      show(next, { scope = () => '', section = 'all', flag = 'all', keepText = false } = {}) {
        clearTimers();
        answer = next;
        scopeLabel = typeof scope === 'function' ? scope : () => scope;
        sections = new Map((next?.sections || []).map(item => [groupKey(item), item]));
        swapped = null;
        state = {
          section: section !== 'all' && sections.has(section) ? section : 'all',
          flag: FLAGS.includes(flag) ? flag : 'all',
          program: '', text: keepText ? state.text : '', sort: null,
        };
        if (!keepText) filter.value = '';
        const hiddenTab = [...sections.keys()].indexOf(state.section) >= VISIBLE_TABS - 1;
        if (hiddenTab) swapped = state.section;
        status.replaceChildren();
        body.hidden = false;
        render();
      },
      loading({ students = null } = {}) {
        clearTimers();
        status.replaceChildren();
        if (!body.hidden && answer) {
          // The previous list stays, dimmed, until the next one arrives.
          body.classList.add('is-stale');
          table.setAttribute('aria-busy', 'true');
        } else {
          body.hidden = true;
          status.append(el('div', { class: 'et-roster-skeleton', 'aria-busy': 'true' },
            ...Array.from({ length: 8 }, () => el('span', { class: 'et-skeleton-bar' }))));
        }
        loadingTimer = root.setTimeout(() => {
          status.prepend(el('p', { class: 'et-roster-loading', role: 'status' },
            students ? words.fragment('loading-n', { n: count(students) }) : words.fragment('loading')));
        }, TIMING.loadingNotice);
      },
      failed({ content, retry = null }) {
        clearTimers();
        answer = null;
        body.hidden = true;
        body.classList.remove('is-stale');
        const alert = el('div', { class: 'alert alert-danger et-roster-error', role: 'alert' }, el('span', {}, content()));
        if (retry) {
          const button = el('button', { type: 'button', class: 'btn btn-sm btn-outline-secondary' }, words.raw('try-again'));
          button.addEventListener('click', retry);
          alert.append(button);
        }
        status.replaceChildren(alert);
      },
      clear() {
        clearTimers();
        answer = null;
        body.hidden = true;
        status.replaceChildren();
      },
      selection() {
        const section = state.section !== 'all' ? sections.get(state.section) : null;
        return { section: state.section, flag: state.flag, group: section ? { exam: section.exam, section_key: section.section_key, gender: section.gender } : null };
      },
      shown: () => Boolean(answer),
      answer: () => answer,
      focusTabs() {
        (tabs.querySelector('[aria-selected="true"]') || chips.querySelector('[aria-checked="true"]'))?.focus();
      },
      element: body,
    };
  }

  root.ExamRoster = Object.freeze({
    copy, ltr, count, bdi, nameNode, el, join, visuallyHidden, fold, slot, slotText, savedAt, clock,
    groupWord, sectionName, send, readAnswer, problem, sourceLine, checkLine, createRoster, rovingKeys,
    groupKey, NUMBER,
  });
})(window);
