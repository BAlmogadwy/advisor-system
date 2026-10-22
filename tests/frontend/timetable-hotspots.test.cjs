/* The optimise result's "courses that left students without a section"
 * (static/js/timetable-hotspots.js), shared by the classic workspace and
 * split view: code, name and students, most affected first, in English and
 * Arabic; an older result (keys only) by its code with no count. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');

function load(t) {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  // base.html loads shared-utils (esc, pluralAr) on every page.
  dom.window.eval(fs.readFileSync(path.join(root, 'static/js/shared-utils.js'), 'utf8'));
  dom.window.eval(fs.readFileSync(path.join(root, 'static/js/timetable-hotspots.js'), 'utf8'));
  return dom.window;
}
const drawn = (w, result, ar) => {
  w.document.body.innerHTML = w.TimetableHotspots.html(result, ar);
  return [...w.document.querySelectorAll('.ux-hotspots-row')].map(row => [
    row.querySelector('.ux-hotspots-code')?.textContent,
    row.querySelector('.ux-hotspots-name')?.textContent ?? null,
    row.querySelector('.ux-hotspots-count')?.textContent ?? null,
  ]);
};

const RESULT = {
  hotspot_courses: ['FE2::FREE_ELECTIVE_COURSE_II', 'CS372::DATABASE_SYSTEMS', 'MATH204::CALCULUS_II'],
  hotspot_details: [
    { course_key: 'FE2::FREE_ELECTIVE_COURSE_II', course_code: 'FE2', course_name: 'Free Elective Course II', students: 14 },
    { course_key: 'CS372::DATABASE_SYSTEMS', course_code: 'CS372', course_name: 'Database Systems', students: 3 },
    { course_key: 'MATH204::CALCULUS_II', course_code: '', course_name: '', students: 1 },
  ],
};

test('each course reads as its code, its name and the students it left out, most first, in an ordered list', t => {
  const w = load(t);
  assert.deepEqual(drawn(w, RESULT, false), [
    ['FE2', 'Free Elective Course II', '14 students'],
    ['CS372', 'Database Systems', '3 students'],
    ['MATH204', null, '1 student'],
  ]);
  assert.equal(w.document.querySelector('.ux-hotspots-list').tagName, 'OL');
  assert.equal(w.document.querySelector('.ux-hotspots-title').textContent, 'Courses that left students without a section');
  assert.match(w.document.querySelector('.ux-hotspots-help').textContent, /every section clashes with their other classes, or the ones that do not are full/);
  // The code is isolated (an LTR code in an RTL line).
  assert.equal(w.document.querySelector('.ux-hotspots-code').tagName, 'BDI');
});

test('in Arabic the words and the counts take their Arabic forms', t => {
  const w = load(t);
  const counts = n => drawn(w, { hotspot_details: [{ course_key: 'CS372::X', course_code: 'CS372', students: n }] }, true)[0][2];
  assert.deepEqual([1, 2, 3, 11, 100].map(counts), ['طالب واحد', 'طالبان', '3 طلاب', '11 طالباً', '100 طالب']);
  assert.equal(w.document.querySelector('.ux-hotspots-title').textContent, 'مقررات بقي طلاب دون شعبة فيها');
});

test('an older result with keys only shows each code with no count; nothing to say draws nothing', t => {
  const w = load(t);
  assert.deepEqual(drawn(w, { hotspot_courses: ['GS103::ISLAMIC_STUDIES', 'ENGL104::TECHNICAL_COMMUNICATION'] }, false), [
    ['GS103', null, null],
    ['ENGL104', null, null],
  ]);
  for (const empty of [{}, { hotspot_courses: [] }, { hotspot_details: [] }, null]) {
    assert.equal(w.TimetableHotspots.html(empty, false), '');
  }
  // Details win over the bare keys when both are there.
  assert.equal(drawn(w, RESULT, false).length, 3);
});

test('what the server sends is escaped, never markup', t => {
  const w = load(t);
  const html = w.TimetableHotspots.html({ hotspot_details: [{ course_key: 'X', course_code: '<b>X</b>', course_name: 'A & <i>B</i>', students: 2 }] }, false);
  assert.ok(!html.includes('<b>') && !html.includes('<i>'));
  w.document.body.innerHTML = html;
  assert.deepEqual([w.document.querySelector('.ux-hotspots-code').textContent, w.document.querySelector('.ux-hotspots-name').textContent], ['<b>X</b>', 'A & <i>B</i>']);
});
