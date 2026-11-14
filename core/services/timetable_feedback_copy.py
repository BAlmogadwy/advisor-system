"""Every word a student reads about timetable feedback, in English and Arabic.

ONE CLOSED VOCABULARY. The services speak in codes: a status, a reason, a close
reason, a refusal. This module turns each code into a sentence. Nothing here is
derived from an English model label or from a service's own string, so a code
the services add later shows up as a missing entry in a test, not as English in
an Arabic page.

ARABIC STYLE. Formal Arabic, in the portal's glossary: «الجدول المتوقع», «الجدول
المسجّل فعليًا», «مرشدك الأكاديمي», «الشعبة», «الفصل الدراسي». The wording does
not depend on whether a student is male or female. Requests are «يُرجى» with a
verbal noun rather than an imperative, statements use the passive or a noun
phrase, and the issues a student picks are written in their own first-person
voice («الوقت لا يناسبني»). Counted nouns go through `arabic_count`, or the
house "label: N" form.

Codes, references, terms and times are isolated left-to-right by the caller
with `bdi()` (or `term_html()`), never inside these strings, so a sentence
reads in the page's own direction around them.
"""

from __future__ import annotations

from collections.abc import Mapping

from django.conf import settings
from django.utils.html import format_html
from django.utils.safestring import SafeString

# The services import nothing from this module, so taking their limits from them
# keeps every number here single-sourced without an import cycle.
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_workflow as workflow
from core.services.student_home_cards import arabic_count

LANGUAGES = ("en", "ar")

# ---------------------------------------------------------------------------
# Words on the pages
# ---------------------------------------------------------------------------

#: Page wording by key: (English, Arabic). Placeholders are {name}; their values
#: are escaped unless the caller passes markup it built with `bdi()`.
STRINGS: Mapping[str, tuple[str, str]] = {
    # Navigation and the list
    "nav": ("My timetable feedback", "ملاحظاتي على الجدول"),
    "list_title": ("My timetable feedback", "ملاحظاتي على الجدول"),
    "list_intro": (
        "Feedback you have sent to your academic advisor about your expected timetable.",
        "الملاحظات المرسلة منك إلى مرشدك الأكاديمي على الجدول المتوقع.",
    ),
    "list_empty": (
        "You have not sent any timetable feedback yet.",
        "لا توجد ملاحظات مرسلة منك على الجدول حتى الآن.",
    ),
    "list_new": ("Send new feedback", "إرسال ملاحظة جديدة"),
    "list_reference": ("Reference", "الرقم المرجعي"),
    "list_term": ("Term", "الفصل الدراسي"),
    "list_status": ("Status", "الحالة"),
    "list_sent": ("Sent", "تاريخ الإرسال"),
    "list_points": ("Points: {count}", "عدد النقاط: {count}"),
    "list_open": ("View", "عرض"),
    # The home page
    "home_send": (
        "Send feedback to my academic advisor",
        "إرسال ملاحظة إلى مرشدي الأكاديمي",
    ),
    "home_view_case": ("View my feedback {reference}", "عرض ملاحظتي {reference}"),
    "home_case_status": ("Status: {status}", "الحالة: {status}"),
    "home_add_point": ("Add another point", "إضافة نقطة أخرى"),
    "home_all_feedback": ("All my timetable feedback", "جميع ملاحظاتي على الجدول"),
    "registered_notice": (
        "Your registration for term {term} is already recorded, and changes to it are made"
        " in the university portal. Feedback still reaches your academic advisor and helps"
        " the next plan.",
        "سبق تسجيلك في الفصل الدراسي {term}، وتُجرى أي تعديلات على التسجيل عبر بوابة"
        " الجامعة. ومع ذلك تصل ملاحظتك إلى مرشدك الأكاديمي وتُسهم في إعداد الخطة القادمة.",
    ),
    "no_forecast_title": ("No expected timetable yet", "لا يوجد جدول متوقع حتى الآن"),
    "no_forecast_body": (
        "No expected timetable has been prepared for you for term {term} yet. Your academic"
        " advisor can be told about it.",
        "لم يُعدّ لك جدول متوقع للفصل الدراسي {term} حتى الآن، ويمكن إبلاغ مرشدك الأكاديمي بذلك.",
    ),
    "no_forecast_button": ("Let my academic advisor know", "إبلاغ مرشدي الأكاديمي"),
    # The form, step one: what is it about
    "new_title": (
        "Send feedback on your expected timetable",
        "إرسال ملاحظة على الجدول المتوقع",
    ),
    "new_intro": (
        "Your feedback goes to your academic advisor, who reads it and replies here.",
        "تصل ملاحظتك إلى مرشدك الأكاديمي، فيطّلع عليها ويرد عليك هنا.",
    ),
    "new_what_legend": ("What is your feedback about?", "ما موضوع ملاحظتك؟"),
    "new_whole_timetable": ("My expected timetable as a whole", "الجدول المتوقع كاملًا"),
    "new_whole_timetable_hint": (
        "For example: too many credit hours, too many days on campus, or a course from"
        " your plan that is missing.",
        "مثل: كثرة الساعات المعتمدة، أو كثرة أيام الحضور إلى الجامعة، أو عدم وجود مقرر"
        " مطلوب في خطتك.",
    ),
    "new_continue": ("Continue", "متابعة"),
    "new_clash_hint": (
        "Its time overlaps another section in this timetable",
        "يتداخل وقتها مع شعبة أخرى في هذا الجدول",
    ),
    "new_no_times": ("No meeting times yet", "لا توجد مواعيد لها بعد"),
    "new_what_changes_title": (
        "What feedback can and cannot do",
        "ما يمكن أن تحققه الملاحظة وما لا يمكن",
    ),
    "new_what_changes_body": (
        "Your academic advisor and the department read your feedback and can take it into"
        " account when timetables are prepared. Sending it does not change your registration"
        " or your expected timetable by itself; registration is done in the university portal.",
        "يطّلع مرشدك الأكاديمي والقسم على ملاحظتك، ويمكن أخذها في الحسبان عند إعداد"
        " الجداول. ولا يغيّر إرسالها تسجيلك أو الجدول المتوقع بحد ذاته، إذ يتم التسجيل عبر"
        " بوابة الجامعة.",
    ),
    "new_only_timetabled": (
        "Only courses that have a place in the timetable are listed; projects and courses"
        " without a section do not appear.",
        "تظهر هنا المقررات التي لها موعد في الجدول فقط، ولا تظهر المشاريع ولا المقررات التي"
        " ليس لها شعبة.",
    ),
    # The form, step two: what is the issue
    "new_about_section": (
        "Feedback on {course}, section {section}",
        "ملاحظة على المقرر {course}، الشعبة {section}",
    ),
    "new_about_timetable": (
        "Feedback on your expected timetable as a whole",
        "ملاحظة على الجدول المتوقع كاملًا",
    ),
    "new_change_choice": ("Choose something else", "اختيار موضوع آخر"),
    "section_word": ("Section", "الشعبة"),
    "list_separator": (", ", "، "),
    "new_reason_legend": ("What is the issue?", "ما المشكلة؟"),
    "new_requested_legend": (
        "Which section would suit you better?",
        "ما الشعبة الأنسب لك؟",
    ),
    "new_requested_hint": (
        "Used only for a clash, an unsuitable time or needing another section. Other"
        " sections of the same course are listed.",
        "تُستخدم فقط عند اختيار التعارض أو عدم مناسبة الوقت أو الحاجة إلى شعبة أخرى، وتظهر"
        " هنا الشعب الأخرى للمقرر نفسه.",
    ),
    "new_requested_none": ("No particular section", "لا شعبة محددة"),
    "new_requested_empty": (
        "No other section of this course is listed for this term.",
        "لا توجد شعبة أخرى لهذا المقرر في هذا الفصل الدراسي.",
    ),
    "new_missing_legend": ("Which course is missing?", "ما المقرر غير الموجود؟"),
    "new_missing_hint": (
        "Used only when a course is missing. Courses from your study plan that are not yet"
        " passed and have a section this term are listed.",
        "يُستخدم فقط عند اختيار عدم وجود مقرر، وتظهر هنا مقررات خطتك الدراسية غير المجتازة"
        " التي لها شعبة في هذا الفصل الدراسي.",
    ),
    "new_missing_none": ("No course chosen", "لم يُختر مقرر"),
    "new_missing_empty": (
        "No course from your plan can be listed here this term.",
        "لا يوجد مقرر من خطتك يمكن عرضه هنا في هذا الفصل الدراسي.",
    ),
    "new_note_label": ("Your note", "ملاحظتك"),
    "new_note_hint": (
        'Optional, except when the issue is "Something else". At most {max} characters.',
        "اختيارية، إلا عند اختيار «أمر آخر». الحد الأقصى لعدد الأحرف: {max}.",
    ),
    "new_privacy": (
        "Your academic advisor and the department can read this. Please do not include"
        " health or family details.",
        "يطّلع على ملاحظتك مرشدك الأكاديمي والقسم. يُرجى عدم ذكر أي تفاصيل صحية أو عائلية.",
    ),
    "new_submit": ("Send to my academic advisor", "إرسال إلى مرشدي الأكاديمي"),
    "new_errors_title": ("Your feedback was not sent", "لم تُرسل ملاحظتك"),
    "new_about_required": (
        "Please choose what your feedback is about.",
        "يُرجى اختيار موضوع الملاحظة.",
    ),
    "new_reason_required": ("Please choose the issue.", "يُرجى اختيار المشكلة."),
    "new_requested_required": (
        "Please choose the section you need.",
        "يُرجى اختيار الشعبة المطلوبة.",
    ),
    "new_missing_required": (
        "Please choose the course that is missing.",
        "يُرجى اختيار المقرر غير الموجود.",
    ),
    "new_open_case": ("Open feedback {reference}", "فتح الملاحظة {reference}"),
    # The form when there is no expected timetable
    "new_no_forecast_title": (
        "No expected timetable has been prepared for you",
        "لم يُعدّ لك جدول متوقع",
    ),
    "new_no_forecast_body": (
        "There is no expected timetable for you for term {term}. Your academic advisor can"
        " be told, to look into it.",
        "لا يوجد جدول متوقع لك للفصل الدراسي {term}، ويمكن إبلاغ مرشدك الأكاديمي ليتابع ذلك.",
    ),
    "new_no_forecast_submit": ("Tell my academic advisor", "إبلاغ مرشدي الأكاديمي"),
    "new_registered_only": (
        "No expected timetable has been prepared for you for term {term}. Your registered"
        " timetable is managed in the university portal, so there is no feedback to send here.",
        "لم يُعدّ لك جدول متوقع للفصل الدراسي {term}. ويُدار الجدول المسجّل فعليًا عبر بوابة"
        " الجامعة، لذا لا توجد هنا ملاحظة يمكن إرسالها.",
    ),
    # One case
    "detail_title": ("Feedback {reference}", "الملاحظة {reference}"),
    "detail_term": ("Term", "الفصل الدراسي"),
    "detail_status": ("Status", "الحالة"),
    "detail_sent": ("Sent", "تاريخ الإرسال"),
    "detail_points_title": ("What you sent", "ما أرسلته"),
    "detail_whole_timetable": ("Your expected timetable as a whole", "الجدول المتوقع كاملًا"),
    "detail_no_forecast": ("No expected timetable was prepared", "لم يُعدّ جدول متوقع"),
    "detail_section": ("{course}, section {section}", "{course}، الشعبة {section}"),
    "detail_reason": ("Issue", "المشكلة"),
    "detail_requested_section": ("A section that would suit you better", "الشعبة الأنسب"),
    "detail_requested_course": ("Missing course", "المقرر غير الموجود"),
    "detail_note": ("Your note", "ملاحظتك"),
    "detail_note_removed": ("Part of this note was removed.", "حُذف جزء من هذه الملاحظة."),
    "detail_withdrawn": ("Withdrawn", "مسحوبة"),
    "detail_seen": (
        "As it was in your expected timetable:",
        "كما ظهرت في الجدول المتوقع:",
    ),
    "detail_thread_title": ("Messages", "الرسائل"),
    "detail_thread_empty": (
        "No messages yet. Your academic advisor will reply here.",
        "لا توجد رسائل بعد، وسيرد مرشدك الأكاديمي هنا.",
    ),
    "detail_message_corrected": ("This message was corrected.", "صُحّحت هذه الرسالة."),
    "detail_message_removed": ("Part of this message was removed.", "حُذف جزء من هذه الرسالة."),
    "detail_other_term": (
        "This feedback is about term {term}, which is no longer the term being planned, so"
        " it is shown as it was sent.",
        "تخص هذه الملاحظة الفصل الدراسي {term}، ولم يعد هو الفصل الجاري التخطيط له، لذا"
        " تُعرض كما أُرسلت.",
    ),
    "detail_add_point": ("Add another point", "إضافة نقطة أخرى"),
    "detail_back": ("All my timetable feedback", "جميع ملاحظاتي على الجدول"),
    # Pages that are not there, or not open
    "unavailable_title": (
        "Timetable feedback is not available",
        "الملاحظات على الجدول غير متاحة",
    ),
    "unavailable_body": (
        "This page is not available. Timetable feedback may be closed.",
        "هذه الصفحة غير متاحة، وربما أُغلق استقبال الملاحظات على الجدول.",
    ),
    "back_home": ("Back to my academic record", "العودة إلى ملخصي الأكاديمي"),
}

# ---------------------------------------------------------------------------
# Words for the services' codes
# ---------------------------------------------------------------------------

#: A case's status, in words a student understands rather than workflow names.
STATUS: Mapping[str, tuple[str, str]] = {
    "SUBMITTED": ("Sent", "مُرسلة"),
    "IN_REVIEW": ("Being reviewed", "قيد المراجعة"),
    "NEEDS_INFORMATION": ("Waiting for your reply", "بانتظار ردك"),
    "FORWARDED": ("With the department", "لدى القسم"),
    "ANSWERED": ("Answered", "تم الرد"),
    "CLOSED": ("Closed", "مغلقة"),
    "WITHDRAWN": ("Withdrawn", "مسحوبة"),
}

#: What a status means for the student, shown under it.
STATUS_EXPLAINED: Mapping[str, tuple[str, str]] = {
    "SUBMITTED": (
        "Your academic advisor will read it and reply here.",
        "سيطّلع عليها مرشدك الأكاديمي ويرد عليك هنا.",
    ),
    "IN_REVIEW": (
        "Your academic advisor is looking at it.",
        "يراجعها مرشدك الأكاديمي حاليًا.",
    ),
    "NEEDS_INFORMATION": (
        "Your academic advisor has asked for more information; see the messages below.",
        "طلب مرشدك الأكاديمي مزيدًا من المعلومات؛ يُرجى الاطلاع على الرسائل أدناه.",
    ),
    "FORWARDED": (
        "Your academic advisor has passed it to the department.",
        "أحال مرشدك الأكاديمي الملاحظة إلى القسم.",
    ),
    "ANSWERED": (
        "Your academic advisor has answered; see the messages below.",
        "رد مرشدك الأكاديمي؛ يُرجى الاطلاع على الرسائل أدناه.",
    ),
    "CLOSED": ("This feedback is closed.", "أُغلقت هذه الملاحظة."),
    "WITHDRAWN": ("This feedback was withdrawn.", "سُحبت هذه الملاحظة."),
}

#: The issues a student picks, in their own first-person voice.
REASON: Mapping[str, tuple[str, str]] = {
    "TIME_CLASH": ("It clashes with another course", "يتعارض مع مقرر آخر"),
    "TIME_UNSUITABLE": ("The time does not suit me", "الوقت لا يناسبني"),
    "PREFER_OTHER_SECTION": ("I need a different section", "أحتاج إلى شعبة أخرى"),
    "ALREADY_PASSED": ("I have already passed this course", "سبق لي اجتياز هذا المقرر"),
    "PREREQUISITE_NOT_MET": ("I have not met its prerequisite", "لم أستوفِ متطلبه السابق"),
    "NOT_IN_MY_PLAN": ("It is not in my study plan", "ليس ضمن خطتي الدراسية"),
    "SECTION_HAS_NO_TIMES": ("The section has no meeting times", "لا توجد مواعيد لهذه الشعبة"),
    "LOAD_TOO_HIGH": ("Too many credit hours", "الساعات المعتمدة أكثر من اللازم"),
    "LOAD_TOO_LOW": ("Too few credit hours", "الساعات المعتمدة أقل من اللازم"),
    "MISSING_COURSE": ("A course I need is missing", "ينقصه مقرر أحتاج إليه"),
    "TOO_MANY_CAMPUS_DAYS": ("Too many days on campus", "أيام الحضور إلى الجامعة كثيرة"),
    "GRADUATION_CONCERN": ("It may delay my graduation", "قد يؤخر تخرجي"),
    "OTHER": ("Something else", "أمر آخر"),
    "NO_FORECAST": (
        "No expected timetable has been prepared for me",
        "لم يُعدّ لي جدول متوقع",
    ),
}

#: Why a case was closed, as the student reads it.
CLOSE_REASON: Mapping[str, tuple[str, str]] = {
    "DUPLICATE": ("Closed as a repeat of other feedback", "أُغلقت لتكرارها مع ملاحظة أخرى"),
    "NOT_APPLICABLE": (
        "Closed: it is not something the timetable can change",
        "أُغلقت لأن موضوعها لا يتعلق بما يمكن تغييره في الجدول",
    ),
    "TERM_ENDED": ("Closed because the term ended", "أُغلقت بانتهاء الفصل الدراسي"),
    "ANSWER_ACCEPTED": ("Closed after the answer was accepted", "أُغلقت بعد قبول الرد"),
    "NO_REPLY_AFTER_ANSWER": (
        "Closed after the answer, with no further reply",
        "أُغلقت بعد الرد لعدم ورود رد آخر",
    ),
    "OTHER": ("Closed", "أُغلقت"),
}

#: Who wrote a message the student can see.
AUTHOR: Mapping[str, tuple[str, str]] = {
    "STUDENT": ("You", "أنت"),
    "ADVISOR": ("Your academic advisor", "مرشدك الأكاديمي"),
    "DEPARTMENT": ("The department", "القسم"),
}

#: Day codes as the home page writes them.
DAY: Mapping[str, tuple[str, str]] = {
    "SUN": ("Sunday", "الأحد"),
    "MON": ("Monday", "الاثنين"),
    "TUE": ("Tuesday", "الثلاثاء"),
    "WED": ("Wednesday", "الأربعاء"),
    "THU": ("Thursday", "الخميس"),
    "FRI": ("Friday", "الجمعة"),
    "SAT": ("Saturday", "السبت"),
}

#: What happened after a point was sent, shown once on the case page.
NOTICE: Mapping[str, tuple[str, str]] = {
    "SENT": (
        "Sent to your academic advisor. The reference is {reference}.",
        "أُرسلت ملاحظتك إلى مرشدك الأكاديمي، والرقم المرجعي: {reference}.",
    ),
    "DUPLICATE": (
        "This point was already sent, so nothing new was added. The reference is {reference}.",
        "سبق إرسال هذه النقطة، فلم تُضف نقطة جديدة. الرقم المرجعي: {reference}.",
    ),
    "REOPENED": (
        "Sent. Your new point reopened this feedback, so your academic advisor will look at"
        " it again.",
        "أُرسلت النقطة، وأعادت فتح هذه الملاحظة ليطّلع عليها مرشدك الأكاديمي مجددًا.",
    ),
}

#: Every refusal the feedback services can give, by code. Messages name no section
#: and no other student's case: NOT_OFFERED and CASE_NOT_FOUND read the same
#: whatever the reason, so the form cannot be used to probe.
REFUSAL: Mapping[str, tuple[str, str]] = {
    "RATE_LIMITED": (
        "Too many attempts in a short time. Please try again in {wait}.",
        "عدد المحاولات كبير خلال وقت قصير. يُرجى المحاولة مجددًا بعد {wait}.",
    ),
    "UNKNOWN_STUDENT": (
        "We could not find your student record. Please contact your academic advisor.",
        "تعذّر العثور على سجلك الطلابي. يُرجى التواصل مع مرشدك الأكاديمي.",
    ),
    "NOT_ACCEPTING": (
        "Feedback on the expected timetable is not being accepted at the moment.",
        "لا يجري استقبال الملاحظات على الجدول المتوقع في الوقت الحالي.",
    ),
    "TIMETABLE_UNAVAILABLE": (
        "We could not load your expected timetable just now. Please try again later.",
        "تعذّر تحميل الجدول المتوقع حاليًا. يُرجى المحاولة مرة أخرى لاحقًا.",
    ),
    "TIMETABLE_CHANGED": (
        "Your expected timetable changed while you were writing. Please check it below"
        " before sending.",
        "تغيّر الجدول المتوقع أثناء كتابة الملاحظة. يُرجى مراجعته أدناه قبل الإرسال.",
    ),
    "UNKNOWN_ANCHOR": (
        "That item is no longer in your expected timetable. Please choose again.",
        "لم يعد هذا البند ضمن الجدول المتوقع. يُرجى الاختيار مجددًا.",
    ),
    "REASON_DOES_NOT_FIT": (
        "Please choose one of the issues listed.",
        "يُرجى اختيار إحدى المشكلات المعروضة.",
    ),
    "NOTE_REQUIRED": (
        "Please describe the issue in your note.",
        "يُرجى توضيح المشكلة في الملاحظة.",
    ),
    "NOTE_TOO_LONG": (
        "Your note is too long. The most it can hold is {max} characters.",
        "الملاحظة أطول من المسموح، والحد الأقصى لعدد الأحرف: {max}.",
    ),
    "NOTE_INVALID": (
        "Your note contains characters that cannot be saved. Please remove them and try again.",
        "تحتوي الملاحظة على رموز لا يمكن حفظها. يُرجى حذفها والمحاولة مجددًا.",
    ),
    "NOT_OFFERED": (
        "That choice is not available for this feedback. Please choose again.",
        "هذا الاختيار غير متاح لهذه الملاحظة. يُرجى الاختيار مجددًا.",
    ),
    "NO_FORECAST_NOT_ALLOWED": (
        "You have a timetable for this term, so please send feedback about it instead.",
        "لديك جدول لهذا الفصل الدراسي، فيُرجى إرسال الملاحظة عليه.",
    ),
    "TOO_MANY_CASES": (
        "You have reached the most feedback allowed for this term ({max}).",
        "بلغت ملاحظاتك في هذا الفصل الدراسي الحد الأقصى المسموح به: {max}.",
    ),
    "COOLING_DOWN": (
        "Feedback was withdrawn recently. New feedback can be sent {hours} after a withdrawal.",
        "سُحبت ملاحظة لك مؤخرًا، ويمكن إرسال ملاحظة جديدة بعد مرور {hours} على السحب.",
    ),
    "ALREADY_RAISED": (
        "You already sent this point in feedback {reference}. To add more to it, open that"
        " feedback.",
        "سبق إرسال هذه النقطة ضمن الملاحظة {reference}. ولإضافة أي تفاصيل يُرجى فتح تلك الملاحظة.",
    ),
    "TOO_MANY_POINTS": (
        "This feedback already holds the most points allowed ({max}).",
        "تحتوي هذه الملاحظة على الحد الأقصى لعدد النقاط: {max}.",
    ),
    "CASE_CHANGED": (
        "Your feedback was changed at the same moment. Please try again.",
        "تغيّرت ملاحظتك في اللحظة نفسها. يُرجى المحاولة مجددًا.",
    ),
    "NOT_ALLOWED": (
        "This cannot be done for your feedback at the moment.",
        "لا يمكن تنفيذ هذا الإجراء على ملاحظتك في الوقت الحالي.",
    ),
    "BODY_REQUIRED": ("Please write your message.", "يُرجى كتابة رسالتك."),
    "BODY_TOO_LONG": (
        "Your message is too long. The most it can hold is {max} characters.",
        "الرسالة أطول من المسموح، والحد الأقصى لعدد الأحرف: {max}.",
    ),
    "BODY_INVALID": (
        "Your message contains characters that cannot be saved. Please remove them and try again.",
        "تحتوي الرسالة على رموز لا يمكن حفظها. يُرجى حذفها والمحاولة مجددًا.",
    ),
    "CASE_NOT_FOUND": ("We could not find that feedback.", "تعذّر العثور على هذه الملاحظة."),
    "POINT_NOT_FOUND": ("We could not find that point.", "تعذّر العثور على هذه النقطة."),
    "LAST_POINT": (
        "This is the only point left. To take it back, withdraw the whole feedback instead.",
        "هذه هي النقطة الوحيدة المتبقية، ولسحبها يُرجى سحب الملاحظة كاملة.",
    ),
    "TOO_MANY_FOLLOW_UPS": (
        "This feedback already holds the most follow-ups allowed ({max}).",
        "بلغت الإضافات على هذه الملاحظة الحد الأقصى المسموح به: {max}.",
    ),
    "REOPEN_WINDOW_PASSED": (
        "More than {days} have passed since the answer, so this feedback can no longer be"
        " reopened. New feedback can be sent instead.",
        "مضى على الرد أكثر من {days}، فلم يعد بالإمكان إعادة فتح هذه الملاحظة. ويمكن إرسال"
        " ملاحظة جديدة بدلًا من ذلك.",
    ),
}

#: For a code no entry covers yet: a plain apology, with the code shown so it
#: can be reported.
UNKNOWN_REFUSAL = (
    "Something went wrong and your feedback was not saved. Please try again.",
    "حدث خطأ ولم تُحفظ ملاحظتك. يُرجى المحاولة مجددًا.",
)

# Agreeing nouns for counted time, for `arabic_count`.
_SECONDS = {
    "zero": "ثوانٍ",
    "one": "ثانية واحدة",
    "two": "ثانيتين",
    "few": "ثوانٍ",
    "many": "ثانية",
    "other": "ثانية",
}
_MINUTES = {
    "zero": "دقائق",
    "one": "دقيقة واحدة",
    "two": "دقيقتين",
    "few": "دقائق",
    "many": "دقيقة",
    "other": "دقيقة",
}
_HOURS = {
    "zero": "ساعات",
    "one": "ساعة واحدة",
    "two": "ساعتين",
    "few": "ساعات",
    "many": "ساعة",
    "other": "ساعة",
}
_DAYS = {
    "zero": "أيام",
    "one": "يوم واحد",
    "two": "يومين",
    "few": "أيام",
    "many": "يومًا",
    "other": "يوم",
}

# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def language_of(code: object) -> str:
    """'ar' for any Arabic language code, 'en' for everything else."""
    return "ar" if str(code or "").lower().startswith("ar") else "en"


def _pick(pair: tuple[str, str], language: str) -> str:
    return pair[1] if language == "ar" else pair[0]


def bdi(value: object) -> SafeString:
    """``value`` isolated left-to-right: for codes, references, sections and times."""
    return format_html('<bdi dir="ltr">{}</bdi>', value)


def term_html(academic_year: object, term: object) -> SafeString:
    """A term as the portal writes it, ``1448/1``, isolated left-to-right."""
    return bdi(f"{academic_year}/{term}")


def strings(language: str) -> dict[str, str]:
    """Every page string without placeholders, in ``language``, for templates.

    Strings with placeholders are left out: the view fills those with `text()` so a
    template can never print an unfilled ``{reference}``.
    """
    return {key: _pick(pair, language) for key, pair in STRINGS.items() if "{" not in pair[0]}


def text(key: str, language: str, **params: object) -> SafeString:
    """One page string, with its placeholders filled and escaped."""
    return format_html(_pick(STRINGS[key], language), **params)


def status_label(code: str, language: str) -> str:
    return _pick(STATUS.get(code, (code, code)), language)


def status_explained(code: str, language: str) -> str:
    return _pick(STATUS_EXPLAINED.get(code, ("", "")), language)


def reason_label(code: str, language: str) -> str:
    return _pick(REASON.get(code, (code, code)), language)


def close_reason_label(code: str, language: str) -> str:
    return _pick(CLOSE_REASON.get(code, CLOSE_REASON["OTHER"]), language)


def author_label(kind: str, language: str) -> str:
    """Who wrote a message. Anything that is not the student reads as the department
    unless it is plainly the advisor, so no staff name or role code reaches a student."""
    return _pick(AUTHOR.get(kind, AUTHOR["DEPARTMENT"]), language)


def day_label(code: str, language: str) -> str:
    return _pick(DAY.get(code, (code, code)), language)


def notice(code: str, language: str, *, reference: str) -> SafeString:
    return format_html(_pick(NOTICE[code], language), reference=bdi(reference))


def wait_text(seconds: int | float | None, language: str) -> str:
    """A wait in the largest unit that reads naturally: seconds under 90, minutes
    under 90, hours after that. Always at least one of its unit, rounded up."""
    total = max(1, int(seconds or 0))
    if total < 90:
        n, english, forms = total, "second", _SECONDS
    elif total < 90 * 60:
        n, english, forms = -(-total // 60), "minute", _MINUTES
    else:
        n, english, forms = -(-total // 3600), "hour", _HOURS
    if language == "ar":
        return arabic_count(n, forms)
    return f"{n} {english}" + ("" if n == 1 else "s")


def count_text(n: int, unit: str, language: str) -> str:
    """``n`` days or hours, agreeing in Arabic."""
    forms = {"day": _DAYS, "hour": _HOURS}[unit]
    if language == "ar":
        return arabic_count(n, forms)
    return f"{n} {unit}" + ("" if n == 1 else "s")


def refusal(
    code: str,
    language: str,
    *,
    reference: str = "",
    retry_after: int = 0,
) -> SafeString:
    """The sentence for a refusal code, its numbers taken from the services' own
    limits so the copy cannot drift from what is enforced."""
    pair = REFUSAL.get(code)
    if pair is None:
        return format_html("{} {}", _pick(UNKNOWN_REFUSAL, language), bdi(f"({code})"))
    limits = {
        "NOTE_TOO_LONG": feedback.NOTE_MAX_CHARS,
        "BODY_TOO_LONG": workflow.STUDENT_TEXT_MAX_CHARS,
        "TOO_MANY_CASES": feedback.MAX_CASES_PER_TERM,
        "TOO_MANY_POINTS": feedback.MAX_LIVE_POINTS,
        "TOO_MANY_FOLLOW_UPS": workflow.MAX_FOLLOW_UPS,
    }
    params: dict[str, object] = {}
    if code in limits:
        params["max"] = bdi(limits[code])
    if code == "RATE_LIMITED":
        params["wait"] = wait_text(retry_after, language)
    if code == "ALREADY_RAISED":
        params["reference"] = bdi(reference)
    if code == "COOLING_DOWN":
        hours = int(feedback.COOL_DOWN_AFTER_WITHDRAWAL.total_seconds() // 3600)
        params["hours"] = count_text(hours, "hour", language)
    if code == "REOPEN_WINDOW_PASSED":
        params["days"] = count_text(
            int(settings.TIMETABLE_FEEDBACK_ANSWER_REOPEN_DAYS), "day", language
        )
    return format_html(_pick(pair, language), **params)
