/* Real rendered user-management forms and production JavaScript, in EN and AR. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { JSDOM, VirtualConsole } = require('jsdom');

const template = fs.readFileSync(process.env.USER_TEST_HTML, 'utf8');
const language = process.env.USER_TEST_LANGUAGE;
const roleLabel = language === 'ar' ? 'لجنة الاختبارات' : 'Exam Committee';
const source = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const users = [
  { username: 'administrator', role: 'SUPER_ADMIN', is_active: true },
  { username: 'advisor', role: 'ADVISOR', advisor_id: 'A001', is_active: true },
  { username: 'general-advisor', role: 'GENERAL_ACADEMIC_ADVISOR', departments: ['AI', 'CS'], is_active: true },
  { username: 'exam-member', role: 'EXAM_COMMITTEE', advisor_id: 'stale-advisor', departments: ['stale-department'], is_active: true },
];

async function page(t, { failedUpdate = false, items = users } = {}) {
  const errors = [];
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(error.message));
  const dom = new JSDOM(template, { url: 'http://exam.test/user-management/', runScripts: 'outside-only', virtualConsole: console });
  const { window } = dom;
  const requests = [];
  const notices = [];
  const dialogs = [];
  window.notify = Object.fromEntries(['error', 'warning', 'success'].map(name => [name, (...args) => notices.push({ name, args })]));
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.document.cookie = 'csrftoken=test-csrf';
  window.dlg = {
    confirm: async options => { dialogs.push(options); return true; },
    prompt: async options => { dialogs.push(options); return 'replacement-password'; },
  };
  window.fetch = async (url, options = {}) => {
    requests.push({ url, ...options });
    if (url === '/ops/users/list/') return { ok: true, status: 200, json: async () => ({ items }) };
    assert.ok(['/ops/users/create/', '/ops/users/update-role/', '/ops/users/set-active/', '/ops/users/set-password/', '/ops/users/delete/'].includes(url));
    const fail = failedUpdate && url === '/ops/users/update-role/';
    return { ok: !fail, status: fail ? 403 : 200, json: async () => fail ? { error: 'Permission denied' } : { ok: true } };
  };
  vm.runInContext(source('shared-utils.js'), dom.getInternalVMContext());
  vm.runInContext(source('shared-ux.js'), dom.getInternalVMContext());
  vm.runInContext(source('page-user-management.js'), dom.getInternalVMContext());
  // The outside-only DOM does not execute inline handlers automatically. Bind
  // the rendered handlers so interactions still follow the production forms.
  for (const element of window.document.querySelectorAll('[onchange], [onclick]')) {
    for (const event of ['change', 'click']) {
      const handler = element.getAttribute(`on${event}`);
      if (handler) element.addEventListener(event, new window.Function('event', handler));
    }
  }
  t.after(() => { window.close(); assert.deepEqual(errors, []); });
  const $ = id => window.document.getElementById(id);
  const select = (id, value) => { $(id).value = value; $(id).dispatchEvent(new window.Event('change', { bubbles: true })); };
  await settle();
  return { window, $, select, requests, notices, dialogs };
}

test('Exam Committee is localized in create/edit/filter controls, its badge and counts', async t => {
  const ui = await page(t);
  for (const id of ['cRole', 'eRole', 'umRoleFilter']) {
    const option = ui.$(id).querySelector('[value="EXAM_COMMITTEE"]');
    assert.equal(option.textContent, roleLabel);
  }
  assert.equal(ui.$('statTotal').textContent, '4');
  assert.equal(ui.$('statAdv').textContent, '1');
  assert.equal(ui.$('statExamCommittee').textContent, '1');
  ui.select('umRoleFilter', 'EXAM_COMMITTEE');
  const rows = ui.$('umTable').querySelectorAll('tbody tr');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].dataset.username, 'exam-member');
  assert.equal(rows[0].querySelector('.um-role-badge').textContent, roleLabel);
  assert.equal(rows[0].cells[3].textContent, '—');
  assert.equal(rows[0].cells[4].textContent, '—');
  ui.select('umRoleFilter', 'ADVISOR');
  assert.equal(ui.$('umTable').querySelector('tbody tr').dataset.username, 'advisor');
});

test('creating a committee account hides scopes and sends none after changing roles', async t => {
  const ui = await page(t);
  assert.equal(ui.$('cAdvisorWrap').classList.contains('hidden'), false);
  ui.$('cAdvisorId').value = 'A001';
  ui.select('cRole', 'GENERAL_ACADEMIC_ADVISOR');
  assert.equal(ui.$('cDeptWrap').classList.contains('hidden'), false);
  ui.$('cDepartments').value = 'AI, CS';
  ui.select('cRole', 'EXAM_COMMITTEE');
  assert.equal(ui.$('cAdvisorWrap').classList.contains('hidden'), true);
  assert.equal(ui.$('cDeptWrap').classList.contains('hidden'), true);
  ui.$('cUsername').value = 'new-committee';
  ui.$('cPassword').value = 'test-password';
  ui.$('cCreateBtn').click();
  await settle();
  const request = ui.requests.find(item => item.url === '/ops/users/create/');
  assert.deepEqual(JSON.parse(request.body), { username: 'new-committee', password: 'test-password', role: 'EXAM_COMMITTEE', advisor_id: '', departments: '' });
  assert.equal(request.headers['X-CSRFToken'], 'test-csrf');
});

test('editing a user to committee clears scope payload and existing roles keep their scope controls', async t => {
  const ui = await page(t);
  ui.window.selectUser('general-advisor');
  assert.equal(ui.$('eDeptWrap').classList.contains('hidden'), false);
  assert.equal(ui.$('eDepartments').value, 'AI, CS');
  ui.select('eRole', 'EXAM_COMMITTEE');
  assert.equal(ui.$('eAdvisorWrap').classList.contains('hidden'), true);
  assert.equal(ui.$('eDeptWrap').classList.contains('hidden'), true);
  ui.$('editPanel').querySelector('.um-act-save').click();
  await settle();
  const request = ui.requests.find(item => item.url === '/ops/users/update-role/');
  assert.deepEqual(JSON.parse(request.body), { username: 'general-advisor', role: 'EXAM_COMMITTEE', advisor_id: '', departments: '' });
  ui.window.selectUser('exam-member');
  assert.equal(ui.$('eRole').value, 'EXAM_COMMITTEE');
  assert.equal(ui.$('eAdvisorWrap').classList.contains('hidden'), true);
  assert.equal(ui.$('eDeptWrap').classList.contains('hidden'), true);
  ui.select('eRole', 'ADVISOR');
  assert.equal(ui.$('eAdvisorWrap').classList.contains('hidden'), false);
  assert.equal(ui.$('eDeptWrap').classList.contains('hidden'), true);
  ui.select('eRole', 'SUPER_ADMIN');
  assert.equal(ui.$('eAdvisorWrap').classList.contains('hidden'), true);
  assert.equal(ui.$('eDeptWrap').classList.contains('hidden'), true);
});

test('failed committee role updates preserve the edit selection and show the server error', async t => {
  const ui = await page(t, { failedUpdate: true });
  ui.window.selectUser('advisor');
  ui.select('eRole', 'EXAM_COMMITTEE');
  ui.$('editPanel').querySelector('.um-act-save').click();
  await settle();
  assert.equal(ui.$('eRole').value, 'EXAM_COMMITTEE');
  assert.equal(ui.$('editPanel').classList.contains('d-none'), false);
  assert.match(ui.$('editingLabel').textContent, /advisor/);
  assert.ok(ui.notices.some(notice => notice.name === 'error' && notice.args[0].includes('Permission denied [HTTP 403]')));
  assert.equal(ui.requests.filter(request => request.url === '/ops/users/list/').length, 1);
});

test('quoted and HTML-shaped usernames stay literal in row actions, dialogs and exact API payloads', async t => {
  for (const username of ['"><img src=x onerror="window.__committeeXss=1">', "x');window.__committeeXss=1;//"]) {
    const ui = await page(t, { items: [{ username, role: 'EXAM_COMMITTEE', is_active: true }] });
    const row = () => ui.$('umTable').querySelector('tbody tr');
    assert.equal(row().dataset.username, username);
    assert.equal(row().cells[1].textContent, username);
    assert.equal(row().querySelectorAll('img, [onclick], [onchange], [onkeydown]').length, 0);
    row().querySelector('.um-btn-edit svg').dispatchEvent(new ui.window.MouseEvent('click', { bubbles: true }));
    assert.ok(ui.$('editingLabel').textContent.includes(username));
    assert.equal(ui.$('editingLabel').querySelector('img'), null);
    ui.$('editPanel').querySelector('.um-act-save').click();
    await settle();
    row().querySelector('.um-active-badge').click();
    await settle();
    const status = row().querySelector('.um-active-badge');
    status.dispatchEvent(new ui.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    await settle();
    row().querySelector('.um-btn-key').click();
    await settle();
    row().querySelector('.um-btn-del').click();
    await settle();
    row().querySelector('.um-check').click();
    assert.match(ui.$('bulkCount').textContent, /1/);
    await ui.window.bulkDisable();
    await settle();
    row().querySelector('.um-check').click();
    await ui.window.bulkEnable();
    await settle();
    for (const request of ui.requests.filter(request => request.method === 'POST')) {
      assert.equal(JSON.parse(request.body).username, username, request.url);
    }
    assert.ok(ui.requests.some(request => request.url === '/ops/users/update-role/'));
    assert.ok(ui.requests.some(request => request.url === '/ops/users/set-password/'));
    assert.ok(ui.requests.some(request => request.url === '/ops/users/delete/'));
    assert.equal(ui.requests.filter(request => request.url === '/ops/users/set-active/').length, 4);
    for (const dialog of ui.dialogs) {
      const body = ui.window.document.createElement('div');
      body.innerHTML = dialog.body;
      assert.equal(body.querySelector('img'), null);
      assert.ok(body.textContent.includes(username));
    }
    assert.equal(ui.window.__committeeXss, undefined);
  }
});
