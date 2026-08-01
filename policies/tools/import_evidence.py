"""Import the second independent extraction as the evidence layer.

DESIGN RULE: import the content UNMODIFIED. The only edits are (a) renaming one
misleading field and (b) adding annotations. Every substantive disagreement with
the rule layer is RECORDED, never patched — because two independently worded
extractions of the same page are a cross-check, and silently reconciling them
destroys the only signal that catches a misreading.

Three things this script does that the source files do not:

1. `source_excerpt_ar` -> `restatement_ar`, plus `is_verbatim: false`.
   The field is byte-identical to the record's own `statement_ar` in 273/277
   records, contains zero Arabic-Indic digits where the page prints them, and is
   demonstrably stale in the four records the upstream audit corrected. It is a
   restatement. Calling it an excerpt invites a future consumer to cite it.

2. A content hash per record, so a `derived_from` pointer fails loudly if the
   evidence it names later changes meaning under the same id. Upstream ids are
   positional (TU-GUIDE-0001..0277 in page order) and have already changed meaning
   once without changing id.

3. Contested / unverified flags, so the defects found in review survive import
   instead of being laundered by it.

Usage:  python policies/tools/import_evidence.py
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = pathlib.Path(r"C:\Users\user\Downloads")
OUT = ROOT / "evidence"

KNOWLEDGE = SRC / "knowledge_records(1).yaml"
POLICY_SUBSET = SRC / "policy_records(1).yaml"
INDEX_CSV = SRC / "knowledge_index (1).csv"
AUDIT = SRC / "source_alignment_audit_v1_1.md"
REPORT = SRC / "extraction_report.md"
UPSTREAM_SOURCES = SRC / "sources(1).yaml"

# Defects found in adversarial review (2026-08-01). Recorded, not repaired: these
# belong to the upstream extraction, and patching them here would erase the
# divergence that exposed them.
CONTESTED: dict[str, str] = {
    "TU-GUIDE-0135": (
        "values.english_code = W is not supported by the page it cites. Printed p.24 "
        "contains no Latin characters at all; W appears only on p.29 against منسحب بعذر. "
        "The rule layer records this as an unresolved open_question "
        "(TU.EXCUSE.TRANSCRIPT_MARK / TU.GRADE.SPECIAL_SYMBOLS). Do not adopt either reading."
    ),
    "TU-GUIDE-0183": (
        "STALE RESTATEMENT: upstream v1.1 corrected statement_ar to 'من 95 إلى 100' but "
        "left the excerpt field reading 'من 95 إلى أقل من 100' — the exact text its own "
        "audit calls the v1 error. The record now contradicts itself."
    ),
    "TU-GUIDE-0191": (
        "STALE RESTATEMENT: upstream v1.1 removed the fabricated lower bound of 0 from "
        "statement_ar but left it in the excerpt field ('من 0 إلى أقل من 60'). Printed "
        "p.28 shows only 'أقل من 60'; there is no 0 on the page."
    ),
    "TU-GUIDE-0227": "STALE RESTATEMENT: v1.1 corrected statement_ar; excerpt field not resynced.",
    "TU-GUIDE-0235": "STALE RESTATEMENT: v1.1 corrected statement_ar; excerpt field not resynced.",
}

# Upstream normalises وحدة (credit unit) and ساعة (contact hour) to one unit on the
# p.23 load records. The rule layer keeps them apart deliberately — the distinction
# is one reading of the unresolved 16-vs-19 question.
UNIT_DIVERGENCE = ["TU-GUIDE-0128", "TU-GUIDE-0129", "TU-GUIDE-0130"]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    for required in (KNOWLEDGE, INDEX_CSV, AUDIT, REPORT, UPSTREAM_SOURCES, POLICY_SUBSET):
        if not required.exists():
            sys.exit(f"missing upstream artefact: {required}")

    OUT.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(KNOWLEDGE.read_text(encoding="utf-8"))
    records = raw["records"] if isinstance(raw, dict) else raw

    subset_ids = {
        r["record_id"]
        for r in (yaml.safe_load(POLICY_SUBSET.read_text(encoding="utf-8")) or {}).get(
            "records", []
        )
    }

    out_records = []
    renamed = second_pass = 0
    excerpt_equals_statement = 0

    for rec in records:
        rid = rec["record_id"]
        new = dict(rec)

        excerpt = new.pop("source_excerpt_ar", None)
        if excerpt is not None:
            new["restatement_ar"] = excerpt
            new["is_verbatim"] = False
            renamed += 1
            if str(excerpt).strip() == str(new.get("statement_ar", "")).strip():
                excerpt_equals_statement += 1

        # Promoted on owner instruction 2026-08-01. The `contested` markers below are NOT
        # cleared by approval: approval grants authority to use a record, it does not
        # make a defective one correct.
        new["verification_status"] = "AUTHORITY_APPROVED"
        new["approved_by"] = "project_owner"
        new["approved_at"] = "2026-08-01"

        new["content_sha256"] = sha256_text(
            f"{new.get('statement_ar', '')}|{new.get('restatement_ar', '')}|{new.get('values', '')}"
        )

        aligned = "source_alignment" in rec
        new["second_pass_checked"] = aligned
        if aligned:
            second_pass += 1

        new["in_upstream_policy_subset"] = rid in subset_ids

        if rid in CONTESTED:
            new["contested"] = {
                "status": "CONTESTED",
                "raised_by": "adversarial review 2026-08-01",
                "reason": CONTESTED[rid],
            }
        if rid in UNIT_DIVERGENCE:
            new["divergence_note"] = (
                "unit normalised to credit_hours upstream; the page says وحدة (credit "
                "units) for this record. The rule layer keeps وحدة/ساعة distinct. Do not "
                "let this value override TU.LOAD.* units."
            )
        out_records.append(new)

    payload = {
        "schema_version": "1.0-imported",
        "import_note": (
            "Imported unmodified except: source_excerpt_ar renamed to restatement_ar with "
            "is_verbatim: false; content_sha256, second_pass_checked, "
            "in_upstream_policy_subset, contested and divergence_note added. No statement, "
            "value or page was altered."
        ),
        "records": out_records,
    }
    target = OUT / "knowledge_records.yaml"
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )

    for src, name in (
        (INDEX_CSV, "knowledge_index.csv"),
        (AUDIT, "source_alignment_audit_v1_1.md"),
        (REPORT, "extraction_report.md"),
        (UPSTREAM_SOURCES, "upstream_sources.yaml"),
    ):
        shutil.copyfile(src, OUT / name)

    manifest = {
        "evidence_set": "TU_STUDENT_GUIDE_V3_1447_EXTRACTION_B",
        "imported_at": "2026-08-01",
        "document_id": "TU_STUDENT_GUIDE_V3_1447",
        "source_file_sha256": "155b7df5ec782860cc047287919e2dcfbe78aa76759b9f0bed105bcad0a15227",
        "provenance": {
            "produced_by": "a second, independent LLM-assisted extraction (not this agent)",
            "upstream_revision": "1.1",
            "upstream_declares_extracted_by": "LLM-assisted extraction",
            "upstream_second_pass": "AI-assisted source alignment review, pages 23-39 only",
            "model_identity": "NOT RECORDED UPSTREAM — unknown model, unknown version",
            "human_verified": False,
        },
        "counts": {
            "records": len(out_records),
            "excerpt_fields_renamed": renamed,
            "restatement_identical_to_statement": excerpt_equals_statement,
            "second_pass_checked": second_pass,
            "never_second_pass_checked": len(out_records) - second_pass,
            "in_upstream_policy_subset": len(subset_ids),
            "contested": len(CONTESTED),
        },
        "artefacts": {
            name: sha256_file(OUT / name)
            for name in (
                "knowledge_records.yaml",
                "knowledge_index.csv",
                "source_alignment_audit_v1_1.md",
                "extraction_report.md",
                "upstream_sources.yaml",
            )
        },
        "dropped": {
            "file": "policy_records(1).yaml",
            "reason": (
                "Strict subset of knowledge_records filtered by normativity "
                "(NORMATIVE_SUMMARY + PROCEDURAL_SUMMARY). Verified: all 185 ids present "
                "in the 277 with no field-level differences and no unique content. "
                "Membership is preserved on each record as in_upstream_policy_subset, so "
                "the subset is reconstructible without keeping a second copy."
            ),
        },
        "known_defects": {
            "restatement_is_not_a_quotation": (
                "restatement_ar equals the record's own statement_ar in "
                f"{excerpt_equals_statement}/{len(out_records)} records, carries zero "
                "Arabic-Indic digits where the page prints them, and is stale in the four "
                "records the upstream audit corrected. It is NOT citable as source text. "
                "Cite evidence/page_text/ for verbatim wording."
            ),
            "positional_ids": (
                "Ids are contiguous and page-ordered with no allocation ledger. A "
                "re-extraction that inserts a record renumbers everything after it. "
                "derived_from pointers therefore carry content_sha256."
            ),
            "coverage_of_second_pass": (
                f"{len(out_records) - second_pass} records were never second-pass checked "
                "— the entire pp.1-21 block, including all student rights, all student "
                "duties, the registration-authority rules and the p.20 workflow records."
            ),
        },
    }
    (OUT / "MANIFEST.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )

    print(f"imported {len(out_records)} evidence records -> {target}")
    print(f"  renamed excerpt field:            {renamed}")
    print(f"  restatement == own statement:     {excerpt_equals_statement}")
    print(f"  second-pass checked:              {second_pass}")
    print(f"  NEVER second-pass checked:        {len(out_records) - second_pass}")
    print(f"  contested:                        {len(CONTESTED)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
