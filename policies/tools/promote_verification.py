"""Promote policy records through the approval stages on the owner's instruction.

Approval is about AUTHORITY — whether this project may operationalise a rule. It is
not a claim that an ambiguous source became unambiguous. So this script promotes the
verification status of every record and deliberately does NOT touch:

  * `open_question`   — a genuine ambiguity in the source text
  * `contested`       — a defect found in the evidence layer
  * `runtime_use`     — whether the DATA exists to evaluate the rule

Those are orthogonal to approval and survive it. A rule can be AUTHORITY_APPROVED and
still say "the source is unclear on this clause" or "your record cannot supply the
input". Collapsing approval into disambiguation would let the adviser assert readings
the page does not support — which is the failure the whole store was built to prevent.

Text-level edit, not a YAML round-trip, so comments and anchors survive.

Usage:  python policies/tools/promote_verification.py --approver "<name>" [--dry-run]
"""

from __future__ import annotations

import argparse
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

OLD_BLOCK = """    source_verified_by: null
    domain_reviewed_by: null
    authority_approved_by: null"""


def new_block(approver: str, date: str) -> str:
    return f"""    source_verified_by: {approver}
    source_verified_at: "{date}"
    domain_reviewed_by: {approver}
    domain_reviewed_at: "{date}"
    authority_approved_by: {approver}
    authority_approved_at: "{date}"
    approval_basis: >
      Owner instruction {date}. The project owner is acting as the authority for this
      system's own use of these records. This is NOT a statement that the Deanship of
      Admission and Registration has reviewed them, and it does not resolve any
      open_question or contested marker — those survive approval unchanged."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approver", required=True)
    ap.add_argument("--date", default="2026-08-01")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = [
        p
        for p in sorted(ROOT.rglob("*.yaml"))
        if p.name not in ("sources.yaml", "evidence_map.yaml")
        and not ({"evidence", "tools"} & set(p.parts))
    ]

    promoted = 0
    carried_open: list[str] = []
    low_confidence: list[str] = []
    still_blocked: list[str] = []

    for path in files:
        text = path.read_text(encoding="utf-8")
        count = text.count("    status: EXTRACTED")
        if count:
            text = text.replace("    status: EXTRACTED", "    status: AUTHORITY_APPROVED")
            text = text.replace(OLD_BLOCK, new_block(args.approver, args.date))
            promoted += count
            if not args.dry_run:
                path.write_text(text, encoding="utf-8")

        for rec in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            if not isinstance(rec, dict):
                continue
            pid = rec.get("policy_id", "?")
            if rec.get("open_question"):
                carried_open.append(pid)
            if rec.get("extraction_confidence") == "low":
                low_confidence.append(pid)
            if rec.get("runtime_use") == "PROHIBITED_FOR_DECISION":
                still_blocked.append(pid)

    verb = "would promote" if args.dry_run else "promoted"
    print(f"{verb} {promoted} records -> AUTHORITY_APPROVED (approver: {args.approver})")
    print("\napproval does NOT change these — they survive it deliberately:")
    print(f"  unresolved open_question   : {len(carried_open)}")
    for pid in carried_open:
        print(f"      {pid}")
    print(f"  extraction_confidence low  : {len(low_confidence)} {low_confidence}")
    print(f"  still PROHIBITED_FOR_DECISION (no data, not no approval): {len(still_blocked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
