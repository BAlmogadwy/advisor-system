/* A section's teacher, as the one-teacher rule decides it.

   Shared by the Timetable Workspace and its split view (they each had their
   own reading of `placement.teacher`, and they disagreed). The server sends
   `teacher` only with the rule on: `{id, name, status, source, reason}`, or
   null for a section its map leaves out.

   - status "tbd": no teacher yet; `reason` says why (the hint).
   - a name: that teacher.
   - no `teacher` key (rule off), null, or a status of "no_links" or
     "frozen_unknown": the name on the section's meetings, if any.
   - otherwise: none. A meeting row can carry a stale name, so it is used
     only where the rule says the map does not know. */
(function () {
  'use strict';
  // The page's language, as the pages read it (base.html's LANGUAGE_CODE), at
  // call time: a page may set it after this script loads.
  const isArabic = () => (typeof LANGUAGE_CODE === 'string' ? LANGUAGE_CODE : document.documentElement.lang) === 'ar';
  const REASONS = {
    d10: ['Every eligible teacher already has the most sections of this course they may teach.', 'كل المدرسين المؤهلين لديهم أقصى عدد مسموح به من شعب هذا المقرر.'],
    weekly_ceiling: ['Every eligible teacher is at the weekly limit.', 'كل المدرسين المؤهلين بلغوا الحد الأسبوعي.'],
    clash: ['Every eligible teacher is teaching at this time.', 'كل المدرسين المؤهلين لديهم محاضرة في هذا الوقت.'],
    daily_cap: ['Every eligible teacher is at the daily limit on this day.', 'كل المدرسين المؤهلين بلغوا الحد اليومي في هذا اليوم.'],
    bound: ['Any eligible teacher would get a longer week.', 'إسنادها لأي مدرس مؤهل يطيل أسبوعه.'],
  };

  /* {state: "tbd" | "named" | "none", name, hint} */
  function teacherOf(placement) {
    const t = placement ? placement.teacher : null;
    if (t && t.status === 'tbd') {
      return { state: 'tbd', name: '', hint: (REASONS[t.reason] || [])[isArabic() ? 1 : 0] || '' };
    }
    const meetingName = ((placement && placement.meetings) || [])
      .map(m => String(m.instructor || '').trim()).find(Boolean) || '';
    const name = (t && t.name) || (!t || t.status === 'no_links' || t.status === 'frozen_unknown' ? meetingName : '');
    return name ? { state: 'named', name, hint: '' } : { state: 'none', name: '', hint: '' };
  }

  window.TimetableSectionTeacher = {
    teacherOf,
    REASONS,
    words: {
      get tbd() { return isArabic() ? 'يُحدَّد المدرس لاحقاً' : 'Teacher to be decided'; },
      get none() { return isArabic() ? 'لا يوجد' : 'None'; },
    },
  };
})();
