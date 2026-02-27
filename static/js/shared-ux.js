/**
 * Shared UX utilities — Advisor Portal
 * Loading bar, wireSortableTable, debounce
 */

/* ── Loading bar (watches disabled buttons with loading text) ── */
(function () {
  const bar = document.createElement('div');
  bar.id = 'ux-loading-bar';
  document.body.prepend(bar);
  const LOADING = [
    'loading...', 'running...', 'parsing...', 'inserting...',
    'saving...', 'checking...', 'loading', 'running', 'جارٍ',
  ];
  function applyLoading(btn) {
    if (!btn || btn.classList.contains('btn-close') || btn.classList.contains('ux-toast-close')) return;
    const txt = (btn.textContent || '').trim().toLowerCase();
    if (btn.disabled && LOADING.some(t => txt.startsWith(t))) btn.classList.add('ux-loading');
    else btn.classList.remove('ux-loading');
  }
  const obs = new MutationObserver(() => {
    document.querySelectorAll('main button').forEach(applyLoading);
    bar.classList.toggle('active', !!document.querySelector('main .ux-loading'));
  });
  const main = document.querySelector('main');
  if (main) obs.observe(main, { attributes: true, attributeFilter: ['disabled'], subtree: true, childList: true });
})();

/* ── Sortable table wiring ── */
function wireSortableTable(tableId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const headers = table.querySelectorAll('thead th[data-sort]');
  headers.forEach((th, idx) => {
    let dir = 'asc';
    th.style.cursor = 'pointer';
    th.setAttribute('tabindex', '0');
    th.setAttribute('aria-sort', 'none');
    function doSort() {
      const tbody = table.querySelector('tbody');
      if (!tbody) return;
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const validRows = rows.filter(r => r.children.length > idx && !r.querySelector('td[colspan]'));
      if (!validRows.length) return;
      const isNum = th.dataset.sort === 'num';
      const asc = dir === 'asc';
      validRows.sort((a, b) => {
        const av = a.children[idx]?.textContent?.trim() || '';
        const bv = b.children[idx]?.textContent?.trim() || '';
        if (isNum) return asc ? parseFloat(av || 0) - parseFloat(bv || 0) : parseFloat(bv || 0) - parseFloat(av || 0);
        return asc ? av.localeCompare(bv, undefined, { numeric: true }) : bv.localeCompare(av, undefined, { numeric: true });
      });
      headers.forEach(h => { h.removeAttribute('data-dir'); h.setAttribute('aria-sort', 'none'); });
      th.setAttribute('data-dir', asc ? 'asc' : 'desc');
      th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
      dir = asc ? 'desc' : 'asc';
      /* Re-append rows; handle detail/companion rows */
      const detailRows = rows.filter(r => r.classList.contains('ap-detail-row') || r.querySelector('td[colspan]'));
      validRows.forEach(r => {
        tbody.appendChild(r);
        const next = r.nextElementSibling;
        if (next && detailRows.includes(next)) tbody.appendChild(next);
      });
    }
    th.addEventListener('click', doSort);
    th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); doSort(); } });
  });
}

/* ── Debounce helper ── */
function debounce(fn, ms = 250) {
  let t;
  return function (...args) { clearTimeout(t); t = setTimeout(() => fn.apply(this, args), ms); };
}

/**
 * Wire a table for mobile card layout.
 *
 * On ≤768px the CSS class `.mobile-cards` transforms table rows into stacked
 * card layouts.  This helper adds the necessary `data-label` attributes to
 * every `<td>` and applies CSS utility classes (`mc-primary`, `mc-actions`,
 * `mc-hide`) based on the provided options.
 *
 * A MutationObserver is used so dynamically-rendered rows are also tagged.
 *
 * @param {string}   tableId              - DOM id of the <table>.
 * @param {Object}   opts
 * @param {string[]} opts.labels          - Label text for each column index.
 * @param {number[]} [opts.primaryCols]   - Column indices that span full width.
 * @param {number[]} [opts.hideCols]      - Column indices hidden on mobile.
 * @param {number}   [opts.actionCol]     - Column index for the actions cell.
 */
function wireMobileCards(tableId, opts) {
  const table = document.getElementById(tableId);
  if (!table) return;

  const labels      = opts.labels      || [];
  const primaryCols = new Set(opts.primaryCols || []);
  const hideCols    = new Set(opts.hideCols    || []);
  const actionCol   = opts.actionCol   ?? -1;

  function tagRow(tr) {
    const cells = tr.querySelectorAll('td');
    cells.forEach((td, i) => {
      if (labels[i]) td.setAttribute('data-label', labels[i]);
      td.classList.toggle('mc-primary', primaryCols.has(i));
      td.classList.toggle('mc-hide',    hideCols.has(i));
      td.classList.toggle('mc-actions', i === actionCol);
    });
  }

  /* Tag existing rows */
  table.querySelectorAll('tbody tr').forEach(tagRow);

  /* Add the CSS hook class */
  table.classList.add('mobile-cards');

  /* Observe for dynamically added rows */
  const tbody = table.querySelector('tbody');
  if (tbody) {
    new MutationObserver(muts => {
      muts.forEach(m => m.addedNodes.forEach(n => {
        if (n.nodeType === 1 && n.tagName === 'TR') tagRow(n);
      }));
    }).observe(tbody, { childList: true });
  }
}
