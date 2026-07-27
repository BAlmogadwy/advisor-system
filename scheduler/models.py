"""Saved plans — the difference between an engine and a product.

Until this existed, `sch_plan` computed a timetable and **printed it**. Nothing
survived closing the terminal, which is precisely how the two previous greenfield
attempts in this project died: layers built beside the product, with no caller
and nothing to show for a run. The blueprint's governing rule is that every slice
must be usable end to end by itself, and a planner that cannot save a plan is not.

Two design points, both consequences of things measured on the way here:

**Every plan carries its fingerprints.** N8 originally promised byte-identical
reproducibility; that promise was retracted, because the only configurations that
deliver it produce timetables three to four times worse. With reproducibility
gone, fingerprints are no longer a nicety — they are the *only* way to know that
two plans answered the same question. A plan whose snapshot, rulebook or
configuration differs from another's is not a better or worse board; it is an
answer to a different question, and comparing them is meaningless.

**Nothing here is a `core` table.** These are `sch_*` tables owned entirely by
this app. The existing timetable engine cannot be regressed by anything stored
here, which is the whole basis on which this subsystem was allowed to exist.
"""

from __future__ import annotations

from django.db import models


class SchedulerPlan(models.Model):
    """One run of the planner, with everything needed to judge it later.

    Stored metrics are the ones a human actually asks about, and each is kept
    beside the floor it should be read against — "19 working days" means nothing
    without "and 19 was the proven minimum". A number without its floor invites
    the reader to imagine headroom that does not exist.
    """

    academic_year = models.TextField()
    term = models.IntegerField()
    gender = models.TextField()
    programs = models.TextField(help_text="comma-separated, as requested")

    created_at = models.DateTimeField(auto_now_add=True)
    label = models.TextField(blank=True, default="")

    # ── provenance (N8, as amended) ───────────────────────────────────────
    # Without reproducibility these are load-bearing: they are what makes two
    # plans comparable, or proves that they are not.
    snapshot_fingerprint = models.TextField()
    rulebook_fingerprint = models.TextField()
    config_fingerprint = models.TextField()
    config = models.JSONField(default=dict)

    # ── outcome ───────────────────────────────────────────────────────────
    solver_status = models.TextField()
    wall_time_seconds = models.FloatField(default=0.0)
    violation_count = models.IntegerField(default=0)
    certification = models.TextField(default="UNCERTIFIED")

    expected_clashes = models.FloatField(
        default=0.0, help_text="the optimised PROXY, not a student outcome"
    )
    naive_baseline = models.FloatField(default=0.0)

    instructor_days = models.IntegerField(default=0)
    instructor_days_floor = models.IntegerField(
        default=0, help_text="proven minimum; equal means it cannot be improved"
    )
    instructor_idle_minutes = models.IntegerField(default=0)
    sections_assigned = models.IntegerField(default=0)
    sections_staffable = models.IntegerField(
        default=0, help_text="only the department's own courses are ever staffed"
    )

    unroomed = models.IntegerField(default=0)
    unroomed_floor = models.IntegerField(
        default=0, help_text="impossible + saturated; no timetable can beat this"
    )

    sibling_pairs_back_to_back = models.IntegerField(default=0)
    sibling_pairs_achievable = models.IntegerField(default=0)

    # Null until someone asks for it: seating is a separate, slower confirmation.
    students_seated = models.IntegerField(null=True, blank=True)
    students_clash_free_percent = models.FloatField(null=True, blank=True)
    student_idle_minutes_avg = models.FloatField(null=True, blank=True)

    notes = models.JSONField(default=list)

    class Meta:
        db_table = "sch_plan"
        indexes = [
            models.Index(fields=["academic_year", "term", "gender"]),
            models.Index(fields=["snapshot_fingerprint"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.academic_year} T{self.term} {self.gender} ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def instructor_days_at_floor(self) -> bool:
        return self.instructor_days == self.instructor_days_floor

    @property
    def unroomed_at_floor(self) -> bool:
        return self.unroomed == self.unroomed_floor

    def comparable_to(self, other: SchedulerPlan) -> bool:
        """Do these two plans answer the same question?

        Reproducibility was retracted, so two runs of the same configuration will
        differ. That is expected and fine. What is NOT fine is comparing runs
        whose inputs, rules or settings differ and reading the difference as
        quality — which is exactly the mistake that produced two retracted
        conclusions while this subsystem was being built.
        """
        return (
            self.snapshot_fingerprint == other.snapshot_fingerprint
            and self.rulebook_fingerprint == other.rulebook_fingerprint
            and self.config_fingerprint == other.config_fingerprint
        )


class SchedulerPlacement(models.Model):
    """One meeting of one section, at a time, in a room (or explicitly not).

    ``room_id`` being null is a first-class outcome, not missing data (D7): a
    room shortage never blocks the build, and ten of the male cohort's meetings
    can never be roomed by any timetable because no lab is large enough.
    """

    plan = models.ForeignKey(SchedulerPlan, on_delete=models.CASCADE, related_name="placements")
    section_id = models.TextField()
    offering_id = models.TextField()
    course_code = models.TextField()
    section_label = models.TextField()
    meeting_index = models.IntegerField()

    kind = models.TextField()
    delivery = models.TextField()
    day = models.TextField()
    start_minute = models.IntegerField()
    end_minute = models.IntegerField()

    room_id = models.TextField(blank=True, default="")
    instructor_id = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "sch_placement"
        indexes = [
            models.Index(fields=["plan", "day"]),
            models.Index(fields=["plan", "instructor_id"]),
        ]
        ordering = ["day", "start_minute", "course_code"]

    def __str__(self) -> str:
        return f"{self.course_code} {self.section_label} {self.day} {self.start_minute}"
