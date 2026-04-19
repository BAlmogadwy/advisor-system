/**
 * Shared utilities — Advisor Portal
 * q(), esc(), CSRF helpers
 */

/* ── DOM shorthand ──────────────────────────────────────────── */
const q = (id) => document.getElementById(id);

/* ── HTML entity escaper ────────────────────────────────────── */
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

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
