/* Staff timetable feedback.

   Enhancements only; every page works without them.
   1. Focus the error summary (or the one-time notice) once it is drawn, so a
      screen reader announces it and a keyboard user starts from it.
   2. On the queue, a "select every case on this page" box above the list, and
      the bulk actions kept out of the way until a case is ticked, then in view at
      the foot of the page with the number ticked. Without JavaScript the box
      stays hidden and the bulk actions always show under the list.
   3. On a case, the composer: the answer, the question and the comment become
      tabs (the ARIA tabs pattern, arrows following the page's direction),
      opening on a form that was refused or holds typed text. Without JavaScript
      the three forms stand one under another.
   4. On a phone, the queue's filters fold into their title unless one is on.

   Nothing here writes HTML: elements are made with createElement and filled
   with textContent. */
(function () {
  'use strict';

  var target = document.getElementById('ttfsErrors') || document.getElementById('ttfsNotice');
  if (target) {
    requestAnimationFrame(function () {
      target.focus();
    });
  }

  var selectAll = function () {
    var all = document.getElementById('ttfsSelectAll');
    var form = document.getElementById('ttfsBulkForm');
    if (!all || !form) {
      return;
    }
    var picks = Array.prototype.slice.call(form.querySelectorAll('input[name="pick"]'));
    if (!picks.length) {
      return;
    }
    var bulk = document.getElementById('ttfsBulk');
    var count = document.getElementById('ttfsBulkCount');
    var number = count ? count.querySelector('[data-selected-count]') : null;
    var tools = document.getElementById('ttfsQueueTools');
    if (tools) {
      tools.hidden = false;
    }
    if (count) {
      count.hidden = false;
    }

    var sync = function () {
      var ticked = picks.filter(function (box) { return box.checked; }).length;
      all.checked = ticked === picks.length;
      all.indeterminate = ticked > 0 && ticked < picks.length;
      if (number) {
        number.textContent = String(ticked);
      }
      if (bulk) {
        bulk.classList.toggle('is-waiting', ticked === 0);
        bulk.classList.toggle('is-active', ticked > 0);
      }
    };

    all.addEventListener('change', function () {
      picks.forEach(function (box) { box.checked = all.checked; });
      sync();
    });
    picks.forEach(function (box) { box.addEventListener('change', sync); });
    sync();
  };

  var composer = function () {
    var actions = document.getElementById('ttfsActions');
    if (!actions) {
      return;
    }
    var panels = Array.prototype.slice.call(actions.querySelectorAll('form.ttfs-compose'));
    if (panels.length < 2) {
      return;
    }
    var list = document.createElement('div');
    list.className = 'ttfs-compose-tabs';
    list.setAttribute('role', 'tablist');
    list.setAttribute('aria-label', actions.getAttribute('data-composer-label') || '');

    var tabs = panels.map(function (panel) {
      var title = panel.querySelector('.ttfs-action-title');
      var tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'ttfs-compose-tab';
      tab.id = panel.id + 'Tab';
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-controls', panel.id);
      tab.textContent = title ? title.textContent : panel.id;
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', tab.id);
      panel.classList.add('is-tabbed');
      if (title) {
        // The tab names the panel now; its heading would only repeat it.
        title.hidden = true;
      }
      list.appendChild(tab);
      return tab;
    });
    panels[0].parentNode.insertBefore(list, panels[0]);

    var select = function (index, focus) {
      tabs.forEach(function (tab, i) {
        var on = i === index;
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
        tab.tabIndex = on ? 0 : -1;
        panels[i].hidden = !on;
      });
      if (focus) {
        tabs[index].focus();
      }
    };

    // Open where the page needs the person: a form with an error at a field, else
    // one that holds typed text, else the first. That covers every refusal: an
    // empty text is a field's error, and anything else came back with its text
    // (only a ready-made answer comes back empty, and the answer is first).
    var start = -1;
    panels.forEach(function (panel, i) {
      if (start < 0 && panel.querySelector('[aria-invalid="true"], .ttf-field-error')) {
        start = i;
      }
    });
    panels.forEach(function (panel, i) {
      var area = panel.querySelector('textarea');
      if (start < 0 && area && area.value.trim()) {
        start = i;
      }
    });
    select(start < 0 ? 0 : start, false);

    var rtl = document.documentElement.getAttribute('dir') === 'rtl';
    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () { select(i, false); });
      tab.addEventListener('keydown', function (event) {
        var last = tabs.length - 1;
        var next = null;
        if (event.key === 'ArrowRight') {
          next = rtl ? i - 1 : i + 1;
        } else if (event.key === 'ArrowLeft') {
          next = rtl ? i + 1 : i - 1;
        } else if (event.key === 'Home') {
          next = 0;
        } else if (event.key === 'End') {
          next = last;
        }
        if (next === null) {
          return;
        }
        event.preventDefault();
        select((next + tabs.length) % tabs.length, true);
      });
    });
  };

  // 4. On a phone the queue's filters fold into their title, so the cases come
  //    first, unless one of them is on. Drawn open, so nothing hides without
  //    JavaScript or at a desk. Decided once, on load: turning the phone never
  //    undoes what the person opened or closed.
  var foldFilters = function () {
    var box = document.getElementById('ttfsFiltersBox');
    if (!box || box.hasAttribute('data-active') || !window.matchMedia) {
      return;
    }
    if (window.matchMedia('(max-width: 640px)').matches) {
      box.open = false;
    }
  };

  selectAll();
  composer();
  foldFilters();
})();
