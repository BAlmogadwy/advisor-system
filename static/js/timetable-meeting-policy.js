/* Presentation of backend-owned fixed English weekly patterns. */
(() => {
  const days = ['SUN', 'MON', 'WED', 'THU'];
  const patterns = [
    { pattern_id: 'ENG_INTENSIVE_V1_AM', label: 'Morning 09:00–12:00', start: '09:00', end: '12:00' },
    { pattern_id: 'ENG_INTENSIVE_V1_PM', label: 'Afternoon 13:00–16:00', start: '13:00', end: '16:00' },
  ].map(pattern => ({ ...pattern, meetings: days.map(day => ({ day, start_time: pattern.start, end_time: pattern.end })) }));
  const isEnglish = item => {
    const normalise = value => String(value || '').toUpperCase().replace(/\s/g, '');
    const key = normalise(item?.course_key), number = normalise(item?.course_number);
    let code = normalise(typeof item === 'string' ? item : item?.course_code);
    if (key.includes('::')) code = key.split('::')[0];
    else if (code.includes('::')) code = code.split('::')[0];
    else if (!/\d/.test(code)) code = ['ENG101', 'ENG102'].includes(key) ? key : number ? (number.startsWith(code) ? number : code + number) : code;
    return ['ENG101', 'ENG102'].includes(code);
  };
  const identify = rows => patterns.find(pattern => rows?.length === 4 && pattern.meetings.every(meeting => rows.some(row =>
    row.day === meeting.day && (row.start_time || row.start) === meeting.start_time && (row.end_time || row.end) === meeting.end_time)))?.pattern_id || null;
  const label = (placement, arabic = false) => {
    const pattern = patterns.find(row => row.start === placement?.start_time && row.end === placement?.end_time);
    // The time range is isolated left-to-right (LRI … PDI): inside an Arabic
    // sentence "13:00–16:00" was otherwise painted as "16:00–13:00".
    return pattern ? (arabic ? `${pattern.start === '09:00' ? 'صباحي' : 'مسائي'} \u2066${pattern.start}–${pattern.end}\u2069` : pattern.label)
      : (arabic ? 'موعد محفوظ' : 'Saved meeting time');
  };
  const roomLabel = (placement, arabic = false, empty = '—') => isEnglish(placement)
    ? (arabic ? 'لا يحتاج إلى قاعة' : 'No room required') : (placement?.room || empty);
  const gridSlots = rows => [...new Map([...patterns.map(pattern => ({ start: pattern.start, end: pattern.end })), ...(rows || []).map(row => ({ start: row.start_time, end: row.end_time }))]
    .filter(row => row.start && row.end).map(row => [`${row.start}|${row.end}`, row])).values()].sort((a, b) => a.start.localeCompare(b.start));
  const snapshot = rows => (rows || []).map(row => ({ id: row.id, board_id: row.board_id, term_section_id: row.term_section_id,
    day: row.day, start_time: row.start_time, end_time: row.end_time, room: row.room || '', is_locked: !!row.is_locked }));
  const remapHistory = (actions, before, after) => {
    const mapping = new Map();
    (before || []).forEach(row => {
      const replacement = (after || []).find(item => item.day === row.day && String(item.board_id) === String(row.board_id));
      if (row.id != null && replacement?.id != null) mapping.set(row.id, replacement.id);
    });
    const visit = value => {
      if (!value || typeof value !== 'object') return;
      if (Array.isArray(value)) { value.forEach(visit); return; }
      for (const [key, item] of Object.entries(value)) {
        if (['id', 'placement_id', 'restored_placement_id'].includes(key) && mapping.has(item)) value[key] = mapping.get(item);
        else if (key === 'placement_ids' && Array.isArray(item)) value[key] = item.map(id => mapping.get(id) ?? id);
        else visit(item);
      }
    };
    visit(actions);
  };
  async function choose(course, { arabic = false, candidates = null, currentPatternId = null, boards = [] } = {}) {
    const previous = document.activeElement;
    const dialog = document.createElement('dialog'); dialog.className = 'tw-pattern-dialog';
    const heading = document.createElement('h2'); heading.id = 'twPatternDialogHeading';
    heading.textContent = `${course} · ${arabic ? 'نمط الشعبة الأسبوعي' : 'Weekly section pattern'}`;
    dialog.setAttribute('aria-labelledby', heading.id); dialog.appendChild(heading);
    const description = document.createElement('p');
    description.textContent = arabic ? 'الأحد، الاثنين، الأربعاء، الخميس · ثلاث ساعات متصلة يومياً. يتم نقل اللقاءات الأربعة معاً.' : 'Sun, Mon, Wed, Thu · three continuous hours each day. All four meetings change together.';
    dialog.appendChild(description);
    let boardSelect = null;
    if (boards.length > 1) {
      const boardLabel = document.createElement('label'); boardLabel.className = 'tw-pattern-board';
      boardLabel.textContent = arabic ? 'اللوحة ' : 'Board ';
      boardSelect = document.createElement('select');
      boards.forEach(board => { const option = document.createElement('option'); option.value = String(board.id); option.textContent = board.label; boardSelect.appendChild(option); });
      boardLabel.appendChild(boardSelect); dialog.appendChild(boardLabel);
    }
    const choices = document.createElement('div'); choices.className = 'tw-pattern-choices'; dialog.appendChild(choices);
    return new Promise(resolve => {
      let finished = false;
      const finish = result => { if (finished) return; finished = true; dialog.close(); dialog.remove(); previous?.focus(); resolve(result); };
      patterns.forEach(pattern => {
        const button = document.createElement('button'); button.type = 'button';
        button.className = 'tw-pattern-choice'; button.dataset.patternId = pattern.pattern_id;
        const optionLabel = `${label({ start_time: pattern.start, end_time: pattern.end }, arabic)}${currentPatternId === pattern.pattern_id ? (arabic ? ' · الحالي' : ' · Current') : ''}`;
        const board = () => boards.find(row => String(row.id) === boardSelect?.value) || boards[0];
        const available = () => board()?.candidates || candidates;
        const evidence = () => available()?.find(row => row.pattern_id === pattern.pattern_id);
        const update = () => {
          const row = evidence();
          button.disabled = (Array.isArray(available()) && !row) || row?.allowed === false || row?.disabled === true || row?.status === 'blocked';
          const unavailable = button.disabled ? (arabic ? 'غير متاح في هذه اللوحة' : 'Unavailable on this board') : '';
          const conflicts = row?.critical_count ? (arabic ? `${row.critical_count} تعارض` : `${row.critical_count} conflict(s)`) : '';
          button.textContent = [optionLabel, unavailable || conflicts].filter(Boolean).join(' · ');
          button.title = row?.reason || row?.detail || row?.primary_reason?.detail || unavailable;
        };
        update(); boardSelect?.addEventListener('change', update);
        button.addEventListener('click', () => finish({ ...pattern, candidate: evidence(), ...(boards.length ? { boardId: board()?.id } : {}) })); choices.appendChild(button);
      });
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'tw-pattern-cancel';
      cancel.textContent = arabic ? 'إلغاء' : 'Cancel'; cancel.addEventListener('click', () => finish(null)); dialog.appendChild(cancel);
      dialog.addEventListener('cancel', event => { event.preventDefault(); finish(null); });
      document.body.appendChild(dialog); dialog.showModal(); choices.querySelector('button:not(:disabled)')?.focus();
    });
  }
  globalThis.TimetableMeetingPolicy = { patterns, days, isEnglish, identify, label, roomLabel, gridSlots, snapshot, remapHistory, choose };
})();
