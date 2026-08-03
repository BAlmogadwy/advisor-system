/* Adviser case actions.

   Deliberately small: every action is one POST to one endpoint, and the server
   decides whether it is allowed. Nothing here mirrors the transition rules —
   duplicating them would give an adviser a button the server refuses, or hide one
   it would have accepted.
*/
(function () {
  const root = document.getElementById('ai-actions');
  if (!root) return;

  const url = root.dataset.actionUrl;
  const errorEl = root.querySelector('.ai-action-error');

  function csrf() {
    const field = root.querySelector('[name=csrfmiddlewaretoken]');
    if (field) return field.value;
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function fail(text) {
    if (!errorEl) return;
    errorEl.textContent = text;
    errorEl.hidden = false;
  }

  async function act(button) {
    const body = { action: button.dataset.aiAction };
    if (button.dataset.aiStatus) body.status = button.dataset.aiStatus;
    if (button.dataset.aiSource) {
      const field = document.getElementById(button.dataset.aiSource);
      body.text = field ? field.value : '';
    }

    button.disabled = true;
    let res;
    try {
      res = await fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
        body: JSON.stringify(body),
      });
    } catch (e) {
      button.disabled = false;
      fail('The action could not be sent. Nothing was changed.');
      return;
    }

    let payload = null;
    try { payload = await res.json(); } catch (e) { payload = null; }

    if (!res.ok || !payload) {
      button.disabled = false;
      /* The server's reason, not a generic one: "record a reply before resolving"
         is the difference between a blocked click and an unexplained one. */
      fail((payload && payload.error) || 'The action was refused.');
      return;
    }

    /* Reload rather than patching the page: status, history and which actions are
       now legal all move together, and the server already decided all three. */
    window.location.reload();
  }

  root.querySelectorAll('[data-ai-action]').forEach(function (button) {
    button.addEventListener('click', function () {
      if (errorEl) errorEl.hidden = true;
      act(button);
    });
  });
})();
