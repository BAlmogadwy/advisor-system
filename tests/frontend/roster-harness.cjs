/*
 * Shared jsdom harness for the Student lists suites (exam-rosters.test.cjs,
 * exam-roster-drawer.test.cjs), run through tests/test_exam_rosters_frontend.py.
 *
 * The pages are the real views' HTML and every answer the fake server gives
 * by default is the real endpoint's, recorded by the pytest wrapper on the
 * export fixture's saved run. A suite may queue other answers (refusals,
 * failures, held replies) with the documented shapes. Unexpected requests and
 * JavaScript errors fail the test that caused them.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM, VirtualConsole } = require('jsdom');

assert.ok(process.env.EXAM_ROSTERS_FIXTURE, 'Run through pytest tests/test_exam_rosters_frontend.py');
const fixture = JSON.parse(fs.readFileSync(process.env.EXAM_ROSTERS_FIXTURE, 'utf8'));
const AR = fixture.language === 'ar';
const say = (en, ar) => (AR ? ar : en);
const RUN = fixture.run;
const read = name => fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8');
const html = name => fs.readFileSync(fixture.pages[name], 'utf8');

const settle = () => new Promise(resolve => setImmediate(resolve));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const idle = async (rounds = 8) => { for (let i = 0; i < rounds; i++) { await pause(1); await settle(); } };
const clone = value => JSON.parse(JSON.stringify(value));
const sorted = value => Object.keys(value).sort().reduce((out, key) => ({ ...out, [key]: value[key] }), {});
const scopeKey = scope => JSON.stringify(sorted(scope));
const byScope = answers => new Map(Object.entries(answers || {}).map(([key, answer]) => [scopeKey(JSON.parse(key)), answer]));
const ROSTERS = byScope(fixture.rosters);
// The same lists once they changed after the save (a later build).
const CHANGED_ROSTERS = byScope(fixture.changed?.rosters);

const json = (data, status = 200) => ({
  ok: status < 400, status, redirected: false, url: '',
  headers: { get: name => (name.toLowerCase() === 'content-type' ? 'application/json' : null) },
  json: async () => clone(data),
});
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const file = (name = `exam_students_MATH101_r${RUN}_ar_7F3A2C1D.xlsx`, reference = 'EXR-7F3A2C1D') => ({
  ok: true, status: 200, redirected: false, url: '',
  headers: { get: key => ({ 'content-type': XLSX, 'content-disposition': `attachment; filename="${name}"`, 'x-export-reference': reference })[key.toLowerCase()] || null },
  blob: async () => ({ size: 2048, type: XLSX }),
});
function hold() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { answer: () => promise, release };
}

const ENDPOINTS = {
  index: `/ops/exam-timetable/${RUN}/rosters/index/`,
  roster: `/ops/exam-timetable/${RUN}/rosters/`,
  lookup: `/ops/exam-timetable/${RUN}/rosters/lookup/`,
  preflight: `/ops/exam-timetable/${RUN}/students/export/preflight/`,
  export: `/ops/exam-timetable/${RUN}/students/export/`,
};

// The Student lists endpoints and the phase-1 export, answered from the
// recorded fixture unless a test queues something else.
function rosterServer({ changed = false } = {}) {
  const calls = [];
  const queues = { index: [], roster: [], lookup: [], preflight: [], export: [] };
  const byDefault = (kind, call) => {
    if (kind === 'index') return json(changed ? fixture.changed.index : fixture.index);
    if (kind === 'roster') {
      const scope = call.body?.scope;
      const answer = scope && ((changed && CHANGED_ROSTERS.get(scopeKey(scope))) || ROSTERS.get(scopeKey(scope)));
      if (!answer) throw new Error(`No recorded roster for ${JSON.stringify(scope)}`);
      return json(answer);
    }
    if (kind === 'lookup') {
      if ('student_id' in call.body) {
        const answer = fixture.lookups[String(call.body.student_id)];
        if (!answer) throw new Error(`No recorded lookup for ${call.body.student_id}`);
        return json(answer);
      }
      const answer = fixture.searches[call.body.query];
      if (!answer) throw new Error(`No recorded search for ${call.body.query}`);
      return json(answer);
    }
    if (kind === 'preflight') return json(fixture.preflight);
    return file();
  };
  return {
    calls,
    queue(kind, ...answers) { queues[kind].push(...answers); },
    requests: kind => calls.filter(call => call.kind === kind),
    bodies: kind => calls.filter(call => call.kind === kind).map(call => call.body),
    route(url, options = {}) {
      const address = new URL(url, 'http://exam.test');
      const kind = Object.keys(ENDPOINTS).find(name => ENDPOINTS[name] === address.pathname);
      if (!kind) return undefined;
      const call = { kind, url: String(url), options, body: options.body ? JSON.parse(options.body) : null, query: address.search };
      calls.push(call);
      const next = queues[kind].length ? queues[kind].shift() : null;
      if (typeof next === 'function') return next(call);
      if (next) return next;
      return byDefault(kind, call);
    },
  };
}

// Focus as a browser keeps it (jsdom does neither): a hidden or disabled
// element, or one in a closed dialog, cannot take it; a focused element that
// becomes so drops it to the body (the PR #115 browserFocus model).
function browserFocusModel(window) {
  const hiddenNow = element => !element.isConnected
    || Boolean(element.closest('[hidden], .d-none, dialog:not([open]), fieldset[disabled]'))
    || element.disabled === true;
  const focus = window.HTMLElement.prototype.focus;
  window.HTMLElement.prototype.focus = function (...args) {
    if (hiddenNow(this)) return undefined;
    return focus.apply(this, args);
  };
  const active = Object.getOwnPropertyDescriptor(window.Document.prototype, 'activeElement');
  Object.defineProperty(window.document, 'activeElement', {
    configurable: true,
    get() {
      const at = active.get.call(this);
      if (at && at !== this.body && hiddenNow(at)) {
        at.blur();
        return active.get.call(this);
      }
      return at;
    },
  });
}

async function loadPage(t, { page, url, scripts, server, extraRoute = null, browserFocus = true, storage = null, narrow = false, before = null }) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(html(page), { url, runScripts: 'outside-only', virtualConsole });
  const { window } = dom;
  t.after(() => { window.close(); assert.deepEqual(errors.map(error => error.message), []); });
  if (storage) Object.defineProperty(window, 'localStorage', { configurable: true, get: () => storage });
  const toasts = [];
  window.notify = { error: (...args) => toasts.push(['error', ...args]), success: (...args) => toasts.push(['success', ...args]) };
  window.dlg = { confirm: async () => true, prompt: async () => false };
  window.HTMLElement.prototype.scrollIntoView = function () {};
  window.matchMedia = query => ({ matches: narrow && /max-width:\s*799px/.test(query), media: query, addEventListener() {}, removeEventListener() {} });
  if (browserFocus) browserFocusModel(window);
  const history = [];
  for (const name of ['pushState', 'replaceState']) {
    const original = window.history[name].bind(window.history);
    window.history[name] = (...args) => { history.push([name, ...args]); return original(...args); };
  }
  const requests = [];
  server.window = window;
  window.fetch = async (target, options = {}) => {
    requests.push({ url: String(target), ...options });
    const routed = server.route(target, options);
    if (routed !== undefined) return routed;
    const answered = extraRoute ? await extraRoute(String(target), options) : undefined;
    if (answered !== undefined) return answered;
    const error = new Error(`Unexpected HTTP request: ${target}`);
    errors.push(error);
    throw error;
  };
  const downloads = [];
  window.URL.createObjectURL = blob => { downloads.push({ blob }); return `blob:roster-${downloads.length}`; };
  window.URL.revokeObjectURL = () => {};
  window.HTMLAnchorElement.prototype.click = function () {
    if (this.download) downloads.at(-1).saved = { href: this.href, filename: this.download };
  };
  window.__examRosterTiming = { loadingNotice: 0, filterDelay: 0, announceDelay: 0, schedule: callback => setTimeout(callback, 0) };
  window.__examRostersTiming = { findDelay: 0 };
  window.__examStudentExportTiming = { debounce: 0, revoke: 0, busy: [0, 0] };
  window.__examJobPoll = { first: 0, quick: 0, steady: 0, slow: 0, slowAfter: 0, announce: 0, reveal: 0, stall: 0, minShown: 0, backoff: [0, 0, 0, 0], timeout: 2000, checkRetry: 30 };
  Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => false });
  Object.defineProperty(window.document, 'visibilityState', { configurable: true, get: () => 'visible' });
  if (before) before(window);
  const context = dom.getInternalVMContext();
  scripts.forEach(([filename, code]) => vm.runInContext(code, context, { filename }));
  await idle();
  const $ = id => window.document.getElementById(id);
  const text = node => (typeof node === 'string' ? $(node) : node).textContent.replace(/\s+/g, ' ').trim();
  const key = (node, name, init = {}) => node.dispatchEvent(new window.KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true, ...init }));
  const type = (input, value) => { input.value = value; input.dispatchEvent(new window.Event('input', { bubbles: true })); };
  // A plain left click on a link (the harness's anchor.click() only records downloads).
  const follow = (link, init = {}) => link.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...init }));
  // Reach the page script's own top-level state (its `let`s are not window properties).
  const run = code => vm.runInContext(code, context);
  return { window, document: window.document, $, text, key, type, follow, run, server, requests, history, downloads, toasts, errors };
}

// The recorded answer for a scope, before or after the lists changed.
const rosterOf = (scope, { changed = false } = {}) => (changed ? CHANGED_ROSTERS : ROSTERS).get(scopeKey(scope));

module.exports = {
  fixture, AR, say, RUN, read, idle, pause, settle, clone, json, file, hold, rosterServer, loadPage, scopeKey, rosterOf, ENDPOINTS,
};
