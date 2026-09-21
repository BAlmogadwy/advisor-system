"""Apply the review's mechanical corrections to the raw annotations.

Eight agents annotated 25 questions each, independently, and the audit found the
predictable failure: calibration drifted along the block boundaries. must_abstain
ranged from 1/25 in one block to 20/25 in another; the same structural gap got four
different reason_codes; PARTIAL and EXPLAIN_ONLY were separated on different lines by
different blocks.

Those are not judgement disagreements, they are the same judgement applied
inconsistently — so they are fixed by ONE rule applied to all 200 rather than by
arguing item by item. Rules that need a human decision are NOT applied here; they are
reported and left for the next pass.

Run:  python evals/advisor/normalise.py --in raw.json --out expected.yaml
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
POLICIES = ROOT / "policies"

# ── Facts about the store, loaded rather than assumed ──────────────────────────


def store_policy_ids() -> set[str]:
    ids: set[str] = set()
    for path in POLICIES.rglob("*.yaml"):
        if path.name in ("sources.yaml", "evidence_map.yaml") or {"evidence", "tools"} & set(
            path.parts
        ):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            ids.update(
                str(r["policy_id"]) for r in data if isinstance(r, dict) and "policy_id" in r
            )
    return ids


def prohibited_policy_ids() -> set[str]:
    out: set[str] = set()
    for path in POLICIES.rglob("*.yaml"):
        if path.name in ("sources.yaml", "evidence_map.yaml") or {"evidence", "tools"} & set(
            path.parts
        ):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for r in data:
                if isinstance(r, dict) and r.get("runtime_use") == "PROHIBITED_FOR_DECISION":
                    out.add(str(r["policy_id"]))
    return out


# ── The corrections ────────────────────────────────────────────────────────────

#: Questions blocked by a gap the schema cannot represent even in principle, as
#: opposed to a field that exists and happens to be empty. The audit found this coded
#: four different ways for attendance alone.
STRUCTURAL = {
    # attendance: no field, no table, no service anywhere
    57,
    60,
    181,
    182,
    183,
    184,
    186,
    187,
    189,
    190,
    191,
    192,
    193,
    194,
    195,
    196,
    197,
    200,
    # actual_term empty on every row -> no event ordering, nothing "consecutive"
    4,
    46,
    70,
    173,
    176,
    177,
    178,
    180,
    # UniqueConstraint(student, course) -> attempt history unrepresentable
    161,
    162,
    165,
    166,
    170,
}

#: Facts no registered capability returns. Demanding these in must_contain makes the
#: grader reward a fabrication — the single worst defect the audit found.
UNSUPPLYABLE = {
    160: ("المبنى", "النظام لا يعرض اسم المبنى لهذه المحاضرات"),
    112: ("وقت الشعبة", "النظام لا يعرض أوقات الشعب المتاحة"),
    131: ("مطروحة", "النظام لا يعرض ما إذا كان المقرر مطروحاً هذا الفصل"),
    118: ("مطروحة", "النظام لا يعرض ما إذا كان المقرر الاختياري مطروحاً"),
    117: ("الشعب المتاحة", "النظام لا يعرض الشعب المتاحة للمقررات الاختيارية"),
}

#: Guards the audit found stated in notes but never promoted to the enforceable list,
#: or missing entirely on items that quote a dated or scoped figure.
EXTRA_FORBIDDEN: dict[int, list[str]] = {
    **{
        q: ["a 1448 term-1 date presented as applying to any other term"]
        for q in (32, 34, 128, 130, 136, 143, 144)
    },
    **{
        q: [
            "an opening date or a date range for a deadline the calendar states only as a closing date"
        ]
        for q in (34, 75, 136)
    },
    29: [
        "quoting the 25% or 24-unit visiting-student figures as if they governed registration in another department"
    ],
    24: ["asserting a seat will be opened, or that the student may enter a full section"],
    3: ["presenting the registrar's earned-credit total as the plan-remaining figure"],
}


#: A capability may be named only if it exists. Verified against the live registry.
def registry_names() -> set[str]:
    import os

    import django

    # These scripts run from anywhere; Django needs the project root importable.
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from core.services.virtual_advisor_capabilities import get_default_registry

    return set(get_default_registry().capabilities)


def normalise(items: list[dict]) -> tuple[list[dict], list[str]]:
    valid_ids = store_policy_ids()
    blocked = prohibited_policy_ids()
    caps = registry_names()

    log: list[str] = []
    out: list[dict] = []

    for it in sorted(items, key=lambda x: x["id"]):
        qid = it["id"]
        e = dict(it)
        e.pop("slice", None)

        # 1. Identifiers that do not resolve. A citation to something that is not a
        #    rule looks like grounding and is not.
        bad = [p for p in e.get("policy_ids", []) if p not in valid_ids]
        if bad:
            e["policy_ids"] = [p for p in e["policy_ids"] if p in valid_ids]
            log.append(f"q{qid}: dropped non-existent policy_id(s) {bad}")
        badcap = [c for c in e.get("capabilities", []) if c not in caps]
        if badcap:
            e["capabilities"] = [c for c in e["capabilities"] if c in caps]
            log.append(f"q{qid}: dropped unregistered capability {badcap}")

        # 2. FULL resting on a rule the store forbids deciding on. A false FULL grades
        #    a fabrication as success, so this outranks the annotator's judgement.
        if e["answer_mode"] == "FULL":
            rests_on = [p for p in e.get("policy_ids", []) if p in blocked]
            if rests_on:
                e["answer_mode"] = "PARTIAL"
                e["reason_code"] = "SOURCE_AMBIGUOUS"
                log.append(f"q{qid}: FULL -> PARTIAL; rests on PROHIBITED_FOR_DECISION {rests_on}")

        # 3. The vocabulary's own line between PARTIAL and EXPLAIN_ONLY: PARTIAL means
        #    some per-student condition IS checkable. No capability means none is.
        if e["answer_mode"] == "PARTIAL" and not e.get("capabilities"):
            e["answer_mode"] = "EXPLAIN_ONLY"
            log.append(f"q{qid}: PARTIAL -> EXPLAIN_ONLY (no capability, so nothing per-student)")

        # 4. One code per structural gap.
        if qid in STRUCTURAL and e["answer_mode"] != "FULL":
            if e.get("reason_code") != "STRUCTURALLY_UNREPRESENTABLE":
                log.append(
                    f"q{qid}: reason_code {e.get('reason_code')} -> STRUCTURALLY_UNREPRESENTABLE"
                )
                e["reason_code"] = "STRUCTURALLY_UNREPRESENTABLE"

        # 5. reason_code is mandatory off FULL and forbidden on it.
        if e["answer_mode"] == "FULL":
            e["reason_code"] = "NONE"
        elif e.get("reason_code") in (None, "", "NONE"):
            e["reason_code"] = "NO_STUDENT_DATA"
            log.append(f"q{qid}: {e['answer_mode']} had no reason_code -> NO_STUDENT_DATA")

        # 6. must_contain may not demand a fact no tool returns.
        if qid in UNSUPPLYABLE:
            token, replacement = UNSUPPLYABLE[qid]
            before = list(e.get("must_contain", []))
            e["must_contain"] = [m for m in before if token not in m]
            if len(e["must_contain"]) != len(before):
                e["must_contain"].append(replacement)
                e.setdefault("must_not_contain", []).append(
                    f"any specific {token} presented as this student's"
                )
                log.append(f"q{qid}: must_contain demanded {token!r}, which no capability returns")

        # 7. Guards the audit found in notes but not in the enforceable list.
        for extra in EXTRA_FORBIDDEN.get(qid, []):
            if extra not in e.get("must_not_contain", []):
                e.setdefault("must_not_contain", []).append(extra)
                log.append(f"q{qid}: promoted a notes-only guard to must_not_contain")

        # 8. One operational test for abstention, replacing eight per-block habits:
        #    a refusal is required exactly when no obtained source and no capability
        #    can support the specific claim the question invites.
        derived = e["answer_mode"] in ("UNSUPPORTED",) or (
            e["answer_mode"] == "EXPLAIN_ONLY" and not e.get("policy_ids")
        )
        if derived and not e.get("must_abstain"):
            e["must_abstain"] = True
            log.append(f"q{qid}: must_abstain false -> true ({e['answer_mode']}, nothing to cite)")

        out.append(e)

    return out, log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    args = ap.parse_args()

    raw = json.loads(pathlib.Path(args.src).read_text(encoding="utf-8"))
    items = raw["items"] if isinstance(raw, dict) else raw
    fixed, log = normalise(items)

    payload = {
        "meta": {
            "count": len(fixed),
            "built": "2026-08-01",
            "grading_contract": {
                "must_contain": (
                    "Literal tokens. Matched case-insensitively as substrings after "
                    "normalising Arabic-Indic digits to ASCII. Give the discriminating "
                    "token (a number, a course code, an Arabic term), never a sentence."
                ),
                "must_not_contain": (
                    "PATTERNS, evaluated by a judge, not substrings. An entry describes "
                    "a class of claim ('any specific number of absences'), so a literal "
                    "string is a valid but narrow instance of one. This is the field "
                    "that catches invention, and it is judged, not matched."
                ),
                "must_abstain": (
                    "True iff the question invites at least one specific claim that no "
                    "obtained source and no registered capability can support. An answer "
                    "that supplies such a claim fails regardless of anything else it got "
                    "right."
                ),
            },
            "known_gaps": [
                "No student fixture is bound yet, so FULL items whose must_contain asks "
                "for 'the student's actual course codes' cannot be graded on specifics. "
                "Binding a fixture student id plus the deterministic tool output is the "
                "next pass, and until it lands those items grade only on shape.",
                "must_abstain was re-derived from one rule; the per-question judgement "
                "behind the original per-block values was not preserved and should be "
                "reviewed by a human against the questions.",
            ],
        },
        "expectations": fixed,
    }
    pathlib.Path(args.dst).write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )

    print(f"normalised {len(fixed)} expectations -> {args.dst}")
    print(f"corrections applied: {len(log)}")
    for line in log:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
