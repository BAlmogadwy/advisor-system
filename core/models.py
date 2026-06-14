import uuid

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
    MODE_CHOICES = (
        (MODE_OPTIMISE_CURRENT, "Optimise current"),
        (MODE_FULL_REBUILD, "Full rebuild"),
        (MODE_OPTIMISE_V2_FULL, "Optimise V2 (full rebuild)"),
        (MODE_OPTIMISE_V2_CURRENT, "Optimise V2 (current)"),
    )

    STAGE_CHOICES = (
        ("greedy", "greedy"),
        ("sa", "sa"),
        ("cpsat", "cpsat"),
        ("chain", "chain"),
        ("rooming_repair", "rooming_repair"),
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
