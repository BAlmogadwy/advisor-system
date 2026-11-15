/*
 * `wireSortableTable` (static/js/shared-ux.js) on a table that carries
 * `.table-group-row` subheads: run through
 * tests/test_shared_ux_grouped_sort_frontend.py.
 *
 * base.html loads shared-ux.js on EVERY page, so the sorter is tested on its
 * own markup rather than through one screen. The two guarantees: a spanning
 * subhead is never sorted as if it were data, and sorting reorders the rows
 * INSIDE each group instead of flattening the table — so a subhead can never
 * end up labelling rows that left its group, and no sort is a state the reader
 * cannot get back out of.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');

const UX = fs.readFileSync(path.join(__dirname, '../../static/js/shared-ux.js'), 'utf8');

/* shared-ux.js prepends its loading bar and observes <main> on load, so it
   needs a document to run in — the same way base.html gives it one. */
function boot(bodyHtml) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => errors.push(error));
  const dom = new JSDOM(`<!doctype html><html><body><main>${bodyHtml}</main></body></html>`, {
    url: 'http://ux.test/',
    runScripts: 'outside-only',
    virtualConsole,
  });
  vm.runInContext(UX, dom.getInternalVMContext());
  return { dom, window: dom.window, document: dom.window.document, errors };
}

const GROUPED = `
  <table id="grouped">
    <thead><tr><th data-sort="text">Course</th><th data-sort="num">Credits</th></tr></thead>
    <tbody>
      <tr class="table-group-row"><th colspan="2" scope="colgroup">TERM 1</th></tr>
      <tr><td>MATH101</td><td>4</td></tr>
      <tr><td>CHEM101</td><td>3</td></tr>
      <tr class="table-group-row"><th colspan="2" scope="colgroup">TERM 2</th></tr>
      <tr><td>PHYS102</td><td>4</td></tr>
      <tr><td>BIOL101</td><td>2</td></tr>
    </tbody>
  </table>
`;

/* The tbody as a reader sees it: subheads in place, rows under them. */
function shape(document, id) {
  return [...document.querySelectorAll(`#${id} tbody tr`)].map((tr) =>
    (tr.classList.contains('table-group-row')
      ? `# ${tr.textContent.trim()}`
      : tr.children[0].textContent.trim()));
}

test('a group subhead is never sorted as if it were data', (t) => {
  const { window, document, errors, dom } = boot(GROUPED);
  t.after(() => { dom.window.close(); assert.deepEqual(errors, []); });
  window.wireSortableTable('grouped');
  document.querySelectorAll('#grouped thead th')[0].click();
  const rows = shape(document, 'grouped');
  assert.equal(rows.filter((r) => r.startsWith('# ')).length, 2, 'both subheads survive');
  /* TERM 1 / TERM 2 keep their order; they are not shuffled in among courses. */
  assert.deepEqual(rows.filter((r) => r.startsWith('# ')), ['# TERM 1', '# TERM 2']);
});

test('sorting reorders rows inside each group, keeping the groups', (t) => {
  const { window, document, errors, dom } = boot(GROUPED);
  t.after(() => { dom.window.close(); assert.deepEqual(errors, []); });
  window.wireSortableTable('grouped');
  document.querySelectorAll('#grouped thead th')[0].click();
  assert.deepEqual(shape(document, 'grouped'), [
    '# TERM 1', 'CHEM101', 'MATH101',
    '# TERM 2', 'BIOL101', 'PHYS102',
  ]);
  /* A second click reverses within the groups, and still does not flatten. */
  document.querySelectorAll('#grouped thead th')[0].click();
  assert.deepEqual(shape(document, 'grouped'), [
    '# TERM 1', 'MATH101', 'CHEM101',
    '# TERM 2', 'PHYS102', 'BIOL101',
  ]);
});

test('a numeric column sorts within its group too', (t) => {
  const { window, document, errors, dom } = boot(GROUPED);
  t.after(() => { dom.window.close(); assert.deepEqual(errors, []); });
  window.wireSortableTable('grouped');
  document.querySelectorAll('#grouped thead th')[1].click();
  assert.deepEqual(shape(document, 'grouped'), [
    '# TERM 1', 'CHEM101', 'MATH101',
    '# TERM 2', 'BIOL101', 'PHYS102',
  ]);
});

test('no row ever leaves its own group', (t) => {
  const { window, document, errors, dom } = boot(GROUPED);
  t.after(() => { dom.window.close(); assert.deepEqual(errors, []); });
  window.wireSortableTable('grouped');
  document.querySelectorAll('#grouped thead th')[0].click();
  const rows = shape(document, 'grouped');
  const term1 = rows.slice(rows.indexOf('# TERM 1') + 1, rows.indexOf('# TERM 2'));
  const term2 = rows.slice(rows.indexOf('# TERM 2') + 1);
  assert.deepEqual(term1.sort(), ['CHEM101', 'MATH101']);
  assert.deepEqual(term2.sort(), ['BIOL101', 'PHYS102']);
});

test('an ungrouped table still sorts as one list', (t) => {
  const { window, document, errors, dom } = boot(`
    <table id="flat">
      <thead><tr><th data-sort="text">Course</th></tr></thead>
      <tbody><tr><td>MATH101</td></tr><tr><td>BIOL101</td></tr><tr><td>CHEM101</td></tr></tbody>
    </table>
  `);
  t.after(() => { dom.window.close(); assert.deepEqual(errors, []); });
  window.wireSortableTable('flat');
  document.querySelectorAll('#flat thead th')[0].click();
  assert.deepEqual(shape(document, 'flat'), ['BIOL101', 'CHEM101', 'MATH101']);
});

test('any spanning row is kept out of the data, class or no class', (t) => {
  /* A `th[colspan]` subhead that is not marked `.table-group-row` is still not
     a course: it must not be shuffled in among the sorted rows. */
  const { window, document, errors, dom } = boot(`
    <table id="spanning">
      <thead><tr><th data-sort="text">Course</th><th data-sort="num">Credits</th></tr></thead>
      <tbody>
        <tr><td>MATH101</td><td>4</td></tr>
        <tr><th colspan="2" scope="colgroup">NOTE</th></tr>
        <tr><td>BIOL101</td><td>2</td></tr>
      </tbody>
    </table>
  `);
  t.after(() => { dom.window.close(); assert.deepEqual(errors, []); });
  window.wireSortableTable('spanning');
  document.querySelectorAll('#spanning thead th')[0].click();
  const rows = [...document.querySelectorAll('#spanning tbody tr')].map(tr => tr.textContent.trim());
  /* The note stays put; only the two courses are reordered beneath it. */
  assert.equal(rows[0], 'NOTE');
  assert.deepEqual(rows.slice(1), ['BIOL1012', 'MATH1014']);
});

test('an empty-state row is not sorted as data', (t) => {
  const { window, document, errors, dom } = boot(`
    <table id="empty">
      <thead><tr><th data-sort="text">Course</th><th data-sort="num">Credits</th></tr></thead>
      <tbody><tr><td colspan="2">Nothing loaded</td></tr></tbody>
    </table>
  `);
  t.after(() => { dom.window.close(); assert.deepEqual(errors, []); });
  window.wireSortableTable('empty');
  document.querySelectorAll('#empty thead th')[0].click();
  assert.deepEqual(shape(document, 'empty'), ['Nothing loaded']);
});
