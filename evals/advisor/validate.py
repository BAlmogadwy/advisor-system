"""Check the expected-answer set is usable as an evaluation set.

An eval set is code. It rots the same way, and it rots silently: a policy_id that no
longer exists, a capability that was renamed, a `must_not_contain` entry too vague to
evaluate — none of these fail loudly, they just quietly stop testing anything while
the suite still reports 200 questions covered.

Checks, in rough order of how badly each one bites:

  1. Every question 1..200 has exactly one expectation. No gaps, no duplicates.
  2. Every cited policy_id exists in policies/. A citation to a deleted rule is worse
     than no citation, because it looks like grounding.
  3. Every named capability is actually registered. Tool names drift.
  4. FULL and PARTIAL answers name at least one capability or policy_id — an answer
     graded as personalised must say what would produce it.
  5. UNSUPPORTED implies must_abstain. If refusal is not required, the mode is wrong.
  6. must_not_contain entries are specific enough to evaluate. "wrong information" is
     decoration; "any specific number of absences" is a test.
  7. No expectation cites a rule the store marks PROHIBITED_FOR_DECISION while
     claiming a FULL personalised answer.

Usage:  python evals/advisor/validate.py
Exit 1 on any failure.
"""

from __future__ import annotations

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
QUESTIONS = HERE / "questions.yaml"
EXPECTED = HERE / "expected.yaml"
POLICIES = ROOT / "policies"

MODES = {"FULL", "PARTIAL", "EXPLAIN_ONLY", "UNSUPPORTED"}

# Phrases that cannot be evaluated by any grader, human or automatic. A
# must_not_contain built out of these tests nothing.
VAGUE = {
    "wrong information", "incorrect advice", "wrong answer", "hallucination",
    "false information", "made up facts", "inaccurate", "anything wrong",
    "معلومات خاطئة", "إجابة خاطئة", "معلومات غير صحيحة",
}


def load_policy_ids() -> set[str]:
    ids: set[str] = set()
    for path in POLICIES.rglob("*.yaml"):
        if path.name in ("sources.yaml", "evidence_map.yaml"):
            continue
        if {"evidence", "tools"} & set(path.parts):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            ids.update(str(r["policy_id"]) for r in data if isinstance(r, dict) and "policy_id" in r)
    return ids


def load_prohibited() -> set[str]:
    """Rules the store says cannot support a decision, whatever the approval status."""
    out: set[str] = set()
    for path in POLICIES.rglob("*.yaml"):
        if path.name in ("sources.yaml", "evidence_map.yaml") or {"evidence", "tools"} & set(path.parts):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for r in data:
                if isinstance(r, dict) and r.get("runtime_use") == "PROHIBITED_FOR_DECISION":
                    out.add(str(r["policy_id"]))
    return out


def load_capability_names() -> tuple[set[str], set[str]]:
    """Returns (all registered names, names a STUDENT may not call).

    The adviser is student-only. A staff-scoped tool named in an expected answer is
    not a typo — it means the set expects an answer the registry will refuse to
    produce, and grading against it would reward talking past a working control.
    """
    import os

    import django

    # These scripts run from anywhere; Django needs the project root importable.
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import get_default_registry

    reg = get_default_registry().capabilities
    return set(reg), {n for n, c in reg.items() if ROLE_STUDENT not in c.allowed_roles}


def main() -> int:
    if not EXPECTED.exists():
        sys.exit(f"missing {EXPECTED} — run the annotation workflow first")

    questions = {q["id"]: q for q in yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]}
    expected = yaml.safe_load(EXPECTED.read_text(encoding="utf-8"))["expectations"]

    policy_ids = load_policy_ids()
    prohibited = load_prohibited()
    capabilities, staff_only = load_capability_names()

    problems: list[str] = []

    seen: dict[int, int] = {}
    for e in expected:
        seen[e["id"]] = seen.get(e["id"], 0) + 1
    missing = sorted(set(questions) - set(seen))
    dupes = sorted(i for i, n in seen.items() if n > 1)
    extra = sorted(set(seen) - set(questions))
    if missing:
        problems.append(f"no expectation for questions: {missing}")
    if dupes:
        problems.append(f"duplicate expectations for: {dupes}")
    if extra:
        problems.append(f"expectations for questions that do not exist: {extra}")

    for e in expected:
        qid = e["id"]
        tag = f"q{qid}"
        mode = e.get("answer_mode")
        if mode not in MODES:
            problems.append(f"{tag}: unknown answer_mode {mode!r}")

        for pid in e.get("policy_ids") or []:
            if pid not in policy_ids:
                problems.append(f"{tag}: cites policy_id {pid!r} which is not in the store")
        for cap in e.get("capabilities") or []:
            if cap not in capabilities:
                problems.append(f"{tag}: names capability {cap!r} which is not registered")
            elif cap in staff_only:
                problems.append(
                    f"{tag}: names {cap!r}, which a student may not call. The adviser is "
                    "student-only; a staff-voiced question is a scope PROBE whose correct "
                    "answer is a refusal, not a tool call."
                )

        if mode in ("FULL", "PARTIAL") and not (e.get("capabilities") or e.get("policy_ids")):
            problems.append(f"{tag}: {mode} but names neither a capability nor a policy_id")

        if mode == "UNSUPPORTED" and not e.get("must_abstain"):
            problems.append(f"{tag}: UNSUPPORTED but must_abstain is false")

        if mode == "FULL" and e.get("reason_code") not in (None, "NONE"):
            problems.append(f"{tag}: FULL but carries reason_code {e.get('reason_code')!r}")
        if mode != "FULL" and e.get("reason_code") in (None, "NONE"):
            problems.append(f"{tag}: {mode} but no reason_code")

        if mode == "FULL":
            blocked = [p for p in (e.get("policy_ids") or []) if p in prohibited]
            if blocked:
                problems.append(
                    f"{tag}: graded FULL but rests on rules the store marks "
                    f"PROHIBITED_FOR_DECISION: {blocked}"
                )

        for phrase in e.get("must_not_contain") or []:
            if phrase.strip().lower() in VAGUE:
                problems.append(f"{tag}: must_not_contain entry is not evaluable: {phrase!r}")
        if not (e.get("must_not_contain") or []):
            problems.append(f"{tag}: empty must_not_contain — nothing guards against invention")
        if not str(e.get("answer_sketch_ar") or "").strip():
            problems.append(f"{tag}: no answer_sketch_ar, so a grader has no rubric")

    modes: dict[str, int] = {}
    for e in expected:
        modes[e.get("answer_mode", "?")] = modes.get(e.get("answer_mode", "?"), 0) + 1
    print(f"expectations: {len(expected)} for {len(questions)} questions")
    for m in ("FULL", "PARTIAL", "EXPLAIN_ONLY", "UNSUPPORTED"):
        print(f"  {m:<13} {modes.get(m, 0):>3}")
    print(f"  must_abstain  {sum(1 for e in expected if e.get('must_abstain')):>3}")

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems[:60]:
            print(f"  ! {p}")
        if len(problems) > 60:
            print(f"  ... and {len(problems) - 60} more")
        return 1
    print("\nvalidation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
