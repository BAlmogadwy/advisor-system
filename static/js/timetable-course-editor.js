/* Extend a saved scenario's course/section plan, then use its usual placement UI.

   Weekly meetings: a 3-credit course may meet as two 100-minute meetings a
   week instead of its credit hours' standard pattern (owner, 2026-11-10). A
   new course takes the choice with it when it is added. A course already in
   the plan changes it with its own Save, never with "Add to scenario": the
   change is the scenario's (POST …/budget/meeting-pattern/), placed sections
   keep their times, and the answer says which of them still meet the old way.

   `request(url, options, onError)` resolves to the server's answer, or null;
   on a refusal it calls `onError(message)` with the server's words, so the
   dialog says them inside itself (a toast sits under a modal dialog).
   `onSaved(budget, change)` gets the scenario's new budget; `change` is
   `{ action: 'meeting_pattern' }` after a weekly-meetings save, which leaves
   the dialog open on its own status line. */
globalThis.TimetableCourseEditor = {
  open({ scenarioId, published, budget, request, onSaved, arabic = false }) {
    const t = (en, ar) => arabic ? ar : en;
    // Course codes and section labels are isolated, so an Arabic line never
    // reorders them ("S1، S3" painted as "S3 ،S1").
    const iso = text => `${String.fromCharCode(0x2066)}${text}${String.fromCharCode(0x2069)}`;
    // The weekly patterns there are (core/services/timetable_meeting_shape.py):
    // '' is the credit hours' standard pattern, the server's default.
    const PATTERN_WORDS = { '': t('Standard (from credit hours)', 'قياسي (حسب الساعات المعتمدة)'), '2x100': t('2 × 100 min', '٢ × ١٠٠ دقيقة') };
    const patternWords = value => PATTERN_WORDS[value] ?? value;
    // A budget row's weekly meetings: the server's label for a configured
    // course, the standard pattern's words for any other.
    const shapeLabel = row => (arabic ? row.meeting_pattern_label_ar : row.meeting_pattern_label) || patternWords(row.meeting_pattern || '');
    const rows = new Map(budget.map(row => [row.course_key, row]));
    const dialog = document.createElement('dialog');
    dialog.dir = arabic ? 'rtl' : 'ltr';
    dialog.style.cssText = 'width:min(480px,92vw);padding:24px;border:1px solid #64748b;border-radius:12px;background:var(--surface,#fff);color:var(--text,#172033)';
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
    const options = (input, values) => input.replaceChildren(...values.map(value => new Option(patternWords(value), value)));
    const select = document.createElement('select');
    select.add(new Option(t('New course', 'مقرر جديد'), ''));
    budget.forEach(row => select.add(new Option(`${row.course_code} · ${row.course_name || row.course_key}`, row.course_key)));
    field(t('Course', 'المقرر'), select);

    // A course already in the plan: its weekly meetings now and, when its
    // credit hours allow another (meeting_pattern_choices), a choice saved on
    // its own. A course with one choice only shows it.
    const weekly = document.createElement('div');
    weekly.style.cssText = 'display:grid;gap:8px';
    weekly.hidden = true;
    const now = document.createElement('p'); now.style.margin = '0';
    weekly.append(now);
    const shape = field(t('Weekly meetings', 'اللقاءات الأسبوعية'), document.createElement('select'), weekly);
    const saveShape = document.createElement('button'); saveShape.type = 'button';
    saveShape.textContent = t('Save weekly meetings', 'حفظ اللقاءات الأسبوعية');
    saveShape.disabled = !scenarioId || published;
    const status = document.createElement('p'); status.setAttribute('role', 'status'); status.style.margin = '0';
    weekly.append(saveShape, status);
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
    // A new course's weekly meetings: 2 × 100 min is only for a 3-credit
    // course, so any other credit hours set it back to the standard pattern.
    const pattern = field(t('Weekly meetings', 'اللقاءات الأسبوعية'), document.createElement('select'));
    options(pattern, ['', '2x100']);
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

    const alert = document.createElement('p'); alert.setAttribute('role', 'alert');
    const showWeekly = () => {
      const row = rows.get(select.value);
      weekly.hidden = !row;
      status.textContent = '';
      if (!row) return;
      now.textContent = t(`Weekly meetings now: ${shapeLabel(row)}`, `اللقاءات الأسبوعية حالياً: ${shapeLabel(row)}`);
      const values = Array.isArray(row.meeting_pattern_choices) ? row.meeting_pattern_choices : [];
      shape.parentElement.hidden = saveShape.hidden = values.length < 2;
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
      const mismatched = Array.isArray(result.mismatched_sections) ? result.mismatched_sections : [];
      if (!mismatched.length) return lines[0];
      const named = sections => sections.map(section => iso(section.section)).join(t(', ', '، '));
      const n = mismatched.length;
      lines.push(t(
        `${n === 1 ? '1 placed section still meets' : `${n} placed sections still meet`} the old way: ${named(mismatched)}. `
          + `Remove ${n === 1 ? 'it' : 'them'} and run Optimise (or Full rebuild) to place ${n === 1 ? 'it' : 'them'} in this pattern.`,
        `عدد الشعب الموضوعة التي ما زالت تجتمع بالطريقة السابقة: ${n} (${named(mismatched)}). احذفها ثم شغّل «تحسين» (أو «إعادة بناء كاملة») لتوضع بهذا النمط.`));
      const external = mismatched.filter(section => section.externally_owned);
      const locked = mismatched.filter(section => section.locked && !section.externally_owned);
      if (locked.length) lines.push(t(`Locked (unlock before removing): ${named(locked)}.`, `مقفلة (ألغِ قفلها قبل الحذف): ${named(locked)}.`));
      if (external.length) lines.push(t(`Owned by another scenario (change there): ${named(external)}.`, `يملكها سيناريو آخر (عدّلها هناك): ${named(external)}.`));
      return lines.join(' ');
    };
    saveShape.onclick = async () => {
      const row = rows.get(select.value);
      if (!row || !scenarioId || published || saveShape.disabled) return;
      saveShape.disabled = true; alert.textContent = ''; status.textContent = '';
      let refusal = '';
      try {
        const result = await request(`/ops/tw/scenarios/${scenarioId}/budget/meeting-pattern/`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ course_key: row.course_key, meeting_pattern: shape.value }),
        }, message => { refusal = typeof message === 'string' ? message : ''; });
        if (!result?.ok) {
          alert.textContent = refusal || t('Could not save the weekly meetings. Check the scenario status, then retry.', 'تعذر حفظ اللقاءات الأسبوعية. تحقق من حالة السيناريو ثم أعد المحاولة.');
          return;
        }
        // The answer's budget is the scenario's own, this course's new row in it.
        (Array.isArray(result.budget) ? result.budget : []).forEach(fresh => rows.set(fresh.course_key, fresh));
        showWeekly();
        status.textContent = savedWords(rows.get(row.course_key), result);
        await onSaved(result.budget, { action: 'meeting_pattern' });
      } catch (error) { alert.textContent = error.message; }
      finally {
        saveShape.disabled = false;
        // A button disabled while it has the keyboard loses it to the page;
        // the dialog stays open, so the keyboard comes back to the button.
        if (dialog.isConnected && !dialog.contains(document.activeElement)) saveShape.focus();
      }
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
      save.disabled = true; alert.textContent = '';
      try {
        const body = { course_key: select.value || code.value.trim().toUpperCase(),
          course_code: code.value, course_name: name.value, credit_hours: hours.value,
          max_per_section: capacity.value, programme_term: term.value, sections: count.value };
        // A new course's weekly meetings go with it. The standard pattern is
        // the server's default and is not sent, so a typed code already in
        // the plan only gains sections, as before.
        if (!select.value && pattern.value) body.meeting_pattern = pattern.value;
        // A refusal is said in the dialog, in the server's words (a toast would
        // sit under the modal dialog): e.g. a typed code already in the plan
        // with other weekly meetings, which its own Save changes.
        let refusal = '';
        const result = await request(`/ops/tw/scenarios/${scenarioId}/budget/`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        }, message => { refusal = typeof message === 'string' ? message : ''; });
        if (!result?.ok) { alert.textContent = refusal || t('Could not add. Check the values and scenario status, then retry.', 'تعذرت الإضافة. تحقق من القيم وحالة السيناريو ثم أعد المحاولة.'); return; }
        await onSaved(result.budget); dialog.close();
      } catch (error) { alert.textContent = error.message; }
      finally { save.disabled = false; }
    };
    dialog.append(form); document.body.append(dialog);
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    dialog.showModal();
  },
};
