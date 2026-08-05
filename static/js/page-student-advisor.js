/* Student academic adviser — durable conversations.

   The server is the only source of truth. The previous version kept its own
   history array and rendered from that, so a reload lost the conversation and the
   screen could disagree with what was stored. Everything rendered here comes from
   an API response, and every API response is serialised from the database rows.

   Identity is never sent: the endpoints resolve the student from the session and
   filter on it in the same query.
*/
(function () {
  const cfg = window.__STUDENT_ADVISOR__ || {};
  const AR = !!cfg.isArabic;

  const formEl = document.getElementById('saForm');
  const questionEl = document.getElementById('saQuestion');
  const messagesEl = document.getElementById('saMessages');
  const sendBtn = document.getElementById('saSend');
  const convListEl = document.getElementById('saConvList');
  const convEmptyEl = document.getElementById('saConvEmpty');
  const newChatBtn = document.getElementById('saNewChat');
  const welcomeEl = document.getElementById('saWelcome');
  const statusEl = document.getElementById('saStatus');
  const composerErrorEl = document.getElementById('saComposerError');
  if (!formEl || !questionEl || !messagesEl || !sendBtn || !convListEl) return;

  const T = {
    thinking:  AR ? 'جارٍ تجهيز الإجابة…' : 'Preparing the answer…',
    failed:    AR ? 'تعذر إكمال الإجابة. حاول مرة أخرى.' : 'Could not complete the answer. Please try again.',
    abstained: AR ? 'لا تتوفر معلومات موثوقة كافية للإجابة.' : 'There is not enough verified information to answer.',
    escalated: AR ? 'تم تجهيز الحالة لمراجعة المرشد الأكاديمي.' : 'Prepared for academic adviser review.',
    retry:     AR ? 'إعادة المحاولة' : 'Retry',
    source:    AR ? 'المصدر' : 'Source',
    sources:   AR ? 'المصادر' : 'Sources',
    details:   AR ? 'تفاصيل المصدر' : 'Source details',
    policyId:  AR ? 'معرّف السياسة' : 'Policy ID',
    policyIds: AR ? 'معرّفات السياسات' : 'Policy IDs',
    effective: AR ? 'الفترة الفعالة' : 'Effective period',
    approved:  AR ? 'حالة المصدر: معتمد' : 'Source status: approved',
    helpful:   AR ? 'هل كانت هذه الإجابة مفيدة؟' : 'Was this answer helpful?',
    yes:       AR ? 'نعم' : 'Yes',
    no:        AR ? 'لا' : 'No',
    thanks:    AR ? 'شكرًا لملاحظتك.' : 'Thank you for the feedback.',
    page:      AR ? 'ص' : 'p.',
    untitled:  AR ? 'محادثة بدون عنوان' : 'Untitled conversation',
    loadFail:  AR ? 'تعذر تحميل المحادثة.' : 'Could not load the conversation.',
    me:        AR ? 'أنا' : 'Me',
    sendFail:  AR ? 'تعذر إرسال سؤالك. حاول مرة أخرى.' : 'Could not send your question. Please try again.',
    offline:   AR ? 'لا يوجد اتصال. سؤالك محفوظ، حاول مرة أخرى.' : 'No connection. Your question is kept — try again.',
    why:       AR ? 'ما سبب عدم فائدة الإجابة؟' : 'Why was the answer not helpful?',
    convList:  AR ? 'المحادثات' : 'Conversations',

    askHuman:  AR ? 'مراجعة المرشد الأكاديمي' : 'Ask an academic adviser',
    sendCase:  AR ? 'إرسال الحالة للمرشد' : 'Send this case to an adviser',
    /* "may need" — never "has been approved" or "an adviser is looking at it".
       Nothing has been agreed at the point this is shown. */
    mayNeed:   AR ? 'هذه الحالة قد تحتاج إلى مراجعة المرشد الأكاديمي.'
                  : 'This case may need review by an academic adviser.',
    willSend:  AR ? 'سيتم إرسال:' : 'What will be sent:',
    wontSend:  AR ? 'لن يتم إرسال المحادثات الأخرى أو السجلات الداخلية للنظام.'
                  : 'Your other conversations and the system’s internal records will not be sent.',
    noteAsk:   AR ? 'هل ترغب في إضافة توضيح للمرشد؟' : 'Would you like to add anything for the adviser?',
    optional:  AR ? 'اختياري' : 'optional',
    confirm:   AR ? 'إرسال' : 'Send',
    cancel:    AR ? 'إلغاء' : 'Cancel',
    caseSent:  AR ? 'تم إرسال الحالة للمرشد الأكاديمي' : 'Sent to an academic adviser',
    caseRef:   AR ? 'رقم الحالة' : 'Case number',
    caseState: AR ? 'الحالة' : 'Status',
    caseWhen:  AR ? 'تاريخ الإرسال' : 'Submitted',
    viewCase:  AR ? 'عرض الحالة' : 'View case',
    backToChat: AR ? 'العودة للمحادثة' : 'Back to the conversation',
    caseFail:  AR ? 'تعذر إرسال الحالة. لم يتغير شيء في محادثتك.'
                  : 'Could not send the case. Nothing in your conversation changed.',
    adviserReply: AR ? 'رد المرشد الأكاديمي' : 'The adviser’s reply',
  };

  /* CATEGORIES, not values. The student already has every one of these in front of
     them in this conversation; what they need before pressing the button is to
     know the boundary of what leaves it. Listing the actual content again would
     also mean rendering the stored evidence into the page, which is the one thing
     this screen must not do. */
  const SHARED_ITEMS = AR
    ? [
        'سؤالك',
        'إجابة المرشد الافتراضي',
        'حالة الإجابة وسبب الإحالة',
        'المصادر التي ظهرت مع الإجابة',
        'المعلومات الناقصة المسجلة، إن وجدت',
      ]
    : [
        'your question',
        'the adviser’s answer',
        'the answer’s status and why it is being referred',
        'the sources shown with the answer',
        'any recorded missing information',
      ];

  const REASONS = [
    ['answer_incorrect',            AR ? 'الإجابة غير صحيحة'     : 'The answer is incorrect'],
    ['did_not_understand_question', AR ? 'لم يفهم سؤالي'          : 'It misunderstood my question'],
    ['information_outdated',        AR ? 'المعلومة غير محدثة'     : 'The information is outdated'],
    ['missing_details',             AR ? 'الإجابة ناقصة'          : 'The answer is incomplete'],
    ['citation_not_helpful',        AR ? 'المصدر غير مفيد'        : 'The source is not helpful'],
    ['needed_human_adviser',        AR ? 'أحتاج إلى مرشد أكاديمي' : 'I need a human adviser'],
  ];

  /* Statuses that carry a real answer beneath them. */
  const ANSWERED = ['COMPLETED', 'ABSTAINED', 'ESCALATED'];

  let currentId = null;
  let busy = false;
  /* Keyed BY TURN, not one slot for the page. One slot breaks as soon as two
     questions fail: the second overwrites the key, so retrying the FIRST sends the
     second's key with the first's text. The server correctly refuses that as a
     reused key carrying a different question, Retry appears to do nothing, and the
     next click mints a fresh key and stores the question twice. */
  const retryKeys = new Map();

  function turnKey(id) {
    let key = retryKeys.get(id);
    if (!key) { key = newKey(); retryKeys.set(id, key); }
    return key;
  }

  function csrf() {
    const el = formEl.querySelector('[name=csrfmiddlewaretoken]');
    if (el) return el.value;
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function withId(template, token, value) {
    return template.replace(token, encodeURIComponent(value));
  }

  /* Never throws. `fetch` REJECTS on a dropped connection rather than resolving,
     so an unguarded await propagates out of send() and skips the line that
     re-enables the composer — one Wi-Fi blip and the student is locked out of their
     own screen, with no error, until they think to reload.

     A null body counts as failure however good the status looks: when the session
     expires, `login_required` answers these JSON endpoints with a 302 to an HTML
     login page, fetch follows it, and a successful-looking 200 arrives carrying no
     JSON at all — which used to clear the composer and discard the question. */
  async function api(path, options) {
    try {
      const res = await fetch(path, Object.assign({
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
        credentials: 'same-origin',
      }, options || {}));
      let body = null;
      try { body = await res.json(); } catch (e) { body = null; }
      /* Retry-After is the only thing that makes a 429 actionable. Without it the
         screen tells the student to try again, which is precisely the one thing
         that cannot work yet. */
      const after = Number(res.headers.get('Retry-After') || (body && body.retry_after) || 0);
      return { ok: res.ok && body !== null, status: res.status, body: body, retryAfter: after };
    } catch (e) {
      return { ok: false, status: 0, body: null };
    }
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  /* ── direction ───────────────────────────────────────────────────
     Two separate bidi defects lived on this screen, and both of them changed
     what the student read rather than merely how it looked.

     **The direction of a block.** `dir="auto"` was set per <p> and per <li>, so
     ONE answer could resolve to two directions: an item opening with a course
     code — «AI221 — الشعبة M1» — takes its direction from that first strong
     character and computes LTR, while its Arabic siblings compute RTL. The list
     then splits across both edges of the bubble and the odd item's marker is
     painted on the far side, outside `padding-inline-start`, 5px beyond the
     bubble's own border.

     An answer is written in ONE language. So the direction is decided ONCE, from
     the whole text, and applied explicitly to the body — descendants inherit it
     and can no longer disagree with each other.

     **The order of a range.** «الشعبة M1 من 09:00-10:15» reached the student as
     «10:15-09:00»: a timetable adviser printing every lecture as ending before
     it starts. This is not a font or an alignment problem, it is UAX#9 working
     exactly as specified. An Arabic letter is class AL, so the digits after it
     resolve to AN (Arabic-Number) rather than EN. Rule W4 fuses an ES hyphen
     into a number only BETWEEN TWO EN — so between two AN runs the hyphen stays
     neutral, resolves to the paragraph's RTL direction by N2, and L2 then
     reverses the two number groups around it.

     `dir="ltr"` on the paragraph does NOT fix it (measured — it changes the
     paragraph level, not the resolved class of the digits). Isolation does: a
     <bdi dir="ltr"> gives the run its own LTR paragraph, where the digits are EN
     and W4 binds the hyphen, and presents the whole run to the surrounding
     Arabic as one neutral unit. The same defect silently affected every credit
     range («19-12 ساعة»), page range and ISO date in an answer. */

  /* Hebrew, Arabic, Syriac, Thaana, NKo, and the Arabic presentation forms. */
  const RTL_STRONG = /[֑-߿ࡠ-ࣿיִ-﷿ﹰ-﻿]/;
  /* Latin, Latin Extended, Greek/Coptic, Cyrillic, Armenian — everything below
     U+0590, which is where Hebrew begins. */
  const LTR_STRONG = /[A-Za-zÀ-˿Ͱ-֏]/;

  /* WHICH SCRIPT THE TEXT IS MOSTLY IN — deliberately not `dir="auto"`'s rule.

     Auto takes the FIRST strong character and stops, and this adviser answers
     course questions: «AI221 هو المقرر الذي سألت عنه…» opens with four Latin
     letters and is Arabic. Position is the wrong evidence, and reproducing it in
     JavaScript would have reproduced the defect one layer down — which is what the
     first version of this function did, and what the course-code test caught.

     Counting is decisive where position is arbitrary: a course code is a handful
     of Latin characters inside a paragraph of Arabic, and no realistic answer is
     close to the boundary. A text with no strong character at all (a bare
     «09:00-10:15», a case reference) falls back to the interface language, because
     the labels around it are in the interface language. */
  function textDirection(text) {
    const source = String(text == null ? '' : text);
    let rtl = 0;
    let ltr = 0;
    for (let i = 0; i < source.length; i += 1) {
      const ch = source.charAt(i);
      if (RTL_STRONG.test(ch)) rtl += 1;
      else if (LTR_STRONG.test(ch)) ltr += 1;
    }
    if (rtl > ltr) return 'rtl';
    if (ltr > rtl) return 'ltr';
    return AR ? 'rtl' : 'ltr';
  }

  /* A maximal run of Latin letters, digits and the ASCII characters that BIND
     them into one token: `09:00-10:15`, `19-12`, `2024-09-01`, `AI221`, `3.75`,
     `M1 M2`. It must begin and end on an alphanumeric, so a bullet's leading
     hyphen and a sentence's trailing full stop stay outside the isolate and keep
     behaving as the neutrals they are. Spaces are internal separators on purpose:
     «09:00 - 10:15» is the same range with the same defect, and splitting it into
     two isolates around a bare neutral hyphen would leave the swap in place. */
  const LTR_RUN = /[A-Za-z0-9](?:[A-Za-z0-9 \t:._/-]*[A-Za-z0-9])?/g;

  function appendIsolated(node, text) {
    const source = String(text);
    let cursor = 0;
    let match;
    LTR_RUN.lastIndex = 0;
    while ((match = LTR_RUN.exec(source)) !== null) {
      if (match.index > cursor) {
        node.appendChild(document.createTextNode(source.slice(cursor, match.index)));
      }
      const run = el('bdi', null, match[0]);
      run.setAttribute('dir', 'ltr');
      node.appendChild(run);
      cursor = match.index + match[0].length;
    }
    if (cursor < source.length) {
      node.appendChild(document.createTextNode(source.slice(cursor)));
    }
    return node;
  }

  /* Isolation is applied ONLY in an RTL context. In an LTR paragraph the digits
     already resolve to EN, W4 already binds the hyphen, and the order is already
     right — so wrapping there would be DOM churn that proves nothing and could
     only introduce a difference. */
  function appendDirected(node, text, dir) {
    if (dir === 'rtl') return appendIsolated(node, text);
    node.appendChild(document.createTextNode(String(text)));
    return node;
  }

  /* The one entry point for a text-bearing node whose language is not known in
     advance: decides the direction from the content, states it, and isolates the
     runs that would otherwise be reordered. Every such node on this screen goes
     through it, so none can be forgotten the way the adviser's reply was. */
  function writeText(node, text) {
    const dir = textDirection(text);
    node.setAttribute('dir', dir);
    return appendDirected(node, text, dir);
  }

  /* ── citations ──────────────────────────────────────────────
     Grouped beneath the answer rather than repeated inline, and built from the
     citation snapshot the API returned — never from a separate lookup, so what is
     shown is what was stored. */
  function renderCitations(citations) {
    if (!citations || !citations.length) return null;

    /* Four rules on one page of one document produced four identical-looking
       lines. What the student needs to check is the REFERENCE, so group by it and
       keep every policy id inside that group's details panel — nothing is hidden,
       it is just no longer repeated. */
    const groups = [];
    const byRef = new Map();
    citations.forEach(function (c) {
      const key = [c.document_title, c.edition, c.page].join(' ');
      let g = byRef.get(key);
      if (!g) {
        g = { citation: c, ids: [] };
        byRef.set(key, g);
        groups.push(g);
      }
      if (g.ids.indexOf(c.policy_id) === -1) g.ids.push(c.policy_id);
    });

    const box = el('div', 'sa-citations');
    box.appendChild(el('h4', 'sa-citations-title', groups.length > 1 ? T.sources : T.source));

    const list = el('ol', 'sa-citation-list');
    groups.forEach(function (g) {
      const c = g.citation;
      const item = el('li', 'sa-citation');
      const parts = [c.document_title];
      if (c.edition) parts.push((AR ? 'الإصدار ' : 'edition ') + c.edition);
      if (c.page) parts.push(T.page + ' ' + c.page);
      /* The reference carries an Arabic document title and Latin page numbers, and
         it is shown in whichever interface language the student chose — so its own
         direction is a property of the text, not of the page. «ص 24-23» was one of
         the ranges arriving reversed. */
      item.appendChild(
        writeText(el('span', 'sa-citation-text'), parts.filter(Boolean).join(AR ? '، ' : ', '))
      );

      const details = el('details', 'sa-citation-details');
      details.appendChild(el('summary', null, T.details));
      const dl = el('dl', 'sa-citation-meta');
      dl.appendChild(el('dt', null, g.ids.length > 1 ? T.policyIds : T.policyId));
      g.ids.forEach(function (id) { dl.appendChild(el('dd', 'sa-citation-policy-id', id)); });
      if (c.effective_from || c.effective_to) {
        dl.appendChild(el('dt', null, T.effective));
        const span = el('dd', null, [c.effective_from, c.effective_to].filter(Boolean).join(' — '));
        /* «2024-09-01 — 2026-08-31» contains no strong-LTR character, so inside an
           Arabic paragraph the bidi algorithm swaps the two dates and the student
           reads the validity period ending before it starts. */
        span.setAttribute('dir', 'ltr');
        dl.appendChild(span);
      }
      details.appendChild(dl);
      details.appendChild(el('p', 'sa-citation-approved', T.approved));
      item.appendChild(details);
      list.appendChild(item);
    });
    box.appendChild(list);
    return box;
  }

  /* ── feedback ───────────────────────────────────────────────── */
  function renderFeedback(message) {
    const wrap = el('div', 'sa-feedback');
    const prompt = el('span', 'sa-feedback-q', T.helpful);
    wrap.appendChild(prompt);

    const yes = el('button', 'btn btn-sm sa-fb-btn', T.yes);
    const no = el('button', 'btn btn-sm sa-fb-btn', T.no);
    yes.type = 'button'; no.type = 'button';
    yes.setAttribute('aria-label', T.helpful + ' — ' + T.yes);
    no.setAttribute('aria-label', T.helpful + ' — ' + T.no);
    /* Without an initial value these are announced as plain buttons, and only
       become toggles after being pressed — so a screen-reader user cannot tell
       beforehand that they carry state, or which one is currently set. */
    yes.setAttribute('aria-pressed', 'false');
    no.setAttribute('aria-pressed', 'false');

    const reasonsBox = el('div', 'sa-fb-reasons');
    reasonsBox.setAttribute('role', 'group');
    reasonsBox.setAttribute('aria-label', T.why);
    reasonsBox.hidden = true;

    function mark(rating) {
      yes.setAttribute('aria-pressed', String(rating === 'HELPFUL'));
      no.setAttribute('aria-pressed', String(rating === 'NOT_HELPFUL'));
      yes.classList.toggle('is-selected', rating === 'HELPFUL');
      no.classList.toggle('is-selected', rating === 'NOT_HELPFUL');
    }

    async function send(rating, reasonCodes) {
      const res = await api(withId(cfg.urls.feedback, 'MESSAGE_ID', message.id), {
        method: 'POST',
        body: JSON.stringify({ rating: rating, reason_codes: reasonCodes || [] }),
      });
      if (res.ok && res.body && res.body.feedback) {
        mark(res.body.feedback.rating);
        prompt.textContent = T.thanks;
      } else {
        prompt.textContent = T.sendFail;
        announce(T.sendFail);
      }
    }

    yes.addEventListener('click', function () {
      reasonsBox.hidden = true;
      /* The server drops the reason codes on a HELPFUL rating, so leaving the chips
         selected would show a state the database does not hold — and the next click
         on one of them would post a set missing it. */
      reasonsBox.querySelectorAll('.sa-fb-reason').forEach(function (b) {
        b.classList.remove('is-selected');
        b.setAttribute('aria-pressed', 'false');
      });
      send('HELPFUL', []);
    });
    no.addEventListener('click', function () { reasonsBox.hidden = false; send('NOT_HELPFUL', []); });

    REASONS.forEach(function (pair) {
      const b = el('button', 'btn btn-sm sa-fb-reason', pair[1]);
      b.type = 'button';
      b.dataset.code = pair[0];
      b.setAttribute('aria-pressed', 'false');
      b.addEventListener('click', function () {
        b.classList.toggle('is-selected');
        b.setAttribute('aria-pressed', String(b.classList.contains('is-selected')));
        if (b.dataset.code === 'needed_human_adviser' && b.classList.contains('is-selected')) {
          /* The chip said what they need; offering the same preview here saves
             them hunting for a second button that means the same thing. */
          const host = wrap.parentElement
            ? wrap.parentElement.querySelector('.sa-escalate')
            : null;
          if (host) openPreview(message, host);
        }
        const chosen = Array.prototype.slice
          .call(reasonsBox.querySelectorAll('.is-selected'))
          .map(function (n) { return n.dataset.code; });
        send('NOT_HELPFUL', chosen);
      });
      reasonsBox.appendChild(b);
    });

    wrap.appendChild(yes);
    wrap.appendChild(no);
    wrap.appendChild(reasonsBox);

    if (message.feedback) {
      mark(message.feedback.rating);
      if (message.feedback.rating === 'NOT_HELPFUL') {
        reasonsBox.hidden = false;
        /* Compared as data, not spliced into a selector: a stored code containing a
           quote would throw SyntaxError out of renderMessage and blank the thread
           from that message down. */
        const chosen = new Set(message.feedback.reason_codes || []);
        reasonsBox.querySelectorAll('.sa-fb-reason').forEach(function (b) {
          if (!chosen.has(b.dataset.code)) return;
          b.classList.add('is-selected');
          b.setAttribute('aria-pressed', 'true');
        });
      }
    }
    return wrap;
  }


  /* ── handing the case to a person ─────────────────────────────
     Available on every completed answer, because being satisfied with a reply is
     not a precondition for wanting a person to look at it. Prominent only when
     the answer itself stopped short. */
  function renderEscalate(message) {
    const wrap = el('div', 'sa-escalate');
    const wanted = message.status === 'ABSTAINED';
    if (wanted) {
      wrap.classList.add('is-prominent');
      wrap.appendChild(el('p', 'sa-escalate-lead', T.mayNeed));
    }
    const button = el(
      'button',
      'btn btn-sm sa-escalate-btn' + (wanted ? ' btn-primary' : ''),
      wanted ? T.sendCase : T.askHuman
    );
    button.type = 'button';
    button.addEventListener('click', function () { openPreview(message, wrap); });
    wrap.appendChild(button);
    return wrap;
  }

  /* Shown BEFORE anything is sent. */
  function openPreview(message, host) {
    if (host.querySelector('.sa-preview')) return;

    const panel = el('div', 'sa-preview');
    panel.setAttribute('role', 'group');
    panel.setAttribute('aria-label', T.willSend);
    panel.appendChild(el('h4', 'sa-preview-title', T.willSend));

    const list = el('ul', 'sa-preview-list');
    SHARED_ITEMS.forEach(function (item) { list.appendChild(el('li', null, item)); });
    panel.appendChild(list);
    panel.appendChild(el('p', 'sa-preview-limit', T.wontSend));

    const noteId = 'sa-note-' + message.id;
    const label = el('label', 'sa-preview-note-label', T.noteAsk + ' (' + T.optional + ')');
    label.setAttribute('for', noteId);
    const note = el('textarea', 'sa-preview-note');
    note.id = noteId;
    note.rows = 3;
    note.maxLength = 2000;
    panel.appendChild(label);
    panel.appendChild(note);

    const actions = el('div', 'sa-preview-actions');
    const send = el('button', 'btn btn-sm btn-primary sa-preview-send', T.confirm);
    const cancel = el('button', 'btn btn-sm sa-preview-cancel', T.cancel);
    send.type = 'button';
    cancel.type = 'button';
    cancel.addEventListener('click', function () { panel.remove(); });
    send.addEventListener('click', function () {
      send.disabled = true;
      submitCase(message, note.value, panel, host, send);
    });
    actions.appendChild(send);
    actions.appendChild(cancel);
    panel.appendChild(actions);

    host.appendChild(panel);
    note.focus();
  }

  async function submitCase(message, note, panel, host, send) {
    const res = await api(withId(cfg.urls.escalate, 'MESSAGE_ID', message.id), {
      method: 'POST',
      body: JSON.stringify({ student_note: String(note || '').trim(), student_requested: true }),
    });

    if (res.ok && res.body && res.body.escalation) {
      panel.remove();
      host.replaceWith(renderCaseCard(res.body.escalation));
      announce(T.caseSent);
      return;
    }

    /* Nothing was created, so nothing in the conversation changes — say so, and
       leave the student's note where they typed it. */
    send.disabled = false;
    const message429 = res.status === 429 ? waitMessage(res.retryAfter) : null;
    const problem = message429 || (res.body && res.body.error) || T.caseFail;
    let error = panel.querySelector('.sa-preview-error');
    if (!error) {
      error = el('p', 'sa-preview-error');
      error.setAttribute('role', 'alert');
      panel.appendChild(error);
    }
    error.textContent = problem;
    announce(problem);
  }

  /* Durable: it is rendered from the stored case on every load, not held in page
     memory, so the reference survives a reload — which is when a student who has
     been waiting comes back to look for it. */
  function renderCaseCard(escalation) {
    const card = el('div', 'sa-case');
    card.appendChild(el('h4', 'sa-case-title', T.caseSent));

    const dl = el('dl', 'sa-case-meta');
    dl.appendChild(el('dt', null, T.caseRef));
    /* The reference is the one string a student reads back to a person over the
       phone. It is Latin and digits with separators, so in an Arabic panel it is
       exactly the shape UAX#9 reorders. */
    dl.appendChild(writeText(el('dd', 'sa-case-ref'), escalation.reference));
    dl.appendChild(el('dt', null, T.caseState));
    dl.appendChild(
      writeText(el('dd', 'sa-case-status'), escalation.status_label || escalation.status)
    );
    if (escalation.created_at) {
      dl.appendChild(el('dt', null, T.caseWhen));
      dl.appendChild(writeText(el('dd', 'sa-case-when'), formatDate(escalation.created_at)));
    }
    card.appendChild(dl);

    if (escalation.resolution_message) {
      const reply = el('div', 'sa-case-reply');
      reply.appendChild(el('h5', null, T.adviserReply));
      /* This was the only message on the screen with no direction at all — a
         person's Arabic reply laid out left-to-right in the default English UI,
         which is the same unreadability the model's answers were fixed for. */
      reply.appendChild(writeText(el('p'), escalation.resolution_message));
      card.appendChild(reply);
    }
    return card;
  }

  function formatDate(iso) {
    try {
      return new Date(iso).toLocaleDateString(AR ? 'ar' : 'en', {
        year: 'numeric', month: 'long', day: 'numeric',
      });
    } catch (e) {
      return String(iso).slice(0, 10);
    }
  }

  /* ── messages ───────────────────────────────────────────────── */
  /* The stored content stays verbatim — the citation snapshots are derived from
     those markers, and rewriting what was said would break the audit trail. Only
     the DISPLAY drops them, because «ص 24 [TU.WITHDRAWAL.MAXIMUM]» mid-sentence is
     an identifier aimed at a validator, not at the person reading the answer. The
     same reference is shown properly in the Sources block beneath. */
  /* Mirrors `_POLICY_ID_RE` on the server: at least THREE dot-separated segments,
     as in TU.WITHDRAWAL.MAXIMUM. The first version required only a leading capital,
     which deletes real content on the very screen this is for — «سيُرصد لك تقدير
     [W]» is the WITHDRAWAL GRADE, and it silently became «سيُرصد لك تقدير». It also
     ate [GPA], [CS101] and every other bracketed acronym an answer might use. */
  const POLICY_MARKER = /\s*\[[A-Z][A-Z0-9]*(?:\.[A-Z0-9_]+){2,}\]/g;

  function displayBody(message) {
    const text = String(message.content || '');
    return message.role === 'ASSISTANT' ? text.replace(POLICY_MARKER, '') : text;
  }

  /* ── the answer's own shape ──────────────────────────────────
     Two defects lived in one line — `el('div', 'sa-body', text)`.

     **Direction.** The bubble had none, so it inherited the PAGE's, and the page
     is `dir="ltr"` whenever the interface language is English. An Arabic answer
     inside an LTR paragraph is not merely right-aligned wrongly: the bidi
     algorithm reorders whole segments of every wrapped line and moves trailing
     punctuation to the visual left, so «أما بالنسبة لمقرر DS341، فالشعبة المتاحة
     لك هي:» renders as «:فالشعبة المتاحة لك هي DS341، أما بالنسبة لمقرر». The
     sentence is not misaligned, it is unreadable — and it is unreadable only for
     the students who chose the English interface, which is why it survived.

     Per MESSAGE is the fix, not `rtl` on the container: a student may ask in
     English and be answered in English in the same thread.

     The first version put `dir="auto"` on each TEXT-BEARING node — every <p> and
     every <li> — to avoid a known trap: HTML's auto algorithm skips descendants
     that carry their own `dir`, so a wrapper whose every child is `dir="auto"`
     finds no strong character of its own and quietly resolves to the page
     direction. That avoided the trap and created a worse one — a list whose items
     each chose their OWN direction. `textDirection` reads the whole answer once
     and states the result, so the wrapper is now safe to carry it and the blocks
     inherit a direction they cannot disagree about. See its comment for both
     defects in full.

     **Markdown.** The model writes it — `**bold**`, `* bullets` — and this rendered
     the asterisks literally. Converted here to real elements, built with
     `createElement`/`createTextNode` only: no `innerHTML`, so nothing in an answer
     can become markup. A safe subset, matching what the model actually emits;
     anything else stays as written rather than being silently dropped. */
  const BOLD = /\*\*([^*]+)\*\*/g;
  const BULLET = /^\s*[*•-]\s+(.*)$/;
  const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;

  function inlineInto(node, text, dir) {
    const source = String(text);
    let cursor = 0;
    let match;
    BOLD.lastIndex = 0;
    while ((match = BOLD.exec(source)) !== null) {
      if (match.index > cursor) {
        appendDirected(node, source.slice(cursor, match.index), dir);
      }
      node.appendChild(appendDirected(el('strong'), match[1], dir));
      cursor = match.index + match[0].length;
    }
    if (cursor < source.length) {
      appendDirected(node, source.slice(cursor), dir);
    }
    return node;
  }

  function renderBody(text) {
    const wrap = el('div', 'sa-body');
    /* ONE direction for the whole answer, stated on the body so every block
       inherits it. Per-block `dir="auto"` is what let a list hold two. */
    const dir = textDirection(text);
    wrap.setAttribute('dir', dir);

    let list = null;
    let listTag = '';
    let para = null;

    String(text).split('\n').forEach(function (line) {
      const bullet = BULLET.exec(line);
      const numbered = bullet ? null : NUMBERED.exec(line);
      const item = bullet || numbered;

      if (item) {
        const tag = bullet ? 'ul' : 'ol';
        para = null;
        if (!list || listTag !== tag) {
          list = el(tag, 'sa-list');
          wrap.appendChild(list);
          listTag = tag;
        }
        wrap.lastChild.appendChild(inlineInto(el('li'), item[1], dir));
        return;
      }

      list = null;
      listTag = '';
      if (!line.trim()) {
        para = null;
        return;
      }
      if (!para) {
        para = el('p', 'sa-para');
        wrap.appendChild(para);
      } else {
        // A soft break inside one paragraph: kept, because the model uses single
        // newlines for structure the student can see.
        para.appendChild(document.createTextNode('\n'));
      }
      inlineInto(para, line, dir);
    });

    if (!wrap.childNodes.length) wrap.appendChild(el('p', 'sa-para', ''));
    return wrap;
  }

  function statusNote(status) {
    if (status === 'FAILED') return T.failed;
    if (status === 'ABSTAINED') return T.abstained;
    if (status === 'ESCALATED') return T.escalated;
    if (status === 'PENDING') return T.thinking;
    return null;
  }

  function renderMessage(message) {
    const role = message.role === 'ASSISTANT' ? 'assistant' : 'user';
    const article = el('article', 'va-message va-message-' + role);
    article.dataset.messageId = message.id;
    article.dataset.status = message.status || '';

    article.appendChild(el('div', 'va-avatar', role === 'assistant' ? 'AI' : T.me));
    const bubble = el('div', 'va-bubble');
    bubble.appendChild(renderBody(displayBody(message)));

    const note = statusNote(message.status);
    if (note && message.status !== 'COMPLETED') {
      bubble.appendChild(el('p', 'sa-status sa-status-' + String(message.status).toLowerCase(), note));
    }

    if (role === 'assistant') {
      const cites = renderCitations(message.citations);
      if (cites) bubble.appendChild(cites);
      if (message.escalation) {
        bubble.appendChild(renderCaseCard(message.escalation));
      }
      /* ESCALATED belongs here too: a turn handed to a person is still an answer
         the student can rate, and once the case is finished they may want another. */
      if (ANSWERED.indexOf(message.status) !== -1) {
        bubble.appendChild(renderFeedback(message));
        /* Offered again once a case is finished with: the answer may still be
           wrong, or the student may have a new reason to want a person. */
        if (!message.escalation || !message.escalation.is_open) {
          bubble.appendChild(renderEscalate(message));
        }
      }
    }

    /* A recoverable question carries its own retry, so the student re-sends THIS
       turn instead of retyping it and creating a visual duplicate. Driven by the
       token rather than by the status: a turn abandoned mid-generation is stuck on
       PENDING, and it needs the same way out that a clean failure gets. */
    if (role === 'user' && message.retry_token) {
      const retry = el('button', 'btn btn-sm sa-retry', T.retry);
      retry.type = 'button';
      retry.addEventListener('click', function () { send(message.content, message.retry_token); });
      bubble.appendChild(retry);
    }

    article.appendChild(bubble);
    return article;
  }

  /* A dedicated polite region. `aria-live` used to sit on the thread itself, which
     renderMessages() empties and rebuilds from scratch — so every answer made a
     screen reader recite the ENTIRE conversation from the top, uninterruptibly. */
  function announce(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function renderMessages(messages) {
    messagesEl.innerHTML = '';
    const chat = messagesEl.closest('.va-chat');
    if (chat) chat.classList.toggle('has-messages', !!(messages && messages.length));
    if (!messages || !messages.length) {
      if (welcomeEl) messagesEl.appendChild(welcomeEl);
      return;
    }
    messages.forEach(function (m) { messagesEl.appendChild(renderMessage(m)); });
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  /* ── conversations ──────────────────────────────────────────── */
  function renderConversations(conversations) {
    convListEl.innerHTML = '';
    if (convEmptyEl) {
      convEmptyEl.hidden = conversations.length > 0;
      convListEl.appendChild(convEmptyEl);
    }
    conversations.forEach(function (c) {
      const title = c.title || T.untitled;
      const b = el('button', 'sa-conv' + (c.id === currentId ? ' is-active' : ''));
      /* `text-overflow: ellipsis` truncates at the END of the line — which in an
         LTR button holding an Arabic title is the title's BEGINNING. In the default
         English interface every Arabic conversation was labelled by its last few
         words, so the sidebar was a list of endings. */
      writeText(b, title);
      b.type = 'button';
      /* No role override here. `role="listitem"` REPLACES the implicit button role,
         so the whole sidebar stopped being announced as actionable and vanished
         from the screen reader's list of buttons. The list semantics live on the
         wrapper instead. */
      b.setAttribute('aria-current', c.id === currentId ? 'true' : 'false');
      b.title = title;  // the label is ellipsised; without this it is unrecoverable
      b.addEventListener('click', function () { openConversation(c.id); });
      const li = el('li', 'sa-conv-item');
      li.appendChild(b);
      convListEl.appendChild(li);
    });
  }

  async function loadConversations() {
    const res = await api(cfg.urls.list, { method: 'GET' });
    if (res.ok && res.body) renderConversations(res.body.conversations || []);
  }

  /* Guards against out-of-order responses: two quick clicks in the sidebar can
     resolve backwards, leaving the thread showing one conversation and the
     highlight another. */
  let openToken = 0;

  async function openConversation(id) {
    const token = ++openToken;
    const res = await api(withId(cfg.urls.messages, 'CONVERSATION_ID', id), { method: 'GET' });
    if (token !== openToken) return;
    if (!res.ok) {
      /* currentId is deliberately NOT moved here. Pointing it at a conversation we
         failed to open would file the student's next question into the thread they
         were trying to leave. */
      messagesEl.innerHTML = '';
      const err = el('p', 'sa-error', T.loadFail);
      const again = el('button', 'btn btn-sm sa-retry', T.retry);
      again.type = 'button';
      again.addEventListener('click', function () { openConversation(id); });
      err.appendChild(again);
      messagesEl.appendChild(err);
      announce(T.loadFail);
      return;
    }
    currentId = id;
    try { window.history.replaceState(null, '', '?c=' + encodeURIComponent(id)); } catch (e) { /* ignore */ }
    renderMessages(res.body.messages || []);
    await loadConversations();
  }

  /* Returns the id, or null having already explained why. A bare null made every
     failure here — including a rate limit carrying a precise wait — surface as the
     generic "could not send", with no countdown and a live button. */
  async function ensureConversation() {
    if (currentId) return currentId;
    const res = await api(cfg.urls.create, { method: 'POST', body: JSON.stringify({}) });
    if (res.status === 429) {
      showComposerError(waitMessage(res.retryAfter));
      holdSend(res.retryAfter);
      return null;
    }
    if (!res.ok || !res.body) {
      showComposerError(res.status === 0 ? T.offline : T.sendFail);
      return null;
    }
    currentId = res.body.conversation.id;
    return currentId;
  }

  /* Says how long, in whole minutes when it is long enough that seconds would be
     noise, because "try again later" and a disabled button is a dead end. */
  function waitMessage(seconds) {
    const wait = Math.max(1, Number(seconds) || 1);
    if (wait >= 90) {
      const minutes = Math.ceil(wait / 60);
      return AR
        ? `لقد أرسلت أسئلة كثيرة. يمكنك المحاولة بعد ${minutes} دقيقة تقريبًا.`
        : `That is a lot of questions. You can try again in about ${minutes} minutes.`;
    }
    return AR
      ? `لقد أرسلت أسئلة كثيرة. يمكنك المحاولة بعد ${wait} ثانية.`
      : `That is a lot of questions. You can try again in ${wait} seconds.`;
  }

  /* Keeps Send disabled for the stated wait, so the interface and the server agree
     about what is possible. */
  let holdTimer = null;
  function holdSend(seconds) {
    const wait = Math.max(1, Number(seconds) || 1);
    sendBtn.disabled = true;
    if (holdTimer) clearTimeout(holdTimer);
    holdTimer = setTimeout(function () {
      holdTimer = null;
      if (!busy) sendBtn.disabled = false;
    }, wait * 1000);
  }

  function setBusy(state) {
    busy = state;
    /* A held Send stays held: re-enabling it when a request finishes would offer
       the student a button the server is still refusing. */
    sendBtn.disabled = state || holdTimer !== null;
    questionEl.disabled = state;
    messagesEl.setAttribute('aria-busy', String(state));
    if (state) announce(T.thinking);
  }

  function showComposerError(text) {
    if (!composerErrorEl) return;
    composerErrorEl.textContent = text;
    composerErrorEl.hidden = false;
    announce(text);
  }

  function clearComposerError() {
    if (!composerErrorEl) return;
    composerErrorEl.textContent = '';
    composerErrorEl.hidden = true;
  }

  function newKey() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return String(Date.now()) + '-' + Math.random().toString(16).slice(2);
  }

  async function send(text, retryToken) {
    if (busy) return;
    const question = String(text || '').trim();
    if (!question) return;

    /* Claim the busy flag BEFORE the first await. Creating a conversation is a real
       round trip, and until this moved, Send stayed enabled across it — a
       double-click made two conversations, one orphaned in the sidebar forever. */
    setBusy(true);
    /* A retry carries the failed turn's OWN token, so the server resumes that turn
       instead of storing the question again. A new question gets a fresh key, kept
       until the send is known to have landed so a lost response can be re-sent
       without asking twice. */
    const slot = 'new:' + question;
    const key = retryToken || turnKey(slot);
    try {
      const id = await ensureConversation();
      if (!id) return;  // it has already said why

      if (!retryToken) {
        messagesEl.appendChild(renderMessage({
          id: 'pending', role: 'STUDENT', content: question, status: 'PENDING',
        }));
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }

      const res = await api(withId(cfg.urls.send, 'CONVERSATION_ID', id), {
        method: 'POST',
        body: JSON.stringify({ message: question, idempotency_key: key }),
      });

      if (res.ok) {
        questionEl.value = '';
        retryKeys.delete(slot);
        clearComposerError();
      } else if (res.status === 0) {
        /* Nothing reached the server, so the same key must be reused next time. */
        showComposerError(T.offline);
      } else if (res.status === 429) {
        showComposerError(waitMessage(res.retryAfter));
        holdSend(res.retryAfter);
      } else {
        showComposerError(T.sendFail);
      }

      /* Re-read the whole conversation rather than appending what we think
         happened: the stored rows are authoritative, including a failed turn.
         Skipped if the student moved to another conversation while waiting —
         yanking them back mid-read is worse than a late refresh. */
      if (currentId === id) await openConversation(id);
      else await loadConversations();
    } finally {
      setBusy(false);
      /* Disabling the focused element sends focus to <body>, so without this every
         question ends with the keyboard user at the top of the document. */
      if (document.activeElement === document.body) questionEl.focus();
    }
  }

  formEl.addEventListener('submit', function (event) {
    event.preventDefault();
    send(questionEl.value, null);
  });

  document.querySelectorAll('[data-sa-example]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      questionEl.value = btn.dataset.saExample;
      questionEl.focus();
    });
  });

  if (newChatBtn) {
    newChatBtn.addEventListener('click', async function () {
      currentId = null;
      renderMessages([]);
      try { window.history.replaceState(null, '', window.location.pathname); } catch (e) { /* ignore */ }
      await loadConversations();
      questionEl.focus();
    });
  }

  /* A reload should land back where the student was: the URL if it names a
     conversation, otherwise their most recent one. */
  (async function start() {
    const res = await api(cfg.urls.list, { method: 'GET' });
    if (!res.ok || !res.body) {
      /* Not the same as having none. Collapsing the two told a student whose
         request merely 500'd that their history was gone. */
      if (convEmptyEl) convEmptyEl.textContent = T.loadFail;
      return;
    }
    const conversations = res.body.conversations || [];
    renderConversations(conversations);
    const wanted = new URLSearchParams(window.location.search).get('c');
    /* Fall back to the most recent thread rather than an empty screen: the list is
       already ordered by last activity, so [0] is where the student left off. */
    const target = wanted || (conversations[0] && conversations[0].id);
    if (target) await openConversation(target);
  })();
})();
