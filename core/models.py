import uuid

from django.conf import settings
from django.db import models, transaction
from django.db.models import F


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
        constraints = [
            models.UniqueConstraint(
                fields=["student", "course"],
                name="uq_student_courses_student_course",
            ),
        ]
        indexes = [
            models.Index(fields=["student", "status"], name="idx_sc_student_status"),
            models.Index(fields=["course", "status"], name="idx_sc_course_status"),
        ]

    def __str__(self) -> str:
        return f"SC({self.student_id}->{self.course_id})"


class ProgrammeRequirement(models.Model):
    program = models.TextField()
    course_code = models.TextField()
    course_name = models.TextField(blank=True, default="")
    type = models.TextField(blank=True, default="")
    programme_term = models.IntegerField(null=True, blank=True)
    credit_hours = models.IntegerField(null=True, blank=True)
    is_online = models.BooleanField(default=False)
    max_capacity = models.IntegerField(null=True, blank=True)

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
            models.Index(fields=["program", "programme_term"], name="idx_pr_program_term"),
        ]

    def __str__(self) -> str:
        return f"Req({self.program}/{self.course_code})"


class Prerequisite(models.Model):
    program = models.TextField()
    course_code = models.TextField()
    prerequisite_course_code = models.TextField()

    class Meta:
        db_table = "prerequisites"
        constraints = [
            models.UniqueConstraint(
                fields=["program", "course_code", "prerequisite_course_code"],
                name="uq_prerequisites_program_course_prereq",
            ),
        ]
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


class Room(models.Model):
    """Department room inventory."""

    SECTION_MALE = "M"
    SECTION_FEMALE = "F"
    SECTION_CHOICES = [
        (SECTION_MALE, "Male"),
        (SECTION_FEMALE, "Female"),
    ]

    room_code = models.TextField()
    wing = models.TextField(blank=True, default="")
    building = models.TextField(blank=True, default="")
    floor = models.IntegerField(null=True, blank=True)
    room_type = models.TextField(blank=True, default="lecture")
    capacity = models.IntegerField(default=0)
    department = models.TextField(blank=True, default="")
    section = models.CharField(max_length=1, choices=SECTION_CHOICES, default=SECTION_MALE)

    class Meta:
        db_table = "rooms"
        indexes = [
            models.Index(fields=["department"], name="idx_room_department"),
            models.Index(fields=["section"], name="idx_room_section"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["room_code", "section"],
                name="uniq_room_code_section",
            ),
        ]

    def __str__(self) -> str:
        return f"Room({self.room_code}/{self.department}/{self.capacity})"


class ElectiveCourse(models.Model):
    """Permanent catalogue of elective courses offered by a department.

    Each row represents one real course (e.g. AI461 "Data Mining") that can
    fill an elective placeholder slot (AI1, AI2) in the degree plan.
    """

    course_code = models.TextField()
    course_name = models.TextField()
    programme = models.TextField()
    category = models.TextField(blank=True, default="")
    credit_hours = models.IntegerField(default=3)
    prerequisites_csv = models.TextField(blank=True, default="")

    class Meta:
        db_table = "elective_courses"
        constraints = [
            models.UniqueConstraint(
                fields=["programme", "course_code"],
                name="uq_elective_programme_code",
            ),
        ]
        indexes = [
            models.Index(fields=["programme"], name="idx_elective_programme"),
        ]

    def __str__(self) -> str:
        return f"Elective({self.programme}/{self.course_code})"


class ElectiveTermMapping(models.Model):
    """Per-term assignment of real elective courses to placeholder slots.

    Each term, the department decides which catalogue courses fill each
    elective slot.  E.g. for term 1448/1, AI1 → AI461 and AI1 → AI462
    means students can choose either Data Mining or Big Data Analytics
    to satisfy their "Department Elective 1" requirement.
    """

    academic_year = models.TextField()
    term = models.IntegerField()
    programme = models.TextField()
    placeholder_code = models.TextField()
    elective = models.ForeignKey(
        ElectiveCourse, on_delete=models.CASCADE, related_name="term_mappings"
    )

    class Meta:
        db_table = "elective_term_mappings"
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "term", "programme", "placeholder_code", "elective"],
                name="uq_elective_mapping",
            ),
        ]
        indexes = [
            models.Index(
                fields=["academic_year", "term", "programme"],
                name="idx_etm_year_term_prog",
            ),
        ]

    def __str__(self) -> str:
        return f"Map({self.placeholder_code}->{self.elective.course_code} {self.academic_year}T{self.term})"


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


class Instructor(models.Model):
    """A global teaching staff member, reused across scenarios and terms.

    Identity is global (a person is the same human everywhere); the
    *assignment* of who teaches what is scenario-independent and lives on the
    ``CourseInstructor`` link (program + course + section M/F). ``normalised_name``
    is the strip+casefold of ``full_name`` (via ``normalise_instructor``) — the
    dedupe target and the join key against the legacy free-text instructor name.
    """

    full_name = models.TextField()
    normalised_name = models.TextField()
    full_name_ar = models.TextField(blank=True, default="")
    email = models.TextField(blank=True, default="")
    employee_no = models.TextField(blank=True, default="")
    department = models.TextField(blank=True, default="")
    # Advisory only — surfaced in the load report, NOT a clash/solver input.
    max_weekly_hours = models.IntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "instructors"
        constraints = [
            models.UniqueConstraint(
                fields=["normalised_name"],
                name="ux_instructors_normalised_name",
            ),
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(email__gt=""),
                name="ux_instructors_email_present",
            ),
        ]
        indexes = [
            models.Index(fields=["department"], name="idx_instructors_dept"),
            models.Index(fields=["is_active"], name="idx_instructors_active"),
        ]

    def __str__(self) -> str:
        return f"Instructor({self.pk}/{self.full_name})"


class CourseInstructor(models.Model):
    """Scenario-INDEPENDENT assignment of a global ``Instructor`` to a course,
    keyed by ``(program, course_code, section M/F)``.

    This is the source of truth for "who teaches this course for this cohort".
    The planner resolves the primary at section-generation time and writes the
    name into ``TermSectionMeeting.instructor`` (the legacy clash key), so an
    assignment made here is independent of any scenario.
    """

    program = models.TextField()
    course_code = models.TextField()  # normalised on write (normalize_course_code)
    section = models.CharField(max_length=1, choices=[("M", "Male"), ("F", "Female")])
    instructor = models.ForeignKey(
        Instructor,
        on_delete=models.PROTECT,
        related_name="course_links",
    )
    role = models.TextField(default="primary")  # primary | co | lab
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "course_instructors"
        constraints = [
            models.UniqueConstraint(
                fields=["program", "course_code", "section", "instructor"],
                name="ux_course_instructor_unique",
            ),
            # Exactly one primary per (program, course, section) so the
            # section-generation write-through has a deterministic display name.
            models.UniqueConstraint(
                fields=["program", "course_code", "section"],
                condition=models.Q(role="primary"),
                name="ux_course_instructor_one_primary",
            ),
        ]
        indexes = [
            models.Index(fields=["program", "course_code", "section"], name="idx_ci_lookup"),
            models.Index(fields=["instructor"], name="idx_ci_instructor"),
        ]

    def __str__(self) -> str:
        return f"CourseInstructor({self.program}/{self.course_code}/{self.section}->{self.instructor_id})"


class TermSection(models.Model):
    # Scenario FK: scopes auto-generated sections to a specific scenario
    # so two scenarios can both have CS211/S1 independently.
    # NULL for imported/scraped sections that are global (not scenario-specific).
    scenario = models.ForeignKey(
        "TimetableScenario",
        on_delete=models.CASCADE,
        related_name="term_sections",
        null=True,
        blank=True,
    )
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
            # Scenario-owned sections: unique per (scenario, course_key, section)
            models.UniqueConstraint(
                fields=["scenario", "course_key", "section"],
                condition=models.Q(scenario__isnull=False),
                name="ux_term_sections_scenario",
            ),
            # Global sections (imported/scraped): unique per (course_key, section)
            models.UniqueConstraint(
                fields=["course_key", "section"],
                condition=models.Q(scenario__isnull=True),
                name="ux_term_sections_global",
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
    student_id = models.IntegerField(null=True, blank=True, db_index=True)
    updated_at = models.TextField(blank=True, default="")

    class Meta:
        db_table = "core_user_scope"

    def __str__(self) -> str:
        return f"Scope(user={self.user_id})"


class StudentLoginOTP(models.Model):
    """One-time email code for student login. The code itself is never stored —
    only a salted SHA-256 hash. Short-lived, single-use, attempt-capped."""

    student_id = models.IntegerField(db_index=True)
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.IntegerField(default=0)
    consumed = models.BooleanField(default=False)
    request_ip = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "core_student_login_otp"
        indexes = [models.Index(fields=["student_id", "consumed"], name="ix_otp_student_consumed")]

    def __str__(self) -> str:
        return f"OTP(student={self.student_id}, consumed={self.consumed})"


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


# ── Timetable Builder Workspace ─────────────────────────────────


class TimetableScenario(models.Model):
    academic_year = models.TextField()
    term = models.TextField()
    name = models.TextField()
    status = models.TextField(default="draft")
    slot_config = models.JSONField(default=list)
    lab_slot_config = models.JSONField(default=list)
    blocked_slots = models.JSONField(default=list)  # [{day, start}] protected institutional blocks
    # Structured cohort identity (populated at generation) so consumers never
    # parse the scenario name. gender = "M"/"F"; programs = ["AI", "DS", ...].
    gender = models.CharField(
        max_length=1, choices=[("M", "Male"), ("F", "Female")], blank=True, default=""
    )
    programs = models.JSONField(default=list)
    created_by = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "timetable_scenarios"
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "term", "name"],
                name="ux_tt_scenario_year_term_name",
            ),
        ]

    def __str__(self) -> str:
        return f"Scenario({self.id}/{self.name})"


class DeliveryBoard(models.Model):
    scenario = models.ForeignKey(
        TimetableScenario,
        on_delete=models.CASCADE,
        related_name="boards",
    )
    label = models.TextField()
    nominal_term = models.IntegerField(null=True, blank=True)
    board_type = models.TextField(default="standard")
    program = models.TextField(blank=True, null=True)  # noqa: DJ001
    target_size = models.IntegerField(default=0)
    display_order = models.IntegerField(default=0)
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "delivery_boards"
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "label"],
                name="ux_delivery_board_scenario_label",
            ),
        ]
        indexes = [
            models.Index(fields=["scenario"], name="idx_db_scenario"),
        ]

    def __str__(self) -> str:
        return f"Board({self.id}/{self.label})"


class SectionPlacement(models.Model):
    board = models.ForeignKey(
        DeliveryBoard,
        on_delete=models.CASCADE,
        related_name="placements",
    )
    term_section = models.ForeignKey(
        TermSection,
        on_delete=models.CASCADE,
        related_name="placements",
    )
    day = models.TextField()
    start_time = models.TextField()
    end_time = models.TextField()
    room = models.TextField(blank=True, default="")
    is_locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "section_placements"
        constraints = [
            models.UniqueConstraint(
                fields=["board", "term_section", "day", "start_time"],
                name="ux_placement_board_section_day_start",
            ),
        ]
        indexes = [
            models.Index(fields=["board"], name="idx_sp_board"),
            models.Index(fields=["term_section"], name="idx_sp_term_section"),
            models.Index(fields=["board", "day", "start_time"], name="idx_sp_board_day_start"),
        ]

    def __str__(self) -> str:
        return f"Placement({self.id}/{self.term_section_id})"


class BoardSectionVisibility(models.Model):
    board = models.ForeignKey(
        DeliveryBoard,
        on_delete=models.CASCADE,
        related_name="visible_sections",
    )
    term_section = models.ForeignKey(
        TermSection,
        on_delete=models.CASCADE,
        related_name="board_visibility",
    )

    class Meta:
        db_table = "board_section_visibility"
        constraints = [
            models.UniqueConstraint(
                fields=["board", "term_section"],
                name="ux_bsv_board_section",
            ),
        ]

    def __str__(self) -> str:
        return f"BSV({self.board_id}->{self.term_section_id})"


class TimeSlotTemplate(models.Model):
    name = models.TextField()
    slots = models.JSONField(default=list)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "time_slot_templates"

    def __str__(self) -> str:
        return f"SlotTemplate({self.id}/{self.name})"


# ── Timetable Workspace: Cohort Classification ─────────────────


class ScenarioStudentMap(models.Model):
    scenario = models.ForeignKey(
        TimetableScenario,
        on_delete=models.CASCADE,
        related_name="student_maps",
    )
    student_id = models.IntegerField()
    primary_term = models.IntegerField()
    is_cross_term = models.BooleanField(default=False)
    recommended_courses = models.JSONField(default=list)
    recommended_course_keys = models.JSONField(default=list)

    class Meta:
        db_table = "scenario_student_maps"
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "student_id"],
                name="ux_ssm_scenario_student",
            ),
        ]
        indexes = [
            models.Index(fields=["scenario"], name="idx_ssm_scenario"),
            models.Index(fields=["scenario", "primary_term"], name="idx_ssm_scenario_pt"),
        ]

    def __str__(self) -> str:
        return f"SSM({self.scenario_id}/{self.student_id}→T{self.primary_term})"


class ScenarioStudentCourseRequest(models.Model):
    """Normalised per-student course demand for a timetable scenario.

    ``ScenarioStudentMap`` remains the compact scenario classification snapshot. This
    table is the canonical row-level source for features that need request
    status, priority, blocked reason, or efficient course/student queries.
    """

    STATUS_REQUESTED = "requested"
    STATUS_BLOCKED = "blocked"
    STATUS_SERVED = "served"
    STATUS_IGNORED = "ignored"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_REQUESTED, "Requested"),
        (STATUS_BLOCKED, "Blocked"),
        (STATUS_SERVED, "Served"),
        (STATUS_IGNORED, "Ignored"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    PRIORITY_NORMAL = "normal"
    PRIORITY_GRADUATING = "graduating"
    PRIORITY_MANUAL_APPROVAL = "manual_approval"
    PRIORITY_SPECIAL_CASE = "special_case"
    PRIORITY_CHOICES = (
        (PRIORITY_NORMAL, "Normal"),
        (PRIORITY_GRADUATING, "Graduating"),
        (PRIORITY_MANUAL_APPROVAL, "Manual approval"),
        (PRIORITY_SPECIAL_CASE, "Special case"),
    )

    scenario = models.ForeignKey(
        TimetableScenario,
        on_delete=models.CASCADE,
        related_name="student_course_requests",
    )
    student_id = models.IntegerField()
    course_key = models.TextField()
    course_code = models.TextField()
    course_name = models.TextField(blank=True, default="")
    primary_term = models.IntegerField(null=True, blank=True)
    is_cross_term = models.BooleanField(default=False)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_REQUESTED)
    priority = models.CharField(max_length=32, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)
    reason_blocked = models.CharField(max_length=80, blank=True, default="")
    reason_detail = models.TextField(blank=True, default="")
    source = models.CharField(max_length=64, default="batch_recommender")
    source_payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scenario_student_course_requests"
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "student_id", "course_key"],
                name="ux_sscr_scenario_student_course",
            ),
        ]
        indexes = [
            models.Index(fields=["scenario", "course_key"], name="idx_sscr_scenario_course"),
            models.Index(fields=["scenario", "student_id"], name="idx_sscr_scenario_student"),
            models.Index(fields=["scenario", "status"], name="idx_sscr_scenario_status"),
            models.Index(fields=["scenario", "priority"], name="idx_sscr_scenario_priority"),
        ]

    def __str__(self) -> str:
        return f"SSCR({self.scenario_id}/{self.student_id}/{self.course_key})"


class ScenarioSectionBudget(models.Model):
    scenario = models.ForeignKey(
        TimetableScenario,
        on_delete=models.CASCADE,
        related_name="section_budgets",
    )
    course_key = models.TextField(blank=True, null=True)  # noqa: DJ001
    course_code = models.TextField()
    course_name = models.TextField(blank=True, default="")
    department = models.TextField(blank=True, default="")
    credit_hours = models.IntegerField(default=0)
    planned_sections = models.IntegerField(default=0)
    max_per_section = models.IntegerField(default=40)
    total_demand = models.IntegerField(default=0)
    programme_term = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "scenario_section_budgets"
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "course_key"],
                condition=models.Q(course_key__isnull=False) & ~models.Q(course_key=""),
                name="ux_ssb_scenario_course_key",
            ),
        ]
        indexes = [
            models.Index(fields=["scenario"], name="idx_ssb_scenario"),
            models.Index(fields=["scenario", "course_key"], name="idx_ssb_scenario_key"),
        ]

    def __str__(self) -> str:
        return f"Budget({self.scenario_id}/{self.course_key or self.course_code})"

    def save(self, *args, **kwargs) -> None:
        if not self.course_key:
            self.course_key = self.course_code
        super().save(*args, **kwargs)


class BoardStudentLink(models.Model):
    board = models.ForeignKey(
        DeliveryBoard,
        on_delete=models.CASCADE,
        related_name="student_links",
    )
    student_id = models.IntegerField()
    link_type = models.TextField(default="primary")

    class Meta:
        db_table = "board_student_links"
        constraints = [
            models.UniqueConstraint(
                fields=["board", "student_id"],
                name="ux_bsl_board_student",
            ),
        ]
        indexes = [
            models.Index(fields=["board"], name="idx_bsl_board"),
            models.Index(fields=["board", "link_type"], name="idx_bsl_board_type"),
        ]

    def __str__(self) -> str:
        return f"BSL({self.board_id}/{self.student_id}/{self.link_type})"


class PlannerJob(models.Model):
    """PR7 — async planner job audit row.

    Single-web-process async shim. See ``docs/PR7-DOR.md`` for the full
    "what this is not" floor (process-local; not durable across deploys;
    cooperative cancel only; no cross-process recovery).
    """

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    MODE_OPTIMISE_CURRENT = "optimise_current"
    MODE_FULL_REBUILD = "full_rebuild"
    MODE_OPTIMISE_V2_FULL = "optimise_v2_full"
    MODE_OPTIMISE_V2_CURRENT = "optimise_v2_current"
    #: Build with the `scheduler` subsystem instead of the original placer. Same
    #: scenario scaffold, same students, same section budgets — a different
    #: engine for the one question of where the classes go.
    MODE_SCHEDULER_BUILD = "scheduler_build"
    MODE_CHOICES = (
        (MODE_OPTIMISE_CURRENT, "Optimise current"),
        (MODE_FULL_REBUILD, "Full rebuild"),
        (MODE_OPTIMISE_V2_FULL, "Optimise V2 (full rebuild)"),
        (MODE_OPTIMISE_V2_CURRENT, "Optimise V2 (current)"),
        (MODE_SCHEDULER_BUILD, "New scheduler engine"),
    )

    STAGE_CHOICES = (
        ("greedy", "greedy"),
        ("sa", "sa"),
        ("cpsat", "cpsat"),
        ("chain", "chain"),
        ("rooming_repair", "rooming_repair"),
        # Stages reported by the alternative engine (MODE_SCHEDULER_BUILD).
        ("snapshot", "snapshot"),
        ("solve", "solve"),
        ("rooming", "rooming"),
        ("persist", "persist"),
    )

    id = models.UUIDField(primary_key=True, editable=False)
    scenario = models.ForeignKey(
        TimetableScenario,
        on_delete=models.CASCADE,
        related_name="planner_jobs",
    )
    board = models.ForeignKey(
        DeliveryBoard,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planner_jobs",
    )
    mode = models.CharField(max_length=32, choices=MODE_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planner_jobs",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)  # noqa: DJ001
    result_json = models.JSONField(null=True, blank=True)
    last_stage_seen = models.CharField(  # noqa: DJ001
        max_length=32, choices=STAGE_CHOICES, null=True, blank=True
    )
    cancel_requested = models.BooleanField(default=False)
    request_signature = models.CharField(max_length=64, blank=True, default="")
    # Per-request optimiser tuning (strategies, CP-SAT budget, iteration caps)
    # so an async V2 job replays the SAME params the synchronous path used.
    params = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "planner_jobs"
        indexes = [
            models.Index(fields=["scenario", "status"], name="idx_pj_scenario_status"),
            models.Index(fields=["submitted_by", "-submitted_at"], name="idx_pj_user_submitted"),
        ]

    def __str__(self) -> str:
        return f"PlannerJob({self.id}/{self.status})"


class TimetableRepairRun(models.Model):
    """Audited, read-first registration repair analysis run."""

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    MODE_CONSERVATIVE = "conservative"
    MODE_BALANCED = "balanced"
    MODE_SIMULATION = "simulation"
    MODE_CHOICES = (
        (MODE_CONSERVATIVE, "Conservative"),
        (MODE_BALANCED, "Balanced"),
        (MODE_SIMULATION, "Simulation"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.ForeignKey(
        TimetableScenario,
        on_delete=models.CASCADE,
        related_name="repair_runs",
    )
    target_placement = models.ForeignKey(
        SectionPlacement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="repair_runs",
    )
    target_section = models.ForeignKey(
        TermSection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="repair_runs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="timetable_repair_runs",
    )
    mode = models.CharField(max_length=24, choices=MODE_CHOICES, default=MODE_CONSERVATIVE)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    requested_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    solver_version = models.CharField(max_length=64, default="repair-readonly-v1")
    constraint_version = models.CharField(max_length=64, default="repair-constraints-v1")
    objective_version = models.CharField(max_length=64, default="conservative-readonly-v1")
    request_payload = models.JSONField(default=dict)
    summary_json = models.JSONField(default=dict)
    before_snapshot = models.JSONField(default=dict)
    error_message = models.TextField(blank=True, default="")

    class Meta:
        db_table = "timetable_repair_runs"
        indexes = [
            models.Index(fields=["scenario", "-requested_at"], name="idx_trr_scenario_time"),
            models.Index(fields=["requested_by", "-requested_at"], name="idx_trr_user_time"),
            models.Index(fields=["status"], name="idx_trr_status"),
        ]

    def __str__(self) -> str:
        return f"RepairRun({self.id}/{self.mode}/{self.status})"


class TimetableRepairCandidate(models.Model):
    """A candidate section move evaluated within a repair run."""

    STATUS_FEASIBLE = "feasible"
    STATUS_REJECTED = "rejected_before_solver"
    STATUS_NOT_SOLVED = "not_solved"
    STATUS_CHOICES = (
        (STATUS_FEASIBLE, "Feasible"),
        (STATUS_REJECTED, "Rejected before solver"),
        (STATUS_NOT_SOLVED, "Not solved"),
    )

    run = models.ForeignKey(
        TimetableRepairRun,
        on_delete=models.CASCADE,
        related_name="candidates",
    )
    candidate_id = models.CharField(max_length=64)
    day = models.TextField()
    start_time = models.TextField()
    end_time = models.TextField()
    room = models.TextField(blank=True, default="")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_NOT_SOLVED)
    solver_status = models.CharField(max_length=32, blank=True, default="not_run")
    score_rank = models.IntegerField(null=True, blank=True)
    metrics_json = models.JSONField(default=dict)
    explanation_json = models.JSONField(default=dict)
    rejection_reasons = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "timetable_repair_candidates"
        constraints = [
            models.UniqueConstraint(
                fields=["run", "candidate_id"],
                name="ux_trc_run_candidate",
            ),
        ]
        indexes = [
            models.Index(fields=["run", "status"], name="idx_trc_run_status"),
            models.Index(fields=["run", "score_rank"], name="idx_trc_run_rank"),
        ]

    def __str__(self) -> str:
        return f"RepairCandidate({self.run_id}/{self.candidate_id}/{self.status})"


class TimetableRepairCandidateMetric(models.Model):
    """Normalized scalar metrics for querying and reporting repair candidates."""

    candidate = models.ForeignKey(
        TimetableRepairCandidate,
        on_delete=models.CASCADE,
        related_name="metric_rows",
    )
    metric_key = models.CharField(max_length=160)
    category = models.CharField(max_length=64, blank=True, default="")
    value_number = models.FloatField(null=True, blank=True)
    value_text = models.TextField(blank=True, default="")
    value_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "timetable_repair_candidate_metrics"
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "metric_key"],
                name="ux_trcm_candidate_metric",
            ),
        ]
        indexes = [
            models.Index(fields=["candidate"], name="idx_trcm_candidate"),
            models.Index(fields=["category", "metric_key"], name="idx_trcm_category_key"),
            models.Index(fields=["metric_key"], name="idx_trcm_key"),
        ]

    def __str__(self) -> str:
        return f"RepairCandidateMetric({self.candidate_id}/{self.metric_key})"


class TimetableRepairRejectedCandidate(models.Model):
    """Structured rejection evidence for candidates skipped before solving."""

    run = models.ForeignKey(
        TimetableRepairRun,
        on_delete=models.CASCADE,
        related_name="rejected_candidates",
    )
    candidate = models.ForeignKey(
        TimetableRepairCandidate,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="rejection_rows",
    )
    candidate_key = models.CharField(max_length=64)
    day = models.TextField()
    start_time = models.TextField()
    end_time = models.TextField()
    room = models.TextField(blank=True, default="")
    reasons_json = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "timetable_repair_rejected_candidates"
        indexes = [
            models.Index(fields=["run"], name="idx_trrc_run"),
        ]

    def __str__(self) -> str:
        return f"RepairRejected({self.run_id}/{self.candidate_key})"


class TimetableRepairStudentChange(models.Model):
    """Student-level change proposed by a repair candidate."""

    CHANGE_UNCHANGED = "unchanged"
    CHANGE_MOVED = "moved_section"
    CHANGE_NEWLY_REGISTERED = "newly_registered"
    CHANGE_UNRESOLVED = "unresolved"
    CHANGE_LOST = "lost_course"
    CHANGE_LOCKED = "locked"
    CHANGE_CHOICES = (
        (CHANGE_UNCHANGED, "Unchanged"),
        (CHANGE_MOVED, "Moved section"),
        (CHANGE_NEWLY_REGISTERED, "Newly registered"),
        (CHANGE_UNRESOLVED, "Unresolved"),
        (CHANGE_LOST, "Lost course"),
        (CHANGE_LOCKED, "Locked"),
    )

    candidate = models.ForeignKey(
        TimetableRepairCandidate,
        on_delete=models.CASCADE,
        related_name="student_changes",
    )
    student_id = models.IntegerField()
    course_key = models.TextField()
    before_section_id = models.TextField(blank=True, default="")
    after_section_id = models.TextField(blank=True, default="")
    change_type = models.CharField(max_length=32, choices=CHANGE_CHOICES)
    details_json = models.JSONField(default=dict)

    class Meta:
        db_table = "timetable_repair_student_changes"
        indexes = [
            models.Index(fields=["candidate"], name="idx_trsc_candidate"),
            models.Index(fields=["student_id"], name="idx_trsc_student"),
            models.Index(fields=["course_key"], name="idx_trsc_course"),
        ]

    def __str__(self) -> str:
        return f"RepairStudentChange({self.candidate_id}/{self.student_id}/{self.change_type})"


class TimetableRepairSnapshot(models.Model):
    """JSON snapshot used for audit, rollback design, and reproducibility."""

    KIND_BEFORE = "before"
    KIND_AFTER = "after"
    KIND_COMPONENT = "component"
    KIND_CHOICES = (
        (KIND_BEFORE, "Before"),
        (KIND_AFTER, "After"),
        (KIND_COMPONENT, "Component"),
    )

    run = models.ForeignKey(
        TimetableRepairRun,
        on_delete=models.CASCADE,
        related_name="snapshots",
    )
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    payload_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "timetable_repair_snapshots"
        indexes = [
            models.Index(fields=["run", "kind"], name="idx_trs_run_kind"),
        ]

    def __str__(self) -> str:
        return f"RepairSnapshot({self.run_id}/{self.kind})"


class TimetableRepairSolverLog(models.Model):
    """Compact solver/audit events for one repair run."""

    run = models.ForeignKey(
        TimetableRepairRun,
        on_delete=models.CASCADE,
        related_name="solver_logs",
    )
    candidate = models.ForeignKey(
        TimetableRepairCandidate,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="solver_logs",
    )
    level = models.CharField(max_length=16, default="info")
    message = models.TextField()
    payload_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "timetable_repair_solver_logs"
        indexes = [
            models.Index(fields=["run", "created_at"], name="idx_trsl_run_time"),
        ]

    def __str__(self) -> str:
        return f"RepairSolverLog({self.run_id}/{self.level})"


class TimetableRepairApproval(models.Model):
    """Approval gate for future apply/rollback flows."""

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_APPLIED = "applied"
    STATUS_ROLLED_BACK = "rolled_back"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_APPLIED, "Applied"),
        (STATUS_ROLLED_BACK, "Rolled back"),
    )

    run = models.ForeignKey(
        TimetableRepairRun,
        on_delete=models.CASCADE,
        related_name="approvals",
    )
    candidate = models.ForeignKey(
        TimetableRepairCandidate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approvals",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_timetable_repair_approvals",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_timetable_repair_approvals",
    )
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "timetable_repair_approvals"
        indexes = [
            models.Index(fields=["run", "status"], name="idx_tra_run_status"),
        ]

    def __str__(self) -> str:
        return f"RepairApproval({self.run_id}/{self.status})"


class TimetableRepairGlobalPlan(models.Model):
    """Coordinated programme/level repair plan built from fresh repair runs."""

    STATUS_DRAFT = "draft"
    STATUS_APPROVED = "approved"
    STATUS_APPLIED = "applied"
    STATUS_ROLLED_BACK = "rolled_back"
    STATUS_FAILED = "failed"
    STATUS_EMPTY = "empty"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_APPLIED, "Applied"),
        (STATUS_ROLLED_BACK, "Rolled back"),
        (STATUS_FAILED, "Failed"),
        (STATUS_EMPTY, "Empty"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.ForeignKey(
        TimetableScenario,
        on_delete=models.CASCADE,
        related_name="repair_global_plans",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_timetable_repair_global_plans",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_timetable_repair_global_plans",
    )
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    scope_program = models.CharField(max_length=64, blank=True, default="")
    scope_nominal_term = models.IntegerField(null=True, blank=True)
    mode = models.CharField(
        max_length=24,
        choices=TimetableRepairRun.MODE_CHOICES,
        default=TimetableRepairRun.MODE_CONSERVATIVE,
    )
    request_signature = models.CharField(max_length=64)
    request_payload = models.JSONField(default=dict)
    simulation_json = models.JSONField(default=dict)
    summary_json = models.JSONField(default=dict)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "timetable_repair_global_plans"
        indexes = [
            models.Index(fields=["scenario", "-created_at"], name="idx_trgp_scenario_time"),
            models.Index(fields=["status"], name="idx_trgp_status"),
            models.Index(fields=["request_signature"], name="idx_trgp_signature"),
        ]

    def __str__(self) -> str:
        return f"RepairGlobalPlan({self.id}/{self.status})"


class TimetableRepairGlobalPlanItem(models.Model):
    """One applyable repair candidate selected into a global repair plan."""

    STATUS_READY = "ready"
    STATUS_APPROVED = "approved"
    STATUS_APPLIED = "applied"
    STATUS_ROLLED_BACK = "rolled_back"
    STATUS_SKIPPED = "skipped"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_READY, "Ready"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_APPLIED, "Applied"),
        (STATUS_ROLLED_BACK, "Rolled back"),
        (STATUS_SKIPPED, "Skipped"),
        (STATUS_FAILED, "Failed"),
    )

    plan = models.ForeignKey(
        TimetableRepairGlobalPlan,
        on_delete=models.CASCADE,
        related_name="items",
    )
    sequence = models.PositiveIntegerField()
    repair_run = models.ForeignKey(
        TimetableRepairRun,
        on_delete=models.CASCADE,
        related_name="global_plan_items",
    )
    candidate = models.ForeignKey(
        TimetableRepairCandidate,
        on_delete=models.CASCADE,
        related_name="global_plan_items",
    )
    placement = models.ForeignKey(
        SectionPlacement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="repair_global_plan_items",
    )
    course_key = models.TextField(blank=True, default="")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_READY)
    metrics_json = models.JSONField(default=dict)
    impact_json = models.JSONField(default=dict)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "timetable_repair_global_plan_items"
        constraints = [
            models.UniqueConstraint(fields=["plan", "sequence"], name="ux_trgpi_plan_sequence"),
            models.UniqueConstraint(fields=["plan", "repair_run"], name="ux_trgpi_plan_run"),
            models.UniqueConstraint(fields=["plan", "candidate"], name="ux_trgpi_plan_candidate"),
        ]
        indexes = [
            models.Index(fields=["plan", "status"], name="idx_trgpi_plan_status"),
            models.Index(fields=["repair_run"], name="idx_trgpi_run"),
            models.Index(fields=["candidate"], name="idx_trgpi_candidate"),
        ]

    def __str__(self) -> str:
        return f"RepairGlobalPlanItem({self.plan_id}/{self.sequence}/{self.status})"


class TimetableRepairJob(models.Model):
    """Durable queue row for repair analysis and simulation work."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    KIND_ANALYSIS = "repair_analysis"
    KIND_SIMULATION = "repair_simulation"
    KIND_CHOICES = (
        (KIND_ANALYSIS, "Repair analysis"),
        (KIND_SIMULATION, "Repair simulation"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    scenario = models.ForeignKey(
        TimetableScenario,
        on_delete=models.CASCADE,
        related_name="repair_jobs",
    )
    repair_run = models.ForeignKey(
        TimetableRepairRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="jobs",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="timetable_repair_jobs",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    request_signature = models.CharField(max_length=64)
    cache_fingerprint = models.CharField(max_length=64, blank=True, default="")
    request_payload = models.JSONField(default=dict)
    progress_json = models.JSONField(default=dict)
    result_json = models.JSONField(default=dict)
    error_message = models.TextField(blank=True, default="")
    cancel_requested = models.BooleanField(default=False)
    attempt_count = models.IntegerField(default=0)
    locked_by = models.CharField(max_length=128, blank=True, default="")
    locked_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "timetable_repair_jobs"
        indexes = [
            models.Index(
                fields=["kind", "status", "submitted_at"], name="idx_trj_kind_status_time"
            ),
            models.Index(fields=["scenario", "kind", "status"], name="idx_trj_scenario_kind"),
            models.Index(fields=["submitted_by", "-submitted_at"], name="idx_trj_user_submitted"),
            models.Index(fields=["request_signature"], name="idx_trj_signature"),
        ]

    def __str__(self) -> str:
        return f"RepairJob({self.id}/{self.kind}/{self.status})"


# ── Adviser conversations ────────────────────────────────────────
#
# The chat previously kept its history in a JavaScript array — last eight turns,
# gone on reload. Nothing was stored, so there was nothing for a student to return
# to, nothing for an adviser to review, and nothing to attach feedback or an
# escalation to. Every one of those depends on messages existing.
#
# student_id is a plain indexed integer rather than a ForeignKey, matching
# StudentLoginOTP: the students table is externally sourced and re-imported, and a
# FK would let a refresh cascade away a student's conversation history.


class AdvisorConversation(models.Model):
    """One chat thread belonging to exactly one student."""

    STATUS_ACTIVE = "ACTIVE"
    STATUS_ARCHIVED = "ARCHIVED"
    STATUS_ESCALATED = "ESCALATED"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
        (STATUS_ESCALATED, "Escalated"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student_id = models.IntegerField(db_index=True)
    title = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "advisor_conversations"
        # `-last_message_at` alone is not portable: SQLite sorts NULLs last on a
        # descending column, PostgreSQL sorts them first. A conversation whose send
        # failed before any message landed would therefore sit at the bottom in
        # development and at the top in production. Say which we mean.
        ordering = [F("last_message_at").desc(nulls_last=True), "-created_at"]
        indexes = [
            models.Index(fields=["student_id", "-last_message_at"], name="idx_adv_conv_student"),
        ]

    def __str__(self) -> str:
        return f"Conversation({self.id}/{self.student_id})"


class FinalDisposition(models.TextChoices):
    """What the final student-visible answer did.

    FAILED is separate from ABSTAIN on purpose: abstaining is a decision the
    adviser made about the evidence, while failing is the adviser never getting to
    decide. An escalation queue that cannot tell them apart fills with outages.
    """

    PASS = "PASS", "Answered"
    ABSTAIN = "ABSTAIN", "Declined to answer"
    ESCALATE = "ESCALATE", "Sent to a human adviser"
    FAILED = "FAILED", "Could not be produced"


class AdvisorMessage(models.Model):
    """One turn. The student-visible body ONLY.

    Tool results and judge traces are deliberately absent: they name database
    tables, quote row counts and cohort statistics, and belong in an operator
    audit record rather than in something rendered to the person who asked.
    """

    ROLE_STUDENT = "STUDENT"
    ROLE_ASSISTANT = "ASSISTANT"
    ROLE_CHOICES = [(ROLE_STUDENT, "Student"), (ROLE_ASSISTANT, "Assistant")]

    ROUTE_AGENT = "AGENT"
    ROUTE_SEEDED_FALLBACK = "SEEDED_FALLBACK"
    ROUTE_CHOICES = [(ROUTE_AGENT, "Agent loop"), (ROUTE_SEEDED_FALLBACK, "Seeded fallback")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        AdvisorConversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    content = models.TextField()

    # How the answer was reached. Per-message rather than per-conversation because a
    # single thread mixes grounded policy answers with pure student-data ones, and
    # "was this grounded?" has to be answerable turn by turn.
    answer_mode = models.CharField(max_length=16, blank=True, default="")
    grounding_state = models.CharField(max_length=24, blank=True, default="")

    #: What the FINAL, validated answer did — not what the policies permitted. A
    #: rule marked PROHIBITED_FOR_DECISION constrains the answer; it does not by
    #: itself mean the turn abstained, because the same rule can be explained
    #: perfectly well in general terms.
    final_disposition = models.CharField(
        max_length=24, choices=FinalDisposition.choices, blank=True, default=""
    )

    #: Why the answer stopped where it did. Server-set from a closed vocabulary —
    #: an adviser triages on these, so a free-text reason is a category nobody can
    #: count. Written once, from the final result, in the same transaction as the
    #: answer and its citations.
    reason_codes = models.JSONField(default=list, blank=True)

    #: Structured only, and `[]` when the runtime produces nothing. Never extracted
    #: from the Arabic answer: parsing prose for "what was missing" invents a
    #: machine-readable field out of a sentence written for a person.
    missing_information = models.JSONField(default=list, blank=True)

    #: Bumped when the meaning of a stored outcome changes, so a reader can tell a
    #: row written under different rules from one it can interpret. Rows written
    #: before this contract existed carry "", which is not any version.
    outcome_schema_version = models.CharField(max_length=16, blank=True, default="")

    model_name = models.CharField(max_length=120, blank=True, default="")
    model_revision = models.CharField(max_length=120, blank=True, default="")
    route = models.CharField(max_length=24, choices=ROUTE_CHOICES, blank=True, default="")
    prompt_version = models.CharField(max_length=40, blank=True, default="")

    # Set by the client per send. A retry after a dropped response reuses it, so a
    # network failure cannot turn one question into two stored turns.
    idempotency_key = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # sha256 of the request that produced this turn. The unique key alone cannot
    # tell a genuine retry from a different question sent under a reused key; with
    # this, the first is replayed and the second is refused.
    request_hash = models.CharField(max_length=64, blank=True, default="")

    # Without a status, a crash between saving the student's message and saving the
    # assistant's leaves a half-turn that is indistinguishable from a question still
    # being answered — so a retry either duplicates it or is wrongly refused.
    STATUS_PENDING = "PENDING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"
    STATUS_ABSTAINED = "ABSTAINED"
    STATUS_ESCALATED = "ESCALATED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_ABSTAINED, "Abstained"),
        (STATUS_ESCALATED, "Escalated"),
    ]
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_COMPLETED)

    # Which question this answers. Pairing by "first assistant row at or after the
    # question" is wrong the moment turns finish out of order — a resumed retry is
    # written AFTER answers to later questions, so the heuristic hands the student a
    # cited answer to a question they did not ask. Null on student turns, and on
    # assistant rows written before this column existed.
    in_reply_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="answers"
    )

    # When generation was claimed. A PENDING row is only trustworthy while something
    # is still working on it; if the worker is killed mid-call nothing ever moves it
    # on, and without an age the turn is stuck exactly as FAILED used to be.
    generation_started_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "advisor_messages"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "created_at"], name="idx_adv_msg_conv"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "idempotency_key"],
                condition=models.Q(idempotency_key__gt=""),
                name="uq_adv_msg_idempotency",
            )
        ]

    def __str__(self) -> str:
        return f"Message({self.id}/{self.role})"


class AdvisorMessageCitation(models.Model):
    """A SNAPSHOT of the citation shown, not a pointer to the current record.

    Policies get revised. A foreign key would silently rewrite history: an answer
    given under edition 3 page 24 would later display whatever that policy says
    now, and the student's record of the advice would no longer match the advice.
    The version hash makes a later divergence detectable rather than invisible.
    """

    VALID = "VALID"
    INVALID = "INVALID"
    VALIDATION_CHOICES = [(VALID, "Valid"), (INVALID, "Invalid")]

    message = models.ForeignKey(AdvisorMessage, on_delete=models.CASCADE, related_name="citations")
    policy_id = models.CharField(max_length=120)
    document_title = models.TextField(blank=True, default="")
    edition = models.CharField(max_length=60, blank=True, default="")
    page = models.CharField(max_length=40, blank=True, default="")

    effective_from = models.CharField(max_length=40, blank=True, default="")
    effective_to = models.CharField(max_length=40, blank=True, default="")
    authority_status = models.CharField(max_length=40, blank=True, default="")

    validation_status = models.CharField(max_length=16, choices=VALIDATION_CHOICES, default=VALID)
    source_version_hash = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "advisor_message_citations"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["message"], name="idx_adv_cit_message"),
            models.Index(fields=["policy_id"], name="idx_adv_cit_policy"),
        ]

    def __str__(self) -> str:
        return f"Citation({self.policy_id} p{self.page})"


class AdvisorFeedback(models.Model):
    """One current verdict per student per assistant message."""

    HELPFUL = "HELPFUL"
    NOT_HELPFUL = "NOT_HELPFUL"
    RATING_CHOICES = [(HELPFUL, "Helpful"), (NOT_HELPFUL, "Not helpful")]

    #: Offered on a negative rating. A closed set so the reasons can be counted;
    #: free text stays available for what the list does not cover.
    REASON_CODES = [
        "answer_incorrect",
        "did_not_understand_question",
        "information_outdated",
        "missing_details",
        "citation_not_helpful",
        "too_long",
        "needed_human_adviser",
    ]

    message = models.ForeignKey(AdvisorMessage, on_delete=models.CASCADE, related_name="feedback")
    student_id = models.IntegerField(db_index=True)
    rating = models.CharField(max_length=16, choices=RATING_CHOICES)
    reason_codes = models.JSONField(default=list, blank=True)
    comment = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "advisor_feedback"
        constraints = [
            models.UniqueConstraint(
                fields=["message", "student_id"], name="uq_adv_feedback_message_student"
            )
        ]
        indexes = [
            models.Index(fields=["rating"], name="idx_adv_fb_rating"),
        ]

    def __str__(self) -> str:
        return f"Feedback({self.message_id}/{self.rating})"


class AdvisorEscalation(models.Model):
    """A case handed to a human adviser, anchored to the turn that produced it.

    Anchored, not free-form: a support ticket that arrives without the question,
    the answer and the sources it rested on makes the adviser reconstruct the
    conversation before they can begin, and reconstruct it from the student's
    memory of it. The source message is therefore required, and the evidence
    travels with the case.

    `evidence_snapshot` is a FROZEN copy for the same reason the citation rows
    are: an escalation is a record of what was said at the time. If it were
    rebuilt from live policies and a live student record, a case opened today
    would quietly change its own facts before anyone read it — and the adviser
    would be answering a question the student never asked.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ASSIGNED = "ASSIGNED", "Assigned"
        NEEDS_INFORMATION = "NEEDS_INFORMATION", "Needs information"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"

    #: A case stops blocking a new one for the same turn once it reaches these.
    TERMINAL_STATUSES = (Status.RESOLVED, Status.CLOSED)

    class Reason(models.TextChoices):
        """Why this case was sent to a person.

        The SAME vocabulary the assistant turn uses, and deliberately not the same
        value. The message records why the ANSWER was limited; this records why a
        HUMAN was asked. A student who simply wants a person to look at a perfectly
        good answer produces STUDENT_REQUESTED here while the turn keeps whatever
        constrained it — which was nothing.

        Set by the server. A reason the student can choose is a reason the student
        can be wrong about, and it is what the adviser queue sorts on.
        """

        PROHIBITED_FOR_DECISION = (
            "PROHIBITED_FOR_DECISION",
            "The regulation reserves this decision to a person",
        )
        POLICY_NOT_FOUND = ("POLICY_NOT_FOUND", "No approved policy governs the question")
        POLICY_UNAVAILABLE = ("POLICY_UNAVAILABLE", "The policy store could not be consulted")
        STUDENT_DATA_MISSING = ("STUDENT_DATA_MISSING", "Required student facts were unavailable")
        PROCEDURE_NOT_DOCUMENTED = (
            "PROCEDURE_NOT_DOCUMENTED",
            "The procedure is not written down",
        )
        CONFLICTING_AUTHORITIES = (
            "CONFLICTING_AUTHORITIES",
            "Sources disagree and a person must choose",
        )
        JUDGE_REJECTED = ("JUDGE_REJECTED", "The answer did not survive review")
        MODEL_UNAVAILABLE = ("MODEL_UNAVAILABLE", "No answer could be produced")
        STUDENT_REQUESTED = ("STUDENT_REQUESTED", "The student asked for a human adviser")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    #: What the student and the adviser quote to each other. Stored rather than
    #: derived from the primary key: a reference that is a slice of the UUID hands
    #: out part of an internal identifier every time it is read aloud, and it is
    #: unreadable over a telephone. Allocated once, never changed — a case number
    #: that moves is worse than no case number.
    reference = models.CharField(max_length=24, unique=True, editable=False)

    # Real foreign keys: conversations and messages are OUR tables, so referential
    # integrity is free and a deleted conversation must not leave orphan cases.
    # `student_id` stays a bare integer — the same convention as the conversation
    # itself, because Student rows are re-imported wholesale and a cascade would
    # delete a student's case history on the next import.
    conversation = models.ForeignKey(
        AdvisorConversation, on_delete=models.CASCADE, related_name="escalations"
    )
    source_message = models.ForeignKey(
        AdvisorMessage, on_delete=models.CASCADE, related_name="escalations"
    )
    student_id = models.IntegerField(db_index=True)

    reason_code = models.CharField(max_length=32, choices=Reason.choices)
    student_note = models.TextField(blank=True, default="")

    #: Written for the adviser, from student-visible messages and an allowlist —
    #: never from the agent trace.
    generated_summary = models.TextField(blank=True, default="")
    evidence_snapshot = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=24, choices=Status.choices, default=Status.OPEN)
    assigned_adviser_id = models.CharField(max_length=64, blank=True, default="")

    #: Adviser-only. Never serialised to the student: the case is about them, the
    #: working notes on it are not addressed to them.
    adviser_notes = models.TextField(blank=True, default="")

    #: What the adviser decided, written TO the student. A separate field from the
    #: notes above and not a repurposing of them: the moment one field has to serve
    #: both audiences, the first request to show the student an outcome publishes
    #: the internal discussion that reached it.
    resolution_message = models.TextField(blank=True, default="")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="advisor_escalations_resolved",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "advisor_escalations"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["student_id", "-created_at"], name="idx_adv_esc_student"),
            models.Index(fields=["status", "-created_at"], name="idx_adv_esc_status"),
            models.Index(fields=["assigned_adviser_id"], name="idx_adv_esc_adviser"),
        ]
        constraints = [
            # One LIVE case per turn. Pressing the button twice is the same request,
            # not two, and two open cases for one question means two advisers can
            # answer it differently. Resolved and closed cases are excluded so a
            # question that comes back later can be raised again.
            models.UniqueConstraint(
                fields=["source_message"],
                condition=~models.Q(status__in=("RESOLVED", "CLOSED")),
                name="uq_adv_esc_one_open_per_message",
            )
        ]

    def __str__(self) -> str:
        return f"{self.reference}({self.status})"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = allocate_escalation_reference()
        super().save(*args, **kwargs)


class RateLimitBucket(models.Model):
    """One counter, shared by every worker.

    A row rather than a dict or a cache entry, because the count has to survive
    both a second gunicorn worker and a restart, and because it has to be
    incremented atomically — `select_for_update` on a row gives that, while the
    database cache backend's `incr` is a get followed by a set.
    """

    #: "<budget>:<student_id>". The budget names the RESOURCE, not the endpoint, so
    #: two doors onto the same expensive work cannot each be given a full allowance.
    key = models.CharField(max_length=200, primary_key=True)
    window_start = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)
    #: What was spent in the window before this one. Carried forward so a new
    #: window does not hand back the whole allowance at its boundary.
    previous_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "rate_limit_buckets"
        indexes = [models.Index(fields=["window_start"], name="idx_ratelimit_window")]

    def __str__(self) -> str:
        return f"{self.key}={self.count}"


class AdvisorReferenceCounter(models.Model):
    """One row per year, holding the last case number issued.

    Counting existing rows and adding one is the obvious approach and it is wrong:
    two students escalating at the same moment both read the same count and both
    claim the same number, and a case number that is not unique is not a reference
    at all. Allocation locks this row instead.
    """

    year = models.PositiveIntegerField(primary_key=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "advisor_reference_counters"

    def __str__(self) -> str:
        return f"{self.year}:{self.last_number}"


def allocate_escalation_reference(*, year: int | None = None) -> str:
    """The next case number, claimed under a row lock.

    Zero-padded and year-scoped so it reads over a telephone and sorts by hand:
    ADV-2026-00184.
    """
    from django.utils import timezone

    year = year or timezone.now().year
    with transaction.atomic():
        counter, _ = AdvisorReferenceCounter.objects.select_for_update().get_or_create(year=year)
        counter.last_number += 1
        counter.save(update_fields=["last_number"])
        return f"ADV-{year}-{counter.last_number:05d}"
