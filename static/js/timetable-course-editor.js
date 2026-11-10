/* Extend a saved scenario's course/section plan, then use its usual placement UI.

   Weekly meetings: a 3-credit course may meet as two 100-minute meetings a
   week instead of its credit hours' standard pattern (owner, 2026-11-10). A
   new course takes the choice with it when it is added. A course already in
   the plan changes it with its own Save, never with "Add to scenario": the
   change is the scenario's (POST …/budget/meeting-pattern/), placed sections
   keep their times, and the answer says which of them still meet the old way.

   A programme's default (owner, 2026-11-10: each programme sets its own
   3-credit courses in Section Planning, Male and Female alike). The budget
   rows say what the scenario's programmes decided for a course
   (`programme_meeting_pattern`: agree, mixed, none or ambiguous, with each
   programme's vote) and whether the course differs from a value they agreed
   on (`meeting_pattern_drift`). A new course's Weekly meetings starts on
   "Programme default", which sends nothing: the server applies the
   programmes' value when it fits the credit hours, and says what it applied
   (`meeting_pattern_applied`), which the page's "Added" then names. A course
   already in the plan names its programmes' default under what it meets now;
   when it differs, "Use programme default" saves that value as its own Save
   would. The pages say the same as a chip on a course (`programmeChip`) and
   as one line over the course list (`renderMeetingSummary`): only for a
   course whose weekly meetings can be chosen, and only when it says something.

   `request(url, options, onError)` resolves to the server's answer, or null;
   on a refusal it calls `onError(message, error)` with the server's words
   and its error object (`{code, details}`, when it sent one), so the dialog
   says them inside itself (a toast sits under a modal dialog).
   "Use programme default(s)" never posts a value read earlier: it asks the
   server to save what the programmes decide NOW (`programmeDefaultBody`),
   naming the value the page showed; when that is gone or changed the server
   refuses (409), the page says why in its own words (`refusalWords`) and
   reads the list again (the dialog through `reread()`, which resolves to the
   scenario's budget rows as read now, or null).
   `onSaved(budget, change)` gets the scenario's new budget; `change` is
   `{ action: 'meeting_pattern', course_key }` after a weekly-meetings save
   (the course saved), which leaves the dialog open on its own status line;
   after an add whose answer names what its programmes decided,
   `{ action: 'add', course_key, message }`, `message` the page's "Added" in
   words. `courseKey` opens the dialog on that course of the plan;
   `returnFocus()` takes the keyboard back when the control that opened the
   dialog was drawn again while it was open (gone when it closes). */
(() => {
  // Course codes, programme codes and section labels are isolated, so an
  // Arabic line never reorders them ("S1، S3" painted as "S3 ،S1").
  const iso = text => `${String.fromCharCode(0x2066)}${text}${String.fromCharCode(0x2069)}`;
  const words = arabic => (en, ar) => (arabic ? ar : en);
  // Meeting lengths in words, shortest first, as the workspace says them
  // (meetingLengthsText, page-timetable-workspace.js): "4 × 180 min" when
  // they are one length, else "75 + 100 min"; in Arabic in the digits of
  // the server's shape labels ("٢ × ١٠٠ دقيقة"). '' for none.
  const lengthsText = (minutes, arabic) => {
    const lengths = (Array.isArray(minutes) ? minutes : []).map(Number).filter(n => n > 0).sort((a, b) => a - b);
    if (!lengths.length) return '';
    const text = lengths.length > 1 && lengths.every(n => n === lengths[0]) ? `${lengths.length} × ${lengths[0]}` : lengths.join(' + ');
    return arabic ? `${text.replace(/\d/g, digit => '٠١٢٣٤٥٦٧٨٩'[digit])} دقيقة` : `${text} min`;
  };

  /* ── What a course's programmes decided (compute_scenario_budget with the
     scenario's programmes; core/services/programme_meeting_patterns.py) ── */
  // Said only for a course whose weekly meetings can be chosen (two choices
  // or more): never an English bundle, never other credit hours.
  const programmeBlock = row => {
    const block = row?.programme_meeting_pattern;
    const offered = Array.isArray(row?.meeting_pattern_choices) && row.meeting_pattern_choices.length > 1;
    return offered && block && typeof block === 'object' ? block : null;
  };
  // A programme's vote as a budget stores it: a shape, else the credit
  // hours' pattern ('').
  const budgetValue = value => (value && value !== 'standard' ? String(value) : '');
  // One programme's vote in a line's words: '' not set, 'standard', a shape.
  const voteWords = (value, arabic) => {
    const t = words(arabic);
    if (value === 'standard') return t('standard', 'قياسي');
    if (value === '2x100') return t('2 × 100 min', '٢ × ١٠٠ دقيقة');
    return value ? String(value) : t('not set', 'غير محدد');
  };
  // What the programmes agreed on: the server's label for a shape (Arabic
  // digits in Arabic), "standard" for the credit hours' pattern chosen.
  const agreedWords = (block, arabic) => (block.meeting_pattern === 'standard'
    ? voteWords('standard', arabic)
    : (arabic ? block.label_ar : block.label) || voteWords(block.meeting_pattern, arabic));

  /* 'drift': the course differs from a value its programmes agreed on;
     'differ': its programmes disagree, so it kept its credit hours' pattern;
     'applied': it meets in the shape its programmes agreed on;
     '' when there is nothing to say (no programme set it, two courses behind
     a typed code, or the course is on the pattern they agreed it keeps). */
  function programmeState(row) {
    const block = programmeBlock(row);
    if (!block) return '';
    if (row.meeting_pattern_drift === true) return 'drift';
    if (block.status === 'mixed') return 'differ';
    if (block.status === 'agree' && budgetValue(block.meeting_pattern)
      && (row.meeting_pattern || '') === budgetValue(block.meeting_pattern)) return 'applied';
    return '';
  }
  // The value "Use programme default" saves for a course ('' or a shape).
  const programmeValue = row => budgetValue(programmeBlock(row)?.meeting_pattern);
  // Its body: the server saves what the programmes decide when it is asked,
  // refusing when that is not the value this row showed.
  const programmeDefaultBody = row => ({ course_key: row.course_key, use_programme_default: true, expected_meeting_pattern: programmeValue(row) });
  /* Why the server would not use a programme default, in the page's words:
     the programmes' default changed, they differ now, none sets it now, or
     two courses are behind the code. '' for any other refusal (the page then
     says the server's own words). */
  function refusalWords(error, arabic = false) {
    const t = words(arabic);
    if (error?.code === 'PROGRAMME_DEFAULT_CHANGED') {
      return t('its programmes’ default changed since the list was drawn.', 'تغيّر افتراضي البرامج له منذ عُرضت القائمة.');
    }
    if (error?.code !== 'PROGRAMME_DEFAULT_UNAVAILABLE') return '';
    const reason = error.details?.reason;
    if (reason === 'mixed') return t('its programmes differ now.', 'برامجه مختلفة الآن.');
    if (reason === 'ambiguous') return t('two courses share its code, so its programmes’ default cannot be told.', 'يشترك مقرران في رمزه، فلا يُعرف افتراضي البرامج له.');
    return t('no programme sets it now.', 'لم يعد أي برنامج يضبطه.');
  }
  const programmeDefaultRefused = error => error?.code === 'PROGRAMME_DEFAULT_CHANGED' || error?.code === 'PROGRAMME_DEFAULT_UNAVAILABLE';
  /* The placed sections a weekly-meetings save left meeting the old way
     (its `mismatched_sections`), in sentences: how many and which, then the
     locked ones and those another scenario owns, which need their own step
     first. `name` names a section; `ending` is where re-placing puts them. */
  function leftBehindLines(sections, arabic, { name = section => section.section, ending = null } = {}) {
    const t = words(arabic);
    const list = Array.isArray(sections) ? sections : [];
    if (!list.length) return [];
    const named = items => items.map(section => iso(name(section))).join(t(', ', '، '));
    const n = list.length;
    const [where, whereAr] = ending || ['in this pattern', 'بهذا النمط'];
    const lines = [t(
      `${n === 1 ? '1 placed section still meets' : `${n} placed sections still meet`} the old way: ${named(list)}. `
        + `Remove ${n === 1 ? 'it' : 'them'} and run Optimise (or Full rebuild) to place ${n === 1 ? 'it' : 'them'} ${where}.`,
      `عدد الشعب الموضوعة التي ما زالت تجتمع بالطريقة السابقة: ${n} (${named(list)}). احذفها ثم شغّل «تحسين» (أو «إعادة بناء كاملة») لتوضع ${whereAr}.`)];
    const external = list.filter(section => section.externally_owned);
    const locked = list.filter(section => section.locked && !section.externally_owned);
    if (locked.length) lines.push(t(`Locked (unlock before removing): ${named(locked)}.`, `مقفلة (ألغِ قفلها قبل الحذف): ${named(locked)}.`));
    if (external.length) lines.push(t(`Owned by another scenario (change there): ${named(external)}.`, `يملكها سيناريو آخر (عدّلها هناك): ${named(external)}.`));
    return lines;
  }
  // The chip next to a course: only when it differs from its programmes'
  // default, or its programmes differ; '' otherwise.
  function programmeChip(row, arabic = false) {
    const t = words(arabic);
    const state = programmeState(row);
    if (state === 'drift') {
      const value = agreedWords(row.programme_meeting_pattern, arabic);
      return t(`programme default ${value}`, `افتراضي البرامج ${value}`);
    }
    return state === 'differ' ? t('programmes differ', 'البرامج مختلفة') : '';
  }

  /* The scenario's weekly-meetings line over a course list: the courses that
     meet in the shape their programmes agreed on, those whose programmes
     differ, and those that differ from their programmes' default, each a
     button that opens the course dialog on it (`onOpen(courseKey)`), then
     "Use programme defaults (N)" for the last (`onApply(rows)`, the rows in
     the order shown). Nothing at all, and the box hidden, when no course has
     anything to say. Built from DOM nodes, never HTML. Drawn again only when
     what it says changed, so a button keeps the keyboard; when the box is
     drawn again under the keyboard it goes to the same course's button (or
     the next control there, else `fallback`). Returns the "Use programme
     defaults" button (or null), for the page's own marks. */
  function renderMeetingSummary(box, rows, { arabic = false, disabled = false, applyClass = '', onOpen = null, onApply = null, fallback = null } = {}) {
    if (!box) return null;
    const t = words(arabic);
    const groups = { applied: [], differ: [], drift: [] };
    (Array.isArray(rows) ? rows : []).forEach(row => {
      const state = programmeState(row);
      if (state) groups[state].push(row);
    });
    const order = (a, b) => String(a.course_code || '').localeCompare(String(b.course_code || ''))
      || String(a.course_key || '').localeCompare(String(b.course_key || ''));
    Object.values(groups).forEach(list => list.sort(order));
    const signature = JSON.stringify([arabic, Object.entries(groups).map(([state, list]) => [state, list.map(row => [row.course_key,
      row.course_code, row.course_name, row.meeting_pattern, row.meeting_durations, row.programme_meeting_pattern])])]);
    if (box.dataset.summary === signature) {
      const kept = box.querySelector('[data-summary-apply]');
      if (kept) kept.disabled = Boolean(disabled);
      return kept;
    }
    const held = box.contains(document.activeElement) ? (document.activeElement.dataset?.summaryKey ?? null) : null;
    box.dataset.summary = signature;
    box.replaceChildren();
    box.hidden = !Object.values(groups).some(list => list.length);
    if (box.hidden) {
      if (held !== null) focusSummary(box, { fallback });
      return null;
    }

    const line = document.createElement('p');
    line.style.margin = '0';
    const head = document.createElement('strong');
    head.textContent = t('Weekly meetings:', 'اللقاءات الأسبوعية:');
    line.append(head, ' ');
    // A course's button: its code, read with its name ("STAT103 Probability:
    // its weekly meetings"), since two courses may share a code.
    const course = row => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.summaryKey = row.course_key;
      button.textContent = row.course_code || row.course_key;
      const name = [row.course_code || row.course_key, row.course_name].filter(Boolean).join(' ');
      button.setAttribute('aria-label', t(`${name}: its weekly meetings`, `${name}: لقاءاته الأسبوعية`));
      // A word in a sentence that opens a dialog: the brand's text ink, underlined.
      button.style.cssText = 'padding:0;border:0;background:none;color:var(--text-brand,#067858);font:inherit;font-weight:600;text-decoration:underline;cursor:pointer';
      button.addEventListener('click', () => onOpen?.(row.course_key));
      return button;
    };
    const listed = (parent, items) => items.forEach((item, index) => {
      if (index) parent.append(t(', ', '، '));
      parent.append(...[item].flat());
    });
    const parts = [];
    // Applied: one group per shape, named by the server's label.
    const byLabel = new Map();
    groups.applied.forEach(row => {
      const label = (arabic ? row.meeting_pattern_label_ar : row.meeting_pattern_label) || agreedWords(row.programme_meeting_pattern, arabic);
      byLabel.set(label, [...(byLabel.get(label) || []), row]);
    });
    byLabel.forEach((list, label) => {
      const part = document.createElement('span');
      listed(part, list.map(course));
      part.append(t(` ${label} (programme default)`, ` ${label} (افتراضي البرامج)`));
      parts.push(part);
    });
    if (groups.differ.length) {
      const part = document.createElement('span');
      part.append(t('Programmes differ: ', 'البرامج مختلفة: '));
      // What it meets now: kept on its credit hours' pattern, or set here.
      const now = row => (row.meeting_pattern
        ? t(` (set here to ${(arabic ? row.meeting_pattern_label_ar : row.meeting_pattern_label) || voteWords(row.meeting_pattern, arabic)})`,
          ` (ضُبط هنا على ${(arabic ? row.meeting_pattern_label_ar : row.meeting_pattern_label) || voteWords(row.meeting_pattern, arabic)})`)
        : t(` (kept ${lengthsText(row.meeting_durations, false) || 'its standard pattern'})`,
          ` (بقي على ${lengthsText(row.meeting_durations, true) || 'نمطه القياسي'})`));
      listed(part, groups.differ.map(row => [course(row), now(row)]));
      parts.push(part);
    }
    if (groups.drift.length) {
      const part = document.createElement('span');
      part.append(t('Differs from programme default: ', 'يختلف عن افتراضي البرامج: '));
      listed(part, groups.drift.map(course));
      parts.push(part);
    }
    parts.forEach((part, index) => { if (index) line.append(' · '); line.append(part); });
    box.append(line);
    let apply = null;
    if (groups.drift.length) {
      apply = document.createElement('button');
      apply.type = 'button';
      apply.dataset.summaryApply = '';
      apply.dataset.summaryKey = '';
      if (applyClass) apply.className = applyClass;
      apply.style.marginTop = '6px';
      const n = groups.drift.length;
      apply.textContent = t(`Use programme defaults (${n})`, `استخدام افتراضي البرامج (${n})`);
      apply.disabled = Boolean(disabled);
      const drift = groups.drift.slice();
      apply.addEventListener('click', () => onApply?.(drift));
      box.append(apply);
    }
    if (held !== null) focusSummary(box, { key: held, fallback });
    return apply;
  }
  /* The keyboard to the summary line: the button of course `key` when it is
     still there, else "Use programme defaults", else its first course, else
     `fallback` (the course list). */
  function focusSummary(box, { key = null, fallback = null } = {}) {
    const buttons = box && !box.hidden ? [...box.querySelectorAll('button')] : [];
    const target = buttons.find(button => key !== null && button.dataset.summaryKey === key && !button.disabled)
      || buttons.find(button => 'summaryApply' in button.dataset && !button.disabled)
      || buttons.find(button => !button.disabled)
      || fallback;
    target?.focus?.();
  }
  /* What a run of "Use programme defaults" did, for the page to say: all of
     them set, or where it stopped and why (the page's words for a programme
     default that is gone or changed, else the server's), then the placed
     sections the saved courses left meeting the old way (`left`: their
     `mismatched_sections`, named with their course), as one course's own
     save names them. The page warns, never reports success, while any is
     left or the run stopped. */
  function programmeDefaultsWords({ saved = 0, total = 0, stopped = null, left = [], arabic = false } = {}) {
    const t = words(arabic);
    let said;
    if (!stopped) {
      said = t(saved === 1 ? '1 course now meets as its programmes set it.' : `${saved} courses now meet as their programmes set them.`,
        `عدد المقررات التي صارت تجتمع كما ضبطتها برامجها: ${saved}.`);
    } else {
      const code = iso(stopped.row?.course_code || stopped.row?.course_key || '');
      const why = refusalWords(stopped.error, arabic) || stopped.refusal || t('it could not be saved.', 'تعذر الحفظ.');
      said = t(`Stopped at ${code}: ${why} ${saved} of ${total} set before it.`,
        `توقف عند ${code}: ${why} ضُبط قبله ${saved} من ${total}.`);
    }
    const lines = leftBehindLines(left, arabic, {
      name: section => [section.course_code, section.section].filter(Boolean).join(' '),
      ending: ['in their new weekly meetings', 'بلقاءاتها الأسبوعية الجديدة'],
    });
    return [said, ...lines].join(' ');
  }

  function open({ scenarioId, published, budget, request, onSaved, reread = null, arabic = false, courseKey = '', returnFocus = null }) {
    const t = words(arabic);
    // The weekly patterns there are (core/services/timetable_meeting_shape.py):
    // '' is the credit hours' standard pattern, the server's default.
    const PATTERN_WORDS = { '': t('Standard (from credit hours)', 'قياسي (حسب الساعات المعتمدة)'), '2x100': t('2 × 100 min', '٢ × ١٠٠ دقيقة') };
    const patternWords = value => PATTERN_WORDS[value] ?? value;
    // A new course's choice has words of its own: '' sends nothing, so the
    // server gives it its programmes' default; "Standard" (the 3-credit
    // standard pattern: the choice is open at 3 credit hours only) and a
    // shape are sent as chosen.
    const NEW_COURSE_WORDS = { '': t('Programme default', 'افتراضي البرامج'),
      standard: `${t('Standard', 'قياسي')} (${lengthsText([75, 75], arabic)})`, '2x100': PATTERN_WORDS['2x100'] };
    // A budget row's weekly meetings: the server's label for a configured
    // course; for any other its minutes (`meeting_durations`), since its
    // credit hours do not decide them all (an English bundle meets 4 × 180
    // min whatever they are); the standard pattern's words only for an older
    // answer with neither.
    const shapeLabel = row => (arabic ? row.meeting_pattern_label_ar : row.meeting_pattern_label)
      || lengthsText(row.meeting_durations, arabic) || patternWords(row.meeting_pattern || '');
    const rows = new Map(budget.map(row => [row.course_key, row]));
    const opener = document.activeElement;
    const dialog = document.createElement('dialog');
    dialog.dir = arabic ? 'rtl' : 'ltr';
    // --t1 is the design system's text colour in both themes (global.css): in
    // the dark one the dialog's --surface is dark, and a fallback ink of the
    // light theme on it would leave the dialog unreadable.
    dialog.style.cssText = 'width:min(480px,92vw);padding:24px;border:1px solid #64748b;border-radius:12px;background:var(--surface,#fff);color:var(--t1,#172033)';
    const title = document.createElement('h2');
    title.textContent = t('Add course / section', 'إضافة مقرر / شعبة');
    dialog.setAttribute('aria-label', title.textContent);
    dialog.append(title);
    const note = document.createElement('p');
    note.textContent = !scenarioId ? t('Select a scenario first.', 'اختر سيناريو أولاً.')
      : published ? t('Copy this published scenario into a draft to add courses or sections.', 'انسخ السيناريو المنشور إلى مسودة لإضافة مقررات أو شعب.')
        : t('Add to the required sections list, then choose a board and time to place each section.', 'أضف إلى قائمة الشعب المطلوبة، ثم اختر اللوحة والوقت لوضع كل شعبة.');
    dialog.append(note);
    const form = document.createElement('form');
    form.style.cssText = 'display:grid;gap:12px';
    const field = (labelText, input, parent = form) => {
      const label = document.createElement('label');
      label.textContent = labelText;
      input.style.cssText = 'display:block;width:100%;padding:8px;color:inherit;background:inherit;border:1px solid #94a3b8;border-radius:5px';
      label.append(input); parent.append(label); return input;
    };
    const options = (input, values, named = patternWords) => input.replaceChildren(...values.map(value => new Option(named(value), value)));
    const select = document.createElement('select');
    select.add(new Option(t('New course', 'مقرر جديد'), ''));
    budget.forEach(row => select.add(new Option(`${row.course_code} · ${row.course_name || row.course_key}`, row.course_key)));
    field(t('Course', 'المقرر'), select);

    // A course already in the plan: its weekly meetings now and, when its
    // credit hours allow another (meeting_pattern_choices), a choice saved on
    // its own. A course with one choice only shows it. Under what it meets
    // now, what its programmes decided, and, when it differs from a value
    // they agreed on, "Use programme default".
    const weekly = document.createElement('div');
    weekly.style.cssText = 'display:grid;gap:8px';
    weekly.hidden = true;
    const now = document.createElement('p'); now.style.margin = '0';
    const programmes = document.createElement('p'); programmes.style.margin = '0'; programmes.hidden = true;
    weekly.append(now, programmes);
    const shape = field(t('Weekly meetings', 'اللقاءات الأسبوعية'), document.createElement('select'), weekly);
    const saveShape = document.createElement('button'); saveShape.type = 'button';
    saveShape.textContent = t('Save weekly meetings', 'حفظ اللقاءات الأسبوعية');
    saveShape.disabled = !scenarioId || published;
    const useDefault = document.createElement('button'); useDefault.type = 'button'; useDefault.hidden = true;
    useDefault.textContent = t('Use programme default', 'استخدام افتراضي البرامج');
    useDefault.disabled = !scenarioId || published;
    const status = document.createElement('p'); status.setAttribute('role', 'status'); status.style.margin = '0';
    weekly.append(saveShape, useDefault, status);
    form.append(weekly);

    const text = (label, required = false) => {
      const input = document.createElement('input'); input.required = required; input.maxLength = 100;
      return field(label, input);
    };
    const number = (label, value, min, max) => {
      const input = document.createElement('input'); Object.assign(input, { type: 'number', value, min, max, step: '1', required: true });
      return field(label, input);
    };
    const code = text(t('Course code', 'رمز المقرر'), true);
    const name = text(t('Course name', 'اسم المقرر'));
    const hours = number(t('Credit hours', 'الساعات المعتمدة'), 3, 1, 12);
    // A new course's weekly meetings: a choice only for a 3-credit course,
    // so any other credit hours set it back to the programmes' default (which
    // the server gives the credit hours' pattern there).
    const pattern = field(t('Weekly meetings', 'اللقاءات الأسبوعية'), document.createElement('select'));
    options(pattern, ['', 'standard', '2x100'], value => NEW_COURSE_WORDS[value]);
    const patternHint = document.createElement('small');
    patternHint.id = 'twCourseEditorPatternHint';
    patternHint.style.cssText = 'margin-top:-8px;color:var(--t3,#475569)';
    patternHint.textContent = t('2 × 100 min is only for 3-credit courses.', '٢ × ١٠٠ دقيقة للمقررات ذات ٣ ساعات معتمدة فقط.');
    pattern.setAttribute('aria-describedby', patternHint.id);
    form.append(patternHint);
    const capacity = number(t('Section capacity', 'سعة الشعبة'), 40, 1, 1000);
    const term = number(t('Programme term (optional)', 'المستوى (اختياري)'), '', 1, 20); term.required = false;
    const count = number(t('Sections to add', 'عدد الشعب المضافة'), 1, 1, 100);
    const syncPattern = () => {
      const allowed = Number(hours.value) === 3;
      if (!allowed) pattern.value = '';
      pattern.disabled = !allowed || !!select.value;
      patternHint.hidden = allowed || !!select.value;
    };
    hours.addEventListener('input', syncPattern);
    hours.addEventListener('change', syncPattern);
    syncPattern();

    // What the course's programmes decided, in a line: their default (the
    // programmes that chose it) or each programme's vote when they differ;
    // nothing when no programme set it, or for a course whose weekly
    // meetings cannot be chosen.
    const programmeLine = row => {
      const block = programmeBlock(row);
      if (!block) return '';
      const named = list => list.map(item => iso(item.program)).join(t(', ', '، '));
      const votes = Array.isArray(block.programmes) ? block.programmes : [];
      if (block.status === 'agree' && block.meeting_pattern) {
        const value = agreedWords(block, arabic);
        const voters = votes.filter(item => item.meeting_pattern === block.meeting_pattern);
        const who = voters.length ? ` (${named(voters)})` : '';
        return t(`Programme default: ${value}${who}`, `افتراضي البرامج: ${value}${who}`);
      }
      if (block.status === 'mixed') {
        const each = votes.map(item => `${iso(item.program)} ${voteWords(item.meeting_pattern, arabic)}`).join(' · ');
        return t(`Programmes differ: ${each}`, `البرامج مختلفة: ${each}`);
      }
      return '';
    };

    const alert = document.createElement('p'); alert.setAttribute('role', 'alert');
    const showWeekly = () => {
      const row = rows.get(select.value);
      weekly.hidden = !row;
      status.textContent = '';
      if (!row) return;
      now.textContent = t(`Weekly meetings now: ${shapeLabel(row)}`, `اللقاءات الأسبوعية حالياً: ${shapeLabel(row)}`);
      programmes.textContent = programmeLine(row);
      programmes.hidden = !programmes.textContent;
      const values = Array.isArray(row.meeting_pattern_choices) ? row.meeting_pattern_choices : [];
      shape.parentElement.hidden = saveShape.hidden = values.length < 2;
      useDefault.hidden = programmeState(row) !== 'drift';
      options(shape, values);
      shape.value = row.meeting_pattern || '';
    };
    select.onchange = () => {
      [code, name, hours, capacity, term].forEach(input => {
        input.disabled = !!select.value; input.parentElement.hidden = !!select.value;
      });
      pattern.parentElement.hidden = !!select.value;
      syncPattern();
      alert.textContent = '';
      showWeekly();
    };

    // How a save went: saved or unchanged, then the placed sections that
    // still meet the old way, which only re-placing changes (a pattern change
    // never moves a placed section); the locked ones, and those another
    // scenario owns, need their own step first.
    const savedWords = (row, result) => {
      const course = iso(row.course_code || row.course_key);
      const label = shapeLabel(row);
      const lines = [result.changed
        ? t(`Saved: ${course} is now set to ${label}.`, `حُفظ: ضُبط ${course} على ${label}.`)
        : t(`Nothing changed: ${course} is already set to ${label}.`, `لم يتغير شيء: ${course} مضبوط مسبقاً على ${label}.`)];
      lines.push(...leftBehindLines(result.mismatched_sections, arabic));
      return lines.join(' ');
    };
    // One weekly-meetings save, from "Save weekly meetings" (the choice) or
    // "Use programme default" (what the programmes decide now, on the
    // server): the same endpoint, the same words after it. A programme
    // default gone or changed since the list was read is said in the page's
    // words, and the course is read again (`reread`), so the dialog shows
    // what its programmes decide now.
    const saveWeekly = async (bodyFor, trigger) => {
      const row = rows.get(select.value);
      if (!row || !scenarioId || published || trigger.disabled) return;
      saveShape.disabled = useDefault.disabled = true; alert.textContent = ''; status.textContent = '';
      let refusal = '';
      let error = null;
      try {
        const result = await request(`/ops/tw/scenarios/${scenarioId}/budget/meeting-pattern/`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(bodyFor(row)),
        }, (message, refused) => {
          refusal = typeof message === 'string' ? message : '';
          error = refused && typeof refused === 'object' ? refused : null;
        });
        if (!result?.ok) {
          const why = refusalWords(error, arabic);
          alert.textContent = (why && t(`Not saved: ${why}`, `لم يُحفظ: ${why}`)) || refusal
            || t('Could not save the weekly meetings. Check the scenario status, then retry.', 'تعذر حفظ اللقاءات الأسبوعية. تحقق من حالة السيناريو ثم أعد المحاولة.');
          if (programmeDefaultRefused(error) && typeof reread === 'function') {
            const fresh = await reread();
            if (Array.isArray(fresh) && dialog.isConnected) {
              fresh.forEach(item => rows.set(item.course_key, item));
              if (select.value === row.course_key) showWeekly();
            }
          }
          return;
        }
        // The answer's budget is the scenario's own, this course's new row in it.
        (Array.isArray(result.budget) ? result.budget : []).forEach(fresh => rows.set(fresh.course_key, fresh));
        showWeekly();
        status.textContent = savedWords(rows.get(row.course_key), result);
        await onSaved(result.budget, { action: 'meeting_pattern', course_key: row.course_key });
      } catch (error) { alert.textContent = error.message; }
      finally {
        saveShape.disabled = useDefault.disabled = false;
        // A button disabled while it has the keyboard loses it to the page;
        // the dialog stays open, so the keyboard comes back to the button,
        // or to "Save weekly meetings" once "Use programme default" is done
        // (hidden: the course meets its programmes' default now).
        const back = trigger.hidden ? saveShape : trigger;
        const lost = !dialog.contains(document.activeElement) || Boolean(document.activeElement?.closest('[hidden]'));
        if (dialog.isConnected && lost) back.focus();
      }
    };
    saveShape.onclick = () => saveWeekly(row => ({ course_key: row.course_key, meeting_pattern: shape.value }), saveShape);
    useDefault.onclick = () => saveWeekly(programmeDefaultBody, useDefault);

    // The page's "Added", when the answer names what the programmes decided
    // for the new course: the shape they gave it, or that they differ. ''
    // for any other add, which the page words as before.
    const addedWords = (applied, course) => {
      const c = iso(course);
      if (applied?.source === 'programmes') {
        const label = patternWords(applied.value);
        return t(`Added ${c} — ${label} from its programmes. Choose the section from Required sections to place it.`,
          `أُضيف ${c} — ${label} حسب برامجه. اختر الشعبة من قائمة الشعب المطلوبة لوضعها.`);
      }
      if (applied?.source === 'standard' && applied.status === 'mixed') {
        return t(`Added ${c} — its programmes differ, so it keeps its credit hours’ weekly meetings. Choose the section from Required sections to place it.`,
          `أُضيف ${c} — برامجه مختلفة، فبقي على اللقاءات الأسبوعية حسب ساعاته المعتمدة. اختر الشعبة من قائمة الشعب المطلوبة لوضعها.`);
      }
      return '';
    };

    form.append(alert);
    const save = document.createElement('button'); save.type = 'submit';
    save.textContent = t('Add to scenario', 'إضافة إلى السيناريو');
    save.disabled = !scenarioId || published; form.append(save);
    const cancel = document.createElement('button'); cancel.type = 'button';
    cancel.textContent = t('Close', 'إغلاق'); cancel.onclick = () => dialog.close(); form.append(cancel);
    form.onsubmit = async event => {
      event.preventDefault();
      if (!scenarioId || published || save.disabled) return;
      // A course in the plan whose weekly meetings were changed here and not
      // saved: "Add to scenario" adds sections only, so it would drop the
      // change without a word. Nothing is sent until it is saved with its own
      // button or set back. Only where the choice is offered: a course with
      // one choice (an English bundle, a 4-credit course) is never held.
      const chosen = rows.get(select.value);
      if (chosen && !saveShape.hidden && shape.value !== (chosen.meeting_pattern || '')) {
        alert.textContent = t('The weekly meetings are changed but not saved: use Save weekly meetings first, or set them back, then add sections.',
          'تغيّرت اللقاءات الأسبوعية ولم تُحفظ: استخدم «حفظ اللقاءات الأسبوعية» أولاً، أو أعدها كما كانت، ثم أضف الشعب.');
        shape.focus();
        return;
      }
      save.disabled = true; alert.textContent = '';
      try {
        const body = { course_key: select.value || code.value.trim().toUpperCase(),
          course_code: code.value, course_name: name.value, credit_hours: hours.value,
          max_per_section: capacity.value, programme_term: term.value, sections: count.value };
        // A new course's weekly meetings go with it when chosen. "Programme
        // default" is the server's default and is not sent, so a typed code
        // already in the plan only gains sections, as before.
        if (!select.value && pattern.value) body.meeting_pattern = pattern.value;
        // A refusal is said in the dialog, in the server's words (a toast would
        // sit under the modal dialog): e.g. a typed code already in the plan
        // with other weekly meetings, which its own Save changes.
        let refusal = '';
        const result = await request(`/ops/tw/scenarios/${scenarioId}/budget/`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        }, message => { refusal = typeof message === 'string' ? message : ''; });
        if (!result?.ok) { alert.textContent = refusal || t('Could not add. Check the values and scenario status, then retry.', 'تعذرت الإضافة. تحقق من القيم وحالة السيناريو ثم أعد المحاولة.'); return; }
        const message = addedWords(result.meeting_pattern_applied, body.course_key);
        await onSaved(result.budget, message ? { action: 'add', course_key: body.course_key, message } : undefined); dialog.close();
      } catch (error) { alert.textContent = error.message; }
      finally { save.disabled = false; }
    };
    dialog.append(form); document.body.append(dialog);
    dialog.addEventListener('close', () => {
      dialog.remove();
      // The control that opened the dialog was drawn again while it was open:
      // the page says where the keyboard goes instead of leaving it nowhere.
      if (typeof returnFocus === 'function' && opener && opener !== document.body && !opener.isConnected) returnFocus();
    }, { once: true });
    // Opened on a course of the plan (the page's summary line): that course is chosen.
    if (courseKey && rows.has(courseKey)) { select.value = courseKey; select.onchange(); }
    dialog.showModal();
  }

  globalThis.TimetableCourseEditor = {
    open, programmeState, programmeChip, programmeValue, programmeDefaultBody, refusalWords, renderMeetingSummary, focusSummary,
    programmeDefaultsWords,
  };
})();
