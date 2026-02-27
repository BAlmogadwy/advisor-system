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
