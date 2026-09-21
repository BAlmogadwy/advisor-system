/* Read-only timetable views. Exam size uses checked enrollments; study terms
 * are plan-specific. Overlap counts are
 * measured pairs from scraper enrollments, never summed into distinct students. */
(function (root) {
  'use strict';

  function enrollmentSize(course) {
    const count = course?.enrolled_count;
    if (!Number.isSafeInteger(count) || count < 0) return 'unknown';
    return count === 0 ? 'zero' : count <= 30 ? '1-30' : count <= 60 ? '31-60'
      : count <= 100 ? '61-100' : count <= 150 ? '101-150' : count <= 200 ? '151-200' : '201-plus';
  }

  function createModel(data, visibleCodes = null) {
    const byCode = new Map(), byIdentity = new Map(), allCodes = new Map();
    const selected = visibleCodes == null ? null : new Set(visibleCodes);
    let ambiguous = false;
    const identities = new Set();
    for (const entry of data?.schedule || []) {
      const code = entry.course_code, identity = entry.course_identity || code;
      if (!code || !identity || allCodes.has(code) || identities.has(identity)) ambiguous = true;
      identities.add(identity);
      const course = { ...entry, identity };
      allCodes.set(code, course);
      if (!selected || selected.has(code)) {
        byCode.set(code, course);
        byIdentity.set(identity, course);
      }
    }
    const plans = new Map();
    const addPlan = program => {
      if (typeof program === 'string' && program.trim() && !plans.has(program)) plans.set(program, new Map());
      return plans.get(program);
    };
    for (const course of byCode.values()) {
      for (const program of course.programs || []) addPlan(program)?.set(course.identity, new Set());
    }
    for (const bucket of data?.buckets_summary || []) {
      const members = (bucket.courses || []).map(code => byCode.get(code)).filter(Boolean);
      if (!members.length) continue;
      const plan = addPlan(bucket.program);
      if (!plan) continue;
      const term = Number(bucket.programme_term);
      for (const course of members) {
        if (!plan.has(course.identity)) plan.set(course.identity, new Set());
        if (Number.isSafeInteger(term) && term > 0) plan.get(course.identity).add(term);
      }
    }
    const review = data?.exam_review;
    let overlapsAvailable = !ambiguous && data?.enrollment_source === 'scraper_timetable'
      && review?.version === 1 && review.enrollment_source === 'scraper_timetable'
      && Array.isArray(review.student_overlaps);
    const overlaps = new Map([...byIdentity.keys()].map(identity => [identity, new Map()]));
    if (overlapsAvailable) {
      for (const edge of review.student_overlaps) {
        const a = allCodes.get(edge?.course_a), b = allCodes.get(edge?.course_b), count = edge?.shared_students;
        if (!a || !b || a.identity === b.identity || !Number.isSafeInteger(count) || count <= 0) {
          overlapsAvailable = false;
          break;
        }
        if (!byIdentity.has(a.identity) || !byIdentity.has(b.identity)) continue;
        const previous = overlaps.get(a.identity).get(b.identity);
        // Conflicting duplicate records cannot be presented as a measured count.
        if (previous !== undefined && previous !== count) { overlapsAvailable = false; break; }
        overlaps.get(a.identity).set(b.identity, count);
        overlaps.get(b.identity).set(a.identity, count);
      }
    }
    if (!overlapsAvailable) overlaps.forEach(neighbors => neighbors.clear());
    return { byCode, byIdentity, plans, overlaps, overlapsAvailable,
      enrollmentCountsAvailable: data?.enrollment_source === 'scraper_timetable', slots: data?.slots || [] };
  }

  function relationship(source, target, slots) {
    if (!source?.day || !target?.day || source.day === 'OVERFLOW' || target.day === 'OVERFLOW') return 'unscheduled';
    if (source.day === target.day) return source.period === target.period ? 'same-period' : 'same-day';
    const days = [...new Set((slots || []).map(slot => slot.day).filter(day => day !== 'OVERFLOW'))];
    const first = days.indexOf(source.day), second = days.indexOf(target.day);
    return first >= 0 && second >= 0 && Math.abs(first - second) === 1 ? 'adjacent-day' : 'separated';
  }

  function createController({ document, isArabic = false }) {
    const $ = id => document.getElementById(id);
    const panel = $('examReviewPanel'), grid = $('schedGrid');
    const noop = { update() {}, reset() {}, refresh() {}, focusCourse() {}, getState: () => ({}) };
    if (!panel || !grid) return noop;
    const text = (en, ar) => isArabic ? ar : en;
    const state = { mode: 'size', plan: '', term: 'all', focus: null };
    let model = createModel(null), stale = false, blocked = false, planSignature = '', legendSignature = '';
    let paintedDrillRow = null, paintedDrillView = null;
    const make = (tag, className, content) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (content !== undefined) node.textContent = content;
      return node;
    };
    const termLabel = term => text(`Term ${term}`, `الفصل ${term}`);
    const relationLabel = value => ({
      'same-period': text('same period', 'الفترة نفسها'),
      'same-day': text('same day', 'اليوم نفسه'),
      'adjacent-day': text('adjacent exam day', 'يوم اختبارات مجاور'),
      'unscheduled': text('unscheduled', 'غير مجدول'),
    })[value] || '';

    function syncPlans() {
      const programs = [...model.plans.keys()].sort((a, b) => a.localeCompare(b));
      if (!model.plans.has(state.plan)) state.plan = programs.length === 1 ? programs[0] : '';
      const signature = JSON.stringify(programs);
      if (signature !== planSignature) {
        const placeholder = make('option', '', text('Choose a study plan', 'اختر الخطة الدراسية'));
        placeholder.value = '';
        $('examReviewPlan').replaceChildren(placeholder, ...programs.map(program => {
          const option = make('option', '', program); option.value = program; return option;
        }));
        planSignature = signature;
      }
      $('examReviewPlan').value = state.plan;
    }

    function paintLegend(plan) {
      const terms = [...new Set([...plan.values()].flatMap(values => [...values]))].sort((a, b) => a - b);
      if (state.term !== 'all' && !terms.includes(Number(state.term))) state.term = 'all';
      const signature = JSON.stringify([state.plan, terms]);
      if (signature !== legendSignature) {
        $('examTermLegend').replaceChildren(...['all', ...terms.map(String)].map(term => {
          const button = make('button', 'et-term-filter', term === 'all' ? text('All terms', 'كل الفصول') : termLabel(term));
          button.type = 'button'; button.dataset.examTerm = term;
          return button;
        }));
        legendSignature = signature;
      }
      for (const button of $('examTermLegend').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.examTerm === state.term));
      $('examTermLegend').hidden = state.mode !== 'term' || !state.plan || !terms.length;
    }

    function termBadge(card, course, plan, muteOthers = false) {
      if (state.mode !== 'term' || !state.plan || !course) return null;
      const terms = plan.get(course.identity);
      if (muteOthers) card.classList.toggle('et-review-muted', !terms || (state.term !== 'all' && !terms.has(Number(state.term))));
      if (!terms) return null;
      const ordered = [...terms].sort((a, b) => a - b);
      card.dataset.examTerm = terms.size === 1 ? String(ordered[0]) : terms.size ? 'multiple' : 'unknown';
      const label = terms.size ? ordered.map(term => text(`T${term}`, `ف${term}`)).join(' / ') : text('Term not recorded', 'الفصل غير مسجل');
      const badge = make('span', 'et-term-badge', label);
      badge.title = `${state.plan} · ` + (terms.size ? ordered.map(termLabel).join(' / ') : label);
      return badge;
    }

    function paintCardView(card, course, plan, muteOthers = false) {
      delete card.dataset.examTerm;
      delete card.dataset.examSize;
      card.dataset.examBaseLabel ||= card.getAttribute('aria-label') || card.title || '';
      let label = course ? `${course.course_code} — ${String(course.course_name || '').trim() || course.course_code}` : card.dataset.examBaseLabel;
      if (course) card.dataset.examBaseLabel = label;
      if (state.mode === 'size' && course) {
        const size = model.enrollmentCountsAvailable ? enrollmentSize(course) : 'unknown';
        card.dataset.examSize = size;
        label += ' · ' + (size === 'unknown'
          ? text('Enrollment count unavailable', 'عدد الطلاب المسجلين غير متاح')
          : text(`${course.enrolled_count} enrolled student${course.enrolled_count === 1 ? '' : 's'} · last checked enrollments`, `عدد الطلاب المسجلين: ${course.enrolled_count} · التسجيلات في آخر تحقق`));
      }
      // The exact count is available without adding another line to every card.
      card.title = label;
      card.setAttribute('aria-label', label);
      return termBadge(card, course, plan, muteOthers);
    }

    function paint() {
      panel.hidden = !model.byCode.size;
      syncPlans();
      if (!model.byIdentity.has(state.focus)) state.focus = null;
      $('examReviewMode').value = state.mode;
      $('examReviewPlanField').hidden = state.mode !== 'term';
      const plan = model.plans.get(state.plan) || new Map();
      paintLegend(plan);
      $('examSizeLegend').hidden = state.mode !== 'size';
      const sizes = new Set([...model.byCode.values()].map(course => model.enrollmentCountsAvailable ? enrollmentSize(course) : 'unknown'));
      $('examSizeZero').hidden = !sizes.has('zero');
      $('examSizeUnknown').hidden = !sizes.has('unknown');
      const usable = model.overlapsAvailable && !blocked;
      const source = usable ? model.byIdentity.get(state.focus) : null;
      const neighbors = source ? model.overlaps.get(source.identity) : new Map();
      grid.classList.toggle('et-review-students', state.mode === 'students');
      const help = state.mode === 'size'
        ? text('Colours group exams by student count for the selected groups. Hover for the exact count from the last check.', 'تصنّف الألوان الاختبارات حسب عدد الطلاب ضمن المجموعات المحددة. مرّر المؤشر لعرض العدد الدقيق من آخر تحقق.')
        : state.mode === 'term'
        ? !state.plan ? text('Choose a study plan to see course terms.', 'اختر الخطة الدراسية لعرض فصول المقررات.')
          : text('Colours and labels identify study terms. Select a term to highlight it; other plans are subdued.', 'تحدد الألوان والتسميات الفصول الدراسية. اختر فصلاً لإبرازه؛ تظهر الخطط الأخرى بلون خافت.')
        : state.mode === 'students'
          ? text('Use the related-students icon on an exam to see its shared students and spacing.', 'استخدم أيقونة الطلاب المشتركين على الاختبار لعرض الطلاب المشتركين وتباعد الاختبارات.')
          : text('Neutral cards. Warning symbols remain visible.', 'بطاقات محايدة. تبقى رموز التنبيه ظاهرة.');
      $('examReviewHelp').textContent = help;
      $('examRelatedSummary').hidden = state.mode !== 'students' || !source;
      $('examRelatedClear').hidden = state.mode !== 'students' || !source;
      $('examRelatedSummary').textContent = source
        ? `${source.course_code} — ${source.course_name || source.course_code} · ` + text(`${neighbors.size} related exams`, `${neighbors.size} اختبارات مرتبطة`) : '';
      const status = [];
      if (state.mode === 'students' && !usable) status.push(text('Shared-student data is unavailable. Check changes to refresh it.', 'بيانات الطلاب المشتركين غير متاحة. تحقق من التغييرات لتحديثها.'));
      if (blocked) status.push(text('Review the course source and rebuild before using student relationships.', 'راجع مصدر المقررات وأعد البناء قبل استخدام علاقات الطلاب.'));
      else if (stale && usable) status.push(text('Based on last checked enrollments; spacing reflects your edits. Check changes to refresh.', 'استناداً إلى التسجيلات في آخر تحقق؛ التباعد يعكس تعديلاتك. تحقق من التغييرات للتحديث.'));
      if (state.mode === 'term' && state.plan) {
        const missing = [...plan.values()].filter(terms => !terms.size).length;
        const multiple = [...plan.values()].filter(terms => terms.size > 1).length;
        if (missing || multiple) status.push(text(`${missing} courses without a recorded term · ${multiple} in multiple terms.`, `${missing} مقررات دون فصل مسجل · ${multiple} في أكثر من فصل.`));
      }
      if (state.mode === 'students' && source && !neighbors.size) status.push(text('No shared students with the other selected exams.', 'لا يوجد طلاب مشتركون مع الاختبارات الأخرى المحددة.'));
      if (state.mode === 'students' && source && [...grid.querySelectorAll('.et-course')].some(card => card.dataset.course === source.course_code && card.classList.contains('et-dim'))) status.push(text('The focused exam is subdued by your course search.', 'الاختبار المحدد خافت بسبب البحث عن المقررات.'));
      $('examReviewStatus').textContent = status.join(' ');
      $('examReviewStatus').hidden = !status.length;

      for (const card of grid.querySelectorAll('.et-course')) {
        const course = model.byCode.get(card.dataset.course);
        card.classList.remove('et-review-muted', 'et-related-focus', 'et-related-match', 'et-related-conflict');
        const badges = card.querySelector('.et-review-badges');
        const term = paintCardView(card, course, plan, true);
        if (!badges || !course) continue;
        const fragments = [];
        const related = card.querySelector('[data-exam-related]');
        if (related) {
          related.disabled = !usable;
          related.setAttribute('aria-pressed', String(source?.identity === course.identity));
        }
        if (term) fragments.push(term);
        if (state.mode === 'students' && source) {
          const count = neighbors.get(course.identity);
          card.classList.toggle('et-related-focus', course.identity === source.identity);
          card.classList.toggle('et-related-match', Boolean(count));
          card.classList.toggle('et-review-muted', course.identity !== source.identity && !count);
          if (count) {
            const placement = relationship(source, course, model.slots), relation = relationLabel(placement);
            const badge = make('span', 'et-related-count', text(`${count} shared`, `${count} مشترك`) + (relation ? ` · ${relation}` : ''));
            badge.title = text(`${count} students take both ${source.course_code} and ${course.course_code}.`, `${count} طلاب مسجلون في ${source.course_code} و${course.course_code}.`);
            fragments.push(badge);
            card.classList.toggle('et-related-conflict', placement === 'same-period');
          }
        }
        // Categorical colours never encode safety. Use a separate, labelled
        // warning for coincident exams, including approved tiny-course overlaps.
        if (usable) {
          const clashes = [...(model.overlaps.get(course.identity) || [])].filter(([identity]) => relationship(course, model.byIdentity.get(identity), model.slots) === 'same-period');
          if (clashes.length) {
            const warning = make('span', 'et-review-warning' + (stale ? ' is-stale' : ''), '⚠');
            const label = text('Shared students in the same period', 'طلاب مشتركون في الفترة نفسها') + ': ' + clashes.map(([identity, count]) => `${model.byIdentity.get(identity).course_code} (${count})`).join(', ') + (stale ? text(' · last checked enrollments', ' · التسجيلات في آخر تحقق') : '');
            warning.setAttribute('role', 'img'); warning.setAttribute('aria-label', label); warning.title = label;
            fragments.push(warning);
          }
        }
        badges.replaceChildren(...fragments);
      }
      // QA details describe the checked report. Share size and term styling, but
      // never hide actionable rows or overlay current-placement relationships.
      const drill = $('kpiDrill');
      if (drill && !drill.classList.contains('d-none')) {
        const firstRow = $('kpiDrillBody')?.firstElementChild;
        const viewKey = state.mode === 'term'
          ? JSON.stringify(['term', state.plan, [...plan].map(([identity, terms]) => [identity, [...terms]])])
          : state.mode === 'size' ? JSON.stringify(['size', model.enrollmentCountsAvailable, [...model.byIdentity].map(([identity, course]) => [identity, course.enrolled_count])]) : 'neutral';
        // Large checked reports may retain thousands of cards. Repaint only
        // when the displayed rows or their colour meaning actually change.
        if (firstRow !== paintedDrillRow || viewKey !== paintedDrillView) {
          for (const card of drill.querySelectorAll('.et-drill-course[data-course-identity]')) {
            const badge = paintCardView(card, model.byIdentity.get(card.dataset.courseIdentity), plan);
            card.querySelector('.et-review-badges')?.replaceChildren(...(badge ? [badge] : []));
          }
          paintedDrillRow = firstRow;
          paintedDrillView = viewKey;
        }
      }
    }

    $('examReviewMode').addEventListener('change', event => {
      state.mode = ['size', 'term', 'students', 'neutral'].includes(event.target.value) ? event.target.value : 'size'; paint();
    });
    $('examReviewPlan').addEventListener('change', event => { state.plan = event.target.value; state.term = 'all'; paint(); });
    $('examTermLegend').addEventListener('click', event => {
      const button = event.target.closest('button[data-exam-term]');
      if (button) { state.term = button.dataset.examTerm; paint(); }
    });
    $('examRelatedClear').addEventListener('click', () => { state.focus = null; paint(); $('examReviewMode').focus(); });
    const focusCourse = identity => {
      if (!model.overlapsAvailable || blocked || !model.byIdentity.has(identity)) return;
      state.focus = identity; state.mode = 'students'; paint();
    };
    grid.addEventListener('click', event => {
      const button = event.target.closest('[data-exam-related]');
      if (button && !button.disabled && !grid.inert) focusCourse(button.dataset.examRelated);
    });
    // The page applies its search classes in another listener. Paint after all
    // listeners have run so the focused-exam notice describes the current query.
    $('schedFilter')?.addEventListener('input', () => {
      queueMicrotask(() => { if (state.mode === 'students') paint(); });
    });
    return {
      update(data, options = {}) {
        if (options.reset || !data) { state.focus = null; state.term = 'all'; }
        model = createModel(data, options.visibleCodes);
        stale = Boolean(options.stale); blocked = Boolean(options.blocked); paint();
      },
      reset() { state.focus = null; state.term = 'all'; model = createModel(null); paint(); },
      refresh: paint,
      focusCourse,
      getState: () => ({ ...state }),
    };
  }
  root.ExamReview = Object.freeze({ createModel, relationship, createController });
})(window);
