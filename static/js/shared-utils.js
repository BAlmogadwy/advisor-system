/**
 * Shared utilities — Advisor Portal
 * q(), esc(), CSRF helpers
 */

/* ── DOM shorthand ──────────────────────────────────────────── */
const q = (id) => document.getElementById(id);

/* ── Arabic plural forms ─────────────────────────────────────
   An Arabic count takes a different form for 1, 2, 3–10, 11–99 and 100+.
   `forms` = { zero, one, two, few, many, other }, "{n}" for the number; a
   missing form falls back to `other`. */
function pluralAr(n, forms) {
  const rule = (typeof Intl !== 'undefined' && Intl.PluralRules) ? new Intl.PluralRules('ar').select(n) : 'other';
  return String(forms[rule] ?? forms.other).replace('{n}', n);
}

/* ── HTML entity escaper ────────────────────────────────────── */
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* ── Safe HTML ──────────────────────────────────────────────
   safeHtml`<b title="${t}">${v}</b>` escapes every value with esc().
   Quote every attribute. Never interpolate inside on*= attributes.
   A nested safeHtml result, or an array of them, passes through as markup.
   null, undefined and false render nothing; 0 renders "0".
   safeHtml.raw(s) is the only way to insert markup, and the ratchet lists
   every use (tests/frontend/template-scan.cjs). The result is an object: a
   sink that tests typeof x === 'string' must be given String(x).
   esc is captured here, so a page that declares its own esc (split does)
   cannot change what safeHtml escapes. */
const safeHtmlEscape = esc;
class SafeHtml {
  constructor(s) { this.s = s; Object.freeze(this); }
  toString() { return this.s; }
  valueOf() { return this.s; }
}
function safeHtmlValue(v) {
  if (v === null || v === undefined || v === false) return '';
  if (v instanceof SafeHtml) return v.s;
  if (Array.isArray(v)) return v.map(safeHtmlValue).join('');
  return safeHtmlEscape(v);
}
function safeHtml(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += safeHtmlValue(values[i]) + strings[i + 1];
  return new SafeHtml(out);
}
safeHtml.raw = s => new SafeHtml(s === null || s === undefined ? '' : String(s));
safeHtml.isSafe = v => v instanceof SafeHtml;

/* ── CSRF helpers ───────────────────────────────────────────── */
function getCookie(name) {
  const v = `; ${document.cookie}`;
  const p = v.split(`; ${name}=`);
  return p.length === 2 ? p.pop().split(';').shift() : '';
}

function getCsrfToken() {
  return getCookie('csrftoken');
}

const csrfToken = getCsrfToken();

function csrfHeaders(extra = {}) {
  return { 'X-CSRFToken': csrfToken, ...extra };
}

/* ── Theme check ────────────────────────────────────────────── */
const _isDark = () => document.documentElement.getAttribute('data-theme') === 'dark';

/* ── Per-course deterministic colouring ──────────────────────
   Hashes a course code to a hue and returns an HSL colour
   tuned for dark / light mode. Used across planner,
   exam-timetable, and advisor-portfolio. */
function colorForCourse(code) {
  let h = 0;
  const s = String(code || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return _isDark() ? `hsl(${h} 45% 22%)` : `hsl(${h} 70% 92%)`;
}
function colorForCourseBorder(code) {
  let h = 0;
  const s = String(code || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return _isDark() ? `hsl(${h} 40% 40%)` : `hsl(${h} 50% 72%)`;
}
