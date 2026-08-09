/* Enhance the server-rendered student timetable with the shared responsive view. */
(function () {
  'use strict';

  var host = document.getElementById('studentHomeTimetable');
  var dataNode = document.getElementById('studentHomeTimetableData');
  if (!host || !dataNode || !window.StudentTimetable) return;

  var meetings;
  try {
    meetings = JSON.parse(dataNode.textContent || '[]');
  } catch (_) {
    return;
  }

  var lang = String(host.dataset.language || document.documentElement.lang || 'en');
  var arabic = lang.toLowerCase().startsWith('ar');
  window.StudentTimetable.render(host, meetings, {
    lang: lang,
    dir: host.getAttribute('dir') || (arabic ? 'rtl' : 'ltr'),
    dayLabels: arabic
      ? { SUN: 'الأحد', MON: 'الاثنين', TUE: 'الثلاثاء', WED: 'الأربعاء', THU: 'الخميس', FRI: 'الجمعة', SAT: 'السبت' }
      : { SUN: 'Sun', MON: 'Mon', TUE: 'Tue', WED: 'Wed', THU: 'Thu', FRI: 'Fri', SAT: 'Sat' },
    timeLabel: host.dataset.timeLabel || (arabic ? 'الوقت' : 'Time'),
    emptyText: host.dataset.emptyMessage || (arabic ? 'لا توجد أوقات لعرضها.' : 'No meeting times to show.'),
    currentLabel: arabic ? 'في جدولك الحالي' : 'In your current timetable',
    proposedLabel: arabic ? 'مقترح' : 'Proposed',
    showCourseName: true,
    showSource: false,
  });
})();
