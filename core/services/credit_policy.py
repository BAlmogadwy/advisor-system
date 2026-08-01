"""Credit-load figures, and the distinction between two of them that are not the same.

The adviser used to publish ONE number, ``max_term_credit_hours = 18``, and the
system prompt told the model to "present it as the registerable set". A student
asking كم ساعة أقدر أسجل was therefore told 18 — one unit below what the university
permits. The number was never wrong; its name was. 18 is how much this system chooses
to RECOMMEND, not how much the regulation ALLOWS.

  RECOMMENDED_MAX_CREDITS   18  our own advisory cap. The recommender fills up to it.
  REGULATORY_MAX_CREDITS    19  what the student may actually register.
  REGULATORY_MIN_CREDITS    12  below which the load is under the minimum.

Everything else in this module exists because publishing 19 is a STUDENT-FACING
REGULATORY CLAIM, and the first attempt made it unconditionally. Three ways that was
wrong, each of which now narrows the claim rather than widening it:

* **The summer term.** Term 3 is real and selectable throughout this app, and its
  limit is 9 — so a term-blind 12..19 does not merely cost a student a unit, it
  overstates their allowance by three. Direction of harm matters: the original bug
  was conservative, this one was not. Outside the modelled main terms the range is
  now OMITTED, so the prompts' existing "say the system does not define one" branch
  fires instead.
* **Expected graduates.** The same source page records a separate 16-hour ceiling
  by request for متوقع تخرجه, and the policy store flags it as an unresolved
  ambiguity. Those students are already identifiable in the evidence dict two keys
  away. They now get an explicit qualification instead of a flat 19.
* **Provenance.** The basis for 19 is an owner decision corroborated by a document
  record that is not itself independently verified. That belongs IN the evidence
  where the model can hedge with it — a caveat written only in a docstring reaches
  nobody, which is exactly what happened to the first version of this module.

Arabic matters here too, and not incidentally: both quantities render naturally as
الحد الأعلى, so a model that reads the evidence correctly can still write the original
bug back out in translation. The Arabic phrasing is therefore supplied ready-made
rather than left to the model to compose.
"""

from __future__ import annotations

from typing import Any, Final

RECOMMENDED_MAX_CREDITS: Final[int] = 18
REGULATORY_MIN_CREDITS: Final[int] = 12
REGULATORY_MAX_CREDITS: Final[int] = 19

#: Terms whose load range this module actually models: the two main terms.
#: Term 3 (الفصل الصيفي) is deliberately absent — its limit is 9 and we do not model it.
MAIN_TERMS: Final[frozenset[int]] = frozenset({1, 2})
SUMMER_TERM: Final[int] = 3

#: Registrar status marking a student the source page qualifies separately.
EXPECTED_GRADUATE_STATUS: Final[str] = "GRADUATION EXPECTED"

REGULATORY_BASIS: Final[dict[str, Any]] = {
    "document": "الدليل الإرشادي للطالب والطالبة، الإصدار الثالث 1447هـ",
    "page": 23,
    "policy_id": "TU.LOAD.SEMESTER_RANGE",
    "authority": "owner decision 2026-08-01",
    "verification_status": "OWNER_APPROVED_NOT_REGISTRAR_VERIFIED",
    "applies_to": "الفصل الرئيس في نظام الفصلين الدراسيين",
    "hedge": (
        "State the range as what the guide records, and tell the student to confirm "
        "with the registrar before relying on the nineteenth unit."
    ),
}

#: Ready-made Arabic. Both limits translate naturally to الحد الأعلى, so leaving the
#: wording to the model lets the distinction collapse in the one language the answer
#: is actually written in.
PHRASING_AR: Final[dict[str, str]] = {
    "recommended": "سقف التوصية (الحد الذي يتوقف عنده الاقتراح الآلي)",
    "regulatory": "الحد الأعلى المسموح بتسجيله",
    "never_call_the_cap": "لا تسمِّ سقف التوصية «الحد الأعلى» ولا «الحد الأقصى»",
}


def credit_policy_evidence(
    recommended_credit_hours: int,
    unknown_for: list[str],
    *,
    term: int | None = None,
    student_status: str | None = None,
) -> dict[str, Any]:
    """The credit block handed to the adviser model.

    Deliberately contains no key called ``max_term_credit_hours``: that name is what
    made the model present an advisory cap as the university's limit, and
    reintroducing it would reintroduce the bug.

    ``term`` and ``student_status`` are keyword-only and default to None, which is
    treated as UNKNOWN — and unknown omits the regulatory range rather than assuming
    a main term. A caller that cannot say which term it is has not earned the right
    to publish a term-specific limit.
    """
    evidence: dict[str, Any] = {
        "max_recommended_credit_hours": RECOMMENDED_MAX_CREDITS,
        "recommended_credit_hours": recommended_credit_hours,
        "credit_hours_unknown_for": unknown_for,
        "phrasing_ar": PHRASING_AR,
    }

    if term in MAIN_TERMS:
        evidence["regulatory_min_credit_hours"] = REGULATORY_MIN_CREDITS
        evidence["regulatory_max_credit_hours"] = REGULATORY_MAX_CREDITS
        evidence["regulatory_basis"] = REGULATORY_BASIS
    else:
        reason = (
            f"term {term} is the summer term; its credit limit is not modelled by this "
            "system (the guide records 9, which this code does not serve)"
            if term == SUMMER_TERM
            else f"the term is not one of the modelled main terms (got {term!r})"
        )
        evidence["regulatory_range_unknown"] = reason
        evidence["regulatory_range_instruction"] = (
            "Do NOT state a registration limit for this term. Say the system does not "
            "define one here and refer the student to the registrar."
        )

    status = (student_status or "").strip().upper()
    if status == EXPECTED_GRADUATE_STATUS and term in MAIN_TERMS:
        evidence["qualification"] = {
            "applies_to": EXPECTED_GRADUATE_STATUS,
            "unresolved": True,
            "detail_ar": (
                "الدليل يذكر للطالب المتوقع تخرجه حداً منفصلاً قدره 16 ساعة بطلب، وهو "
                "أقل من الحد العام؛ والنص غير واضح في العلاقة بين الرقمين."
            ),
            "instruction": (
                "This student is an expected graduate. The source records a SEPARATE "
                "16-hour ceiling by request for that category, and the relationship "
                "between 16 and the ordinary range is genuinely unresolved in the "
                "source. Do not assert the ordinary maximum applies to them — present "
                "both figures and tell them to confirm with their college."
            ),
        }

    evidence["note"] = (
        "Two different limits. max_recommended_credit_hours is where THIS SYSTEM stops "
        f"suggesting courses ({RECOMMENDED_MAX_CREDITS}); regulatory_max_credit_hours, "
        "when present, is what the university permits the student to register and is "
        "HIGHER. A student may register up to the regulatory maximum even though the "
        "suggestion list stops earlier. Never present the recommendation cap as the "
        "registration limit. If regulatory_max_credit_hours is absent, no registration "
        "limit is known for this term — say so rather than substituting the "
        "recommendation cap. The current term's registered credits do not reduce the "
        "planning term's allowance."
    )
    return evidence
