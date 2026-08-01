"""Credit-load figures, and the distinction between two of them that are not the same.

The adviser used to publish ONE number, ``max_term_credit_hours = 18``, and the
system prompt told the model to "present it as the registerable set". A student
asking كم ساعة أقدر أسجل was therefore told 18 — one unit below what the university
actually permits. The number was never wrong; its name was. 18 is how much this
system chooses to RECOMMEND, not how much the regulation ALLOWS.

Two different quantities, from two different authorities:

  RECOMMENDED_MAX_CREDITS   18  our own advisory cap. The recommender greedily fills
                                up to it. A product decision, not a rule.
  REGULATORY_MAX_CREDITS    19  what the student may actually register.
  REGULATORY_MIN_CREDITS    12  below which the load is under the minimum.

Keep them apart in anything shown to a student: "we suggest up to 18" and "you may
register up to 19" are both true, and collapsing them into one figure silently
narrows the student's options.

Provenance is recorded because these are the only regulation-derived numbers the
running code asserts. The policy store carries the same figures with page citations
(TU.LOAD.SEMESTER_RANGE, p.23), but every record there is staged EXTRACTED and none
may be served yet — so the runtime authority for 12 and 19 is the owner's decision
of 2026-08-01, with the store as corroboration rather than the other way round.
"""

from __future__ import annotations

from typing import Final

# Our advisory cap. Historically ``recommender.MAX_CREDITS``, duplicated verbatim in
# recommender_batch; both now import this so the two cannot drift apart.
RECOMMENDED_MAX_CREDITS: Final[int] = 18

# The university's own range for a main term under the two-semester system.
REGULATORY_MIN_CREDITS: Final[int] = 12
REGULATORY_MAX_CREDITS: Final[int] = 19

CREDIT_POLICY_PROVENANCE: Final[dict[str, object]] = {
    "recommended_max": {
        "value": RECOMMENDED_MAX_CREDITS,
        "kind": "ADVISORY_DEFAULT",
        "authority": "this system's own recommendation policy",
        "meaning_ar": "الحد الذي يتوقف عنده الاقتراح الآلي",
    },
    "regulatory_range": {
        "min": REGULATORY_MIN_CREDITS,
        "max": REGULATORY_MAX_CREDITS,
        "kind": "UNIVERSITY_REGULATION",
        "applies_to": "الفصل الرئيس في نظام الفصلين الدراسيين",
        "authority": "owner decision 2026-08-01",
        "corroborated_by": {
            "policy_id": "TU.LOAD.SEMESTER_RANGE",
            "document": "الدليل الإرشادي للطالب والطالبة، الإصدار الثالث 1447هـ",
            "page": 23,
            "verification_status": "EXTRACTED",
            "note": (
                "The policy store record is not yet SOURCE_VERIFIED, so it corroborates "
                "the owner's decision rather than establishing the figure itself."
            ),
        },
        "meaning_ar": "ما يسمح النظام للطالب بتسجيله فعلياً",
    },
    "not_covered": (
        "Summer terms and the full-year system have their own limits (9, and 24-39) "
        "which this module does not model, because the project has no summer-term or "
        "full-year concept. Do not apply these figures to either."
    ),
}


def credit_policy_evidence(recommended_credit_hours: int, unknown_for: list[str]) -> dict:
    """The credit block handed to the adviser model.

    Deliberately does NOT contain a key called ``max_term_credit_hours``. That name
    is what caused the model to present an advisory cap as the university's limit,
    and reintroducing it would reintroduce the bug.
    """
    return {
        "max_recommended_credit_hours": RECOMMENDED_MAX_CREDITS,
        "regulatory_min_credit_hours": REGULATORY_MIN_CREDITS,
        "regulatory_max_credit_hours": REGULATORY_MAX_CREDITS,
        "recommended_credit_hours": recommended_credit_hours,
        "credit_hours_unknown_for": unknown_for,
        "note": (
            "Two different limits. max_recommended_credit_hours is where THIS SYSTEM "
            "stops suggesting courses; regulatory_max_credit_hours is what the "
            "university permits the student to register. A student may register up to "
            "the regulatory maximum even though the suggestion list stops earlier. "
            "Never present the recommendation cap as the registration limit. "
            "The current term's registered credits do not reduce the planning term's "
            "allowance."
        ),
    }
