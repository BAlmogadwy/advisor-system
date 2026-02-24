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
