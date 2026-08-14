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
  const emptyStateEl = document.getElementById('saEmptyState');
  const statusEl = document.getElementById('saStatus');
  const composerErrorEl = document.getElementById('saComposerError');
  const layoutEl = document.getElementById('saAdvisorLayout');
  const historyDrawerEl = document.getElementById('saConversationDrawer');
  const historyToggleBtn = document.getElementById('saHistoryToggle');
  const historyCloseBtn = document.getElementById('saHistoryClose');
  const historyBackdropBtn = document.getElementById('saHistoryBackdrop');
  const threadTitleEl = document.getElementById('saThreadTitle');
  const jumpLatestBtn = document.getElementById('saJumpLatest');
  if (!formEl || !questionEl || !messagesEl || !sendBtn || !convListEl) return;

  const newConversationTitle = newChatBtn
    ? ((newChatBtn.querySelector('span:last-child') || newChatBtn).textContent || '').trim()
    : '';

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

    timetableTitle: AR ? 'خيارات الجدول' : 'Timetable alternatives',
    planningOnly: AR
      ? 'مقترحات للتخطيط فقط — لا يتم حفظ جدول أو تسجيل مقرر هنا.'
      : 'Planning proposals only — nothing is saved and no course is registered here.',
    currentSections: AR ? 'الشعب الحالية المثبتة' : 'Current retained sections',
    expectedSections: AR ? 'شُعب الخطة المتوقعة المثبتة' : 'Expected-plan sections retained',
    plannerOption: AR ? 'خيار المخطط' : 'Planner option',
    creditHours: AR ? 'ساعة' : 'credits',
    meetings: AR ? 'المواعيد' : 'Meetings',
    section: AR ? 'الشعبة' : 'section',
    enforcedConstraints: AR ? 'قيود الجدول المطلوبة' : 'Requested timetable constraints',
    mustTake: AR ? 'مقرر إلزامي' : 'Must take',
    pinnedSection: AR ? 'شعبة مثبتة' : 'Pinned section',
    constraintProblems: AR ? 'تعذر تحقيق القيود المطلوبة' : 'Requested constraints could not be satisfied',
    noValidConstrainedOption: AR
      ? 'لم يُعرض جدول جزئي على أنه صالح. عدّل القيد المطلوب ثم أعد المحاولة.'
      : 'No partial timetable is presented as valid. Adjust the requested constraint and try again.',
    unplaced: AR ? 'لم تُدرج في هذا الخيار' : 'Not placed in this option',
    noAdditions: AR ? 'لا توجد إضافات في هذا الخيار.' : 'This option has no additions.',
    noAdditionalCourses: AR
      ? 'تم الإبقاء على جدولك الحالي؛ لا يوجد مقرر إضافي مطلوب أو موصى به لبناء خيار جديد.'
      : 'Your current timetable is retained; there is no requested or recommended additional course to build into a new option.',
    noAdditionalExpectedCourses: AR
      ? 'تم الإبقاء على جدولك المتوقع؛ لا يوجد مقرر إضافي مطلوب أو موصى به لبناء خيار جديد. هذه الخطة ليست تسجيلًا فعليًا.'
      : 'Your expected timetable is retained; there is no requested or recommended additional course to build into a new option. This plan is not actual registration.',
    replaceCourse: AR ? 'استبدل' : 'Replace',
    replaceWith: AR ? 'بـ' : 'with',
    outsidePlanReplacement: AR
      ? 'تنبيه: المقرر البديل خارج خطتك الدراسية المسجلة؛ تحقق من احتسابه في بوابة الجامعة.'
      : 'Caution: the replacement course is outside your recorded study plan; verify how it will count in the university portal.',

    graduationMapTitle: AR ? 'مسار السيناريو حتى إكمال الخطة' : 'Scenario path to plan completion',
    graduationMapComplete: AR
      ? 'وصلت المحاكاة إلى جميع متطلبات الخطة. هذا تقدير تخطيطي وليس موعد تخرج رسميًا.'
      : 'The simulation reached every plan requirement. This is a planning estimate, not an official graduation date.',
    graduationMapIncomplete: AR
      ? 'تعرض الخريطة ما استطاعت المحاكاة ترتيبه فقط، ثم تتوقف عند المتطلبات غير المحسومة؛ لذلك لا تمثل موعدًا نهائيًا للتخرج.'
      : 'The map shows only what the simulation could schedule, then stops at unresolved requirements; it is not a final graduation date.',
    graduationReadOnly: AR
      ? 'سيناريو للقراءة فقط — لا يغيّر جدولك أو يسجل مقررات في بوابة الجامعة.'
      : 'Read-only scenario — it does not change your timetable or register courses in the university portal.',
    scenarioTerms: AR ? 'حسب فصول السيناريو' : 'By scenario term',
    prerequisiteChain: AR ? 'حسب سلسلة المتطلبات' : 'By prerequisite chain',
    completedBefore: AR ? 'مجتاز قبل السيناريو' : 'Passed before scenario',
    planningBaselineScenario: AR ? 'الفصل المرجعي للتخطيط' : 'Planning baseline term',
    projectedScenario: AR ? 'فصل متوقع' : 'Projected term',
    assumedBaseline: AR ? 'مفترض اجتيازه في الفصل المرجعي للتخطيط' : 'Assumed passed in the planning baseline term',
    projectedCourse: AR ? 'مخطط في السيناريو' : 'Planned in scenario',
    unresolvedCourse: AR ? 'غير محسوم' : 'Unresolved',
    unresolvedRequirements: AR ? 'متطلبات لم تحسمها المحاكاة' : 'Requirements the simulation could not resolve',
    missingPrerequisites: AR ? 'متطلبات سابقة ناقصة' : 'Missing prerequisites',
    creditGate: AR ? 'شرط الساعات' : 'Credit requirement',
    scenarioChange: AR ? 'تعديل الفصل المرجعي للتخطيط في هذا السيناريو' : 'Planning-baseline change in this scenario',
    removed: AR ? 'حذف' : 'Removed',
    added: AR ? 'إضافة' : 'Added',
    maximumPerTerm: AR ? 'حد المحاكاة لكل فصل' : 'Simulation cap per term',
    waitingTerm: AR ? 'لا مقررات مخططة' : 'no planned courses',
    openFullMap: AR ? 'عرض الخريطة بحجم كامل' : 'Open full scenario map',
    closeFullMap: AR ? 'إغلاق العرض الكامل' : 'Close full-screen map',

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
  let activeExpandedMapCloser = null;
  let answerRevealTimer = null;
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

  /* The conversation list is navigation, not part of the academic answer. Keeping
     it in an off-canvas drawer gives structured results the full workspace width
     while preserving every existing conversation action. */
  function setHistoryOpen(open, restoreFocus) {
    if (!layoutEl || !historyDrawerEl || !historyToggleBtn) return;
    layoutEl.classList.toggle('history-open', !!open);
    historyDrawerEl.setAttribute('aria-hidden', String(!open));
    historyToggleBtn.setAttribute('aria-expanded', String(!!open));
    if (open && historyCloseBtn) historyCloseBtn.focus();
    if (!open && restoreFocus) historyToggleBtn.focus();
  }

  if (historyToggleBtn) {
    historyToggleBtn.addEventListener('click', function () {
      setHistoryOpen(historyToggleBtn.getAttribute('aria-expanded') !== 'true', false);
    });
  }
  if (historyCloseBtn) historyCloseBtn.addEventListener('click', function () { setHistoryOpen(false, true); });
  if (historyBackdropBtn) historyBackdropBtn.addEventListener('click', function () { setHistoryOpen(false, true); });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && activeExpandedMapCloser) {
      activeExpandedMapCloser();
      return;
    }
    if (event.key === 'Escape' && layoutEl && layoutEl.classList.contains('history-open')) {
      setHistoryOpen(false, true);
    }
  });

  /* A compact one-line composer that grows only when the question needs it. The
     cap prevents a long draft from shrinking the conversation into a sliver. */
  function resizeComposer() {
    questionEl.style.height = 'auto';
    const height = Math.min(Math.max(questionEl.scrollHeight, 46), 144);
    questionEl.style.height = height + 'px';
    formEl.classList.toggle('is-expanded', height > 58);
  }

  questionEl.addEventListener('input', resizeComposer);
  questionEl.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    if (!sendBtn.disabled) formEl.requestSubmit(sendBtn);
  });
  resizeComposer();

  /* The advisor is a workspace, not a fixed dashboard card. Measure the visible
     viewport below the page heading so the composer sits at its lower edge. The
     visual viewport matters on phones: it shrinks when the software keyboard
     opens, while 100vh does not reliably do so. */
  let workspaceResizeFrame = null;
  function fitWorkspaceToViewport() {
    workspaceResizeFrame = null;
    if (!layoutEl) return;
    const viewport = window.visualViewport;
    const viewportTop = viewport ? viewport.offsetTop : 0;
    const viewportBottom = viewport
      ? viewport.offsetTop + viewport.height
      : window.innerHeight;
    const layoutTop = Math.max(layoutEl.getBoundingClientRect().top, viewportTop);
    const keyboardOpen = !!viewport && viewport.height < window.innerHeight - 100;
    const minimum = keyboardOpen ? 240 : (window.innerWidth <= 768 ? 360 : 480);
    const available = Math.floor(viewportBottom - layoutTop - 12);
    layoutEl.style.setProperty('--sa-workspace-height', Math.max(minimum, available) + 'px');
  }
  function scheduleWorkspaceFit() {
    if (workspaceResizeFrame !== null) cancelAnimationFrame(workspaceResizeFrame);
    workspaceResizeFrame = requestAnimationFrame(fitWorkspaceToViewport);
  }
  window.addEventListener('resize', scheduleWorkspaceFit, { passive: true });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', scheduleWorkspaceFit, { passive: true });
    window.visualViewport.addEventListener('scroll', scheduleWorkspaceFit, { passive: true });
  }
  scheduleWorkspaceFit();

  function syncJumpLatest() {
    if (!jumpLatestBtn) return;
    const remaining = messagesEl.scrollHeight - messagesEl.clientHeight - messagesEl.scrollTop;
    jumpLatestBtn.hidden = remaining < 120;
  }

  function scrollToLatest() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
    syncJumpLatest();
  }

  messagesEl.addEventListener('scroll', syncJumpLatest, { passive: true });
  if (jumpLatestBtn) jumpLatestBtn.addEventListener('click', scrollToLatest);

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

  /* Hebrew, Arabic, Syriac, Thaana, NKo, and the Arabic presentation forms.
     U+0600-0605 and U+06DD are AN, not strong; the tashkeel at U+064B-065F are
     NSM; U+060C and U+061F are neutral punctuation. None of them decides a
     direction, so none of them is counted. */
  const RTL_STRONG = /[א-״ؠ-يٮ-ۓەۥۦۮۯۺ-ۿܐ-ݏހ-ޥޱߊ-ߪࡠ-ࡪࢠ-ࢴיִ-ﴽﵐ-ﷻﹰ-ﻼ]/;
  /* Latin, Latin Extended, IPA, Greek/Coptic, Cyrillic, Armenian. Stops at
     U+02AF: the spacing modifiers and the mathematical signs U+00D7 and U+00F7
     that fall inside a naive \u00C0-\u02FF range are ON, not strong — a naive
     range made `textDirection('3 × 4')` answer 'ltr' on the strength of a
     multiplication sign. */
  const LTR_STRONG = /[A-Za-zÀ-ÖØ-öø-ʯͰ-ͳͶ-ͽͿ-ϿЀ-ԯԱ-Ֆա-և]/;

  /* WHICH SCRIPT A SHORT LABEL IS IN — for the nodes the server does not label.

     THE SERVER ALREADY DECIDED THIS FOR EVERY MESSAGE.
     `virtual_advisor._answer_language` picks Arabic or English from the question,
     deterministically, before generation, and pins the model to it with an
     instruction never to switch. The API now ships that decision as
     `message.language`, and `renderBody` takes it. No message's direction is
     guessed here any more.

     That matters because every character rule tried here was wrong on a real
     answer shape. `dir="auto"` takes the FIRST strong character, so an Arabic
     answer opening «AI221 هو المقرر…» computes LTR. A character MAJORITY fails in
     the other direction: Arabic words are short and English course titles are
     long, so «الشرط المسبق هو Introduction to Artificial Intelligence قبل
     التسجيل» is 23 Arabic characters against 36 Latin, and a timetable table is
     almost entirely course codes and clock times. Both are Arabic answers, and
     both lose a count.

     What is left for this function is the handful of nodes with no message behind
     them: a conversation title, a case reference, a citation line. They are short
     labels rather than prose, first-strong is the right rule for them, and the
     interface language is the right tie-break when they hold no strong character
     at all. */
  function textDirection(text) {
    const source = String(text == null ? '' : text);
    for (let i = 0; i < source.length; i += 1) {
      const ch = source.charAt(i);
      if (RTL_STRONG.test(ch)) return 'rtl';
      if (LTR_STRONG.test(ch)) return 'ltr';
    }
    return AR ? 'rtl' : 'ltr';
  }

  /* ── what needs isolating, and what must NOT be ─────────────
     The first version of this fix isolated every run of Latin-or-digits. That is
     a way of REVERSING them, and it shipped the very defect it was written to
     remove.

     UAX#9 BD8/X9: an isolate is replaced by U+FFFC in the enclosing run, and
     U+FFFC is class ON. Splitting one Latin sequence at any character the pattern
     did not treat as an internal separator — `@`, `?`, `&`, `%`, `,` — therefore
     deleted every strong L from the outer paragraph. Those separators had been
     resolving to L by N1, as neutrals between two L runs; with both sides now
     neutral they resolve to R by N2, and L2 lays the pieces out right to left.

     Measured on the real page: «reg@taibahu.edu.sa» reached the student as
     «taibahu.edu.sa@reg» — an address that does not exist, in a sentence telling
     them to write to it. Also every URL with a query string, every `%20` file
     name, and any English clause containing a comma.

     A LATIN LETTER NEVER NEEDED ISOLATING. Rule W7 changes a European number to L
     when the last strong type before it is L, so `AI221`, `Study%20Plan.pdf` and
     `Section 3-4` were always one coherent L island and N1 bound their
     punctuation into it.

     What reorders is a number with no Latin letter in front of it: W2 makes it AN
     when the last strong type is AL, W4 then refuses to bind the hyphen (it fuses
     an ES separator only between two EN), and L2 swaps the groups around it. So
     the rule here is UAX#9's own condition — isolate a digit run when the last
     strong character before it is right-to-left, and leave every other run alone. */
  const DIGIT_RUN = /[0-9](?:[0-9 \t:.\/-]*[0-9])?/g;

  /* Carries the last strong character across a whole block. `inlineInto` hands
     the line over in pieces around `**bold**`, and the bidi paragraph does not
     restart at those boundaries. Seeded with the block's own direction, which is
     what sot resolves to. */
  function strongScanner(dir) {
    return { last: dir === 'rtl' ? 'rtl' : 'ltr' };
  }

  function trackStrong(state, text) {
    for (let i = 0; i < text.length; i += 1) {
      const ch = text.charAt(i);
      if (RTL_STRONG.test(ch)) state.last = 'rtl';
      else if (LTR_STRONG.test(ch)) state.last = 'ltr';
    }
  }

  function appendIsolated(node, text, state) {
    const source = String(text);
    let cursor = 0;
    let match;
    DIGIT_RUN.lastIndex = 0;
    while ((match = DIGIT_RUN.exec(source)) !== null) {
      const before = source.slice(cursor, match.index);
      trackStrong(state, before);
      if (before) node.appendChild(document.createTextNode(before));

      if (state.last === 'rtl') {
        const run = el('bdi', null, match[0]);
        run.setAttribute('dir', 'ltr');
        node.appendChild(run);
      } else {
        /* Preceded by a Latin letter, so W7 already resolved these digits to L.
           Isolating here would turn a working L island into a neutral and hand its
           punctuation to N2 — the defect above, in miniature. */
        node.appendChild(document.createTextNode(match[0]));
      }
      cursor = match.index + match[0].length;
    }
    const tail = source.slice(cursor);
    if (tail) {
      trackStrong(state, tail);
      node.appendChild(document.createTextNode(tail));
    }
    return node;
  }

  /* An early-out for LTR blocks, and DELIBERATELY REDUNDANT: the W7 gate below
     already decides this. In an LTR block sot is `ltr`, so a digit run is only
     isolated when an Arabic letter precedes it — and in an LTR paragraph even that
     does not reorder, because N2 resolves the neutral to L rather than to R. A
     mutant that deletes this guard is therefore EQUIVALENT, and measured to be so
     rather than assumed; it is kept because it says what the rule is for and
     because it skips a per-character scan of every English answer. */
  function appendDirected(node, text, dir, state) {
    if (dir === 'rtl') return appendIsolated(node, text, state || strongScanner(dir));
    node.appendChild(document.createTextNode(String(text)));
    return node;
  }

  /* The one entry point for a SHORT LABEL with no message behind it. Every such
     node on this screen goes through it, so none can be forgotten the way the
     human adviser's reply was. Messages do not use this — they carry the
     language the server pinned. */
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
      const key = [c.document_title, c.edition, c.page].join('\u0000');
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
      /* The one message on this screen written by a PERSON, and the one that had
         no direction at all — an Arabic reply laid out left-to-right in the default
         English interface, on the answer a student has been waiting days to read.
         It takes the language the student ASKED in, which the API carries: it is
         who the reply is addressed to, and reading the direction off the reply's
         own characters is the guess this change exists to remove. */
      const replyDir = escalation.language === 'ar' ? 'rtl'
        : escalation.language === 'en' ? 'ltr'
        : textDirection(escalation.resolution_message);
      const para = el('p');
      para.setAttribute('dir', replyDir);
      reply.appendChild(appendDirected(para, escalation.resolution_message, replyDir));
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

  /* One scanner per BLOCK, threaded through every segment: `**bold**` splits the
     line into pieces, but the bidi paragraph does not restart at a <strong>, so
     "the last strong character before this number" has to be carried across them.
     Scoped to the block and not to the message, because each <p> and each <li> IS
     its own bidi paragraph and sot resets at its start. */
  function inlineInto(node, text, dir) {
    const source = String(text);
    const state = strongScanner(dir);
    let cursor = 0;
    let match;
    BOLD.lastIndex = 0;
    while ((match = BOLD.exec(source)) !== null) {
      if (match.index > cursor) {
        appendDirected(node, source.slice(cursor, match.index), dir, state);
      }
      node.appendChild(appendDirected(el('strong'), match[1], dir, state));
      cursor = match.index + match[0].length;
    }
    if (cursor < source.length) {
      appendDirected(node, source.slice(cursor), dir, state);
    }
    return node;
  }

  function renderBody(text, language) {
    const wrap = el('div', 'sa-body');
    /* ONE direction for the whole answer, stated on the body so every block
       inherits it — per-block `dir="auto"` is what let a list hold two — and taken
       from the SERVER, which pinned the model to this language before it wrote a
       word. `textDirection` is the fallback for a stored row from before the API
       carried it, and for nothing else. */
    const dir = language === 'ar' ? 'rtl' : language === 'en' ? 'ltr' : textDirection(text);
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

  function renderThinkingMessage() {
    const article = el('article', 'va-message sa-thinking-message');
    article.id = 'saThinkingMessage';
    article.setAttribute('aria-hidden', 'true');
    article.appendChild(el('div', 'va-avatar', 'AI'));
    const bubble = el('div', 'va-bubble');
    const dots = el('span', 'sa-thinking-dots');
    for (let i = 0; i < 3; i += 1) dots.appendChild(el('span'));
    bubble.appendChild(dots);
    bubble.appendChild(el('span', 'sa-thinking-label', T.thinking));
    article.appendChild(bubble);
    return article;
  }

  function showThinkingMessage() {
    const existing = document.getElementById('saThinkingMessage');
    if (existing) existing.remove();
    messagesEl.appendChild(renderThinkingMessage());
    scrollToLatest();
  }

  function cancelAnswerReveal() {
    if (answerRevealTimer !== null) clearTimeout(answerRevealTimer);
    answerRevealTimer = null;
  }

  function progressiveRevealEnabled() {
    if (window.__SA_FORCE_PROGRESSIVE_REVEAL__ === true) return true;
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      return false;
    }
    /* Browser automation reads completed DOM deterministically. The dedicated
       animation test opts back in; real student browsers animate normally. */
    return !navigator.webdriver;
  }

  function revealAssistantMessage(article) {
    if (!article || !progressiveRevealEnabled()) return;
    const body = article.querySelector('.sa-body');
    const bubble = article.querySelector('.va-bubble');
    if (!body || !bubble) return;

    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    let current = walker.nextNode();
    while (current) {
      if (current.nodeValue) textNodes.push(current);
      current = walker.nextNode();
    }
    const queue = [];
    textNodes.forEach(function (node) {
      const parts = node.nodeValue.match(/\S+\s*|\s+/gu) || [];
      node.nodeValue = '';
      parts.forEach(function (part) { queue.push({ node: node, text: part }); });
    });
    if (!queue.length) return;

    const deferred = Array.from(bubble.children)
      .filter(function (node) { return node !== body; })
      .map(function (node) {
        const state = { node: node, hidden: node.hidden };
        node.hidden = true;
        return state;
      });
    const caret = el('span', 'sa-answer-caret');
    caret.setAttribute('aria-hidden', 'true');
    body.appendChild(caret);
    article.classList.add('is-revealing');
    article.setAttribute('aria-busy', 'true');

    let index = 0;
    let ticks = 0;
    const perTick = Math.max(1, Math.ceil(queue.length / 180));
    function finish() {
      cancelAnswerReveal();
      caret.remove();
      deferred.forEach(function (state) { state.node.hidden = state.hidden; });
      article.classList.remove('is-revealing');
      article.removeAttribute('aria-busy');
      scrollToLatest();
    }
    function writeNext() {
      const stop = Math.min(queue.length, index + perTick);
      while (index < stop) {
        const item = queue[index];
        item.node.nodeValue += item.text;
        index += 1;
      }
      ticks += 1;
      if (ticks % 4 === 0) scrollToLatest();
      if (index >= queue.length) {
        finish();
        return;
      }
      answerRevealTimer = setTimeout(writeNext, 20);
    }
    writeNext();
  }

  const TIMETABLE_DAYS = {
    SUN: AR ? 'الأحد' : 'Sun',
    MON: AR ? 'الاثنين' : 'Mon',
    TUE: AR ? 'الثلاثاء' : 'Tue',
    WED: AR ? 'الأربعاء' : 'Wed',
    THU: AR ? 'الخميس' : 'Thu',
    FRI: AR ? 'الجمعة' : 'Fri',
    SAT: AR ? 'السبت' : 'Sat',
  };

  function ltrNode(tag, className, text) {
    const node = el(tag, className, text);
    node.setAttribute('dir', 'ltr');
    return node;
  }

  function timetableCourseKey(row) {
    const node = el('strong', 'sa-tt-course-key');
    node.appendChild(ltrNode('bdi', null, String(row.course_code || '')));
    if (row.section) {
      node.appendChild(document.createTextNode(' · ' + T.section + ' '));
      node.appendChild(ltrNode('bdi', null, String(row.section)));
    }
    return node;
  }

  function renderTimetablePresentation(presentation, language) {
    if (!presentation || presentation.kind !== 'timetable_proposals') return null;
    const alternatives = Array.isArray(presentation.alternatives)
      ? presentation.alternatives : [];
    const baselineKind = String(presentation.baseline_kind || 'REGISTERED').toUpperCase();
    if (baselineKind === 'MIXED_REVIEW_REQUIRED') return null;
    const baseline = Array.isArray(presentation.baseline_sections)
      ? presentation.baseline_sections
      : (baselineKind === 'EXPECTED_PLAN' && Array.isArray(presentation.expected_plan_sections)
        ? presentation.expected_plan_sections
        : (Array.isArray(presentation.current_sections) ? presentation.current_sections : []));
    const mustTake = Array.isArray(presentation.must_take_courses)
      ? presentation.must_take_courses.filter(Boolean) : [];
    const pinned = Array.isArray(presentation.pinned_sections)
      ? presentation.pinned_sections : [];
    const constraintFailures = Array.isArray(presentation.constraint_failures)
      ? presentation.constraint_failures : [];
    if (!alternatives.length && !baseline.length && !mustTake.length
        && !pinned.length && !constraintFailures.length) return null;

    const wrap = el('section', 'sa-timetable');
    const dir = language === 'ar' ? 'rtl' : language === 'en' ? 'ltr' : (AR ? 'rtl' : 'ltr');
    wrap.setAttribute('dir', dir);
    wrap.setAttribute('aria-label', T.timetableTitle);

    const heading = el('div', 'sa-tt-heading');
    heading.appendChild(el('h4', 'sa-tt-title', T.timetableTitle));
    if (presentation.planning_term) {
      heading.appendChild(ltrNode('span', 'sa-tt-term', presentation.planning_term));
    }
    wrap.appendChild(heading);
    wrap.appendChild(el('p', 'sa-tt-boundary', T.planningOnly));

    const replacement = presentation.replacement && typeof presentation.replacement === 'object'
      ? presentation.replacement : null;
    const removedCourse = replacement && replacement.remove_course;
    const addedCourse = replacement && replacement.add_course;
    if (removedCourse && removedCourse.course_code && addedCourse && addedCourse.course_code) {
      const notice = el('section', 'sa-tt-replacement');
      notice.setAttribute('aria-label', T.replaceCourse);
      const swap = el('p', 'sa-tt-replacement-swap');
      swap.appendChild(el('span', null, T.replaceCourse + ' '));
      swap.appendChild(ltrNode('strong', 'sa-tt-replacement-code is-removed', String(removedCourse.course_code)));
      swap.appendChild(document.createTextNode(' ' + T.replaceWith + ' '));
      swap.appendChild(ltrNode('strong', 'sa-tt-replacement-code is-added', String(addedCourse.course_code)));
      notice.appendChild(swap);
      if (replacement.outside_plan_addition === true) {
        const caution = el('p', 'sa-tt-replacement-caution', T.outsidePlanReplacement);
        caution.setAttribute('role', 'note');
        notice.appendChild(caution);
      }
      wrap.appendChild(notice);
    }

    if (mustTake.length || pinned.length) {
      const constraints = el('section', 'sa-tt-constraints');
      constraints.appendChild(el('h5', 'sa-tt-subtitle', T.enforcedConstraints));
      const chips = el('div', 'sa-tt-constraint-chips');
      mustTake.forEach(function (code) {
        const chip = el('span', 'sa-tt-constraint-chip is-required');
        chip.appendChild(el('span', null, T.mustTake + ': '));
        chip.appendChild(ltrNode('bdi', null, String(code)));
        chips.appendChild(chip);
      });
      pinned.forEach(function (row) {
        if (!row || !row.course_code || !row.section_label) return;
        const chip = el('span', 'sa-tt-constraint-chip is-pinned');
        chip.appendChild(el('span', null, T.pinnedSection + ': '));
        chip.appendChild(ltrNode('bdi', null, String(row.course_code)));
        chip.appendChild(document.createTextNode(' · '));
        chip.appendChild(ltrNode('bdi', null, String(row.section_label)));
        chips.appendChild(chip);
      });
      constraints.appendChild(chips);
      wrap.appendChild(constraints);
    }

    if (constraintFailures.length) {
      const alert = el('section', 'sa-tt-constraint-alert');
      alert.setAttribute('role', 'alert');
      alert.appendChild(el('h5', 'sa-tt-subtitle', T.constraintProblems));
      const list = el('ul', null);
      constraintFailures.forEach(function (row) {
        const item = el('li');
        if (row && row.course_code) {
          item.appendChild(ltrNode('strong', null, String(row.course_code)));
          if (row.section_label) {
            item.appendChild(document.createTextNode(' · '));
            item.appendChild(ltrNode('bdi', null, String(row.section_label)));
          }
        }
        if (row && row.reason) {
          if (item.childNodes.length) item.appendChild(document.createTextNode(' — '));
          item.appendChild(document.createTextNode(String(row.reason)));
        }
        list.appendChild(item);
      });
      alert.appendChild(list);
      if (!alternatives.length) {
        alert.appendChild(el('p', 'sa-tt-empty', T.noValidConstrainedOption));
      }
      wrap.appendChild(alert);
    }

    if (baseline.length) {
      const retained = el('details', 'sa-tt-current');
      const retainedLabel = baselineKind === 'EXPECTED_PLAN'
        ? T.expectedSections : T.currentSections;
      retained.appendChild(el('summary', null, retainedLabel + ' (' + baseline.length + ')'));
      const list = el('div', 'sa-tt-current-list');
      baseline.forEach(function (course) {
        const row = el('div', 'sa-tt-current-row');
        row.appendChild(timetableCourseKey(course));
        if (course.course_name) row.appendChild(el('span', 'sa-tt-course-name', course.course_name));
        (course.meetings || []).forEach(function (meeting) {
          row.appendChild(ltrNode('span', 'sa-tt-current-meeting', meeting));
        });
        list.appendChild(row);
      });
      retained.appendChild(list);
      wrap.appendChild(retained);
    }

    if (!alternatives.length && presentation.no_additional_courses === true) {
      wrap.appendChild(el(
        'p',
        'sa-tt-empty sa-tt-no-additional-courses',
        baselineKind === 'EXPECTED_PLAN'
          ? T.noAdditionalExpectedCourses : T.noAdditionalCourses
      ));
    }

    const optionList = el('div', 'sa-tt-options');
    alternatives.forEach(function (option, index) {
      const details = el('details', 'sa-tt-option');
      details.open = index === 0;
      const summary = el('summary', 'sa-tt-summary');
      const names = (option.planner_options || []).filter(Boolean);
      const title = names.length ? names.join(' / ') : String(index + 1);
      const optionName = el('strong', 'sa-tt-option-name');
      optionName.appendChild(document.createTextNode(T.plannerOption + ' '));
      optionName.appendChild(ltrNode('bdi', null, title));
      summary.appendChild(optionName);

      const coverage = Number(option.scheduled_courses || 0) + '/' + Number(option.target_courses || 0);
      const credits = Number(option.total_credit_hours || option.proposed_credit_hours || 0);
      const meta = el('span', 'sa-tt-summary-meta');
      meta.appendChild(ltrNode('span', 'sa-tt-coverage', coverage));
      if (credits) meta.appendChild(el('span', 'sa-tt-credits', credits + ' ' + T.creditHours));
      summary.appendChild(meta);
      details.appendChild(summary);

      const body = el('div', 'sa-tt-option-body');
      const meetings = Array.isArray(option.meetings) ? option.meetings : [];
      if (meetings.length) {
        body.appendChild(el('h5', 'sa-tt-subtitle', T.meetings));
        const meetingList = el('ul', 'sa-tt-meetings');
        meetings.forEach(function (meeting) {
          const item = el('li', 'sa-tt-meeting');
          const course = el('span', 'sa-tt-meeting-course');
          course.appendChild(timetableCourseKey(meeting));
          if (meeting.course_name) course.appendChild(el('small', null, meeting.course_name));
          item.appendChild(course);

          const when = el('span', 'sa-tt-when');
          when.appendChild(el('span', 'sa-tt-day', TIMETABLE_DAYS[meeting.day] || meeting.day || ''));
          when.appendChild(
            ltrNode('bdi', 'sa-tt-time', String(meeting.start || '') + '–' + String(meeting.end || ''))
          );
          item.appendChild(when);
          meetingList.appendChild(item);
        });
        body.appendChild(meetingList);
      } else {
        body.appendChild(el('p', 'sa-tt-empty', T.noAdditions));
      }

      const unplaced = Array.isArray(option.unplaced_courses) ? option.unplaced_courses : [];
      if (unplaced.length) {
        body.appendChild(el('h5', 'sa-tt-subtitle sa-tt-unplaced-title', T.unplaced));
        const unplacedList = el('ul', 'sa-tt-unplaced');
        unplaced.forEach(function (course) {
          const item = el('li');
          item.appendChild(ltrNode('strong', 'sa-tt-course-key', course.course_code || ''));
          if (course.course_name) item.appendChild(document.createTextNode(' — ' + course.course_name));
          if (course.reason) item.appendChild(el('span', 'sa-tt-reason', course.reason));
          unplacedList.appendChild(item);
        });
        body.appendChild(unplacedList);
      }
      details.appendChild(body);
      optionList.appendChild(details);
    });
    wrap.appendChild(optionList);
    return wrap;
  }

  function graduationBandLabel(value) {
    const label = String(value || '');
    if (label === 'Completed before the scenario') return T.completedBefore;
    if (label.indexOf('Planning baseline ') === 0) {
      return T.planningBaselineScenario + ' ' + label.slice('Planning baseline '.length);
    }
    if (label.indexOf('Current ') === 0) {
      // Backward compatibility for already stored presentation payloads.
      return T.planningBaselineScenario + ' ' + label.slice('Current '.length);
    }
    if (label.indexOf('Projected ') === 0) {
      return T.projectedScenario + ' ' + label.slice('Projected '.length);
    }
    return label;
  }

  function graduationGraphStrings(labels) {
    const band = function (n) {
      return graduationBandLabel(labels[String(n)] || String(n));
    };
    return {
      termHeading: band,
      pgNoTermBand: T.waitingTerm,
      pgGateTip: function (h) { return T.creditGate + ': ' + h + ' ' + T.creditHours; },
      pgInferredTip: AR ? 'موضع مستنتج خارج ترتيب السيناريو' : 'position inferred outside the scenario order',
      pgTermTip: band,
      pgGate: T.creditGate,
      pgInferred: AR ? 'موضع مستنتج' : 'inferred position',
      pgFoundation: AR ? 'بداية السلسلة' : 'chain start',
      pgIntermediate: AR ? 'وسط السلسلة' : 'chain middle',
      pgTerminal: AR ? 'نهاية السلسلة' : 'chain end',
      pgHoverHint: AR ? 'مرّر لإبراز السلسلة' : 'hover to highlight a chain',
      pgPassed: T.completedBefore,
      pgStudying: T.assumedBaseline,
      pgOpen: T.projectedCourse,
      pgLocked: T.unresolvedCourse,
      pgSameTermWarn: function (n) {
        return AR ? n + ' علاقة متطلبات داخل الفصل نفسه' : n + ' prerequisite relation(s) within one term';
      },
      pgBackwardWarn: function (n) {
        return AR ? n + ' علاقة متطلبات بعد مقررها' : n + ' prerequisite relation(s) after their course';
      },
    };
  }

  function renderGraduationMobileList(graph, labels) {
    const host = el('div', 'sa-grad-mobile');
    const byTerm = new Map();
    (graph.extraNodes || []).forEach(function (code) {
      const term = Number(graph.termOf && graph.termOf[code]);
      const key = Number.isFinite(term) ? term : 0;
      if (!byTerm.has(key)) byTerm.set(key, []);
      byTerm.get(key).push(code);
    });
    const statusText = {
      passed: T.completedBefore,
      studying: T.assumedBaseline,
      open: T.projectedCourse,
      locked: T.unresolvedCourse,
    };
    const statusClass = {
      passed: 'is-passed', studying: 'is-studying', open: 'is-open', locked: 'is-locked',
    };
    Array.from(byTerm.keys()).sort(function (a, b) { return a - b; }).forEach(function (term) {
      const section = el('section', 'sa-grad-mobile-term');
      section.appendChild(el('h5', 'sa-grad-band-title', graduationBandLabel(labels[String(term)] || term)));
      const courses = el('div', 'sa-grad-mobile-courses');
      byTerm.get(term).sort().forEach(function (code) {
        const status = (graph.statusOf && graph.statusOf[code]) || 'locked';
        const item = el('span', 'sa-grad-course ' + (statusClass[status] || 'is-locked'));
        item.title = ((graph.nameOf && graph.nameOf[code]) || code) + ' — ' + (statusText[status] || status);
        item.appendChild(ltrNode('bdi', null, code));
        courses.appendChild(item);
      });
      section.appendChild(courses);
      host.appendChild(section);
    });
    return host;
  }

  function renderGraduationPresentation(presentation, language) {
    if (!presentation || presentation.kind !== 'graduation_scenario') return null;
    const graph = presentation.graph || {};
    if (!Array.isArray(graph.extraNodes) || !graph.extraNodes.length) return null;

    const wrap = el('section', 'sa-graduation-map');
    const dir = language === 'ar' ? 'rtl' : language === 'en' ? 'ltr' : (AR ? 'rtl' : 'ltr');
    wrap.setAttribute('dir', dir);
    wrap.setAttribute('aria-label', T.graduationMapTitle);

    const heading = el('div', 'sa-tt-heading');
    const title = presentation.program
      ? T.graduationMapTitle + ' — ' + presentation.program : T.graduationMapTitle;
    heading.appendChild(el('h4', 'sa-tt-title', title));
    const headingActions = el('div', 'sa-grad-heading-actions');
    if (presentation.planning_term) {
      headingActions.appendChild(ltrNode('span', 'sa-tt-term', presentation.planning_term));
    }
    const expand = el('button', 'btn btn-sm sa-grad-expand', T.openFullMap);
    expand.type = 'button';
    expand.setAttribute('aria-expanded', 'false');
    let mapPlaceholder = null;
    function closeThisMap() { setMapExpanded(false); }
    function setMapExpanded(state) {
      if (state && activeExpandedMapCloser && activeExpandedMapCloser !== closeThisMap) {
        activeExpandedMapCloser();
      }
      if (state && !mapPlaceholder && wrap.parentNode) {
        mapPlaceholder = document.createComment('graduation-map-home');
        wrap.parentNode.insertBefore(mapPlaceholder, wrap);
        document.body.appendChild(wrap);
      }
      if (!state && mapPlaceholder && mapPlaceholder.parentNode) {
        mapPlaceholder.parentNode.insertBefore(wrap, mapPlaceholder);
        mapPlaceholder.remove();
        mapPlaceholder = null;
      }
      wrap.classList.toggle('is-expanded', state);
      document.documentElement.classList.toggle('sa-overlay-open', state);
      expand.setAttribute('aria-expanded', String(state));
      expand.textContent = state ? T.closeFullMap : T.openFullMap;
      activeExpandedMapCloser = state ? closeThisMap
        : (activeExpandedMapCloser === closeThisMap ? null : activeExpandedMapCloser);
      if (state) {
        wrap.setAttribute('role', 'dialog');
        wrap.setAttribute('aria-modal', 'true');
      } else {
        wrap.removeAttribute('role');
        wrap.removeAttribute('aria-modal');
        if (document.contains(expand)) expand.focus();
      }
    }
    expand.addEventListener('click', function () {
      setMapExpanded(expand.getAttribute('aria-expanded') !== 'true');
    });
    wrap.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && expand.getAttribute('aria-expanded') === 'true') {
        setMapExpanded(false);
        return;
      }
      if (event.key === 'Tab' && expand.getAttribute('aria-expanded') === 'true') {
        const focusable = Array.from(wrap.querySelectorAll('button, summary, a[href]'))
          .filter(function (node) { return !node.disabled && node.getClientRects().length; });
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });
    headingActions.appendChild(expand);
    heading.appendChild(headingActions);
    wrap.appendChild(heading);
    wrap.appendChild(el(
      'p',
      'sa-grad-result ' + (presentation.simulation_completed ? 'is-complete' : 'is-incomplete'),
      presentation.simulation_completed ? T.graduationMapComplete : T.graduationMapIncomplete
    ));
    wrap.appendChild(el('p', 'sa-tt-boundary', T.graduationReadOnly));

    const removed = Array.isArray(presentation.removed_current_courses)
      ? presentation.removed_current_courses : [];
    const added = Array.isArray(presentation.added_current_courses)
      ? presentation.added_current_courses : [];
    if (removed.length || added.length) {
      const change = el('div', 'sa-grad-change');
      change.appendChild(el('strong', null, T.scenarioChange + ': '));
      if (removed.length) {
        change.appendChild(document.createTextNode(T.removed + ' '));
        removed.forEach(function (course, index) {
          if (index) change.appendChild(document.createTextNode(', '));
          change.appendChild(ltrNode('bdi', null, course.code));
        });
      }
      if (removed.length && added.length) change.appendChild(document.createTextNode(' · '));
      if (added.length) {
        change.appendChild(document.createTextNode(T.added + ' '));
        added.forEach(function (course, index) {
          if (index) change.appendChild(document.createTextNode(', '));
          change.appendChild(ltrNode('bdi', null, course.code));
        });
      }
      wrap.appendChild(change);
    }

    const panel = el('div', 'sa-grad-panel');
    const toolbar = el('div', 'sa-grad-toolbar');
    const modeGroup = el('div', 'pg-modes');
    modeGroup.setAttribute('role', 'group');
    const byTerm = el('button', 'pg-mode is-on', T.scenarioTerms);
    byTerm.type = 'button'; byTerm.setAttribute('aria-pressed', 'true');
    const byChain = el('button', 'pg-mode', T.prerequisiteChain);
    byChain.type = 'button'; byChain.setAttribute('aria-pressed', 'false');
    modeGroup.appendChild(byTerm); modeGroup.appendChild(byChain);
    toolbar.appendChild(modeGroup);
    if (presentation.max_credits_per_term) {
      toolbar.appendChild(el(
        'span', 'sa-tt-credits',
        T.maximumPerTerm + ': ' + presentation.max_credits_per_term + ' ' + T.creditHours
      ));
    }
    panel.appendChild(toolbar);

    const desktop = el('div', 'sa-grad-desktop');
    desktop.setAttribute('dir', 'ltr');
    desktop.setAttribute('role', 'img');
    desktop.setAttribute('aria-label', T.graduationMapTitle);
    panel.appendChild(desktop);
    panel.appendChild(renderGraduationMobileList(graph, presentation.band_labels || {}));
    wrap.appendChild(panel);

    const draw = function (mode) {
      if (!window.PrereqGraph) return;
      desktop.innerHTML = '';
      window.PrereqGraph.render(graph.items || [], desktop, {
        termOf: graph.termOf || {},
        nameOf: graph.nameOf || {},
        statusOf: graph.statusOf || {},
        extraNodes: graph.extraNodes || [],
        mode: mode,
        t: graduationGraphStrings(presentation.band_labels || {}),
      });
    };
    byTerm.addEventListener('click', function () {
      byTerm.classList.add('is-on'); byTerm.setAttribute('aria-pressed', 'true');
      byChain.classList.remove('is-on'); byChain.setAttribute('aria-pressed', 'false');
      draw('term');
    });
    byChain.addEventListener('click', function () {
      byChain.classList.add('is-on'); byChain.setAttribute('aria-pressed', 'true');
      byTerm.classList.remove('is-on'); byTerm.setAttribute('aria-pressed', 'false');
      draw('depth');
    });
    draw('term');

    const unresolved = Array.isArray(presentation.unresolved_requirements)
      ? presentation.unresolved_requirements : [];
    if (unresolved.length) {
      const blockers = el('details', 'sa-grad-blockers');
      blockers.open = !presentation.simulation_completed;
      blockers.appendChild(el('summary', null, T.unresolvedRequirements + ' (' + unresolved.length + ')'));
      const list = el('ul', 'sa-grad-blocker-list');
      unresolved.forEach(function (row) {
        const item = el('li', 'sa-grad-blocker');
        item.appendChild(ltrNode('strong', null, row.code));
        if (row.name) item.appendChild(document.createTextNode(' — ' + row.name));
        if (Array.isArray(row.missing_prerequisites) && row.missing_prerequisites.length) {
          const missing = el('span', 'sa-grad-blocker-reason');
          missing.appendChild(document.createTextNode(T.missingPrerequisites + ': '));
          row.missing_prerequisites.forEach(function (code, index) {
            if (index) missing.appendChild(document.createTextNode(', '));
            missing.appendChild(ltrNode('bdi', null, code));
          });
          item.appendChild(missing);
        }
        if (row.credit_hour_gate && row.credit_hour_gate.required) {
          item.appendChild(el(
            'span', 'sa-grad-blocker-reason',
            T.creditGate + ': ' + row.credit_hour_gate.required + ' ' + T.creditHours
          ));
        }
        list.appendChild(item);
      });
      blockers.appendChild(list);
      wrap.appendChild(blockers);
    }
    return wrap;
  }

  function renderMessage(message) {
    const role = message.role === 'ASSISTANT' ? 'assistant' : 'user';
    const article = el('article', 'va-message va-message-' + role);
    article.dataset.messageId = message.id;
    article.dataset.status = message.status || '';

    article.appendChild(el('div', 'va-avatar', role === 'assistant' ? 'AI' : T.me));
    const bubble = el('div', 'va-bubble');
    bubble.appendChild(renderBody(displayBody(message), message.language));
    const presentation = renderGraduationPresentation(message.presentation, message.language)
      || renderTimetablePresentation(message.presentation, message.language);
    if (presentation) bubble.appendChild(presentation);

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

  function renderMessages(messages, options) {
    cancelAnswerReveal();
    if (activeExpandedMapCloser) activeExpandedMapCloser();
    document.documentElement.classList.remove('sa-overlay-open');
    messagesEl.innerHTML = '';
    const chat = messagesEl.closest('.va-chat');
    if (chat) chat.classList.toggle('has-messages', !!(messages && messages.length));
    if (emptyStateEl) {
      emptyStateEl.hidden = !!(messages && messages.length);
      messagesEl.appendChild(emptyStateEl);
    }
    if (!messages || !messages.length) {
      syncJumpLatest();
      return;
    }
    const revealMessageId = options && options.revealMessageId;
    messages.forEach(function (m) {
      const article = renderMessage(m);
      messagesEl.appendChild(article);
      if (revealMessageId && String(m.id) === String(revealMessageId)) {
        revealAssistantMessage(article);
      }
    });
    scrollToLatest();
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
      b.addEventListener('click', function () {
        setHistoryOpen(false, false);
        openConversation(c.id);
      });
      const li = el('li', 'sa-conv-item');
      li.appendChild(b);
      convListEl.appendChild(li);
    });
    if (threadTitleEl) {
      const active = conversations.find(function (c) { return c.id === currentId; });
      const title = active
        ? (active.title || T.untitled)
        : (newConversationTitle || T.untitled);
      threadTitleEl.textContent = '';
      writeText(threadTitleEl, title);
    }
  }

  async function loadConversations() {
    const res = await api(cfg.urls.list, { method: 'GET' });
    if (res.ok && res.body) renderConversations(res.body.conversations || []);
  }

  /* Guards against out-of-order responses: two quick clicks in the sidebar can
     resolve backwards, leaving the thread showing one conversation and the
     highlight another. */
  let openToken = 0;

  async function openConversation(id, options) {
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
    renderMessages(res.body.messages || [], options || null);
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
    formEl.classList.toggle('is-busy', state);
    sendBtn.setAttribute('aria-busy', String(state));
    messagesEl.setAttribute('aria-busy', String(state));
    if (state) announce(T.thinking);
    else if (!composerErrorEl || composerErrorEl.hidden) announce('');
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
          id: 'pending', role: 'STUDENT', content: question, status: 'COMPLETED',
        }));
      }
      showThinkingMessage();

      const res = await api(withId(cfg.urls.send, 'CONVERSATION_ID', id), {
        method: 'POST',
        body: JSON.stringify({ message: question, idempotency_key: key }),
      });

      if (res.ok) {
        questionEl.value = '';
        resizeComposer();
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
      const revealMessageId = res.ok && res.body && res.body.assistant_message
        ? res.body.assistant_message.id : null;
      if (currentId === id) await openConversation(id, { revealMessageId: revealMessageId });
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
      setHistoryOpen(false, false);
      renderMessages([]);
      if (threadTitleEl) {
        threadTitleEl.textContent = '';
        writeText(threadTitleEl, newConversationTitle || T.untitled);
      }
      try { window.history.replaceState(null, '', window.location.pathname); } catch (e) { /* ignore */ }
      await loadConversations();
      questionEl.focus();
    });
  }

  /* The timetable card, drawn by the SAME function the thread uses.

     The Telegram channel sends a picture of the proposed timetable, and the
     picture has to be of THIS card — not of a second one drawn server-side. A
     Pillow or matplotlib re-implementation would be a second answer to "what does
     a timetable look like", and this codebase has already paid for that twice
     (the lecture grid duplicated in four places; three cohort classifiers
     disagreeing about " M1"). Exporting the real function means the image cannot
     drift from the screen the student is linked to, and Arabic shaping stays the
     browser's job rather than becoming ours again.

     Exposed only as a render entry point: it takes a presentation object that the
     server has already put through `normalise_presentation`, and reaches nothing
     else. */
  window.__SA_RENDER_TIMETABLE_CARD__ = renderTimetablePresentation;
  window.__SA_RENDER_GRADUATION_CARD__ = renderGraduationPresentation;

  /* A card-only page has no thread, no session and no endpoints to call. Without
     this guard the bootstrap below would fire there, request the conversation
     list unauthenticated, and paint the "could not load" state into the very
     screenshot we are taking. */
  if (cfg.cardOnly) return;

  /* A direct visit to the adviser is a fresh workspace. An existing conversation
     opens only when its id is explicit in the URL (the History drawer writes that
     id when a student selects a thread). This keeps history durable without making
     yesterday's answer look like the starting point of every new visit. */
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
    if (wanted) await openConversation(wanted);
  })();
})();
