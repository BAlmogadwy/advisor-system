/* The safe-HTML ratchet (stage 3, A1).

   Every template literal in static/js/*.js that builds HTML is scanned. Each
   interpolation the scanner cannot prove safe is a "raw" fingerprint: the file
   and the expression, never a line number, so moving code causes no churn.
   tests/fixtures/html_sink_allowlist.json holds today's raw fingerprints with
   counts that may only fall. New HTML is written with safeHtml
   (static/js/shared-utils.js), which escapes every value itself.

   A literal is HTML when its own text has a tag (`<b`, `</b`), when it is the
   right-hand side of innerHTML / outerHTML / insertAdjacentHTML /
   document.write, or when it is printed straight into an HTML literal (a
   branch of a safe ternary, the body of a map-join, or the whole value).

   In an untagged HTML literal an interpolation is safe when it is a string or
   number literal, an i18n constant (T.x, *_WORDS.x), a numeric value
   (Number(…), Math.*(…), parseInt(…), parseFloat(…), or one term ending in
   .length or .toFixed(…)), a known HTML escaper call spanning the whole
   expression, a nested literal, a safeHtml literal, a ternary whose output
   branches are all safe, or a `.map(x => `…`).join('…')`. encodeURIComponent
   counts only in text and double-quoted values (it leaves ' alone), and so do
   the two escapers that leave ' alone (WEAK_ESCAPERS). Inside an
   event-handler attribute, an unquoted attribute value or a bare tag, no
   escaper makes a value safe: the browser decodes entities before it runs a
   handler, and esc() leaves spaces and "=" alone. Everything else is raw.

   Inside a safeHtml literal every interpolation is escaped, so it is safe,
   except these errors: an escaper call or reference (double escape); any
   interpolation inside an on*= attribute (event handler); a value that is not
   a literal in a bare tag or an unquoted attribute (unquoted); any
   interpolation inside <script> or <style> (script or style). Every
   safeHtml.raw(…) call anywhere is an opt-out fingerprint, keyed raw|<call>.

   No regex here is typed with a backslash: the tool chain that writes these
   files collapses typed backslash pairs (it did in the prototype), so each one
   is built by re() with "~" standing for the backslash.

   CLI:  node tests/frontend/template-scan.cjs              report; exit 1 on a new sink or an error
         node tests/frontend/template-scan.cjs --lower      lower the allowlist; never adds (exit 1)
         node tests/frontend/template-scan.cjs --bootstrap  write it only when it does not exist
   HTML_SINK_JS_DIR and HTML_SINK_ALLOWLIST point the CLI elsewhere (its tests). */
'use strict';
const fs = require('node:fs');
const path = require('node:path');

const BS = String.fromCharCode(92);
const re = (source, flags) => new RegExp(source.split('~').join(BS), flags);

const TAG = 'safeHtml';
const ELLIPSIS = String.fromCharCode(0x2026);
const PLACEHOLDER = '`' + ELLIPSIS + '`';
const ROOT = path.resolve(__dirname, '..', '..');
const JS_DIR = path.join(ROOT, 'static', 'js');
const ALLOWLIST_PATH = path.join(ROOT, 'tests', 'fixtures', 'html_sink_allowlist.json');

/* Known HTML escapers (each escapes & < > " and, but for page-planner's and
   shared-timetable's, '). URL and CSS encoders are not HTML escapers. */
const ESCAPERS = ['esc', 'escape', 'escapeHtml', 'escHtml', 'escapeAttr', 'pgEsc', 'escAttr', 'htmlEscape', '_escapeText'];
const WEAK_ESCAPERS = { 'page-planner.js': ['escapeHtml'], 'shared-timetable.js': ['_escapeText'] };
const ESCAPER_CALL = re('^(?:' + ESCAPERS.join('|') + ')~s*~(');
const ESCAPER_REF = re('(?:^|[^.~w$])(?:' + ESCAPERS.join('|') + ')(?![~w$])');
const NUMERIC_CALL = re('^(?:Number|Math~.[A-Za-z]+|parseInt|parseFloat)~s*~(');
const I18N = re('^(?:T|t|TXT|L|I18N|i18n|tr|MSG|STR|WORDS|[A-Z_]+_WORDS)~.[A-Za-z_$][~w$]*$');
const NUMBER = re('^[0-9]+(?:~.[0-9]+)?$');
const LENGTH = re('~.length$');
const TO_FIXED = re('~.toFixed~([^()]*~)$');
const QUOTES = "'" + '"';
const MAP_JOIN = re('^[~w$.?]+~.map~(~s*~(?[~w$, ]*~)?~s*=>~s*(?:' + TAG + '~s*)?' + PLACEHOLDER + '~s*~)~s*~.join~(~s*([' + QUOTES + '])[^' + QUOTES + ']*~1~s*~)$');
const HTMLISH = re('</?[A-Za-z]');
const SINK_BEFORE = re('(?:(?:innerHTML|outerHTML)~s*~+?=|insertAdjacentHTML~s*~([^,()]*,|document~s*~.~s*write(?:ln)?~s*~()~s*$');
const RAW_CALL_AHEAD = re('^~s*~.~s*raw~s*~(');
const SPACE_RUN = re('~s+', 'g');
const LETTER = /[A-Za-z]/;
const ID_START = /[A-Za-z_$]/;
const ID_PART = /[A-Za-z0-9_$]/;
const DIGIT = /[0-9]/;

const NL = 10;
const SPACE = re('^~s$');
const isSpace = c => c !== undefined && c !== '' && SPACE.test(c);
const REGEX_AFTER_PUNCT = new Set('(,=:[!&|?{};+-*%<>~^}'.split(''));
const CONTROL_WORDS = new Set(['if', 'while', 'for', 'with']);
const REGEX_AFTER_WORD = new Set(['return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete', 'void', 'throw', 'case', 'do', 'else', 'yield', 'await']);

const normalise = text => text.trim().replace(SPACE_RUN, ' ');

/* ── Scanner ──────────────────────────────────────────────────────────────
   Returns { literals, optOuts }. Each literal: { tag, member, line, parts,
   parent, sink }; parts alternate static ('s') and expression ('e'); an
   expression part lists its direct nested literals in `children`, in the
   order of the PLACEHOLDERs in its text. Literals are in creation order, so a
   parent always comes before its children. */
function scan(src) {
  const literals = [];
  const optOuts = [];
  const n = src.length;
  let i = 0;
  let line = 1;

  const advance = () => { if (src.charCodeAt(i) === NL) line++; i++; };

  function readQuoted() {
    const quote = src[i];
    const start = i;
    i++;
    while (i < n && src[i] !== quote) {
      if (src[i] === BS) advance();
      advance();
    }
    i++;
    return src.slice(start, i);
  }

  function readRegex() {
    const start = i;
    let inClass = false;
    i++;
    while (i < n) {
      const c = src[i];
      if (c === BS) { i += 2; continue; }
      if (src.charCodeAt(i) === NL) break;
      if (c === '[') inClass = true;
      else if (c === ']') inClass = false;
      else if (c === '/' && !inClass) { i++; break; }
      i++;
    }
    while (i < n && LETTER.test(src[i])) i++;
    return src.slice(start, i);
  }

  function readTemplate(tag, member, parent, sink) {
    const lit = { tag, member, line, parts: [], parent, sink };
    literals.push(lit);
    i++;
    let cur = '';
    let curLine = line;
    while (i < n && src[i] !== '`') {
      const c = src[i];
      if (c === BS) { cur += c + (src[i + 1] || ''); i++; advance(); continue; }
      if (c === '$' && src[i + 1] === '{') {
        lit.parts.push({ type: 's', text: cur, line: curLine });
        cur = '';
        i += 2;
        const part = { type: 'e', text: '', line, children: [] };
        lit.parts.push(part);
        part.text = readCode('}', { lit, part });
        curLine = line;
        continue;
      }
      cur += c;
      advance();
    }
    lit.parts.push({ type: 's', text: cur, line: curLine });
    i++;
    return lit;
  }

  const lastSignificant = text => {
    for (let k = text.length - 1; k >= 0; k--) if (!isSpace(text[k])) return text[k];
    return '';
  };

  /* Reads code up to `close` (consumed) at bracket depth 0, or to the end.
     Returns the code's text: comments dropped, nested literals as
     PLACEHOLDER. `owner` is the expression part being read, if any. */
  function readCode(close, owner) {
    let text = '';
    let tail = '';
    const capture = close !== null;
    /* text is kept only for an expression; tail (the last code read) serves
       the tag, member and sink checks without copying a whole file. */
    const emit = piece => {
      if (capture) text += piece;
      tail += piece;
      if (tail.length > 400) tail = tail.slice(-200);
    };
    let prev = { kind: 'start' };
    const stack = [];
    while (i < n) {
      const c = src[i];
      if (isSpace(c)) { emit(' '); advance(); continue; }
      if (c === '/' && src[i + 1] === '/') {
        while (i < n && src.charCodeAt(i) !== NL) i++;
        emit(' ');
        continue;
      }
      if (c === '/' && src[i + 1] === '*') {
        i += 2;
        while (i < n && !(src[i] === '*' && src[i + 1] === '/')) advance();
        i += 2;
        emit(' ');
        continue;
      }
      if (c === "'" || c === '"') { emit(readQuoted()); prev = { kind: 'value' }; continue; }
      if (c === '`') {
        const tagged = prev.kind === 'id';
        const sink = !tagged && SINK_BEFORE.test(tail);
        const lit = readTemplate(tagged ? prev.name : '', tagged && prev.member, owner, sink);
        if (owner) owner.part.children.push(lit);
        emit(PLACEHOLDER);
        prev = { kind: 'value' };
        continue;
      }
      if (c === '/' && (prev.kind === 'start' || prev.control
        || (prev.kind === 'punct' && REGEX_AFTER_PUNCT.has(prev.ch))
        || (prev.kind === 'id' && REGEX_AFTER_WORD.has(prev.name)))) {
        emit(readRegex());
        prev = { kind: 'value' };
        continue;
      }
      if (ID_START.test(c)) {
        const start = i;
        while (i < n && ID_PART.test(src[i])) i++;
        const name = src.slice(start, i);
        const member = lastSignificant(tail) === '.';
        emit(name);
        prev = { kind: 'id', name, member };
        if (name === TAG && !member) {
          const ahead = RAW_CALL_AHEAD.exec(src.slice(i, i + 40));
          if (ahead) {
            const callLine = line;
            for (let k = 0; k < ahead[0].length; k++) advance();
            const args = readCode(')', owner);
            const call = TAG + '.raw(' + args + ')';
            emit('.raw(' + args + ')');
            optOuts.push({ expr: normalise(call), line: callLine });
            prev = { kind: 'value' };
          }
        }
        continue;
      }
      if (DIGIT.test(c)) {
        const start = i;
        while (i < n && (ID_PART.test(src[i]) || src[i] === '.')) i++;
        emit(src.slice(start, i));
        prev = { kind: 'value' };
        continue;
      }
      /* A "/" after the ")" of if (…), while (…) or for (…) starts a regex. */
      let control = false;
      if (c === '(' || c === '[' || c === '{') stack.push({ ch: c, control: c === '(' && prev.kind === 'id' && CONTROL_WORDS.has(prev.name) });
      else if (c === ')' || c === ']' || c === '}') {
        if (stack.length) control = stack.pop().control;
        else if (c === close) { i++; return text; }
      }
      emit(c);
      prev = { kind: 'punct', ch: c, control };
      advance();
    }
    return text;
  }

  readCode(null, null);
  return { literals, optOuts };
}

/* ── Expressions ──────────────────────────────────────────────────────── */

/* The expression with every string literal emptied (for name searches). */
function withoutStrings(e) {
  let out = '';
  for (let k = 0; k < e.length; k++) {
    const c = e[k];
    if (c === "'" || c === '"') {
      out += c + c;
      k++;
      while (k < e.length && e[k] !== c) { if (e[k] === BS) k++; k++; }
      continue;
    }
    out += c;
  }
  return out;
}

function isStringLiteral(e) {
  const q = e[0];
  if ((q !== "'" && q !== '"') || e.length < 2 || e[e.length - 1] !== q) return false;
  for (let k = 1; k < e.length - 1; k++) {
    if (e[k] === BS) { k++; continue; }
    if (e[k] === q) return false;
  }
  return true;
}

/* Does the call that starts the expression span all of it? */
function callSpansAll(e) {
  let depth = 0;
  let opened = false;
  for (let k = 0; k < e.length; k++) {
    const c = e[k];
    if (c === "'" || c === '"') {
      k++;
      while (k < e.length && e[k] !== c) { if (e[k] === BS) k++; k++; }
      continue;
    }
    if (c === '(') { depth++; opened = true; }
    else if (c === ')') { depth--; if (opened && depth === 0 && k < e.length - 1) return false; }
  }
  return opened && depth === 0;
}

/* Splits `test ? yes : no` at its top level; null when it is not a ternary.
   Offsets are into `e`, so a branch's PLACEHOLDERs can be counted. */
function parseTernary(e) {
  let depth = 0;
  let question = -1;
  let nested = 0;
  for (let k = 0; k < e.length; k++) {
    const c = e[k];
    if (c === "'" || c === '"') {
      k++;
      while (k < e.length && e[k] !== c) { if (e[k] === BS) k++; k++; }
      continue;
    }
    if (c === '`') { k += PLACEHOLDER.length - 1; continue; }
    if ('([{'.includes(c)) { depth++; continue; }
    if (')]}'.includes(c)) { depth--; continue; }
    if (depth !== 0) continue;
    if (c === '?') {
      if (e[k + 1] === '?') { k++; continue; }
      if (e[k + 1] === '.' && !DIGIT.test(e[k + 2] || '')) continue;
      if (question < 0) question = k;
      else nested++;
      continue;
    }
    if (c === ':' && question >= 0) {
      if (nested > 0) { nested--; continue; }
      return {
        test: { text: e.slice(0, question), at: 0 },
        yes: { text: e.slice(question + 1, k), at: question + 1 },
        no: { text: e.slice(k + 1), at: k + 1 },
      };
    }
  }
  return null;
}

/* No operator, comma or space outside brackets and strings: `a.b(c).length`
   is one term, `name + list.length` is not. */
function isSingleTerm(e) {
  let depth = 0;
  for (let k = 0; k < e.length; k++) {
    const c = e[k];
    if (c === "'" || c === '"') {
      k++;
      while (k < e.length && e[k] !== c) { if (e[k] === BS) k++; k++; }
      continue;
    }
    if ('([{'.includes(c)) depth++;
    else if (')]}'.includes(c)) depth--;
    else if (depth === 0 && (isSpace(c) || '+-*/%|&?:,<>=!~^'.includes(c))) return false;
  }
  return true;
}

const placeholdersBefore = (e, at) => e.slice(0, at).split(PLACEHOLDER).length - 1;

function isLiteralOnly(expr) {
  const e = expr.trim();
  if (e === '' || isStringLiteral(e) || NUMBER.test(e)) return true;
  const t = parseTernary(e);
  return Boolean(t) && isLiteralOnly(t.yes.text) && isLiteralOnly(t.no.text);
}

/* Classifies one interpolation of an untagged HTML literal.
   where: 'text' (element text), 'dq' or 'sq' (a double- or single-quoted
   attribute value), or 'strict' (an event handler, an unquoted value or a bare
   tag, where no escaper is enough). weak: this file's escapers that leave '
   alone, so they do not count in a single-quoted value.
   Returns { safe, flows }: flows lists the PLACEHOLDER indices (in this
   expression) whose literal is printed straight into the HTML. */
const TAGGED_LITERAL = re('^' + TAG + '~s*' + PLACEHOLDER + '$');
const URL_ENCODER_CALL = re('^encodeURIComponent~s*~(');
const MAP_TAGGED = re('^[~w$.?]+~.map~(~s*~(?[~w$, ]*~)?~s*=>~s*' + TAG);
function classify(expr, where = 'text', weak = []) {
  const e = expr.trim();
  const strict = where === 'strict';
  if (e === PLACEHOLDER) return { safe: true, flows: [0] };
  if (e === '' || isStringLiteral(e) || NUMBER.test(e) || I18N.test(e)) return { safe: true, flows: [] };
  if ((NUMERIC_CALL.test(e) && callSpansAll(e)) || ((LENGTH.test(e) || TO_FIXED.test(e)) && isSingleTerm(e))) return { safe: true, flows: [] };
  if (!strict && TAGGED_LITERAL.test(e)) return { safe: true, flows: [] };
  if (!strict && ESCAPER_CALL.test(e) && callSpansAll(e)) {
    const name = e.slice(0, e.indexOf('(')).trim();
    if (!(where === 'sq' && weak.includes(name))) return { safe: true, flows: [] };
  }
  if ((where === 'text' || where === 'dq') && URL_ENCODER_CALL.test(e) && callSpansAll(e)) return { safe: true, flows: [] };
  const ternary = parseTernary(e);
  if (ternary) {
    const flows = [];
    for (const branch of [ternary.yes, ternary.no]) {
      const inner = classify(branch.text, where, weak);
      if (!inner.safe) return { safe: false, flows: [] };
      const base = placeholdersBefore(e, branch.at);
      for (const k of inner.flows) flows.push(base + k);
    }
    return { safe: true, flows };
  }
  if (!strict && MAP_JOIN.test(e)) return { safe: true, flows: MAP_TAGGED.test(e) ? [] : [0] };
  return { safe: false, flows: [] };
}

/* ── HTML context ─────────────────────────────────────────────────────────
   A small tokenizer over a literal's static text, enough to know where each
   interpolation lands: text, a quoted or unquoted attribute value (and its
   name), a bare tag, a comment, or script/style content. */
const TEXT = 'text';
function htmlContexts(lit, initial) {
  const state = { mode: (initial && initial.mode) || TEXT, attr: (initial && initial.attr) || '', tagName: (initial && initial.tagName) || '', quote: (initial && initial.quote) || '', raw: (initial && initial.raw) || '' };
  const at = [];
  const step = (text) => {
    for (let k = 0; k < text.length; k++) {
      const c = text[k];
      switch (state.mode) {
        case TEXT:
          if (c === '<' && text.startsWith('!--', k + 1)) { state.mode = 'comment'; k += 3; }
          else if (c === '<' && (LETTER.test(text[k + 1] || '') || (text[k + 1] === '/' && LETTER.test(text[k + 2] || '')))) {
            state.mode = 'tagname'; state.tagName = '';
            if (text[k + 1] === '/') { k++; state.closing = true; } else state.closing = false;
          }
          break;
        case 'comment':
          if (c === '-' && text.startsWith('->', k + 1)) { state.mode = TEXT; k += 2; }
          break;
        case 'rawtext':
          if (c === '<' && text.slice(k + 1, k + 2 + state.raw.length).toLowerCase() === '/' + state.raw) {
            state.mode = 'tagname'; state.closing = true; state.tagName = ''; k++;
          }
          break;
        case 'tagname':
          if (isSpace(c) || c === '/') state.mode = 'tag';
          else if (c === '>') endTag();
          else state.tagName += c.toLowerCase();
          break;
        case 'tag':
          if (c === '>') endTag();
          else if (!isSpace(c) && c !== '/') { state.mode = 'attrname'; state.attr = c.toLowerCase(); }
          break;
        case 'attrname':
          if (c === '=') state.mode = 'beforevalue';
          else if (isSpace(c)) state.mode = 'afterattr';
          else if (c === '>') endTag();
          else if (c === '/') state.mode = 'tag';
          else state.attr += c.toLowerCase();
          break;
        case 'afterattr':
          if (c === '=') state.mode = 'beforevalue';
          else if (c === '>') endTag();
          else if (!isSpace(c) && c !== '/') { state.mode = 'attrname'; state.attr = c.toLowerCase(); }
          break;
        case 'beforevalue':
          if (c === '"' || c === "'") { state.mode = 'quoted'; state.quote = c; }
          else if (c === '>') endTag();
          else if (!isSpace(c)) state.mode = 'unquoted';
          break;
        case 'quoted':
          if (c === state.quote) state.mode = 'tag';
          break;
        case 'unquoted':
          if (isSpace(c)) state.mode = 'tag';
          else if (c === '>') endTag();
          break;
        default:
          break;
      }
    }
  };
  function endTag() {
    const name = state.tagName;
    state.mode = (!state.closing && (name === 'script' || name === 'style')) ? 'rawtext' : TEXT;
    state.raw = state.mode === 'rawtext' ? name : '';
  }
  for (const part of lit.parts) {
    if (part.type === 's') { step(part.text); continue; }
    at.push({ ...state });
    if (state.mode === 'beforevalue') state.mode = 'unquoted';
  }
  return at;
}

const inHandler = ctx => (ctx.mode === 'quoted' || ctx.mode === 'unquoted' || ctx.mode === 'beforevalue') && re('^on[a-z]+$').test(ctx.attr);
const isBare = ctx => ['tag', 'tagname', 'attrname', 'afterattr', 'unquoted', 'beforevalue'].includes(ctx.mode);

/* ── Audit ────────────────────────────────────────────────────────────── */

/* One file's fingerprints ({ "<file>|<expr>": count }), errors and totals. */
function auditSource(file, src) {
  const { literals, optOuts } = scan(src);
  const fingerprints = {};
  const errors = [];
  let raw = 0;
  let tagged = 0;
  const add = key => { fingerprints[key] = (fingerprints[key] || 0) + 1; };
  const analysed = new Map();
  for (const lit of literals) {
    const isTagged = lit.tag === TAG && !lit.member;
    let initial = null;
    let inherited = false;
    if (lit.parent) {
      const parent = analysed.get(lit.parent.lit);
      const index = lit.parent.lit.parts.filter(p => p.type === 'e').indexOf(lit.parent.part);
      initial = parent.contexts[index];
      const slot = lit.parent.part.children.indexOf(lit);
      inherited = parent.html && !parent.tagged && parent.flows[index].includes(slot);
    }
    const staticText = lit.parts.filter(p => p.type === 's').map(p => p.text).join('');
    const html = isTagged || lit.sink || inherited || HTMLISH.test(staticText);
    const contexts = htmlContexts(lit, initial);
    const flows = [];
    const expressions = lit.parts.filter(p => p.type === 'e');
    if (isTagged) tagged++;
    expressions.forEach((part, index) => {
      const ctx = contexts[index];
      const expr = normalise(part.text);
      if (isTagged) {
        flows.push([]);
        const where = `${file}:${part.line}`;
        if (ESCAPER_REF.test(withoutStrings(expr))) errors.push({ file, line: part.line, kind: 'double escape', expr, where });
        if (inHandler(ctx)) errors.push({ file, line: part.line, kind: 'event handler', expr, where });
        else if (isBare(ctx) && !isLiteralOnly(expr)) errors.push({ file, line: part.line, kind: 'unquoted', expr, where });
        if (ctx.mode === 'rawtext') errors.push({ file, line: part.line, kind: 'script or style', expr, where });
        return;
      }
      if (!html) { flows.push([]); return; }
      const where = inHandler(ctx) || isBare(ctx) ? 'strict'
        : ctx.mode === 'quoted' ? (ctx.quote === "'" ? 'sq' : 'dq') : 'text';
      const verdict = classify(expr, where, WEAK_ESCAPERS[file] || []);
      flows.push(verdict.flows);
      if (!verdict.safe) { raw++; add(`${file}|${expr}`); }
    });
    analysed.set(lit, { html, tagged: isTagged, contexts, flows });
  }
  for (const out of optOuts) add(`${file}|raw|${out.expr}`);
  return { fingerprints, errors, raw, tagged, optOuts: optOuts.length, literals: literals.length };
}

/* Every top-level static/js/*.js (vendor/ is not scanned). */
function audit(dir = JS_DIR) {
  const files = fs.readdirSync(dir).filter(f => f.endsWith('.js') && fs.statSync(path.join(dir, f)).isFile()).sort();
  const fingerprints = {};
  const errors = [];
  let raw = 0;
  let tagged = 0;
  let optOuts = 0;
  for (const file of files) {
    const r = auditSource(file, fs.readFileSync(path.join(dir, file), 'utf8'));
    Object.assign(fingerprints, r.fingerprints);
    errors.push(...r.errors);
    raw += r.raw;
    tagged += r.tagged;
    optOuts += r.optOuts;
  }
  return { files, fingerprints, errors, raw, tagged, optOuts };
}

/* ── Allowlist ────────────────────────────────────────────────────────── */

const splitKey = key => { const bar = key.indexOf('|'); return { file: key.slice(0, bar), expr: key.slice(bar + 1) }; };
const byCodeUnit = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

function toAllowlist(fingerprints) {
  const files = {};
  for (const key of Object.keys(fingerprints).sort(byCodeUnit)) {
    const { file, expr } = splitKey(key);
    (files[file] = files[file] || {})[expr] = fingerprints[key];
  }
  const sorted = {};
  for (const file of Object.keys(files).sort(byCodeUnit)) sorted[file] = files[file];
  return { version: 1, files: sorted };
}

function fromAllowlist(allowlist) {
  if (!allowlist || allowlist.version !== 1 || typeof allowlist.files !== 'object') {
    throw new Error('html_sink_allowlist.json: expected {"version": 1, "files": {...}}');
  }
  const flat = {};
  for (const [file, entries] of Object.entries(allowlist.files)) {
    for (const [expr, count] of Object.entries(entries)) {
      if (!Number.isInteger(count) || count < 1) throw new Error(`html_sink_allowlist.json: ${file}: ${expr} has count ${count}`);
      flat[`${file}|${expr}`] = count;
    }
  }
  return flat;
}

const readAllowlist = (file = ALLOWLIST_PATH) => fromAllowlist(JSON.parse(fs.readFileSync(file, 'utf8')));
const allowlistText = fingerprints => JSON.stringify(toAllowlist(fingerprints), null, 2) + String.fromCharCode(NL);

/* added: a fingerprint not in the allowlist; raised: a count above the
   allowed one; stale: an allowed count above the actual one (lower it). */
function compareToAllowlist(actual, allowed) {
  const added = [];
  const raised = [];
  const stale = [];
  for (const [key, count] of Object.entries(actual)) {
    if (!(key in allowed)) added.push({ ...splitKey(key), actual: count, allowed: 0 });
    else if (count > allowed[key]) raised.push({ ...splitKey(key), actual: count, allowed: allowed[key] });
  }
  for (const [key, count] of Object.entries(allowed)) {
    const now = actual[key] || 0;
    if (count > now) stale.push({ ...splitKey(key), actual: now, allowed: count });
  }
  return { added, raised, stale };
}

const describe = entry => `${entry.file}: ${entry.expr} (now ${entry.actual}, allowed ${entry.allowed})`;

module.exports = {
  ALLOWLIST_PATH, JS_DIR, PLACEHOLDER, ESCAPERS,
  scan, classify, parseTernary, htmlContexts, auditSource, audit,
  toAllowlist, fromAllowlist, readAllowlist, allowlistText, compareToAllowlist, describe,
};

if (require.main === module) {
  const mode = process.argv[2] || '';
  const dir = process.env.HTML_SINK_JS_DIR || JS_DIR;
  const file = process.env.HTML_SINK_ALLOWLIST || ALLOWLIST_PATH;
  const result = audit(dir);
  const fileCount = new Set(Object.keys(result.fingerprints).map(k => splitKey(k).file)).size;
  const totals = `${result.raw} raw interpolations in ${Object.keys(result.fingerprints).length} fingerprints across ${fileCount} files; ${result.tagged} safeHtml literals; ${result.optOuts} safeHtml.raw calls; ${result.errors.length} errors`;
  for (const error of result.errors) console.error(`${error.where} ${error.kind}: ${error.expr}`);
  if (mode === '--bootstrap') {
    if (fs.existsSync(file)) {
      console.error(`${file} exists: only --lower may change it.`);
      process.exit(1);
    }
    fs.writeFileSync(file, allowlistText(result.fingerprints));
    console.log(`Wrote ${file}: ${totals}`);
    process.exit(0);
  }
  const diff = compareToAllowlist(result.fingerprints, readAllowlist(file));
  const growth = [...diff.added, ...diff.raised];
  if (growth.length) {
    console.error('New raw HTML interpolations; write them with safeHtml (the allowlist never grows):');
    for (const entry of growth) console.error('  ' + describe(entry));
  }
  if (mode === '--lower') {
    if (growth.length) process.exit(1);
    fs.writeFileSync(file, allowlistText(result.fingerprints));
    console.log(`Lowered ${diff.stale.length} entries. ${totals}`);
    process.exit(0);
  }
  console.log(totals);
  if (diff.stale.length) console.log(`${diff.stale.length} allowlist entries are stale: run --lower.`);
  process.exit(growth.length || result.errors.length ? 1 : 0);
}
