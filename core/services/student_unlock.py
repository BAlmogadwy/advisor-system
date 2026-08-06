"""Build a student's "what can I take, what is locked, and why" report.

Consumes the existing plan payload and prerequisite helpers — it does not invent a
second definition of eligibility. One rule, stated once:

    open   = not taken, every course prerequisite is passed-or-being-studied,
             and the credit-hour gate (if any) is met
    locked = not taken, and not open

Everything else on the screen (steps, nearest reachable course, how many courses a
blocker frees) is derived from that, over THIS student's own remaining plan.

THREE forward relations, and they are not interchangeable. `dependents[code]`
carries all three because every consumer that computed one of them locally picked
a different one and called it the same thing:

    listed                 X names `code` among its prerequisites. A catalogue
                           fact; true whatever the student has passed.
    waiting_only_on_this   X is not taken, and `code` is the ONLY prerequisite
                           condition it still fails — course or credit hours.
                           This is the set that becomes prerequisite-satisfied the
                           day `code` is passed.
    on_chain_of_count      `code` is somewhere in X's remaining chain. Passing it
                           removes one link; it does not make X takeable.

On the controlled evaluation record, AI331 scores 5, 3 and 6 on those three. Reporting any one of
them as "what AI331 unlocks" is wrong two times out of three, and the reverse-edge
count — the easiest to compute — is the one every caller was reporting.

None of the three is a statement that X can be REGISTERED. This module knows the
prerequisite records and nothing about offerings, permissions or seats.
"""

from __future__ import annotations

from core.services.eligibility import hour_gate, split_hour_prereqs
from core.services.recommender import eligible_next_term_courses
from core.services.student_helpers import (
    get_prerequisites,
    is_elective_slot,
    normalize_code,
)

_MAX_DEPTH = 12  # cycle/pathological-chain guard

# Elective placeholders are not registrable courses; they are "choose one with your
# advisor" slots, so they are listed apart and never counted as open. Which makes
# misclassifying a MANDATORY course as one of them expensive: it vanishes from the
# open and locked lists, from every counts bucket, and from any prerequisite
# explanation, and the student is told to choose it with their adviser.


def _is_placeholder(code: str, ctype: str) -> bool:
    """Delegates. The declared requirement type decides, and nothing else.

    This used to match a `GS`/`GSE`/`FE` code prefix BEFORE consulting the type,
    which claimed seven `Mandatory` courses — see `is_elective_slot` and issue #55.
    The `len(code) <= 4` guard that followed it is gone too: it was defending
    against a concrete course carrying an elective type, and no row in the plan
    does that.
    """
    return is_elective_slot(ctype)


def build_unlock_report(student_id: int, year: int, term: int) -> dict:
    """Return the full report. Pure read; raises nothing the caller must handle
    beyond the usual DB errors."""
    from core.report_views import _build_student_plan_payload

    payload, _err = _build_student_plan_payload(student_id)
    if not payload:
        return {}

    program = str(payload.get("program") or "")
    passed = {
        c["course_code"] for t in payload["terms"] for c in t["courses"] if c["status"] == "passed"
    }
    studying = {
        c["course_code"]
        for t in payload["terms"]
        for c in t["courses"]
        if c["status"] == "studying"
    }
    satisfied = passed | studying

    # The plan payload carries no course names; fetch them once for the whole plan.
    from core.models import Course

    codes = {c["course_code"] for t in payload["terms"] for c in t["courses"]}
    names = {
        normalize_code(k): (v or "")
        for k, v in Course.objects.filter(course_code__in=codes).values_list(
            "course_code", "description"
        )
    }

    # ── one pass: classify every plan course ──
    info: dict[str, dict] = {}
    for tblock in payload["terms"]:
        for c in tblock["courses"]:
            code = c["course_code"]
            course_prereqs, req_hours = split_hour_prereqs(get_prerequisites(code, program))
            gate = hour_gate(student_id, req_hours) if req_hours else None
            missing = [p for p in course_prereqs if p not in satisfied]
            is_open = c["status"] == "not_taken" and not missing and (gate is None or gate["met"])
            info[code] = {
                "code": code,
                "name": names.get(code, ""),
                "credits": c.get("credit_hours"),
                "term": c.get("programme_term") or 0,
                "type": str(c.get("type") or ""),
                "status": c["status"],
                "course_prereqs": course_prereqs,
                "missing": missing,
                "gate": gate,
                "open": is_open,
                "placeholder": _is_placeholder(code, c.get("type") or ""),
            }

    try:
        fits = set(eligible_next_term_courses(student_id, year, term))
    except Exception:  # noqa: BLE001 — a recommender failure must not lose the screen
        fits = set()

    def unsatisfied_closure(code: str) -> set[str]:
        """Every plan course that must still be passed before `code` can be taken."""
        out: set[str] = set()
        stack, depth = [(code, 0)], 0
        seen = {code}
        while stack:
            cur, depth = stack.pop()
            if depth > _MAX_DEPTH:
                continue
            for p in info.get(cur, {}).get("missing", []):
                if p in out or p in seen and p != code:
                    continue
                out.add(p)
                seen.add(p)
                if p in info:
                    stack.append((p, depth + 1))
        return out

    closures = {c: unsatisfied_closure(c) for c, i in info.items() if i["status"] == "not_taken"}

    def steps_to(code: str) -> int | None:
        """How many passes away this course is: layered by dependency distance."""
        remaining = set(closures.get(code, set()))
        n = 0
        while remaining:
            n += 1
            if n > _MAX_DEPTH:
                return None
            layer = {c for c in remaining if not (closures.get(c, set()) & remaining)}
            if not layer:  # cycle
                return None
            remaining -= layer
        return n + 1  # + the course itself

    open_courses, locked_courses, elective_slots, done, in_progress = [], [], [], [], []
    for code, i in sorted(info.items(), key=lambda kv: (kv[1]["term"], kv[0])):
        row = {k: i[k] for k in ("code", "name", "credits", "term", "type")}
        if i["status"] == "passed":
            done.append(row)
            continue
        if i["status"] == "studying":
            in_progress.append(row)
            continue
        if i["placeholder"]:
            elective_slots.append(row)
            continue
        if i["open"]:
            open_courses.append({**row, "fits_this_term": code in fits})
            continue

        # ── locked: explain it ──
        reasons = []
        for m in i["missing"]:
            sub = info.get(m)
            if sub is None:
                reasons.append({"kind": "UNKNOWN_PREREQ", "code": m})
            else:
                own = (
                    "open"
                    if sub["open"]
                    else sub["status"]
                    if sub["status"] != "not_taken"
                    else "locked"
                )
                reasons.append(
                    {"kind": "MISSING_COURSE", "code": m, "name": sub["name"], "own_status": own}
                )
        if i["gate"] is not None and not i["gate"]["met"]:
            reasons.append({"kind": "MISSING_HOURS", **i["gate"]})
        if not reasons:
            reasons.append({"kind": "ASK_ADVISOR"})

        cl = closures.get(code, set())
        reachable = [info[c] for c in cl if c in info and info[c]["open"]]
        nearest = min(reachable, key=lambda x: (x["term"], x["code"])) if reachable else None
        hours_only = not any(r["kind"] == "MISSING_COURSE" for r in reasons)
        # "N steps" counts course passes. A course blocked only by a credit-hour gate
        # has no course chain at all, so claiming "1 step" would be a lie.
        st = None if hours_only else steps_to(code)
        locked_courses.append(
            {
                **row,
                "steps": st,
                "reasons": reasons,
                "nearest_open": {"code": nearest["code"], "name": nearest["name"]}
                if nearest
                else None,
                "hours_only": hours_only,
                "frees_eventually": 0,  # filled below
            }
        )

    # ── the three forward relations, computed once for every course in the plan ──
    #
    # This is the FOURTH place the reverse relation was written down: the advisor
    # capability, `course_detail`, `student_home_cards.unlock_leaders` and here.
    # Three of them derived a different answer from the same graph and published it
    # under the same word. See the module docstring for what each one means.
    dependents: dict[str, dict] = {}
    for code in info:
        rows = []
        for other, oi in info.items():
            if code not in oi["course_prereqs"]:
                continue
            # `missing` is already "prerequisites not passed and not being studied",
            # so subtracting `code` leaves exactly what would STILL be outstanding
            # the day this course is passed.
            outstanding = sorted(set(oi["missing"]) - {code})
            # A credit-hour gate is outstanding too. `unlock_leaders` compared course
            # codes only, so a capstone gated on 146 hours counted as "waiting on this
            # one alone" while the hours it is actually waiting for went unmentioned.
            hours_short = oi["gate"] is not None and not oi["gate"]["met"]
            rows.append(
                {
                    "code": other,
                    "name": oi["name"],
                    "status": oi["status"],
                    "also_waiting_on": outstanding,
                    "also_waiting_on_credit_hours": hours_short,
                    # NOT "unlocked by": the claim is about what this course is
                    # waiting for, which is a fact about the prerequisite records.
                    # Whether it is then offered, permitted or seated is not knowable
                    # here and is not claimed anywhere downstream of this flag.
                    "waiting_only_on_this": (
                        oi["status"] == "not_taken"
                        and not oi["placeholder"]
                        and code in oi["missing"]
                        and not outstanding
                        and not hours_short
                    ),
                }
            )
        rows.sort(key=lambda r: (not r["waiting_only_on_this"], r["code"]))
        dependents[code] = {
            "listed": rows,
            "waiting_only_on_this": [r["code"] for r in rows if r["waiting_only_on_this"]],
            "on_chain_of_count": sum(1 for c, cl in closures.items() if code in cl),
        }

    for row in locked_courses:
        row["frees_eventually"] = dependents[row["code"]]["on_chain_of_count"]

    blockers = [
        {
            "code": c,
            "name": info[c]["name"],
            "frees_now": len(dependents[c]["waiting_only_on_this"]),
            "frees_eventually": dependents[c]["on_chain_of_count"],
        }
        for c, i in info.items()
        # Placeholders excluded. `open` does not exclude them — «PROGRAM ELECTIVE
        # COURSE I» satisfies "not taken, nothing missing" — so the ranked list came
        # out with six choose-one-with-your-adviser slots sitting at zero impact
        # among the real courses. They cannot be passed, so they cannot free
        # anything, and offering them as candidates is the elective-placeholder
        # confusion this module already refuses everywhere else.
        if i["open"] and not i["placeholder"]
    ]
    top_blocker = max(
        blockers, key=lambda b: (b["frees_eventually"], b["frees_now"], b["code"]), default=None
    )
    if top_blocker and top_blocker["frees_eventually"] == 0:
        top_blocker = None

    open_courses.sort(key=lambda r: (not r["fits_this_term"], r["term"], r["code"]))
    locked_courses.sort(
        key=lambda r: (r["steps"] is None, r["steps"] or 99, -r["frees_eventually"], r["code"])
    )

    # ── payload for the personalised dependency graph ──
    # Nodes come from prerequisite edges only, so a course with no prerequisite row
    # would be invisible; extra_nodes puts the whole plan on the map.
    status_of = {}
    for code, i in info.items():
        status_of[code] = (
            i["status"]
            if i["status"] in ("passed", "studying")
            else "open"
            if i["open"]
            else "locked"
        )
    graph = {
        "items": [
            {"course_code": c, "prerequisite_course_code": p}
            for c, i in info.items()
            for p in i["course_prereqs"]
        ],
        "termOf": {c: i["term"] for c, i in info.items() if i["term"]},
        "nameOf": {c: i["name"] for c, i in info.items() if i["name"]},
        "statusOf": status_of,
        "extraNodes": sorted(info),
    }

    return {
        "graph": graph,
        "dependents": dependents,
        # Every open course with its two forward counts, not just the winner.
        # `top_blocker` is a `max()` over this list and answers only "which one
        # course", so a question that ranks three named courses, or asks which opens
        # the most DIRECTLY rather than over the whole chain, had nothing to read.
        "blockers": sorted(
            (b for b in blockers if b["frees_now"] or b["frees_eventually"]),
            key=lambda b: (-b["frees_now"], -b["frees_eventually"], b["code"]),
        ),
        "program": program,
        "counts": {
            "open": len(open_courses),
            "one_step": sum(1 for r in locked_courses if r["steps"] == 2),
            "locked": len(locked_courses),
            "passed": len(done),
            "studying": len(in_progress),
        },
        "top_blocker": top_blocker,
        "open_courses": open_courses,
        "elective_slots": elective_slots,
        "locked_courses": locked_courses,
        "done": done,
        "in_progress": in_progress,
        "no_record_at_all": not done and not in_progress,
    }
