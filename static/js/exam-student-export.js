/*
 * Exam student data export: the "Student data" dialog of the exam timetable.
 *
 * It exports the saved timetable on the board through the preflight and
 * export endpoints of core/exam_student_export_views.py. The page script lends
 * it the saved run and its guards (window.examStudentExportHost); nothing here
 * decides policy:
 *
 * - The pickers list the saved run's exams, sections, periods, rooms and days
 *   at once, then take the preflight's reading of the same run (which adds any
 *   section new since the save) when it answers.
 * - Counts and the Download label come from the preflight, re-run (debounced)
 *   on every option that changes them. Until it first answers, counts are the
 *   saved figures, prefixed "about", and Download waits with its reason said.
 *   One preflight is out at a time: options changed meanwhile are sent when
 *   it answers, so a burst of changes costs the server two rebuilds, not one
 *   per change. A figure is shown as current only while every option it
 *   depends on is the one it was priced with; otherwise it is dimmed while a
 *   new answer is on its way, and cleared when none is.
 * - Only timetable facts and options are sent, never a student ID. The file
 *   name comes from Content-Disposition and the reference from
 *   X-Export-Reference.
 * - Every sentence is rendered by the template (#examStudentExportCopy) in the
 *   page language; codes, times, counts and file names sit in <bdi dir="ltr">.
 */
(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const host = window.examStudentExportHost;
  const dialog = $('examStudentExportDialog');
  const copyNode = $('examStudentExportCopy');
  if (!host || !dialog || !copyNode) return;

  // busy: pauses before asking again while another export holds the server's
  // one export slot (the preflight never queues for it); then Try again.
  const TIMING = { debounce: 300, revoke: 1000, busy: [500, 1000, 2000, 3000, 4000, 5000], ...(window.__examStudentExportTiming || {}) };
  const STORE_KEY = 'exam-student-export';
  const GENDERS = ['M', 'F', 'U'];
  const SCOPES = ['section', 'course', 'room', 'period', 'day', 'all'];
  const OVERFLOW = 'OVERFLOW';
  const UNASSIGNED = 'UNASSIGNED';
  const FILE_TYPES = /^application\/(?:vnd\.openxmlformats-officedocument\.spreadsheetml\.sheet|zip)\b/i;
  const NUMBER = new Intl.NumberFormat('en-US');
  const natural = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });

  // ── Copy ────────────────────────────────────────────────────

  const LTR = Symbol('ltr');
  const ltr = value => ({ [LTR]: String(value) });
  const isolated = value => {
    const bdi = document.createElement('bdi');
    bdi.dir = 'ltr';
    bdi.textContent = value;
    return bdi;
  };
  const raw = key => copyNode.getAttribute(`data-${key}`) ?? '';

  // A copy template's {name} slots take text, a node, or ltr(...) for a code,
  // count, time or file name, which is isolated left-to-right. The result is
  // a fresh fragment each call: inserting a fragment empties it.
  function copy(key, values = {}) {
    const fragment = document.createDocumentFragment();
    raw(key).split(/(\{\w+\})/).forEach(part => {
      const slot = /^\{(\w+)\}$/.exec(part);
      if (!slot) {
        if (part) fragment.append(part);
        return;
      }
      const value = values[slot[1]];
      if (value instanceof Node) fragment.append(value);
      else if (value && typeof value === 'object' && LTR in value) fragment.append(isolated(value[LTR]));
      else fragment.append(String(value ?? ''));
    });
    return fragment;
  }

  // Plain text for a toast, where an isolated run keeps its order through
  // U+2066/U+2069 instead of a <bdi>.
  function copyText(key, values = {}) {
    const plain = Object.fromEntries(Object.entries(values).map(([name, value]) => [name,
      value && typeof value === 'object' && LTR in value ? `\u2066${value[LTR]}\u2069` : value]));
    return copy(key, plain).textContent;
  }

  const count = n => ltr(NUMBER.format(n));
  const rowsCopy = n => copy(n === 1 ? 'rows-one' : 'rows', { n: count(n) });

  // ── State ───────────────────────────────────────────────────

  let context = null;     // the saved run on the board when the dialog opened
  let opener = null;
  let facts = null;       // what the pickers offer
  let saved = null;       // the run's saved counts, "about" until the preflight answers
  let answer = null;      // the last preflight answer
  let answeredFor = null; // the options that answer's counts were priced for
  let choicesDigest = ''; // the server's digest of the choices the pickers hold
  let refusal = null;     // a gate refused this run: { code, live, saved }
  let failure = null;     // { content: () => node|text, retry, blocks, reason }
  let pending = false;    // a preflight for the current options is on its way
  let inFlight = false;   // a preflight is out; the next waits for its answer
  let slotWaits = 0;      // "another export is being prepared" answers in a row
  let busy = false;       // a download is on its way
  let stale = false;      // the board changed under the dialog
  let lastCheck = '';     // what the check panel says, so it is announced once
  let refusedDate = null; // the date input to focus once the options unlock
  let timer = 0;
  let preflightToken = 0;
  let downloadToken = 0;
  let preflightController = null;
  let downloadController = null;

  // ── Facts: the saved run, then the preflight's reading of it ─

  function examOrder(a, b) {
    return (a.scheduled === b.scheduled ? 0 : a.scheduled ? -1 : 1)
      || (Number(a.slot_index) - Number(b.slot_index))
      || natural.compare(a.code, b.code);
  }

  function factsFromRun(run) {
    const schedule = Array.isArray(run.schedule) ? run.schedule : [];
    const enrollment = run.section_enrollment && typeof run.section_enrollment === 'object' ? run.section_enrollment : {};
    const exams = schedule.map(entry => ({
      code: String(entry.course_code),
      name: String(entry.course_name || ''),
      scheduled: entry.day !== OVERFLOW,
      slot_index: entry.slot_index,
      day: entry.day,
      period: entry.period,
      sections: (Array.isArray(enrollment[entry.course_code]) ? enrollment[entry.course_code] : []).map(row => ({
        section_key: String(row.section_key),
        gender: row.gender,
        section: String(row.section ?? ''),
        mapping_status: row.mapping_status,
        membership: '',
      })),
    })).sort(examOrder);
    const periods = new Map();
    const rooms = new Map();
    schedule.filter(entry => entry.day !== OVERFLOW).forEach(entry => {
      periods.set(entry.slot_index, { slot_index: entry.slot_index, day: entry.day, period: entry.period });
      (Array.isArray(entry.rooms) ? entry.rooms : []).forEach(room => {
        if (!room || room.room_code === UNASSIGNED) return;
        rooms.set(`${entry.slot_index}\u001f${room.room_code}`, {
          slot_index: entry.slot_index, room_code: String(room.room_code), gender: room.gender,
        });
      });
    });
    // Days in slot order, as the export numbers them; OVERFLOW is never a day.
    const placed = [
      ...(Array.isArray(run.slots) ? run.slots.map(slot => [slot.index, slot.day]) : []),
      ...schedule.map(entry => [entry.slot_index, entry.day]),
    ].sort((a, b) => a[0] - b[0]);
    return {
      exams,
      periods: [...periods.values()].sort((a, b) => a.slot_index - b.slot_index),
      days: [...new Set(placed.map(([, day]) => day).filter(day => day && day !== OVERFLOW))],
      rooms: [...rooms.values()].sort((a, b) => a.slot_index - b.slot_index || natural.compare(a.room_code, b.room_code)),
      programs: null,
      departments: [],
      genders: GENDERS.filter(gender => exams.some(exam => exam.sections.some(section => section.gender === gender))),
    };
  }

  function factsFromChoices(choices) {
    return {
      exams: choices.exams.map(exam => ({
        code: exam.code, name: exam.name, scheduled: exam.scheduled, slot_index: exam.slot_index,
        day: exam.day, period: exam.period, sections: exam.sections.map(section => ({ ...section })),
      })),
      periods: choices.periods.map(({ slot_index, day, period }) => ({ slot_index, day, period })),
      days: choices.days.map(day => day.day),
      rooms: choices.rooms.map(({ slot_index, room_code, gender }) => ({ slot_index, room_code, gender })),
      programs: choices.programs.map(program => program.program),
      departments: choices.departments,
      genders: GENDERS.filter(gender => (choices.groups?.[gender] || 0) > 0 || facts.genders.includes(gender)),
    };
  }

  // What the run saved: every section group's student count at save.
  function savedCounts(run) {
    const figures = { all: 0, course: {}, section: {}, period: {}, day: {}, room: {} };
    const add = (bucket, key, n) => { bucket[key] = (bucket[key] || 0) + n; };
    const enrollment = run.section_enrollment || {};
    (run.schedule || []).forEach(entry => {
      (enrollment[entry.course_code] || []).forEach(row => {
        const n = Number(row.student_count) || 0;
        figures.all += n;
        add(figures.course, entry.course_code, n);
        add(figures.section, sectionValue(entry.course_code, row.section_key, row.gender), n);
        add(figures.period, entry.slot_index, n);
        add(figures.day, entry.day, n);
      });
      (entry.rooms || []).forEach(room => {
        if (!room || room.room_code === UNASSIGNED) return;
        (room.section_parts || []).forEach(part => {
          add(figures.room, `${entry.slot_index}\u001f${room.room_code}`, Number(part.student_count) || 0);
        });
      });
    });
    return figures;
  }

  // ── Form values ─────────────────────────────────────────────

  const sectionValue = (exam, key, gender) => JSON.stringify([exam, key, gender]);
  const checked = name => dialog.querySelector(`input[name="${name}"]:checked`)?.value || '';
  const scopeKind = () => checked('examStudentScope');
  const slotOf = select => (select.value === '' ? null : Number(select.value));
  const programBoxes = () => [...$('examStudentProgramList').querySelectorAll('input[type="checkbox"]')];
  const groupBoxes = () => [...dialog.querySelectorAll('input[name="examStudentGroup"]')]
    .filter(input => !input.closest('[hidden]'));
  const dateInputs = () => [...$('examStudentDates').querySelectorAll('input')];

  function scope(kind = scopeKind()) {
    if (kind === 'section') {
      const [exam, section_key, gender] = $('examStudentSection').value ? JSON.parse($('examStudentSection').value) : [];
      return { kind, exam: exam || '', section_key: section_key || '', gender: gender || '' };
    }
    if (kind === 'course') return { kind, exam: $('examStudentCourse').value };
    if (kind === 'room') return { kind, slot_index: slotOf($('examStudentRoomPeriod')), room_code: $('examStudentRoom').value };
    if (kind === 'period') return { kind, slot_index: slotOf($('examStudentPeriod')) };
    if (kind === 'day') return { kind, day: $('examStudentDay').value };
    return { kind: 'all' };
  }

  const complete = value => Object.values(value).every(field => field !== '' && field !== null && field !== undefined);

  // Every picker's value, so one preflight prices every scope choice.
  function pickers() {
    const section = scope('section');
    return {
      exam: $('examStudentCourse').value,
      section_key: section.section_key,
      gender: section.gender,
      slot_index: slotOf($('examStudentPeriod')),
      room_code: $('examStudentRoom').value,
      day: $('examStudentDay').value,
    };
  }

  // All ticked is sent as none: the server's "every programme".
  function selectedPrograms() {
    if (!facts?.programs) return [];
    const boxes = programBoxes();
    const chosen = boxes.filter(box => box.checked).map(box => box.value);
    return chosen.length === boxes.length ? [] : chosen;
  }

  function dates() {
    return Object.fromEntries(dateInputs().filter(input => input.value && input.validity.valid)
      .map(input => [input.dataset.day, input.value]));
  }

  function body({ forPreflight = false } = {}) {
    const value = {
      scope: scope(),
      programs: selectedPrograms(),
      groups: groupBoxes().filter(box => box.checked).map(box => box.value),
      one_file_per_group: $('examStudentOneFile').checked,
      rows: checked('examStudentRows'),
      contents: checked('examStudentContents'),
      language: checked('examStudentLanguage'),
      dates: dates(),
    };
    if (forPreflight) value.pickers = pickers();
    else value.prepared_for = $('examStudentPreparedFor').value.trim();
    return value;
  }

  const optionsOf = () => body({ forPreflight: true });
  const keyOf = value => JSON.stringify(value);

  // What each count was priced with. A scope's rows depend on the programs,
  // groups and rows and on that scope's own fields: the chosen scope's, or
  // the picker values every other scope is priced with.
  const SCOPE_FIELDS = {
    section: ['exam', 'section_key', 'gender'], course: ['exam'], room: ['slot_index', 'room_code'],
    period: ['slot_index'], day: ['day'], all: [],
  };
  function fieldsOf(options, kind) {
    const source = options.scope.kind === kind ? options.scope : options.pickers;
    return SCOPE_FIELDS[kind].map(name => source[name] ?? null);
  }

  // "Every program" travels as none, so none ticked must not pass for it.
  const noProgram = () => Boolean(facts?.programs) && !programBoxes().some(box => box.checked);

  function pricedWith(options, filters, kinds) {
    return Boolean(answeredFor)
      && !(filters.includes('programs') && noProgram())
      && filters.every(key => keyOf(answeredFor[key]) === keyOf(options[key]))
      && kinds.every(kind => keyOf(fieldsOf(answeredFor, kind)) === keyOf(fieldsOf(options, kind)));
  }

  const scopePriced = (kind, options) => pricedWith(options, ['programs', 'groups', 'rows'], [kind]);
  // Group counts are for the chosen scope whatever groups are ticked.
  const groupsPriced = options => answeredFor?.scope.kind === options.scope.kind
    && pricedWith(options, ['programs', 'rows'], [options.scope.kind]);

  // Why these options can be neither priced nor downloaded, before asking.
  function invalidReason() {
    if (!groupBoxes().some(box => box.checked)) return 'reason-groups';
    if (facts?.programs && !programBoxes().some(box => box.checked)) return 'reason-programs';
    if (!complete(scope())) return 'reason-scope';
    if (dateInputs().some(input => !input.validity.valid)) return 'reason-dates';
    return '';
  }

  const defaultFilters = () => !selectedPrograms().length && checked('examStudentRows') === 'all'
    && groupBoxes().every(box => box.checked);

  // ── Preferences: remembered per viewer, never required ──────

  function preferences() {
    try {
      const stored = JSON.parse(window.localStorage.getItem(STORE_KEY) || '{}');
      return stored && typeof stored === 'object' ? stored : {};
    } catch (_) {
      return {};
    }
  }

  function remember() {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify({
        scope: scopeKind(), contents: checked('examStudentContents'), language: checked('examStudentLanguage'),
      }));
    } catch (_) {
      // A private window or blocked storage: the defaults serve next time.
    }
  }

  function setRadio(name, value, fallback) {
    const inputs = [...dialog.querySelectorAll(`input[name="${name}"]`)];
    const target = inputs.find(input => input.value === value) || inputs.find(input => input.value === fallback);
    if (target) target.checked = true;
  }

  // ── Pickers ─────────────────────────────────────────────────

  const groupWord = gender => raw(`group-${String(gender).toLowerCase()}`);

  function examLabel(exam) {
    return `${exam.code}${exam.name ? ` · ${exam.name}` : ''}${exam.scheduled ? '' : ` · ${raw('not-scheduled')}`}`;
  }

  function sectionLabel(section) {
    let label = section.section;
    if (section.mapping_status === 'missing') label = `${raw('section-missing')} · ${groupWord(section.gender)}`;
    else if (section.mapping_status === 'ambiguous') label = `${raw('section-ambiguous')} · ${groupWord(section.gender)}`;
    if (section.membership === 'new') label += ` · ${raw('section-new')}`;
    if (section.membership === 'gone') label += ` · ${raw('section-gone')}`;
    return label;
  }

  function fill(select, items, valueOf, labelOf, wanted) {
    select.replaceChildren(...items.map(item => new Option(labelOf(item), valueOf(item))));
    if (!items.length) select.append(new Option(raw('no-choice'), ''));
    if (items.some(item => valueOf(item) === wanted)) select.value = wanted;
    select.disabled = !items.length;
  }

  function fillSections(wanted) {
    const exam = facts.exams.find(item => item.code === $('examStudentCourse').value);
    fill($('examStudentSection'), exam ? exam.sections : [],
      section => sectionValue(exam.code, section.section_key, section.gender), sectionLabel, wanted);
  }

  function fillRooms(wanted) {
    const slot = slotOf($('examStudentPeriod'));
    fill($('examStudentRoom'), facts.rooms.filter(room => room.slot_index === slot),
      room => room.room_code, room => `${room.room_code} · ${groupWord(room.gender)}`, wanted);
  }

  function renderPickers() {
    const keep = {
      exam: $('examStudentCourse').value, section: $('examStudentSection').value,
      slot: $('examStudentPeriod').value, room: $('examStudentRoom').value, day: $('examStudentDay').value,
    };
    [$('examStudentCourse'), $('examStudentSectionExam')].forEach(select => fill(select, facts.exams,
      exam => exam.code, examLabel, keep.exam));
    [$('examStudentPeriod'), $('examStudentRoomPeriod')].forEach(select => fill(select, facts.periods,
      period => String(period.slot_index), period => `${period.day} ${period.period}`, keep.slot));
    fill($('examStudentDay'), facts.days, day => day, day => day, keep.day);
    fillSections(keep.section);
    fillRooms(keep.room);
    renderPrograms();
    renderGroups();
  }

  function renderPrograms() {
    const list = $('examStudentProgramList');
    const all = $('examStudentAllPrograms');
    if (!facts.programs) {
      list.replaceChildren();
      $('examStudentDepartments').replaceChildren();
      all.checked = true;
      all.indeterminate = false;
      all.disabled = true;
      return;
    }
    if (list.childElementCount) return;
    all.disabled = false;
    list.replaceChildren(...facts.programs.map(program => {
      const label = document.createElement('label');
      label.className = 'et-export-check-option';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = program;
      box.checked = true;
      label.append(box, isolated(program));
      return label;
    }));
    const arabic = raw('locale').startsWith('ar');
    $('examStudentDepartments').replaceChildren(...facts.departments.filter(department => department.programs.length)
      .map(department => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'et-export-shortcut';
        button.dataset.programs = JSON.stringify(department.programs);
        button.setAttribute('aria-pressed', 'false');
        button.textContent = arabic ? department.name_ar : department.name_en;
        return button;
      }));
    syncPrograms();
  }

  // "All programs" and the department shortcuts say what is ticked.
  function syncPrograms() {
    const boxes = programBoxes();
    const chosen = boxes.filter(box => box.checked).map(box => box.value).sort();
    const all = $('examStudentAllPrograms');
    if (boxes.length) {
      all.checked = chosen.length === boxes.length;
      all.indeterminate = chosen.length > 0 && chosen.length < boxes.length;
    }
    $('examStudentDepartments').querySelectorAll('button').forEach(button => {
      const programs = JSON.parse(button.dataset.programs).sort();
      const exactly = programs.length === chosen.length && programs.every((program, i) => program === chosen[i]);
      button.setAttribute('aria-pressed', String(exactly));
    });
  }

  // A group that sits exams appears ticked; one that does not, is not offered.
  function renderGroups() {
    GENDERS.forEach(gender => {
      const label = dialog.querySelector(`[data-group="${gender}"]`);
      const present = facts.genders.includes(gender);
      if (label.hidden !== present) return;
      label.hidden = !present;
      label.querySelector('input').checked = present;
    });
  }

  function renderDates() {
    $('examStudentDates').replaceChildren(...facts.days.map(day => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'date';
      input.dataset.day = day;
      input.className = 'form-control form-control-sm';
      label.append(isolated(day), input);
      return label;
    }));
    $('examStudentDatesDetails').open = false;
  }

  // ── Rendering ───────────────────────────────────────────────

  function refusalContent() {
    const { code } = refusal;
    if (code === 'lists_term_mismatch' && Array.isArray(refusal.live) && Array.isArray(refusal.saved)) {
      const term = ([year, number]) => copy('term', { year: ltr(year), term: ltr(number) });
      return copy('refused-lists_term_mismatch', { live: term(refusal.live), saved: term(refusal.saved) });
    }
    return copy(['rebuild_required', 'lists_unavailable', 'not_found'].includes(code) ? `refused-${code}` : 'refused');
  }

  function sectionName(change) {
    if (change.mapping_status === 'missing') return raw('section-missing');
    if (change.mapping_status === 'ambiguous') return raw('section-ambiguous');
    return change.section;
  }

  function changeItems(check) {
    const items = check.changed.slice(0, 5).map(change => {
      const li = document.createElement('li');
      const values = { exam: ltr(change.exam), section: ltr(sectionName(change)) };
      if (change.membership === 'new') li.append(copy('change-new', { ...values, counts: ltr(`+${change.now}`) }));
      else if (change.membership === 'gone') li.append(copy('change-gone', { ...values, counts: ltr(`−${change.saved}`) }));
      else if (change.membership === 'changed') li.append(copy('change-item', { ...values, counts: ltr(`${change.saved} → ${change.now}`) }));
      else li.append(copy('change-mix', values));
      return li;
    });
    if (check.changed.length > 5) {
      const li = document.createElement('li');
      li.append(copy('change-more', { n: count(check.changed.length - 5) }));
      items.push(li);
    }
    if (check.exams_missing?.length) {
      const li = document.createElement('li');
      const exams = document.createDocumentFragment();
      check.exams_missing.forEach((exam, index) => exams.append(index ? ' · ' : '', isolated(exam)));
      li.append(copy('exams-missing', { exams }));
      items.push(li);
    }
    return items;
  }

  function checkState() {
    if (refusal) return 'refused';
    if (answer?.check) return answer.check.status === 'matches' ? 'matches' : 'changed';
    return failure ? 'unchecked' : 'checking';
  }

  function renderCheck() {
    const state = checkState();
    const check = answer?.check;
    // The panel is a polite live region: rewriting the same words would
    // announce them again on every option change, so only a change is said.
    const said = keyOf([state, ['matches', 'changed'].includes(state) ? check : null, state === 'refused' ? refusal : null]);
    if (said === lastCheck) return;
    lastCheck = said;
    $('examStudentExportCheck').dataset.state = state;
    $('examStudentExportCheckMark').textContent = { matches: '✓', changed: '≠', refused: '✕' }[state] || '';
    const detail = $('examStudentExportCheckDetail');
    const changes = $('examStudentExportChanges');
    const note = $('examStudentExportCheckNote');
    changes.replaceChildren();
    note.replaceChildren();
    if (state === 'refused') {
      $('examStudentExportCheckTitle').replaceChildren(copy('refused-title'));
      detail.replaceChildren(refusalContent());
    } else if (state === 'matches') {
      $('examStudentExportCheckTitle').replaceChildren(copy('matches-title'));
      detail.replaceChildren(copy('matches-detail', {
        matching: count(check.sections_matching), total: count(check.sections_total), code: ltr(check.lists_code_now),
      }));
    } else if (state === 'changed') {
      $('examStudentExportCheckTitle').replaceChildren(copy('changed-title'));
      detail.replaceChildren(copy('changed-detail', { changed: count(check.changed.length), total: count(check.sections_total) }));
      changes.append(...changeItems(check));
      if (check.no_seat > 0) note.append(copy(check.no_seat === 1 ? 'no-seat-one' : 'no-seat', { n: count(check.no_seat) }), ' ');
      note.append(copy('resize'));
    } else {
      $('examStudentExportCheckTitle').replaceChildren(copy(`${state}-title`));
      detail.replaceChildren(copy('checking-detail'));
    }
    changes.hidden = !changes.childElementCount;
    note.hidden = !note.childNodes.length;
  }

  // The saved figures, "about", until the first answer; then the preflight's,
  // for as long as they were priced with these options or new ones are coming.
  function scopeFigure(kind, options) {
    if (answer) {
      const n = answer.counts?.scope_rows?.[kind];
      return n === undefined || !(pending || scopePriced(kind, options)) ? null : rowsCopy(n);
    }
    if (!saved || !defaultFilters() || !complete(scope(kind))) return null;
    const value = scope(kind);
    const n = {
      all: saved.all,
      course: saved.course[value.exam],
      section: saved.section[sectionValue(value.exam, value.section_key, value.gender)],
      period: saved.period[value.slot_index],
      day: saved.day[value.day],
      room: saved.room[`${value.slot_index}\u001f${value.room_code}`],
    }[kind] || 0;
    return copy('about-rows', { n: count(n) });
  }

  // A count priced with other options is dimmed while its new one is on its
  // way and cleared when none is (options that cannot be priced, a failure).
  function renderCounts() {
    const options = optionsOf();
    const waiting = pending && Boolean(answer);
    SCOPES.forEach(kind => {
      const cell = dialog.querySelector(`[data-scope-count="${kind}"]`);
      cell.replaceChildren((!refusal && scopeFigure(kind, options)) || '');
      cell.classList.toggle('is-waiting', waiting && !scopePriced(kind, options));
    });
    const grouped = groupsPriced(options);
    GENDERS.forEach(gender => {
      const cell = dialog.querySelector(`[data-group-count="${gender}"]`);
      const n = answer?.counts?.groups?.[gender];
      cell.replaceChildren(!refusal && n !== undefined && (pending || grouped) ? rowsCopy(n) : '');
      cell.classList.toggle('is-waiting', waiting && !grouped);
    });
    $('examStudentExportScope').setAttribute('aria-busy', String(pending));
  }

  function blockReason() {
    if (stale) return 'reason-stale';
    if (busy) return 'reason-preparing';
    if (refusal) return 'refused';
    const invalid = invalidReason();
    if (invalid) return invalid;
    if (failure?.blocks) return failure.reason || 'reason-failed';
    if (pending && slotWaits) return 'reason-busy';
    if (!answer) return 'reason-checking';
    if (pending) return 'reason-updating';
    if (!answer.counts?.rows) return 'reason-empty';
    return '';
  }

  function renderFooter() {
    const reason = blockReason();
    const button = $('examStudentExportDownload');
    button.textContent = busy ? raw('preparing') : raw(answer?.check?.status === 'changed' ? 'download-changed' : 'download');
    button.setAttribute('aria-disabled', String(Boolean(reason)));
    button.setAttribute('aria-busy', String(busy));
    const why = $('examStudentExportReason');
    why.replaceChildren(reason === 'refused' ? refusalContent() : reason ? copy(reason) : '');
    why.hidden = !reason;
    dialog.querySelectorAll('.et-export-step').forEach(fieldset => { fieldset.disabled = busy; });

    // Files and rows for the options last priced: dimmed while re-pricing,
    // and gone once these options cannot be priced or pricing them failed.
    const current = !failure && !invalidReason() && Boolean(answeredFor) && keyOf(answeredFor) === keyOf(optionsOf());
    const files = !refusal && answer?.counts?.rows && (pending || current) ? answer.files || [] : [];
    $('examStudentExportSummary').replaceChildren(files.length
      ? copy('summary', { files: copy(`files-${Math.min(files.length, 3)}`), rows: rowsCopy(answer.counts.rows) }) : '');
    const list = $('examStudentExportFiles');
    list.replaceChildren(...files.map(file => {
      const li = document.createElement('li');
      const name = isolated(String(file.name).replace('<REF>', '…'));
      name.className = 'et-export-file-name';
      const rows = document.createElement('span');
      rows.className = 'et-export-file-rows';
      rows.append(rowsCopy(file.rows));
      li.append(name, rows);
      return li;
    }));
    list.hidden = !files.length;
    [$('examStudentExportSummary'), list].forEach(node => node.classList.toggle('is-waiting', pending));
    $('examStudentExportPrivacyText').replaceChildren(copy(checked('examStudentContents') === 'summary' ? 'privacy-summary' : 'privacy-full'));

    $('examStudentExportError').hidden = !failure;
    $('examStudentExportErrorText').replaceChildren(failure ? failure.content() : '');
    $('examStudentExportRetry').hidden = !failure?.retry;
  }

  function render() {
    renderCheck();
    renderCounts();
    renderFooter();
  }

  // ── Requests ────────────────────────────────────────────────

  const endpoint = suffix => `/ops/exam-timetable/${context.runId}/students/export/${suffix}`;
  const headers = () => ({ 'Content-Type': 'application/json', 'X-CSRFToken': host.csrf() });

  // A request only goes out, and an answer only counts, for the board the
  // dialog opened on.
  function isCurrent() {
    if (host.isCurrent(context)) return true;
    stale = true;
    abortAll();
    failure = { content: () => copy('reason-stale'), retry: null, blocks: true };
    render();
    return false;
  }

  function abortAll() {
    clearTimeout(timer);
    timer = 0;
    preflightToken++;
    downloadToken++;
    preflightController?.abort();
    downloadController?.abort();
    preflightController = null;
    downloadController = null;
    inFlight = false;
    slotWaits = 0;
    pending = false;
    busy = false;
  }

  // A refused date is marked on its own input, which says why through the
  // error text; any change of the options clears the mark.
  function markDate(day, focus) {
    const input = dateInputs().find(item => item.dataset.day === day);
    if (!input) return;
    $('examStudentDatesDetails').open = true;
    input.setAttribute('aria-invalid', 'true');
    input.setAttribute('aria-describedby', 'examStudentExportErrorText');
    // A download locks the options: the input takes focus once they unlock.
    if (focus) refusedDate = input;
  }

  function clearDateMark() {
    dateInputs().forEach(input => {
      input.removeAttribute('aria-invalid');
      input.removeAttribute('aria-describedby');
    });
  }

  // A refusal of the run, or a failure of this attempt.
  function problem(status, data, { retry, blocks, focus = false }) {
    const code = data.code || data.error_code || '';
    if (status === 404 || code === 'not_found' || code === 'run_not_found') {
      refusal = { code: 'not_found' };
      return;
    }
    if (status === 409) {
      refusal = { code, live: data.live_term, saved: data.saved_term };
      return;
    }
    if (code === 'invalid_options' && data.field === 'dates') {
      // Sending the same dates again cannot succeed: no Try again, and
      // Download waits until a date changes.
      const day = typeof data.day === 'string' ? data.day : '';
      markDate(day, focus);
      failure = {
        content: () => (day ? copy('error-date-day', { day: ltr(day) }) : copy('error-dates')),
        retry: null, blocks: true, reason: 'reason-dates',
      };
      return;
    }
    let content;
    if (['export_slot_busy', 'audit_unavailable', 'empty_scope'].includes(code)) content = () => copy(`error-${code}`);
    else if (code === 'export_failed') content = () => copy('error-export_failed', { reference: ltr(data.reference || '') });
    else if (code === 'invalid_options') content = () => copy('error-options');
    else content = () => data.error || copy('error-download');
    failure = { content, retry, blocks };
  }

  const thrown = error => (error instanceof TypeError ? () => copy('error-network') : () => error.message);

  function schedulePreflight({ now = false } = {}) {
    if (!context || stale) return;
    clearTimeout(timer);
    timer = 0;
    failure = null;
    slotWaits = 0;
    clearDateMark();
    pending = !refusal && !invalidReason();
    render();
    if (pending) later(now ? 0 : TIMING.debounce);
  }

  function later(ms) {
    timer = setTimeout(() => {
      timer = 0;
      runPreflight();
    }, ms);
  }

  // After an answer (or a failure) for the options `sent`: if the options
  // moved on meanwhile - the user, or the server's reading adding a choice -
  // ask for them now, unless a debounce is still counting down.
  function askAgainIfMoved(sent) {
    pending = !refusal && !invalidReason() && keyOf(optionsOf()) !== sent;
    if (pending && !timer) runPreflight();
  }

  const isRefusal = (status, data) => status === 404 || status === 409
    || ['not_found', 'run_not_found'].includes(data.code || data.error_code || '');

  async function runPreflight() {
    // One at a time: the answer on its way asks again for newer options.
    if (inFlight || !isCurrent()) return;
    const mine = ++preflightToken;
    const controller = new AbortController();
    preflightController = controller;
    inFlight = true;
    const options = optionsOf();
    const sent = keyOf(options);
    // The choices hardly change; the server re-sends them only when they do.
    const payload = choicesDigest ? { ...options, known_choices: choicesDigest } : options;
    try {
      const response = await fetch(endpoint('preflight/'), {
        method: 'POST', headers: headers(), body: JSON.stringify(payload), signal: controller.signal,
      });
      const data = await host.readResponse(response);
      if (mine !== preflightToken || !isCurrent()) return;
      inFlight = false;
      if (!response.ok || !data.ok) {
        if ((data.code || '') === 'export_slot_busy' && slotWaits < TIMING.busy.length) {
          // Another export holds the server's one slot: wait, then ask again.
          later(TIMING.busy[slotWaits++]);
          render();
          return;
        }
        if (!isRefusal(response.status, data) && keyOf(optionsOf()) !== sent) {
          // A failure for options already replaced: ask for the new ones.
          askAgainIfMoved(sent);
        } else {
          pending = false;
          problem(response.status, data, { retry: () => schedulePreflight({ now: true }), blocks: true });
        }
        render();
        return;
      }
      slotWaits = 0;
      refusal = null;
      answer = data;
      answeredFor = options;
      if (data.choices) {
        facts = factsFromChoices(data.choices);
        renderPickers();
      }
      if (typeof data.choices_digest === 'string') choicesDigest = data.choices_digest;
      askAgainIfMoved(sent);
      render();
    } catch (error) {
      if (mine !== preflightToken || error?.name === 'AbortError') return;
      inFlight = false;
      if (keyOf(optionsOf()) !== sent) askAgainIfMoved(sent);
      else {
        pending = false;
        failure = { content: thrown(error), retry: () => schedulePreflight({ now: true }), blocks: true };
      }
      render();
    } finally {
      if (preflightController === controller) preflightController = null;
      if (mine === preflightToken) inFlight = false;
    }
  }

  function filename(disposition) {
    const name = disposition.match(/filename="([^"]+)"/i)?.[1] || disposition.match(/filename=([^;\s]+)/i)?.[1] || '';
    return name.replace(/[\\/]/g, '_');
  }

  async function download() {
    if (!context || blockReason() || !isCurrent()) return;
    // The options lock while the file is made: focus in them (Enter in a
    // field submits) would fall to the page, so it waits on Download.
    if (document.activeElement?.closest?.('.et-export-step')) $('examStudentExportDownload').focus({ preventScroll: true });
    const mine = ++downloadToken;
    const controller = new AbortController();
    downloadController = controller;
    busy = true;
    failure = null;
    $('examStudentExportStatus').replaceChildren();
    render();
    let objectUrl = '';
    try {
      const response = await fetch(endpoint(''), {
        method: 'POST', headers: headers(), body: JSON.stringify(body()), signal: controller.signal,
      });
      if (mine !== downloadToken || !isCurrent()) return;
      const type = response.headers?.get('content-type') || '';
      if (!response.ok || !FILE_TYPES.test(type)) {
        const data = await host.readResponse(response);
        if (mine === downloadToken) problem(response.status, data, { retry: download, blocks: false, focus: true });
        return;
      }
      const blob = await response.blob();
      if (mine !== downloadToken || !isCurrent()) return;
      const name = filename(response.headers.get('content-disposition') || '') || 'exam_students.xlsx';
      const reference = response.headers.get('x-export-reference') || '';
      objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = name;
      document.body.append(link);
      link.click();
      link.remove();
      $('examStudentExportStatus').replaceChildren(copy('downloaded', { file: ltr(name), reference: ltr(reference) }));
      if (typeof notify !== 'undefined') {
        notify.success(copyText('downloaded-title', { file: ltr(name) }), copyText('reference', { reference: ltr(reference) }));
      }
    } catch (error) {
      if (mine !== downloadToken || error?.name === 'AbortError') return;
      failure = { content: thrown(error), retry: download, blocks: false };
    } finally {
      // The browser has taken the file before its blob URL is released.
      if (objectUrl) setTimeout(() => URL.revokeObjectURL(objectUrl), TIMING.revoke);
      if (mine === downloadToken) {
        busy = false;
        downloadController = null;
        render();
        refusedDate?.focus();
        refusedDate = null;
      }
    }
  }

  // ── Open and close ──────────────────────────────────────────

  function savedAt(iso) {
    const date = new Date(iso);
    if (!iso || Number.isNaN(date.getTime())) return '';
    const pad = n => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function renderSource(run) {
    const source = $('examStudentExportSource');
    const label = document.createElement('bdi');
    label.textContent = run.label || '';
    source.replaceChildren(copy('source', { id: ltr(context.runId), label, saved: ltr(savedAt(run.created_at)) }));
    const term = Object.values(run.section_enrollment || {}).flat().find(row => row?.academic_year && row?.term);
    if (term) source.append(copy('source-term', { year: ltr(term.academic_year), term: ltr(term.term) }));
  }

  function open(from) {
    if (dialog.open || host.blockReason()) return;
    const run = host.savedRun();
    context = host.context();
    opener = from;
    facts = factsFromRun(run);
    saved = savedCounts(run);
    answer = null;
    answeredFor = null;
    choicesDigest = '';
    refusal = null;
    failure = null;
    stale = false;
    busy = false;
    pending = false;
    $('examStudentExportStatus').replaceChildren();

    const stored = preferences();
    setRadio('examStudentScope', stored.scope, 'all');
    setRadio('examStudentContents', stored.contents, 'full');
    setRadio('examStudentLanguage', stored.language, 'ar');
    setRadio('examStudentRows', 'all', 'all');
    $('examStudentPreparedFor').value = '';
    $('examStudentProgramList').replaceChildren();
    ['examStudentCourse', 'examStudentSectionExam', 'examStudentSection', 'examStudentPeriod', 'examStudentRoomPeriod',
      'examStudentRoom', 'examStudentDay'].forEach(id => $(id).replaceChildren());
    GENDERS.forEach(gender => {
      const label = dialog.querySelector(`[data-group="${gender}"]`);
      label.hidden = !facts.genders.includes(gender);
      label.querySelector('input').checked = !label.hidden;
    });
    renderPickers();
    renderDates();
    $('examStudentOneFile').checked = groupBoxes().length > 1;
    renderSource(run);
    pending = true;
    render();

    if (dialog.showModal) dialog.showModal();
    else dialog.setAttribute('open', '');
    $('examStudentExportTitle').focus({ preventScroll: true });
    schedulePreflight({ now: true });
  }

  function close() {
    abortAll();
    context = null;
    if (dialog.close) dialog.close();
    else dialog.removeAttribute('open');
    const target = opener;
    opener = null;
    target?.focus({ preventScroll: true });
  }

  // ── Events ──────────────────────────────────────────────────

  $('examStudentDataBtn').addEventListener('click', event => open(event.currentTarget));
  $('examDepartmentStudentLink')?.addEventListener('click', () => {
    host.closeDepartmentFiles();
    open($('departmentFilesBtn'));
  });
  $('examStudentExportClose').addEventListener('click', close);
  $('examStudentExportCancel').addEventListener('click', close);
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    close();
  });
  $('examStudentExportForm').addEventListener('submit', event => {
    event.preventDefault();
    download();
  });
  $('examStudentExportRetry').addEventListener('click', () => {
    const retry = failure?.retry;
    if (!retry) return;
    // Trying again hides this button; focus waits on Download, whose
    // description says what is happening, instead of falling to the page.
    $('examStudentExportDownload').focus({ preventScroll: true });
    retry();
  });

  // Choosing in a row's picker chooses that row; the two exam pickers and
  // the two period pickers are one value each on the server, so they agree.
  const pick = kind => setRadio('examStudentScope', kind, kind);
  const mirror = (from, to) => { $(to).value = $(from).value; };
  $('examStudentSectionExam').addEventListener('change', () => { mirror('examStudentSectionExam', 'examStudentCourse'); fillSections(''); pick('section'); });
  $('examStudentCourse').addEventListener('change', () => { mirror('examStudentCourse', 'examStudentSectionExam'); fillSections(''); pick('course'); });
  $('examStudentSection').addEventListener('change', () => pick('section'));
  $('examStudentRoomPeriod').addEventListener('change', () => { mirror('examStudentRoomPeriod', 'examStudentPeriod'); fillRooms(''); pick('room'); });
  $('examStudentPeriod').addEventListener('change', () => { mirror('examStudentPeriod', 'examStudentRoomPeriod'); fillRooms(''); pick('period'); });
  $('examStudentRoom').addEventListener('change', () => pick('room'));
  $('examStudentDay').addEventListener('change', () => pick('day'));

  $('examStudentAllPrograms').addEventListener('change', event => {
    programBoxes().forEach(box => { box.checked = event.target.checked; });
  });
  // A shortcut picks exactly that department; pressed again, every program.
  $('examStudentDepartments').addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    const programs = JSON.parse(button.dataset.programs);
    const pressed = button.getAttribute('aria-pressed') === 'true';
    programBoxes().forEach(box => { box.checked = pressed || programs.includes(box.value); });
    syncPrograms();
    schedulePreflight();
  });

  // Whatever changes what would be counted or named asks again.
  dialog.addEventListener('change', event => {
    if (!context || event.target === $('examStudentPreparedFor')) return;
    if (event.target.closest('#examStudentProgramList') || event.target === $('examStudentAllPrograms')) syncPrograms();
    if (event.target.matches('select, input[name="examStudentScope"], input[name="examStudentContents"], input[name="examStudentLanguage"]')) remember();
    schedulePreflight();
  });
})();
