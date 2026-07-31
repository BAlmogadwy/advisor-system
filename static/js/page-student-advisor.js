/* Student academic advisor chat.
   Deliberately minimal: no student-ID box, no model picker and no tool-results
   panel (those are staff surfaces). Identity is enforced server-side from the
   session, so this page never sends a student id. */
(function () {
  const cfg = window.__STUDENT_ADVISOR__ || {};
  const formEl = document.getElementById('saForm');
  const questionEl = document.getElementById('saQuestion');
  const messagesEl = document.getElementById('saMessages');
  const sendBtn = document.getElementById('saSend');
  if (!formEl || !questionEl || !messagesEl || !sendBtn) return;

  const history = [];
  const AR = (document.documentElement.lang || '').startsWith('ar');
  const T = {
    thinking: AR ? 'جارٍ التفكير…' : 'Thinking…',
    failed: AR ? 'تعذّر الحصول على إجابة. حاول مرة أخرى.' : 'Could not get an answer. Please try again.',
    offline: AR ? 'المرشد الذكي غير متاح حاليًا.' : 'The AI advisor is unavailable right now.',
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function addMessage(role, text) {
    const article = document.createElement('article');
    article.className = 'va-message va-message-' + role;
    const avatar = document.createElement('div');
    avatar.className = 'va-avatar';
    avatar.textContent = role === 'user' ? (AR ? 'أنا' : 'You') : 'AI';
    const bubble = document.createElement('div');
    bubble.className = 'va-bubble';
    bubble.innerHTML = esc(text).replace(/\n/g, '<br>');
    article.appendChild(avatar);
    article.appendChild(bubble);
    messagesEl.appendChild(article);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return bubble;
  }

  function csrf() {
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  async function ask(question) {
    addMessage('user', question);
    const pending = addMessage('assistant', T.thinking);
    sendBtn.disabled = true;
    try {
      const res = await fetch(cfg.chatUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
        body: JSON.stringify({ message: question, history: history.slice(-8) }),
      });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok) {
        pending.textContent = data.error ? data.error : (res.status === 503 ? T.offline : T.failed);
        return;
      }
      const answer = String(data.answer || T.failed);
      pending.innerHTML = esc(answer).replace(/\n/g, '<br>');
      history.push({ role: 'user', content: question });
      history.push({ role: 'assistant', content: answer });
    } catch (e) {
      pending.textContent = T.failed;
    } finally {
      sendBtn.disabled = false;
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }

  formEl.addEventListener('submit', function (ev) {
    ev.preventDefault();
    const q = questionEl.value.trim();
    if (!q) return;
    questionEl.value = '';
    ask(q);
  });

  // Example prompts
  document.querySelectorAll('[data-sa-example]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      questionEl.value = btn.getAttribute('data-sa-example') || btn.textContent.trim();
      questionEl.focus();
    });
  });
})();
