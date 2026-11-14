"""Every word staff read about timetable feedback, in English and Arabic.

The student's own words live in `timetable_feedback_copy`; these are the staff
side's. One closed vocabulary again: each status, reason, event, refusal and
notice is an entry here, so a code added later shows up as a missing entry in a
test, not as English on an Arabic page.

ARABIC STYLE. Formal Arabic in the portal's glossary (docs/i18n_glossary_ar.md):
«المرشد»، «معرّف المرشد»، «الطالب»، «البرنامج»، «القسم»، «الشعبة». Buttons and
labels are verbal nouns («إرسال الرد»، «الإحالة إلى القسم»), and a sentence
addressed to the member of staff avoids a gendered imperative («يُرجى» with a
verbal noun). «الطالب» is the role noun, as everywhere else on the staff pages. A
comment for colleagues is «تعليق», never «ملاحظة», which is the student's whole
feedback.

Codes, references, ids and dates are isolated left-to-right by the caller with
`copy.bdi()`, never inside these strings.
"""

from __future__ import annotations

from collections.abc import Mapping

from django.utils.html import format_html
from django.utils.safestring import SafeString

from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_staff as staff_service

#: The student copy's own renderer: it escapes a string with no placeholders
#: directly, because a bare format_html() is deprecated and an error in Django 6.
_render = copy._render

STRINGS: Mapping[str, tuple[str, str]] = {
    # The sidebar and the queue
    "nav": ("Timetable feedback", "ملاحظات الطلاب على الجداول"),
    "nav_count": ("Waiting on you: {count}", "بانتظار إجرائك: {count}"),
    "queue_title": ("Timetable feedback from students", "ملاحظات الطلاب على الجداول"),
    "queue_intro": (
        "Feedback students sent about the expected timetable they were shown. Advisors see"
        " their own students' cases and any the department assigns them; the department sees"
        " every case in its programmes.",
        "ملاحظات أرسلها الطلاب على الجدول المتوقع المعروض لهم. يرى المرشد حالات طلابه وما"
        " يسنده إليه القسم، ويرى القسم جميع حالات برامجه.",
    ),
    "guide_link": ("How to handle timetable feedback", "دليل التعامل مع ملاحظات الطلاب"),
    "tabs_label": ("Groups of cases", "مجموعات الحالات"),
    "tab_action": ("Waiting on you", "بانتظار إجرائك"),
    "tab_open": ("All open", "جميع الحالات المفتوحة"),
    "tab_student": ("Waiting for the student", "بانتظار الطالب"),
    "tab_department": ("With the department", "لدى القسم"),
    "tab_closed": ("Closed", "المغلقة"),
    "tab_all": ("All", "الكل"),
    "filters_title": ("Filter the cases", "تصفية الحالات"),
    "filter_search": ("Student ID or reference", "الرقم الجامعي أو الرقم المرجعي"),
    "filter_reason": ("Issue", "المشكلة"),
    "filter_programme": ("Programme", "البرنامج"),
    "filter_registered": ("Registration recorded", "التسجيل مسجّل"),
    "filter_any": ("Any", "الكل"),
    "filter_yes": ("Yes", "نعم"),
    "filter_no": ("No", "لا"),
    "filter_new": (
        "Only where the student wrote since staff last acted",
        "ما كتب فيه الطالب بعد آخر إجراء من الموظفين فقط",
    ),
    "filter_graduating": ("Only students expected to graduate", "المتوقع تخرجهم فقط"),
    "filter_unrouted": (
        "Only students with no advisor account",
        "الطلاب الذين ليس لمرشدهم حساب فقط",
    ),
    "filter_apply": ("Apply", "تطبيق"),
    "filter_clear": ("Clear the filters", "مسح عوامل التصفية"),
    "col_select": ("Select", "تحديد"),
    "col_reference": ("Reference", "الرقم المرجعي"),
    "col_days": ("Days open", "عدد الأيام"),
    "col_student": ("Student", "الطالب"),
    "col_points": ("What was raised", "ما أُثير"),
    "col_status": ("Status", "الحالة"),
    "col_handler": ("Handled by", "يتابعها"),
    "col_activity": ("Last activity", "آخر نشاط"),
    "select_case": ("Select {reference}", "تحديد {reference}"),
    "select_all": ("Select every case on this page", "تحديد جميع حالات هذه الصفحة"),
    "nobody": ("Nobody yet", "لا أحد بعد"),
    "marker_new": ("The student wrote", "كتب الطالب"),
    "marker_unrouted": ("No advisor account", "لا حساب للمرشد"),
    "marker_registered": ("Registration recorded", "التسجيل مسجّل"),
    "marker_graduating": ("Graduation expected", "تخرج متوقع"),
    "student_unavailable": ("Student record unavailable", "سجل الطالب غير متاح"),
    "more_points": ("and {count} more", "و{count} أخرى"),
    "empty_tab": ("No cases here.", "لا توجد حالات هنا."),
    "empty_NO_ADVISOR_ID": (
        "Your account has no advisor id, so no student's case can reach you. Please contact"
        " the system administrator.",
        "لا يحمل حسابك معرّف مرشد، لذا لا تصل إليك حالات الطلاب. يُرجى التواصل مع مدير النظام.",
    ),
    "empty_NO_DEPARTMENTS": (
        "No programmes are set on your account, so no case can reach you. Please contact the"
        " system administrator.",
        "لم تُحدَّد أي برامج لحسابك، لذا لا تصل إليك أي حالة. يُرجى التواصل مع مدير النظام.",
    ),
    "pages_label": ("Pages", "الصفحات"),
    "page_of": ("Page {page} of {pages}", "الصفحة {page} من {pages}"),
    "page_previous": ("Previous page", "الصفحة السابقة"),
    "page_next": ("Next page", "الصفحة التالية"),
    # Many cases at once
    "bulk_title": ("With the selected cases", "إجراء على الحالات المحددة"),
    "bulk_hint": (
        "Each case is answered or closed on its own. A case that changed since this page was"
        " drawn is skipped and listed. At most {max} at once.",
        "يُجرى الإجراء على كل حالة على حدة، وتُتخطى الحالة التي تغيّرت منذ عرض هذه الصفحة"
        " وتُذكر في النتيجة. الحد الأقصى في المرة الواحدة: {max}.",
    ),
    "bulk_template": ("Ready-made answer", "الرد الجاهز"),
    "bulk_answer": ("Answer the selected cases", "الرد على الحالات المحددة"),
    "bulk_close_note": ("Comment for colleagues", "تعليق للزملاء"),
    "bulk_close": ("Close the selected cases as duplicates", "إغلاق الحالات المحددة لتكرارها"),
    "bulk_result_title": ("What was done", "ما تم تنفيذه"),
    "bulk_done": ("Done: {count}", "تم تنفيذها: {count}"),
    "bulk_skipped": ("Skipped: {count}", "تم تخطيها: {count}"),
    "bulk_errors_title": ("Nothing was changed", "لم يُجرَ أي تغيير"),
    "back_to_queue": ("Back to the cases", "العودة إلى الحالات"),
    # One case
    "case_title": ("Feedback {reference}", "الملاحظة {reference}"),
    "case_facts": ("About this case", "بيانات الحالة"),
    "case_student": ("Student", "الطالب"),
    "case_programme": ("Programme", "البرنامج"),
    "case_cohort": ("Cohort", "الفئة"),
    "cohort_M": ("Male — M", "طلاب — M"),
    "cohort_F": ("Female — F", "طالبات — F"),
    "case_standing": ("Standing", "الوضع الأكاديمي"),
    "case_term": ("Term", "الفصل الدراسي"),
    "case_sent": ("Sent", "تاريخ الإرسال"),
    "case_status": ("Status", "الحالة"),
    "case_language": ("Wrote in", "لغة الكتابة"),
    "language_ar": ("Arabic", "العربية"),
    "language_en": ("English", "الإنجليزية"),
    "case_registered": ("Registration recorded when sent", "التسجيل مسجّل عند الإرسال"),
    "case_handler": ("Handled by", "يتابعها"),
    "case_advisor_now": ("Student's advisor id now", "معرّف مرشد الطالب حاليًا"),
    "case_advisor_then": ("Advisor id when sent", "معرّف المرشد عند الإرسال"),
    "case_assigned": ("Assigned by the department to", "أسندها القسم إلى"),
    "case_none": ("None", "لا يوجد"),
    "case_frozen_note": (
        "The student record is unavailable, so the programme, cohort and standing are shown"
        " as they were when the feedback was sent.",
        "سجل الطالب غير متاح، لذا يُعرض البرنامج والفئة والوضع الأكاديمي كما كانت عند"
        " إرسال الملاحظة.",
    ),
    "case_visible_to": (
        "Accounts holding advisor id {advisor}: {count}. Each of them can open this case, as"
        " can the department for the student's programme and the system administrators.",
        "عدد الحسابات التي تحمل معرّف المرشد {advisor}: {count}. ويمكن لكل منها فتح هذه"
        " الحالة، وكذلك القسم المسؤول عن برنامج الطالب ومديرو النظام.",
    ),
    "case_unrouted": (
        "No active account holds the student's advisor id, so this case is the department's"
        " until it is assigned to an advisor.",
        "لا يوجد حساب نشط يحمل معرّف مرشد الطالب، لذا تبقى هذه الحالة لدى القسم إلى أن تُسند"
        " إلى مرشد.",
    ),
    "case_record_link": ("The student's graduation report", "تقرير تخرج الطالب"),
    "points_title": ("What the student sent", "ما أرسله الطالب"),
    "point_whole_timetable": ("The expected timetable as a whole", "الجدول المتوقع كاملًا"),
    "point_no_forecast": ("No expected timetable was prepared", "لم يُعدّ جدول متوقع"),
    "point_section": ("{course}, section {section}", "{course}، الشعبة {section}"),
    "point_seen": ("As the student was shown it:", "كما ظهرت للطالب:"),
    "point_issue": ("Issue", "المشكلة"),
    "point_requested_section": ("Section asked for", "الشعبة المطلوبة"),
    "point_requested_course": ("Missing course", "المقرر غير الموجود"),
    "point_note": ("Details", "التفاصيل"),
    "point_sent": ("Sent", "تاريخ الإرسال"),
    "point_withdrawn": ("Withdrawn by the student", "سحبها الطالب"),
    "point_changed": (
        "The student's expected timetable has changed since this was sent.",
        "تغيّر الجدول المتوقع للطالب منذ إرسال هذه النقطة.",
    ),
    "point_note_redacted": (
        "Words were removed from these details.",
        "حُذفت كلمات من هذه التفاصيل.",
    ),
    "current_title": ("The student's expected timetable now", "الجدول المتوقع للطالب حاليًا"),
    "current_other_term": (
        "This case is about term {term}, which is no longer the term being planned, so only"
        " what the student sent is shown.",
        "تخص هذه الحالة الفصل الدراسي {term}، ولم يعد هو الفصل الجاري التخطيط له، لذا يُعرض"
        " ما أرسله الطالب فقط.",
    ),
    "current_unavailable": (
        "The student's timetable could not be read just now.",
        "تعذّرت قراءة جدول الطالب حاليًا.",
    ),
    "current_empty": ("No expected timetable now.", "لا يوجد جدول متوقع حاليًا."),
    "current_registered": (
        "The registration for this term is recorded.",
        "التسجيل في هذا الفصل الدراسي مسجّل.",
    ),
    "thread_title": ("Messages", "الرسائل"),
    "thread_empty": ("No messages yet.", "لا توجد رسائل بعد."),
    "thread_internal": (
        "Staff only, never shown to the student",
        "للموظفين فقط، ولا تظهر للطالب",
    ),
    "thread_shown_as": ("Shown to the student as: {label}", "تظهر للطالب باسم: {label}"),
    "thread_from_student": ("The student", "الطالب"),
    "message_redacted": ("Words were removed from this message.", "حُذفت كلمات من هذه الرسالة."),
    "redact_link": ("Remove words", "حذف كلمات"),
    "redact_link_named": ("Remove words from {what}", "حذف كلمات من {what}"),
    # Between items of a list, and from one value to the next, in the page's direction.
    "list_separator": (", ", "، "),
    "change_arrow": ("→", "←"),
    "history_title": ("History", "السجل"),
    "history_student": ("The student", "الطالب"),
    "history_system": ("The system", "النظام"),
    "history_empty": ("Nothing yet.", "لا شيء بعد."),
    "actions_title": ("What can be done", "الإجراءات المتاحة"),
    "actions_none": (
        "Nothing more can be done on this case.",
        "لا يوجد إجراء آخر متاح على هذه الحالة.",
    ),
    "language_hint": (
        "The student wrote in {name}; please answer in it.",
        "كتب الطالب باللغة {name}، ويُرجى الرد بها.",
    ),
    "text_hint": ("At most {max} characters.", "الحد الأقصى لعدد الأحرف: {max}."),
    "action_take": ("Take this case", "تولّي هذه الحالة"),
    "action_take_over": ("Take this case over", "تولّي هذه الحالة بدلًا من متابعها الحالي"),
    "action_ask_title": ("Ask the student", "سؤال الطالب"),
    "action_ask_label": ("Your question to the student", "السؤال الموجّه إلى الطالب"),
    "action_ask": ("Send the question", "إرسال السؤال"),
    "action_answer_title": ("Answer the student", "الرد على الطالب"),
    "action_answer_label": ("Your answer to the student", "الرد الموجّه إلى الطالب"),
    "action_template_label": (
        "Or send a ready-made answer instead",
        "أو إرسال رد جاهز بدلًا من ذلك",
    ),
    "action_template_hint": (
        "Used only when no answer is typed, in the language the student wrote in.",
        "يُستخدم فقط عند عدم كتابة رد، وباللغة التي كتب بها الطالب.",
    ),
    "template_none": ("No ready-made answer", "بدون رد جاهز"),
    "action_answer": ("Send the answer", "إرسال الرد"),
    "action_forward_title": ("Forward to the department", "الإحالة إلى القسم"),
    "action_forward_label": (
        "Why the department should handle it (staff only)",
        "سبب الإحالة إلى القسم (للموظفين فقط)",
    ),
    "action_forward": ("Forward to the department", "الإحالة إلى القسم"),
    "action_close_title": ("Close the case", "إغلاق الحالة"),
    "action_close_reason": ("Reason for closing", "سبب الإغلاق"),
    "action_close_label": ("Comment for colleagues (staff only)", "تعليق للزملاء (للموظفين فقط)"),
    "action_close": ("Close the case", "إغلاق الحالة"),
    "action_note_title": ("Comment for colleagues", "تعليق للزملاء"),
    "action_note_label": ("Comment (staff only)", "التعليق (للموظفين فقط)"),
    "action_note": ("Add the comment", "إضافة التعليق"),
    "action_assign_title": ("Assign to an advisor", "الإسناد إلى مرشد"),
    "action_assign_label": ("Advisor id", "معرّف المرشد"),
    "action_assign_hint": (
        "The advisor holding this id will see the case and can take it.",
        "سيرى المرشد الذي يحمل هذا المعرّف الحالة ويمكنه تولّيها.",
    ),
    "action_assign": ("Assign the case", "إسناد الحالة"),
    "errors_title": ("Nothing was changed", "لم يُجرَ أي تغيير"),
    "changed_by": (
        "Last changed by {name} on {time}.",
        "آخر من غيّرها: {name}، بتاريخ {time}.",
    ),
    # Removing words
    "redact_title": ("Remove words from {reference}", "حذف كلمات من {reference}"),
    "redact_intro": (
        "Copy each passage to remove exactly as it appears, one per line. Each is replaced"
        " with {marker} everywhere it appears, the rest stays as it is, and the removed words"
        " are not kept anywhere.",
        "يُرجى نسخ كل جزء مراد حذفه كما يظهر تمامًا، جزءًا في كل سطر. ويُستبدل كل جزء"
        " بالعلامة {marker} أينما ظهر، ويبقى الباقي كما هو، ولا تُحفظ الكلمات المحذوفة في أي"
        " مكان.",
    ),
    "redact_current": ("The text now", "النص الحالي"),
    "redact_label": ("Words to remove", "الكلمات المراد حذفها"),
    "redact_submit": ("Remove these words", "حذف هذه الكلمات"),
    "redact_cancel": ("Back to the case", "العودة إلى الحالة"),
    "redact_point": ("the details of a point", "تفاصيل نقطة"),
    "redact_message": ("a message", "رسالة"),
    # Pages that are not there
    "unavailable_title": ("Timetable feedback is not available", "ملاحظات الطلاب غير متاحة"),
    "unavailable_body": (
        "This page is not available. Timetable feedback may be switched off.",
        "هذه الصفحة غير متاحة، وربما أُوقفت ميزة ملاحظات الطلاب على الجداول.",
    ),
    # The guide
    "guide_title": ("Handling timetable feedback", "دليل التعامل مع ملاحظات الطلاب على الجداول"),
    "guide_intro": (
        "Students send feedback on the expected timetable they are shown. Sending it never"
        " changes a registration or a timetable: it reaches the student's academic advisor,"
        " who answers it and passes on what the department should decide.",
        "يرسل الطلاب ملاحظاتهم على الجدول المتوقع المعروض لهم. ولا يغيّر إرسالها أي تسجيل أو"
        " جدول، بل تصل إلى المرشد الأكاديمي للطالب، فيرد عليها ويحيل إلى القسم ما يلزم أن"
        " يقرره.",
    ),
    "guide_steps_title": ("Working on a case", "العمل على الحالة"),
    "guide_reasons_title": ("By issue", "حسب المشكلة"),
    "guide_rules_title": ("Rules that always hold", "قواعد ثابتة"),
}

#: The guide's steps: (title, text) pairs in each language.
GUIDE_STEPS: tuple[tuple[tuple[str, str], tuple[str, str]], ...] = (
    (
        ("Take it", "تولّي الحالة"),
        (
            "Take a new case, so that colleagues who share your advisor id see you are on it.",
            "يُرجى تولّي الحالة الجديدة ليعلم الزملاء الذين يشاركونك معرّف المرشد أنها في عهدتك.",
        ),
    ),
    (
        ("Ask", "السؤال"),
        (
            "Ask when something is missing. The case then waits for the student, whose reply"
            " comes back to whoever asked.",
            "يُرجى السؤال عند نقص أي معلومة، فتنتظر الحالة رد الطالب، ويعود الرد إلى من سأل.",
        ),
    ),
    (
        ("Answer", "الرد"),
        (
            "Answer in the language the student wrote in. The student can accept the answer,"
            " or reopen the case within {days} if it did not help.",
            "يُرجى الرد باللغة التي كتب بها الطالب. ويمكن للطالب قبول الرد، أو إعادة فتح الحالة"
            " خلال {days} إذا لم يكن الرد كافيًا.",
        ),
    ),
    (
        ("Forward", "الإحالة"),
        (
            "Forward what only the department can decide, such as a section that should open"
            " or move, and say why in the comment. The student sees only that the department"
            " has it.",
            "يُرجى إحالة ما لا يقرره إلا القسم، مثل فتح شعبة أو نقلها، مع ذكر السبب في التعليق،"
            " ولا يرى الطالب إلا أن الحالة لدى القسم.",
        ),
    ),
    (
        ("Close", "الإغلاق"),
        (
            "Close a duplicate, or something the timetable cannot change, with a comment for"
            " colleagues. The student sees that the case was closed, and why.",
            "يُرجى إغلاق الحالة المكررة، أو ما لا يمكن تغييره في الجدول، مع تعليق للزملاء،"
            " ويرى الطالب أن الحالة أُغلقت وسبب إغلاقها.",
        ),
    ),
)

#: The rules every member of staff should know.
GUIDE_RULES: tuple[tuple[str, str], ...] = (
    (
        "Comments for colleagues are never shown to the student.",
        "لا تظهر تعليقات الزملاء للطالب أبدًا.",
    ),
    (
        'A message to the student shows as from "your academic advisor" when you are their'
        ' advisor and from "the department" otherwise, never with your name.',
        "تظهر الرسالة للطالب على أنها من «مرشدك الأكاديمي» إذا كنت مرشده، ومن «القسم» في غير"
        " ذلك، ولا يظهر اسمك أبدًا.",
    ),
    (
        "Nothing here changes a registration or a timetable. Registration changes are made in"
        " the university portal.",
        "لا يغيّر أي إجراء هنا تسجيلًا أو جدولًا، وتُجرى تعديلات التسجيل عبر بوابة الجامعة.",
    ),
    (
        "A case belongs to the student's current advisor. When the student moves to another"
        " advisor, the case moves too.",
        "تتبع الحالة المرشد الحالي للطالب، فإذا انتقل الطالب إلى مرشد آخر انتقلت الحالة معه.",
    ),
    (
        "A student whose advisor has no account is the department's, which answers or assigns"
        " the case.",
        "حالات الطلاب الذين ليس لمرشدهم حساب تبقى لدى القسم، فيرد عليها أو يسندها إلى مرشد.",
    ),
)

#: What each issue usually calls for. Advice, not a rule.
GUIDE_REASONS: Mapping[str, tuple[str, str]] = {
    "TIME_CLASH": (
        "Check the clash in the expected timetable. Forward it when a section has to change.",
        "يُرجى التحقق من التعارض في الجدول المتوقع، وإحالته إلى القسم إذا لزم تغيير شعبة.",
    ),
    "TIME_UNSUITABLE": (
        "Usually answered: explain what can and cannot change. Forward a pattern you see"
        " across students.",
        "يُكتفى غالبًا بالرد مع توضيح ما يمكن تغييره وما لا يمكن، وتُحال إلى القسم إذا تكررت"
        " لدى عدد من الطلاب.",
    ),
    "PREFER_OTHER_SECTION": (
        "Look at the section asked for. Forward when the department has to move the student.",
        "يُرجى الاطلاع على الشعبة المطلوبة، والإحالة إلى القسم إذا لزم نقل الطالب.",
    ),
    "ALREADY_PASSED": (
        "Check the record. If the course is passed, the expected timetable needs correcting:"
        " forward it.",
        "يُرجى التحقق من السجل الأكاديمي، فإذا كان المقرر مجتازًا فالجدول المتوقع يحتاج إلى"
        " تصحيح، وتُحال الحالة إلى القسم.",
    ),
    "PREREQUISITE_NOT_MET": (
        "Check the prerequisite in the record, then answer or forward.",
        "يُرجى التحقق من المتطلب السابق في السجل الأكاديمي، ثم الرد أو الإحالة.",
    ),
    "NOT_IN_MY_PLAN": (
        "Check the study plan. A course outside it needs the department.",
        "يُرجى التحقق من الخطة الدراسية، والمقرر خارجها يحتاج إلى القسم.",
    ),
    "SECTION_HAS_NO_TIMES": (
        "Times are set by the department: forward it.",
        "يحدد القسم المواعيد، لذا تُحال الحالة إليه.",
    ),
    "LOAD_TOO_HIGH": (
        "Answer with the load rules that apply; forward an exception.",
        "يُرجى الرد بقواعد العبء الدراسي المعمول بها، وإحالة أي استثناء إلى القسم.",
    ),
    "LOAD_TOO_LOW": (
        "Check what the student still needs; forward a missing section.",
        "يُرجى التحقق مما يحتاجه الطالب، وإحالة أي شعبة ناقصة إلى القسم.",
    ),
    "MISSING_COURSE": (
        "Check the course asked for against the plan and the sections offered.",
        "يُرجى مطابقة المقرر المطلوب مع الخطة الدراسية والشعب المطروحة.",
    ),
    "TOO_MANY_CAMPUS_DAYS": (
        "Usually answered; the days follow the sections offered.",
        "يُكتفى غالبًا بالرد، إذ تتبع أيام الحضور الشعب المطروحة.",
    ),
    "GRADUATION_CONCERN": (
        "Check the graduation report first, and forward anything that puts graduation at risk.",
        "يُرجى مراجعة تقرير التخرج أولًا، وإحالة كل ما يهدد التخرج إلى القسم.",
    ),
    "OTHER": ("Read the details, then decide.", "يُرجى قراءة التفاصيل ثم اتخاذ القرار."),
    "NO_FORECAST": (
        "No expected timetable was prepared for the student: the department's to handle.",
        "لم يُعدّ للطالب جدول متوقع، ويتولى القسم معالجة ذلك.",
    ),
}

#: A case's status, in the workflow's own terms.
STATUS: Mapping[str, tuple[str, str]] = {
    "SUBMITTED": ("New", "جديدة"),
    "IN_REVIEW": ("In review", "قيد المراجعة"),
    "NEEDS_INFORMATION": ("Waiting for the student's reply", "بانتظار رد الطالب"),
    "FORWARDED": ("With the department", "لدى القسم"),
    "ANSWERED": ("Answered", "تم الرد"),
    "CLOSED": ("Closed", "مغلقة"),
    "WITHDRAWN": ("Withdrawn by the student", "سحبها الطالب"),
}

#: The issue a student picked, said about them rather than in their voice.
REASON: Mapping[str, tuple[str, str]] = {
    "TIME_CLASH": ("Clashes with another course", "يتعارض مع مقرر آخر"),
    "TIME_UNSUITABLE": ("The time does not suit the student", "الوقت لا يناسب الطالب"),
    "PREFER_OTHER_SECTION": ("Needs a different section", "يحتاج إلى شعبة أخرى"),
    "ALREADY_PASSED": ("Course already passed", "المقرر مجتاز سابقًا"),
    "PREREQUISITE_NOT_MET": ("Prerequisite not met", "المتطلب السابق غير مستوفى"),
    "NOT_IN_MY_PLAN": ("Not in the study plan", "ليس ضمن الخطة الدراسية"),
    "SECTION_HAS_NO_TIMES": ("Section has no meeting times", "الشعبة بلا مواعيد"),
    "LOAD_TOO_HIGH": ("Too many credit hours", "الساعات المعتمدة أكثر من اللازم"),
    "LOAD_TOO_LOW": ("Too few credit hours", "الساعات المعتمدة أقل من اللازم"),
    "MISSING_COURSE": ("A needed course is missing", "ينقصه مقرر مطلوب"),
    "TOO_MANY_CAMPUS_DAYS": ("Too many days on campus", "أيام الحضور كثيرة"),
    "GRADUATION_CONCERN": ("May delay graduation", "قد يؤخر التخرج"),
    "OTHER": ("Something else", "أمر آخر"),
    "NO_FORECAST": ("No expected timetable prepared", "لم يُعدّ جدول متوقع"),
}

#: Why a case was closed.
CLOSE_REASON: Mapping[str, tuple[str, str]] = {
    "DUPLICATE": ("Duplicate of another case", "مكررة مع حالة أخرى"),
    "NOT_APPLICABLE": (
        "Not something the timetable can change",
        "لا تتعلق بما يمكن تغييره في الجدول",
    ),
    "OTHER": ("Another reason", "سبب آخر"),
    "TERM_ENDED": ("The term ended", "انتهى الفصل الدراسي"),
    "ANSWER_ACCEPTED": ("The student accepted the answer", "قبِل الطالب الرد"),
    "NO_REPLY_AFTER_ANSWER": ("No reply after the answer", "لم يرد الطالب بعد الرد"),
}

#: Each entry of a case's history.
EVENT: Mapping[str, tuple[str, str]] = {
    "SUBMITTED": ("Sent by the student", "أرسلها الطالب"),
    "POINT_ADDED": ("Point added by the student", "أضاف الطالب نقطة"),
    "POINT_WITHDRAWN": ("Point withdrawn by the student", "سحب الطالب نقطة"),
    "VIEWED": ("Opened", "فتح الحالة"),
    "TAKEN": ("Took the case", "تولّي الحالة"),
    "ASSIGNED": ("Assigned the case", "إسناد الحالة"),
    "STATUS_CHANGED": ("Changed the status", "تغيير الحالة"),
    "NOTE_ADDED": ("Added a comment for colleagues", "إضافة تعليق للزملاء"),
    "STUDENT_REPLY": ("Reply from the student", "رد من الطالب"),
    "STUDENT_FOLLOWUP": ("Message from the student", "رسالة من الطالب"),
    "STAFF_MESSAGE": ("Message to the student", "رسالة إلى الطالب"),
    "FORWARDED": ("Forwarded to the department", "الإحالة إلى القسم"),
    "DEPARTMENT_DECISION": ("Department decision", "قرار القسم"),
    "ASSESSED": ("Checked against the current timetable", "المطابقة مع الجدول الحالي"),
    "REDACTED": ("Removed words", "حذف كلمات"),
    "ANSWER_CONFIRMED": ("Answer accepted by the student", "قبِل الطالب الرد"),
    "REOPENED": ("Reopened by the student", "أعاد الطالب فتح الحالة"),
    "WITHDRAWN": ("Withdrawn by the student", "سحب الطالب الحالة"),
    "AUTO_CLOSED": ("Closed automatically", "أُغلقت تلقائيًا"),
}

#: What happened after an action, shown once on the case page.
NOTICE: Mapping[str, tuple[str, str]] = {
    "TAKEN": ("You now handle this case.", "أصبحت هذه الحالة في عهدتك."),
    "NOTE_ADDED": ("The comment was added for colleagues.", "أُضيف التعليق للزملاء."),
    "ASKED": ("The question was sent to the student.", "أُرسل السؤال إلى الطالب."),
    "ANSWERED": ("The answer was sent to the student.", "أُرسل الرد إلى الطالب."),
    "FORWARDED": ("The case was forwarded to the department.", "أُحيلت الحالة إلى القسم."),
    "CLOSED": ("The case was closed.", "أُغلقت الحالة."),
    "ASSIGNED": (
        "The case was assigned to advisor id {advisor}.",
        "أُسندت الحالة إلى المرشد ذي المعرّف {advisor}.",
    ),
    "REDACTED": ("The words were removed.", "حُذفت الكلمات."),
}

#: Every refusal the staff side can give, by code.
REFUSAL: Mapping[str, tuple[str, str]] = {
    "CASE_NOT_FOUND": (
        "This case was not found, or it is not one you can open.",
        "لم يُعثر على هذه الحالة، أو أنها خارج نطاق صلاحياتك.",
    ),
    "CASE_CHANGED": (
        "This case changed after the page was drawn, so nothing was changed. Please read"
        " it again before acting.",
        "تغيّرت هذه الحالة بعد عرض الصفحة، فلم يُجرَ أي تغيير. يُرجى مراجعتها مجددًا قبل"
        " اتخاذ أي إجراء.",
    ),
    "NOT_ALLOWED": (
        "This cannot be done to the case in its current status.",
        "لا يمكن تنفيذ هذا الإجراء على الحالة في وضعها الحالي.",
    ),
    "ALREADY_YOURS": ("You already handle this case.", "هذه الحالة في عهدتك بالفعل."),
    "HANDLED_BY_OTHER": (
        "Someone else handles this case, so it was left to them. Open it to act on it.",
        "يتابع هذه الحالة شخص آخر، فتُركت له. ويُرجى فتحها لاتخاذ أي إجراء عليها.",
    ),
    "BODY_REQUIRED": ("Please write the text.", "يُرجى كتابة النص."),
    "BODY_TOO_LONG": (
        "The text is too long. The most it can hold is {max} characters.",
        "النص أطول من المسموح، والحد الأقصى لعدد الأحرف: {max}.",
    ),
    "BODY_INVALID": (
        "The text contains characters that cannot be saved. Please remove them and try again.",
        "يحتوي النص على رموز لا يمكن حفظها. يُرجى حذفها والمحاولة مجددًا.",
    ),
    "CLOSE_REASON_REQUIRED": (
        "Please choose why the case is closed.",
        "يُرجى اختيار سبب الإغلاق.",
    ),
    "DEPARTMENT_ONLY": ("Only the department can do this.", "هذا الإجراء متاح للقسم فقط."),
    "UNKNOWN_ADVISOR": (
        "No active account holds that advisor id.",
        "لا يوجد حساب نشط يحمل معرّف المرشد هذا.",
    ),
    "ALREADY_ASSIGNED": (
        "The case is already assigned to that advisor id.",
        "الحالة مسندة بالفعل إلى معرّف المرشد هذا.",
    ),
    "TEMPLATE_UNKNOWN": (
        "Please choose one of the ready-made answers.",
        "يُرجى اختيار أحد الردود الجاهزة.",
    ),
    "REDACT_NOTHING": (
        "Please type the words to remove.",
        "يُرجى كتابة الكلمات المراد حذفها.",
    ),
    "REDACT_TEXT_NOT_FOUND": (
        "Some of those words are not in the text. Please copy them exactly as they appear.",
        "بعض هذه الكلمات غير موجودة في النص. يُرجى نسخها كما تظهر تمامًا.",
    ),
    "REDACT_TARGET_NOT_FOUND": (
        "That text was not found in this case.",
        "لم يُعثر على هذا النص في هذه الحالة.",
    ),
    "BULK_NOTHING": ("Please select at least one case.", "يُرجى تحديد حالة واحدة على الأقل."),
    "BULK_TOO_MANY": (
        "At most {max} cases can be changed at once.",
        "الحد الأقصى لعدد الحالات التي يمكن تغييرها دفعة واحدة: {max}.",
    ),
}

#: For a code no entry covers yet, with the code shown so it can be reported.
UNKNOWN_REFUSAL = (
    "Something went wrong and nothing was changed.",
    "حدث خطأ ولم يُجرَ أي تغيير.",
)

#: The ready-made answers, by what they say, for the staff who pick one.
TEMPLATE_NAME: Mapping[str, tuple[str, str]] = {
    "REGISTERED_IN_PORTAL": (
        "Registration already recorded: changes are made in the portal",
        "التسجيل مسجّل: تُجرى التعديلات عبر البوابة",
    ),
    "NOTED_FOR_PLANNING": (
        "Noted for when the timetables are prepared",
        "سُجّلت للأخذ بها عند إعداد الجداول",
    ),
    "NO_CHANGE_THIS_TERM": (
        "Cannot be changed this term",
        "لا يمكن تغييرها في هذا الفصل الدراسي",
    ),
}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _pick(pair: tuple[str, str], language: str) -> str:
    return pair[1] if language == "ar" else pair[0]


def strings(language: str) -> dict[str, str]:
    """Every page string without placeholders, in ``language``, for templates."""
    return {key: _pick(pair, language) for key, pair in STRINGS.items() if "{" not in pair[0]}


def text(key: str, language: str, **params: object) -> SafeString:
    """One page string, with its placeholders filled and escaped."""
    return _render(_pick(STRINGS[key], language), params)


def fill(pair: tuple[str, str], language: str, **params: object) -> SafeString:
    """Any (English, Arabic) pair of this module, picked and filled like `text`."""
    return _render(_pick(pair, language), params)


def plain(key: str, language: str, **params: object) -> str:
    """One page string as plain text, for a <title>."""
    return _pick(STRINGS[key], language).format(**{k: str(v) for k, v in params.items()})


def status_label(code: str, language: str) -> str:
    return _pick(STATUS.get(code, (code, code)), language)


def reason_label(code: str, language: str) -> str:
    return _pick(REASON.get(code, (code, code)), language)


def close_reason_label(code: str, language: str) -> str:
    return _pick(CLOSE_REASON.get(code, CLOSE_REASON["OTHER"]), language)


def event_label(code: str, language: str) -> str:
    return _pick(EVENT.get(code, (code, code)), language)


def template_name(key: str, language: str) -> str:
    return _pick(TEMPLATE_NAME[key], language)


def notice(code: str, language: str, **params: object) -> SafeString:
    return _render(_pick(NOTICE[code], language), params)


def refusal(code: str, language: str) -> SafeString:
    """The sentence for a refusal code, its numbers taken from the service."""
    pair = REFUSAL.get(code)
    if pair is None:
        return format_html("{} {}", _pick(UNKNOWN_REFUSAL, language), copy.bdi(f"({code})"))
    params: dict[str, object] = {}
    if code == "BODY_TOO_LONG":
        params["max"] = copy.bdi(staff_service.STAFF_TEXT_MAX_CHARS)
    if code == "BULK_TOO_MANY":
        params["max"] = copy.bdi(staff_service.BULK_MAX)
    return _render(_pick(pair, language), params)
