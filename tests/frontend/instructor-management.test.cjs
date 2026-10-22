/* Instructor management (core/templates/core/instructor_management.html,
 * static/js/page-instructor-management.js): the page on the design system's
 * tabs, segmented radio and dialog; a status that is only a status; a
 * deactivation that asks first; a form whose Active box and errors work. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const template = read('core/templates/core/instructor_management.html');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };

const INSTRUCTORS = [
  { id: 1, full_name: 'Dr Hala Omar', full_name_ar: 'د. هالة عمر', email: 'hala@example.edu', employee_no: 'E-1', department: 'CS', max_weekly_hours: 12, is_active: true },
  { id: 2, full_name: 'ابراهيم فتحي', full_name_ar: 'ابراهيم فتحي', email: '', employee_no: 'None', department: '', max_weekly_hours: null, is_active: true },
  { id: 3, full_name: 'Dr Old Name', full_name_ar: '', email: '', employee_no: '', department: '', max_weekly_hours: null, is_active: false },
];
const COURSES = [
  { course_code: 'CS101', course_name: 'INTRODUCTION TO COMPUTER SCIENCE II', programme_term: 1, credit_hours: 3, instructor: null },
  { course_code: "X'1", course_name: 'Already Mixed Case', programme_term: 2, credit_hours: 2, instructor: { id: 1, full_name: 'Dr Hala Omar' } },
];

async function page(t, { arabic = false, answer = () => undefined } = {}) {
  const html = template.replace(/<script[\s\S]*?<\/script>/g, '').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
  const dom = new JSDOM(`<!doctype html><html lang="${arabic ? 'ar' : 'en'}" dir="${arabic ? 'rtl' : 'ltr'}"><body><main>${html}</main></body></html>`,
    { runScripts: 'outside-only', url: 'http://localhost/instructor-management/' });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, posts = [], gets = [], notices = [], confirms = [];
  w.eval(read('static/js/shared-utils.js'));
  w.eval(read('static/js/ux-controls.js'));
  w.djCsrfToken = 'x';
  w.notify = Object.fromEntries(['success', 'error', 'warning', 'info'].map(type => [type, text => notices.push({ type, text })]));
  w.dlg = { confirm: async options => { confirms.push(options); return w.confirmAnswer ?? true; } };
  w.fetch = async (url, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase();
    if (method === 'POST') posts.push({ url, body: JSON.parse(opts.body || '{}') }); else gets.push(url);
    let body = answer(url, opts);
    if (body === undefined) {
      if (url.startsWith('/ops/instructors/list/')) body = { ok: true, instructors: INSTRUCTORS };
      else if (url.startsWith('/ops/instructors/course-assignments/?')) body = { ok: true, courses: COURSES };
      else if (url.startsWith('/ops/instructors/load-report/')) body = { ok: true, rows: [], totals: null };
      else body = { ok: true, instructor: { id: 9 } };
    }
    return { ok: !body.httpError, status: body.httpError || 200, json: async () => body };
  };
  w.eval(read('static/js/page-instructor-management.js'));
  w.UXControls.wire();
  // The page wires itself on DOMContentLoaded: the document's own, once.
  if (d.readyState === 'loading') await new Promise(resolve => d.addEventListener('DOMContentLoaded', resolve, { once: true }));
  else d.dispatchEvent(new w.Event('DOMContentLoaded'));
  await settle();
  return { w, d, posts, gets, notices, confirms, $: id => d.getElementById(id) };
}

test('course names in capitals read in title case; Roman numerals, acronyms and mixed-case names stay', async t => {
  const h = await page(t);
  const name = h.w.imCourseName;
  assert.equal(name('INTRODUCTION TO COMPUTER SCIENCE II'), 'Introduction to Computer Science II');
  assert.equal(name('ISLAMIC STUDIES: BELIEF AND WORSHIP'), 'Islamic Studies: Belief and Worship');
  assert.equal(name('FUNDAMENTALS OF AI AND IOT'), 'Fundamentals of AI and IOT');
  assert.equal(name('THE ART OF DATA'), 'The Art of Data', 'a first joining word is capitalised');
  assert.equal(name('Already Mixed Case'), 'Already Mixed Case');
  assert.equal(name('مقدمة في الحوسبة'), 'مقدمة في الحوسبة');
});

test('the page count is the active instructors, read once whichever tab is open', async t => {
  const h = await page(t);
  assert.equal(h.$('imInstructorCount').textContent, '2 active instructors');
  assert.equal(h.gets.filter(url => url.startsWith('/ops/instructors/list/')).length, 1);
  assert.ok(h.gets.includes('/ops/instructors/list/?include_inactive=1'), 'every instructor is read; the page filters');
  const ar = await page(t, { arabic: true });
  assert.equal(ar.$('imInstructorCount').textContent, 'عضوان نشطان');
});

test('the tabs are the global tabs: one pane shows, the roster is read once and Refresh reads it again', async t => {
  const h = await page(t);
  assert.deepEqual([...h.d.querySelectorAll('.im-pane')].map(p => p.hidden), [false, true, true]);
  h.$('imTabBtnRoster').click();
  await settle();
  assert.deepEqual([...h.d.querySelectorAll('.im-pane')].map(p => p.hidden), [true, false, true]);
  assert.equal(h.$('imTabBtnRoster').getAttribute('aria-selected'), 'true');
  assert.equal(h.gets.filter(url => url.startsWith('/ops/instructors/list/')).length, 1, 'no second read for the tab');
  h.$('imRefresh').click();
  await settle();
  assert.equal(h.gets.filter(url => url.startsWith('/ops/instructors/list/')).length, 2);
});

test('the roster: the second name only when it differs, empty fields as a dash, a status that is not a button', async t => {
  const h = await page(t);
  h.$('imTabBtnRoster').click();
  await settle();
  const rows = [...h.d.querySelectorAll('#imInstructorTable tbody tr')];
  assert.equal(rows.length, 2, 'inactive hidden by default');
  assert.equal(rows[0].querySelector('.im-name-other')?.textContent, 'د. هالة عمر');
  assert.equal(rows[1].querySelector('.im-name-other'), null, 'the same name is not repeated');
  assert.equal(rows[1].cells[3].textContent.trim(), '—', 'a stored "None" is no value');
  assert.equal(rows[0].querySelector('.pill-status').tagName, 'SPAN');
  assert.equal(rows[0].querySelector('button.pill-status'), null);
  assert.deepEqual([...rows[0].querySelectorAll('.im-row-actions button')].map(b => b.textContent.trim()), ['Edit', 'Deactivate']);
  h.$('imIncludeInactive').checked = true;
  h.$('imIncludeInactive').dispatchEvent(new h.w.Event('change'));
  assert.equal(h.d.querySelectorAll('#imInstructorTable tbody tr').length, 3, 'inactive shown without another read');
  h.$('imInstructorSearch').value = 'hala';
  h.$('imInstructorSearch').dispatchEvent(new h.w.Event('input'));
  assert.equal(h.d.querySelectorAll('#imInstructorTable tbody tr').length, 1);
});

test('deactivating asks first and sends nothing when declined; activating does not ask', async t => {
  const h = await page(t);
  h.$('imTabBtnRoster').click();
  await settle();
  h.w.confirmAnswer = false;
  h.d.querySelector('[data-im-active="1"]').click();
  await settle();
  assert.equal(h.confirms.length, 1);
  assert.match(h.confirms[0].body, /Dr Hala Omar will no longer appear when assigning courses/);
  assert.equal(h.posts.length, 0, 'declined: nothing sent');
  h.w.confirmAnswer = true;
  h.d.querySelector('[data-im-active="1"]').click();
  await settle();
  assert.deepEqual(h.posts.at(-1), { url: '/ops/instructors/set-active/', body: { id: 1, is_active: false } });
  h.$('imIncludeInactive').checked = true;
  h.$('imIncludeInactive').dispatchEvent(new h.w.Event('change'));
  h.d.querySelector('[data-im-active="3"]').click();
  await settle();
  assert.equal(h.confirms.length, 2, 'activating asks nothing');
  assert.deepEqual(h.posts.at(-1).body, { id: 3, is_active: true });
});

test('the form is the design system dialog: errors inside it, the Active box sent, focus back on Add', async t => {
  let refuse = false;
  const h = await page(t, { answer: url => (url === '/ops/instructors/create/' && refuse ? { ok: false, error: { message: 'An instructor with this name or email already exists' } } : undefined) });
  h.$('imTabBtnRoster').click();
  await settle();
  h.$('imCreateInstructor').focus();
  h.$('imCreateInstructor').click();
  await settle();
  const dialog = h.$('imInstructorDialog');
  assert.equal(h.w.UXControls.dialog.isOpen(dialog), true);
  // No name: said in the dialog, nothing sent, no toast.
  h.$('imInstructorForm').requestSubmit(h.$('imInstructorSave'));
  await settle();
  assert.equal(h.$('imInstructorError').hidden, false);
  assert.equal(h.$('imInstructorError').textContent, 'Name is required');
  assert.equal(h.$('imInstructorName').getAttribute('aria-invalid'), 'true');
  assert.deepEqual([h.posts.length, h.notices.length], [0, 0]);
  // A refusal from the server: still inside the dialog.
  h.$('imInstructorName').value = 'Dr New';
  h.$('imInstructorActive').checked = false;
  refuse = true;
  h.$('imInstructorForm').requestSubmit(h.$('imInstructorSave'));
  await settle();
  assert.equal(h.$('imInstructorError').textContent, 'An instructor with this name or email already exists');
  assert.equal(h.notices.length, 0, 'no toast behind the dialog');
  assert.equal(h.w.UXControls.dialog.isOpen(dialog), true);
  // Saved: the Active box is sent; the dialog closes and Add has the keyboard.
  refuse = false;
  h.$('imInstructorForm').requestSubmit(h.$('imInstructorSave'));
  await settle();
  const sent = h.posts.at(-1);
  assert.equal(sent.url, '/ops/instructors/create/');
  assert.equal(sent.body.is_active, false);
  assert.equal(sent.body.full_name, 'Dr New');
  assert.equal(h.w.UXControls.dialog.isOpen(dialog), false);
  assert.equal(h.d.activeElement, h.$('imCreateInstructor'));
  assert.deepEqual(h.notices.map(n => n.type), ['success']);
});

test('assignments: the section is a radio group; codes travel escaped in data attributes; names in title case', async t => {
  const h = await page(t);
  h.$('caProgram').value = 'AI';
  h.$('caProgram').dispatchEvent(new h.w.Event('change'));
  await settle();
  assert.ok(h.gets.includes('/ops/instructors/course-assignments/?program=AI&section=M'));
  const female = h.d.querySelector('#caSection [data-sec="F"]');
  female.click();
  await settle();
  assert.ok(h.gets.includes('/ops/instructors/course-assignments/?program=AI&section=F'));
  assert.deepEqual([...h.d.querySelectorAll('#caSection [data-sec]')].map(b => [b.getAttribute('aria-checked'), b.tabIndex]), [['false', -1], ['true', 0]]);
  const rows = [...h.d.querySelectorAll('#caTable tbody tr[data-course]')];
  assert.equal(rows[0].cells[3].textContent, 'Introduction to Computer Science II');
  // A code with a quote stays data: no inline script, and it reaches the actions as written.
  assert.equal(h.d.querySelector('#caTable [onclick]'), null);
  assert.equal(rows[1].dataset.course, "X'1");
  rows[1].querySelector('[data-ca-clear]').click();
  await settle();
  assert.deepEqual(h.posts.at(-1), { url: '/ops/instructors/course-assignments/clear/', body: { program: 'AI', course_code: "X'1", section: 'F' } });
  // The search also finds a course by its instructor.
  h.$('caSearch').value = 'hala';
  h.$('caSearch').dispatchEvent(new h.w.Event('input'));
  assert.deepEqual([...h.d.querySelectorAll('#caTable tbody tr[data-course]')].map(r => r.style.display), ['none', '']);
});

test('the load report: a summary, the highest load first, and no badge where no limit is set', async t => {
  const rows = [
    { full_name: 'Light', programs: ['AI'], course_count: 1, distinct_courses: 1, total_credit_hours: 3, load_status: 'na' },
    { full_name: 'Heavy', programs: ['CS'], course_count: 6, distinct_courses: 5, total_credit_hours: 21, load_status: 'over' },
    { full_name: 'None yet', programs: [], course_count: 0, distinct_courses: 0, total_credit_hours: 0, load_status: 'na' },
  ];
  const h = await page(t, { answer: url => (url.startsWith('/ops/instructors/load-report/') ? { ok: true, rows, totals: { course_count: 7, total_credit_hours: 24 } } : undefined) });
  h.$('imTabBtnReport').click();
  await settle();
  const names = [...h.d.querySelectorAll('.im-report-table tbody tr:not(.im-report-totals) strong')].map(e => e.textContent);
  assert.deepEqual(names, ['Heavy', 'Light', 'None yet']);
  const stats = Object.fromEntries([...h.d.querySelectorAll('.im-report-summary .ux-stat')].map(s => [s.querySelector('dt').textContent, s.querySelector('dd').textContent]));
  assert.deepEqual(stats, { 'With a load': '2', 'Courses assigned': '7', 'Credit Hours': '24', 'Over the limit': '1', 'No weekly limit': '2' });
  assert.equal(h.d.querySelector('.im-report-summary .ux-stat[data-tone="warning"] dt').textContent, 'Over the limit');
  const light = [...h.d.querySelectorAll('.im-report-table tbody tr')].find(r => r.textContent.includes('Light'));
  assert.equal(light.querySelector('.pill-status'), null);
  assert.equal(light.cells[6].textContent.trim(), 'No weekly limit');
});

test('a name with quotes stays text: nothing it holds becomes an attribute', async t => {
  const evil = 'Ali " autofocus onfocus="alert(1)';
  const h = await page(t, { answer: url => (url.startsWith('/ops/instructors/list/')
    ? { ok: true, instructors: [{ id: 7, full_name: evil, full_name_ar: '', is_active: true }] }
    : url.startsWith('/ops/instructors/course-assignments/?')
      ? { ok: true, courses: [{ course_code: 'C"1', course_name: 'X', instructor: { id: 7, full_name: evil } }] }
      : undefined) });
  h.$('imTabBtnRoster').click();
  await h.w.Promise.resolve();
  await new Promise(r => setTimeout(r, 20));
  const edit = h.d.querySelector('[data-im-edit="7"]');
  assert.equal(edit.hasAttribute('onfocus'), false);
  assert.equal(edit.hasAttribute('autofocus'), false);
  assert.equal(edit.getAttribute('aria-label'), `Edit — ${evil}`);
  h.$('imTabBtnAssignments').click();
  h.$('caProgram').value = 'AI';
  h.$('caProgram').dispatchEvent(new h.w.Event('change'));
  await new Promise(r => setTimeout(r, 20));
  const chip = h.d.querySelector('.ca-chip-name');
  assert.equal(chip.hasAttribute('onfocus'), false);
  assert.equal(chip.dataset.caAssign, 'C"1');
  assert.equal(h.d.querySelectorAll('[onfocus], [autofocus]').length, 0);
});

test('Add opens once and reads the advisors once; an Edit save gives the keyboard to that row', async t => {
  const h = await page(t);
  h.$('imTabBtnRoster').click();
  await new Promise(r => setTimeout(r, 20));
  h.$('imCreateInstructor').click();
  await new Promise(r => setTimeout(r, 20));
  assert.equal(h.gets.filter(url => url.startsWith('/ops/instructors/advisors/')).length, 1);
  h.w.UXControls.dialog.close(h.$('imInstructorDialog'));
  h.d.querySelector('[data-im-edit="1"]').focus();
  h.d.querySelector('[data-im-edit="1"]').click();
  await new Promise(r => setTimeout(r, 20));
  h.$('imInstructorDepartment').value = 'AI';
  h.$('imInstructorForm').requestSubmit(h.$('imInstructorSave'));
  for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 5));
  assert.equal(h.posts.at(-1).url, '/ops/instructors/update/');
  const edit = h.d.querySelector('[data-im-edit="1"]');
  assert.ok(edit && edit.isConnected);
  assert.equal(h.d.activeElement, edit, 'the new Edit of the saved row');
});

test('a refusal is said in the server’s words; legacy "None" never returns to the form', async t => {
  const h = await page(t, { answer: url => (url === '/ops/instructors/update/'
    ? { httpError: 409, ok: false, error: { message: 'Another instructor already has this name or email' } } : undefined) });
  h.$('imTabBtnRoster').click();
  await new Promise(r => setTimeout(r, 20));
  h.d.querySelector('[data-im-edit="2"]').click();
  await new Promise(r => setTimeout(r, 20));
  assert.equal(h.$('imInstructorEmployeeNo').value, '', 'a stored "None" is not put back');
  h.$('imInstructorForm').requestSubmit(h.$('imInstructorSave'));
  for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 5));
  assert.equal(h.$('imInstructorError').textContent, 'Another instructor already has this name or email');
});

test('badges use the design system tones; bulk assign never picks the first of several', async t => {
  const h = await page(t);
  h.$('imTabBtnRoster').click();
  await new Promise(r => setTimeout(r, 20));
  assert.ok(h.d.querySelector('#imInstructorTable .pill-status').classList.contains('pill-g'));
  h.$('imTabBtnAssignments').click();
  h.$('caProgram').value = 'AI';
  h.$('caProgram').dispatchEvent(new h.w.Event('change'));
  await new Promise(r => setTimeout(r, 20));
  const box = h.d.querySelector('[data-ca-course="CS101"]');
  box.checked = true;
  box.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  const before = h.posts.length;
  // Both active instructors' names contain the letter ا.
  h.$('caBulkTypeahead').value = 'ا';
  h.$('caBulkAssign').click();
  await new Promise(r => setTimeout(r, 20));
  assert.equal(h.posts.length, before, 'several match: nothing assigned');
  assert.match(h.notices.at(-1).text, /instructors match; type the full name/);
  h.$('caBulkTypeahead').value = 'dr hala omar';
  h.$('caBulkAssign').click();
  await new Promise(r => setTimeout(r, 20));
  assert.equal(h.posts.at(-1).url, '/ops/instructors/course-assignments/assign-bulk/');
  assert.equal(h.posts.at(-1).body.instructor_id, 1);
  // Another programme: the ticks do not carry over.
  await new Promise(r => setTimeout(r, 20));
  const again = h.d.querySelector('[data-ca-course="CS101"]');
  again.checked = true;
  again.dispatchEvent(new h.w.Event('change', { bubbles: true }));
  assert.equal(h.$('caBulkbar').classList.contains('d-none'), false);
  h.$('caProgram').value = 'CS';
  h.$('caProgram').dispatchEvent(new h.w.Event('change'));
  await new Promise(r => setTimeout(r, 20));
  assert.equal(h.$('caBulkbar').classList.contains('d-none'), true);
});
