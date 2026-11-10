from typing import Any

from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.html import escape

from core.models import (
    AcademicAdvisor,
    AuditLog,
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    UserScope,
)
from core.services.programme_meeting_patterns import programme_pattern_label
from core.services.timetable_meeting_shape import PROGRAMME_NOT_SET

#: The fields a weekly-meetings vote depends on: the course it names
#: (programme, code, name), whether it may vote (type) and the credit hours the
#: database requires of it (ck_pr_meeting_pattern_fits_credits).
VOTE_BOUND_FIELDS = ("program", "course_code", "course_name", "type", "credit_hours")


def _is_arabic(request: HttpRequest) -> bool:
    return str(getattr(request, "LANGUAGE_CODE", "") or "").lower().startswith("ar")


def _holds_vote(obj: ProgrammeRequirement | None) -> bool:
    return obj is not None and (obj.meeting_pattern or PROGRAMME_NOT_SET) != PROGRAMME_NOT_SET


def _delete_selected_keeping_votes(
    modeladmin: "ProgrammeRequirementAdmin", request: HttpRequest, queryset: QuerySet[Any]
) -> Any:
    """Django's "Delete selected", minus the rows holding a weekly-meetings vote.

    Filtered before Django's own action runs, so its confirmation page, its
    admin log and its "Successfully deleted N" count only the rows it deletes.
    """
    held = queryset.exclude(meeting_pattern=PROGRAMME_NOT_SET)
    if held.exists():
        modeladmin.say_votes_kept(request, held)
        queryset = queryset.filter(meeting_pattern=PROGRAMME_NOT_SET)
        if not queryset.exists():
            return None
    return delete_selected(modeladmin, request, queryset)


@admin.register(ProgrammeRequirement)
class ProgrammeRequirementAdmin(admin.ModelAdmin):
    """A programme's study-plan rows, with its weekly-meetings votes protected.

    ``meeting_pattern`` (the programme's vote on how a 3-credit course meets
    each week) is written only by Section Planning, which previews and audits
    every change. Here it is read-only. A row holding a vote also keeps the
    fields the vote depends on, and cannot be deleted: a credit change would
    meet the database's CHECK as a server error, a rename would hand the vote
    to another course and a delete would lose it with no audit. Clearing the
    vote in Section Planning unlocks the row.
    """

    def get_readonly_fields(
        self, request: HttpRequest, obj: ProgrammeRequirement | None = None
    ) -> tuple[str, ...]:
        if _holds_vote(obj):
            return ("meeting_pattern", *VOTE_BOUND_FIELDS)
        return ("meeting_pattern",)

    def get_fieldsets(self, request: HttpRequest, obj: ProgrammeRequirement | None = None) -> Any:
        fieldsets = super().get_fieldsets(request, obj)
        if not _holds_vote(obj) or obj is None:
            return fieldsets
        arabic = _is_arabic(request)
        label = programme_pattern_label(obj.meeting_pattern, obj.credit_hours, arabic=arabic)
        if arabic:
            note = (
                f"لهذا المقرر لقاءات أسبوعية حدّدها برنامجه في تخطيط الشعب ({label}). "
                "البرنامج والرمز والاسم والنوع والساعات المعتمدة مقفلة هنا، ولا يمكن "
                "حذف الصف: امسح لقاءاته الأسبوعية في تخطيط الشعب أولاً."
            )
        else:
            note = (
                f"This course has weekly meetings set for its programme in Section Planning "
                f"({label}). Its programme, code, name, type and credit hours are locked "
                "here and the row cannot be deleted: clear its weekly meetings in Section "
                "Planning first."
            )
        # The admin prints a fieldset description unescaped, so escape it here.
        (name, options), *rest = fieldsets
        return [(name, {**options, "description": escape(note)}), *rest]

    def has_delete_permission(
        self, request: HttpRequest, obj: ProgrammeRequirement | None = None
    ) -> bool:
        if _holds_vote(obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request: HttpRequest) -> dict[str, Any]:
        # Django has already kept "Delete selected" only for users allowed to
        # delete; this swaps in the version that leaves votes alone.
        actions = super().get_actions(request)
        if "delete_selected" in actions:
            _func, name, description = actions["delete_selected"]
            actions["delete_selected"] = (_delete_selected_keeping_votes, name, description)
        return actions

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        # The bulk action already leaves these rows out; this covers a vote set
        # between its confirmation and the delete, and any other caller.
        held = queryset.exclude(meeting_pattern=PROGRAMME_NOT_SET)
        if held.exists():
            self.say_votes_kept(request, held)
            queryset = queryset.filter(meeting_pattern=PROGRAMME_NOT_SET)
        super().delete_queryset(request, queryset)

    def say_votes_kept(self, request: HttpRequest, held: QuerySet[Any]) -> None:
        rows = [
            f"{program} {code}"
            for program, code in held.order_by("program", "course_code").values_list(
                "program", "course_code"
            )
        ]
        if _is_arabic(request):
            text = (
                f"لم يُحذف {len(rows)} صف لأن لها لقاءات أسبوعية محددة في تخطيط الشعب: "
                f"{'، '.join(rows)}. امسح لقاءاتها الأسبوعية هناك أولاً."
            )
        else:
            text = (
                f"Kept {len(rows)} row(s) that have weekly meetings set in Section Planning: "
                f"{', '.join(rows)}. Clear their weekly meetings there first."
            )
        self.message_user(request, text, messages.WARNING)


admin.site.register(Student)
admin.site.register(Course)
admin.site.register(StudentCourse)
admin.site.register(Prerequisite)
admin.site.register(AcademicAdvisor)
admin.site.register(TermSection)
admin.site.register(TermSectionMeeting)
admin.site.register(StudentTermSection)
admin.site.register(UserScope)
admin.site.register(AuditLog)
