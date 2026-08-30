/* Dedicated Adviser Portfolio graduation workspace controls. */
(function () {
  'use strict';

  const tablist = document.getElementById('agBaselineTabs');
  const tabs = Array.from(tablist?.querySelectorAll('[role="tab"][href]') || []);

  if (tablist && tabs.length) {
    tablist.addEventListener('keydown', function (event) {
      if (!tabs.includes(event.target)) return;
      const current = tabs.indexOf(event.target);
      let next = null;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        next = (current + 1) % tabs.length;
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        next = (current - 1 + tabs.length) % tabs.length;
      } else if (event.key === 'Home') {
        next = 0;
      } else if (event.key === 'End') {
        next = tabs.length - 1;
      }
      if (next === null) return;
      event.preventDefault();
      tabs[next].focus();
      tabs[next].click();
    });
  }

  /* Keep the Portfolio section highlighted for its nested student workspace. */
  document.querySelectorAll('.sidebar .nav-link').forEach(function (link) {
    if (link.getAttribute('href') === '/advisor-portfolio/') link.classList.add('active');
  });
})();
