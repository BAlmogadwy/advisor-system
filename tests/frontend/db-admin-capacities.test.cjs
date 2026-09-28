/*
 * DB Admin's Programme Capacities panel (static/js/page-db-admin.js) on the
 * page its real view renders, in English and Arabic: run through
 * tests/test_db_admin_frontend.py. The page script runs unmodified; only HTTP
 * answers and (unless a test asks for the real static/js/dialog.js) the shared
 * confirmation dialog are scripted. Any request the suite does not expect
 * fails the test.
 *
 * The panel saves through Section Planning's write path: an edit is a draft
 * (no request on input), Save sends only the edited rows for the programme
 * that was LOADED, previews them, lists course · name · old → new for
 * confirmation and commits with the preview's token.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');

assert.ok(process.env.DBA_TEST_HTML, 'Run through pytest tests/test_db_admin_frontend.py');
const template = fs.readFileSync(process.env.DBA_TEST_HTML, 'utf8');
const AR = process.env.DBA_TEST_LANGUAGE === 'ar';
const say = (en, ar) => (AR ? ar : en);
const read = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const SHARED = read('shared-utils.js');
const DIALOG = read('dialog.js');
const PAGE = read('page-db-admin.js');

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

const LIST = '/ops/db/programme-capacities/';
const LIMITS = '/ops/db/programme-capacities/limits/';

/* The saved rows, per stored programme. AI492 is a graduation project in AI
 * and co-op training in AI2: one code, two courses. */
function savedRows() {
  return {
    AI: [
      { course_code: 'AI492', course_name: 'GRADUATION PROJECT II', credit_hours: 3, max_capacity: 5 },
      { course_code: 'CS211', course_name: 'DATA STRUCTURES', credit_hours: 3, max_capacity: 30 },
      { course_code: 'MATH203', course_name: '', credit_hours: 3, max_capacity: null },
    ],
    AI2: [
      { course_code: 'AI492', course_name: 'COOPERATIVE TRAINING', credit_hours: 3, max_capacity: 5 },
    ],
  };
}

function answer(data, status = 200) {
  return {
    ok: status >= 200 && status < 300, status, statusText: '',
    headers: { get: () => 'application/json' },
    json: async () => data,
  };
}

/* A server that behaves like section_limits for one programme: a preview
 * lists the rows that change, a commit writes them. Tests may script either. */
function limitsServer(store, { preview, commit } = {}) {
  const plan = body => {
    const rows = store[body.programs[0]] || [];
    const changes = [];
    let unchanged = 0;
    for (const change of body.changes) {
      const row = rows.find(r => r.course_code === change.course_code);
      if (row.max_capacity === change.max_capacity) { unchanged += 1; continue; }
      changes.push({ course_code: row.course_code, course_name: row.course_name, program: body.programs[0],
        old: row.max_capacity, new: change.max_capacity, scope: 'programmes' });
    }
    return { changes, unchanged };
  };
  return body => {
    if (body.dry_run === true) {
      if (preview) return preview(body);
      return answer({ ok: true, dry_run: true, programs: body.programs, preview_token: 'token-1', ...plan(body) });
    }
    if (commit) return commit(body);
    const { changes, unchanged } = plan(body);
    for (const change of changes) {
      store[body.programs[0]].find(r => r.course_code === change.course_code).max_capacity = change.new;
    }
    return answer({ ok: true, dry_run: false, programs: body.programs, changed: changes, changed_count: changes.length, unchanged });
  };
}

async function page(t, { confirm = async () => true, realDialogs = false, store = savedRows(), limits = null, list = null } = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(template, { url: 'http://admin.test/db-admin/', runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  t.after(() => { window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  const dialogs = [];
  window.notify = { success() {}, error() {}, warning() {} };
  if (!realDialogs) window.dlg = { confirm: async options => { dialogs.push(options); return confirm(options); } };
  const server = limits ? limits(store) : limitsServer(store);
  const requests = [];
  window.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ url, method, body });
    if (method === 'GET' && url.startsWith(`${LIST}?`)) {
      const asked = new URL(url, 'http://admin.test').searchParams.get('program');
      if (list) return list(asked);
      const program = asked.replace(/\s+/g, '').toUpperCase();
      return answer({ ok: true, program, rows: JSON.parse(JSON.stringify(store[program] || [])) });
    }
    if (method === 'POST' && url === LIMITS) return server(body);
    const error = new Error(`Unexpected HTTP request: ${method} ${url}`);
    errors.push(error);
    throw error;
  };
  const context = dom.getInternalVMContext();
  vm.runInContext(SHARED, context, { filename: 'shared-utils.js' });
  if (realDialogs) {
    window.requestAnimationFrame = callback => window.setTimeout(callback, 0);
    vm.runInContext(DIALOG, context, { filename: 'dialog.js' });
  }
  vm.runInContext(PAGE, context, { filename: 'page-db-admin.js' });
  const $ = id => window.document.getElementById(id);
  const emit = (element, type) => element.dispatchEvent(new window.Event(type, { bubbles: true }));
  const text = element => (typeof element === 'string' ? $(element) : element).textContent.replace(/\s+/g, ' ').trim();
  const row = code => window.document.querySelector(`#capBody tr[data-code="${code}"]`);
  const input = code => row(code).querySelector('.cap-input');
  const state = code => text(row(code).querySelector('.cap-state'));
  const setBox = value => { $('capProgram').value = value; emit($('capProgram'), 'input'); };
  const load = async program => { setBox(program); $('capLoad').click(); await settle(); };
  const type = async (code, value) => {
    const field = input(code);
    field.focus();
    field.value = value;
    emit(field, 'input');
    emit(field, 'change');
    field.blur();
    emit(field, 'blur');
    await settle();
  };
  const save = async () => { $('capSave').click(); await settle(); };
  const writes = () => requests.filter(request => request.method !== 'GET');
  const reads = () => requests.filter(request => request.method === 'GET');
  const saveOff = () => $('capSave').getAttribute('aria-disabled') === 'true';
  const leave = () => {
    const event = new window.Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  };
  /* The page script's own bindings (its word tables), read in its realm. */
  const run = code => vm.runInContext(code, context);
  return { window, $, emit, text, row, input, state, setBox, load, type, save, writes, reads, requests, dialogs, saveOff, leave, run, store };
}

/* The rows a review dialog lists, cell by cell. */
function reviewRows(ui, html) {
  const box = ui.window.document.createElement('div');
  box.innerHTML = html;
  return [...box.querySelectorAll('.sp-limit-review tbody tr')]
    .map(tr => [...tr.querySelectorAll('td')].map(td => td.textContent.replace(/\s+/g, ' ').trim()));
}

test('a loaded programme lists each course by code and name; nothing is a draft and Save is off', async t => {
  const ui = await page(t);
  await ui.load(' ai ');

  assert.deepEqual(ui.reads().map(r => r.url), [`${LIST}?program=AI`]);
  assert.deepEqual(
    [...ui.$('capBody').querySelectorAll('tr')].map(tr => [...tr.querySelectorAll('td')].slice(0, 4).map(td => ui.text(td))),
    [['AI492', 'GRADUATION PROJECT II', '3', '5'], ['CS211', 'DATA STRUCTURES', '3', '30'], ['MATH203', '—', '3', '--']],
  );
  assert.equal(ui.input('AI492').value, '5');
  assert.equal(ui.input('AI492').type, 'text', 'a number field would turn "abc" into an empty field, i.e. a removal');
  assert.equal(ui.input('AI492').getAttribute('aria-label'),
    say('New seat limit for AI492 GRADUATION PROJECT II', 'الحد الجديد لـ AI492 GRADUATION PROJECT II'));
  assert.equal(ui.$('capSave').classList.contains('d-none'), false);
  assert.equal(ui.saveOff(), true);
  assert.equal(ui.text('capSave'), say('Save changes', 'حفظ التغييرات'));
  assert.equal(ui.text('capStatus'), say('No unsaved changes', 'لا توجد تعديلات غير محفوظة'));
  assert.equal(ui.text('capCount'), say('3 courses · AI', '3 مقررات · AI'));
  assert.deepEqual(ui.writes(), []);
});

test('an edit is a draft: marked Modified, counted on Save, and nothing is sent', async t => {
  const ui = await page(t);
  await ui.load('AI');

  await ui.type('AI492', '8');

  assert.deepEqual(ui.writes(), []);
  assert.ok(ui.row('AI492').classList.contains('cap-draft'));
  assert.equal(ui.state('AI492'), say('Modified', 'معدَّل'));
  assert.equal(ui.saveOff(), false);
  assert.equal(ui.text('capSave'), say('Save changes (1)', 'حفظ التغييرات (1)'));
  assert.equal(ui.text('capStatus'), say('1 unsaved change', '1 تعديل غير محفوظ'));
  assert.ok(!ui.row('CS211').classList.contains('cap-draft'), 'an untouched row is not a draft');

  await ui.type('AI492', '5');
  assert.ok(!ui.row('AI492').classList.contains('cap-draft'), 'back to the saved value: no draft');
  assert.equal(ui.saveOff(), true);
});

test('Save sends only the edited rows of the loaded programme, confirms, commits with the token, then reloads', async t => {
  const ui = await page(t);
  await ui.load('AI');
  await ui.type('AI492', '8');
  await ui.type('MATH203', '١٢');

  await ui.save();

  const [preview, commit] = ui.writes();
  const changes = [{ course_code: 'AI492', max_capacity: 8 }, { course_code: 'MATH203', max_capacity: 12 }];
  assert.deepEqual(preview.body, { programs: ['AI'], changes, dry_run: true }, 'CS211 was not edited: it is not sent');
  assert.deepEqual(commit.body, { programs: ['AI'], changes, dry_run: false, preview_token: 'token-1' });
  assert.equal(ui.writes().length, 2);
  assert.equal(ui.dialogs.length, 1);
  assert.deepEqual(ui.reads().map(r => r.url), [`${LIST}?program=AI`, `${LIST}?program=AI`], 'the rows are reloaded');
  assert.equal(ui.input('AI492').value, '8');
  assert.equal(ui.input('MATH203').value, '12');
  assert.ok(!ui.row('AI492').classList.contains('cap-draft'), 'saved: no longer a draft');
  assert.equal(ui.saveOff(), true);
  assert.equal(ui.text('capOut'), say(
    'Saved 2 seat limits for AI; each change is in the audit log.',
    'حُفظ 2 حدّان لبرنامج AI وسُجّل كل تغيير في سجل التدقيق.',
  ));
});

test('the review lists course · name · old → new, starts on Keep editing, and Keep editing writes nothing', async t => {
  const ui = await page(t, { confirm: async () => false });
  await ui.load('AI');
  await ui.type('AI492', '8');

  await ui.save();

  assert.equal(ui.dialogs.length, 1);
  const review = ui.dialogs[0];
  assert.equal(review.title, say('Save 1 seat limit for AI?', 'حفظ 1 من حدود الشعب لبرنامج AI؟'));
  assert.equal(review.initialFocus, 'cancel');
  assert.equal(review.waitForClose, true);
  assert.equal(review.cancelText, say('Keep editing', 'متابعة التعديل'));
  assert.equal(review.confirmText, say('Save 1 change', 'حفظ 1 تغيير'));
  assert.deepEqual(reviewRows(ui, review.body), [['AI492', 'GRADUATION PROJECT II', '5', '8']]);
  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true], 'Keep editing sends no commit');
  assert.ok(ui.row('AI492').classList.contains('cap-draft'), 'the draft is kept');
  assert.equal(ui.input('AI492').value, '8');
  assert.equal(ui.saveOff(), false);
});

test('a new review clears what an earlier attempt said', async t => {
  const ui = await page(t, { confirm: async () => false });
  await ui.load('AI');
  await ui.type('AI492', 'abc');
  await ui.save();
  assert.equal(ui.text('capOut'), say('Fix the invalid values before saving.', 'صحّح القيم غير الصالحة قبل الحفظ.'));

  await ui.type('AI492', '8');
  await ui.save();

  assert.equal(ui.dialogs.length, 1);
  assert.equal(ui.text('capOut'), '', 'kept editing: nothing stale is left on screen');
});

test('an emptied saved limit is a removal: marked, sent as null, and the review says remove', async t => {
  const ui = await page(t);
  await ui.load('AI');

  await ui.type('MATH203', '');
  assert.ok(!ui.row('MATH203').classList.contains('cap-draft'), 'an empty row with no saved limit is not a draft');
  await ui.type('CS211', '');
  assert.equal(ui.state('CS211'), say('Modified: limit removed', 'معدَّل: يُزال الحد'));
  assert.equal(ui.input('CS211').value, '', 'an emptied field stays empty');

  await ui.save();

  assert.deepEqual(ui.writes()[0].body.changes, [{ course_code: 'CS211', max_capacity: null }]);
  assert.deepEqual(reviewRows(ui, ui.dialogs[0].body), [[
    'CS211', 'DATA STRUCTURES', '30', say('remove (the rule applies)', 'إزالة (تُطبَّق القاعدة)'),
  ]]);
  assert.equal(ui.store.AI.find(r => r.course_code === 'CS211').max_capacity, null);
  assert.equal(ui.input('CS211').value, '');
});

test('typing another programme after loading blocks Save until that programme is loaded', async t => {
  const ui = await page(t);
  await ui.load('AI');
  await ui.type('AI492', '8');

  ui.setBox('AI2');
  const blocked = say(
    'The rows on screen are for AI, but the box says AI2. Load that programme, or type AI again, to save.',
    'الصفوف المعروضة لبرنامج AI، والمكتوب في الحقل AI2. حمّل ذلك البرنامج أو أعد كتابة AI لتتمكن من الحفظ.',
  );
  assert.equal(ui.saveOff(), true);
  assert.equal(ui.text('capStatus'), `${say('1 unsaved change', '1 تعديل غير محفوظ')} · ${blocked}`);
  await ui.save();
  assert.deepEqual(ui.writes(), [], 'AI\'s rows are never written into AI2');
  assert.equal(ui.text('capOut'), blocked);

  ui.setBox(' ai');
  assert.equal(ui.saveOff(), false, 'the loaded programme again: Save is back');
  await ui.save();
  assert.deepEqual(ui.writes().map(w => w.body.programs), [['AI'], ['AI']]);
  assert.equal(ui.store.AI2[0].max_capacity, 5, 'AI2\'s AI492 is another course, untouched');
  assert.equal(ui.store.AI[0].max_capacity, 8);
});

test('the commit writes the programme the review showed, even if the box changes meanwhile', async t => {
  let release;
  const ui = await page(t, {
    limits: store => {
      const real = limitsServer(store);
      return body => (body.dry_run ? new Promise(resolve => { release = () => resolve(real(body)); }) : real(body));
    },
  });
  await ui.load('AI');
  await ui.type('AI492', '8');

  ui.$('capSave').click();
  await settle();
  ui.setBox('AI2');   // typed while the review is on its way
  release();
  await settle();

  assert.equal(ui.dialogs[0].title, say('Save 1 seat limit for AI?', 'حفظ 1 من حدود الشعب لبرنامج AI؟'));
  assert.deepEqual(ui.writes().map(w => w.body.programs), [['AI'], ['AI']]);
  assert.equal(ui.store.AI[0].max_capacity, 8);
  assert.equal(ui.store.AI2[0].max_capacity, 5);
});

test('Load with unsaved changes asks first; Keep editing keeps them, Discard loads the other programme', async t => {
  let answerNext = false;
  const ui = await page(t, { confirm: async () => answerNext });
  await ui.load('AI');
  await ui.type('AI492', '8');

  await ui.load('AI2');
  assert.equal(ui.dialogs.length, 1);
  assert.equal(ui.dialogs[0].title, say('Discard 1 unsaved change?', 'تجاهل 1 تعديل غير محفوظ؟'));
  assert.equal(ui.dialogs[0].initialFocus, 'cancel');
  assert.equal(ui.reads().length, 1, 'kept editing: AI2 is not loaded');
  assert.equal(ui.input('AI492').value, '8');

  answerNext = true;
  await ui.load('AI2');
  assert.equal(ui.reads().length, 2);
  assert.equal(ui.text(ui.row('AI492').querySelectorAll('td')[1]), 'COOPERATIVE TRAINING');
  assert.equal(ui.input('AI492').value, '5', 'AI\'s draft is not carried into AI2');
  await ui.type('AI492', '7');
  await ui.save();
  assert.deepEqual(ui.writes()[0].body, { programs: ['AI2'], changes: [{ course_code: 'AI492', max_capacity: 7 }], dry_run: true });
});

test('an invalid value blocks Save and says what to fix', async t => {
  const ui = await page(t);
  await ui.load('AI');

  for (const bad of ['abc', '0', '501', '2.5']) {
    await ui.type('AI492', bad);
    assert.ok(ui.row('AI492').classList.contains('cap-invalid'), bad);
    assert.equal(ui.input('AI492').getAttribute('aria-invalid'), 'true');
    assert.equal(ui.state('AI492'), say('Whole number 1–500', 'رقم صحيح من 1 إلى 500'));
  }
  await ui.type('CS211', '28');
  assert.equal(ui.saveOff(), true);
  assert.equal(ui.text('capStatus'), say('1 unsaved change · 1 value to fix before saving', '1 تعديل غير محفوظ · 1 قيمة تحتاج تصحيحاً قبل الحفظ'));
  await ui.save();
  assert.deepEqual(ui.writes(), []);
  assert.equal(ui.text('capOut'), say('Fix the invalid values before saving.', 'صحّح القيم غير الصالحة قبل الحفظ.'));
});

test('saved limits that moved under the review are said in the page words; the rows reload and only the drafts stay', async t => {
  const cases = [
    ['preview', answer({ ok: false, code: 'preview_stale', error: 'server words', changes: [] }, 409), say(
      'The saved limits changed since you reviewed them. Press Save changes again to review the new list.',
      'تغيّرت الحدود المحفوظة منذ المراجعة. اضغط حفظ التغييرات مرة أخرى لمراجعة القائمة الجديدة.',
    )],
    ['commit', answer({ ok: false, code: 'limit_changed', error: 'server words' }, 409), say(
      'A limit changed while saving, so nothing was saved. Press Save changes again.',
      'تغيّر حد أثناء الحفظ فلم يُحفظ شيء. اضغط حفظ التغييرات مرة أخرى.',
    )],
  ];
  for (const [stage, reply, words] of cases) {
    await t.test(`${stage} ${reply.status}`, async st => {
      const ui = await page(st, {
        limits: store => limitsServer(store, {
          [stage]: () => {
            /* Someone else saved AI492 = 6 and CS211 = 45 meanwhile; CS211 was never edited here. */
            store.AI[0].max_capacity = 6;
            store.AI[1].max_capacity = 45;
            return reply;
          },
        }),
      });
      await ui.load('AI');
      await ui.type('AI492', '8');

      await ui.save();

      assert.equal(ui.text('capOut'), words);
      assert.equal(ui.reads().length, 2, 'the rows are reloaded');
      assert.equal(ui.text(ui.row('AI492').querySelectorAll('td')[3]), '6', 'the saved limit is the new one');
      assert.equal(ui.input('AI492').value, '8', 'the draft is kept');
      assert.ok(ui.row('AI492').classList.contains('cap-draft'));
      assert.equal(ui.dialogs.length, stage === 'commit' ? 1 : 0);

      /* A course nobody edited here takes the other save's value: it is not a draft that would undo it. */
      assert.equal(ui.text(ui.row('CS211').querySelectorAll('td')[3]), '45');
      assert.equal(ui.input('CS211').value, '45');
      assert.ok(!ui.row('CS211').classList.contains('cap-draft'), 'CS211 was never edited');
      assert.equal(ui.text('capSave'), say('Save changes (1)', 'حفظ التغييرات (1)'));

      await ui.save();
      const again = ui.writes().filter(w => w.body.dry_run === true).at(-1);
      assert.deepEqual(again.body.changes, [{ course_code: 'AI492', max_capacity: 8 }], 'only the edited course is sent again');
    });
  }
});

test('refusals and failures are said in the page\'s words, and nothing is reloaded over the drafts', async t => {
  const cases = [
    [answer({ ok: false, code: 'audit_unavailable', error: 'x' }, 503), 'commit',
      say("Couldn't record the change in the audit log, so nothing was saved. Try again.", 'تعذّر تسجيل التغيير في سجل التدقيق، لذلك لم يُحفظ شيء. حاول مرة أخرى.')],
    [answer({ ok: false, code: 'invalid_limit', course_code: 'AI492', error: 'x' }, 400), 'preview',
      say('The limit for AI492 must be a whole number from 1 to 500.', 'حد AI492 يجب أن يكون رقماً صحيحاً من 1 إلى 500.')],
    [answer({ ok: false, code: 'course_not_in_programmes', course_code: 'AI492', error: 'x' }, 400), 'preview',
      say('AI492 is not taught by AI.', 'AI492 لا يُدرَّس في AI.')],
    [answer({ error: 'Insufficient role: requires SUPER_ADMIN' }, 403), 'preview',
      say('You are not allowed to save limits.', 'لا تملك صلاحية حفظ الحدود.')],
    [answer({ error: 'Rate limit exceeded.' }, 429), 'preview',
      say('Too many requests. Wait a moment and try again.', 'طلبات كثيرة. انتظر قليلاً ثم حاول.')],
  ];
  for (const [reply, stage, words] of cases) {
    await t.test(`${stage} ${reply.status}`, async st => {
      const scripted = stage === 'commit' ? { commit: () => reply } : { preview: () => reply };
      const ui = await page(st, { limits: store => limitsServer(store, scripted) });
      await ui.load('AI');
      await ui.type('AI492', '8');

      await ui.save();

      assert.equal(ui.text('capOut'), words);
      assert.equal(ui.reads().length, 1, 'no reload');
      assert.equal(ui.input('AI492').value, '8');
      assert.equal(ui.saveOff(), false, 'Save can be tried again');
      assert.equal(ui.text('capSave'), say('Save changes (1)', 'حفظ التغييرات (1)'));
    });
  }
});

test('a preview that changes nothing says so and writes nothing', async t => {
  const ui = await page(t, {
    limits: store => limitsServer(store, {
      preview: body => answer({ ok: true, dry_run: true, programs: body.programs, preview_token: 't', changes: [], unchanged: 1 }),
    }),
  });
  await ui.load('AI');
  await ui.type('AI492', '8');

  await ui.save();

  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true]);
  assert.equal(ui.dialogs.length, 0);
  assert.equal(ui.text('capOut'), say('Nothing to change: the saved limits already match.', 'لا شيء يتغيّر: الحدود المحفوظة مطابقة.'));
  assert.equal(ui.reads().length, 2, 'the saved limits are reloaded');
});

test('the real review dialog starts on Keep editing; Enter there saves nothing; confirming saves', async t => {
  const ui = await page(t, { realDialogs: true });
  await ui.load('AI');
  await ui.type('AI492', '8');
  const save = ui.$('capSave');
  save.focus();
  save.click();
  await settle();
  assert.equal(ui.text('capSave'), say('Checking…', 'جارٍ التحقق…'), 'reviewing is not saving');
  await pause(80);

  let dialog = ui.window.document.querySelector('.dlg-backdrop');
  assert.ok(dialog, 'the review is open');
  const cancel = dialog.querySelector('.btn-cancel');
  assert.equal(ui.window.document.activeElement, cancel, 'a review starts on "Keep editing"');
  const described = ui.window.document.getElementById(dialog.getAttribute('aria-describedby'));
  assert.deepEqual(reviewRows(ui, described.innerHTML), [['AI492', 'GRADUATION PROJECT II', '5', '8']]);
  const event = new ui.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  cancel.dispatchEvent(event);
  if (!event.defaultPrevented) cancel.click();
  await pause(260);
  await settle();
  assert.equal(ui.window.document.querySelector('.dlg-backdrop'), null);
  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true], 'Enter on Keep editing never commits');
  assert.equal(ui.window.document.activeElement, save, 'the keyboard is back on Save');

  save.click();
  await settle();
  await pause(80);
  dialog = ui.window.document.querySelector('.dlg-backdrop');
  dialog.querySelector('.btn-confirm').click();
  await pause(260);
  await settle();
  assert.deepEqual(ui.writes().map(w => w.body.dry_run), [true, true, false]);
  assert.equal(ui.store.AI[0].max_capacity, 8);
  assert.equal(ui.window.document.activeElement, save, 'Save keeps the keyboard after the reload');
  assert.equal(ui.text('capSave'), say('Save changes', 'حفظ التغييرات'));
});

test('leaving with unsaved changes asks the browser to confirm', async t => {
  const ui = await page(t);
  await ui.load('AI');
  const leave = () => {
    const event = new ui.window.Event('beforeunload', { cancelable: true });
    ui.window.dispatchEvent(event);
    return event.defaultPrevented;
  };
  assert.equal(leave(), false);
  await ui.type('AI492', '8');
  assert.equal(leave(), true);
});

/* A list answer that failed: a gateway page (not JSON), or a JSON refusal. */
function failedList(kind) {
  if (kind === 'html') {
    return {
      ok: false, status: 502, statusText: 'Bad Gateway',
      headers: { get: () => 'text/html' },
      text: async () => '<html><body>Bad gateway</body></html>',
    };
  }
  return answer({ error: 'Insufficient role: requires SUPER_ADMIN' }, 403);
}

/* A list that answers `good` times from the store, then fails. */
function listThatFails(store, good, kind = 'html') {
  let calls = 0;
  return asked => {
    calls += 1;
    if (calls > good) return failedList(kind);
    const program = asked.replace(/\s+/g, '').toUpperCase();
    return answer({ ok: true, program, rows: JSON.parse(JSON.stringify(store[program] || [])) });
  };
}

const LIST_FAILED = say('The list could not be loaded. Try again.', 'تعذّر تحميل القائمة. حاول مرة أخرى.');
const REFRESH_FAILED = say(
  'The list on screen could not be refreshed and may be out of date: press Load to refresh it.',
  'تعذّر تحديث القائمة المعروضة وقد لا تكون محدَّثة: اضغط "تحميل" لتحديثها.',
);

test('a saved limit outside 1–500 that nobody edits is neither invalid nor a draft, and the other rows still save', async t => {
  const store = savedRows();
  store.AI.push(
    { course_code: 'BIOL101', course_name: 'BIOLOGY', credit_hours: 3, max_capacity: 0 },
    { course_code: 'CHEM101', course_name: 'CHEMISTRY', credit_hours: 3, max_capacity: 9999 },
  );
  const ui = await page(t, { store });
  await ui.load('AI');

  for (const code of ['BIOL101', 'CHEM101']) {
    assert.ok(!ui.row(code).classList.contains('cap-invalid'), `${code} was not edited`);
    assert.ok(!ui.row(code).classList.contains('cap-draft'), code);
    assert.equal(ui.input(code).getAttribute('aria-invalid'), 'false');
    assert.equal(ui.state(code), say('Saved value outside 1–500', 'القيمة المحفوظة خارج المدى من 1 إلى 500'));
  }
  assert.equal(ui.text('capStatus'), say('No unsaved changes', 'لا توجد تعديلات غير محفوظة'));
  assert.equal(ui.leave(), false, 'nothing was edited: leaving is not guarded');

  await ui.type('CS211', '31');
  assert.equal(ui.text('capStatus'), say('1 unsaved change', '1 تعديل غير محفوظ'));
  assert.equal(ui.saveOff(), false);
  await ui.save();
  assert.deepEqual(ui.writes()[0].body.changes, [{ course_code: 'CS211', max_capacity: 31 }]);
  assert.equal(ui.store.AI.find(r => r.course_code === 'CHEM101').max_capacity, 9999, 'untouched');

  await ui.load('AI2');
  assert.equal(ui.dialogs.length, 1, 'only the review: loading another programme asks nothing');

  await ui.load('AI');
  await ui.type('BIOL101', '600');
  assert.ok(ui.row('BIOL101').classList.contains('cap-invalid'), 'an edited value is checked');
  await ui.type('BIOL101', '');
  assert.equal(ui.state('BIOL101'), say('Modified: limit removed', 'معدَّل: يُزال الحد'));
});

test('while a review is on its way the fields are read-only, Save is off, and Save and Load are ignored', async t => {
  let release;
  const ui = await page(t, {
    limits: store => {
      const real = limitsServer(store);
      return body => (body.dry_run && !release ? new Promise(resolve => { release = () => resolve(real(body)); }) : real(body));
    },
  });
  await ui.load('AI');
  await ui.type('AI492', '8');
  assert.equal(ui.input('CS211').readOnly, false);

  ui.$('capSave').click();
  ui.$('capSave').click();   // a double click
  await settle();
  ui.$('capSave').click();   // Enter again, still checking
  await settle();

  assert.equal(ui.text('capSave'), say('Checking…', 'جارٍ التحقق…'));
  assert.equal(ui.saveOff(), true, 'Save is off while it runs');
  assert.equal(ui.$('capBody').querySelectorAll('.cap-input').length, 3);
  for (const field of ui.$('capBody').querySelectorAll('.cap-input')) assert.equal(field.readOnly, true, 'no edit can be lost to the reload');
  ui.setBox('AI2');
  ui.$('capLoad').click();
  await settle();
  assert.equal(ui.reads().length, 1, 'Load waits for the save');
  assert.equal(ui.dialogs.length, 0, 'no discard question over a save in flight');
  ui.setBox('AI');

  release();
  await settle();

  assert.equal(ui.writes().filter(w => w.body.dry_run).length, 1, 'one review');
  assert.equal(ui.dialogs.length, 1, 'one dialog');
  assert.equal(ui.writes().length, 2);
  assert.equal(ui.store.AI[0].max_capacity, 8);
  for (const field of ui.$('capBody').querySelectorAll('.cap-input')) assert.equal(field.readOnly, false, 'editable again');
  assert.equal(ui.text('capSave'), say('Save changes', 'حفظ التغييرات'));
});

test('while the commit and the reload after it run the fields stay read-only and Save is off; then they are editable again', async t => {
  let commitDone, reloadDone;
  let lists = 0;
  const store = savedRows();
  const rowsOf = asked => {
    const program = asked.replace(/\s+/g, '').toUpperCase();
    return answer({ ok: true, program, rows: JSON.parse(JSON.stringify(store[program] || [])) });
  };
  const ui = await page(t, {
    store,
    /* The first load answers at once; the reload after the save waits. */
    list: asked => {
      lists += 1;
      return lists === 2 ? new Promise(resolve => { reloadDone = () => resolve(rowsOf(asked)); }) : rowsOf(asked);
    },
    limits: s => {
      const real = limitsServer(s);
      return body => (body.dry_run ? real(body) : new Promise(resolve => { commitDone = () => resolve(real(body)); }));
    },
  });
  await ui.load('AI');
  await ui.type('AI492', '8');
  const readOnly = () => [...ui.$('capBody').querySelectorAll('.cap-input')].map(field => field.readOnly);
  const busy = stage => {
    assert.deepEqual(readOnly(), [true, true, true], `${stage}: no edit can be lost to the reload`);
    assert.equal(ui.saveOff(), true, `${stage}: Save is off`);
    assert.equal(ui.input('CS211').value, '30', `${stage}: another row keeps its value`);
  };

  ui.$('capSave').click();
  await settle();
  assert.ok(commitDone, 'the review was confirmed and the commit is on its way');
  assert.equal(ui.text('capSave'), say('Saving...', 'جارٍ الحفظ...'));
  busy('commit');
  ui.setBox('AI');   // the panel redraws while the commit runs
  busy('commit, after a redraw');
  ui.$('capLoad').click();
  await settle();
  assert.equal(ui.reads().length, 1, 'Load waits for the commit');
  assert.equal(ui.dialogs.length, 1, 'only the review');

  commitDone();
  await settle();
  assert.ok(reloadDone, 'the reload is on its way');
  assert.equal(ui.store.AI[0].max_capacity, 8);
  assert.equal(ui.text(ui.row('AI492').querySelectorAll('td')[3]), '8', 'what the commit wrote reads as saved at once');
  busy('reload');
  ui.setBox('AI');   // and while the reload runs
  busy('reload, after a redraw');

  reloadDone();
  await settle();
  assert.deepEqual(readOnly(), [false, false, false], 'editable again');
  assert.equal(ui.input('CS211').value, '30');
  assert.equal(ui.text('capSave'), say('Save changes', 'حفظ التغييرات'));
  assert.equal(ui.saveOff(), true, 'nothing left to save');
  assert.equal(ui.text('capOut'), say(
    'Saved 1 seat limit for AI; each change is in the audit log.',
    'حُفظ 1 حد لبرنامج AI وسُجّل كل تغيير في سجل التدقيق.',
  ));
  await ui.type('CS211', '31');
  assert.equal(ui.saveOff(), false, 'a new edit can be saved');
});

test('a list that fails to load is said in the page words and leaves the rows, their programme and the drafts as they were', async t => {
  for (const kind of ['html', 'json']) {
    await t.test(kind, async st => {
      const store = savedRows();
      const ui = await page(st, { store, list: listThatFails(store, 1, kind) });
      await ui.load('AI');
      await ui.type('AI492', '8');

      await ui.load('AI2');   // the discard question is answered yes

      assert.equal(ui.dialogs.length, 1);
      assert.equal(ui.text('capOut'), LIST_FAILED, 'never the raw answer');
      assert.ok(ui.$('capOut').classList.contains('has-error'));
      assert.equal(ui.text(ui.row('AI492').querySelectorAll('td')[1]), 'GRADUATION PROJECT II', 'AI\'s rows stay');
      assert.equal(ui.input('AI492').value, '8');
      assert.ok(ui.row('AI492').classList.contains('cap-draft'));
      assert.equal(ui.text('capCount'), say('3 courses · AI', '3 مقررات · AI'));
      ui.setBox('AI');
      assert.equal(ui.saveOff(), false, 'still AI\'s rows: Save writes AI');
    });
  }
});

test('a reload that fails after a save keeps the saved message, says the list may be stale, and the saved rows are no longer drafts', async t => {
  const store = savedRows();
  const ui = await page(t, { store, list: listThatFails(store, 1) });
  await ui.load('AI');
  await ui.type('AI492', '8');

  await ui.save();

  assert.equal(ui.store.AI[0].max_capacity, 8);
  assert.equal(ui.text('capOut'), `${say(
    'Saved 1 seat limit for AI; each change is in the audit log.',
    'حُفظ 1 حد لبرنامج AI وسُجّل كل تغيير في سجل التدقيق.',
  )} ${REFRESH_FAILED}`);
  assert.equal(ui.text(ui.row('AI492').querySelectorAll('td')[3]), '8', 'the row shows what was written');
  assert.ok(!ui.row('AI492').classList.contains('cap-draft'), 'saved: not a draft');
  assert.equal(ui.saveOff(), true);
  assert.equal(ui.text('capSave'), say('Save changes', 'حفظ التغييرات'));
  assert.equal(ui.leave(), false, 'nothing unsaved');
  await ui.load('AI');
  assert.equal(ui.dialogs.length, 1, 'Load does not ask to discard a saved limit');
});

test('a reload that fails after a stale answer keeps the rows and the drafts', async t => {
  const store = savedRows();
  const ui = await page(t, {
    store,
    list: listThatFails(store, 1),
    limits: s => limitsServer(s, { preview: () => answer({ ok: false, code: 'preview_stale', error: 'x', changes: [] }, 409) }),
  });
  await ui.load('AI');
  await ui.type('AI492', '8');

  await ui.save();

  assert.equal(ui.reads().length, 2, 'a reload was tried');
  assert.equal(ui.text('capOut'), `${say(
    'The saved limits changed since you reviewed them. Press Save changes again to review the new list.',
    'تغيّرت الحدود المحفوظة منذ المراجعة. اضغط حفظ التغييرات مرة أخرى لمراجعة القائمة الجديدة.',
  )} ${REFRESH_FAILED}`);
  assert.equal(ui.input('AI492').value, '8');
  assert.ok(ui.row('AI492').classList.contains('cap-draft'));
  assert.equal(ui.$('capBody').querySelectorAll('tr').length, 3);
  assert.equal(ui.saveOff(), false);
});

test('the review says how many requested rows already match', async t => {
  for (const [unchanged, en, ar] of [
    [1, '1 row already matches and will not change.', '1 صف مطابق أصلاً ولن يتغيّر.'],
    [2, '2 rows already match and will not change.', '2 صفان مطابقان أصلاً ولن يتغيّرا.'],
  ]) {
    await t.test(String(unchanged), async st => {
      const ui = await page(st, {
        confirm: async () => false,
        limits: store => limitsServer(store, {
          preview: body => answer({
            ok: true, dry_run: true, programs: body.programs, preview_token: 't', unchanged,
            changes: [{ course_code: 'AI492', course_name: 'GRADUATION PROJECT II', program: 'AI', old: 5, new: 8, scope: 'programmes' }],
          }),
        }),
      });
      await ui.load('AI');
      await ui.type('AI492', '8');
      await ui.save();

      const box = ui.window.document.createElement('div');
      box.innerHTML = ui.dialogs[0].body;
      assert.equal(ui.text(box.querySelector('p.fs-sm')), say(en, ar));
    });
  }
});

test('a value still to fix counts as unsaved: Load asks before dropping it and leaving is guarded', async t => {
  const ui = await page(t, { confirm: async () => false });
  await ui.load('AI');
  await ui.type('AI492', 'abc');

  assert.equal(ui.leave(), true);
  await ui.load('AI2');
  assert.equal(ui.dialogs.length, 1);
  assert.equal(ui.dialogs[0].title, say('Discard 1 unsaved change?', 'تجاهل 1 تعديل غير محفوظ؟'));
  assert.equal(ui.reads().length, 1, 'kept editing');
  assert.equal(ui.input('AI492').value, 'abc');
});

test('counts agree with their number in both languages', async t => {
  const ui = await page(t);
  const forms = [1, 2, 3, 11, 100];
  const table = expr => forms.map(n => ui.run(`(${expr})(${n})`));
  assert.deepEqual(table('n => TC.drafts(n)'), say(
    ['1 unsaved change', '2 unsaved changes', '3 unsaved changes', '11 unsaved changes', '100 unsaved changes'],
    ['1 تعديل غير محفوظ', '2 تعديلان غير محفوظين', '3 تعديلات غير محفوظة', '11 تعديلاً غير محفوظ', '100 تعديل غير محفوظ'],
  ));
  assert.deepEqual(table('n => TC.discardTitle(n)'), say(
    ['Discard 1 unsaved change?', 'Discard 2 unsaved changes?', 'Discard 3 unsaved changes?', 'Discard 11 unsaved changes?', 'Discard 100 unsaved changes?'],
    ['تجاهل 1 تعديل غير محفوظ؟', 'تجاهل 2 تعديلين غير محفوظين؟', 'تجاهل 3 تعديلات غير محفوظة؟', 'تجاهل 11 تعديلاً غير محفوظ؟', 'تجاهل 100 تعديل غير محفوظ؟'],
  ));
  assert.deepEqual(table('n => TC.toFix(n)'), say(
    ['1 value to fix before saving', '2 values to fix before saving', '3 values to fix before saving', '11 values to fix before saving', '100 values to fix before saving'],
    ['1 قيمة تحتاج تصحيحاً قبل الحفظ', '2 قيمتان تحتاجان تصحيحاً قبل الحفظ', '3 قيم تحتاج تصحيحاً قبل الحفظ', '11 قيمةً تحتاج تصحيحاً قبل الحفظ', '100 قيمة تحتاج تصحيحاً قبل الحفظ'],
  ));
  assert.deepEqual(table('n => TC.confirmSave(n)'), say(
    ['Save 1 change', 'Save 2 changes', 'Save 3 changes', 'Save 11 changes', 'Save 100 changes'],
    ['حفظ 1 تغيير', 'حفظ 2 تغييرين', 'حفظ 3 تغييرات', 'حفظ 11 تغييراً', 'حفظ 100 تغيير'],
  ));
  assert.deepEqual(table('n => TC.saved(n, "AI")'), say(
    forms.map(n => `Saved ${n} seat limit${n === 1 ? '' : 's'} for AI; each change is in the audit log.`),
    ['1 حد', '2 حدّان', '3 حدود', '11 حداً', '100 حد'].map(c => `حُفظ ${c} لبرنامج AI وسُجّل كل تغيير في سجل التدقيق.`),
  ));
  assert.deepEqual(table('n => TC.count(n, "AI")'), say(
    ['1 course · AI', '2 courses · AI', '3 courses · AI', '11 courses · AI', '100 courses · AI'],
    ['1 مقرر · AI', '2 مقرران · AI', '3 مقررات · AI', '11 مقرراً · AI', '100 مقرر · AI'],
  ));
  assert.deepEqual(table('n => TC.loaded(n, "AI")'), say(
    ['1 course', '2 courses', '3 courses', '11 courses', '100 courses'].map(c => `Loaded ${c} for program "AI".`),
    ['1 مقرر', '2 مقررين', '3 مقررات', '11 مقرراً', '100 مقرر'].map(c => `تم تحميل ${c} للبرنامج "AI".`),
  ));
  assert.deepEqual(table('n => TC.unchanged(n)'), say(
    ['1 row already matches', '2 rows already match', '3 rows already match', '11 rows already match', '100 rows already match'].map(c => `${c} and will not change.`),
    ['1 صف مطابق أصلاً ولن يتغيّر.', '2 صفان مطابقان أصلاً ولن يتغيّرا.', '3 صفوف مطابقة أصلاً ولن تتغيّر.', '11 صفاً مطابقاً أصلاً ولن تتغيّر.', '100 صف مطابق أصلاً ولن تتغيّر.'],
  ));

  /* And on the page: a loaded programme's count. */
  await ui.load('AI2');
  assert.equal(ui.text('capCount'), say('1 course · AI2', '1 مقرر · AI2'));
  assert.equal(ui.text('capOut'), say('Loaded 1 course for program "AI2".', 'تم تحميل 1 مقرر للبرنامج "AI2".'));
});
