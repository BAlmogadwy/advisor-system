from django.conf import settings
from django.db import models


class Student(models.Model):
    student_id = models.IntegerField(primary_key=True)
    registration_no = models.TextField(blank=True, default="")
    name = models.TextField(blank=True, default="")
    nationality = models.TextField(blank=True, default="")
    status = models.TextField(blank=True, default="")
    gpa = models.FloatField(null=True, blank=True)
    total_registered_credits = models.IntegerField(null=True, default=0)
    total_earned_credits = models.IntegerField(null=True, default=0)
    current_registered_credits = models.IntegerField(null=True, default=0)
    program = models.TextField(null=True, blank=True)  # noqa: DJ001
    section = models.TextField(blank=True, default="")
    advisor_id = models.TextField(blank=True, default="")

    class Meta:
        db_table = "students"
        indexes = [
            models.Index(fields=["program"], name="idx_students_program"),
            models.Index(fields=["advisor_id"], name="idx_students_advisor_id"),
            models.Index(fields=["section"], name="idx_students_section"),
        ]

    def __str__(self) -> str:
        return f"Student({self.student_id})"


class Course(models.Model):
    course_id = models.AutoField(primary_key=True)
    course_code = models.TextField(unique=True)
    department = models.TextField(blank=True, default="")
    description = models.TextField(blank=True, default="")
    credit_hours = models.IntegerField(null=True, default=0)
    is_external = models.BooleanField(default=False)

    class Meta:
        db_table = "courses"

    def __str__(self) -> str:
        return self.course_code


class StudentCourse(models.Model):
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name="student_courses",
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="student_courses",
    )
    programme_term = models.IntegerField(null=True, blank=True)
    status = models.TextField(blank=True, default="")
    grade = models.TextField(blank=True, default="")
    mark = models.FloatField(null=True, blank=True)
    actual_term = models.TextField(blank=True, default="")

    class Meta:
        db_table = "student_courses"

    def __str__(self) -> str:
        return f"SC({self.student_id}->{self.course_id})"


class ProgrammeRequirement(models.Model):
    program = models.TextField()
    course_code = models.TextField()
    type = models.TextField(blank=True, default="")
    programme_term = models.IntegerField(null=True, blank=True)
    credit_hours = models.IntegerField(null=True, blank=True)
    is_online = models.BooleanField(default=False)

    class Meta:
        db_table = "programme_requirements"
        constraints = [
            models.UniqueConstraint(
                fields=["program", "course_code"],
                name="uq_programme_requirements_program_code",
            ),
        ]
        indexes = [
            models.Index(fields=["program"], name="idx_pr_program"),
        ]

    def __str__(self) -> str:
        return f"Req({self.program}/{self.course_code})"


class Prerequisite(models.Model):
    program = models.TextField()
    course_code = models.TextField()
    prerequisite_course_code = models.TextField()

    class Meta:
        db_table = "prerequisites"
        indexes = [
            models.Index(
                fields=["program", "course_code"],
                name="idx_prereq_program_code",
            ),
            models.Index(
                fields=["prerequisite_course_code", "program"],
                name="idx_prereq_prereq_program",
            ),
        ]

    def __str__(self) -> str:
        return f"Prereq({self.course_code}->{self.prerequisite_course_code})"


class AcademicAdvisor(models.Model):
    advisor_id = models.TextField(primary_key=True)
    full_name = models.TextField()
    email = models.TextField(unique=True)
    department = models.TextField()
    created_at = models.TextField(blank=True, default="")

    class Meta:
        db_table = "academic_advisors"

    def __str__(self) -> str:
        return f"Advisor({self.advisor_id})"


class TermSection(models.Model):
    source_tag = models.TextField(default="other")
    course_name = models.TextField(blank=True, default="")
    available_capacity = models.IntegerField(null=True, blank=True)
    registered_count = models.IntegerField(null=True, blank=True)
    course_code = models.TextField()
    course_number = models.TextField()
    course_key = models.TextField()
    section = models.TextField()
    source_file = models.TextField(blank=True, default="")
    created_at = models.TextField(blank=True, default="")
    updated_at = models.TextField(blank=True, default="")

    class Meta:
        db_table = "term_sections"
        constraints = [
            models.UniqueConstraint(
                fields=["course_key", "section"],
                name="ux_term_sections_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["course_key"], name="idx_ts_course_key"),
        ]

    def __str__(self) -> str:
        return f"TermSection({self.course_key}:{self.section})"


class TermSectionMeeting(models.Model):
    term_section = models.ForeignKey(
        TermSection,
        on_delete=models.CASCADE,
        related_name="meetings",
    )
    day = models.TextField()
    start_time = models.TextField()
    end_time = models.TextField()
    building = models.TextField(blank=True, default="")
    floor_wing = models.TextField(blank=True, default="")
    room = models.TextField(blank=True, default="")
    instructor = models.TextField(blank=True, default="")
    created_at = models.TextField(blank=True, default="")
    updated_at = models.TextField(blank=True, default="")

    class Meta:
        db_table = "term_section_meetings"
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "term_section",
                    "day",
                    "start_time",
                    "end_time",
                    "room",
                    "instructor",
                ],
                name="ux_term_section_meetings_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"Meeting({self.term_section_id}/{self.day})"


class StudentTermSection(models.Model):
    student_id = models.IntegerField()
    academic_year = models.TextField()
    term = models.TextField()
    term_section = models.ForeignKey(
        TermSection,
        on_delete=models.CASCADE,
        related_name="student_sections",
    )
    source = models.TextField(default="manual")
    created_at = models.TextField(blank=True, default="")
    updated_at = models.TextField(blank=True, default="")

    class Meta:
        db_table = "student_term_sections"
        constraints = [
            models.UniqueConstraint(
                fields=["student_id", "term_section"],
                name="ux_student_term_sections_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["student_id"],
                name="ix_sts_student",
            ),
        ]

    def __str__(self) -> str:
        return f"STS({self.student_id}->{self.term_section_id})"


class UserScope(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
    )
    advisor_id = models.TextField(blank=True, default="")
    departments = models.TextField(blank=True, default="")
    updated_at = models.TextField(blank=True, default="")

    class Meta:
        db_table = "core_user_scope"

    def __str__(self) -> str:
        return f"Scope(user={self.user_id})"


class AuditLog(models.Model):
    ts_utc = models.TextField()
    actor_username = models.TextField(blank=True, default="")
    actor_role = models.TextField(blank=True, default="")
    action = models.TextField()
    endpoint = models.TextField(blank=True, default="")
    method = models.TextField(blank=True, default="")
    status = models.TextField(blank=True, default="")
    details_json = models.TextField(blank=True, default="{}")
    error_text = models.TextField(blank=True, default="")
    prev_hash = models.TextField(blank=True, default="")
    entry_hash = models.TextField(blank=True, default="")

    class Meta:
        db_table = "core_audit_log"
        indexes = [
            models.Index(fields=["action"], name="idx_audit_action"),
            models.Index(fields=["actor_username"], name="idx_audit_actor"),
            models.Index(fields=["ts_utc"], name="idx_audit_ts"),
        ]

    def __str__(self) -> str:
        return f"AuditLog({self.id}/{self.action})"


# ── Exam Timetable Builder ──────────────────────────────────────


class ExamTimetableRun(models.Model):
    label = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    result_json = models.TextField(default="{}")

    class Meta:
        db_table = "exam_timetable_runs"

    def __str__(self) -> str:
        return f"ExamTimetableRun({self.id}/{self.label})"
