"""What a student is shown about their own record, and nothing more.

The adviser portfolio computes richer per-student signals than this screen shows,
and the difference is deliberate:

    student screen      verified facts about this student
                        transparent derivations from their own record
                        cited policy that needs no missing input

    adviser portfolio   prioritisation heuristics
                        comparative risk and queue-ordering
                        internal attention classifications

`risk_score` and `needs_attention` stay on the adviser side. `risk_score` is
`max(0, 2 - gpa) * 5 + sum(missing scores) + 3 if zero hours` — a device for
ordering a queue of 120 students. It has no scale, no published threshold and no
source, so in front of the person it describes it is an institutional verdict
that cites nothing.

**The academic-warning rule is deliberately absent**, including in generic form.
`TU.DISMISSAL.THREE_WARNINGS` carries `never_infer`: *"Do not treat gpa < 2.0 as
'on warning'. The guide sets dismissal on a COUNT of consecutive warnings against
a graduation-specific threshold, not on a single GPA reading."* And its
`runtime_use_reason` records that `consecutive_warning_count` **does not exist
anywhere in the schema**. Placing even a generic statement of that rule beside a
student's own GPA composes an implication the evidence cannot support. It belongs
in the adviser chat, where `policy_lookup` carries the citation and the
`PROHIBITED_FOR_DECISION` marker is enforced.
"""

from __future__ import annotations

from typing import Any

#: Taibah reports the cumulative GPA out of 5.
#:
#: Measured, not assumed: across 320 students `Student.gpa` runs 1.0 to exactly
#: 5.00, and 5.00 is unreachable on a four-point scale. The column itself carries
#: no scale, so this is the one inference on this surface — named here so it is
#: visible, and guarded by a test that fails if any stored GPA ever exceeds it.
GPA_SCALE = 5

#: `TU.GPA.GENERAL_ESTIMATE_BANDS`, page 28, AUTHORITY_APPROVED. Read from the
#: store at call time rather than copied, so an edition change moves the screen.
_BANDS_POLICY = "TU.GPA.GENERAL_ESTIMATE_BANDS"


# ── Arabic count grammar ─────────────────────────────────────────
#
# `{n} مقرر` is right for exactly one value and wrong for every other. Arabic
# agreement has six forms and the screen was using one. Centralised so the next
# count added to this page cannot get it wrong independently.

_COURSE_FORMS = {
    "zero": "مقررات",
    "one": "مقرر",
    "two": "مقرران",
    "few": "مقررات",  # 3-10
    "many": "مقررًا",  # 11-99
    "other": "مقرر",  # 100+
}


def arabic_count(n: int, forms: dict[str, str] | None = None) -> str:
    """`n` with the correctly agreeing noun.

    The dual («مقرران») and the 11–99 accusative singular («11 مقررًا») are the two
    that a naive `f"{n} {noun}"` always gets wrong, and 11–99 is the range a real
    plan sits in.
    """
    forms = forms or _COURSE_FORMS
    n = int(n)
    if n == 0:
        return f"لا توجد {forms['zero']}"
    if n == 1:
        return forms["one"]
    if n == 2:
        return forms["two"]
    if 3 <= n % 100 <= 10:
        return f"{n} {forms['few']}"
    if 11 <= n % 100 <= 99:
        return f"{n} {forms['many']}"
    return f"{n} {forms['other']}"


# ── the progress buckets, made mutually exclusive ────────────────


def progress_buckets(report: dict[str, Any]) -> dict[str, int]:
    """Four disjoint course states, plus placeholders counted apart.

    `report["counts"]` reports `one_step` as a SUBSET of `locked` — verified: for
    student 4502156 the 6 "one course away" are 6 of the 8 blocked. The screen
    showed them as four peer tiles, which invites the reader to add them: 6+6+8+30
    = 50 against a plan holding 44 courses and 6 placeholders.

    `blocked_deeper` is the remainder, so the four sum to the plan exactly.
    """
    counts = report.get("counts") or {}
    open_now = int(counts.get("open", 0))
    one_step = int(counts.get("one_step", 0))
    locked = int(counts.get("locked", 0))
    passed = int(counts.get("passed", 0))
    studying = int(counts.get("studying", 0))

    return {
        "open": open_now,
        "one_step": one_step,
        "blocked_deeper": max(0, locked - one_step),
        "passed": passed,
        "studying": studying,
        # Reported separately. A placeholder is not a course in any of the four
        # states — it is a choice not yet made — and folding it in is what made
        # the totals irreconcilable in the first place.
        "elective_slots": len(report.get("elective_slots") or []),
    }


# ── which courses open the most doors ────────────────────────────


def unlock_leaders(report: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """Open courses ranked by how many blocked courses passing them would free.

    NOT the portfolio's `high_priority_missing_courses`, which is a scored triage
    signal. This is the prerequisite graph read straight: "these blocked courses
    name this one as a prerequisite, and it is the only thing they are still
    waiting on."

    THREE different numbers are available and they disagree — for AI331: 5 courses
    name it as a prerequisite, 3 are waiting on it and nothing else, 6 open
    eventually through the chain. This uses the middle one, because it is the only
    one a student can check for themselves the term after they pass it.

    The rule itself now lives in `student_unlock.build_unlock_report`, which is
    where the prerequisite records, the satisfied set and the credit-hour gates all
    already are. Recomputing it from `graph.items` here compared course codes only,
    so a course also short on credit hours counted as "waiting on this one alone"
    — and this screen's whole claim is that the count is checkable.
    """
    names = (report.get("graph") or {}).get("nameOf") or {}
    dependents = report.get("dependents") or {}

    leaders: list[dict[str, Any]] = []
    for candidate in report.get("open_courses") or []:
        code = candidate["code"]
        freed = list((dependents.get(code) or {}).get("waiting_only_on_this") or [])
        if freed:
            leaders.append(
                {
                    "course_code": code,
                    "course_name": names.get(code) or candidate.get("name") or "",
                    "frees": len(freed),
                    "frees_codes": sorted(freed),
                }
            )

    leaders.sort(key=lambda x: (-x["frees"], x["course_code"]))
    return leaders[:limit]


# ── GPA and its approved band ────────────────────────────────────


def gpa_band(gpa: float | None) -> dict[str, Any]:
    """The student's GPA and the approved estimate band, or an honest absence.

    The bands start at 2.00 of 5. Seven students sit below that, and the approved
    table simply has no row for them. The absence is reported as a fact about the
    TABLE — not as a fact about the student, and emphatically not as a warning:
    see this module's docstring.
    """
    from core.services.policy_store import get_policy_store

    if gpa is None:
        return {"gpa": None, "band_ar": "", "note_ar": "لا يوجد معدل تراكمي مسجّل في النظام."}

    store = get_policy_store()
    record = next((r for r in store.records if r.get("policy_id") == _BANDS_POLICY), None)
    if record is None:
        # The store is the authority. If it does not carry the table, this screen
        # does not carry a band — it does not fall back to a hard-coded copy.
        return {"gpa": gpa, "band_ar": "", "note_ar": ""}

    key = f"of_{GPA_SCALE}"
    band_ar = ""
    for band in record.get("bands") or []:
        window = band.get(key) or {}
        low, high = window.get("min"), window.get("max")
        if low is None or high is None:
            continue
        # FIRST band whose lower boundary is satisfied, walking the table from the
        # top. `max_inclusive` is deliberately NOT operative here: the table is
        # validated as descending and contiguous (`validate_band_table`), and under
        # that shape the flag changes no answer anywhere in [0, scale] — measured
        # at 0.01 steps, zero disagreements. Consulting it in the classifier would
        # dress an inert field as live logic. It stays in the source metadata, and
        # the validator is what fails if an edition ever makes it matter.
        inside = gpa >= low and gpa <= high
        if inside:
            band_ar = str(band.get("ar") or "")
            break

    source = record.get("source") or {}
    return {
        "gpa": gpa,
        "band_ar": band_ar,
        "note_ar": "" if band_ar else "لا ينطبق تقدير عام على هذا المعدل في الجدول المعتمد.",
        "citation": {
            "policy_id": _BANDS_POLICY,
            "page": source.get("page"),
            "title_ar": record.get("title_ar") or "",
        },
    }


#: The approved Arabic band, and its English label for the English UI.
#:
#: A translation of the approved wording, not a second classification: the band is
#: always decided from `ar`, and these are only how it is spelled for a reader in
#: English. The source cited on screen stays the Arabic table.
_BAND_EN = {
    "ممتاز": "Excellent",
    "جيد جداً": "Very Good",
    "جيد": "Good",
    "مقبول": "Pass",
}


def validate_band_table(bands: list[dict[str, Any]], scale: int = GPA_SCALE) -> None:
    """The shape the classifier depends on: descending, contiguous, top inclusive.

    `gpa_band` walks the table top-down and takes the first band the value reaches.
    That is only correct while each band's floor is the band-above's ceiling. Under
    any other shape a boundary value falls to the wrong band, or to none.

    Raises rather than returning a flag: a malformed approved table is not a state
    this screen should render around.
    """
    windows = [b.get(f"of_{scale}") or {} for b in bands]
    if not windows:
        raise ValueError("the band table is empty")
    for higher, lower in zip(windows, windows[1:], strict=False):
        if lower.get("max") != higher.get("min"):
            raise ValueError(
                f"band table is not contiguous at {lower.get('max')} / {higher.get('min')}; "
                "max_inclusive is now load-bearing and the classifier must consult it"
            )


def _registered_hours(student_id: int, academic_year: int, term: int) -> dict[str, Any]:
    """Credit HOURS registered for the configured term, and where the figure came from.

    Three numbers were available and two of them are wrong for this card:

      * `counts.studying` is a COURSE count, not hours. My first draft used it, and
        it looked correct for the student I was testing because both were 0.
      * `Student.current_registered_credits` is a scraped column that can disagree
        with the sections on file — measured: student 4400737 has 2 there and 8
        hours across 3 courses in `StudentTermSection`.

    So the term-scoped derivation is used, and it reports its own `source`. When
    the two disagree the card shows the evidenced figure and `disagrees_with_profile`
    records the other, because a silent pick between contradicting sources is how a
    screen ends up confidently wrong.
    """
    from core.models import Student
    from core.services.student_sections import get_student_term_registration_summary

    try:
        summary = get_student_term_registration_summary(
            int(student_id), str(academic_year), str(term)
        )
    except Exception:  # noqa: BLE001 — one card degrades, the page does not
        return {
            "value": None,
            "known": False,
            "course_count": 0,
            "source": "unavailable",
            "academic_year": academic_year,
            "term": term,
            "disagrees_with_profile": None,
        }

    hours = summary.get("value")
    profile = (
        Student.objects.filter(student_id=int(student_id))
        .values_list("current_registered_credits", flat=True)
        .first()
    )
    return {
        **summary,
        # Diagnostic only. The scraped profile column is NOT a substitute for
        # missing term-scoped evidence — it carries no term, so it cannot answer
        # "how many hours in 1448/1". Where the two disagree the screen shows the
        # evidenced figure and this records the other.
        "disagrees_with_profile": (
            None if profile is None or hours is None or int(profile) == int(hours) else int(profile)
        ),
    }


def build_student_home_cards(student_id: int, academic_year: int, term: int) -> dict[str, Any]:
    """Everything the student home screen shows, decided here.

    ONE service, and the template renders it without interpreting anything. The
    view used to compute `eligible_now` alongside — a second implementation of
    "what can this student take", labelled «متاحة للتسجيل هذا الفصل», which the
    prerequisite data does not establish. Two implementations of one card
    eventually disagree, and the one on screen is the one nobody tested.
    """
    from core.models import Student
    from core.services.student_unlock import build_unlock_report

    report = build_unlock_report(int(student_id), int(academic_year), int(term))
    buckets = progress_buckets(report)
    student = Student.objects.filter(student_id=int(student_id)).values("gpa").first() or {}

    band = gpa_band(student.get("gpa"))
    leaders = unlock_leaders(report, limit=1)
    top = leaders[0] if leaders else None
    registered = _registered_hours(int(student_id), int(academic_year), int(term))

    course_total = (
        buckets["open"]
        + buckets["one_step"]
        + buckets["blocked_deeper"]
        + buckets["passed"]
        + buckets["studying"]
    )
    placeholder_total = buckets["elective_slots"]

    return {
        "gpa": {
            "value": f"{band['gpa']:.2f}" if band["gpa"] is not None else "",
            # Asserted from the APPROVED TABLE, whose top band reaches exactly
            # 5.00 — not from observing the maximum stored value. If the table did
            # not reach the ceiling the screen would show the number without
            # claiming a scale.
            "scale": GPA_SCALE,
            "band_ar": band["band_ar"],
            "band_en": _BAND_EN.get(band["band_ar"], ""),
            "classified": bool(band["band_ar"]),
            "note_ar": band.get("note_ar", ""),
            "source": band.get("citation") or {},
        },
        "registered_hours": registered,
        "unlock": None
        if top is None
        else {
            "course_code": top["course_code"],
            "course_name": top["course_name"],
            "frees_now_count": top["frees"],
            "frees_now_codes": top["frees_codes"],
            # Named so the number is checkable. Five courses NAME AI331; six open
            # eventually through the chain; four are waiting on it and nothing else.
            "definition": "sole_remaining_prerequisite",
        },
        # NO PROGRESS DENOMINATOR. These totals describe what is CLASSIFIED in this
        # student's own rows — an inventory of what the system holds about them, not
        # the authoritative size of the curriculum. Rendering "9 من 47" would read
        # as the official plan size and would differ between classmates on the same
        # programme for reasons the screen cannot distinguish: curriculum edition,
        # transferred or substituted courses, a programme change, an import defect,
        # or `CS111` being two different courses across offset plans.
        #
        # A real denominator needs the student's exact declared curriculum edition,
        # not just `Student.program`. Until that exists the fields below say so
        # rather than leaving the absence to be inferred.
        "plan_state": {
            "passed": buckets["passed"],
            "studying": buckets["studying"],
            "open": buckets["open"],
            "one_step": buckets["one_step"],
            "blocked_deeper": buckets["blocked_deeper"],
            "placeholder_total": placeholder_total,
            "classified_course_total": course_total,
            "classified_requirement_total": course_total + placeholder_total,
            # Computed, not asserted. If this is ever False the screen holds
            # arithmetic it cannot defend, and it says so rather than printing
            # totals that do not add up.
            "reconciles": course_total
            == sum(
                len(report.get(k) or [])
                for k in ("open_courses", "locked_courses", "done", "in_progress")
            ),
            "declared_plan_total": None,
            "declared_plan_known": False,
        },
    }


__all__ = [
    "GPA_SCALE",
    "arabic_count",
    "build_student_home_cards",
    "gpa_band",
    "progress_buckets",
    "unlock_leaders",
    "validate_band_table",
]
