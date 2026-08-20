/* Enhance the server-rendered student timetables with the shared responsive view.
 *
 * There may be TWO on this page — the registrar's snapshot and an imported
 * expected plan — so this walks every host rather than looking up one fixed id.
 * Each host names its own JSON payload through `data-meetings-id`; a host that
 * points at a missing or malformed payload is skipped and leaves the
 * server-rendered agenda table in place, which is the accessible view anyway.
 */
(function () {
  'use strict';

  if (!window.StudentTimetable) return;

  var hosts = document.querySelectorAll('.student-timetable-host[data-meetings-id]');
  Array.prototype.forEach.call(hosts, function (host) {
    var dataNode = document.getElementById(host.dataset.meetingsId);
    if (!dataNode) return;

    var meetings;
    try {
      meetings = JSON.parse(dataNode.textContent || '[]');
    } catch (_) {
      return;
    }

    var lang = String(host.dataset.language || document.documentElement.lang || 'en');
    var arabic = lang.toLowerCase().startsWith('ar');
    // Isolated per host. With two grids on the page, an exception raised while
    // rendering one would otherwise abandon the loop and leave the other blank —
    // and the blank one could be the registered timetable, silently replaced by
    // nothing beside a fully drawn expected plan.
    try {
      window.StudentTimetable.render(host, meetings, {
        lang: lang,
        dir: host.getAttribute('dir') || (arabic ? 'rtl' : 'ltr'),
        dayLabels: arabic
          ? { SUN: 'الأحد', MON: 'الاثنين', TUE: 'الثلاثاء', WED: 'الأربعاء', THU: 'الخميس', FRI: 'الجمعة', SAT: 'السبت' }
          : { SUN: 'Sun', MON: 'Mon', TUE: 'Tue', WED: 'Wed', THU: 'Thu', FRI: 'Fri', SAT: 'Sat' },
        timeLabel: host.dataset.timeLabel || (arabic ? 'الوقت' : 'Time'),
        emptyText: host.dataset.emptyMessage || (arabic ? 'لا توجد أوقات لعرضها.' : 'No meeting times to show.'),
        currentLabel: arabic ? 'مسجّل' : 'Registered',
        proposedLabel: arabic ? 'متوقع' : 'Expected',
        showCourseName: true,
        showSource: false,
      });
    } catch (_) {
      /* The server-rendered agenda table under "Course and time details" stays. */
    }
  });
})();
