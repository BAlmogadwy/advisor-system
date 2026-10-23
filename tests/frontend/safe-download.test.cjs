/* safeDownload (static/js/safe-fetch.js, stage 3 B2): a download that stays on
 * the page. Each answer takes its own branch — a file is saved through a link
 * named by the server, JSON is the server's words, an HTML page that loaded is
 * the login form ("signed out"), any other failure names its status, a network
 * failure says so — and the trigger is busy (aria-busy, aria-disabled, the busy
 * words) until it ends; a second activation meanwhile does nothing. The real
 * file runs in jsdom; only fetch, the toast and the browser's save are stubbed. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/safe-fetch.js'), 'utf8');
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

function page(t, { lang = 'en', answer } = {}) {
  const dom = new JSDOM(`<!doctype html><html lang="${lang}"><body>
    <button id="plain" type="button"><svg aria-hidden="true"></svg>
      Export
    </button>
    <button id="named" type="button" aria-label="Export scenario">Export</button>
  </body></html>`, { runScripts: 'outside-only', url: 'http://localhost/timetable-workspace/' });
  t.after(() => dom.window.close());
  const w = dom.window;
  const calls = [], toasts = [], saved = [], urls = [], revoked = [];
  w.notify = { error: (title, sub) => toasts.push([title, sub]) };
  w.console.error = () => {};
  w.fetch = async (url, options) => { calls.push({ url, options }); return answer(url, calls.length); };
  w.URL.createObjectURL = blob => { urls.push(blob); return `blob:${urls.length}`; };
  w.URL.revokeObjectURL = href => revoked.push(href);
  w.HTMLAnchorElement.prototype.click = function () {
    saved.push({ href: this.href, download: this.download, attached: this.isConnected, hidden: this.hidden });
  };
  w.eval(source);
  const plain = w.document.getElementById('plain');
  const named = w.document.getElementById('named');
  return { w, calls, toasts, saved, urls, revoked, plain, named };
}
const json = value => JSON.parse(JSON.stringify(value));  // out of the page's realm
const file = (name, type = XLSX) => new Response('PK', { status: 200, headers: { 'Content-Type': type, 'Content-Disposition': name } });

test('a file is saved through a link named by the server, and nothing is said', async t => {
  const p = page(t, { answer: () => file('attachment; filename="timetable_A_1448_T2.xlsx"') });
  const result = json(await p.w.safeDownload('/ops/tw/scenarios/1/export.xlsx'));
  assert.deepEqual(result, { ok: true, filename: 'timetable_A_1448_T2.xlsx' });
  assert.deepEqual(json(p.saved), [{ href: 'blob:1', download: 'timetable_A_1448_T2.xlsx', attached: true, hidden: true }]);
  assert.equal(p.urls.length, 1);
  assert.deepEqual(p.toasts, []);
  assert.equal(p.calls[0].options.credentials, 'same-origin');
  assert.equal(p.w.document.querySelectorAll('a').length, 0, 'the link is removed again');
});

test('a filename* name is decoded first; with no header the URL names the file', async t => {
  const encoded = encodeURIComponent('جدول التوزيع.xlsx');
  const p = page(t, { answer: (_url, n) => n === 1
    ? file(`attachment; filename="fallback.xlsx"; filename*=UTF-8''${encoded}`)
    : new Response('PK', { status: 200, headers: { 'Content-Type': 'application/zip' } }) });
  assert.equal((await p.w.safeDownload('/x/export.xlsx')).filename, 'جدول التوزيع.xlsx');
  assert.equal((await p.w.safeDownload('/ops/tw/scenarios/1/export-per-plan?search=AI2')).filename, 'export-per-plan');
});

for (const lang of ['en', 'ar']) {
  test(`the trigger is busy until the download ends, and a second activation does nothing (${lang})`, async t => {
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const p = page(t, { lang, answer: async () => { await gate; return file('attachment; filename="a.xlsx"'); } });
    const label = p.plain.textContent;
    const first = p.w.safeDownload('/export.xlsx', { busyEl: p.plain, kind: 'export' });
    assert.equal(p.plain.getAttribute('aria-busy'), 'true');
    assert.equal(p.plain.getAttribute('aria-disabled'), 'true');
    assert.equal(p.plain.textContent.trim(), lang === 'ar' ? 'جارٍ التصدير…' : 'Exporting…');
    assert.ok(p.plain.querySelector('svg'), 'the icon stays');
    const second = json(await p.w.safeDownload('/export.xlsx', { busyEl: p.plain, kind: 'export' }));
    assert.deepEqual(second, { ok: false, reason: 'busy' });
    release();
    assert.equal((await first).ok, true);
    assert.equal(p.calls.length, 1, 'one request');
    assert.equal(p.plain.textContent, label);
    assert.equal(p.plain.getAttribute('aria-busy'), null);
    assert.equal(p.plain.getAttribute('aria-disabled'), null);
  });
}

test('a trigger with its own accessible name reads the busy words too, and gets its name back', async t => {
  const p = page(t, { answer: () => file('attachment; filename="a.zip"', 'application/zip') });
  const done = p.w.safeDownload('/export-per-plan', { busyEl: p.named, kind: 'export' });
  assert.equal(p.named.getAttribute('aria-label'), 'Exporting…');
  await done;
  assert.equal(p.named.getAttribute('aria-label'), 'Export scenario');
  assert.equal(p.named.textContent, 'Export');
});

test('JSON is an error in the server’s words; nothing is saved', async t => {
  const body = { ok: false, error: { code: 'EXPORT_FAILED', message: 'The export could not be built. Try again; if it fails again, tell the timetable administrator.' } };
  const p = page(t, { answer: () => new Response(JSON.stringify(body), { status: 500, headers: { 'Content-Type': 'application/json' } }) });
  const result = json(await p.w.safeDownload('/export.xlsx', { busyEl: p.plain, kind: 'export' }));
  assert.deepEqual(result, { ok: false, reason: 'error', message: body.error.message });
  assert.deepEqual(p.toasts, [['Export failed', body.error.message]]);
  assert.deepEqual(p.saved, []);
  assert.equal(p.plain.getAttribute('aria-busy'), null, 'not busy after an error either');
});

for (const lang of ['en', 'ar']) {
  test(`a login page that loaded means signed out (${lang})`, async t => {
    const p = page(t, { lang, answer: () => new Response('<!doctype html><title>Sign in</title>', { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } }) });
    const result = await p.w.safeDownload('/export.xlsx', { kind: 'export' });
    assert.equal(result.reason, 'signed_out');
    assert.deepEqual(p.toasts, [lang === 'ar'
      ? ['تعذّر التصدير', 'انتهت جلستك. سجّل الدخول ثم صدّر من جديد.']
      : ['Export failed', 'You are signed out. Sign in again, then export.']]);
    assert.deepEqual(p.saved, []);
  });
}

test('an error page and any other failure name the status; a network failure says so', async t => {
  const answers = [
    () => new Response('<html>Bad gateway</html>', { status: 502, headers: { 'Content-Type': 'text/html' } }),
    () => new Response('nope', { status: 403, headers: { 'Content-Type': 'text/plain' } }),
    () => { throw new TypeError('Failed to fetch'); },
  ];
  const p = page(t, { answer: (_url, n) => answers[n - 1]() });
  assert.equal((await p.w.safeDownload('/a')).reason, 'http');
  assert.equal((await p.w.safeDownload('/b')).reason, 'http');
  assert.equal((await p.w.safeDownload('/c')).reason, 'network');
  assert.deepEqual(p.toasts, [
    ['Download failed', 'The server answered with error 502. Try again.'],
    ['Download failed', 'The server answered with error 403. Try again.'],
    ['Download failed', 'The file could not be downloaded. Check your connection, then try again.'],
  ]);
  assert.deepEqual(p.saved, []);
});

test('the Arabic words for a status and for the network', async t => {
  const answers = [
    () => new Response('x', { status: 500, headers: { 'Content-Type': 'text/plain' } }),
    () => { throw new TypeError('offline'); },
  ];
  const p = page(t, { lang: 'ar', answer: (_url, n) => answers[n - 1]() });
  await p.w.safeDownload('/a');
  await p.w.safeDownload('/b');
  assert.deepEqual(p.toasts, [
    ['تعذّر التنزيل', 'ردّ الخادم بالخطأ 500. حاول مرة أخرى.'],
    ['تعذّر التنزيل', 'تعذّر تنزيل الملف. تحقّق من الاتصال ثم حاول مرة أخرى.'],
  ]);
});
