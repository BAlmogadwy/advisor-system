"""Merge the answerable batch into the eval set, applying the verifiers' rejects.

The 200-question set requires abstention on 103 of 200, so a model that refuses
everything scores 51.5%. This batch is the counterweight: 100 questions where
refusing is a FAILURE, weighted toward the five staff capabilities the original set
never exercised at all.

Two verifiers audited the batch by EXECUTING against the live database rather than
reading code, and raised 37 rejects on 100 items. Two of their findings are facts
about the project, not about the questions, and they change what can be asked:

  * ZERO of the 1,668 meetings on student-linked sections carry an instructor name.
    1,113 meetings elsewhere do, which is why a casual count says otherwise — but
    none of them are the ones a real student's timetable resolves to. No question may
    ask who teaches a class, and every timetable question must forbid naming one.
  * Every StudentTermSection row is ('1447','2'). The calendar covers 1448 term 1.
    my_timetable and the calendar describe DIFFERENT TERMS, so any question welding
    "when does term start" to "where is my first lecture" is incoherent against the
    data and is dropped rather than patched.

Run: python evals/advisor/merge_answerable.py --in raw.json --rejects rej.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent

#: The batch starts here so the original 200 keep their owner-assigned numbers.
ID_BASE = 200

#: Rejects that mean "delete", not "amend".
FATAL = {"DUPLICATE", "NOT_ANSWERABLE"}

#: Applied to every timetable-derived question, because the field is empty on every
#: meeting a student can actually reach.
NO_INSTRUCTOR_GUARD = "any instructor or faculty name for a class, which no student-linked meeting records"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def load_store_ids() -> set[str]:
    ids: set[str] = set()
    for path in (ROOT / "policies").rglob("*.yaml"):
        if path.name in ("sources.yaml", "evidence_map.yaml") or {"evidence", "tools"} & set(path.parts):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            ids.update(str(r["policy_id"]) for r in data if isinstance(r, dict) and "policy_id" in r)
    return ids


def load_prohibited() -> set[str]:
    out: set[str] = set()
    for path in (ROOT / "policies").rglob("*.yaml"):
        if path.name in ("sources.yaml", "evidence_map.yaml") or {"evidence", "tools"} & set(path.parts):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for r in data:
                if isinstance(r, dict) and r.get("runtime_use") == "PROHIBITED_FOR_DECISION":
                    out.add(str(r["policy_id"]))
    return out


def registry_names() -> set[str]:
    import os

    import django

    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from core.services.virtual_advisor_capabilities import get_default_registry

    return set(get_default_registry().capabilities)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--rejects", dest="rej", required=True)
    args = ap.parse_args()

    raw = json.loads(pathlib.Path(args.src).read_text(encoding="utf-8"))
    items = raw["items"] if isinstance(raw, dict) else raw
    rejects = json.loads(pathlib.Path(args.rej).read_text(encoding="utf-8"))

    valid_ids = load_store_ids()
    blocked = load_prohibited()
    caps = registry_names()

    by_q: dict[str, list[dict]] = {}
    for r in rejects:
        by_q.setdefault(norm(r["question_ar"])[:60], []).append(r)

    kept: list[dict] = []
    dropped: list[tuple[str, str]] = []
    log: list[str] = []

    for it in items:
        key = norm(it["ar"])[:60]
        problems = {r["problem"] for r in by_q.get(key, [])}

        if problems & FATAL:
            dropped.append((it["ar"][:60], sorted(problems & FATAL)[0]))
            continue

        e = dict(it)
        slice_name = e.pop("slice", "")

        bad = [p for p in e.get("policy_ids", []) if p not in valid_ids]
        if bad:
            e["policy_ids"] = [p for p in e["policy_ids"] if p in valid_ids]
            log.append(f"{key[:34]}: dropped unresolvable policy_id {bad}")
        badcap = [c for c in e.get("capabilities", []) if c not in caps]
        if badcap:
            e["capabilities"] = [c for c in e["capabilities"] if c in caps]
            log.append(f"{key[:34]}: dropped unregistered capability {badcap}")

        # A FULL answer resting on a rule the store forbids deciding on grades a
        # fabrication as success. Same check normalise.py applies to the first 200 —
        # it belongs here too, and its absence is why validate.py caught two.
        if e["answer_mode"] == "FULL":
            rests_on = [p for p in e.get("policy_ids", []) if p in blocked]
            if rests_on:
                e["answer_mode"] = "PARTIAL"
                log.append(f"{key[:34]}: FULL -> PARTIAL; rests on PROHIBITED {rests_on}")

        # A FULL answer with nothing to read is a rule answer wearing the wrong label,
        # and it inflates the batch's apparent capability coverage.
        if e["answer_mode"] == "FULL" and not e.get("capabilities"):
            e["answer_mode"] = "EXPLAIN_ONLY"
            log.append(f"{key[:34]}: FULL -> EXPLAIN_ONLY (no capability to read)")

        if "WRONG_MODE" in problems and e["answer_mode"] == "FULL":
            e["answer_mode"] = "EXPLAIN_ONLY"
            e["capabilities"] = []
            log.append(f"{key[:34]}: FULL -> EXPLAIN_ONLY (verifier: nothing student-specific graded)")

        if slice_name == "timetable-sections" or "my_timetable" in e.get("capabilities", []):
            if NO_INSTRUCTOR_GUARD not in e.get("must_not_contain", []):
                e.setdefault("must_not_contain", []).append(NO_INSTRUCTOR_GUARD)

        if problems & {"UNGRADEABLE", "WEAK_GUARD"}:
            detail = next(r["detail"] for r in by_q[key] if r["problem"] in {"UNGRADEABLE", "WEAK_GUARD"})
            e["review_flag"] = f"{sorted(problems & {'UNGRADEABLE', 'WEAK_GUARD'})[0]}: {detail[:220]}"

        e["reason_code"] = "NONE" if e["answer_mode"] == "FULL" else "NO_STUDENT_DATA"
        e["must_abstain"] = False  # by construction: refusing any of these is a failure
        e["source_slice"] = slice_name
        kept.append(e)

    for i, e in enumerate(kept, start=1):
        e["id"] = ID_BASE + i

    questions_path = HERE / "questions.yaml"
    qdoc = yaml.safe_load(questions_path.read_text(encoding="utf-8"))
    qdoc["categories"].append(
        {"id": "C16", "ar": "أسئلة قابلة للإجابة (مولّدة ومُتحقَّق منها)",
         "range": [ID_BASE + 1, ID_BASE + len(kept)]}
    )
    for e in kept:
        qdoc["questions"].append({"id": e["id"], "c": "C16", "ar": e["ar"]})
    qdoc["meta"]["count"] = len(qdoc["questions"])
    questions_path.write_text(
        yaml.safe_dump(qdoc, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )

    exp_path = HERE / "expected.yaml"
    edoc = yaml.safe_load(exp_path.read_text(encoding="utf-8"))
    for e in kept:
        e.pop("ar", None)
        e.pop("category_ar", None)
        e.pop("why_answerable", None)
        edoc["expectations"].append(e)
    edoc["meta"]["count"] = len(edoc["expectations"])
    exp_path.write_text(
        yaml.safe_dump(edoc, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )

    print(f"kept {len(kept)} of {len(items)}; dropped {len(dropped)}")
    for q, why in dropped:
        print(f"  DROP [{why}] {q}")
    print(f"\namendments: {len(log)}")
    for line in log:
        print(f"  {line}")
    flagged = [e["id"] for e in kept if e.get("review_flag")]
    print(f"\nflagged for human review: {len(flagged)} -> {flagged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
