"""
core/services/timetable_generate.py
Cohort-based timetable workspace generation.

This module is the main entry point for creating a fully scaffolded
TimetableScenario from scratch.  The pipeline:

  1. Run the batch recommender for every student in the target program(s)
     to determine which courses each student should take next semester.
  2. Filter out non-schedulable courses (capstone, training, free electives).
  3. Classify each student by their "primary term" — the curriculum term
     (e.g. Term 3, Term 5) where the majority of their recommended courses
     fall.  Students whose courses span multiple terms are flagged as
     "cross-term".
  4. Compute a section plan (how many sections of each course are needed)
     using demand counts and capacity rules from section_planning.
  5. Create the TimetableScenario, one DeliveryBoard per active term level,
     and persist:
       - ScenarioStudentMap  (student classification records)
       - ScenarioStudentCourseRequest  (normalised per-course demand rows)
       - ScenarioSectionBudget  (planned sections per course)
       - BoardStudentLink  (primary + visitor links between students
         and boards)
  6. Run the auto-placement engine to produce an initial draft timetable.

The single public function is ``generate_workspace_scenario()``.

Dependencies:
  - core.services.recommender_batch  (batch_recommend, batch_recommend_multi_program)
  - core.services.section_planning   (capacity rules, section plan computation)
  - core.services.timetable_autoplace (auto_place_scenario, get_meeting_pattern)
  - core.services.timetable_workspace (compute_scenario_budget)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping

from core.models import (
    BoardStudentLink,
    DeliveryBoard,
    ProgrammeRequirement,
    ScenarioSectionBudget,
    ScenarioStudentMap,
    SectionPlacement,
    TimeSlotTemplate,
    TimetableScenario,
)
from core.services.course_identity import planner_course_key
from core.services.reporting import get_student_ids
from core.services.section_planning import (
    DEFAULT_MAX_EXTERNAL,
    DEFAULT_MAX_LOCAL_4CR,
    DEFAULT_MAX_LOCAL_OTHER,
    compute_plan_summary,
    compute_section_plan,
    fold_slot_limits,
    lowest_declared_capacities,
    slot_declared_limits,
)
from core.services.student_helpers import normalize_code
from core.services.timetable_demand import sync_scenario_student_course_requests

# ---------------------------------------------------------------------------
# Course exclusion rules
# ---------------------------------------------------------------------------
# Certain courses must never appear on a timetable board because they are
# not delivered in a regular classroom setting.  Three exclusion layers:
#
# 1. EXCLUDED_COURSE_CODES — explicit course codes (currently empty; can be
#    populated with codes like "FE1", "FE2" for free-elective placeholders).
# 2. EXCLUDED_SUFFIXES — course numbers ending with these digits are
#    capstone / graduation-project sequences (e.g. CS490, CS491, CS492).
# 3. EXCLUDED_NAME_KEYWORDS / EXCLUDED_NAME_PATTERNS_PROJECT — fuzzy match
#    against the course name (English and Arabic) to catch training,
#    cooperative, internship, and graduation-project courses. The name is the
#    student's own programme plan's name for that code (the catalogue
#    description only when the plan has none): one display code can name two
#    courses, e.g. CYB386 is CYP's internship but CYP2's machine-learning class.
#
# The helper ``_is_excluded_course()`` applies all three layers.
# ---------------------------------------------------------------------------
EXCLUDED_COURSE_CODES: frozenset[str] = frozenset()  # None currently hardcoded
EXCLUDED_SUFFIXES = ("101", "490", "491", "492")
EXCLUDED_NAME_KEYWORDS = frozenset(
    {
        "graduation",
        "practical training",
        "training",
        "cooperative",
        "co-operative",
        "coop",
        "internship",
        "تخرج",
        "مشروع تخرج",
        "تدريب",
    }
)
# Patterns that indicate a capstone/project course.
# Note: plain "project" is intentionally omitted to avoid false-positives
# like "Project Management" — only multi-word capstone patterns are listed.
EXCLUDED_NAME_PATTERNS_PROJECT = ("project i", "project ii", "graduation project")


def _is_excluded_course(code: str, course_name: object) -> bool:
    """Determine whether a course should be excluded from timetable scheduling.

    A course is excluded if **any** of the following are true:

    1. Its normalised code is in the ``EXCLUDED_COURSE_CODES`` set.
    2. The numeric portion of the code ends with one of the
       ``EXCLUDED_SUFFIXES`` (490, 491, 492 — capstone sequences).
    3. ``course_name`` contains any of the ``EXCLUDED_NAME_KEYWORDS`` or
       ``EXCLUDED_NAME_PATTERNS_PROJECT``.

    Args:
        code: Raw course code string (e.g. "CS 490", "ARAB101").
        course_name: The name of THIS course identity -- the student's
            programme plan name for ``code`` (catalogue description only
            when the plan has none). Never looked up by the bare code: a
            display code is not a course identity.

    Returns:
        True if the course should be kept off the timetable, False otherwise.
    """
    c = normalize_code(code)

    # Layer 1: explicit code blacklist
    if c in EXCLUDED_COURSE_CODES:
        return True

    # Layer 2: numeric suffix check (e.g. "490" in "CS490")
    digits = "".join(ch for ch in c if ch.isdigit())
    if digits.endswith(EXCLUDED_SUFFIXES):
        return True

    # Layer 3: keyword / pattern match against this identity's name
    name = str(course_name or "").lower()
    if name:
        if any(kw in name for kw in EXCLUDED_NAME_KEYWORDS):
            return True
        if any(pat in name for pat in EXCLUDED_NAME_PATTERNS_PROJECT):
            return True
    return False


def _resolve_elective_slots(
    all_recs: dict[int, list[str]],
    elective_map: Mapping[tuple[str, str], list],
    programme_of: Callable[[int], str],
    passed: Mapping[int, set[str]],
    studying: Mapping[int, set[str]],
) -> tuple[dict[int, list[str]], list[tuple[int, str, str]], list[tuple[int, str, str]]]:
    """Turn each student's elective placeholders into the real courses they take.

    The recommender returns plan codes as they stand (``DS1``, ``AI2``). A
    placeholder is a *slot*: one course the student must pick from the list the
    department mapped onto it. This resolves every slot a student still owes and
    reports the ones that cannot be filled.

    Two rules, and the second is why this is one function rather than two:

    * **One course per slot.** A student eligible for 13 electives needs one of
      them, not 13.
    * **A course fills the slot it was picked for.** Expanding first and
      assigning afterwards loses which slots the student owed, so a course that
      several slots offer was credited to the lowest-numbered one — a student who
      had passed ``DS1`` and owed only ``DS3`` had their course booked against
      ``DS1``, at ``DS1``'s term. The slot decides the section's size
      (:func:`slot_declared_limits`) and its term, so that was wrong twice over.

    A student is never given one course for two of their own slots: nobody clears
    two requirements by sitting one class, and a course they have passed or are
    sitting now fills nothing. A slot left with no course of its own stays
    **unserved** and is returned with the reason, because the alternative is what
    used to happen — it vanished, and with it the demand, the sections and any
    sign that something was missing. Demand is one count per owed slot, so a
    student owing two gets two courses. The slots are matched to courses as a
    whole (:func:`_match_slots_to_courses`) rather than one at a time, so a slot
    is called unserved only when no assignment could have filled it.

    Students are spread across the courses a slot offers, least-used first, so
    demand does not pile onto whichever course happens to sort first.

    Args:
        all_recs: student id -> recommended codes, placeholders included.
        elective_map: (programme, placeholder) -> the mapped elective rows.
            Keyed by programme as well as slot: ``AI3`` is term 10 in the AI plan
            and term 9 in AI2, and the two plans may offer different courses.
        programme_of: a student's programme code.
        passed: student id -> normalised codes already passed.
        studying: student id -> normalised codes being taken now.

    Returns:
        ``(recs, picks, unserved)``. ``recs`` is ``all_recs`` with each
        placeholder replaced by the course picked for it; ``picks`` is one
        ``(student, slot, course)`` per filled slot; ``unserved`` is one
        ``(student, slot, reason)`` per slot no course could fill, the reason
        being ``no_eligible_course`` or ``courses_exhausted``.
    """
    picks: list[tuple[int, str, str]] = []
    unserved: list[tuple[int, str, str]] = []
    if not elective_map:
        return all_recs, picks, unserved

    slots_by_programme: dict[str, set[str]] = defaultdict(set)
    for programme, slot in elective_map:
        slots_by_programme[programme].add(slot)

    # Global tally, so the first student takes the first course, the second the
    # next, and no single course absorbs a whole cohort.
    assigned_count: Counter[str] = Counter()

    for sid, recs in all_recs.items():
        programme = programme_of(int(sid))
        owned_slots = slots_by_programme.get(programme)
        if not owned_slots:
            continue

        kept: list[str] = []
        owed: list[str] = []
        for code in recs:
            norm = normalize_code(code)
            if norm in owned_slots:
                if norm not in owed:
                    owed.append(norm)
            else:
                kept.append(code)
        if not owed:
            continue

        student_passed = set(passed.get(int(sid), ()))
        student_studying = set(studying.get(int(sid), ()))
        already_done = student_passed | student_studying

        # What each owed slot could be filled with, least-used course first so
        # demand spreads. Sorted slots keep a rebuild of the same data stable.
        candidates: dict[str, list[str]] = {}
        for slot in sorted(owed):
            eligible = [
                code
                for code in dict.fromkeys(
                    normalize_code(ec.course_code)
                    for ec in elective_map.get((programme, slot), ())
                    if all(
                        p in student_passed or p in student_studying
                        for p in (
                            normalize_code(p) for p in ec.prerequisites_csv.split(",") if p.strip()
                        )
                    )
                )
                # A course already passed, or being taken right now, cannot fill
                # a slot: the student would be sitting it twice.
                if code not in already_done
            ]
            candidates[slot] = sorted(eligible, key=lambda c: (assigned_count[c], c))

        matched = _match_slots_to_courses(candidates)
        for slot in sorted(owed):
            pick = matched.get(slot)
            if pick is None:
                # Said apart so the report can tell "nothing this student can
                # take" from "a course existed but their other slots took it".
                reason = "courses_exhausted" if candidates[slot] else "no_eligible_course"
                unserved.append((int(sid), slot, reason))
                continue
            assigned_count[pick] += 1
            picks.append((int(sid), slot, pick))
            kept.append(pick)

        all_recs[sid] = kept

    return all_recs, picks, unserved


def _match_slots_to_courses(candidates: Mapping[str, list[str]]) -> dict[str, str]:
    """Give as many of one student's slots as possible a course of their own.

    Taking each slot's best course in turn can strand a later slot whose only
    course an earlier one took, when the earlier slot had another course it
    could just as well have had: ``DS1`` offering A and B and ``DS3`` offering
    only A is served in full by DS1->B, DS3->A, but handing A to DS1 first
    leaves DS3 with nothing. That mattered little while an unfillable slot was
    dropped in silence; now that it is reported, a greedy pass would send the
    department looking for a course to map that it does not actually need.

    So this is a maximum bipartite matching (Kuhn's algorithm) over a handful of
    slots, which is exact. Each slot's list is already in preference order, and
    augmenting paths are followed in that order, so the result stays stable and
    still favours the least-used courses.
    """
    course_of_slot: dict[str, str] = {}
    slot_of_course: dict[str, str] = {}

    def augment(slot: str, seen: set[str]) -> bool:
        for course in candidates.get(slot, ()):
            if course in seen:
                continue
            seen.add(course)
            holder = slot_of_course.get(course)
            if holder is None or augment(holder, seen):
                slot_of_course[course] = slot
                course_of_slot[slot] = course
                return True
        return False

    for slot in sorted(candidates):
        augment(slot, set())
    return course_of_slot


def _summarise_unserved_slots(
    unserved: list[tuple[int, str, str]],
    programme_of: Callable[[int], str],
    placeholder_terms: Mapping[tuple[str, str], int],
) -> list[dict]:
    """Group unfilled elective slots into one row per (programme, slot, reason).

    Each row carries the term the slot sits at, so a reader can see which board
    will have nothing for it, and why — the two reasons call for different
    answers and must not be read as one number:

    ``no_eligible_course``
        This student can take none of the courses the slot offers: they have not
        met a prerequisite, or they have already taken them. Nothing for the
        department to do; the student is simply not ready for it this term.
    ``courses_exhausted``
        The slot's courses all went to other slots the same student owes. One
        course cannot clear two requirements, so the department has to map a
        further course onto the slot before anyone can be given it.
    """
    counts: Counter[tuple[str, str, str]] = Counter()
    for sid, slot, reason in unserved:
        counts[(programme_of(int(sid)), slot, reason)] += 1
    return [
        {
            "programme": programme,
            "slot": slot,
            "programme_term": placeholder_terms.get((programme, slot)),
            "reason": reason,
            "students": n,
        }
        for (programme, slot, reason), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def default_generate_slots() -> tuple[list[dict], list[dict]]:
    """The lecture and lab columns a new draft gets when none are chosen.

    Lectures: the admin-configured default template, else ``DEFAULT_SLOTS``.
    Labs: ``DEFAULT_LAB_SLOTS`` (with the online evening windows). The Generate
    dialog prefills its time slots from these, so what it shows as the defaults
    is what a draft generated without its own columns gets.
    """
    from core.services.timetable_autoplace import DEFAULT_LAB_SLOTS, DEFAULT_SLOTS

    lectures: list[dict] = []
    default_tpl = TimeSlotTemplate.objects.filter(is_default=True).first()
    if default_tpl and isinstance(default_tpl.slots, list):
        lectures = [dict(slot) for slot in default_tpl.slots if isinstance(slot, dict)]
    return (
        lectures or [dict(slot) for slot in DEFAULT_SLOTS],
        [dict(slot) for slot in DEFAULT_LAB_SLOTS],
    )


def generate_workspace_scenario(
    year: int,
    semester: int,
    program: str | list[str],
    section: str | None = None,
    scenario_name: str = "",
    strategy: str = "compact",
    max_local_4cr: int = DEFAULT_MAX_LOCAL_4CR,
    max_local_other: int = DEFAULT_MAX_LOCAL_OTHER,
    max_external: int = DEFAULT_MAX_EXTERNAL,
    course_overrides: dict[str, int] | None = None,
    created_by: str = "",
    run_autoplace: bool = True,
    slot_config: list[dict] | None = None,
    lab_slot_config: list[dict] | None = None,
) -> dict:
    """Create a fully scaffolded timetable workspace scenario end-to-end.

    This is the main orchestrator.  It runs the full pipeline described in
    the module docstring: recommend -> classify -> plan sections -> create
    scenario/boards -> persist records -> auto-place.

    Args:
        year: Academic year (e.g. 1446).
        semester: Semester number (1, 2, or 3 for summer).
        program: Single programme code (``"AI"``) **or** a list of codes
            (``["AI", "DS", "CS"]``) for multi-program generation.
        section: Optional section filter (e.g. ``"1"``).  When provided,
            only students in that section are included.
        scenario_name: Human-readable label for the scenario.  Auto-
            generated from program/semester/timestamp when left blank.
        max_local_4cr: Max students per section for local 4-credit courses.
        max_local_other: Max students per section for local non-4-credit
            courses.
        max_external: Max students per section for external (service)
            courses.
        course_overrides: Optional dict mapping course codes to explicit
            section counts, bypassing the demand-based computation.
        created_by: Username or identifier stored on the scenario record.

    Returns:
        A dict containing:
          - ``scenario``: metadata dict (id, year, term, name, status, ...).
          - ``boards``: list of board summary dicts, one per term level,
            each with student counts, courses, and auto-placement stats.
          - ``section_budget``: enriched budget with meeting-pattern info.
          - ``section_plan``: raw section plan from ``compute_section_plan``.
          - ``plan_summary``: aggregate section-plan statistics.
          - ``student_summary``: counts of total, classified, on-plan,
            and cross-term students broken down by term level.

    Business rules:
      - Non-schedulable courses (capstone, training, co-op) are filtered out
        before any section planning — see ``_is_excluded_course()``.
      - A student's **primary term** is the curriculum term that appears most
        often among their recommended courses.  They are linked to that
        term's board as ``"primary"``.
      - Cross-term students also receive ``"visitor"`` links to every other
        board whose courses they need.
      - The auto-placement engine runs at the end to produce a first-draft
        timetable that can be manually adjusted afterward.
    """

    # ── Step 1: Collect recommendations (batch-optimized) ─────────
    # Run the recommender for every student in the target program(s).
    # The batch variants pre-load shared data (prerequisites, programme
    # requirements) once, avoiding N+1 queries.

    from core.services.recommender_batch import batch_recommend, batch_recommend_multi_program

    # Normalize ``program`` to a list so the rest of the function can
    # treat single-program and multi-program cases uniformly.
    programs = [program] if isinstance(program, str) else list(program)
    program_label = ",".join(programs)

    student_ids = get_student_ids(
        program=programs if len(programs) > 1 else programs[0],
        section=section,
    )

    # Single-program uses a faster code path; multi-program must resolve
    # each student's individual programme before recommending.
    if len(programs) == 1:
        all_recs = batch_recommend(student_ids, programs[0], year, semester)
    else:
        all_recs = batch_recommend_multi_program(student_ids, year, semester)

    from core.models import Course, Student

    student_program_map: dict[int, str] = {}
    for sid, prog in Student.objects.filter(student_id__in=student_ids).values_list(
        "student_id", "program"
    ):
        if prog:
            student_program_map[int(sid)] = str(prog)

    course_name_by_program: dict[str, dict[str, str]] = defaultdict(dict)
    course_meta_by_program: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for row in ProgrammeRequirement.objects.filter(program__in=programs).values(
        "program", "course_code", "course_name", "credit_hours", "type"
    ):
        prog = str(row["program"])
        code = normalize_code(row["course_code"])
        if not code:
            continue
        name = str(row.get("course_name") or "").strip()
        course_name_by_program[prog][code] = name
        course_meta_by_program[prog][code] = {
            "course_code": code,
            "course_name": name,
            "credit_hours": row.get("credit_hours") or 3,
            "department": "".join(ch for ch in code if ch.isalpha()),
        }

    course_fallback_names = {
        normalize_code(code): description or ""
        for code, description in Course.objects.values_list("course_code", "description")
    }

    def _student_program(student_id: int) -> str:
        if len(programs) == 1:
            return programs[0]
        return student_program_map.get(student_id, "")

    def _course_meta_for_student(student_id: int, code: object) -> dict[str, object]:
        norm_code = normalize_code(code)
        prog = _student_program(int(student_id))
        meta = dict(course_meta_by_program.get(prog, {}).get(norm_code, {}))
        if not meta:
            meta = {
                "course_code": norm_code,
                "course_name": course_fallback_names.get(norm_code, ""),
                "credit_hours": 3,
                "department": "".join(ch for ch in norm_code if ch.isalpha()),
            }
        if not meta.get("course_name"):
            meta["course_name"] = course_fallback_names.get(norm_code, "")
        meta["course_key"] = planner_course_key(norm_code, meta.get("course_name"))
        return meta

    # ── Step 1b: Replace elective placeholders with real courses ──
    # batch_recommend() returns plan codes as-is (AI1, CS1). Here we resolve
    # them to real elective courses based on student eligibility.
    from core.models import ElectiveTermMapping, StudentCourse

    etm_qs = ElectiveTermMapping.objects.filter(
        academic_year=str(year),
        term=semester,
        programme__in=programs,
    ).select_related("elective")

    # Keyed by (programme, placeholder), never the bare placeholder code: a plan
    # names its own slots and may offer its own courses for them, and the same
    # code sits at different terms in different plans (``AI3`` is term 10 in AI
    # and term 9 in AI2). One shared key let the first plan loaded decide both
    # the course list and the term for every other plan.
    elective_map: dict[tuple[str, str], list] = defaultdict(list)
    for m in etm_qs:
        elective_map[(str(m.programme), normalize_code(m.placeholder_code))].append(m.elective)

    # Each slot's own term, from the plan that declares it. One query, not one
    # per mapping row.
    placeholder_terms: dict[tuple[str, str], int] = {}
    if elective_map:
        for prog, code, pt in (
            ProgrammeRequirement.objects.filter(
                program__in=sorted({p for p, _ in elective_map}),
                course_code__in=sorted({s for _, s in elective_map}),
                # A term of 0 is not a term; the old lookup rejected it too.
                programme_term__gte=1,
            )
            # Ordered and first-one-wins, so two rows for one slot cannot make
            # the term depend on the order the database happens to return.
            .order_by("program", "course_code", "programme_term")
            .values_list("program", "course_code", "programme_term")
        ):
            placeholder_terms.setdefault((str(prog), normalize_code(code)), int(pt))

    slot_picks: list[tuple[int, str, str]] = []
    unserved_slots: list[tuple[int, str, str]] = []
    if elective_map:
        # Load passed/studying courses for eligibility checking
        sc_qs = StudentCourse.objects.filter(
            student_id__in=list(all_recs.keys()),
        ).select_related("course")
        stu_passed: dict[int, set[str]] = defaultdict(set)
        stu_studying: dict[int, set[str]] = defaultdict(set)
        for sc in sc_qs:
            code = normalize_code(sc.course.course_code)
            if sc.status == "passed":
                stu_passed[sc.student_id].add(code)
            elif sc.status == "studying":
                stu_studying[sc.student_id].add(code)

        all_recs, slot_picks, unserved_slots = _resolve_elective_slots(
            all_recs,
            elective_map,
            _student_program,
            stu_passed,
            stu_studying,
        )

    # The term a resolved elective counts at is its SLOT's term, per student.
    # One course may fill slots at two different terms (DS482 is offered for
    # both DS1 at term 8 and DS3 at term 10), and a single course->term map
    # cannot hold that: whichever slot was written last won, so the course was
    # budgeted at one arbitrary term and the other term got nothing.
    elective_pick_terms: dict[tuple[int, str], int] = {}
    for pick_sid, pick_slot, pick_course in slot_picks:
        pick_term = placeholder_terms.get((_student_program(int(pick_sid)), pick_slot))
        if pick_term is not None:
            elective_pick_terms[(int(pick_sid), normalize_code(pick_course))] = pick_term

    # Filter out non-schedulable courses (capstone, training, etc.) and
    # build three structures:
    #   - ``aggregate``: Counter of planner_course_key -> total demand.
    #   - ``student_recs``: visible registrar course codes for classification.
    #   - ``student_course_keys``: planner identities for scheduling.
    aggregate: Counter[str] = Counter()
    student_recs: dict[int, list[str]] = {}
    student_course_keys: dict[int, list[str]] = {}
    course_metadata: dict[str, dict[str, object]] = {}

    for sid, recs in all_recs.items():
        # Exclusion is decided per course identity: the student's own plan
        # names the course, so CYP's CYB386 internship drops out while CYP2's
        # CYB386 machine-learning class stays.
        filtered: list[str] = []
        keys: list[str] = []
        for code in recs:
            meta = _course_meta_for_student(int(sid), code)
            if _is_excluded_course(code, meta.get("course_name")):
                continue
            key = str(meta["course_key"])
            course_metadata.setdefault(key, meta)
            filtered.append(code)
            keys.append(key)
        if filtered:
            aggregate.update(keys)
            student_recs[sid] = filtered
            student_course_keys[sid] = keys

    # ── Step 2: Classify students by primary term ────────────────
    # Each course in ProgrammeRequirement has a ``programme_term`` (the
    # curriculum term it nominally belongs to, e.g. Term 3).  We use
    # this to figure out which term level each student "belongs to" by
    # counting how many of their recommended courses fall in each term
    # and picking the mode (most common term).

    # Per-program term maps: essential for multi-program scenarios.
    # Example: IS plan has FE1 at term 7, IS2 plan has FE1 at term 8.
    # Without scoping, the merged map would assign FE1 to whichever
    # program's row was loaded last, misclassifying students from the
    # other program into the wrong term level (e.g. IS students landing
    # on even-term boards 4/8 instead of their correct odd-term 3/5/7/9).
    pr_qs = ProgrammeRequirement.objects.filter(program__in=programs).values_list(
        "program", "course_code", "programme_term"
    )
    course_term_by_program: dict[str, dict[str, int]] = defaultdict(dict)
    for prog, code, pt in pr_qs:
        if pt is not None:
            course_term_by_program[prog][normalize_code(code)] = pt

    # A resolved elective's term is NOT written into these maps. It belongs to
    # the slot the student was given it for, which ``elective_pick_terms`` holds
    # per student, so one course can count at two terms for two students.

    # Fallback: merged map for students whose program is unknown
    course_term_map: dict[str, int] = {}
    for prog_map in course_term_by_program.values():
        course_term_map.update(prog_map)

    # ``student_program_map`` was loaded before recommendation expansion so
    # both classification and planner identity use the same programme source.

    classified: list[dict] = []
    term_student_counts: Counter[int] = Counter()

    for sid, recs in student_recs.items():
        # Use the student's own program's term map when available;
        # fall back to the merged map for unknown programs.
        student_prog = student_program_map.get(sid)
        term_map = (
            course_term_by_program.get(student_prog, course_term_map)
            if student_prog
            else course_term_map
        )

        # Count how many of this student's recommended courses fall in
        # each curriculum term.
        term_counts: Counter[int] = Counter()
        for code in recs:
            norm = normalize_code(code)
            pt = elective_pick_terms.get((int(sid), norm), term_map.get(norm))
            if pt is not None:
                term_counts[pt] += 1

        # Students with no classifiable courses are skipped (e.g. all
        # their courses are electives without a programme_term).
        if not term_counts:
            continue

        # Primary term = the term with the most recommended courses.
        primary_term = term_counts.most_common(1)[0][0]
        # Cross-term = student needs courses from more than one term
        # level.  These students will get "visitor" board links later.
        is_cross_term = len(term_counts) > 1

        classified.append(
            {
                "student_id": sid,
                "primary_term": primary_term,
                "is_cross_term": is_cross_term,
                "recommended_courses": recs,
                "term_counts": dict(term_counts),
            }
        )
        term_student_counts[primary_term] += 1

    course_key_term_counts: dict[str, Counter[int]] = defaultdict(Counter)
    for sid, recs in student_recs.items():
        student_prog = student_program_map.get(sid)
        term_map = (
            course_term_by_program.get(student_prog, course_term_map)
            if student_prog
            else course_term_map
        )
        for code, key in zip(recs, student_course_keys.get(sid, []), strict=False):
            norm = normalize_code(code)
            pt = elective_pick_terms.get((int(sid), norm), term_map.get(norm))
            if pt is not None:
                course_key_term_counts[key][pt] += 1

    # ── Step 3: Compute section plan (section budget) ─────────────
    # The section plan determines how many sections of each course are
    # needed, based on aggregate student demand and per-section capacity
    # limits.  Capacity limits differ by department locality and credit
    # hours (see section_planning.py for the rules).

    # Load programme-specific capacity overrides.  When a course appears
    # in multiple programs with different capacities, take the minimum
    # to respect the most restrictive constraint.  The rule lives in
    # section_planning so Section Planning sizes pooled programmes the same way.
    programme_capacities: dict[str, int] = (
        lowest_declared_capacities(programs, course_metadata) if aggregate else {}
    )

    # Normalize any user-supplied course overrides so they match the
    # canonical upper-case-no-spaces format used everywhere else.
    norm_overrides: dict[str, int] | None = None
    if course_overrides:
        norm_overrides = {normalize_code(k): v for k, v in course_overrides.items()}

    # A resolved elective is taught for its slot: AI463 filling AI's AI1 slot
    # (30 seats) is sized at 30, not by its own rule. Section Planning applies
    # the same two functions, so its numbers are the sections built here.
    if aggregate and slot_picks:
        slot_limits = slot_declared_limits(
            ((_student_program(int(sid)), slot, course, 1) for sid, slot, course in slot_picks),
            norm_overrides or {},
        )
        fold_slot_limits(programme_capacities, course_metadata, slot_limits, norm_overrides or {})

    plan = compute_section_plan(
        aggregate,
        max_local_4cr=max_local_4cr,
        max_local_other=max_local_other,
        max_external=max_external,
        course_overrides=norm_overrides,
        programme_capacities=programme_capacities,
        course_metadata=course_metadata,
    )
    summary = compute_plan_summary(plan)

    # ── Step 4: Create scenario + boards ─────────────────────────
    # A TimetableScenario is the top-level container.  Each scenario
    # holds one DeliveryBoard per active curriculum-term level (e.g.
    # "Term 3", "Term 5").  Boards group students and section
    # placements that belong to the same cohort.

    # Auto-generate a descriptive name when none is supplied.
    if not scenario_name:
        import datetime

        ts = datetime.datetime.now().strftime("%H%M%S")
        sec_label = f" {section}" if section else ""
        scenario_name = f"{program_label}{sec_label} T{semester} Draft {ts}"

    # Resolve the time-slot grid: the caller's columns (the Generate dialog's
    # own time slots, validated by the view), else the generator's defaults.
    default_lectures, default_labs = default_generate_slots()
    lecture_columns: list[object] = list(slot_config or default_lectures)
    lab_columns: list[object] = list(lab_slot_config or default_labs)

    scenario = TimetableScenario.objects.create(
        academic_year=str(year),
        term=str(semester),
        name=scenario_name,
        slot_config=lecture_columns,
        lab_slot_config=lab_columns,
        created_by=created_by,
        # Structured cohort identity so the planner resolves course instructors
        # without parsing the scenario name. Only M/F are valid gender values.
        gender=(section if section in ("M", "F") else ""),
        programs=programs,
    )

    # Create one DeliveryBoard per active term level.  "Active" means
    # at least one classified student has that term as their primary.
    active_terms = sorted(term_student_counts.keys())
    board_by_term: dict[int, DeliveryBoard] = {}

    # For multi-program scenarios the board's ``program`` field stores
    # the comma-joined label (e.g. "AI,DS,CS").
    board_program = programs[0] if len(programs) == 1 else program_label
    for pt in active_terms:
        board = DeliveryBoard.objects.create(
            scenario=scenario,
            label=f"Term {pt}",
            nominal_term=pt,
            board_type="standard",
            program=board_program,
            target_size=term_student_counts[pt],
            display_order=pt,
        )
        board_by_term[pt] = board

    # ── Step 5: Persist classification + budget + links ──────────
    # Three bulk-create operations store the generated data so it can
    # be queried later by the workspace UI and the auto-placer.

    # 5a. ScenarioStudentMap — one row per classified student, recording
    #     their primary term, cross-term flag, and recommended courses.
    ssm_objs = [
        ScenarioStudentMap(
            scenario=scenario,
            student_id=s["student_id"],
            primary_term=s["primary_term"],
            is_cross_term=s["is_cross_term"],
            recommended_courses=s["recommended_courses"],
            recommended_course_keys=student_course_keys.get(
                s["student_id"], s["recommended_courses"]
            ),
        )
        for s in classified
    ]
    ScenarioStudentMap.objects.bulk_create(ssm_objs, ignore_conflicts=True)
    sync_scenario_student_course_requests(
        scenario=scenario,
        classified_students=classified,
        student_course_keys=student_course_keys,
    )

    # 5b. ScenarioSectionBudget — one row per course, recording the
    #     number of planned sections, capacity per section, total
    #     demand, and the curriculum term the course belongs to.
    budget_objs = []
    for entry in plan:
        course_key = str(entry.get("course_key") or entry["course_code"])
        code = entry["course_code"]
        # Look up which curriculum term this course belongs to so it
        # can be associated with the correct board later.
        # For same-code/different-name courses, use the term where this
        # exact planner key has the most student demand.
        key_terms = course_key_term_counts.get(course_key, Counter())
        if key_terms:
            pt = key_terms.most_common(1)[0][0]
        else:
            norm_code = normalize_code(code)
            pt_candidates = [
                prog_map[norm_code]
                for prog_map in course_term_by_program.values()
                if norm_code in prog_map
            ]
            pt = max(set(pt_candidates), key=pt_candidates.count) if pt_candidates else None
        budget_objs.append(
            ScenarioSectionBudget(
                scenario=scenario,
                course_key=course_key,
                course_code=code,
                course_name=str(entry.get("course_name") or ""),
                department=entry.get("department", ""),
                credit_hours=entry.get("credit_hours", 0),
                planned_sections=entry["num_sections"],
                max_per_section=entry["max_per_section"],
                total_demand=entry["total_students"],
                programme_term=pt,
            )
        )
    ScenarioSectionBudget.objects.bulk_create(budget_objs, ignore_conflicts=True)

    # 5c. BoardStudentLink — connects students to boards.
    #     Two link types:
    #       "primary" — the student's main board (their primary term).
    #       "visitor" — boards the student must also attend because
    #                   some of their courses belong to a different
    #                   term level (cross-term students only).
    bsl_objs = []
    for s in classified:
        # Every classified student gets exactly one primary link.
        primary_board = board_by_term.get(s["primary_term"])
        if primary_board:
            bsl_objs.append(
                BoardStudentLink(
                    board=primary_board,
                    student_id=s["student_id"],
                    link_type="primary",
                )
            )

        # Cross-term students additionally get visitor links to every
        # other board whose courses they need.
        if s["is_cross_term"]:
            stu_prog = student_program_map.get(s["student_id"])
            stu_term_map = (
                course_term_by_program.get(stu_prog, course_term_map)
                if stu_prog
                else course_term_map
            )
            for code in s["recommended_courses"]:
                norm = normalize_code(code)
                # The slot's term again: a resolved elective has no entry in the
                # course->term maps, so without this a student counted as
                # cross-term BECAUSE of their elective got no link to the board
                # that elective sits on.
                ct = elective_pick_terms.get((int(s["student_id"]), norm), stu_term_map.get(norm))
                if ct and ct != s["primary_term"] and ct in board_by_term:
                    bsl_objs.append(
                        BoardStudentLink(
                            board=board_by_term[ct],
                            student_id=s["student_id"],
                            link_type="visitor",
                        )
                    )

    BoardStudentLink.objects.bulk_create(bsl_objs, ignore_conflicts=True)

    # ── Step 6: Auto-place first draft ───────────────────────────
    # Run the constraint-based auto-placement engine, which assigns
    # each budgeted section to a (day, slot) on its board while
    # respecting conflict and capacity constraints.  The result is a
    # first-draft timetable that advisors can fine-tune manually.

    from core.services.timetable_autoplace import auto_place_scenario, get_meeting_pattern
    from core.services.timetable_flags import is_one_teacher_active

    if run_autoplace and is_one_teacher_active():
        # One teacher per section: a time-blind map while placing, then the
        # teachers re-picked on the board that was placed.
        from core.services.section_teacher_assignment import run_placer

        auto_result = run_placer(
            scenario.id, lambda: auto_place_scenario(scenario.id, strategy=strategy), rebuild=True
        )
    elif run_autoplace:
        auto_result = auto_place_scenario(scenario.id, strategy=strategy)
    else:
        # Deferred build — the scenario scaffold (boards, students, budget) is
        # created now and returned fast; the heavy placement runs off the request
        # thread as an async planner job (see tw_generate_workspace_view), so the
        # request can't overrun a gunicorn/dev-server timeout. No placements yet.
        auto_result = {"boards": {}}

    # ── Build response ───────────────────────────────────────────
    # Assemble a comprehensive response dict that the frontend and API
    # consumers use to render the workspace.

    boards_response = []
    for pt in active_terms:
        board = board_by_term[pt]

        # Count primary and visitor students linked to this board.
        primary_count = BoardStudentLink.objects.filter(board=board, link_type="primary").count()
        visitor_count = BoardStudentLink.objects.filter(board=board, link_type="visitor").count()
        # How many section placements the auto-placer created on this board.
        placement_count = SectionPlacement.objects.filter(board=board).count()

        # Courses that belong to this board's term level.
        board_courses = [b.course_code for b in budget_objs if b.programme_term == pt]

        # Auto-placement statistics for this board (placed vs skipped).
        board_auto = auto_result["boards"].get(board.label, {})

        boards_response.append(
            {
                "id": board.id,
                "label": board.label,
                "nominal_term": pt,
                "program": board_program,
                "primary_count": primary_count,
                "visitor_count": visitor_count,
                "courses": board_courses,
                "placement_count": placement_count,
                "auto_placed": board_auto.get("placed", 0),
                "auto_skipped": board_auto.get("skipped", 0),
                "critical": 0,
                "warning": 0,
            }
        )

    # Re-compute the budget from the DB (now includes actual placement
    # counts) and enrich each entry with meeting-pattern info so the UI
    # can show how many meetings per week each course requires and their
    # durations (e.g. a 3-credit course meets 3x50min or 2x75min).
    from core.services.timetable_workspace import compute_scenario_budget

    budget_response = compute_scenario_budget(scenario.id)
    for b in budget_response:
        cr = b.get("credit_hours", 3)
        pattern = get_meeting_pattern(cr, b.get("course_key") or b.get("course_code", ""))
        b["meetings_per_week"] = len(pattern)
        b["meeting_durations"] = pattern

    # Student summary: how many are on-plan (all courses in one term)
    # vs cross-term (courses spanning multiple term levels).
    on_plan = sum(1 for s in classified if not s["is_cross_term"])
    cross_term = sum(1 for s in classified if s["is_cross_term"])

    return {
        "scenario": {
            "id": scenario.id,
            "academic_year": scenario.academic_year,
            "term": scenario.term,
            "name": scenario.name,
            "status": scenario.status,
            "slot_config": scenario.slot_config,
            "lab_slot_config": scenario.lab_slot_config,
            "created_at": scenario.created_at.isoformat() if scenario.created_at else "",
        },
        "boards": boards_response,
        "section_budget": budget_response,
        "section_plan": plan,
        "plan_summary": summary,
        "student_summary": {
            "total": len(student_ids),
            "with_recommendations": len(student_recs),
            "classified": len(classified),
            "on_plan": on_plan,
            "cross_term": cross_term,
            "by_term": dict(term_student_counts),
        },
        # Elective slots no course could fill, per (programme, slot, reason).
        # Nothing is scheduled for these, so they are reported rather than
        # dropped. Only ``courses_exhausted`` asks anything of the department (a
        # further course mapped onto the slot); ``no_eligible_course`` means the
        # students themselves are not ready for what it already offers.
        "unserved_elective_slots": _summarise_unserved_slots(
            unserved_slots, _student_program, placeholder_terms
        ),
    }
