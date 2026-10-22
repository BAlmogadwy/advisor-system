/* Courses that left students without a section (an optimise result's
 * "hotspots"), drawn the same by the classic workspace and split view.
 *
 * The engine ranks a course by how many students who need it this term got
 * no section of it because every section clashes with their other courses,
 * or the ones that do not clash are full or held in reserve
 * (core/services/timetable_candidate_eval.py, hotspot_course_counts). A
 * result carries `hotspot_details` ({course_key, course_code, course_name,
 * students}, most affected first); an older one only `hotspot_courses`
 * (keys), which are shown by their code, with no count.
 *
 * Needs `esc` and `pluralAr` (shared-utils.js, loaded on every page). */
(() => {
  const WORDS = {
    en: {
      title: 'Courses that left students without a section',
      help: 'Students who need these courses this term got no section: every section clashes with their other classes, or the ones that do not are full. Most affected first.',
      students: n => `${n} ${n === 1 ? 'student' : 'students'}`,
    },
    ar: {
      title: 'مقررات بقي طلاب دون شعبة فيها',
      help: 'طلاب يحتاجون هذه المقررات هذا الفصل ولم يحصلوا على شعبة: كل شعبها تتعارض مع موادهم الأخرى، أو ما لا يتعارض منها ممتلئ. الأكثر تأثراً أولاً.',
      students: n => pluralAr(n, { one: 'طالب واحد', two: 'طالبان', few: '{n} طلاب', many: '{n} طالباً', other: '{n} طالب' }),
    },
  };

  /* One row per hotspot course: {key, code, name, students}; `students` is
     null when the result does not say. */
  function rows(result) {
    const details = Array.isArray(result?.hotspot_details) ? result.hotspot_details : null;
    if (details) {
      return details.map(item => {
        const key = String(item?.course_key ?? '');
        const count = Number(item?.students);
        return {
          key,
          code: String(item?.course_code || key.split('::')[0] || '').trim(),
          name: String(item?.course_name || '').trim(),
          students: Number.isFinite(count) && count > 0 ? count : null,
        };
      });
    }
    return (Array.isArray(result?.hotspot_courses) ? result.hotspot_courses : []).map(key => ({
      key: String(key ?? ''),
      code: String(key ?? '').split('::')[0].trim(),
      name: '',
      students: null,
    }));
  }

  /* The section as HTML, '' when there is nothing to say. */
  function html(result, ar = false) {
    const list = rows(result).filter(row => row.code);
    if (!list.length) return '';
    const w = WORDS[ar ? 'ar' : 'en'];
    const items = list.map(row => `<li class="ux-hotspots-row">`
      + `<bdi class="ux-hotspots-code">${esc(row.code)}</bdi>`
      + (row.name ? `<span class="ux-hotspots-name">${esc(row.name)}</span>` : '')
      + (row.students ? `<span class="ux-hotspots-count">${esc(w.students(row.students))}</span>` : '')
      + `</li>`).join('');
    return `<section class="ux-hotspots"><h3 class="ux-hotspots-title">${esc(w.title)}</h3>`
      + `<p class="ux-hotspots-help">${esc(w.help)}</p><ol class="ux-hotspots-list">${items}</ol></section>`;
  }

  window.TimetableHotspots = { rows, html, WORDS };
})();
