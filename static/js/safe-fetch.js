/**
 * safeFetch — Advisor Portal
 * Global fetch() wrapper with automatic error handling, CSRF injection,
 * and toast feedback.
 *
 * Usage:
 *   const data = await safeFetch('/api/endpoint/', opts, 'Failed to load data');
 *   if (!data) return;           // null means the request failed (user was notified)
 *   // ... use data normally ...
 *
 * For non-JSON responses, use safeFetchRaw() which returns the Response object.
 *
 * CSRF tokens are automatically injected for same-origin mutating requests
 * (POST, PUT, PATCH, DELETE) so callers don't need to add them manually.
 */

/* ── CSRF auto-injection helper ─────────────────────────────── */
const _SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE']);

function _injectCsrf(options) {
  const method = (options.method || 'GET').toUpperCase();
  if (_SAFE_METHODS.has(method)) return options;

  /* Only inject for same-origin requests */
  const headers = new Headers(options.headers || {});
  if (!headers.has('X-CSRFToken')) {
    const token = typeof getCsrfToken === 'function' ? getCsrfToken() : csrfToken;
    if (token) headers.set('X-CSRFToken', token);
  }
  return { ...options, headers };
}

/**
 * Fetch JSON from a URL with automatic error handling.
 *
 * @param {string}         url           - The URL to fetch.
 * @param {RequestInit}    [options={}]  - Standard fetch options (method, headers, body, etc.).
 * @param {string}         [errorMessage='Request failed'] - User-facing prefix shown in the toast on error.
 * @returns {Promise<object|null>}       - Parsed JSON on success, or null on failure.
 */
async function safeFetch(url, options = {}, errorMessage = 'Request failed') {
  try {
    const response = await fetch(url, _injectCsrf(options));
    if (!response.ok) {
      /* Try to extract a server error message from the JSON body */
      let serverMsg = `${response.status} ${response.statusText}`;
      try {
        const errData = await response.json();
        const detail = errData.error || errData.message || errData.detail || '';
        if (detail) serverMsg = String(detail);
      } catch {
        /* Response body was not JSON — use the status text */
      }
      notify.error(errorMessage, serverMsg);
      console.error('[safeFetch]', url, response.status, serverMsg);
      return null;
    }
    return await response.json();
  } catch (err) {
    notify.error(errorMessage, err.message || String(err));
    console.error('[safeFetch]', url, err);
    return null;
  }
}

/* ── safeDownload: a file download that stays on the page ───────────
 * A download opened in a new tab shows a server error as a raw page. This
 * fetches the URL instead and switches on what came back:
 *   - JSON (the error envelope): a toast with the server's words;
 *   - HTML (the login page, after a redirect): "you are signed out";
 *   - any other answer that is not a file: a toast with the HTTP status;
 *   - a file: saved through a temporary <a download>, named by the server's
 *     Content-Disposition.
 * While it runs, `busyEl` (the trigger) reads "Exporting…" (or the kind's
 * busy words), is aria-busy and aria-disabled, and a second activation is
 * ignored. Resolves {ok: true, filename} or {ok: false, reason, message}. */
const _DOWNLOAD_WORDS = {
  download: {
    en: { busy: 'Preparing…', failed: 'Download failed', signedOut: 'You are signed out. Sign in again, then try again.' },
    ar: { busy: 'جارٍ التحضير…', failed: 'تعذّر التنزيل', signedOut: 'انتهت جلستك. سجّل الدخول ثم حاول من جديد.' },
  },
  export: {
    en: { busy: 'Exporting…', failed: 'Export failed', signedOut: 'You are signed out. Sign in again, then export.' },
    ar: { busy: 'جارٍ التصدير…', failed: 'تعذّر التصدير', signedOut: 'انتهت جلستك. سجّل الدخول ثم صدّر من جديد.' },
  },
};
const _DOWNLOAD_STATUS_WORDS = {
  en: { http: status => `The server answered with error ${status}. Try again.`, network: 'The file could not be downloaded. Check your connection, then try again.' },
  ar: { http: status => `ردّ الخادم بالخطأ ${status}. حاول مرة أخرى.`, network: 'تعذّر تنزيل الملف. تحقّق من الاتصال ثم حاول مرة أخرى.' },
};

/* The file name a Content-Disposition header gives (RFC 6266: filename*
 * first, then filename), else the URL's last path segment. */
function _downloadName(disposition, url) {
  const header = String(disposition || '');
  const star = header.match(/filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i);
  if (star) {
    try { return decodeURIComponent(star[2].trim().replace(/^"|"$/g, '')); } catch { /* fall through */ }
  }
  const plain = header.match(/filename\s*=\s*("([^"]*)"|[^;]+)/i);
  if (plain) return (plain[2] !== undefined ? plain[2] : plain[1]).trim();
  const path = String(url).split(/[?#]/)[0].split('/').filter(Boolean).pop();
  return path || 'download';
}

/* The trigger's busy state: its visible label (its last text) and its
 * accessible name read the busy words; restore() puts both back. */
function _markDownloadBusy(el, words) {
  const texts = [...el.childNodes].filter(node => node.nodeType === 3 && node.nodeValue.trim());
  const label = texts[texts.length - 1] || null;
  const before = { text: label ? label.nodeValue : null, aria: el.getAttribute('aria-label') };
  if (label) label.nodeValue = ` ${words.busy} `;
  if (before.aria !== null) el.setAttribute('aria-label', words.busy);
  el.setAttribute('aria-busy', 'true');
  el.setAttribute('aria-disabled', 'true');
  return () => {
    if (label) label.nodeValue = before.text;
    if (before.aria !== null) el.setAttribute('aria-label', before.aria);
    el.removeAttribute('aria-busy');
    el.removeAttribute('aria-disabled');
  };
}

async function safeDownload(url, { busyEl = null, kind = 'download' } = {}) {
  const lang = document.documentElement.lang === 'ar' ? 'ar' : 'en';
  const words = { ...(_DOWNLOAD_WORDS[kind] || _DOWNLOAD_WORDS.download)[lang], ..._DOWNLOAD_STATUS_WORDS[lang] };
  if (busyEl && busyEl.getAttribute('aria-busy') === 'true') return { ok: false, reason: 'busy' };
  const restore = busyEl ? _markDownloadBusy(busyEl, words) : () => {};
  const fail = (reason, message) => {
    notify.error(words.failed, message);
    return { ok: false, reason, message };
  };
  try {
    let response;
    try {
      response = await fetch(url, { credentials: 'same-origin' });
    } catch (err) {
      console.error('[safeDownload]', url, err);
      return fail('network', words.network);
    }
    const type = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
    if (type === 'application/json') {
      let body = null;
      try { body = await response.json(); } catch { /* not JSON after all */ }
      const error = body && body.error;
      const message = (error && (error.message || (typeof error === 'string' ? error : ''))) || (body && body.message) || words.http(response.status);
      return fail('error', String(message));
    }
    if (type === 'text/html') {
      // An export never answers a page of its own: a page that loaded is the
      // login form the session expiry redirected to; one that failed is a
      // proxy's or the server's error page.
      return response.ok ? fail('signed_out', words.signedOut) : fail('http', words.http(response.status));
    }
    if (!response.ok) return fail('http', words.http(response.status));
    const filename = _downloadName(response.headers.get('Content-Disposition'), url);
    const blob = await response.blob();
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = filename;
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Revoked later, not at once: a browser may still be reading the blob.
    setTimeout(() => URL.revokeObjectURL(href), 10000);
    return { ok: true, filename };
  } catch (err) {
    console.error('[safeDownload]', url, err);
    return fail('network', words.network);
  } finally {
    restore();
  }
}

/**
 * Fetch a URL with error handling, returning the raw Response object.
 * Use this when you need to inspect headers or handle non-JSON bodies.
 *
 * @param {string}         url           - The URL to fetch.
 * @param {RequestInit}    [options={}]  - Standard fetch options.
 * @param {string}         [errorMessage='Request failed'] - User-facing prefix shown in the toast on error.
 * @returns {Promise<Response|null>}     - The Response object on success, or null on failure.
 */
async function safeFetchRaw(url, options = {}, errorMessage = 'Request failed') {
  try {
    const response = await fetch(url, _injectCsrf(options));
    if (!response.ok) {
      notify.error(errorMessage, `${response.status} ${response.statusText}`);
      console.error('[safeFetchRaw]', url, response.status, response.statusText);
      return null;
    }
    return response;
  } catch (err) {
    notify.error(errorMessage, err.message || String(err));
    console.error('[safeFetchRaw]', url, err);
    return null;
  }
}
