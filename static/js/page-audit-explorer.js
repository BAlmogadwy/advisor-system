(function () {
  'use strict';

  var IS_AR = document.documentElement.lang === 'ar';

  /* ================================================================
     AE-03: Format ISO timestamps to readable "Mon DD, YYYY HH:MM"
     Full ISO string is preserved in the title attribute on hover.
     ================================================================ */
  var MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var MONTHS_AR = ['\u064A\u0646\u0627\u064A\u0631','\u0641\u0628\u0631\u0627\u064A\u0631','\u0645\u0627\u0631\u0633','\u0623\u0628\u0631\u064A\u0644','\u0645\u0627\u064A\u0648','\u064A\u0648\u0646\u064A\u0648','\u064A\u0648\u0644\u064A\u0648','\u0623\u063A\u0633\u0637\u0633','\u0633\u0628\u062A\u0645\u0628\u0631','\u0623\u0643\u062A\u0648\u0628\u0631','\u0646\u0648\u0641\u0645\u0628\u0631','\u062F\u064A\u0633\u0645\u0628\u0631'];

  function formatTimestamp(isoStr) {
    if (!isoStr || isoStr.length < 16) return isoStr;
    var d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    var months = IS_AR ? MONTHS_AR : MONTHS;
    var month = months[d.getUTCMonth()];
    var day = d.getUTCDate();
    var year = d.getUTCFullYear();
    var hh = String(d.getUTCHours()).padStart(2, '0');
    var mm = String(d.getUTCMinutes()).padStart(2, '0');
    return month + ' ' + day + ', ' + year + ' ' + hh + ':' + mm;
  }

  (function reformatTimestamps() {
    var tsCells = document.querySelectorAll('.audit-ts');
    for (var i = 0; i < tsCells.length; i++) {
      var raw = tsCells[i].textContent.trim();
      if (!raw) continue;
      var formatted = formatTimestamp(raw);
      if (formatted !== raw) {
        tsCells[i].setAttribute('title', raw);
        tsCells[i].textContent = formatted;
      }
    }
  })();

  /* ================================================================
     Bilingual labels
     ================================================================ */
  var L = {
    showing:    IS_AR ? '\u0639\u0631\u0636'      : 'Showing',
    of:         IS_AR ? '\u0645\u0646'             : 'of',
    records:    IS_AR ? '\u0633\u062C\u0644'       : 'records',
    page:       IS_AR ? '\u0635\u0641\u062D\u0629' : 'Page',
    expand:     IS_AR ? '\u062A\u0641\u0627\u0635\u064A\u0644' : 'Details',
    collapse:   IS_AR ? '\u0625\u062E\u0641\u0627\u0621' : 'Collapse',
    noRecords:  IS_AR ? '\u0644\u0627 \u062A\u0648\u062C\u062F \u0633\u062C\u0644\u0627\u062A \u0644\u0639\u0631\u0636\u0647\u0627 \u0641\u064A \u0627\u0644\u062C\u062F\u0648\u0644 \u0627\u0644\u0632\u0645\u0646\u064A' : 'No records to show in timeline'
  };

  /* ================================================================
     Client-side pagination
     ================================================================ */
  var table      = document.getElementById('auditTable');
  var tbody      = table ? table.querySelector('tbody') : null;
  var allRows    = [];
  var pagination = document.getElementById('auditPagination');
  var currentPage = 1;
  var pageSize    = 50;

  // Collect data rows (skip the empty-state row)
  if (tbody) {
    var trs = tbody.querySelectorAll('tr');
    for (var i = 0; i < trs.length; i++) {
      var cells = trs[i].querySelectorAll('td');
      // A real data row has exactly 10 cells (matching thead columns)
      if (cells.length === 10) {
        allRows.push(trs[i]);
      }
    }
  }

  var totalRows  = allRows.length;
  var totalPages = 1;

  var elStart       = document.getElementById('auditPageStart');
  var elEnd         = document.getElementById('auditPageEnd');
  var elTotal       = document.getElementById('auditTotal');
  var elCurrentPage = document.getElementById('auditCurrentPage');
  var elTotalPages  = document.getElementById('auditTotalPages');
  var elPageSize    = document.getElementById('auditPageSize');
  var btnFirst      = document.getElementById('auditFirstPage');
  var btnPrev       = document.getElementById('auditPrevPage');
  var btnNext       = document.getElementById('auditNextPage');
  var btnLast       = document.getElementById('auditLastPage');

  function renderPage() {
    totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
    if (currentPage > totalPages) currentPage = totalPages;
    if (currentPage < 1) currentPage = 1;

    var start = (currentPage - 1) * pageSize;
    var end   = Math.min(start + pageSize, totalRows);

    for (var i = 0; i < allRows.length; i++) {
      allRows[i].style.display = (i >= start && i < end) ? '' : 'none';
    }

    elStart.textContent       = totalRows === 0 ? '0' : String(start + 1);
    elEnd.textContent         = String(end);
    elTotal.textContent       = String(totalRows);
    elCurrentPage.textContent = String(currentPage);
    elTotalPages.textContent  = String(totalPages);

    btnFirst.disabled = currentPage <= 1;
    btnPrev.disabled  = currentPage <= 1;
    btnNext.disabled  = currentPage >= totalPages;
    btnLast.disabled  = currentPage >= totalPages;
  }

  if (totalRows > 0 && pagination) {
    pagination.classList.remove('d-none');
    renderPage();

    btnFirst.addEventListener('click', function () { currentPage = 1; renderPage(); });
    btnPrev.addEventListener('click',  function () { currentPage--; renderPage(); });
    btnNext.addEventListener('click',  function () { currentPage++; renderPage(); });
    btnLast.addEventListener('click',  function () { currentPage = totalPages; renderPage(); });

    elPageSize.addEventListener('change', function () {
      pageSize = parseInt(this.value, 10) || 50;
      currentPage = 1;
      renderPage();
    });
  }

  /* ================================================================
     View toggle: Table <-> Timeline
     ================================================================ */
  var tableRegion   = document.getElementById('auditTableRegion');
  var timelineEl    = document.getElementById('auditTimeline');
  var viewBtns      = document.querySelectorAll('.audit-view-btn');
  var timelineBuilt = false;

  function setActiveView(view) {
    for (var i = 0; i < viewBtns.length; i++) {
      var btn = viewBtns[i];
      var isActive = btn.getAttribute('data-view') === view;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    }

    if (view === 'table') {
      tableRegion.style.display = '';
      if (totalRows > 0) pagination.classList.remove('d-none');
      timelineEl.classList.add('d-none');
    } else {
      tableRegion.style.display = 'none';
      pagination.classList.add('d-none');
      timelineEl.classList.remove('d-none');
      if (!timelineBuilt) {
        buildTimeline();
        timelineBuilt = true;
      }
    }
  }

  for (var v = 0; v < viewBtns.length; v++) {
    viewBtns[v].addEventListener('click', function () {
      setActiveView(this.getAttribute('data-view'));
    });
  }

  /* ================================================================
     Build timeline HTML from table rows
     ================================================================ */
  /* esc() now in shared-utils.js */

  function buildTimeline() {
    if (allRows.length === 0) {
      timelineEl.innerHTML = '<div class="empty-state text-center" style="padding:2rem">' +
        '<div class="empty-title">' + esc(L.noRecords) + '</div></div>';
      return;
    }

    // Parse rows into data objects
    var records = [];
    for (var i = 0; i < allRows.length; i++) {
      var cells = allRows[i].querySelectorAll('td');
      records.push({
        id:       (cells[0] || {}).textContent || '',
        ts:       (cells[1] || {}).textContent || '',
        tsRaw:    (cells[1] ? cells[1].getAttribute('title') : '') || (cells[1] || {}).textContent || '',
        user:     (cells[2] || {}).textContent || '',
        role:     (cells[3] || {}).textContent || '',
        action:   (cells[4] || {}).textContent || '',
        status:   (cells[5] || {}).textContent || '',
        reason:   (cells[6] || {}).textContent || '',
        endpoint: (cells[7] || {}).textContent || '',
        error:    (cells[8] || {}).textContent || '',
        hash:     (cells[9] || {}).textContent || ''
      });
    }

    // Group by date
    var groups = {};
    var groupOrder = [];
    for (var j = 0; j < records.length; j++) {
      var r = records[j];
      var tsRaw = r.tsRaw.trim();
      var tsDisplay = r.ts.trim();
      // Use raw ISO for grouping by date; fall back to display text
      var datePart, timePart;
      if (tsRaw.length >= 10 && tsRaw.charAt(4) === '-') {
        datePart = tsRaw.substring(0, 10);
        timePart = tsRaw.length > 11 ? tsRaw.substring(11).replace(/\.\d+.*$/, '') : tsDisplay;
      } else {
        datePart = tsDisplay.length >= 10 ? tsDisplay.substring(0, 10) : tsDisplay;
        timePart = tsDisplay;
      }

      if (!groups[datePart]) {
        groups[datePart] = [];
        groupOrder.push(datePart);
      }
      groups[datePart].push({
        time: timePart,
        timeRaw: tsRaw,
        id: r.id.trim(),
        user: r.user.trim(),
        role: r.role.trim(),
        action: r.action.trim(),
        status: r.status.trim().toLowerCase(),
        reason: r.reason.trim(),
        endpoint: r.endpoint.trim(),
        error: r.error.trim(),
        hash: r.hash.trim()
      });
    }

    var html = '';
    for (var g = 0; g < groupOrder.length; g++) {
      var day = groupOrder[g];
      var items = groups[day];
      html += '<div class="atl-day">' + esc(day) + '</div>';
      for (var k = 0; k < items.length; k++) {
        var it = items[k];
        var statusCls = 'atl-status-' + it.status;
        var uid = 'atl-extra-' + g + '-' + k;
        html += '<div class="atl-item">' +
          '<div class="atl-time" title="' + esc(it.timeRaw) + '">' + esc(it.time) + '</div>' +
          '<div class="atl-content">' +
            '<div class="atl-action">' + esc(it.action) + '</div>' +
            '<div class="atl-detail">' +
              esc(it.user) + (it.role ? ' (' + esc(it.role) + ')' : '') +
              ' &mdash; <span class="atl-status ' + statusCls + '">' + esc(it.status) + '</span>' +
              (it.reason ? ' &middot; ' + esc(it.reason) : '') +
            '</div>' +
            '<button type="button" class="atl-expand" aria-expanded="false" aria-controls="' + uid + '" data-target="' + uid + '">' +
              esc(L.expand) +
            '</button>' +
            '<div class="atl-extra" id="' + uid + '" role="region">' +
              (it.endpoint ? '<div><strong>Endpoint:</strong> ' + esc(it.endpoint) + '</div>' : '') +
              (it.hash     ? '<div><strong>Hash:</strong> ' + esc(it.hash) + '</div>' : '') +
              (it.error    ? '<div><strong>Error:</strong> ' + esc(it.error) + '</div>' : '') +
              '<div><strong>ID:</strong> ' + esc(it.id) + '</div>' +
            '</div>' +
          '</div>' +
        '</div>';
      }
    }

    timelineEl.innerHTML = html;

    // Attach expand/collapse handlers
    var expandBtns = timelineEl.querySelectorAll('.atl-expand');
    for (var e = 0; e < expandBtns.length; e++) {
      expandBtns[e].addEventListener('click', function () {
        var targetId = this.getAttribute('data-target');
        var target = document.getElementById(targetId);
        if (!target) return;
        var isOpen = target.classList.contains('open');
        target.classList.toggle('open', !isOpen);
        this.setAttribute('aria-expanded', isOpen ? 'false' : 'true');
        this.textContent = isOpen ? L.expand : L.collapse;
      });
    }
  }

  /* ================================================================
     Keyboard support for view toggle
     ================================================================ */
  var toggleContainer = document.querySelector('.audit-view-toggle');
  if (toggleContainer) {
    toggleContainer.addEventListener('keydown', function (e) {
      var tabs = Array.prototype.slice.call(toggleContainer.querySelectorAll('.audit-view-btn'));
      var idx = tabs.indexOf(document.activeElement);
      if (idx === -1) return;
      var next = -1;
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        var dir = (e.key === 'ArrowRight') ? 1 : -1;
        // In RTL, reverse direction
        if (IS_AR) dir = -dir;
        next = (idx + dir + tabs.length) % tabs.length;
        tabs[next].focus();
        tabs[next].click();
      }
    });
  }

})();
