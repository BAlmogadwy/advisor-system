/* Student timetable feedback.

   Two small enhancements; the pages work without either.
   1. Focus the error summary (or the one-time notice) once it is drawn, so a
      screen reader announces it and a keyboard user starts from it.
   2. On the "what is the issue" step, show only the optional list the chosen
      issue uses. Without JavaScript every list stays visible, and the server
      ignores the ones the chosen issue does not use. */
(function () {
  'use strict';

  var target = document.getElementById('ttfErrors') || document.getElementById('ttfNotice');
  if (target) {
    requestAnimationFrame(function () {
      target.focus();
    });
  }

  var form = document.querySelector('form[data-ttf-describe]');
  if (!form) {
    return;
  }
  var groups = Array.prototype.slice.call(form.querySelectorAll('[data-ttf-for]'));

  function sync() {
    var checked = form.querySelector('input[name="reason"]:checked');
    var reason = checked ? checked.value : '';
    groups.forEach(function (group) {
      var codes = (group.getAttribute('data-ttf-for') || '').split(' ');
      group.hidden = codes.indexOf(reason) === -1;
    });
  }

  form.addEventListener('change', function (event) {
    if (event.target && event.target.name === 'reason') {
      sync();
    }
  });
  sync();
})();
