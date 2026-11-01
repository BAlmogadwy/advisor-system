/* Extend a saved scenario's course/section plan, then use its usual placement UI. */
globalThis.TimetableCourseEditor = {
  open({ scenarioId, published, budget, request, onSaved, arabic = false }) {
    const t = (en, ar) => arabic ? ar : en;
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
    const field = (labelText, input) => {
      const label = document.createElement('label');
      label.textContent = labelText;
      input.style.cssText = 'display:block;width:100%;padding:8px;color:inherit;background:inherit;border:1px solid #94a3b8;border-radius:5px';
      label.append(input); form.append(label); return input;
    };
    const select = document.createElement('select');
    select.add(new Option(t('New course', 'مقرر جديد'), ''));
    budget.forEach(row => select.add(new Option(`${row.course_code} · ${row.course_name || row.course_key}`, row.course_key)));
    field(t('Course', 'المقرر'), select);
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
    const capacity = number(t('Section capacity', 'سعة الشعبة'), 40, 1, 1000);
    const term = number(t('Programme term (optional)', 'المستوى (اختياري)'), '', 1, 20); term.required = false;
    const count = number(t('Sections to add', 'عدد الشعب المضافة'), 1, 1, 100);
    select.onchange = () => {
      [code, name, hours, capacity, term].forEach(input => {
        input.disabled = !!select.value; input.parentElement.hidden = !!select.value;
      });
    };
    const alert = document.createElement('p'); alert.setAttribute('role', 'alert'); form.append(alert);
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
        const result = await request(`/ops/tw/scenarios/${scenarioId}/budget/`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ course_key: select.value || code.value.trim().toUpperCase(),
            course_code: code.value, course_name: name.value, credit_hours: hours.value,
            max_per_section: capacity.value, programme_term: term.value, sections: count.value }),
        });
        if (!result?.ok) { alert.textContent = t('Could not add. Check the values and scenario status, then retry.', 'تعذرت الإضافة. تحقق من القيم وحالة السيناريو ثم أعد المحاولة.'); return; }
        await onSaved(result.budget); dialog.close();
      } catch (error) { alert.textContent = error.message; }
      finally { save.disabled = false; }
    };
    dialog.append(form); document.body.append(dialog);
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    dialog.showModal();
  },
};
