"""Verify every rule's `calendar_binding` against the extracted calendar.

Three ways a binding goes wrong, all silent without this check:

  1. The dates in the rule drift from the calendar (someone edits one, not the other).
  2. A binding names an event that no longer exists after a re-capture.
  3. A binding presents a DEADLINE_ONLY event as a date RANGE — inventing an opening
     date the university never published. The source repeats the closing date in its
     من and إلى columns for those rows, so a range looks available when it is not.

Exit code 1 on any of them.

Usage:  python policies/tools/check_calendar_bindings.py
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CALENDAR_DIR = ROOT / "calendar"

WINDOW_KEYS = ("window", "add_drop")  # binding shapes that assert a from..to range


def norm(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def load_calendar_events() -> dict[str, dict]:
    events: dict[str, dict] = {}
    for path in sorted(CALENDAR_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for ev in doc.get("events", []):
            events[norm(ev["event_ar"])] = ev
    return events


def iter_bindings():
    for path in sorted(ROOT.rglob("*.yaml")):
        if path.name in ("sources.yaml", "evidence_map.yaml"):
            continue
        if {"evidence", "tools", "calendar"} & set(path.parts):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            continue
        for rec in data:
            binding = rec.get("calendar_binding")
            if binding:
                yield rec["policy_id"], binding


def binding_events(binding: dict) -> list[dict]:
    """A binding names one event, or several under `events`."""
    if "events" in binding:
        return list(binding["events"])
    return [binding]


def main() -> int:
    events = load_calendar_events()
    if not events:
        sys.exit(f"no calendar events found under {CALENDAR_DIR}")

    problems: list[str] = []
    checked = 0

    for policy_id, binding in iter_bindings():
        for entry in binding_events(binding):
            title = norm(entry.get("event_ar"))
            if not title:
                # Shape-only bindings (e.g. add_drop / term_start) name no event.
                continue
            checked += 1
            ev = events.get(title)
            if ev is None:
                near = [t for t in events if title[:28] and title[:28] in t]
                problems.append(
                    f"{policy_id}: names an event absent from the calendar\n"
                    f"    {title[:88]}\n"
                    f"    nearest: {near[0][:88] if near else '(none)'}"
                )
                continue

            stated = entry.get("deadline") or {}
            if stated:
                for field, cal_field in (("gregorian", "gregorian"), ("hijri", "hijri")):
                    want = norm(ev["ends"][cal_field])
                    got = norm(stated.get(field))
                    if got and got != want:
                        problems.append(
                            f"{policy_id}: {field} deadline disagrees with the calendar\n"
                            f"    rule={got!r}  calendar={want!r}"
                        )

            if ev["window_completeness"] == "DEADLINE_ONLY" and any(
                k in entry for k in WINDOW_KEYS
            ):
                problems.append(
                    f"{policy_id}: presents a DEADLINE_ONLY event as a date range.\n"
                    f"    The source publishes only the closing date for\n"
                    f"    {title[:80]}\n"
                    f"    A range here invents an opening date."
                )

    print(f"calendar bindings checked: {checked}")
    for p in problems:
        print(f"\n  FAIL {p}")
    print(f"\n{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
