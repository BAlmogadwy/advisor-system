/* Student timetable proposal workspace.
 *
 * The scheduling engine is the existing planner_builder through the shared
 * student_planner adapter.  This file only manages the signed-in student's
 * temporary draft and renders the answer.  There is deliberately no save,
 * apply, registration, capacity override, or student-id control.
 */
(function () {
  'use strict';

  const root = document.querySelector('.sp-layout');
  if (!root || !window.StudentTimetable) return;

  const AR = String(root.dataset.language || '').toLowerCase().startsWith('ar');
  const draftId = root.dataset.draftId;
  const base = '/student/planner/drafts/' + encodeURIComponent(draftId) + '/';
  const q = (id) => document.getElementById(id);
  const els = {
    term: q('spTerm'), courseCount: q('spCourseCount'), creditCount: q('spCreditCount'),
    creditCeiling: q('spCreditCeiling'), currentEmpty: q('spCurrentEmpty'),
    currentSummary: q('spCurrentSummary'), currentDetails: q('spCurrentDetails'),
    currentGrid: q('spCurrentGrid'), search: q('spCourseSearch'), catalog: q('spCatalog'),
    catalogEmpty: q('spCatalogEmpty'), requested: q('spRequested'),
    requestedEmpty: q('spRequestedEmpty'), keep: q('spKeep'), rebuild: q('spRebuild'),
    confirm: q('spConfirm'), confirmText: q('spConfirmText'), confirmBtn: q('spConfirmBtn'),
    confirmCancel: q('spConfirmCancel'), generate: q('spGenerate'), options: q('spOptions'),
    optionPreview: q('spOptionPreview'),
    optionsEmpty: q('spOptionsEmpty'), unplacedBox: q('spUnplacedBox'),
    unplaced: q('spUnplaced'), status: q('spStatus'),
  };

  const T = {
    loading: AR ? 'جارٍ تحميل مساحة التخطيط…' : 'Loading your planning workspace…',
    loadFail: AR ? 'تعذّر تحميل مساحة التخطيط.' : 'Could not load the planning workspace.',
    editFail: AR ? 'تعذّر تحديث الاختيار.' : 'Could not update the selection.',
    building: AR ? 'جارٍ بناء خيارات الجدول…' : 'Building timetable options…',
    buildFail: AR ? 'تعذّر بناء خيارات الجدول.' : 'Could not build timetable options.',
    chooseOne: AR ? 'اختر مقررًا واحدًا على الأقل أولًا.' : 'Choose at least one course first.',
    anySection: AR ? 'أي شعبة مناسبة' : 'Any suitable section',
    remove: AR ? 'إزالة' : 'Remove',
    add: AR ? 'إضافة' : 'Add',
    selected: AR ? 'مختار' : 'Selected',
    inCurrent: AR ? 'في جدولك' : 'In your timetable',
    fixedCurrent: AR ? 'شعبة حالية مثبتة' : 'Current section fixed',
    proposed: AR ? 'مقترح' : 'Proposed',
    recommended: AR ? 'موصى به' : 'Recommended',
    ready: AR ? 'جاهز للتخطيط' : 'Ready to plan',
    blocked: AR ? 'متطلب سابق ناقص' : 'Missing prerequisite',
    notOffered: AR ? 'لا توجد شُعب في البيانات الحالية' : 'No sections in current data',
    missing: AR ? 'الناقص' : 'Missing',
    days: AR ? 'أيام حضور' : 'Campus days',
    earliest: AR ? 'أول محاضرة' : 'Earliest class',
    latest: AR ? 'آخر محاضرة' : 'Latest class',
    credits: AR ? 'ساعة' : 'credits',
    details: AR ? 'تفاصيل المقررات والأوقات' : 'Course and time details',
    copy: AR ? 'نسخ قائمة البوابة' : 'Copy portal checklist',
    copied: AR ? 'نُسخت القائمة. لم يتم حفظ أو تسجيل أي شيء.' : 'Checklist copied. Nothing was saved or registered.',
    copyFail: AR ? 'تعذّر النسخ تلقائيًا. حدّد القائمة وانسخها يدويًا.' : 'Automatic copy failed. Select and copy the checklist manually.',
    option: AR ? 'الخيار' : 'Option',
    coverage: AR ? 'تمت الجدولة' : 'Scheduled',
    unscheduled: AR ? 'غير مجدول' : 'not scheduled',
    complete: AR ? 'مكتمل' : 'Complete',
    partial: AR ? 'جزئي' : 'Partial',
    recordedTimesClear: AR ? 'لا تداخل بين الأوقات المسجّلة' : 'No overlap among recorded times',
    optionMissing: AR ? 'لم يدخل في هذا الخيار' : 'Not placed in this option',
    source: AR ? 'النوع' : 'Type',
    hourUnit: AR ? 'ساعة' : 'credits',
    course: AR ? 'المقرر' : 'Course',
    section: AR ? 'الشعبة' : 'Section',
    day: AR ? 'اليوم' : 'Day',
    from: AR ? 'من' : 'From',
    to: AR ? 'إلى' : 'To',
    time: AR ? 'الوقت' : 'Time',
    generated: AR ? 'تم إعداد خيارات مؤقتة على الشاشة.' : 'Temporary on-screen options are ready.',
    none: AR ? 'لم يجد المخطط جدولًا كاملًا بهذه الاختيارات.' : 'The planner could not find a complete timetable for this selection.',
    stale: AR ? 'تغيّر جدولك الحالي منذ بناء هذه الخيارات. أعد البناء لتحديثها.' : 'Your current timetable changed after these options were built. Build again to refresh them.',
    expired: AR ? 'انتهت صلاحية مساحة التخطيط. افتح مخططًا جديدًا.' : 'This planning workspace expired. Open a new planner.',
    freshWarning: AR
      ? 'سيعرض النظام اقتراحًا قد يستخدم شُعبًا مختلفة عن شُعبك الحالية. لن يتغيّر تسجيلك الحقيقي.'
      : 'The system may propose sections different from your current ones. Your real registration will not change.',
    confirmReady: AR ? 'تم التأكيد. يمكنك الآن بناء الخيارات.' : 'Confirmed. You can now build the options.',
    refreshPrompt: AR ? 'غيّرت اختيارك. اضغط «بناء خيارات الجدول» لعرض اقتراح جديد.' : 'Your selection changed. Build timetable options to see a new proposal.',
    sun: AR ? 'الأحد' : 'Sun', mon: AR ? 'الاثنين' : 'Mon', tue: AR ? 'الثلاثاء' : 'Tue',
    wed: AR ? 'الأربعاء' : 'Wed', thu: AR ? 'الخميس' : 'Thu', fri: AR ? 'الجمعة' : 'Fri', sat: AR ? 'السبت' : 'Sat',
  };
  const DAY_LABELS = { SUN: T.sun, MON: T.mon, TUE: T.tue, WED: T.wed, THU: T.thu, FRI: T.fri, SAT: T.sat };
  let data = null;
  let selectedCodes = [];
  let fixedSections = {};
  let filter = 'recommended';
  let activeOptionIndex = 0;
  let confirmation = null;
  let busy = false;

  function csrf() {
    const token = document.querySelector('[name=csrfmiddlewaretoken]');
    if (token) return token.value;
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  async function api(path, options) {
    try {
      const response = await fetch(path, Object.assign({ credentials: 'same-origin' }, options || {}));
      let body = null;
      try { body = await response.json(); } catch (_) { body = null; }
      return { ok: response.ok && body !== null, status: response.status, body: body || {} };
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

  function say(message) { els.status.textContent = message || ''; }
  function setBusy(state) {
    busy = state;
    root.classList.toggle('is-busy', state);
    els.generate.disabled = state || effectiveCodes().length === 0;
    els.generate.setAttribute('aria-busy', state ? 'true' : 'false');
  }
  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text != null) item.textContent = String(text);
    return item;
  }
  function ltr(value) { return '\u2066' + String(value == null ? '' : value) + '\u2069'; }
  function bdi(value, className) {
    const item = node('bdi', className, value);
    item.dir = 'ltr';
    return item;
  }
  function catalogByCode(code) {
    return ((data && data.workspace && data.workspace.catalog) || [])
      .find((row) => row.course_code === code);
  }
  function currentCourseMap() {
    const map = new Map();
    (((data || {}).workspace || {}).current_timetable || []).forEach((row) => {
      const code = String(row.course_code || '').replace(/\s+/g, '').toUpperCase();
      if (!code) return;
      const existing = map.get(code) || { course_code: code, course_name: row.course_name || '', section: row.section || '', credits: Number(row.credits || 0) };
      if (!existing.section && row.section) existing.section = row.section;
      if (!existing.credits && row.credits) existing.credits = Number(row.credits || 0);
      map.set(code, existing);
    });
    return map;
  }
  function effectiveCodes() {
    if (!els.keep.checked) return selectedCodes.slice();
    return Array.from(new Set(selectedCodes.concat(Array.from(currentCourseMap().keys()))));
  }
  function selectedCredits() {
    return effectiveCodes().reduce((sum, code) => {
      const current = currentCourseMap().get(code);
      return sum + Number((catalogByCode(code) || current || {}).credits || 0);
    }, 0);
  }
  function renderWeek(host, meetings, emptyMessage) {
    const blocks = (meetings || []).filter((meeting) => meeting.day && meeting.start && meeting.end);
    const hasCurrent = blocks.some((meeting) => meeting.source === 'current');
    const hasProposed = blocks.some((meeting) => meeting.source !== 'current');
    window.StudentTimetable.render(host, blocks, {
      lang: AR ? 'ar' : 'en',
      dir: AR ? 'rtl' : 'ltr',
      dayLabels: DAY_LABELS,
      timeLabel: T.time,
      emptyText: emptyMessage || '',
      currentLabel: T.fixedCurrent,
      proposedLabel: T.proposed,
      showCourseName: true,
      showSource: hasCurrent && hasProposed,
    });
  }

  function renderCurrent(workspace) {
    const rows = workspace.current_timetable || [];
    const meetings = rows.filter((row) => row.day && row.start && row.end)
      .map((row) => Object.assign({}, row, { source: 'current' }));
    const courses = Array.from(currentCourseMap().values());
    els.currentEmpty.hidden = courses.length > 0;
    els.currentSummary.replaceChildren();
    courses.forEach((course) => {
      const chip = node('div', 'sp-current-chip');
      chip.appendChild(bdi(course.course_code, 'sp-code'));
      chip.appendChild(bdi(course.section || '—', 'sp-current-section'));
      els.currentSummary.appendChild(chip);
    });
    els.currentDetails.hidden = meetings.length === 0;
    if (meetings.length) renderWeek(els.currentGrid, meetings, '');
  }

  function statusLabel(course) {
    if (course.status === 'blocked') return T.blocked;
    if (course.status === 'offering_unknown') return T.notOffered;
    return T.ready;
  }

  function renderCatalog() {
    const search = String(els.search.value || '').trim().toLowerCase();
    const catalog = ((data && data.workspace && data.workspace.catalog) || []).filter((course) => {
      const haystack = (course.course_code + ' ' + course.course_name).toLowerCase();
      if (search && !haystack.includes(search)) return false;
      if (!search && filter === 'recommended' && !course.recommended) return false;
      if (!search && filter === 'ready' && course.status !== 'ready') return false;
      return true;
    });
    els.catalog.replaceChildren();
    els.catalogEmpty.hidden = catalog.length > 0;
    catalog.forEach((course) => {
      const selected = effectiveCodes().includes(course.course_code);
      const fixedCurrent = els.keep.checked && currentCourseMap().has(course.course_code);
      const unavailable = course.status !== 'ready' && !selected;
      const card = node('article', 'sp-course-card' + (selected ? ' is-selected' : '') + (unavailable ? ' is-disabled' : ''));
      const main = node('div', 'sp-course-main');
      const title = node('div', 'sp-course-title');
      title.appendChild(bdi(course.course_code, 'sp-code'));
      if (course.recommended) title.appendChild(node('span', 'sp-tag sp-tag-rec', T.recommended));
      if (fixedCurrent) title.appendChild(node('span', 'sp-tag sp-tag-current', T.inCurrent));
      main.appendChild(title);
      main.appendChild(node('span', 'sp-name', course.course_name || ''));
      const meta = node('div', 'sp-course-meta');
      meta.appendChild(node('span', 'sp-tag ' + (course.status === 'ready' ? 'sp-tag-ready' : 'sp-tag-blocked'), statusLabel(course)));
      meta.appendChild(node('span', 'sp-muted', String(course.credits || 0) + ' ' + T.credits));
      if ((course.missing_prerequisites || []).length) {
        meta.appendChild(node('span', 'sp-muted', T.missing + ': ' + course.missing_prerequisites.join(', ')));
      }
      main.appendChild(meta);
      card.appendChild(main);
      if (!fixedCurrent) {
        const button = node('button', selected ? 'btn btn-sm btn-outline-danger' : 'btn btn-sm btn-outline-primary', selected ? T.remove : T.add);
        button.type = 'button';
        button.disabled = busy || unavailable;
        button.addEventListener('click', () => toggleCourse(course.course_code));
        card.appendChild(button);
      }
      els.catalog.appendChild(card);
    });
  }

  function renderRequested() {
    els.requested.replaceChildren();
    const codes = effectiveCodes();
    const current = currentCourseMap();
    els.requestedEmpty.hidden = codes.length > 0;
    codes.forEach((code) => {
      const course = catalogByCode(code) || { course_code: code, course_name: '', sections: [] };
      const fixedCurrent = els.keep.checked && current.has(code);
      const currentSection = fixedCurrent ? String((current.get(code) || {}).section || '') : '';
      const item = node('li', 'sp-requested-item');
      const label = node('div', 'sp-requested-label');
      const labelLine = node('div', 'sp-requested-title');
      labelLine.appendChild(bdi(code, 'sp-code'));
      if (fixedCurrent) labelLine.appendChild(node('span', 'sp-tag sp-tag-current', T.fixedCurrent));
      label.appendChild(labelLine);
      if (course.course_name) label.appendChild(node('span', 'sp-name', course.course_name));
      item.appendChild(label);

      const select = document.createElement('select');
      select.className = 'form-select form-select-sm sp-section-select';
      select.setAttribute('aria-label', (AR ? 'الشعبة المفضلة لمقرر ' : 'Preferred section for ') + code);
      const any = document.createElement('option');
      any.value = '';
      any.textContent = T.anySection;
      select.appendChild(any);
      (course.sections || []).forEach((section) => {
        const option = document.createElement('option');
        option.value = String(section.id);
        const times = (section.meetings || []).map((meeting) =>
          (DAY_LABELS[meeting.day] || meeting.day) + ' ' + ltr(meeting.start + '–' + meeting.end)
        ).join(' · ');
        option.textContent = ltr(section.label) + (times ? ' — ' + times : '');
        option.selected = fixedCurrent
          ? String(section.label || '') === currentSection
          : Number(fixedSections[code]) === Number(section.id);
        select.appendChild(option);
      });
      select.disabled = busy || fixedCurrent || !(course.sections || []).length;
      select.addEventListener('change', () => pinSection(code, select.value));
      item.appendChild(select);

      if (!fixedCurrent) {
        const remove = node('button', 'btn btn-sm btn-link text-danger', T.remove);
        remove.type = 'button';
        remove.disabled = busy;
        remove.addEventListener('click', () => toggleCourse(code));
        item.appendChild(remove);
      }
      els.requested.appendChild(item);
    });
    els.courseCount.textContent = String(codes.length);
    els.creditCount.textContent = String(selectedCredits());
    els.generate.disabled = busy || codes.length === 0;
  }

  function renderUnplaced(unplaced) {
    els.unplaced.replaceChildren();
    els.unplacedBox.hidden = !(unplaced || []).length;
    (unplaced || []).forEach((row) => {
      const item = node('li');
      item.appendChild(bdi(row.course_code, 'sp-code'));
      item.appendChild(node('span', 'sp-reason', (row.course_name ? ' — ' + row.course_name + ': ' : ': ') + (row.reason || '')));
      els.unplaced.appendChild(item);
    });
  }

  function renderDetails(option, index) {
    const details = document.createElement('details');
    details.className = 'sp-option-details';
    details.appendChild(node('summary', '', T.details));
    const wrap = node('div', 'sp-grid-wrap');
    wrap.tabIndex = 0;
    const table = node('table', 'sp-grid');
    const caption = node('caption', 'sp-visually-hidden', optionLabel(option, index));
    table.appendChild(caption);
    const head = document.createElement('thead');
    const row = document.createElement('tr');
    [T.course, T.section, T.day, T.from, T.to, T.source].forEach((label) => {
      const th = node('th', '', label); th.scope = 'col'; row.appendChild(th);
    });
    head.appendChild(row); table.appendChild(head);
    const body = document.createElement('tbody');
    (option.meetings || []).forEach((meeting) => {
      const tr = document.createElement('tr');
      const courseCell = node('td');
      courseCell.appendChild(bdi(meeting.course_code, 'sp-code'));
      if (meeting.course_name) courseCell.appendChild(node('span', '', ' — ' + meeting.course_name));
      tr.appendChild(courseCell);
      tr.appendChild(bdiCell(meeting.section));
      tr.appendChild(node('td', '', DAY_LABELS[meeting.day] || meeting.day));
      tr.appendChild(bdiCell(meeting.start));
      tr.appendChild(bdiCell(meeting.end));
      tr.appendChild(node('td', '', meeting.source === 'current' ? T.inCurrent : T.proposed));
      body.appendChild(tr);
    });
    table.appendChild(body); wrap.appendChild(table); details.appendChild(wrap);
    return details;
  }

  function bdiCell(value) {
    const cell = node('td');
    cell.appendChild(bdi(value));
    return cell;
  }

  function optionLabel(option, index) {
    const names = (option.planner_options || []).filter(Boolean);
    return names.length ? T.option + ' ' + names.join(' · ') : T.option + ' ' + (index + 1);
  }

  function coverage(option) {
    const scheduled = Number(option.scheduled_courses != null ? option.scheduled_courses : (option.courses || []).length);
    const target = Number(option.target_courses != null && option.target_courses > 0 ? option.target_courses : effectiveCodes().length);
    return { scheduled: scheduled, target: target, complete: target > 0 && scheduled >= target };
  }

  function checklist(option) {
    const lines = (option.courses || []).map((course) => course.course_code + ' — ' + course.section);
    const heading = AR
      ? 'قائمة نقل يدوية إلى بوابة الجامعة (ليست تسجيلًا):'
      : 'Manual university-portal checklist (not a registration):';
    return heading + '\n' + lines.join('\n');
  }

  async function copyChecklist(option, button) {
    const value = checklist(option);
    const originalLabel = button.textContent;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        const area = document.createElement('textarea');
        area.value = value; area.setAttribute('readonly', ''); area.className = 'sp-copy-buffer';
        document.body.appendChild(area); area.select();
        if (!document.execCommand('copy')) throw new Error('copy refused');
        area.remove();
      }
      button.textContent = AR ? 'تم النسخ' : 'Copied';
      say(T.copied);
      setTimeout(() => { button.textContent = originalLabel; }, 1800);
    } catch (_) {
      say(T.copyFail + '\n' + value);
    }
  }

  function appendOptionMissing(card, option) {
    if (!(option.unplaced || []).length) return;
    const box = node('div', 'sp-option-missing');
    box.appendChild(node('strong', '', T.optionMissing));
    const list = node('ul', 'sp-unplaced');
    (option.unplaced || []).forEach((row) => {
      const item = node('li');
      item.appendChild(bdi(row.course_code, 'sp-code'));
      item.appendChild(node('span', 'sp-reason', (row.course_name ? ' — ' + row.course_name + ': ' : ': ') + (row.reason || '')));
      list.appendChild(item);
    });
    box.appendChild(list);
    card.appendChild(box);
  }

  function renderOptionPreview(option, index) {
    const card = node('article', 'sp-option sp-option-preview-card');
    const header = node('header', 'sp-option-head');
    const title = node('div');
    const heading = node('h3', 'h6 mb-1', optionLabel(option, index));
    heading.id = 'spOption' + index;
    card.setAttribute('aria-labelledby', heading.id);
    title.appendChild(heading);
    const resultCoverage = coverage(option);
    const coverageLine = node('div', 'sp-option-coverage');
    coverageLine.appendChild(node('span', resultCoverage.complete ? 'sp-tag sp-tag-ready' : 'sp-tag sp-tag-warning', resultCoverage.complete ? T.complete : T.partial));
    coverageLine.appendChild(node('span', 'sp-muted', T.coverage + ' ' + resultCoverage.scheduled + '/' + resultCoverage.target + ' · ' + String(option.credit_hours || 0) + ' ' + T.hourUnit));
    title.appendChild(coverageLine);
    header.appendChild(title);
    const copy = node('button', 'btn btn-sm btn-outline-primary', T.copy + ' (' + resultCoverage.scheduled + '/' + resultCoverage.target + ')');
    copy.type = 'button';
    copy.addEventListener('click', () => copyChecklist(option, copy));
    header.appendChild(copy);
    card.appendChild(header);

    const facts = node('div', 'sp-option-facts');
    [[T.days, option.days_on_campus], [T.earliest, ltr(option.earliest_start || '—')], [T.latest, ltr(option.latest_end || '—')]]
      .forEach(([label, value]) => {
        const fact = node('div'); fact.appendChild(node('span', '', label)); fact.appendChild(node('strong', '', value)); facts.appendChild(fact);
      });
    card.appendChild(facts);
    const legend = node('div', 'sp-option-legend');
    if ((option.meetings || []).some((meeting) => meeting.source === 'current')) {
      legend.appendChild(node('span', 'sp-legend-current', T.fixedCurrent));
    }
    if ((option.meetings || []).some((meeting) => meeting.source !== 'current')) {
      legend.appendChild(node('span', 'sp-legend-proposed', T.proposed));
    }
    legend.appendChild(node('span', 'sp-recorded-clear', T.recordedTimesClear));
    card.appendChild(legend);
    appendOptionMissing(card, option);
    const grid = node('div', 'sp-week sp-option-week');
    grid.tabIndex = 0;
    renderWeek(grid, option.meetings || [], AR ? 'لا توجد أوقات لعرضها.' : 'No meeting times to show.');
    card.appendChild(grid);
    card.appendChild(node('p', 'sp-scroll-hint', AR ? 'مرّر أفقيًا لعرض بقية الأيام عند الحاجة.' : 'Scroll horizontally to see the remaining days when needed.'));
    card.appendChild(renderDetails(option, index));
    return card;
  }

  function renderOptionChoice(option, index, options) {
    const resultCoverage = coverage(option);
    const button = node('button', 'sp-option-choice' + (index === activeOptionIndex ? ' is-active' : ''));
    button.type = 'button';
    button.setAttribute('aria-pressed', index === activeOptionIndex ? 'true' : 'false');
    button.appendChild(node('strong', 'sp-option-choice-title', optionLabel(option, index)));
    button.appendChild(node('span', resultCoverage.complete ? 'sp-tag sp-tag-ready' : 'sp-tag sp-tag-warning', resultCoverage.complete ? T.complete : T.partial));
    button.appendChild(node('span', 'sp-option-choice-coverage', T.coverage + ' ' + resultCoverage.scheduled + '/' + resultCoverage.target));
    button.appendChild(node('span', 'sp-muted', String(option.credit_hours || 0) + ' ' + T.hourUnit + ' · ' + String(option.days_on_campus || 0) + ' ' + T.days));
    if ((option.unplaced || []).length) button.appendChild(node('span', 'sp-option-choice-missing', T.unscheduled + ' ' + option.unplaced.length));
    button.addEventListener('click', () => {
      activeOptionIndex = index;
      renderOptions(options, false);
    });
    return button;
  }

  function renderOptions(options, resetActive) {
    const rows = options || [];
    if (resetActive !== false) activeOptionIndex = 0;
    if (activeOptionIndex >= rows.length) activeOptionIndex = 0;
    els.options.replaceChildren();
    els.optionPreview.replaceChildren();
    els.optionsEmpty.hidden = rows.length > 0;
    if (!rows.length) {
      els.optionsEmpty.textContent = data && data.draft && data.draft.has_current_generation
        ? T.none
        : (AR ? 'ابنِ الخيارات لعرض الجداول الممكنة هنا.' : 'Build options to see possible timetables here.');
      return;
    }
    rows.forEach((option, index) => els.options.appendChild(renderOptionChoice(option, index, rows)));
    els.optionPreview.appendChild(renderOptionPreview(rows[activeOptionIndex], activeOptionIndex));
  }

  function render(payload) {
    data = payload;
    const draft = payload.draft || {};
    const workspace = payload.workspace || {};
    selectedCodes = (draft.requested || []).map((row) => row.course_code);
    fixedSections = {};
    (draft.requested || []).forEach((row) => { if (row.fixed_section_id) fixedSections[row.course_code] = row.fixed_section_id; });
    els.term.textContent = draft.academic_year + '/' + draft.term;
    els.term.dir = 'ltr';
    els.creditCeiling.textContent = String(workspace.credit_ceiling || '—') + (workspace.credit_ceiling ? ' ' + T.hourUnit : '');
    els.keep.checked = !!draft.keep_current_sections;
    els.rebuild.checked = !draft.keep_current_sections;
    renderCurrent(workspace);
    renderCatalog();
    renderRequested();
    renderOptions(payload.alternatives || []);
    renderUnplaced(payload.unplaced || []);
    if (!draft.is_live) say(T.expired);
    else if (draft.is_stale) say(T.stale);
  }

  async function load() {
    say(T.loading);
    const result = await api(base, { headers: { Accept: 'application/json' } });
    if (!result.ok) { say(result.body.error || T.loadFail); return; }
    render(result.body); say('');
  }

  async function saveSelection(nextCodes, nextPins) {
    if (busy) return false;
    setBusy(true);
    confirmation = null;
    els.confirm.hidden = true;
    const codes = Array.from(new Set(nextCodes || []));
    const pins = Object.assign({}, nextPins || {});
    if (els.keep.checked) {
      currentCourseMap().forEach((_course, code) => {
        if (!codes.includes(code)) codes.push(code);
        delete pins[code];
      });
    }
    const result = await post(base + 'edit/', {
      course_codes: codes,
      fixed_sections: pins,
      keep_current_sections: els.keep.checked,
    });
    setBusy(false);
    if (!result.ok) { say(result.body.error || T.editFail); await load(); return false; }
    render(result.body); say(T.refreshPrompt); return true;
  }

  function toggleCourse(code) {
    if (els.keep.checked && currentCourseMap().has(code)) return;
    const next = selectedCodes.includes(code)
      ? selectedCodes.filter((item) => item !== code)
      : selectedCodes.concat([code]);
    const pins = Object.assign({}, fixedSections);
    if (!next.includes(code)) delete pins[code];
    saveSelection(next, pins);
  }

  function pinSection(code, rawId) {
    const pins = Object.assign({}, fixedSections);
    if (rawId) pins[code] = Number(rawId); else delete pins[code];
    saveSelection(selectedCodes.slice(), pins);
  }

  async function changeMode(keep) {
    if (busy) return;
    setBusy(true); confirmation = null; els.confirm.hidden = true;
    const codes = selectedCodes.slice();
    const pins = Object.assign({}, fixedSections);
    if (keep) {
      currentCourseMap().forEach((_course, code) => {
        if (!codes.includes(code)) codes.push(code);
        delete pins[code];
      });
    }
    const result = await post(base + 'edit/', {
      course_codes: codes,
      fixed_sections: pins,
      keep_current_sections: keep,
    });
    setBusy(false);
    if (!result.ok) { say(result.body.error || T.editFail); await load(); return; }
    render(result.body);
    if (!keep) askConfirmation(); else say(T.refreshPrompt);
  }

  function askConfirmation(serverText) {
    els.confirmText.textContent = serverText || T.freshWarning;
    els.confirm.hidden = false;
    say(els.confirmText.textContent);
    els.confirmBtn.focus();
  }

  async function confirmFresh() {
    const result = await post(base + 'confirm-rebuild/', {});
    if (!result.ok) { say(result.body.error || T.editFail); return; }
    confirmation = result.body.confirmation;
    els.confirm.hidden = true;
    els.generate.focus();
    say(T.confirmReady);
  }

  async function generate() {
    if (busy) return;
    if (!effectiveCodes().length) { say(T.chooseOne); return; }
    if (els.rebuild.checked && !confirmation) { askConfirmation(); return; }
    setBusy(true); say(T.building);
    const result = await post(base + 'generate/', confirmation ? { confirmation: confirmation } : {});
    setBusy(false);
    if (result.status === 428) { confirmation = null; askConfirmation(result.body.error); return; }
    if (!result.ok) { say(result.body.error || T.buildFail); return; }
    confirmation = null;
    render(result.body);
    say((result.body.alternatives || []).length ? T.generated : T.none);
  }

  els.search.addEventListener('input', renderCatalog);
  document.querySelectorAll('[data-sp-filter]').forEach((button) => {
    button.addEventListener('click', () => {
      filter = button.dataset.spFilter || 'all';
      document.querySelectorAll('[data-sp-filter]').forEach((item) => item.classList.toggle('is-active', item === button));
      renderCatalog();
    });
  });
  els.keep.addEventListener('change', () => { if (els.keep.checked) changeMode(true); });
  els.rebuild.addEventListener('change', () => { if (els.rebuild.checked) changeMode(false); });
  els.confirmBtn.addEventListener('click', confirmFresh);
  els.confirmCancel.addEventListener('click', () => {
    els.confirm.hidden = true; els.keep.checked = true; els.keep.focus(); changeMode(true);
  });
  els.generate.addEventListener('click', generate);
  load();
})();
