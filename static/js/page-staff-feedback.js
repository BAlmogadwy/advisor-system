/* Staff timetable feedback.

   Two small enhancements; the pages work without either.
   1. Focus the error summary (or the one-time notice) once it is drawn, so a
      screen reader announces it and a keyboard user starts from it.
   2. On the queue, a "select every case on this page" box. Without JavaScript
      it stays hidden and each case is ticked on its own. */
(function () {
  'use strict';

  var target = document.getElementById('ttfsErrors') || document.getElementById('ttfsNotice');
  if (target) {
    requestAnimationFrame(function () {
      target.focus();
    });
  }

  var all = document.getElementById('ttfsSelectAll');
  var form = document.getElementById('ttfsBulkForm');
  if (!all || !form) {
    return;
  }
  var picks = Array.prototype.slice.call(form.querySelectorAll('input[name="pick"]'));
  if (!picks.length) {
    return;
  }
  all.hidden = false;

  function sync() {
    var ticked = picks.filter(function (box) { return box.checked; }).length;
    all.checked = ticked === picks.length;
    all.indeterminate = ticked > 0 && ticked < picks.length;
  }

  all.addEventListener('change', function () {
    picks.forEach(function (box) { box.checked = all.checked; });
    sync();
  });
  picks.forEach(function (box) { box.addEventListener('change', sync); });
  sync();
})();
