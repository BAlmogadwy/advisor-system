"""Everything the gateway itself says, as opposed to what the adviser says.

Separated from the routing so that the wording is reviewable on its own — these
are the only sentences in the feature not written by the adviser, and two of them
are promises about data handling that have to be exactly true.

Arabic first with a short English gloss, matching the student login screen's
«عربي · English» idiom. The adviser's own answers are not touched: it pins its
reply language before it writes a word, and a channel that re-decided that would
be overriding the one component that actually knows.

The privacy text is deliberately unflattering. A Telegram bot chat is a
conversation with a third-party cloud service; saying so plainly is the whole
point of showing it before anything is linked, and calling it "secure" or
"encrypted" would be false in the way that matters — Telegram bot chats are not
end-to-end encrypted, and the operator can read them.
"""

from __future__ import annotations

START = (
    "مرحبًا بك في المرشد الأكاديمي.\n"
    "هذه القناة تتيح لك سؤال المرشد الأكاديمي نفسه الموجود في المنصة، من خلال تيليجرام.\n\n"
    "قبل الإجابة على أي سؤال يخصّ سجلك، يلزم ربط حسابك الجامعي.\n"
    "أرسل /link للبدء.\n\n"
    "الأوامر: /link للربط · /help للمساعدة · /privacy للخصوصية\n"
    "Welcome. Send /link to connect your university account before asking about your record."
)

HELP_UNLINKED = (
    "الأوامر المتاحة الآن:\n"
    "/link — ربط حسابك الجامعي\n"
    "/help — هذه القائمة\n"
    "/privacy — ما الذي يُعالَج وما الذي يُحفظ\n\n"
    "لا يمكن الإجابة على الأسئلة المتعلقة بسجلك قبل الربط.\n"
    "Link your account with /link before asking about your record."
)

HELP_LINKED = (
    "يمكنك سؤالي مباشرةً بالعربية أو بالإنجليزية، دون أوامر.\n"
    "مثال: ما المواد المتبقية لي؟\n\n"
    "الأوامر:\n"
    "/new — بدء محادثة جديدة\n"
    "/advisor — تحويل آخر إجابة إلى مرشد أكاديمي بشري\n"
    "/unlink — إلغاء ربط تيليجرام\n"
    "/privacy — ما الذي يُعالَج وما الذي يُحفظ\n"
    "/help — هذه القائمة\n\n"
    "Ask in Arabic or English. /new starts a fresh conversation, /unlink disconnects."
)

#: Shown on `/privacy` AND on the linking confirmation page. One text, so the
#: promise made before linking and the promise available afterwards cannot drift.
PRIVACY = (
    "الخصوصية في قناة تيليجرام\n\n"
    "• تيليجرام خدمة سحابية خارجية. محادثتك مع هذا البوت ليست مشفّرة طرفًا إلى طرف، "
    "وتمرّ عبر خوادم تيليجرام.\n"
    "• ما يُعالَج: نص سؤالك، ومعرّف حسابك في تيليجرام، لغرض واحد هو الرد عليك تحت هويتك "
    "الجامعية المُتحقَّق منها.\n"
    "• ما تحفظه الجامعة: الربط بين حساب تيليجرام وحسابك الجامعي، وأسئلتك وإجابات المرشد "
    "ضمن سجل محادثاتك نفسه الذي تراه في المنصة.\n"
    "• ما لا يُحفظ: اسمك الظاهر في تيليجرام، ولا اسم المستخدم، ولا رقم الجوال، ولا الصورة "
    "الشخصية.\n"
    "• لا تُستخدم محادثاتك في تدريب أي نموذج.\n"
    "• لإلغاء الربط في أي وقت: /unlink — يُلغى الربط فورًا. أما سجل المحادثات فيبقى في "
    "حسابك الجامعي وفق سياسة الاحتفاظ المعمول بها في المنصة، ولا يحذفه إلغاء الربط.\n\n"
    "Telegram is an external cloud service and this chat is not end-to-end encrypted. "
    "/unlink revokes the connection immediately; your advisor conversation history "
    "remains in your university account under the platform's existing retention policy."
)

#: The one message an unlinked sender gets for anything that is not a supported
#: command. It says nothing about the sender and nothing about any student.
NEEDS_LINK = (
    "لا يمكنني الإجابة على أسئلة تخصّ سجلك الأكاديمي قبل ربط حسابك الجامعي.\n"
    "أرسل /link للربط.\n"
    "Link your university account with /link first."
)


def link_invitation(url: str, minutes: int) -> str:
    """The linking message. Carries a URL and a deadline, and no identifiers.

    States all three steps up front, including the `/confirm` one, so a student who
    is *not* expecting a code does not meet it for the first time on a web page —
    that surprise is what a forwarded-link attack relies on.
    """
    return (
        "لربط حسابك الجامعي:\n"
        "١) افتح الرابط التالي وسجّل الدخول بالطريقة المعتادة.\n"
        "٢) اضغط «أوافق وأربط الحساب»، وسيظهر لك رمز تأكيد.\n"
        "٣) عُد إلى هنا وأرسل: ‎/confirm ثم الرمز.\n\n"
        f"{url}\n\n"
        f"الرابط صالح لمدة {minutes} دقيقة، ويُستخدم مرة واحدة فقط.\n"
        "لا تشارك هذا الرابط ولا الرمز مع أحد.\n\n"
        "قبل أن تربط، اطّلع على /privacy.\n"
        f"Open the link, sign in, approve, then send /confirm <code> back here. "
        f"Valid {minutes} minutes, single use."
    )


CONFIRM_USAGE = (
    "أرسل الرمز هكذا: ‎/confirm ABC123\n"
    "الرمز يظهر في المتصفح بعد تسجيل الدخول والموافقة على الربط.\n"
    "Send /confirm <code> — the code is shown in your browser after you approve."
)

#: One answer for a wrong code, an expired approval, and a chat with nothing
#: awaiting confirmation. Telling them apart would say whether an approval exists
#: for a chat — which is precisely what somebody holding a forwarded link wants.
CONFIRM_INVALID = (
    "الرمز غير صحيح أو انتهت صلاحيته.\n"
    "ابدأ من جديد بإرسال /link.\n"
    "That code is not valid or has expired — send /link to start again."
)


LINK_NOT_CONFIGURED = (
    "خدمة الربط غير مهيّأة حاليًا. يرجى المحاولة لاحقًا أو التواصل مع الدعم.\n"
    "Linking is not configured right now."
)

ALREADY_LINKED = (
    "حسابك مرتبط بالفعل. يمكنك طرح سؤالك مباشرة.\n"
    "لإلغاء الربط: /unlink\n"
    "Your account is already linked — just ask your question."
)

#: Said in the CHAT when /confirm cannot proceed because one side is already
#: linked. Phrased so a student on a new handset is told what to do, rather than
#: being told their chat belongs to somebody else — a sentence that reads as an
#: account compromise when the real cause is an old link they forgot.
STUDENT_ALREADY_LINKED_CHAT = (
    "حسابك الجامعي مرتبط بمحادثة تيليجرام أخرى.\n"
    "ألغِ الربط السابق أولًا من تلك المحادثة أو من المنصة، ثم أعد المحاولة.\n"
    "Your account is linked to another Telegram chat — unlink that one first."
)

CHAT_ALREADY_LINKED_CHAT = (
    "محادثة تيليجرام هذه مرتبطة بحساب طالب آخر.\n"
    "أرسل /unlink أولًا ثم أعد المحاولة.\n"
    "This chat is linked to another student account — send /unlink first."
)

LINK_CONFIRMED = (
    "تم ربط حسابك الجامعي بنجاح. يمكنك الآن طرح أسئلتك مباشرةً بالعربية أو بالإنجليزية.\n"
    "Your university account is now linked."
)

UNLINKED = (
    "تم إلغاء الربط فورًا. لن يتمكّن هذا الحساب في تيليجرام من الوصول إلى سجلك بعد الآن.\n"
    "سجل محادثاتك يبقى في حسابك الجامعي على المنصة.\n"
    "لإعادة الربط لاحقًا: /link\n"
    "Unlinked. Your conversation history remains in your university account."
)

NOT_LINKED_TO_UNLINK = "لا يوجد ربط نشط لهذا الحساب.\nThere is no active link for this chat."

NEW_CONVERSATION = (
    "بدأنا محادثة جديدة. لن أستند إلى ما سبق في هذه المحادثة.\nStarted a fresh conversation."
)

#: Deliberately not "جارٍ إعداد إجابتك" alone — an acknowledgement must not read
#: as a result. It says the question arrived, not that it has been answered.
WORKING = "وصلني سؤالك، وجارٍ إعداد الإجابة… · Received — preparing your answer…"

#: The safe answer when the adviser or the model could not produce one. Arabic
#: first, no exception class, no subsystem name: varying the input and reading
#: back which error came out is a free map of what just broke.
GENERATION_FAILED = (
    "تعذّر إعداد الإجابة الآن. سؤالك محفوظ، ويمكنك إعادة المحاولة بعد قليل.\n"
    "The adviser could not answer just now. Your question was saved."
)

NO_STUDENT_RECORD = (
    "تعذر العثور على سجلك الأكاديمي. يرجى التواصل مع عمادة القبول والتسجيل.\n"
    "Your academic record could not be found."
)

QUESTION_TOO_LONG = (
    "سؤالك طويل جدًا. يرجى اختصاره وإعادة إرساله.\nThat question is too long — please shorten it."
)

UNSUPPORTED_CONTENT = (
    "أستقبل الرسائل النصية فقط في الوقت الحالي. يرجى كتابة سؤالك نصًّا.\n"
    "I can only read text messages for now."
)

UNKNOWN_COMMAND = "أمر غير معروف. أرسل /help لعرض الأوامر المتاحة.\nUnknown command — send /help."


def rate_limited(retry_after_seconds: int) -> str:
    minutes = max(1, round(int(retry_after_seconds or 0) / 60))
    return (
        "لقد أرسلت طلبات كثيرة. يرجى المحاولة بعد نحو "
        f"{minutes} دقيقة.\n"
        "Too many requests — please try again shortly."
    )


ESCALATION_CREATED = (
    "تم تحويل سؤالك إلى مرشد أكاديمي بشري.\n"
    "الرقم المرجعي: {reference}\n"
    "يمكنك متابعة الحالة من المنصة.\n"
    "Escalated to a human adviser."
)

ESCALATION_EXISTS = (
    "هذا السؤال محوَّل بالفعل إلى مرشد أكاديمي.\n"
    "الرقم المرجعي: {reference}\n"
    "This turn is already with a human adviser."
)

ESCALATION_NOT_WARRANTED = (
    "هذه الإجابة لا تحتاج إلى مراجعة المرشد الأكاديمي.\nThis answer does not need adviser review."
)

ESCALATION_NOTHING_TO_ESCALATE = (
    "لا توجد إجابة حديثة يمكن تحويلها. اطرح سؤالك أولًا.\n"
    "There is no recent answer to escalate — ask a question first."
)

__all__ = [
    "ALREADY_LINKED",
    "CHAT_ALREADY_LINKED_CHAT",
    "CONFIRM_INVALID",
    "CONFIRM_USAGE",
    "ESCALATION_CREATED",
    "ESCALATION_EXISTS",
    "ESCALATION_NOTHING_TO_ESCALATE",
    "ESCALATION_NOT_WARRANTED",
    "GENERATION_FAILED",
    "HELP_LINKED",
    "HELP_UNLINKED",
    "LINK_CONFIRMED",
    "LINK_NOT_CONFIGURED",
    "NEEDS_LINK",
    "NEW_CONVERSATION",
    "NOT_LINKED_TO_UNLINK",
    "NO_STUDENT_RECORD",
    "PRIVACY",
    "QUESTION_TOO_LONG",
    "START",
    "STUDENT_ALREADY_LINKED_CHAT",
    "UNKNOWN_COMMAND",
    "UNLINKED",
    "UNSUPPORTED_CONTENT",
    "WORKING",
    "link_invitation",
    "rate_limited",
]
