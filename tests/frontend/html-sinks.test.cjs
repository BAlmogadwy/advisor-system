/* The global safeHtml tag (static/js/shared-utils.js) and the safe-HTML
   ratchet (tests/frontend/template-scan.cjs over static/js/*.js, against
   tests/fixtures/html_sink_allowlist.json). Stage 3, A1. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { JSDOM } = require('jsdom');

const scanner = require('./template-scan.cjs');
const { loadSharedUtils, SHARED_UTILS } = require('./shared-utils-harness.cjs');

const BS = String.fromCharCode(92);
const NL = String.fromCharCode(10);
const rx = (source, flags) => new RegExp(source.split('~').join(BS), flags);
const PH = scanner.PLACEHOLDER;
const PAYLOAD = '<img src=x onerror="window.__xss=1">';
const { safeHtml } = loadSharedUtils();

/* The fingerprints of one synthetic file. */
const sinks = (src, file = 't.js') => scanner.auditSource(file, src);

test('safeHtml escapes text and quoted-attribute values', () => {
  const out = String(safeHtml`<p data-x="${PAYLOAD}" title='${PAYLOAD}'>${PAYLOAD}</p>`);
  const escaped = '&lt;img src=x onerror=&quot;window.__xss=1&quot;&gt;';
  assert.equal(out, `<p data-x="${escaped}" title='${escaped}'>${escaped}</p>`);
  const mixed = `a"b'c&d<e>`;
  assert.equal(String(safeHtml`<b data-v="${mixed}">${mixed}</b>`),
    '<b data-v="a&quot;b&#39;c&amp;d&lt;e&gt;">a&quot;b&#39;c&amp;d&lt;e&gt;</b>');
  // Parsed, the values come back exactly and no element is made from them.
  const dom = new JSDOM(`<div id="host">${safeHtml`<p data-x="${PAYLOAD}" data-m="${mixed}">${PAYLOAD}${mixed}</p>`}</div>`);
  const p = dom.window.document.querySelector('#host p');
  assert.equal(p.dataset.x, PAYLOAD);
  assert.equal(p.dataset.m, mixed);
  assert.equal(p.textContent, PAYLOAD + mixed);
  assert.equal(dom.window.document.querySelectorAll('img').length, 0);
  dom.window.close();
});

test('safeHtml keeps nested results and arrays without double escaping', () => {
  const inner = safeHtml`<i>${'R&D'}</i>`;
  const out = String(safeHtml`<p>${inner}${[inner, [safeHtml`<b>${'R&D'}</b>`]]}</p>`);
  assert.equal(out, '<p><i>R&amp;D</i><i>R&amp;D</i><b>R&amp;D</b></p>');
  assert.equal(out.split('R&amp;D').length - 1, 3);
  assert.ok(!out.includes('&amp;amp;'));
  // An array of plain strings is escaped item by item and joined with nothing.
  assert.equal(String(safeHtml`${['<a>', 'b&']}`), '&lt;a&gt;b&amp;');
});

test('safeHtml drops null, undefined and false and prints 0', () => {
  assert.equal(String(safeHtml`<i>${null}|${undefined}|${false}|${0}|${''}|${[null, 0]}</i>`), '<i>|||0||0</i>');
  assert.equal(String(safeHtml``), '');
});

test('safeHtml.raw is the only way to insert markup', () => {
  assert.equal(String(safeHtml`${'<b>x</b>'}`), '&lt;b&gt;x&lt;/b&gt;');
  assert.equal(String(safeHtml`${safeHtml.raw('<b>x</b>')}`), '<b>x</b>');
  assert.equal(String(safeHtml.raw(null)), '');
  assert.equal(String(safeHtml.raw(undefined)), '');
  assert.equal(String(safeHtml.raw(7)), '7');
  assert.equal(safeHtml.isSafe(safeHtml.raw('<b>')), true);
  assert.equal(safeHtml.isSafe(safeHtml`<b></b>`), true);
  assert.equal(safeHtml.isSafe('<b>'), false);
  // An object that only looks like a result is data, and a result is frozen.
  const forged = { s: '<b>forged</b>', toString() { return this.s; } };
  assert.equal(String(safeHtml`${forged}`), '&lt;b&gt;forged&lt;/b&gt;');
  const real = safeHtml`<i>${'x'}</i>`;
  assert.ok(Object.isFrozen(real));
  assert.throws(() => { 'use strict'; real.s = '<script>'; }, TypeError);
  assert.equal(String(real), '<i>x</i>');
});

test('safeHtml results coerce through innerHTML, += and template interpolation', () => {
  const dom = new JSDOM('<div id="host"></div>', { runScripts: 'outside-only' });
  const w = dom.window;
  w.eval(fs.readFileSync(SHARED_UTILS, 'utf8'));
  const piece = w.safeHtml`<b>${'<i>'}</b>`;
  assert.equal(typeof piece, 'object');
  const host = w.document.getElementById('host');
  host.innerHTML = piece;
  assert.equal(host.innerHTML, '<b>&lt;i&gt;</b>');
  assert.equal(host.querySelectorAll('i').length, 0);
  host.insertAdjacentHTML('beforeend', piece);
  assert.equal(host.querySelectorAll('b').length, 2);
  let body = '<p>';
  body += piece;
  assert.equal(body, '<p><b>&lt;i&gt;</b>');
  assert.equal(`${piece}`, '<b>&lt;i&gt;</b>');
  assert.equal([piece, piece].join(''), '<b>&lt;i&gt;</b><b>&lt;i&gt;</b>');
  w.close();
});

test('safeHtml escapes with shared-utils esc even when a page declares its own', () => {
  // Split declares a top-level esc after base.html loads shared-utils.js.
  const dom = new JSDOM('', { runScripts: 'outside-only' });
  dom.window.eval(fs.readFileSync(SHARED_UTILS, 'utf8'));
  dom.window.eval('function esc(s) { return String(s); }');
  assert.equal(String(dom.window.safeHtml`${'<b>'}`), '&lt;b&gt;');
  dom.window.close();
});

test('scanner ignores backticks in regex literals, comments and strings', () => {
  const src = [
    'const a = /`<b>${x}`/g; // `<i>${y}</i>`',
    '/* `<u>${z}</u>` */ const b = \'`<s>${w}</s>`\' + "`<q>${v}</q>`" + \'' + BS + '\'`\';',
    'if (ok) /`<em>${u}<' + BS + '/em>`/.test(s);',
    'const c = x / 2 / y; const d = `<p>${real}</p>`;',
  ].join(NL);
  const { literals } = scanner.scan(src);
  assert.equal(literals.length, 1);
  assert.equal(literals[0].line, 4);
  assert.deepEqual(literals[0].parts.filter(p => p.type === 'e').map(p => p.text), ['real']);
  assert.deepEqual(sinks(src).fingerprints, { 't.js|real': 1 });
});

test('scanner scans nested templates as their own literals', () => {
  const src = 'const a = `<ul>${items.map(i => `<li data-id="${i.id}">${esc(i.name)}</li>`).join(\'\')}</ul>`;';
  const { literals } = scanner.scan(src);
  assert.equal(literals.length, 2);
  const [outer, inner] = literals;
  assert.equal(inner.parent.lit, outer);
  assert.deepEqual(outer.parts.filter(p => p.type === 'e').map(p => p.text), [`items.map(i => ${PH}).join('')`]);
  assert.deepEqual(inner.parts.filter(p => p.type === 'e').map(p => p.text), ['i.id', 'esc(i.name)']);
  assert.deepEqual(sinks(src).fingerprints, { 't.js|i.id': 1 });
});

test('scanner recognises the safeHtml tag across whitespace', () => {
  const src = [
    'const a = safeHtml`<b>${one}</b>`;',
    'const b = safeHtml   `<b>${two}</b>`;',
    'const c = safeHtml',
    '  `<b>${three}</b>`;',
    'const d = safeHtml /* tag */ `<b>${four}</b>`;',
    'const e = x.safeHtml`<b>${member}</b>`;',
    'const f = notSafeHtml`<b>${other}</b>`;',
  ].join(NL);
  const { literals } = scanner.scan(src);
  assert.deepEqual(literals.map(l => [l.tag, l.member]), [
    ['safeHtml', false], ['safeHtml', false], ['safeHtml', false], ['safeHtml', false],
    ['safeHtml', true], ['notSafeHtml', false],
  ]);
  const result = sinks(src);
  assert.equal(result.tagged, 4);
  assert.deepEqual(result.fingerprints, { 't.js|member': 1, 't.js|other': 1 });
});

test('classifier: literal ternaries and nested-template map-joins are safe; data is raw', () => {
  const safe = expr => scanner.classify(expr).safe;
  for (const expr of [
    "IS_AR ? 'نعم' : 'Yes'", "a ? 'x' : b ? 'y' : 'z'", "a ? b ? 'p' : 'q' : 'r'", `ok ? ${PH} : ''`,
    `items.map(i => ${PH}).join('')`, `rows.map((r, k) => ${PH}).join(', ')`, `rows.map(r => safeHtml${PH}).join('')`,
    "'text'", '42', 'T.title', 'ROOM_WORDS.none', 'esc(c.course_name)', 'Number(x)', 'Math.round(s.ratio*100)',
    'items.filter(i => i.ok).length', 'n.toFixed(1)', `safeHtml${PH}`, PH,
  ]) assert.equal(safe(expr), true, expr);
  for (const expr of [
    'c.course_name', "c.prerequisites_csv || 'no prereq'", "info.type || 'Elective'", 'ph', "a ? name : ''",
    'esc(x) + y', 'name + list.length', "a?.b ?? 'c'", 'items.filter(f).map(i => `…`).join(\'\')', 'safeHtml.raw(x)',
    'String(x)', 'x.toLocaleString()',
    // Only T.x and *_WORDS.x are i18n constants: t, tr and L name data rows.
    't.course_name', 'tr.id', 'L.x', 'WORDS.x', 'TXT.x', 'MSG.x', 'STR.x', 'I18N.x', 'i18n.x', 'T_x.y',
    // Escapers are trusted by name only where static/js defines one.
    'escAttr(x)', 'htmlEscape(x)',
  ]) assert.equal(safe(expr), false, expr);
  assert.deepEqual(scanner.classify(`a ? ${PH} : b ? esc(x) : ${PH}`).flows, [0, 1]);
  assert.deepEqual(scanner.classify(`items.map(i => ${PH}).join('')`).flows, [0]);
  assert.deepEqual(scanner.classify('esc(x)').flows, []);
});

test('classifier: an escaper does not make an event handler, an unquoted value or a bare tag safe', () => {
  const src = [
    'const a = `<td onclick="pick(\'${esc(id)}\')" data-n="${Number(n)}">x</td>`;',
    'const b = `<td class=${esc(cls)} ${on ? \'checked\' : \'\'}>${esc(t)}</td>`;',
    "const c = `<a href='/x/${encodeURIComponent(p)}' title=\"${encodeURIComponent(q)}\">${encodeURIComponent(r)}</a>`;",
    "const d = `<b title='${escapeHtml(t)}'>${escapeHtml(u)}</b>`;",
  ].join(NL);
  assert.deepEqual(sinks(src).fingerprints, { 't.js|esc(id)': 1, 't.js|esc(cls)': 1, 't.js|encodeURIComponent(p)': 1 });
  // page-planner's escapeHtml leaves ' alone: not enough in a single-quoted value.
  assert.deepEqual(sinks(src, 'page-planner.js').fingerprints, {
    'page-planner.js|esc(id)': 1, 'page-planner.js|esc(cls)': 1,
    'page-planner.js|encodeURIComponent(p)': 1, 'page-planner.js|escapeHtml(t)': 1,
  });
});

test('scanner: innerHTML right-hand sides and nested literals printed into HTML are HTML', () => {
  const src = [
    'el.innerHTML = `${name} (${count})`;',
    'el.insertAdjacentHTML(\'beforeend\', `${label}`);',
    'const a = `<p>${c ? `${n} more` : esc(`${skip}`)}</p>`;',
    'const b = `<input ${c ? `title="${tip}"` : \'\'}>`;',
    'const plain = `${notHtml} text`;',
  ].join(NL);
  assert.deepEqual(sinks(src).fingerprints, {
    't.js|name': 1, 't.js|count': 1, 't.js|label': 1, 't.js|n': 1, 't.js|tip': 1,
  });
});

test('scanner reports safeHtml errors: double escape, event handler, unquoted value, script content', () => {
  const src = [
    'const a = safeHtml`<b>${esc(x)}</b><i>${list.map(esc).join(\', \')}</i><s>${\'esc\'}</s><u>${o.esc}</u>`;',
    'const b = safeHtml`<b onclick="go(${id})" onmouseover=${id}>x</b>`;',
    'const c = safeHtml`<td class=${cls} ${on ? \'checked\' : \'\'} data-x="${ok}">${ok}</td>`;',
    'const d = safeHtml`<script>${data}</script><style>${css}</style><p>${fine}</p>`;',
  ].join(NL);
  const { errors, fingerprints } = sinks(src);
  assert.deepEqual(errors.map(e => [e.line, e.kind, e.expr]), [
    [1, 'double escape', 'esc(x)'], [1, 'double escape', "list.map(esc).join(', ')"],
    [2, 'event handler', 'id'], [2, 'event handler', 'id'],
    [3, 'unquoted', 'cls'],
    [4, 'script or style', 'data'], [4, 'script or style', 'css'],
  ]);
  assert.deepEqual(fingerprints, {});
});

test('scanner lists every safeHtml.raw call as an opt-out', () => {
  const src = [
    'const a = safeHtml.raw(trusted);',
    'const b = safeHtml`<i>${safeHtml . raw ( other )}</i>`;',
    'const c = `<p>${safeHtml.raw(x)}</p>`;',
  ].join(NL);
  const result = sinks(src);
  assert.equal(result.optOuts, 3);
  assert.deepEqual(result.fingerprints, {
    't.js|raw|safeHtml.raw(trusted)': 1, 't.js|raw|safeHtml.raw( other )': 1,
    't.js|raw|safeHtml.raw(x)': 1, 't.js|safeHtml.raw(x)': 1,
  });
});

test('compareToAllowlist reports added, raised and stale fingerprints', () => {
  const actual = { 'a.js|x': 2, 'a.js|y': 1, 'b.js|new': 1, 'b.js|z': 1 };
  const allowed = { 'a.js|x': 1, 'a.js|y': 1, 'b.js|z': 3, 'c.js|gone': 2 };
  const diff = scanner.compareToAllowlist(actual, allowed);
  assert.deepEqual(diff.added, [{ file: 'b.js', expr: 'new', actual: 1, allowed: 0 }]);
  assert.deepEqual(diff.raised, [{ file: 'a.js', expr: 'x', actual: 2, allowed: 1 }]);
  assert.deepEqual(diff.stale, [
    { file: 'b.js', expr: 'z', actual: 1, allowed: 3 },
    { file: 'c.js', expr: 'gone', actual: 0, allowed: 2 },
  ]);
  assert.deepEqual(scanner.compareToAllowlist(allowed, allowed), { added: [], raised: [], stale: [] });
  // The file format round-trips, sorted by file, then by expression.
  const json = scanner.toAllowlist({ 'b.js|z': 1, 'a.js|y': 2, 'a.js|x': 1, 'a.js|raw|safeHtml.raw(q)': 1 });
  assert.deepEqual(json, { version: 1, files: { 'a.js': { 'raw|safeHtml.raw(q)': 1, x: 1, y: 2 }, 'b.js': { z: 1 } } });
  assert.deepEqual(Object.keys(json.files['a.js']), ['raw|safeHtml.raw(q)', 'x', 'y']);
  assert.deepEqual(scanner.fromAllowlist(json), { 'a.js|raw|safeHtml.raw(q)': 1, 'a.js|x': 1, 'a.js|y': 2, 'b.js|z': 1 });
  assert.throws(() => scanner.fromAllowlist({ version: 1, files: { 'a.js': { x: 0 } } }), /count 0/);
  assert.throws(() => scanner.fromAllowlist({ files: {} }), /version/);
});

/* The CLI on a synthetic static/js and allowlist in a temporary folder. */
function cli(t, files, allowlist, mode) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'html-sinks-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const js = path.join(dir, 'js');
  fs.mkdirSync(js);
  for (const [name, src] of Object.entries(files)) fs.writeFileSync(path.join(js, name), src);
  const list = path.join(dir, 'allowlist.json');
  if (allowlist) fs.writeFileSync(list, JSON.stringify(allowlist));
  const run = spawnSync(process.execPath, [path.join(__dirname, 'template-scan.cjs'), ...(mode ? [mode] : [])], {
    env: { ...process.env, HTML_SINK_JS_DIR: js, HTML_SINK_ALLOWLIST: list }, encoding: 'utf8',
  });
  return { status: run.status, out: run.stdout + run.stderr, list: fs.existsSync(list) ? JSON.parse(fs.readFileSync(list, 'utf8')) : null };
}

test('CLI --lower lowers stale counts and never adds a sink', t => {
  const files = { 'a.js': 'const a = `<p>${x}</p>`;' };
  const lowered = cli(t, files, { version: 1, files: { 'a.js': { x: 3, gone: 1 } } }, '--lower');
  assert.equal(lowered.status, 0, lowered.out);
  assert.deepEqual(lowered.list, { version: 1, files: { 'a.js': { x: 1 } } });
  const before = { version: 1, files: { 'a.js': { gone: 1 } } };
  const refused = cli(t, files, before, '--lower');
  assert.equal(refused.status, 1);
  assert.match(refused.out, /a[.]js: x [(]now 1, allowed 0[)]/);
  assert.match(refused.out, /safeHtml/);
  assert.deepEqual(refused.list, before);
  const check = cli(t, files, before);
  assert.equal(check.status, 1);
});

test('CLI --bootstrap writes an allowlist only when there is none', t => {
  const files = { 'a.js': 'const a = `<p>${x}</p>`;' };
  const made = cli(t, files, null, '--bootstrap');
  assert.equal(made.status, 0, made.out);
  assert.deepEqual(made.list, { version: 1, files: { 'a.js': { x: 1 } } });
  const kept = { version: 1, files: {} };
  const refused = cli(t, files, kept, '--bootstrap');
  assert.equal(refused.status, 1);
  assert.deepEqual(refused.list, kept);
});

/* ── The repository ────────────────────────────────────────────────────── */

const repository = scanner.audit();
const allowlist = scanner.readAllowlist(scanner.ALLOWLIST_PATH);

test('repository: every raw HTML interpolation is allowlisted and no count rose', () => {
  const diff = scanner.compareToAllowlist(repository.fingerprints, allowlist);
  const growth = [...diff.added, ...diff.raised];
  assert.equal(growth.length, 0, [
    'New raw HTML interpolations in static/js. Build this HTML with safeHtml`…`',
    '(static/js/shared-utils.js), which escapes every value; never add to',
    'tests/fixtures/html_sink_allowlist.json by hand:',
    ...growth.map(entry => '  ' + scanner.describe(entry)),
  ].join(NL));
  // It read the real static/js: the pages this PR converted are among the files.
  for (const file of ['shared-utils.js', 'page-timetable-workspace.js', 'page-timetable-workspace-split.js']) {
    assert.ok(repository.files.includes(file), `${file} not scanned`);
  }
});

test('repository: the allowlist has no stale entries (run --lower)', () => {
  const { stale } = scanner.compareToAllowlist(repository.fingerprints, allowlist);
  assert.equal(stale.length, 0, [
    'These raw interpolations are gone or fewer: run node tests/frontend/template-scan.cjs --lower',
    ...stale.map(entry => '  ' + scanner.describe(entry)),
  ].join(NL));
});

test('repository: no escaper inside safeHtml and no interpolation in on*= attributes', () => {
  assert.deepEqual(repository.errors.map(e => `${e.where} ${e.kind}: ${e.expr}`), []);
});

test('repository: the N-1 and N-2 sinks are not allowlisted', () => {
  const closed = [
    // N-1, the classic electives dialog, and the Optimise result's reserve chips.
    'page-timetable-workspace.js|ph',
    "page-timetable-workspace.js|info.programme_term || '?'",
    "page-timetable-workspace.js|info.type || 'Elective'",
    'page-timetable-workspace.js|info.credit_hours || 3',
    'page-timetable-workspace.js|c.course_code',
    'page-timetable-workspace.js|checked',
    'page-timetable-workspace.js|c.course_name',
    "page-timetable-workspace.js|c.prerequisites_csv || 'no prereq'",
    'page-timetable-workspace.js|s.section',
    // N-2, the split electives list.
    "page-timetable-workspace-split.js|ph.programme_term || '?'",
    "page-timetable-workspace-split.js|ph.type || 'Elective'",
    'page-timetable-workspace-split.js|ph.credit_hours || 3',
  ];
  for (const key of closed) {
    assert.equal(allowlist[key], undefined, `allowlisted: ${key}`);
    assert.equal(repository.fingerprints[key], undefined, `raw in static/js: ${key}`);
  }
});

/* The classifier trusts an escaper by its name, so each trusted name must be a
   real escaper wherever a file calls it. Each definition in static/js is read
   alone (the shortest slice from its start, ending at a } or ;, that compiles)
   and run on & < > " '. */
const DANGEROUS = '&<>"' + "'";
const ENTITIES = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#34;': '"', '&#39;': "'", '&#x27;': "'", '&apos;': "'" };
const ENTITY = new RegExp(Object.keys(ENTITIES).join('|'), 'g');

function escaperDefinitions(file, src, name) {
  const found = [];
  const start = rx('(?:^|[^.~w$])((?:function~s+' + name + '~s*~(|(?:const|let|var)~s+' + name + '~s*=))', 'g');
  for (let m; (m = start.exec(src));) {
    const at = m.index + m[0].length - m[1].length;
    let fn = null;
    for (let k = at; k < Math.min(src.length, at + 4000) && !fn; k++) {
      if (src[k] !== '}' && src[k] !== ';') continue;
      const code = src.slice(at, k + 1);
      try { new vm.Script(code); } catch { continue; }
      fn = vm.runInNewContext(code + NL + ';' + name, {});
    }
    found.push({ file, name, line: src.slice(0, at).split(NL).length, fn });
  }
  return found;
}

test('repository: every trusted escaper is defined in static/js and escapes & < > and both quotes', () => {
  const sources = Object.fromEntries(repository.files.map(f => [f, fs.readFileSync(path.join(scanner.JS_DIR, f), 'utf8')]));
  const definitions = [];
  for (const [file, src] of Object.entries(sources)) {
    for (const name of scanner.ESCAPERS) definitions.push(...escaperDefinitions(file, src, name));
  }
  // No name is trusted that static/js does not define.
  for (const name of scanner.ESCAPERS) {
    assert.ok(definitions.some(d => d.name === name), `${name} is trusted but defined nowhere in static/js: drop it from ESCAPERS`);
  }
  // Every definition escapes all five, but for the WEAK_ESCAPERS, which leave ' alone (and are listed exactly).
  for (const d of definitions) {
    const where = `${d.file}:${d.line} ${d.name}`;
    assert.equal(typeof d.fn, 'function', `${where}: the definition could not be read alone`);
    const out = String(d.fn(DANGEROUS));
    assert.equal(out.replace(ENTITY, e => ENTITIES[e]), DANGEROUS, `${where} changes text: ${out}`);
    const leftRaw = [...DANGEROUS].filter(c => out.replace(ENTITY, '').includes(c));
    const weak = (scanner.WEAK_ESCAPERS[d.file] || []).includes(d.name);
    const message = weak && !leftRaw.length
      ? `${where} escapes ' as well: drop it from WEAK_ESCAPERS`
      : `${where} leaves ${leftRaw.join(' ')} raw: ${out}`;
    assert.deepEqual(leftRaw, weak ? ["'"] : [], message);
  }
  for (const [file, names] of Object.entries(scanner.WEAK_ESCAPERS)) {
    for (const name of names) assert.ok(definitions.some(d => d.file === file && d.name === name), `WEAK_ESCAPERS lists ${file} ${name}, which is not defined there`);
  }
  // A file calls an escaper it defines, or a top-level one of shared-utils.js (a global on every page).
  const globals = scanner.ESCAPERS.filter(name => rx('^function~s+' + name + '~s*~(', 'm').test(sources['shared-utils.js']));
  assert.deepEqual(globals, ['esc']);
  for (const [file, src] of Object.entries(sources)) {
    for (const name of scanner.ESCAPERS) {
      if (!rx('(?:^|[^.~w$])' + name + '~s*~(').test(src)) continue;
      const local = definitions.some(d => d.file === file && d.name === name);
      assert.ok(local || globals.includes(name), `${file} calls ${name}( but neither it nor shared-utils.js defines ${name}`);
    }
  }
  assert.ok(definitions.length >= scanner.ESCAPERS.length);
});
