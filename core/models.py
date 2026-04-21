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


class ScenarioSectionBudget(models.Model):
    scenario = models.ForeignKey(
        TimetableScenario,
        on_delete=models.CASCADE,
        related_name="section_budgets",
    )
    course_code = models.TextField()
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
                fields=["scenario", "course_code"],
                name="ux_ssb_scenario_course",
            ),
        ]
        indexes = [
            models.Index(fields=["scenario"], name="idx_ssb_scenario"),
        ]

    def __str__(self) -> str:
        return f"Budget({self.scenario_id}/{self.course_code})"


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
    MODE_CHOICES = (
        (MODE_OPTIMISE_CURRENT, "Optimise current"),
        (MODE_FULL_REBUILD, "Full rebuild"),
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

    class Meta:
        db_table = "planner_jobs"
        indexes = [
            models.Index(fields=["scenario", "status"], name="idx_pj_scenario_status"),
            models.Index(fields=["submitted_by", "-submitted_at"], name="idx_pj_user_submitted"),
        ]

    def __str__(self) -> str:
        return f"PlannerJob({self.id}/{self.status})"
