(function () {
  const cfg = window.virtualAdvisorConfig || {};
  // Every word this script writes, in the page's language (Arabic when <html lang="ar">).
  const IS_AR = document.documentElement.lang === 'ar';
  const T = {
    thinking: IS_AR ? 'جارٍ التفكير…' : 'Thinking...',
    ask: IS_AR ? 'إرسال' : 'Ask',
    checkingTools: IS_AR ? 'جارٍ فحص أدوات البيانات المتحقَّق منها…' : 'Checking verified data tools...',
    you: IS_AR ? 'أنت' : 'You',
    generalMode: IS_AR ? 'وضع الإرشاد العام. يُرجى إدخال الرقم الجامعي لاستخدام سياق متحقَّق منه للطالب.' : 'General advisor mode. Add a student ID for verified student context.',
    ctxStudent: IS_AR ? 'الطالب' : 'Student',
    ctxProgram: IS_AR ? 'البرنامج' : 'Program',
    ctxSection: IS_AR ? 'الشعبة' : 'Section',
    ctxGpa: IS_AR ? 'المعدل' : 'GPA',
    ctxEarned: IS_AR ? 'الساعات المعتمدة المكتسبة' : 'Earned credits',
    ctxPassed: IS_AR ? 'المقررات المجتازة' : 'Passed courses',
    ctxRegistered: IS_AR ? 'المقررات المسجّلة هذا الفصل' : 'Registered this term',
    ctxRemaining: IS_AR ? 'المتطلبات المتبقية' : 'Remaining requirements',
    ctxRecommendations: IS_AR ? 'التوصيات' : 'Recommendations',
    noFilters: IS_AR ? 'لا توجد مرشحات' : 'No filters',
    advisorScope: IS_AR ? 'نطاق المرشد: ' : 'Advisor scope ',
    departmentScope: IS_AR ? 'نطاق القسم: ' : 'Department scope ',
    superAdminScope: IS_AR ? 'نطاق المشرف العام' : 'Super admin scope',
    matchedPre: IS_AR ? 'المطابقون: ' : '',
    matchedPost: IS_AR ? '' : ' matched',
    shownPre: IS_AR ? 'المعروضون: ' : '',
    shownPost: IS_AR ? '' : ' shown',
    truncatedPre: IS_AR ? 'النتائج مقتطَعة: ' : '',
    truncatedPost: IS_AR ? '' : ' truncated',
    yes: IS_AR ? 'نعم' : 'Yes',
    no: IS_AR ? 'لا' : 'No',
    thId: IS_AR ? 'الرقم الجامعي' : 'ID',
    thName: IS_AR ? 'الاسم' : 'Name',
    thProgram: IS_AR ? 'البرنامج' : 'Program',
    thGpa: IS_AR ? 'المعدل' : 'GPA',
    thEarned: IS_AR ? 'المكتسبة' : 'Earned',
    thCurrent: IS_AR ? 'الحالية' : 'Current',
    thAdvisor: IS_AR ? 'المرشد' : 'Advisor',
    noMatch: IS_AR ? 'لم يطابق هذا الاستعلام أي طالب في البيانات المتحقَّق منها.' : 'No verified students matched this query.',
    noTool: IS_AR ? 'لم تكن أي أداة بيانات متحقَّق منها لازمة للإجابة الأخيرة.' : 'No verified data tool was needed for the latest answer.',
    findStudents: IS_AR ? 'البحث عن الطلاب' : 'Find Students',
    verifiedResult: IS_AR ? 'نتيجة متحقَّق منها' : 'Verified result',
    toolError: IS_AR ? 'خطأ في الأداة' : 'Tool error',
    serverUnreachable: IS_AR ? 'تعذّر الوصول إلى خادم النموذج المحلي.' : 'Local model server is not reachable.',
    modelsVisiblePre: IS_AR ? 'عدد النماذج الظاهرة: ' : '',
    modelVisible: IS_AR ? '' : ' model visible',
    modelsVisible: IS_AR ? '' : ' models visible',
    autoModel: IS_AR ? 'النموذج المحمَّل تلقائيًا' : 'Auto loaded model',
    contactFailed: IS_AR ? 'تعذّر الاتصال بخادم النموذج المحلي.' : 'Could not contact local model server.',
    previewFailed: IS_AR ? 'تعذّرت معاينة الأدوات المتحقَّق منها، وقد تتضمن الإجابة النهائية الأدلة مع ذلك.' : 'Verified tool preview failed. The final answer may still include evidence.',
    noAnswer: IS_AR ? 'تعذّر على المرشد المحلي الإجابة.' : 'The local advisor could not answer.',
    requestFailed: IS_AR ? 'تعذّر إرسال الطلب إلى المرشد المحلي. يُرجى التحقق من تشغيل LM Studio ثم إعادة المحاولة.' : 'The local advisor request failed. Check LM Studio and try again.',
    cleared: IS_AR ? 'تم مسح المحادثة. يمكن طرح سؤال جديد عند الاستعداد.' : 'Conversation cleared. Ask a fresh question when ready.',
  };
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

  // `detail` is a technical name (an exception class) the server sends with an
  // Arabic sentence: it stays Latin, isolated left-to-right, after the sentence.
  function fillText(el, text, detail) {
    el.textContent = text;
    if (!detail) return;
    const bdi = document.createElement('bdi');
    bdi.dir = 'ltr';
    bdi.textContent = String(detail);
    el.append(' (', bdi, ').');
  }

  function setStatus(ok, text, detail) {
    if (!statusEl) return;
    statusEl.classList.toggle('is-ok', !!ok);
    statusEl.classList.toggle('is-bad', !ok);
    const label = statusEl.querySelector('span:last-child');
    if (label) fillText(label, text, detail);
  }

  function setBusy(isBusy) {
    if (sendBtn) {
      sendBtn.disabled = isBusy;
      sendBtn.textContent = isBusy ? T.thinking : T.ask;
    }
  }

  function setToolPreviewBusy(isBusy) {
    if (!toolResultsEl) return;
    if (isBusy) {
      toolResultsEl.innerHTML = `<div class="va-tool-loading">${T.checkingTools}</div>`;
      if (downloadBtn) downloadBtn.disabled = true;
    }
  }

  function appendMessage(role, content, detail) {
    if (!messagesEl) return;
    const article = document.createElement('article');
    article.className = `va-message va-message-${role}`;

    const avatar = document.createElement('div');
    avatar.className = 'va-avatar';
    avatar.textContent = role === 'user' ? T.you : 'AI';

    const bubble = document.createElement('div');
    bubble.className = 'va-bubble';
    fillText(bubble, content, detail);

    article.appendChild(avatar);
    article.appendChild(bubble);
    messagesEl.appendChild(article);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function updateContextSummary(summary) {
    if (!contextEl || !summary) return;
    if (summary.mode !== 'student') {
      contextEl.textContent = T.generalMode;
      return;
    }
    contextEl.innerHTML = '';
    [
      [T.ctxStudent, summary.student_id],
      [T.ctxProgram, summary.program || '-'],
      [T.ctxSection, summary.section || '-'],
      [T.ctxGpa, summary.gpa ?? '-'],
      [T.ctxEarned, summary.total_earned_credits ?? '-'],
      [T.ctxPassed, summary.passed_count ?? 0],
      [T.ctxRegistered, summary.current_registration_count ?? summary.studying_count ?? 0],
      [T.ctxRemaining, summary.remaining_requirement_count ?? '-'],
      [T.ctxRecommendations, summary.recommendation_count ?? 0],
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
      .join(' | ') || T.noFilters;
    const scope = result.scope_applied || {};
    const scopeText = scope.role === 'ADVISOR'
      ? `${T.advisorScope}${scope.advisor_id || '-'}`
      : scope.role === 'GENERAL_ACADEMIC_ADVISOR'
        ? `${T.departmentScope}${(scope.departments || []).join(', ') || '-'}`
        : T.superAdminScope;
    return `
      <div class="va-tool-summary">
        <span>${T.matchedPre}<b>${result.count ?? 0}</b>${T.matchedPost}</span>
        <span>${T.shownPre}<b>${result.returned ?? 0}</b>${T.shownPost}</span>
        <span>${T.truncatedPre}<b>${result.truncated ? T.yes : T.no}</b>${T.truncatedPost}</span>
      </div>
      <div class="va-tool-meta">
        <span>${escHtml(filterText)}</span>
        <span>${escHtml(scopeText)}</span>
      </div>
      <div class="va-tool-table-wrap">
        <table class="va-tool-table">
          <thead>
            <tr>
              <th>${T.thId}</th>
              <th>${T.thName}</th>
              <th>${T.thProgram}</th>
              <th>${T.thGpa}</th>
              <th>${T.thEarned}</th>
              <th>${T.thCurrent}</th>
              <th>${T.thAdvisor}</th>
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
            `).join('') : `<tr><td colspan="7">${T.noMatch}</td></tr>`}
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
      toolResultsEl.innerHTML = T.noTool;
      return;
    }
    toolResultsEl.innerHTML = usable.map((result) => `
      <article class="va-tool-card">
        <div class="va-tool-card-head">
          <span>${escHtml(result.tool === 'find_students' ? T.findStudents : result.tool)}</span>
          <strong>${result.ok ? T.verifiedResult : T.toolError}</strong>
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
        setStatus(false, data.error_display || data.error || T.serverUnreachable, data.error_display ? data.error_detail : '');
        return;
      }
      setStatus(true, `${T.modelsVisiblePre}${data.models.length}${data.models.length === 1 ? T.modelVisible : T.modelsVisible}`);
      if (modelEl) {
        const current = modelEl.value;
        modelEl.innerHTML = `<option value="">${T.autoModel}</option>`;
        data.models.forEach((m) => {
          const opt = document.createElement('option');
          opt.value = m.id;
          opt.textContent = m.id;
          modelEl.appendChild(opt);
        });
        if (current) modelEl.value = current;
      }
    } catch (err) {
      setStatus(false, T.contactFailed);
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
            if (toolResultsEl) toolResultsEl.innerHTML = T.previewFailed;
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
        appendMessage('assistant', data.error_display || data.error || T.noAnswer, data.error_display ? data.error_detail : '');
        return;
      }
      appendMessage('assistant', data.answer);
      history.push({ role: 'assistant', content: data.answer });
      updateContextSummary(data.context_summary);
      renderToolResults(data.tool_results);
    } catch (err) {
      appendMessage('assistant', T.requestFailed);
    } finally {
      setBusy(false);
    }
  }

  document.getElementById('vaRefreshModels')?.addEventListener('click', refreshModels);
  document.getElementById('vaClear')?.addEventListener('click', () => {
    history.splice(0, history.length);
    if (messagesEl) {
      messagesEl.innerHTML = '';
      appendMessage('assistant', T.cleared);
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
