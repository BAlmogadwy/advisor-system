(function () {
  const cfg = window.virtualAdvisorConfig || {};
  const statusEl = document.getElementById('vaStatus');
  const modelEl = document.getElementById('vaModel');
  const formEl = document.getElementById('vaForm');
  const questionEl = document.getElementById('vaQuestion');
  const messagesEl = document.getElementById('vaMessages');
  const sendBtn = document.getElementById('vaSend');
  const contextEl = document.getElementById('vaContextSummary');
  const toolResultsEl = document.getElementById('vaToolResults');
  const downloadBtn = document.getElementById('vaDownloadResults');
  const history = [];
  let lastToolRows = [];

  function setStatus(ok, text) {
    if (!statusEl) return;
    statusEl.classList.toggle('is-ok', !!ok);
    statusEl.classList.toggle('is-bad', !ok);
    const label = statusEl.querySelector('span:last-child');
    if (label) label.textContent = text;
  }

  function setBusy(isBusy) {
    if (sendBtn) {
      sendBtn.disabled = isBusy;
      sendBtn.textContent = isBusy ? 'Thinking...' : 'Ask';
    }
  }

  function setToolPreviewBusy(isBusy) {
    if (!toolResultsEl) return;
    if (isBusy) {
      toolResultsEl.innerHTML = '<div class="va-tool-loading">Checking verified data tools...</div>';
      if (downloadBtn) downloadBtn.disabled = true;
    }
  }

  function appendMessage(role, content) {
    if (!messagesEl) return;
    const article = document.createElement('article');
    article.className = `va-message va-message-${role}`;

    const avatar = document.createElement('div');
    avatar.className = 'va-avatar';
    avatar.textContent = role === 'user' ? 'You' : 'AI';

    const bubble = document.createElement('div');
    bubble.className = 'va-bubble';
    bubble.textContent = content;

    article.appendChild(avatar);
    article.appendChild(bubble);
    messagesEl.appendChild(article);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function updateContextSummary(summary) {
    if (!contextEl || !summary) return;
    if (summary.mode !== 'student') {
      contextEl.textContent = 'General advisor mode. Add a student ID for verified student context.';
      return;
    }
    contextEl.innerHTML = '';
    [
      ['Student', summary.student_id],
      ['Program', summary.program || '-'],
      ['Section', summary.section || '-'],
      ['GPA', summary.gpa ?? '-'],
      ['Earned credits', summary.total_earned_credits ?? '-'],
      ['Passed courses', summary.passed_count ?? 0],
      ['Studying now', summary.studying_count ?? 0],
      ['Remaining requirements', summary.remaining_requirement_count ?? '-'],
      ['Recommendations', summary.recommendation_count ?? 0],
    ].forEach(([label, value]) => {
      const row = document.createElement('div');
      row.className = 'va-context-row';
      const k = document.createElement('span');
      k.textContent = label;
      const v = document.createElement('strong');
      v.textContent = String(value);
      row.append(k, v);
      contextEl.appendChild(row);
    });
  }

  function escCsv(value) {
    const text = String(value ?? '');
    if (!/[",\n]/.test(text)) return text;
    return `"${text.replace(/"/g, '""')}"`;
  }

  function escHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[ch]));
  }

  function resultRowsForCsv(result) {
    if (!result || result.tool !== 'find_students') return [];
    return (result.students || []).map((student) => ({
      student_id: student.student_id || '',
      name: student.name || '',
      program: student.program || '',
      section: student.section || '',
      status: student.status || '',
      gpa: student.gpa ?? '',
      total_earned_credits: student.total_earned_credits ?? '',
      current_registered_credits: student.current_registered_credits ?? '',
      advisor_id: student.advisor_id || '',
    }));
  }

  function downloadToolCsv() {
    if (!lastToolRows.length) return;
    const headers = Object.keys(lastToolRows[0]);
    const csv = [
      headers.join(','),
      ...lastToolRows.map((row) => headers.map((key) => escCsv(row[key])).join(',')),
    ].join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'virtual-advisor-results.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  function renderFindStudents(result) {
    const filters = result.filters || {};
    const students = result.students || [];
    const filterText = Object.entries(filters)
      .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(', ') : value}`)
      .join(' | ') || 'No filters';
    const scope = result.scope_applied || {};
    const scopeText = scope.role === 'ADVISOR'
      ? `Advisor scope ${scope.advisor_id || '-'}`
      : scope.role === 'GENERAL_ACADEMIC_ADVISOR'
        ? `Department scope ${(scope.departments || []).join(', ') || '-'}`
        : 'Super admin scope';
    return `
      <div class="va-tool-summary">
        <span><b>${result.count ?? 0}</b> matched</span>
        <span><b>${result.returned ?? 0}</b> shown</span>
        <span><b>${result.truncated ? 'Yes' : 'No'}</b> truncated</span>
      </div>
      <div class="va-tool-meta">
        <span>${escHtml(filterText)}</span>
        <span>${escHtml(scopeText)}</span>
      </div>
      <div class="va-tool-table-wrap">
        <table class="va-tool-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Name</th>
              <th>Program</th>
              <th>GPA</th>
              <th>Earned</th>
              <th>Current</th>
              <th>Advisor</th>
            </tr>
          </thead>
          <tbody>
            ${students.length ? students.map((student) => `
              <tr>
                <td>${escHtml(student.student_id || '')}</td>
                <td>${escHtml(student.name || '')}</td>
                <td>${escHtml(`${student.program || ''} ${student.section || ''}`.trim())}</td>
                <td>${escHtml(student.gpa ?? '-')}</td>
                <td>${escHtml(student.total_earned_credits ?? '-')}</td>
                <td>${escHtml(student.current_registered_credits ?? '-')}</td>
                <td>${escHtml(student.advisor_id || '-')}</td>
              </tr>
            `).join('') : '<tr><td colspan="7">No verified students matched this query.</td></tr>'}
          </tbody>
        </table>
      </div>
    `;
  }

  function renderToolResults(results) {
    if (!toolResultsEl) return;
    const usable = Array.isArray(results) ? results.filter((item) => item && item.ok) : [];
    lastToolRows = usable.flatMap(resultRowsForCsv);
    if (downloadBtn) downloadBtn.disabled = !lastToolRows.length;
    if (!usable.length) {
      toolResultsEl.innerHTML = 'No verified data tool was needed for the latest answer.';
      return;
    }
    toolResultsEl.innerHTML = usable.map((result) => `
      <article class="va-tool-card">
        <div class="va-tool-card-head">
          <span>${escHtml(result.tool === 'find_students' ? 'Find Students' : result.tool)}</span>
          <strong>${result.ok ? 'Verified result' : 'Tool error'}</strong>
        </div>
        ${result.tool === 'find_students' ? renderFindStudents(result) : `<pre>${escHtml(JSON.stringify(result, null, 2))}</pre>`}
      </article>
    `).join('');
  }

  async function refreshModels() {
    try {
      const res = await fetch(cfg.healthUrl, { headers: { Accept: 'application/json' } });
      const data = await res.json();
      if (!data.ok) {
        setStatus(false, data.error || 'Local model server is not reachable.');
        return;
      }
      setStatus(true, `${data.models.length} model${data.models.length === 1 ? '' : 's'} visible`);
      if (modelEl) {
        const current = modelEl.value;
        modelEl.innerHTML = '<option value="">Auto loaded model</option>';
        data.models.forEach((m) => {
          const opt = document.createElement('option');
          opt.value = m.id;
          opt.textContent = m.id;
          modelEl.appendChild(opt);
        });
        if (current) modelEl.value = current;
      }
    } catch (err) {
      setStatus(false, 'Could not contact local model server.');
    }
  }

  function fieldValue(id) {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }

  async function submitQuestion(event) {
    event.preventDefault();
    const message = questionEl ? questionEl.value.trim() : '';
    if (!message) return;

    const priorHistory = history.slice(-8);
    appendMessage('user', message);
    history.push({ role: 'user', content: message });
    if (questionEl) questionEl.value = '';
    setBusy(true);
    setToolPreviewBusy(true);

    const payload = {
      message,
      student_id: fieldValue('vaStudentId') || null,
      academic_year: fieldValue('vaAcademicYear') || null,
      term: fieldValue('vaTerm') || null,
      model: modelEl ? modelEl.value : '',
      history: priorHistory,
    };

    try {
      if (cfg.toolPreviewUrl) {
        fetch(cfg.toolPreviewUrl, {
          method: 'POST',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            'X-CSRFToken': cfg.csrfToken,
          },
          body: JSON.stringify({ message }),
        })
          .then((res) => res.json())
          .then((data) => {
            if (data && data.ok) renderToolResults(data.tool_results);
          })
          .catch(() => {
            if (toolResultsEl) toolResultsEl.innerHTML = 'Verified tool preview failed. The final answer may still include evidence.';
          });
      }
      const res = await fetch(cfg.chatUrl, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'X-CSRFToken': cfg.csrfToken,
        },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        appendMessage('assistant', data.error || 'The local advisor could not answer.');
        return;
      }
      appendMessage('assistant', data.answer);
      history.push({ role: 'assistant', content: data.answer });
      updateContextSummary(data.context_summary);
      renderToolResults(data.tool_results);
    } catch (err) {
      appendMessage('assistant', 'The local advisor request failed. Check LM Studio and try again.');
    } finally {
      setBusy(false);
    }
  }

  document.getElementById('vaRefreshModels')?.addEventListener('click', refreshModels);
  document.getElementById('vaClear')?.addEventListener('click', () => {
    history.splice(0, history.length);
    if (messagesEl) {
      messagesEl.innerHTML = '';
      appendMessage('assistant', 'Conversation cleared. Ask a fresh question when ready.');
    }
    renderToolResults([]);
  });
  downloadBtn?.addEventListener('click', downloadToolCsv);
  document.querySelectorAll('[data-va-example]').forEach((button) => {
    button.addEventListener('click', () => {
      if (questionEl) {
        questionEl.value = button.getAttribute('data-va-example') || '';
        questionEl.focus();
      }
    });
  });
  formEl?.addEventListener('submit', submitQuestion);
  refreshModels();
})();
