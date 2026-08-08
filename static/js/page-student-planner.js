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
  if (!root || !window.WeekGrid) return;

  const AR = String(root.dataset.language || '').toLowerCase().startsWith('ar');
  const draftId = root.dataset.draftId;
  const base = '/student/planner/drafts/' + encodeURIComponent(draftId) + '/';
  const q = (id) => document.getElementById(id);
  const els = {
    term: q('spTerm'), courseCount: q('spCourseCount'), creditCount: q('spCreditCount'),
    creditCeiling: q('spCreditCeiling'), currentEmpty: q('spCurrentEmpty'),
    currentGrid: q('spCurrentGrid'), search: q('spCourseSearch'), catalog: q('spCatalog'),
    catalogEmpty: q('spCatalogEmpty'), requested: q('spRequested'),
    requestedEmpty: q('spRequestedEmpty'), keep: q('spKeep'), rebuild: q('spRebuild'),
    confirm: q('spConfirm'), confirmText: q('spConfirmText'), confirmBtn: q('spConfirmBtn'),
    confirmCancel: q('spConfirmCancel'), generate: q('spGenerate'), options: q('spOptions'),
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
  const COLORS = ['#167d78', '#315da8', '#8a5aa8', '#b86b32', '#3f7f4f', '#9a4767'];

  let data = null;
  let selectedCodes = [];
  let fixedSections = {};
  let filter = 'all';
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
    els.generate.disabled = state || selectedCodes.length === 0;
    els.generate.setAttribute('aria-busy', state ? 'true' : 'false');
  }
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }
  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text != null) item.textContent = String(text);
    return item;
  }
  function catalogByCode(code) {
    return ((data && data.workspace && data.workspace.catalog) || [])
      .find((row) => row.course_code === code);
  }
  function selectedCredits() {
    return selectedCodes.reduce((sum, code) => sum + Number((catalogByCode(code) || {}).credits || 0), 0);
  }
  function courseColor(code) {
    let hash = 0;
    String(code || '').split('').forEach((char) => { hash = ((hash << 5) - hash) + char.charCodeAt(0); });
    return COLORS[Math.abs(hash) % COLORS.length];
  }

  function renderWeek(host, meetings, emptyMessage) {
    const blocks = (meetings || []).filter((meeting) => meeting.day && meeting.start && meeting.end);
    host.innerHTML = window.WeekGrid.renderWeekGrid({
      mode: 'blocks',
      blocks: blocks,
      days: ['SUN', 'MON', 'TUE', 'WED', 'THU'],
      dayLabels: DAY_LABELS,
      timeLabel: T.time,
      empty: '<p class="sp-muted mb-0">' + esc(emptyMessage || '') + '</p>',
      bg: () => 'color-mix(in srgb, var(--surface) 82%, var(--brand) 18%)',
      accent: (meeting) => courseColor(meeting.course_code),
      cellHtml: (meeting) => '<span class="wg-cid">' + esc(meeting.course_code) + '</span>' +
        '<span class="wg-meta">' + esc(meeting.section || '') + '<br>' + esc(meeting.start) + '–' + esc(meeting.end) + '</span>',
    });
  }

  function renderCurrent(workspace) {
    const rows = workspace.current_timetable || [];
    const meetings = rows.filter((row) => row.day && row.start && row.end);
    els.currentEmpty.hidden = rows.length > 0;
    renderWeek(els.currentGrid, meetings, AR ? 'لا توجد أوقات حالية لعرضها.' : 'No current meeting times to show.');
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
      if (filter === 'recommended' && !course.recommended) return false;
      if (filter === 'ready' && course.status !== 'ready') return false;
      return true;
    });
    els.catalog.replaceChildren();
    els.catalogEmpty.hidden = catalog.length > 0;
    catalog.forEach((course) => {
      const selected = selectedCodes.includes(course.course_code);
      const unavailable = course.status !== 'ready' && !selected;
      const card = node('article', 'sp-course-card' + (selected ? ' is-selected' : '') + (unavailable ? ' is-disabled' : ''));
      const main = node('div', 'sp-course-main');
      const title = node('div', 'sp-course-title');
      title.appendChild(node('strong', 'sp-code', course.course_code));
      if (course.recommended) title.appendChild(node('span', 'sp-tag sp-tag-rec', T.recommended));
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
      const button = node('button', selected ? 'btn btn-sm btn-outline-danger' : 'btn btn-sm btn-outline-primary', selected ? T.remove : T.add);
      button.type = 'button';
      button.disabled = busy || unavailable;
      button.addEventListener('click', () => toggleCourse(course.course_code));
      card.appendChild(button);
      els.catalog.appendChild(card);
    });
  }

  function renderRequested() {
    els.requested.replaceChildren();
    els.requestedEmpty.hidden = selectedCodes.length > 0;
    selectedCodes.forEach((code) => {
      const course = catalogByCode(code) || { course_code: code, course_name: '', sections: [] };
      const item = node('li', 'sp-requested-item');
      const label = node('div', 'sp-requested-label');
      label.appendChild(node('strong', 'sp-code', code));
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
          (DAY_LABELS[meeting.day] || meeting.day) + ' ' + meeting.start + '–' + meeting.end
        ).join(' · ');
        option.textContent = section.label + (times ? ' — ' + times : '');
        option.selected = Number(fixedSections[code]) === Number(section.id);
        select.appendChild(option);
      });
      select.disabled = busy || !(course.sections || []).length;
      select.addEventListener('change', () => pinSection(code, select.value));
      item.appendChild(select);

      const remove = node('button', 'btn btn-sm btn-link text-danger', T.remove);
      remove.type = 'button';
      remove.disabled = busy;
      remove.addEventListener('click', () => toggleCourse(code));
      item.appendChild(remove);
      els.requested.appendChild(item);
    });
    els.courseCount.textContent = String(selectedCodes.length);
    els.creditCount.textContent = String(selectedCredits());
    els.generate.disabled = busy || selectedCodes.length === 0;
  }

  function renderUnplaced(unplaced) {
    els.unplaced.replaceChildren();
    els.unplacedBox.hidden = !(unplaced || []).length;
    (unplaced || []).forEach((row) => {
      const item = node('li');
      item.appendChild(node('strong', 'sp-code', row.course_code));
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
    const caption = node('caption', 'sp-visually-hidden', T.option + ' ' + (index + 1));
    table.appendChild(caption);
    const head = document.createElement('thead');
    const row = document.createElement('tr');
    [T.course, T.section, T.day, T.from, T.to].forEach((label) => {
      const th = node('th', '', label); th.scope = 'col'; row.appendChild(th);
    });
    head.appendChild(row); table.appendChild(head);
    const body = document.createElement('tbody');
    (option.meetings || []).forEach((meeting) => {
      const tr = document.createElement('tr');
      tr.appendChild(node('td', '', meeting.course_code + (meeting.course_name ? ' — ' + meeting.course_name : '')));
      tr.appendChild(node('td', '', meeting.section));
      tr.appendChild(node('td', '', DAY_LABELS[meeting.day] || meeting.day));
      tr.appendChild(node('td', '', meeting.start));
      tr.appendChild(node('td', '', meeting.end));
      body.appendChild(tr);
    });
    table.appendChild(body); wrap.appendChild(table); details.appendChild(wrap);
    return details;
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
      setTimeout(() => { button.textContent = T.copy; }, 1800);
    } catch (_) {
      say(T.copyFail + '\n' + value);
    }
  }

  function renderOption(option, index) {
    const card = node('article', 'sp-option');
    const header = node('header', 'sp-option-head');
    const title = node('div');
    const heading = node('h3', 'h6 mb-1', T.option + ' ' + (index + 1));
    heading.id = 'spOption' + index;
    card.setAttribute('aria-labelledby', heading.id);
    title.appendChild(heading);
    title.appendChild(node('span', 'sp-muted', String(option.credit_hours || 0) + ' ' + T.credits));
    header.appendChild(title);
    const copy = node('button', 'btn btn-sm btn-outline-primary', T.copy);
    copy.type = 'button';
    copy.addEventListener('click', () => copyChecklist(option, copy));
    header.appendChild(copy);
    card.appendChild(header);

    const facts = node('div', 'sp-option-facts');
    [[T.days, option.days_on_campus], [T.earliest, option.earliest_start || '—'], [T.latest, option.latest_end || '—']]
      .forEach(([label, value]) => {
        const fact = node('div'); fact.appendChild(node('span', '', label)); fact.appendChild(node('strong', '', value)); facts.appendChild(fact);
      });
    card.appendChild(facts);
    const grid = node('div', 'sp-week sp-option-week');
    renderWeek(grid, option.meetings || [], AR ? 'لا توجد أوقات لعرضها.' : 'No meeting times to show.');
    card.appendChild(grid);
    card.appendChild(renderDetails(option, index));
    return card;
  }

  function renderOptions(options) {
    els.options.replaceChildren();
    els.optionsEmpty.hidden = (options || []).length > 0;
    if (!(options || []).length) {
      els.optionsEmpty.textContent = data && data.draft && data.draft.has_current_generation
        ? T.none
        : (AR ? 'ابنِ الخيارات لعرض الجداول الممكنة هنا.' : 'Build options to see possible timetables here.');
    }
    (options || []).forEach((option, index) => els.options.appendChild(renderOption(option, index)));
  }

  function render(payload) {
    data = payload;
    const draft = payload.draft || {};
    const workspace = payload.workspace || {};
    selectedCodes = (draft.requested || []).map((row) => row.course_code);
    fixedSections = {};
    (draft.requested || []).forEach((row) => { if (row.fixed_section_id) fixedSections[row.course_code] = row.fixed_section_id; });
    els.term.textContent = draft.academic_year + '/' + draft.term;
    els.creditCeiling.textContent = String(workspace.credit_ceiling || '—');
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
    const result = await post(base + 'edit/', {
      course_codes: nextCodes,
      fixed_sections: nextPins,
      keep_current_sections: els.keep.checked,
    });
    setBusy(false);
    if (!result.ok) { say(result.body.error || T.editFail); await load(); return false; }
    render(result.body); say(T.refreshPrompt); return true;
  }

  function toggleCourse(code) {
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
    const result = await post(base + 'edit/', { keep_current_sections: keep });
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
    if (!selectedCodes.length) { say(T.chooseOne); return; }
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
