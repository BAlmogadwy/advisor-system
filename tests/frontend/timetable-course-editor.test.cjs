const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

function setup(t, options = {}) {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.dispatchEvent(new w.Event('close')); };
  w.eval(fs.readFileSync(path.join(__dirname, '../../static/js/timetable-course-editor.js'), 'utf8'));
  const calls = [], saved = [];
  w.TimetableCourseEditor.open({ scenarioId: 42, budget: [{ course_key: 'AI101::PLAN', course_code: 'AI101', course_name: '<Course>' }],
    request: async (url, opts) => { calls.push([url, JSON.parse(opts.body)]); return { ok: true, budget: ['updated'] }; },
    onSaved: async rows => saved.push(rows), ...options });
  return { w, calls, saved, form: w.document.querySelector('form') };
}

test('new course submits capacity, credits and count, then refreshes the list', async t => {
  const { w, calls, saved, form } = setup(t);
  const inputs = form.querySelectorAll('input');
  inputs[0].value = 'ai999'; inputs[1].value = 'A new course'; inputs[5].value = '2';
  assert.equal(form.checkValidity(), true);
  await form.onsubmit({ preventDefault() {} });
  assert.equal(calls[0][0], '/ops/tw/scenarios/42/budget/');
  assert.equal(calls[0][1].course_key, 'AI999');
  assert.equal(calls[0][1].sections, '2');
  assert.equal(saved.length, 1);
  assert.equal(w.document.querySelector('dialog'), null);
});

test('existing course keeps its exact identity and disables new-course requirements', async t => {
  const { form, calls, w } = setup(t, { arabic: true });
  const select = form.querySelector('select');
  select.value = 'AI101::PLAN'; select.onchange();
  assert.equal(form.checkValidity(), true);
  assert.equal(w.document.querySelector('dialog').dir, 'rtl');
  assert.equal(w.document.querySelector('Course'), null);
  await form.onsubmit({ preventDefault() {} });
  assert.equal(calls[0][1].course_key, 'AI101::PLAN');
});

test('failure stays visible inside the dialog and allows retry', async t => {
  const { form, w } = setup(t, { request: async () => null });
  form.querySelector('input').value = 'AI101';
  await form.onsubmit({ preventDefault() {} });
  assert.ok(w.document.querySelector('dialog'));
  assert.match(form.querySelector('[role=alert]').textContent, /Could not add/);
  assert.equal(form.querySelector('[type=submit]').disabled, false);
});

test('published scenarios explain the draft-copy route and prevent saving', t => {
  const { form, w } = setup(t, { published: true });
  assert.equal(form.querySelector('[type=submit]').disabled, true);
  assert.match(w.document.querySelector('dialog').textContent, /Copy this published scenario/);
});
